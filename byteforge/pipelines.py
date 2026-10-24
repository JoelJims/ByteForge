"""Standalone archives for genuine Parquet and token streams + native BtrBlocks."""

import hashlib
import json
import shutil
import struct
import time
from contextlib import ExitStack
from pathlib import Path

from byteforge.dataset import (
    MAGIC,
    MAX_PAYLOAD,
    MAX_ROWS,
    U64,
    checksum,
    dump_json,
    row_windows,
)
from byteforge.native import NativeIntegers
from byteforge.query_index import build_filter, decode_filter
from byteforge.representation import leaf_blocks, pack_unsigned, unpack_unsigned
from byteforge.sgtt import require_sgtt
from byteforge.tokenizers import Tokenizer, encode_rows

FORMAT = "byteforge-hdfs-archive-v1"
BLOCK_FORMAT = "byteforge-hdfs-block-archive-v2"
BLOCK_ROWS = 256
MAX_BLOCK_NATIVE_BYTES = 321 << 20
MAX_BLOCK_FRAME_BYTES = MAX_BLOCK_NATIVE_BYTES + (1 << 20)
PARQUET_SETTINGS = {
    "compression": "zstd",
    "compression_level": 3,
    "version": "2.6",
    "data_page_version": "1.0",
    "use_dictionary": True,
    "data_page_size": 1 << 20,
    "dictionary_pagesize_limit": 1 << 20,
    "write_statistics": True,
}


class TestByteEncoder:
    """Infrastructure fixture only. Not LLM-derived and never a production B result."""

    identity = "test-only-byte-symbols-v1"
    vocabulary = tuple(bytes([value]) for value in range(256))
    test_only = True

    def encode(self, value: str) -> tuple[int, ...]:
        return tuple(value.encode("utf-8"))


def read_json(path: Path, limit: int = 64 << 20) -> dict:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("Invalid/oversized metadata file")
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Expected metadata object")
    return value


def prepared_manifest(prepared: Path) -> dict:
    manifest = read_json(prepared / "dataset.json")
    if checksum(prepared / "rows.bin") != manifest["canonical_sha256"]:
        raise ValueError("Canonical input checksum mismatch")
    return manifest


def finish(output: Path, manifest: dict) -> dict:
    manifest["files"] = {
        path.name: {"bytes": path.stat().st_size, "sha256": checksum(path)}
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    dump_json(output / "manifest.json", manifest)
    return manifest


def bounded_integer(value, name: str, maximum: int = 2**64 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def validate_dataset(dataset: dict) -> None:
    if not isinstance(dataset, dict):
        raise ValueError("Invalid dataset metadata")
    for name in ("rows", "payload_bytes", "canonical_file_bytes", "source_file_bytes"):
        bounded_integer(dataset.get(name), name)
    rows, payload = dataset["rows"], dataset["payload_bytes"]
    final = dataset.get("final_lf")
    if type(final) is not bool or (not rows and final):
        raise ValueError("Invalid final newline metadata")
    if dataset["canonical_file_bytes"] != 16 + 8 * rows + payload:
        raise ValueError("Inconsistent canonical length")
    if dataset["source_file_bytes"] != payload + max(0, rows - 1) + int(final):
        raise ValueError("Inconsistent source length")
    for name in ("canonical_sha256", "logical_sha256", "source_sha256"):
        value = dataset.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError(f"Invalid {name}")


def validate_index(index: dict, data_bytes: int, dataset_rows: int) -> None:
    """Validate the entire directory before starting native decompression."""
    if type(index.get("version")) is not int or index["version"] != 1:
        raise ValueError("Invalid index/version")
    if not isinstance(index.get("windows"), list):
        raise ValueError("Invalid windows")
    position = rows = 0
    for window in index["windows"]:
        if not isinstance(window, dict):
            raise ValueError("Invalid window")
        count = bounded_integer(window.get("rows"), "window rows", MAX_ROWS)
        tokens = bounded_integer(window.get("tokens"), "window tokens", MAX_PAYLOAD)
        rows += count
        if not count or rows > dataset_rows:
            raise ValueError("Invalid window row count")
        for name, total in (("ids", tokens), ("offsets", count + 1)):
            if not isinstance(window.get(name), list):
                raise ValueError("Invalid stream directory")
            elements = 0
            for leaf in window[name]:
                if not isinstance(leaf, dict):
                    raise ValueError("Invalid leaf")
                n = bounded_integer(leaf.get("elements"), "leaf elements", 65536)
                size = bounded_integer(leaf.get("bytes"), "leaf bytes", 4 << 20)
                offset = bounded_integer(leaf.get("offset"), "leaf offset")
                bounded_integer(leaf.get("scheme"), "scheme", 5)
                elements += n
                if not n or size < 32 or offset != position or elements > total:
                    raise ValueError("Invalid leaf directory")
                position += size
                if position > data_bytes:
                    raise ValueError("Truncated native data")
            if elements != total:
                raise ValueError("Stream element count mismatch")
    if rows != dataset_rows or position != data_bytes:
        raise ValueError("Index row/data length mismatch")


def validate_block_index(index: dict, data_bytes: int, dataset: dict) -> None:
    """Validate v2 row ranges and block-local native offsets before decoding."""
    if type(index.get("version")) is not int or index["version"] != 2:
        raise ValueError("Unsupported block index version")
    blocks = index.get("blocks")
    block_rows = index.get("block_rows")
    if (
        not isinstance(blocks, list)
        or type(index.get("rows")) is not int
        or index["rows"] != dataset["rows"]
        or type(block_rows) is not int
        or not 1 <= block_rows <= BLOCK_ROWS
        or type(data_bytes) is not int
        or data_bytes < 0
    ):
        raise ValueError("Invalid block directory")
    if index.get("pruning") != {"kind": "byte-trigram-bloom", "version": 1}:
        raise ValueError("Unsupported block pruning metadata")
    row_start = file_position = payload_total = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Invalid block metadata")
        rows = bounded_integer(block.get("rows"), "block rows", BLOCK_ROWS)
        start = bounded_integer(block.get("row_start"), "block row start")
        tokens = bounded_integer(block.get("tokens"), "block token count", MAX_PAYLOAD)
        payload = bounded_integer(block.get("payload_bytes"), "block payload", MAX_PAYLOAD)
        empty_rows = bounded_integer(block.get("empty_rows"), "empty row count", rows)
        empty_ids = block.get("empty_row_ids")
        frame_offset = bounded_integer(block.get("data_offset"), "block data offset")
        frame_bytes = bounded_integer(
            block.get("frame_bytes"), "block frame bytes", MAX_BLOCK_FRAME_BYTES
        )
        native_bytes = bounded_integer(
            block.get("native_bytes"), "block native bytes", MAX_BLOCK_NATIVE_BYTES
        )
        digest = block.get("frame_sha256")
        if (
            not rows
            or start != row_start
            or frame_offset != file_position
            or not frame_bytes
            or not native_bytes
            or not isinstance(empty_ids, list)
            or len(empty_ids) != empty_rows
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("Invalid block bounds or checksum")
        if any(
            type(row_id) is not int
            or not 0 <= row_id < rows
            or (pos > 0 and row_id <= empty_ids[pos - 1])
            for pos, row_id in enumerate(empty_ids)
        ):
            raise ValueError("Invalid empty-row index")
        leaves_position = 0
        for name, element_count in (("ids", tokens), ("offsets", rows + 1)):
            leaves = block.get(name)
            if not isinstance(leaves, list):
                raise ValueError("Invalid block leaf list")
            elements = 0
            for leaf in leaves:
                if not isinstance(leaf, dict):
                    raise ValueError("Invalid block leaf")
                count = bounded_integer(leaf.get("elements"), "leaf elements", 65536)
                size = bounded_integer(leaf.get("bytes"), "leaf bytes", 4 << 20)
                offset = bounded_integer(leaf.get("offset"), "leaf offset")
                bounded_integer(leaf.get("scheme"), "scheme", 5)
                if not count or size < 32 or offset != leaves_position:
                    raise ValueError("Invalid block leaf bounds")
                elements += count
                leaves_position += size
                if elements > element_count or leaves_position > native_bytes:
                    raise ValueError("Block leaf stream exceeds declared length")
            if elements != element_count:
                raise ValueError("Block leaf element count mismatch")
        if leaves_position != native_bytes:
            raise ValueError("Block native length mismatch")
        decode_filter(block.get("trigram_bloom"))
        row_start += rows
        file_position += frame_bytes
        payload_total += payload
        if row_start > dataset["rows"] or file_position > data_bytes:
            raise ValueError("Block directory exceeds archive bounds")
    if (
        row_start != dataset["rows"]
        or file_position != data_bytes
        or payload_total != dataset["payload_bytes"]
        or (dataset["rows"] == 0) != (len(blocks) == 0)
    ):
        raise ValueError("Block directory does not cover dataset")


def load_archive(archive: Path) -> dict:
    manifest = read_json(archive / "manifest.json")
    archive_format = manifest.get("format")
    if archive_format not in (FORMAT, BLOCK_FORMAT) or manifest.get("pipeline") not in ("A", "B"):
        raise ValueError("Unsupported archive format/pipeline")
    if archive_format == BLOCK_FORMAT and manifest.get("pipeline") != "B":
        raise ValueError("Block archive format is valid only for pipeline B")
    validate_dataset(manifest.get("dataset"))
    if manifest["pipeline"] == "B":
        tokenizer = manifest.get("tokenizer")
        if not isinstance(tokenizer, dict) or tokenizer.get("test_only") is not True:
            require_sgtt()
        if tokenizer.get("identity") != TestByteEncoder.identity:
            raise ValueError("Unsupported test-only encoder identity")
    if manifest["pipeline"] == "A":
        expected = {"data.parquet"}
    elif archive_format == BLOCK_FORMAT:
        if manifest.get("outer_codec") != {
            "name": "zstd-per-block",
            "level": 1,
            "checksum": True,
            "content_size": True,
        } or manifest.get("index_codec") != {
            "name": "zstd",
            "level": 1,
            "checksum": True,
            "content_size": True,
        }:
            raise ValueError("Unsupported block archive codec")
        expected = {"data.bin", "index.json.zst", "vocabulary.bin"}
    else:
        outer = manifest.get("outer_codec", {}).get("name", "none")
        if outer == "none":
            expected_data = "data.bin"
        elif outer == "zstd":
            metadata = manifest["outer_codec"]
            if (
                metadata.get("level") != 1
                or metadata.get("checksum") is not True
                or metadata.get("content_size") is not True
            ):
                raise ValueError("Unsupported outer Zstandard configuration")
            expected_data = "data.zst"
        else:
            raise ValueError("Unsupported outer codec")
        expected = {expected_data, "index.json", "vocabulary.bin"}
    if set(manifest["files"]) != expected or {p.name for p in archive.iterdir()} != expected | {
        "manifest.json"
    }:
        raise ValueError("Archive has missing or unexpected files")
    for name, entry in manifest["files"].items():
        path = archive / name
        if (
            not isinstance(entry, dict)
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != entry["bytes"]
            or (
                not (archive_format == BLOCK_FORMAT and name == "data.bin")
                and checksum(path) != entry["sha256"]
            )
        ):
            raise ValueError(f"Archive checksum/size mismatch: {name}")
    if archive_format == BLOCK_FORMAT:
        index_path = archive / "index.json.zst"
        if index_path.stat().st_size > 64 << 20:
            raise ValueError("Compressed block index exceeds limit")
        import zstandard as zstd

        try:
            index_raw = zstd.ZstdDecompressor().decompress(
                index_path.read_bytes(), max_output_size=256 << 20
            )
            index = json.loads(index_raw)
        except (zstd.ZstdError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Invalid compressed block index") from error
        if not isinstance(index, dict):
            raise ValueError("Expected block index object")
        validate_block_index(index, (archive / "data.bin").stat().st_size, manifest["dataset"])
        manifest["_validated_block_index"] = index
    return manifest


def build_parquet(prepared: Path, output: Path, *, profile: str = "standard") -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if profile not in ("standard", "fast-scan"):
        raise ValueError("Unknown Parquet profile")
    settings = dict(PARQUET_SETTINGS)
    if profile == "fast-scan":
        settings.update(compression_level=1, use_dictionary=False)
    start = time.perf_counter_ns()
    dataset = prepared_manifest(prepared)
    output.mkdir(parents=True, exist_ok=False)
    schema = pa.schema([pa.field("value", pa.string(), nullable=False)])
    codec_start = time.perf_counter_ns()
    with pq.ParquetWriter(output / "data.parquet", schema, **settings) as writer:
        for rows in row_windows(prepared / "rows.bin"):
            writer.write_table(
                pa.table({"value": pa.array(rows, type=pa.string())}, schema=schema),
                row_group_size=MAX_ROWS,
            )
    write_ns = time.perf_counter_ns() - codec_start
    manifest = finish(
        output,
        {
            "format": FORMAT,
            "pipeline": "A",
            "dataset": dataset,
            "schema": str(schema),
            "settings": {
                **settings,
                "max_rows_per_window": MAX_ROWS,
                "max_payload_per_window": MAX_PAYLOAD,
            },
            "dependencies": {
                "pyarrow": pa.__version__,
                "arrow_cpp": pa.cpp_version,
                "linked_zstd_version": None,
            },
        },
    )
    metadata = pq.read_metadata(output / "data.parquet")
    return {
        "encode_total_ns": time.perf_counter_ns() - start,
        "parquet_conversion_write_close_ns": write_ns,
        "codec_compressed_bytes": None,
        "codec_size_reason": "Parquet chunks include page headers; raw codec bytes not exposed",
        "parquet_file_bytes": manifest["files"]["data.parquet"]["bytes"],
        "column_chunk_compressed_bytes": sum(
            metadata.row_group(i).column(0).total_compressed_size
            for i in range(metadata.num_row_groups)
        ),
    }


def _vocabulary_bytes(vocabulary: tuple[bytes, ...]) -> bytes:
    if not 1 <= len(vocabulary) <= 65536 or any(type(v) is not bytes or not v for v in vocabulary):
        raise ValueError("Invalid token table")
    if 4 + sum(8 + len(v) for v in vocabulary) > MAX_PAYLOAD:
        raise ValueError("Vocabulary exceeds supported size")
    return struct.pack("<I", len(vocabulary)) + b"".join(U64.pack(len(v)) + v for v in vocabulary)


def _read_vocabulary(path: Path) -> tuple[bytes, ...]:
    if path.stat().st_size > MAX_PAYLOAD:
        raise ValueError("Vocabulary exceeds supported size")
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("Truncated vocabulary")
    count = struct.unpack_from("<I", data)[0]
    if not 1 <= count <= 65536:
        raise ValueError("Vocabulary count out of range")
    position, entries = 4, []
    for _ in range(count):
        if position + 8 > len(data):
            raise ValueError("Truncated vocabulary length")
        length = U64.unpack_from(data, position)[0]
        position += 8
        if not length or position + length > len(data):
            raise ValueError("Invalid vocabulary entry")
        entries.append(data[position : position + length])
        position += length
    if position != len(data):
        raise ValueError("Trailing vocabulary bytes")
    return tuple(entries)


def build_hybrid(
    prepared: Path,
    output: Path,
    tokenizer: Tokenizer,
    worker: Path,
    *,
    allow_test_encoder_on_real: bool = False,
    outer_codec: str = "none",
    max_payload_per_window: int = MAX_PAYLOAD,
) -> dict:
    if outer_codec not in ("none", "zstd"):
        raise ValueError("Unsupported BtrBlocks outer codec")
    if type(max_payload_per_window) is not int or not 1 <= max_payload_per_window <= MAX_PAYLOAD:
        raise ValueError("max_payload_per_window must be in 1..64 MiB")
    zstd_module = None
    if outer_codec == "zstd":
        import zstandard as zstd_module
    start = time.perf_counter_ns()
    dataset = prepared_manifest(prepared)
    test_only = bool(getattr(tokenizer, "test_only", False))
    if not test_only:
        require_sgtt()
    if (
        tokenizer.identity != TestByteEncoder.identity
        or tokenizer.vocabulary != TestByteEncoder.vocabulary
    ):
        raise ValueError("Unsupported test-only encoder identity/table")
    if test_only and dataset["kind"] != "synthetic" and not allow_test_encoder_on_real:
        raise ValueError("Test-only encoder is restricted to explicitly synthetic inputs")
    output.mkdir(parents=True, exist_ok=False)
    vocabulary = _vocabulary_bytes(tokenizer.vocabulary)
    (output / "vocabulary.bin").write_bytes(vocabulary)
    windows = []
    token_ns = serialization_ns = 0
    with NativeIntegers(worker) as native, (output / "data.bin").open("xb") as stream:
        for rows in row_windows(prepared / "rows.bin", max_payload=max_payload_per_window):
            phase = time.perf_counter_ns()
            encoded = encode_rows(rows, tokenizer)
            token_ns += time.perf_counter_ns() - phase
            window = {"rows": len(rows), "tokens": len(encoded.ids), "ids": [], "offsets": []}
            for name, values in (("ids", encoded.ids), ("offsets", encoded.offsets)):
                for leaf in leaf_blocks(values):
                    phase = time.perf_counter_ns()
                    raw = pack_unsigned(leaf, native_int32=True)
                    serialization_ns += time.perf_counter_ns() - phase
                    compressed, scheme = native.transform(1, len(leaf), raw)
                    entry = {
                        "offset": stream.tell(),
                        "bytes": len(compressed),
                        "elements": len(leaf),
                        "scheme": scheme,
                    }
                    stream.write(compressed)
                    window[name].append(entry)
            windows.append(window)
        native_ns, info, codec_bytes = native.phase_ns, native.info, native.codec_bytes
    native_path = output / "data.bin"
    native_data_bytes = native_path.stat().st_size
    outer_ns = 0
    if outer_codec == "zstd":
        phase = time.perf_counter_ns()
        compressed_path = output / "data.zst"
        compressor = zstd_module.ZstdCompressor(
            level=1, write_checksum=True, write_content_size=True
        )
        with native_path.open("rb") as source, compressed_path.open("xb") as target:
            with compressor.stream_writer(target, closefd=False) as stream:
                shutil.copyfileobj(source, stream, length=1 << 20)
        native_path.unlink()
        outer_ns = time.perf_counter_ns() - phase
    dump_json(output / "index.json", {"version": 1, "windows": windows})
    outer_metadata = None
    if outer_codec == "zstd":
        outer_metadata = {
            "name": "zstd",
            "level": 1,
            "checksum": True,
            "content_size": True,
            "library_version": zstd_module.__version__,
        }
    manifest = finish(
        output,
        {
            "format": FORMAT,
            "pipeline": "B",
            "dataset": dataset,
            "tokenizer": {
                "identity": tokenizer.identity,
                "test_only": test_only,
                "validation_scope": "archive infrastructure only; not SGTT"
                if test_only
                else "caller-provided tokenizer; inspect its configuration and rights",
                "configuration": getattr(tokenizer, "configuration", {}),
                "decode_assets": "complete ID-to-byte vocabulary.bin",
            },
            "native": info,
            **(
                {"outer_codec": outer_metadata, "native_data_bytes": native_data_bytes}
                if outer_metadata
                else {}
            ),
            "representation": "separate nonnegative int32 ID/offset leaves; little endian",
            "settings": {
                "leaf_elements": 65536,
                "max_rows_per_window": MAX_ROWS,
                "max_payload_per_window": max_payload_per_window,
                "selection": "upstream defaults",
            },
        },
    )
    return {
        "encode_total_ns": time.perf_counter_ns() - start,
        "tokenization_and_local_validation_ns": token_ns,
        "integer_serialization_ns": serialization_ns,
        "native_encode_ipc_ns": native_ns,
        "codec_compressed_bytes": codec_bytes,
        "codec_size_scope": "BtrBlocks encoded column data, including nested cascade metadata; "
        "excludes outer datablock headers and null maps",
        "native_block_bytes": native_data_bytes,
        "outer_compression_ns": outer_ns,
        "native_data_bytes": native_data_bytes,
        "archive_data_bytes": manifest["files"][
            "data.zst" if outer_codec == "zstd" else "data.bin"
        ]["bytes"],
    }


def build_hybrid_blocked(
    prepared: Path,
    output: Path,
    tokenizer: Tokenizer,
    worker: Path,
    *,
    allow_test_encoder_on_real: bool = False,
    block_rows: int = BLOCK_ROWS,
) -> dict:
    """Create independently compressed row blocks plus safe trigram filters."""
    if type(block_rows) is not int or not 1 <= block_rows <= BLOCK_ROWS:
        raise ValueError(f"block_rows must be in 1..{BLOCK_ROWS}")
    dataset = prepared_manifest(prepared)
    test_only = bool(getattr(tokenizer, "test_only", False))
    if not test_only:
        require_sgtt()
    if (
        tokenizer.identity != TestByteEncoder.identity
        or tokenizer.vocabulary != TestByteEncoder.vocabulary
    ):
        raise ValueError("Unsupported test-only encoder identity/table")
    if test_only and dataset["kind"] != "synthetic" and not allow_test_encoder_on_real:
        raise ValueError("Test-only encoder is restricted to explicitly synthetic inputs")
    import zstandard as zstd

    started = time.perf_counter_ns()
    output.mkdir(parents=True, exist_ok=False)
    (output / "vocabulary.bin").write_bytes(_vocabulary_bytes(tokenizer.vocabulary))
    compressor = zstd.ZstdCompressor(level=1, write_checksum=True, write_content_size=True)
    blocks, token_ns, serialization_ns = [], 0, 0
    source_row = frame_position = native_total = frame_total = 0
    with NativeIntegers(worker) as native, (output / "data.bin").open("xb") as data_stream:
        for rows in row_windows(prepared / "rows.bin", max_rows=block_rows):
            token_start = time.perf_counter_ns()
            encoded = encode_rows(rows, tokenizer)
            token_ns += time.perf_counter_ns() - token_start
            native_payload = bytearray()
            leaf_directories = {"ids": [], "offsets": []}
            for name, values in (("ids", encoded.ids), ("offsets", encoded.offsets)):
                for leaf in leaf_blocks(values):
                    serial_start = time.perf_counter_ns()
                    raw = pack_unsigned(leaf, native_int32=True)
                    serialization_ns += time.perf_counter_ns() - serial_start
                    compressed, scheme = native.transform(1, len(leaf), raw)
                    leaf_directories[name].append(
                        {
                            "offset": len(native_payload),
                            "bytes": len(compressed),
                            "elements": len(leaf),
                            "scheme": scheme,
                        }
                    )
                    native_payload.extend(compressed)
            if not native_payload:
                raise ValueError("A nonempty row block must contain its row-offset leaf")
            frame = compressor.compress(native_payload)
            row_bytes = [row.encode("utf-8", errors="strict") for row in rows]
            block_grams = {gram for data in row_bytes for gram in _row_trigrams(data)}
            block = {
                "row_start": source_row,
                "rows": len(rows),
                "tokens": len(encoded.ids),
                "payload_bytes": sum(map(len, row_bytes)),
                "empty_rows": sum(row == "" for row in rows),
                "empty_row_ids": [i for i, row in enumerate(rows) if row == ""],
                "data_offset": frame_position,
                "frame_bytes": len(frame),
                "frame_sha256": hashlib.sha256(frame).hexdigest(),
                "native_bytes": len(native_payload),
                **leaf_directories,
                "trigram_bloom": build_filter(block_grams),
            }
            data_stream.write(frame)
            blocks.append(block)
            source_row += len(rows)
            frame_position += len(frame)
            native_total += len(native_payload)
            frame_total += len(frame)
    index = {
        "version": 2,
        "rows": dataset["rows"],
        "block_rows": block_rows,
        "pruning": {"kind": "byte-trigram-bloom", "version": 1},
        "blocks": blocks,
    }
    index_raw = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    index_compressor = zstd.ZstdCompressor(level=1, write_checksum=True, write_content_size=True)
    with (output / "index.json.zst").open("xb") as index_stream:
        index_stream.write(index_compressor.compress(index_raw))
    manifest = finish(
        output,
        {
            "format": BLOCK_FORMAT,
            "archive_version": 2,
            "pipeline": "B",
            "dataset": dataset,
            "tokenizer": {
                "identity": tokenizer.identity,
                "test_only": test_only,
                "validation_scope": "archive infrastructure only; not SGTT",
                "configuration": getattr(tokenizer, "configuration", {}),
                "decode_assets": "complete ID-to-byte vocabulary.bin",
            },
            "native": native.info,
            "outer_codec": {
                "name": "zstd-per-block",
                "level": 1,
                "checksum": True,
                "content_size": True,
            },
            "index_codec": {
                "name": "zstd",
                "level": 1,
                "checksum": True,
                "content_size": True,
            },
            "representation": "per-row token IDs and token offsets; int32 little-endian leaves",
            "settings": {
                "block_rows": block_rows,
                "leaf_elements": 65536,
                "max_payload_per_block": MAX_PAYLOAD,
                "selection": "upstream defaults",
                "query_pruning": "byte trigram Bloom; false-positive only",
            },
        },
    )
    return {
        "encode_total_ns": time.perf_counter_ns() - started,
        "tokenization_ns": token_ns,
        "integer_serialization_ns": serialization_ns,
        "native_encode_ipc_ns": native.phase_ns,
        "block_count": len(blocks),
        "native_payload_bytes": native_total,
        "frame_bytes": frame_total,
        "index_bytes": manifest["files"]["index.json.zst"]["bytes"],
        "archive_total_bytes": sum(entry["bytes"] for entry in manifest["files"].values())
        + (output / "manifest.json").stat().st_size,
    }


def _row_trigrams(data: bytes):
    for pos in range(max(0, len(data) - 2)):
        yield data[pos : pos + 3]


def _decode_block_buffers(
    stream,
    block: dict,
    native: NativeIntegers,
    vocabulary: tuple[bytes, ...],
) -> tuple[bytes, tuple[int, ...], dict]:
    """Read, authenticate and decode exactly one independently compressed block."""
    import zstandard as zstd

    stream.seek(block["data_offset"])
    frame = stream.read(block["frame_bytes"])
    if (
        len(frame) != block["frame_bytes"]
        or hashlib.sha256(frame).hexdigest() != block["frame_sha256"]
    ):
        raise ValueError("Block frame is truncated or corrupt")
    try:
        native_payload = zstd.ZstdDecompressor().decompress(
            frame, max_output_size=block["native_bytes"]
        )
    except zstd.ZstdError as error:
        raise ValueError("Invalid compressed block frame") from error
    if len(native_payload) != block["native_bytes"]:
        raise ValueError("Block native payload length mismatch")
    stats = {
        "compressed_bytes_read": len(frame),
        "native_bytes_decompressed": len(native_payload),
        "integer_bytes_decoded": 0,
        "decoded_payload_bytes": 0,
    }

    def decode_leaves(name: str, *, compact_bytes: bool = False):
        output = bytearray()
        expected_elements = block["tokens"] if name == "ids" else block["rows"] + 1
        elements = 0
        for leaf in block[name]:
            start = leaf["offset"]
            compressed = native_payload[start : start + leaf["bytes"]]
            if len(compressed) != leaf["bytes"]:
                raise ValueError("Truncated native leaf")
            raw, scheme = native.transform(3 if compact_bytes else 2, leaf["elements"], compressed)
            if scheme != leaf["scheme"]:
                raise ValueError("Native block scheme mismatch")
            output.extend(raw)
            elements += leaf["elements"]
            stats["integer_bytes_decoded"] += leaf["elements"] * 4
        if elements != expected_elements:
            raise ValueError("Decoded block element count mismatch")
        return bytes(output)

    compact = native.info["protocol"] >= 3 and vocabulary == TestByteEncoder.vocabulary
    ids_raw = decode_leaves("ids", compact_bytes=compact)
    offsets_raw = decode_leaves("offsets")
    token_offsets = unpack_unsigned(offsets_raw)
    if (
        len(token_offsets) != block["rows"] + 1
        or token_offsets[0] != 0
        or token_offsets[-1] != block["tokens"]
        or any(a > b for a, b in zip(token_offsets, token_offsets[1:], strict=False))
    ):
        raise ValueError("Invalid block row offsets")
    payload = bytearray()
    if compact:
        payload.extend(ids_raw)
        byte_offsets = tuple(token_offsets)
    else:
        token_ids = unpack_unsigned(ids_raw)
        byte_offsets = [0]
        for row_start, row_end in zip(token_offsets, token_offsets[1:], strict=False):
            for token_id in token_ids[row_start:row_end]:
                if token_id >= len(vocabulary):
                    raise ValueError("Token ID outside archived vocabulary")
                payload.extend(vocabulary[token_id])
            byte_offsets.append(len(payload))
        byte_offsets = tuple(byte_offsets)
    if len(payload) != block["payload_bytes"]:
        raise ValueError("Decoded block payload size mismatch")
    stats["decoded_payload_bytes"] = len(payload)
    return bytes(payload), byte_offsets, stats


def _hybrid_rows(
    archive: Path,
    manifest: dict,
    worker: Path,
    phases: dict,
    *,
    native_session: NativeIntegers | None = None,
    as_bytes: bool = False,
):
    vocabulary = _read_vocabulary(archive / "vocabulary.bin")
    if vocabulary != TestByteEncoder.vocabulary:
        raise ValueError("Test-only vocabulary identity mismatch")
    if manifest["format"] == BLOCK_FORMAT:
        index = manifest["_validated_block_index"]
        with ExitStack() as stack:
            native = native_session or stack.enter_context(
                NativeIntegers(worker, expected=manifest["native"])
            )
            stream = stack.enter_context((archive / "data.bin").open("rb"))
            initial_native_ns = native.phase_ns
            try:
                for block in index["blocks"]:
                    payload, offsets, stats = _decode_block_buffers(
                        stream, block, native, vocabulary
                    )
                    for key, value in stats.items():
                        phases[key] = phases.get(key, 0) + value
                    phase = time.perf_counter_ns()
                    pieces = (payload[a:b] for a, b in zip(offsets, offsets[1:], strict=False))
                    rows = (
                        tuple(pieces)
                        if as_bytes
                        else tuple(piece.decode("utf-8", errors="strict") for piece in pieces)
                    )
                    phases["detokenization_validation_ns"] = (
                        phases.get("detokenization_validation_ns", 0)
                        + time.perf_counter_ns()
                        - phase
                    )
                    yield rows
            finally:
                phases["native_decompression_ipc_ns"] = native.phase_ns - initial_native_ns
        return
    index = read_json(archive / "index.json")
    outer = manifest.get("outer_codec", {}).get("name", "none")
    data_name = "data.zst" if outer == "zstd" else "data.bin"
    native_data_bytes = manifest.get("native_data_bytes", (archive / data_name).stat().st_size)
    validate_index(index, native_data_bytes, manifest["dataset"]["rows"])
    position = 0
    with ExitStack() as stack:
        native = native_session or stack.enter_context(
            NativeIntegers(worker, expected=manifest["native"])
        )
        initial_native_ns = native.phase_ns
        source = stack.enter_context((archive / data_name).open("rb"))
        if outer == "zstd":
            import zstandard as zstd

            stream = stack.enter_context(
                zstd.ZstdDecompressor().stream_reader(source, closefd=False)
            )
        else:
            stream = source
        for window in index["windows"]:
            if (
                type(window["rows"]) is not int
                or not 1 <= window["rows"] <= MAX_ROWS
                or not 0 <= window["tokens"] <= MAX_PAYLOAD
            ):
                raise ValueError("Invalid window bounds")
            decoded = {}
            for name, total in (("ids", window["tokens"]), ("offsets", window["rows"] + 1)):
                values = []
                byte_values = bytearray()
                for leaf in window[name]:
                    count, size = leaf["elements"], leaf["bytes"]
                    decoded_so_far = len(byte_values) if name == "ids" else len(values)
                    if (
                        type(count) is not int
                        or not 1 <= count <= 65536
                        or not 32 <= size <= 4 << 20
                        or leaf["offset"] != position
                        or decoded_so_far + count > total
                    ):
                        raise ValueError("Invalid leaf directory")
                    compressed = stream.read(size)
                    if len(compressed) != size:
                        raise ValueError("Truncated native data")
                    compact_bytes = name == "ids" and native.info["protocol"] >= 3
                    raw, scheme = native.transform(3 if compact_bytes else 2, count, compressed)
                    if scheme != leaf["scheme"]:
                        raise ValueError("Native scheme mismatch")
                    if compact_bytes:
                        byte_values.extend(raw)
                    else:
                        values.extend(unpack_unsigned(raw))
                    position += size
                compact_bytes = name == "ids" and native.info["protocol"] >= 3
                decoded_count = len(byte_values) if compact_bytes else len(values)
                if decoded_count != total:
                    raise ValueError("Stream element count mismatch")
                decoded[name] = bytes(byte_values) if compact_bytes else tuple(values)
            phase = time.perf_counter_ns()
            # This archive version accepts only the complete byte table above.
            # Protocol 3 checks each ID in native code; protocol 2 retains the
            # checked Python conversion for compatibility with older workers.
            payload = decoded["ids"] if type(decoded["ids"]) is bytes else bytes(decoded["ids"])
            offsets = decoded["offsets"]
            if (
                not offsets
                or offsets[0] != 0
                or offsets[-1] != len(payload)
                or any(a > b for a, b in zip(offsets, offsets[1:], strict=False))
            ):
                raise ValueError("Invalid row offsets")
            if len(payload) > MAX_PAYLOAD:
                raise ValueError("Decoded window exceeds payload limit")
            pieces = (payload[a:b] for a, b in zip(offsets, offsets[1:], strict=False))
            rows = (
                tuple(pieces)
                if as_bytes
                else tuple(piece.decode("utf-8", errors="strict") for piece in pieces)
            )
            phases["detokenization_validation_ns"] += time.perf_counter_ns() - phase
            yield rows
        if stream.read(1):
            raise ValueError("Unindexed trailing native bytes")
        phases["native_decompression_ipc_ns"] = native.phase_ns - initial_native_ns


def restore(archive: Path, output: Path, *, worker: Path | None = None) -> dict:
    start = time.perf_counter_ns()
    manifest = load_archive(archive)
    validation_ns = time.perf_counter_ns() - start
    phases = {"detokenization_validation_ns": 0}
    if manifest["pipeline"] == "A":
        import pyarrow as pa
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(archive / "data.parquet")
        expected = pa.schema([pa.field("value", pa.string(), nullable=False)])
        if not parquet.schema_arrow.equals(expected):
            raise ValueError("Parquet schema mismatch")
        batches = (
            tuple(batch.column(0).to_pylist())
            for batch in parquet.iter_batches(
                batch_size=MAX_ROWS, columns=["value"], use_threads=False
            )
        )
    else:
        if worker is None:
            raise FileNotFoundError("Native worker required for B archive")
        batches = _hybrid_rows(archive, manifest, worker, phases)
    count = payload = 0
    logical = hashlib.sha256()
    with output.open("xb") as stream:
        stream.write(MAGIC + U64.pack(manifest["dataset"]["rows"]))
        for rows in batches:
            for row in rows:
                if type(row) is not str:
                    raise ValueError("Unexpected null/missing output")
                data = row.encode("utf-8", errors="strict")
                length = U64.pack(len(data))
                logical.update(length)
                logical.update(data)
                stream.write(length)
                stream.write(data)
                payload += len(data)
                count += 1
    dataset = manifest["dataset"]
    if (
        count != dataset["rows"]
        or payload != dataset["payload_bytes"]
        or logical.hexdigest() != dataset["logical_sha256"]
        or checksum(output) != dataset["canonical_sha256"]
    ):
        raise ValueError("Reconstruction count/payload/checksum mismatch")
    return {
        **(phases if manifest["pipeline"] == "B" else {}),
        "restore_total_ns": time.perf_counter_ns() - start,
        "archive_validation_ns": validation_ns,
        "rows": count,
        "payload_bytes": payload,
        "correctness": True,
    }


def storage(archive: Path) -> dict:
    manifest = load_archive(archive)
    components = {p.name: p.stat().st_size for p in archive.iterdir()}
    total = sum(components.values())
    payload = manifest["dataset"]["payload_bytes"]
    return {
        "archive_total_bytes": total,
        "component_bytes": components,
        "payload_bytes": payload,
        "ratio": payload / total if payload else None,
        "saved_pct": 100 * (1 - total / payload) if payload else None,
    }

"""Standalone archives for genuine Parquet and token streams + native BtrBlocks."""

import hashlib
import json
import struct
import time
from pathlib import Path

from byteforge.dataset import (
    MAGIC,
    MAX_PAYLOAD,
    MAX_ROWS,
    U64,
    checksum,
    dump_json,
    row_windows,
    write_rows,
)
from byteforge.native import NativeIntegers
from byteforge.representation import TokenRows, leaf_blocks, pack_unsigned, unpack_unsigned
from byteforge.tokenizers import Tokenizer, encode_rows

FORMAT = "byteforge-hdfs-archive-v1"
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


def load_archive(archive: Path) -> dict:
    manifest = read_json(archive / "manifest.json")
    if manifest.get("format") != FORMAT or manifest.get("pipeline") not in ("A", "B"):
        raise ValueError("Unsupported archive format/pipeline")
    expected = (
        {"data.parquet"}
        if manifest["pipeline"] == "A"
        else {"data.bin", "index.json", "vocabulary.bin"}
    )
    if set(manifest["files"]) != expected or {p.name for p in archive.iterdir()} != expected | {
        "manifest.json"
    }:
        raise ValueError("Archive has missing or unexpected files")
    for name, entry in manifest["files"].items():
        path = archive / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != entry["bytes"]
            or checksum(path) != entry["sha256"]
        ):
            raise ValueError(f"Archive checksum/size mismatch: {name}")
    return manifest


def build_parquet(prepared: Path, output: Path) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    start = time.perf_counter_ns()
    dataset = prepared_manifest(prepared)
    output.mkdir(parents=True, exist_ok=False)
    schema = pa.schema([pa.field("value", pa.string(), nullable=False)])
    codec_start = time.perf_counter_ns()
    with pq.ParquetWriter(output / "data.parquet", schema, **PARQUET_SETTINGS) as writer:
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
                **PARQUET_SETTINGS,
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
) -> dict:
    start = time.perf_counter_ns()
    dataset = prepared_manifest(prepared)
    test_only = bool(getattr(tokenizer, "test_only", False))
    if test_only and dataset["kind"] != "synthetic" and not allow_test_encoder_on_real:
        raise ValueError("Test-only encoder is restricted to explicitly synthetic inputs")
    output.mkdir(parents=True, exist_ok=False)
    vocabulary = _vocabulary_bytes(tokenizer.vocabulary)
    (output / "vocabulary.bin").write_bytes(vocabulary)
    windows = []
    token_ns = serialization_ns = 0
    with NativeIntegers(worker) as native, (output / "data.bin").open("xb") as stream:
        for rows in row_windows(prepared / "rows.bin"):
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
    dump_json(output / "index.json", {"version": 1, "windows": windows})
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
            "representation": "separate nonnegative int32 ID/offset leaves; little endian",
            "settings": {
                "leaf_elements": 65536,
                "max_rows_per_window": MAX_ROWS,
                "max_payload_per_window": MAX_PAYLOAD,
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
        "native_block_bytes": manifest["files"]["data.bin"]["bytes"],
    }


def _hybrid_rows(archive: Path, manifest: dict, worker: Path, phases: dict):
    vocabulary = _read_vocabulary(archive / "vocabulary.bin")
    index = read_json(archive / "index.json")
    if index.get("version") != 1 or not isinstance(index.get("windows"), list):
        raise ValueError("Invalid index/version")
    position = 0
    with (
        NativeIntegers(worker, expected=manifest["native"]) as native,
        (archive / "data.bin").open("rb") as stream,
    ):
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
                for leaf in window[name]:
                    count, size = leaf["elements"], leaf["bytes"]
                    if (
                        type(count) is not int
                        or not 1 <= count <= 65536
                        or not 32 <= size <= 4 << 20
                        or leaf["offset"] != position
                        or len(values) + count > total
                    ):
                        raise ValueError("Invalid leaf directory")
                    compressed = stream.read(size)
                    if len(compressed) != size:
                        raise ValueError("Truncated native data")
                    raw, scheme = native.transform(2, count, compressed)
                    if scheme != leaf["scheme"]:
                        raise ValueError("Native scheme mismatch")
                    values.extend(unpack_unsigned(raw))
                    position += size
                if len(values) != total:
                    raise ValueError("Stream element count mismatch")
                decoded[name] = tuple(values)
            phase = time.perf_counter_ns()
            encoded = TokenRows(decoded["ids"], decoded["offsets"])
            encoded.validate(len(vocabulary))
            if sum(len(vocabulary[token]) for token in encoded.ids) > MAX_PAYLOAD:
                raise ValueError("Decoded window exceeds payload limit")
            rows = encoded.restore(vocabulary)
            phases["detokenization_validation_ns"] += time.perf_counter_ns() - phase
            yield rows
        if stream.read(1):
            raise ValueError("Unindexed trailing native bytes")
        phases["native_decompression_ipc_ns"] = native.phase_ns


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
                logical.update(U64.pack(len(data)) + data)
                payload += len(data)
                count += 1
            write_rows(stream, rows)
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

"""Executable dependency probes. This is not a benchmark runner."""

import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from byteforge.representation import pack_unsigned, unpack_unsigned

FIXTURE = ("", "hello", "hello", "a\x00b", "😀", "中文", "العربية", "é", "e\u0301", "a\nb")


def parquet_roundtrip(rows: tuple[str | None, ...]) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"value": pa.array(rows, type=pa.string())})
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        compression_level=3,
        version="2.6",
        data_page_version="1.0",
        use_dictionary=True,
        data_page_size=1 << 20,
        dictionary_pagesize_limit=1 << 20,
        write_statistics=True,
        row_group_size=65536,
    )
    data = sink.getvalue()
    restored = tuple(pq.read_table(pa.BufferReader(data), use_threads=False)["value"].to_pylist())
    if restored != rows:
        raise ValueError("Parquet exact round trip failed")
    metadata = pq.read_metadata(pa.BufferReader(data))
    columns = [metadata.row_group(i).column(0) for i in range(metadata.num_row_groups)]
    if any(column.compression != "ZSTD" for column in columns):
        raise ValueError("writer did not use ZSTD")
    return {
        "rows": len(rows),
        "parquet_file_bytes": len(data),
        "column_chunk_compressed_bytes": sum(c.total_compressed_size for c in columns),
        "encodings": [list(c.encodings) for c in columns],
        "note": "Column-chunk size includes page headers; not raw Zstd payload size.",
    }


def zstd_roundtrip(values: tuple[int, ...], width: int) -> dict:
    import zstandard as zstd

    raw = pack_unsigned(values, width)
    encoded = zstd.ZstdCompressor(level=3, write_checksum=True).compress(raw)
    restored = zstd.ZstdDecompressor().decompress(encoded, max_output_size=len(raw) + 1)
    if restored != raw or unpack_unsigned(restored, width) != values:
        raise ValueError("Zstd integer exact round trip failed")
    return {
        "elements": len(values),
        "width": width,
        "input_bytes": len(raw),
        "zstd_frame_bytes": len(encoded),
        "level": 3,
        "checksum": True,
    }


def collect() -> dict:
    report = {
        "kind": "compatibility_probe",
        "benchmark": False,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--porcelain"], text=True),
        "dependencies": {},
        "probes": {},
        "sgtt": {"status": "not_run", "reason": "Code/table reuse rights unresolved"},
        "btrblocks": {"status": "not_run", "reason": "Run the separate native probe"},
    }
    for package in ("pyarrow", "zstandard", "cmake", "ruff"):
        try:
            report["dependencies"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report["dependencies"][package] = None
    for name, probe in (
        (
            "parquet_zstd",
            lambda: [
                parquet_roundtrip(rows)
                for rows in ((), ("",), ("", "", ""), FIXTURE, (None, "", None, "中文"))
            ],
        ),
        (
            "zstd_integer_streams",
            lambda: [
                zstd_roundtrip(values, width)
                for width in (16, 32)
                for values in ((), (0, 1, 65535), tuple(range(65536)))
            ],
        ),
    ):
        try:
            report["probes"][name] = {"status": "valid", "cases": probe()}
        except ImportError as error:
            report["probes"][name] = {"status": "unavailable_dependency", "reason": str(error)}
        except Exception as error:
            report["probes"][name] = {"status": "execution_failed", "reason": repr(error)}
    try:
        import pyarrow as pa
        import zstandard as zstd

        report["dependencies"]["arrow_cpp"] = pa.cpp_version
        # The Python codec interface does not expose its linked Zstd version.
        report["dependencies"]["arrow_zstd_version"] = None
        report["dependencies"]["python_zstd_library"] = list(zstd.ZSTD_VERSION)
    except ImportError:
        pass
    return report


if __name__ == "__main__":
    result = collect()
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if len(sys.argv) == 2:
        output = Path(sys.argv[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as stream:
            stream.write(serialized)
    else:
        print(serialized, end="")
    sys.exit(0 if all(p["status"] == "valid" for p in result["probes"].values()) else 1)

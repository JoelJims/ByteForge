"""Streaming raw-LF log contract and bounded canonical row windows."""

import hashlib
import json
import os
import struct
from importlib.resources import files
from pathlib import Path

MAGIC = b"BFROWS01"
U64 = struct.Struct("<Q")
MAX_ROWS = 65536
MAX_PAYLOAD = 64 << 20
DEFAULT_HDFS = Path("data/loghub-2.0/HDFS/HDFS_full.log")
HDFS = {
    "dataset": "LogHub-2.0/HDFS",
    "release": "Zenodo 8275861, v1 (2023-08-23)",
    "official_repository": "https://github.com/logpai/loghub-2.0",
    "documentation_revision": "ac4aad2ea86f561e5a0acbc6587fa28b487259f9",
    "artifact_url": "https://zenodo.org/records/8275861/files/HDFS.zip",
    "artifact_name": "HDFS.zip",
    "raw_member": "HDFS/HDFS_full.log",
    "artifact_bytes": 321944794,
    "artifact_md5": "f23880dd4938379a535ab71a8d27a798",
    "published_rows": 11167740,
    "published_rows_scope": "README annotated HDFS logs; full raw count not locally verified",
    "zip_directory_raw_bytes": 1565682217,
    "format": "raw text, strict UTF-8 validation on ingestion",
    "license_url": "https://github.com/logpai/loghub-2.0/blob/main/LICENSE",
    "license_notice": files("byteforge").joinpath("assets/loghub-2.0-LICENSE.txt").read_text(),
    "rights": "Research/academic use; retain license notice, repository URL and citations. "
    "Zenodo metadata additionally declares CC-BY-4.0.",
    "citations": [
        "Jiang et al., A Large-scale Evaluation for Log Parsing Techniques: "
        "How Far are We?, ISSTA 2024",
        "Zhu et al., Loghub: A Large Collection "
        "of System Log Datasets for AI-driven Log Analytics, ISSRE 2023",
    ],
    "inspection": "Official metadata, ZIP directory and first partial member bytes only; "
    "full dataset not downloaded or validated during implementation.",
}
CONTRACT = {
    "version": "raw-lf-utf8-v1",
    "schema": "value: non-null UTF-8 string",
    "delimiter": "LF byte only; delimiter excluded from value; CR retained",
    "blank_lines": "one empty string per LF-delimited blank record",
    "final_newline": "terminal LF terminates the last record; no phantom extra row",
    "preprocessing": "none; BOM, NUL, whitespace, Unicode forms and order preserved",
}


def dump_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hdfs_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("BYTEFORGE_HDFS_PATH", DEFAULT_HDFS))


def require_raw(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(
            f"Raw HDFS file missing: {path}. Download HDFS.zip from "
            f"{HDFS['artifact_url']} and extract {HDFS['raw_member']}; "
            "use --path or BYTEFORGE_HDFS_PATH. No automatic download."
        )
    if path.suffix.lower() in (".csv", ".zip", ".gz"):
        raise ValueError("Expected extracted raw log text, not an archive or parsed CSV")


def write_rows(stream, rows) -> None:
    for row in rows:
        if type(row) is not str:
            raise ValueError("Primary raw-log contract excludes null/missing values")
        data = row.encode("utf-8", errors="strict")
        stream.write(U64.pack(len(data)))
        stream.write(data)


def row_windows(path: Path, *, max_rows: int = MAX_ROWS, max_payload: int = MAX_PAYLOAD):
    if not 1 <= max_rows <= MAX_ROWS or not 1 <= max_payload <= MAX_PAYLOAD:
        raise ValueError("Invalid canonical window bounds")
    with path.open("rb") as stream:
        if stream.read(8) != MAGIC:
            raise ValueError("Invalid canonical magic/version")
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("Truncated canonical header")
        remaining = U64.unpack(header)[0]
        rows, payload = [], 0
        for _ in range(remaining):
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("Truncated canonical row length")
            length = U64.unpack(header)[0]
            if length > max_payload:
                raise ValueError("Row exceeds supported window payload; no rows were dropped")
            data = stream.read(length)
            if len(data) != length:
                raise ValueError("Truncated canonical value")
            row = data.decode("utf-8", errors="strict")
            if rows and (len(rows) == max_rows or payload + length > max_payload):
                yield tuple(rows)
                rows, payload = [], 0
            rows.append(row)
            payload += length
        if rows:
            yield tuple(rows)
        if stream.read(1):
            raise ValueError("Trailing canonical data")


def prepare(path: Path, output: Path, *, synthetic: bool = False) -> dict:
    require_raw(path)
    output.mkdir(parents=True, exist_ok=False)
    source_hash, logical_hash = hashlib.sha256(), hashlib.sha256()
    count = payload = offset = 0
    last_lf = False
    try:
        with path.open("rb") as source, (output / "rows.bin").open("xb") as canonical:
            canonical.write(MAGIC + U64.pack(0))
            while physical := source.readline(MAX_PAYLOAD + 2):
                source_hash.update(physical)
                last_lf = physical.endswith(b"\n")
                value = physical[:-1] if last_lf else physical
                if len(value) > MAX_PAYLOAD:
                    raise ValueError(f"Row {count + 1}, source byte {offset}: exceeds 64 MiB")
                try:
                    value.decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise ValueError(
                        f"Invalid UTF-8 at row {count + 1}, source byte "
                        f"{offset + error.start}; dataset rejected"
                    ) from error
                framing = U64.pack(len(value))
                canonical.write(framing + value)
                logical_hash.update(framing + value)
                count += 1
                payload += len(value)
                offset += len(physical)
            canonical.seek(8)
            canonical.write(U64.pack(count))
        manifest = {
            "version": 1,
            "kind": "synthetic" if synthetic else "hdfs_raw_candidate",
            "provenance": {"synthetic": True} if synthetic else HDFS,
            "contract": CONTRACT,
            "source_path": str(path.resolve()),
            "source_sha256": source_hash.hexdigest(),
            "source_file_bytes": offset,
            "rows": count,
            "payload_bytes": payload,
            "matches_published_full_shape": None
            if synthetic
            else count == HDFS["published_rows"] and offset == HDFS["zip_directory_raw_bytes"],
            "final_lf": last_lf,
            "logical_sha256": logical_hash.hexdigest(),
            "canonical_sha256": checksum(output / "rows.bin"),
            "canonical_file_bytes": (output / "rows.bin").stat().st_size,
            "artifact_identity": "synthetic"
            if synthetic
            else "user-supplied raw file; archive checksum/provenance not independently verified",
        }
        dump_json(output / "dataset.json", manifest)
        return manifest
    except Exception as error:
        dump_json(
            output / "failure.json",
            {"status": "rejected", "reason": str(error), "accepted_rows_before_failure": count},
        )
        raise


def reconstruct_raw(canonical: Path, manifest: dict, output: Path) -> None:
    """Restore physical framing as well as logical values, including final LF state."""
    count = 0
    with output.open("xb") as stream:
        for rows in row_windows(canonical):
            for row in rows:
                if count:
                    stream.write(b"\n")
                stream.write(row.encode("utf-8"))
                count += 1
        if count != manifest["rows"]:
            raise ValueError("Reconstructed row count mismatch")
        if manifest["final_lf"]:
            stream.write(b"\n")
    if (
        output.stat().st_size != manifest["source_file_bytes"]
        or checksum(output) != manifest["source_sha256"]
    ):
        raise ValueError("Physical raw-log reconstruction checksum/size mismatch")

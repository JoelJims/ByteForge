"""Measure literal log searches directly on the uncompressed raw log file."""

import argparse
import hashlib
import json
import os
import statistics
import struct
import subprocess
import time
from array import array
from pathlib import Path

from byteforge.benchmark import environment
from byteforge.dataset import checksum, dump_json


def _records(path: Path):
    """Yield (row ID, bytes), removing only an LF record terminator."""
    with path.open("rb", buffering=1 << 20) as stream:
        row_id = 0
        for physical in stream:
            value = physical[:-1] if physical.endswith(b"\n") else physical
            yield row_id, value
            row_id += 1


def _matches(operation: str, needle: bytes, value: bytes) -> bool:
    if operation == "contains":
        return needle in value
    if operation == "equals":
        return needle == value
    return value.startswith(needle)


def _scan(path: Path, operation: str, needle: bytes):
    ids = []
    rows = payload = 0
    for row_id, value in _records(path):
        rows += 1
        payload += len(value)
        if _matches(operation, needle, value):
            ids.append(row_id)
    return ids, rows, payload


def _scan_native(executable: Path, path: Path, operation: str, needle: bytes):
    begin = time.perf_counter_ns()
    cpu_begin = time.process_time_ns()
    process = subprocess.run(
        [str(executable.resolve()), str(path.resolve()), operation, needle.hex()],
        capture_output=True,
        check=True,
    )
    parent_cpu = time.process_time_ns() - cpu_begin
    elapsed = time.perf_counter_ns() - begin
    if len(process.stdout) < 8:
        raise ValueError("Native raw scanner returned a truncated result")
    count = struct.unpack_from("<Q", process.stdout)[0]
    if len(process.stdout) != 8 + count * 8:
        raise ValueError("Native raw scanner result length mismatch")
    ids = array("Q")
    if ids.itemsize != 8:
        raise RuntimeError("Native scanner requires 64-bit array items")
    ids.frombytes(process.stdout[8:])
    if struct.pack("<Q", 1) != struct.pack("=Q", 1):
        ids.byteswap()
    return list(ids), json.loads(process.stderr), elapsed, parent_cpu


def _reference(path: Path, operation: str, needle: str):
    """Independent UTF-8 text reference scan; outside timed observations."""
    result = []
    rows = payload = 0
    with path.open("rb", buffering=1 << 20) as stream:
        for row_id, physical in enumerate(stream):
            value = physical[:-1] if physical.endswith(b"\n") else physical
            text = value.decode("utf-8", errors="strict")
            payload += len(value)
            rows += 1
            if (
                text == needle
                if operation == "equals"
                else text.startswith(needle)
                if operation == "prefix"
                else needle in text
            ):
                result.append(row_id)
    return result, rows, payload


def _digest(ids):
    h = hashlib.sha256()
    for row_id in ids:
        h.update(struct.pack("<Q", row_id))
    return h.hexdigest()


def run(args):
    if args.repetitions < 1 or args.warmups < 0:
        raise ValueError("Invalid warmup or repetition count")
    if not args.source.is_file():
        raise FileNotFoundError(args.source)
    if args.scanner in ("native", "both") and (
        args.native_scanner is None or not args.native_scanner.is_file()
    ):
        raise FileNotFoundError("Native scanner missing; compile probes/raw_scan/raw_query.cpp")
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    args.output.mkdir(parents=True, exist_ok=False)
    queries = (
        json.loads(args.queries_json.read_text())
        if args.queries_json
        else [
            {"id": "common-warning", "operation": "contains", "value": "warning"},
            {"id": "rare-failure", "operation": "contains", "value": "failure"},
            {"id": "absent-word", "operation": "contains", "value": "ByteForgeAbsentSentinel"},
        ]
    )
    if (
        not isinstance(queries, list)
        or not queries
        or len({q["id"] for q in queries}) != len(queries)
    ):
        raise ValueError("Queries must be a nonempty list with unique IDs")
    for query in queries:
        if (
            query.get("operation") not in ("contains", "equals", "prefix")
            or type(query.get("value")) is not str
        ):
            raise ValueError("Each query requires contains/equals/prefix and a string value")
        query["value"].encode("utf-8", errors="strict")

    source_hash = checksum(args.source)
    reference = {}
    identity = None
    for query in queries:
        expected, rows, payload = _reference(args.source, query["operation"], query["value"])
        if identity is None:
            identity = rows, payload
        elif identity != (rows, payload):
            raise ValueError("Source parsing changed between reference scans")
        reference[query["id"]] = expected

    dump_json(
        args.output / "run.json",
        {
            "environment": environment(),
            "benchmark": "raw-file-only literal row scans; no archive, codec, sidecar or index",
            "scanners": (
                ["python-lines"]
                if args.scanner == "python"
                else ["native-mmap"]
                if args.scanner == "native"
                else ["python-lines", "native-mmap"]
            ),
            "native_executable_sha256": (
                checksum(args.native_scanner) if args.scanner in ("native", "both") else None
            ),
            "source_path": str(args.source.resolve()),
            "source_sha256": source_hash,
            "source_bytes": args.source.stat().st_size,
            "rows": identity[0],
            "payload_bytes": identity[1],
            "line_contract": (
                "LF only is excluded; CR and all other bytes retained; final LF creates "
                "no phantom row; blank records are empty values"
            ),
            "queries": queries,
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "timing": "each request reopens and scans the raw file; OS cache uncontrolled",
            "result": (
                "ordered row IDs; timing includes row scanning and match-ID accumulation, "
                "excludes oracle/reference and digest"
            ),
            "seed": args.seed,
        },
    )
    scanners = (
        ["python-lines"]
        if args.scanner == "python"
        else ["native-mmap"]
        if args.scanner == "native"
        else ["python-lines", "native-mmap"]
    )
    trials = [
        (query, slot, scanner)
        for query in queries
        for slot in range(args.scans_per_query_per_round)
        for scanner in scanners
    ]
    import random

    rng = random.Random(args.seed)
    rows_seen = payload_seen = None
    with (args.output / "observations.jsonl").open("x", encoding="utf-8") as output:
        for round_id in range(args.warmups + args.repetitions):
            order = trials.copy()
            rng.shuffle(order)
            for query, scan_slot, scanner in order:
                needle = query["value"].encode("utf-8")
                if scanner == "python-lines":
                    begin = time.perf_counter_ns()
                    cpu_begin = time.process_time_ns()
                    ids, rows, payload = _scan(args.source, query["operation"], needle)
                    elapsed = time.perf_counter_ns() - begin
                    cpu = time.process_time_ns() - cpu_begin
                    diagnostics = {"scan_wall_ns": elapsed, "scan_cpu_ns": cpu}
                    parent_cpu = cpu
                else:
                    ids, diagnostics, elapsed, parent_cpu = _scan_native(
                        args.native_scanner, args.source, query["operation"], needle
                    )
                    rows, payload = diagnostics["rows"], diagnostics["payload_bytes"]
                if ids != reference[query["id"]]:
                    raise AssertionError(f"Reference mismatch for {query['id']}")
                if (rows, payload) != identity:
                    raise AssertionError("Input row/payload counts changed")
                rows_seen, payload_seen = rows, payload
                record = {
                    "round": round_id,
                    "warmup": round_id < args.warmups,
                    "track": "raw-file-scan",
                    "scanner": scanner,
                    "scan_slot": scan_slot,
                    "query_id": query["id"],
                    "operation": query["operation"],
                    "value": query["value"],
                    "request_ns": elapsed,
                    "process_cpu_ns": parent_cpu,
                    "scanner_wall_ns": diagnostics["scan_wall_ns"],
                    "scanner_cpu_ns": diagnostics["scan_cpu_ns"],
                    "rows_scanned": rows,
                    "payload_bytes_scanned": payload,
                    "result_count": len(ids),
                    "result_sha256": _digest(ids),
                    "correctness": True,
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
    observations = [
        json.loads(line) for line in (args.output / "observations.jsonl").read_text().splitlines()
    ]
    summaries = {}
    for query in queries:
        for scanner in scanners:
            samples = [
                r["request_ns"]
                for r in observations
                if not r["warmup"] and r["query_id"] == query["id"] and r["scanner"] == scanner
            ]
            summaries[f"{query['id']}/{scanner}"] = {
                "p50_ns": statistics.median(samples),
                "p95_ns": statistics.quantiles(samples, n=20, method="inclusive")[18]
                if len(samples) > 1
                else samples[0],
                "min_ns": min(samples),
                "max_ns": max(samples),
                "trials": len(samples),
            }
    dump_json(
        args.output / "summary.json",
        {
            "latencies": summaries,
            "correctness_failures": 0,
            "observations_sha256": checksum(args.output / "observations.jsonl"),
        },
    )
    dump_json(
        args.output / "complete.json",
        {
            "status": "complete",
            "queries": len(queries),
            "repetitions": args.repetitions,
            "observations": len(observations),
            "correctness_failures": 0,
            "rows": rows_seen,
            "payload_bytes": payload_seen,
        },
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--queries-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--scans-per-query-per-round", type=int, default=1)
    parser.add_argument("--scanner", choices=("python", "native", "both"), default="python")
    parser.add_argument("--native-scanner", type=Path)
    parser.add_argument("--seed", type=int, default=20261024)
    args = parser.parse_args()
    if args.scans_per_query_per_round < 1:
        parser.error("--scans-per-query-per-round must be positive")
    raise SystemExit(run(args))

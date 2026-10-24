"""Measure literal log searches directly on the uncompressed raw log file."""

import argparse
import hashlib
import json
import os
import statistics
import struct
import time
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
            "timing": (
                "each request reopens and sequentially scans the raw file; cold/warm "
                "describe first/repeated pass, OS cache uncontrolled"
            ),
            "result": (
                "ordered row IDs; timing includes row scanning and match-ID accumulation, "
                "excludes oracle/reference and digest"
            ),
            "seed": args.seed,
        },
    )
    trials = [(query, slot) for query in queries for slot in range(args.scans_per_query_per_round)]
    import random

    rng = random.Random(args.seed)
    rows_seen = payload_seen = None
    with (args.output / "observations.jsonl").open("x", encoding="utf-8") as output:
        for round_id in range(args.warmups + args.repetitions):
            order = trials.copy()
            rng.shuffle(order)
            for query, scan_slot in order:
                begin = time.perf_counter_ns()
                cpu_begin = time.process_time_ns()
                ids, rows, payload = _scan(
                    args.source, query["operation"], query["value"].encode("utf-8")
                )
                elapsed = time.perf_counter_ns() - begin
                cpu = time.process_time_ns() - cpu_begin
                if ids != reference[query["id"]]:
                    raise AssertionError(f"Reference mismatch for {query['id']}")
                if (rows, payload) != identity:
                    raise AssertionError("Input row/payload counts changed")
                rows_seen, payload_seen = rows, payload
                record = {
                    "round": round_id,
                    "warmup": round_id < args.warmups,
                    "track": "raw-file-scan",
                    "scan_slot": scan_slot,
                    "query_id": query["id"],
                    "operation": query["operation"],
                    "value": query["value"],
                    "request_ns": elapsed,
                    "process_cpu_ns": cpu,
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
        for track in ("raw-file-scan",):
            samples = [
                r["request_ns"]
                for r in observations
                if not r["warmup"] and r["query_id"] == query["id"] and r["track"] == track
            ]
            summaries[f"{query['id']}/{track}"] = {
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
    parser.add_argument("--seed", type=int, default=20261024)
    args = parser.parse_args()
    if args.scans_per_query_per_round < 1:
        parser.error("--scans-per-query-per-round must be positive")
    raise SystemExit(run(args))

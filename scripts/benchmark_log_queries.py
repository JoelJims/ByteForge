"""Exploratory literal-query latency: first request, streamed scans, bounded cache."""

import argparse
import json
import os
import random
import statistics
import time
from pathlib import Path

from byteforge.benchmark import environment
from byteforge.dataset import checksum, dump_json
from byteforge.native import worker_info
from byteforge.pipelines import load_archive, storage
from byteforge.query import QuerySession


def run(args):
    if args.repetitions < 1 or args.warmups < 0 or min(args.batch, args.cached_batch) < 1:
        raise ValueError("Invalid trial/batch counts")
    import pyarrow as pa

    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    args.output.mkdir(parents=True, exist_ok=False)
    physical = args.source.read_bytes()
    pieces = physical.split(b"\n")
    if physical.endswith(b"\n"):
        pieces.pop()
    if not physical:
        pieces = []
    values = [piece.decode("utf-8", errors="strict") for piece in pieces]
    queries = [("contains", "WARN"), ("equals", values[0] if values else "")]
    expected = {
        op: [
            i
            for i, row in enumerate(values)
            if (value in row if op == "contains" else value == row)
        ]
        for op, value in queries
    }
    archives = {
        label: Path(path).resolve() for label, path in (x.split("=", 1) for x in args.archive)
    }
    for archive in archives.values():
        if load_archive(archive)["dataset"]["source_sha256"] != checksum(args.source):
            raise ValueError("Archive/source mismatch")
    dump_json(
        args.output / "run.json",
        {
            "environment": environment(),
            "source_sha256": checksum(args.source),
            "native": worker_info(args.worker),
            "queries": queries,
            "archives": {label: {"path": str(p), **storage(p)} for label, p in archives.items()},
            "cache_budgets": [0, args.cache_bytes],
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "stream_batch": args.batch,
            "cached_batch": args.cached_batch,
            "track": "exploratory_literal_filter_to_consumed_row_ids; "
            "not SQL or held-out evaluation",
            "cache_scope": "Arrow value buffers only, same limit for all; no result cache",
            "first_scope": "archive open, first scan, filter and result comparison; "
            "imports preloaded",
            "batch_scope": "retained handle, scans or populated Arrow cache; "
            "result comparison included",
            "os_cache": "uncontrolled; no cold-disk claim",
            "seed": 20261023,
        },
    )
    observations = []
    rng = random.Random(20261023)
    with (args.output / "observations.jsonl").open("x") as stream:
        for round_id in range(args.warmups + args.repetitions):
            order = [(label, budget) for label in archives for budget in (0, args.cache_bytes)]
            rng.shuffle(order)
            for label, budget in order:
                base = {
                    "round": round_id,
                    "warmup": round_id < args.warmups,
                    "archive": label,
                    "cache_budget_bytes": budget,
                }
                start = time.perf_counter_ns()
                try:
                    with QuerySession(
                        archives[label], worker=args.worker, cache_bytes=budget
                    ) as session:
                        first = session.filter(*queries[0])
                        assert first["row_ids"] == expected[queries[0][0]]
                        record = {
                            **base,
                            "track": "first_query",
                            "operation": queries[0][0],
                            "request_ns": time.perf_counter_ns() - start,
                            "open_ns": session.open_ns,
                            "query_ns": first["query_ns"],
                            "status": "valid",
                            "count": first["count"],
                            "cache_buffer_bytes": first["cache_buffer_bytes"],
                        }
                        stream.write(json.dumps(record) + "\n")
                        observations.append(record)
                        for op, value in queries:
                            count = args.cached_batch if first["cache_buffer_bytes"] else args.batch
                            start = time.perf_counter_ns()
                            for _ in range(count):
                                result = session.filter(op, value)
                                assert result["row_ids"] == expected[op]
                            duration = time.perf_counter_ns() - start
                            record = {
                                **base,
                                "track": "repeated_query",
                                "operation": op,
                                "batch_ns": duration,
                                "batch_count": count,
                                "request_ns": duration / count,
                                "cache_hit": result["cache_hit"],
                                "cache_buffer_bytes": result["cache_buffer_bytes"],
                                "status": "valid",
                                "count": result["count"],
                            }
                            stream.write(json.dumps(record) + "\n")
                            stream.flush()
                            observations.append(record)
                except Exception as error:
                    record = {**base, "status": "failed", "reason": repr(error)}
                    stream.write(json.dumps(record) + "\n")
                    stream.flush()
                    observations.append(record)
    summary = {}
    measured = [r for r in observations if not r["warmup"] and r["status"] == "valid"]
    for r in measured:
        key = f"{r['archive']}/{r['cache_budget_bytes']}/{r['track']}/{r['operation']}"
        summary.setdefault(key, []).append(r["request_ns"])
    aggregates = {
        key: {
            "median_ns": statistics.median(v),
            "min_ns": min(v),
            "max_ns": max(v),
            "trials": len(v),
        }
        for key, v in summary.items()
    }
    failures = [r for r in observations if r["status"] != "valid"]
    dump_json(
        args.output / "summary.json",
        {
            "latencies": aggregates,
            "failures": failures,
            "observations_sha256": checksum(args.output / "observations.jsonl"),
        },
    )
    return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", action="append", required=True, help="label=archive-path")
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--cached-batch", type=int, default=16384)
    parser.add_argument("--cache-bytes", type=int, default=16 << 20)
    raise SystemExit(run(parser.parse_args()))

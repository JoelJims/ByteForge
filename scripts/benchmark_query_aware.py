"""Cold and repeated literal-query benchmark for Parquet and B archives."""

import argparse
import hashlib
import json
import os
import re
import statistics
import struct
import time
from collections import Counter
from pathlib import Path

from byteforge.benchmark import environment
from byteforge.dataset import checksum, dump_json
from byteforge.native import worker_info
from byteforge.pipelines import load_archive, storage
from byteforge.query import QuerySession


def _rows(path: Path) -> list[str]:
    raw = path.read_bytes()
    values = raw.split(b"\n")
    if raw.endswith(b"\n"):
        values.pop()
    if not raw:
        values = []
    return [value.decode("utf-8", errors="strict") for value in values]


def _queries(values: list[str]) -> list[dict]:
    frequencies = Counter()
    for row in values:
        frequencies.update(set(re.findall(r"blk_[^\s,:]+", row)))
    ids = sorted(frequencies)
    present = [term for term in ids if frequencies[term]]
    rare = min(present, key=lambda term: (frequencies[term], -len(term), term)) if present else ""
    first = values[0] if values else ""
    cases = [
        {"id": "common-warning", "operation": "contains", "value": "WARN"},
        {"id": "common-block-prefix", "operation": "contains", "value": "blk_"},
        {"id": "rare-block-id", "operation": "contains", "value": rare},
        {
            "id": "absent-sentinel",
            "operation": "contains",
            "value": "__BYTEFORGE_QUERY_SENTINEL_9f38d7__",
        },
        {"id": "punctuation", "operation": "contains", "value": "src:"},
        {"id": "case-sensitive-absent", "operation": "contains", "value": "warn"},
        {"id": "unicode-absent", "operation": "contains", "value": "日志"},
        {"id": "empty-term", "operation": "contains", "value": ""},
        {"id": "exact-first-row", "operation": "equals", "value": first},
        {"id": "prefix-first-row", "operation": "prefix", "value": first[:10]},
        {
            "id": "row-range-warning",
            "operation": "contains",
            "value": "WARN",
            "row_range": [min(1000, len(values)), min(1500, len(values))],
        },
    ]
    return cases


def _expected(rows: list[str], query: dict) -> list[int]:
    operation, value = query["operation"], query["value"]
    start, stop = query.get("row_range", [0, len(rows)])
    if operation == "equals":
        matches = (row == value for row in rows)
    elif operation == "prefix":
        matches = (row.startswith(value) for row in rows)
    else:
        matches = (value in row for row in rows)
    return [i for i, match in enumerate(matches) if match and start <= i < stop]


def _digest(ids: list[int]) -> str:
    return hashlib.sha256(b"".join(struct.pack("<Q", value) for value in ids)).hexdigest()


def _peak_worker_rss(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None
    return None


def run(args) -> int:
    if args.repetitions < 1 or args.warmups < 0 or args.cache_bytes < 1:
        raise ValueError("Invalid repetitions, warmups or cache limit")
    import pyarrow as pa

    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    args.output.mkdir(parents=True, exist_ok=False)
    rows = _rows(args.source)
    source_hash = checksum(args.source)
    archives = {
        label: Path(path).resolve() for label, path in (x.split("=", 1) for x in args.archive)
    }
    if len(archives) < 2:
        raise ValueError("Compare at least two archives")
    for archive in archives.values():
        if load_archive(archive)["dataset"]["source_sha256"] != source_hash:
            raise ValueError("Archive/source checksum mismatch")
    queries = _queries(rows)
    if args.query_ids:
        requested = set(args.query_ids)
        known = {query["id"] for query in queries}
        unknown = requested - known
        if unknown:
            raise ValueError(f"Unknown query IDs: {sorted(unknown)}")
        queries = [query for query in queries if query["id"] in requested]
        if not queries:
            raise ValueError("No benchmark queries selected")
    # Reference answers depend only on the immutable input and query definition.
    # Compute once so a large dataset does not get rescanned for every trial.
    expected_results = {query["id"]: _expected(rows, query) for query in queries}
    configs = [(label, budget) for label in archives for budget in (0, args.cache_bytes)]
    dump_json(
        args.output / "run.json",
        {
            "environment": environment(),
            "source_sha256": source_hash,
            "source_rows": len(rows),
            "source_bytes": args.source.stat().st_size,
            "native": worker_info(args.worker),
            "archives": {
                label: {"path": str(path), **storage(path)} for label, path in archives.items()
            },
            "queries": queries,
            "cache_budgets": [0, args.cache_bytes],
            "cache_policy": "bounded decoded Arrow blocks; LRU for archive v2; no result cache",
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "warm_repetitions_per_session": args.warm_repetitions,
            "repeated_warmups_per_session": min(2, args.warm_repetitions),
            "block_rows": {
                label: (
                    load_archive(path).get("settings", {}).get("block_rows")
                    or load_archive(path).get("_validated_block_index", {}).get("block_rows")
                )
                for label, path in archives.items()
            },
            "timing": "cold includes QuerySession open and first filter; warm times one filter",
            "OS_cache": "uncontrolled; fresh QuerySession and native worker per trial",
            "seed": args.seed,
            "peak_memory": "native worker VmHWM per session; parent peak not isolated",
        },
    )
    trials = [(label, budget, query) for label, budget in configs for query in queries]
    observations = []
    import random

    rng = random.Random(args.seed)
    with (args.output / "observations.jsonl").open("x", encoding="utf-8") as out:
        for round_id in range(args.warmups + args.repetitions):
            order = trials.copy()
            rng.shuffle(order)
            for label, budget, query in order:
                expected = expected_results[query["id"]]
                base = {
                    "round": round_id,
                    "warmup": round_id < args.warmups,
                    "archive": label,
                    "cache_budget_bytes": budget,
                    "query_id": query["id"],
                    "operation": query["operation"],
                    "value": query["value"],
                    "row_range": query.get("row_range"),
                }
                try:
                    request_start = time.perf_counter_ns()
                    with QuerySession(
                        archives[label], worker=args.worker, cache_bytes=budget
                    ) as session:
                        first = session.filter(
                            query["operation"],
                            query["value"],
                            row_range=tuple(query["row_range"]) if query.get("row_range") else None,
                        )
                        native_pid = session.native.process.pid if session.native else None
                        if first["row_ids"] != expected:
                            raise AssertionError("Cold query row IDs differ from reference")
                        record = {
                            **base,
                            "track": "cold",
                            "request_ns": time.perf_counter_ns() - request_start,
                            "archive_open_ns": session.open_ns,
                            "result_count": first["count"],
                            "result_sha256": _digest(first["row_ids"]),
                            "correctness": True,
                            **{
                                k: v
                                for k, v in first.items()
                                if k not in ("row_ids", "value", "operation")
                            },
                        }
                        record["worker_peak_rss_bytes"] = _peak_worker_rss(native_pid)
                        out.write(json.dumps(record, ensure_ascii=False) + "\n")
                        observations.append(record)
                        for warmup_id in range(min(2, args.warm_repetitions)):
                            warmed = session.filter(
                                query["operation"],
                                query["value"],
                                row_range=tuple(query["row_range"])
                                if query.get("row_range")
                                else None,
                            )
                            if warmed["row_ids"] != expected:
                                raise AssertionError("Warmup query row IDs differ from reference")
                            warmup_record = {
                                **base,
                                "track": "warm",
                                "warmup": True,
                                "warmup_id": warmup_id,
                                "request_ns": warmed["query_ns"],
                                "result_count": warmed["count"],
                                "correctness": True,
                                **{
                                    k: v
                                    for k, v in warmed.items()
                                    if k not in ("row_ids", "value", "operation")
                                },
                            }
                            out.write(json.dumps(warmup_record, ensure_ascii=False) + "\n")
                            observations.append(warmup_record)
                        for _ in range(args.warm_repetitions):
                            warm_start = time.perf_counter_ns()
                            warmed = session.filter(
                                query["operation"],
                                query["value"],
                                row_range=tuple(query["row_range"])
                                if query.get("row_range")
                                else None,
                            )
                            if warmed["row_ids"] != expected:
                                raise AssertionError("Warm query row IDs differ from reference")
                            record = {
                                **base,
                                "track": "warm",
                                "request_ns": time.perf_counter_ns() - warm_start,
                                "result_count": warmed["count"],
                                "result_sha256": _digest(warmed["row_ids"]),
                                "correctness": True,
                                **{
                                    k: v
                                    for k, v in warmed.items()
                                    if k not in ("row_ids", "value", "operation")
                                },
                            }
                            record["worker_peak_rss_bytes"] = _peak_worker_rss(native_pid)
                            out.write(json.dumps(record, ensure_ascii=False) + "\n")
                            observations.append(record)
                    out.flush()
                except Exception as error:
                    record = {**base, "track": "failed", "status": "failed", "reason": repr(error)}
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    observations.append(record)
    groups = {}
    for item in observations:
        if item.get("warmup") or item.get("status") == "failed":
            continue
        key = f"{item['archive']}/{item['cache_budget_bytes']}/{item['track']}/{item['query_id']}"
        groups.setdefault(key, []).append(item["request_ns"])
    summary = {
        key: {
            "p50_ns": statistics.median(values),
            "p95_ns": statistics.quantiles(values, n=20, method="inclusive")[18]
            if len(values) >= 2
            else values[0],
            "min_ns": min(values),
            "max_ns": max(values),
            "trials": len(values),
        }
        for key, values in groups.items()
    }
    failures = [item for item in observations if item.get("status") == "failed"]
    dump_json(
        args.output / "summary.json",
        {
            "latencies": summary,
            "failures": failures,
            "observations_sha256": checksum(args.output / "observations.jsonl"),
        },
    )
    dump_json(
        args.output / "complete.json",
        {
            "status": "complete" if not failures else "failed",
            "failed_observations": len(failures),
            "query_count": len(queries),
            "archives": len(archives),
            "repetitions": args.repetitions,
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
    parser.add_argument(
        "--warm-repetitions",
        type=int,
        help="timed repeated filters per session; defaults to --repetitions",
    )
    parser.add_argument(
        "--query-ids",
        nargs="+",
        help="optional subset of generated query IDs for a bounded large-data run",
    )
    parser.add_argument("--cache-bytes", type=int, default=16 << 20)
    parser.add_argument("--seed", type=int, default=20261024)
    args = parser.parse_args()
    if args.warm_repetitions is None:
        args.warm_repetitions = args.repetitions
    if args.warm_repetitions < 1:
        parser.error("--warm-repetitions must be at least 1")
    raise SystemExit(run(args))

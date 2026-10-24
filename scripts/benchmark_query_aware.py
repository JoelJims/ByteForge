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


def _iter_rows(path: Path):
    with path.open("rb") as stream:
        for physical in stream:
            value = physical[:-1] if physical.endswith(b"\n") else physical
            yield value.decode("utf-8", errors="strict")


def _large_queries(path: Path) -> list[dict]:
    first = next(_iter_rows(path), "")
    fields = first.split()
    sampled = fields[1] if len(fields) > 1 else first[:32]
    return [
        {"id": "common-warning", "operation": "contains", "value": "warning"},
        {"id": "sampled-timestamp", "operation": "contains", "value": sampled},
        {
            "id": "absent-sentinel",
            "operation": "contains",
            "value": "__BYTEFORGE_QUERY_SENTINEL_9f38d7__",
        },
    ]


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


def _expected_stream(path: Path, queries: list[dict]) -> dict[str, list[int]]:
    expected = {query["id"]: [] for query in queries}
    for row_id, row in enumerate(_iter_rows(path)):
        for query in queries:
            value = query["value"]
            operation = query["operation"]
            match = (
                row == value
                if operation == "equals"
                else row.startswith(value)
                if operation == "prefix"
                else value in row
            )
            bounds = query.get("row_range", [0, 1 << 64])
            if match and bounds[0] <= row_id < bounds[1]:
                expected[query["id"]].append(row_id)
    return expected


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
    large_source = args.source.stat().st_size > (512 << 20)
    rows = None if large_source else _rows(args.source)
    source_hash = checksum(args.source)
    archives = {
        label: Path(path).resolve() for label, path in (x.split("=", 1) for x in args.archive)
    }
    word_indexes = {
        label: Path(path).resolve() for label, path in (x.split("=", 1) for x in args.word_index)
    }
    if word_indexes.keys() - archives.keys():
        raise ValueError("Word index labels must identify an archive")
    if len(archives) < 2:
        raise ValueError("Compare at least two archives")
    source_rows = None
    for archive in archives.values():
        dataset = load_archive(archive)["dataset"]
        if dataset["source_sha256"] != source_hash:
            raise ValueError("Archive/source checksum mismatch")
        if source_rows is not None and dataset["rows"] != source_rows:
            raise ValueError("Archive row-count mismatch")
        source_rows = dataset["rows"]
    queries = (
        json.loads(args.queries_json.read_text())
        if args.queries_json
        else _large_queries(args.source)
        if large_source
        else _queries(rows)
    )
    if (
        not isinstance(queries, list)
        or not queries
        or len({q["id"] for q in queries}) != len(queries)
    ):
        raise ValueError("Expected a nonempty query list with unique IDs")
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
    expected_results = (
        _expected_stream(args.source, queries)
        if large_source
        else {query["id"]: _expected(rows, query) for query in queries}
    )
    budgets = args.cache_budgets if args.cache_budgets is not None else [0, args.cache_bytes]
    if not budgets or any(b < 0 for b in budgets) or len(set(budgets)) != len(budgets):
        raise ValueError("Expected distinct nonnegative cache budgets")
    configs = [(label, budget) for label in archives for budget in budgets]
    dump_json(
        args.output / "run.json",
        {
            "environment": environment(),
            "source_sha256": source_hash,
            "source_rows": source_rows,
            "source_bytes": args.source.stat().st_size,
            "native": worker_info(args.worker),
            "archives": {
                label: {"path": str(path), **storage(path)} for label, path in archives.items()
            },
            "queries": queries,
            "word_indexes": {
                label: {
                    "path": str(path),
                    "total_bytes": sum(p.stat().st_size for p in path.iterdir()),
                    "manifest": json.loads((path / "manifest.json").read_text()),
                }
                for label, path in word_indexes.items()
            },
            "cache_budgets": budgets,
            "cpu_scope": "parent process only; excludes native worker CPU",
            "cache_policy": "bounded decoded Arrow blocks; LRU for archive v2; no result cache",
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "warm_repetitions_per_session": args.warm_repetitions,
            "repeated_warmups_per_session": args.session_warmups,
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
                    cpu_start = time.process_time_ns()
                    from byteforge.word_index import IndexedQuerySession

                    session_class = IndexedQuerySession if label in word_indexes else QuerySession
                    options = {"word_index": word_indexes[label]} if label in word_indexes else {}
                    with session_class(
                        archives[label], worker=args.worker, cache_bytes=budget, **options
                    ) as session:
                        first = session.filter(
                            query["operation"],
                            query["value"],
                            row_range=tuple(query["row_range"]) if query.get("row_range") else None,
                        )
                        request_ns = time.perf_counter_ns() - request_start
                        cpu_ns = time.process_time_ns() - cpu_start
                        native_pid = session.native.process.pid if session.native else None
                        if first["row_ids"] != expected:
                            raise AssertionError("Cold query row IDs differ from reference")
                        record = {
                            **base,
                            "track": "cold",
                            "request_ns": request_ns,
                            "parent_cpu_ns": cpu_ns,
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
                        for warmup_id in range(args.session_warmups):
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
                            warm_cpu = time.process_time_ns()
                            warmed = session.filter(
                                query["operation"],
                                query["value"],
                                row_range=tuple(query["row_range"])
                                if query.get("row_range")
                                else None,
                            )
                            warm_ns = time.perf_counter_ns() - warm_start
                            warm_cpu_ns = time.process_time_ns() - warm_cpu
                            if warmed["row_ids"] != expected:
                                raise AssertionError("Warm query row IDs differ from reference")
                            record = {
                                **base,
                                "track": "warm",
                                "request_ns": warm_ns,
                                "parent_cpu_ns": warm_cpu_ns,
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
    parser.add_argument("--word-index", action="append", default=[], help="label=index-directory")
    parser.add_argument("--queries-json", type=Path, help="explicit reproducible query definitions")
    parser.add_argument(
        "--cache-budgets", type=int, nargs="+", help="override compared cache limits"
    )
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
        "--session-warmups",
        type=int,
        default=2,
        help="untimed repeated filters before timing warm requests (default: 2)",
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
    if args.warm_repetitions < 1 or args.session_warmups < 0:
        parser.error("warm repetitions must be positive and session warmups nonnegative")
    raise SystemExit(run(args))

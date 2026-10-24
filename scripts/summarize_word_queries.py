"""Render comparable query latency/storage tables from completed raw observations."""

import argparse
import json
import statistics
from pathlib import Path


def summarize(directory: Path) -> str:
    run = json.loads((directory / "run.json").read_text())
    if len(run["cache_budgets"]) != 1:
        raise ValueError("This table requires one cache budget; do not mix cache treatments")
    complete = json.loads((directory / "complete.json").read_text())
    if complete["status"] != "complete":
        raise ValueError("Cannot summarize an incomplete/failed run as successful")
    raw_path = directory / "observations.jsonl"
    if raw_path.exists():
        raw = raw_path.read_bytes()
    else:
        import zstandard as zstd

        raw = zstd.ZstdDecompressor().decompress(raw_path.with_suffix(".jsonl.zst").read_bytes())
    observations = [json.loads(line) for line in raw.splitlines()]
    from byteforge.dataset import checksum

    if raw_path.exists():
        digest = checksum(raw_path)
    else:
        import hashlib

        digest = hashlib.sha256(raw).hexdigest()
    summary = json.loads((directory / "summary.json").read_text())
    if digest != summary["observations_sha256"]:
        raise ValueError("Raw observation checksum mismatch")
    valid = [r for r in observations if not r["warmup"] and r.get("correctness") is True]
    output = [
        "| Configuration | Archive + optional index, MB | Additional word index, MB |",
        "|---|---:|---:|",
    ]
    for name, archive in run["archives"].items():
        index_bytes = run.get("word_indexes", {}).get(name, {}).get("total_bytes", 0)
        size = archive["archive_total_bytes"]
        output.append(f"| {name} | {(size + index_bytes) / 1e6:.3f} | {index_bytes / 1e6:.3f} |")
    output += [
        "",
        "| Configuration / query | Cold p50 / p95, ms | Warm p50 / p95, ms | "
        "Cold parent CPU p50, ms | Matches | Trials |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in run["archives"]:
        for query in run["queries"]:
            group = [r for r in valid if r["archive"] == name and r["query_id"] == query["id"]]
            cold = [r for r in group if r["track"] == "cold"]
            warm = [r for r in group if r["track"] == "warm"]
            if not cold or not warm:
                raise ValueError("Missing cold/warm observations")

            def latency(rows):
                values = [r["request_ns"] / 1e6 for r in rows]
                p95 = (
                    statistics.quantiles(values, n=20, method="inclusive")[18]
                    if len(values) > 1
                    else values[0]
                )
                return f"{statistics.median(values):.2f} / {p95:.2f}"

            cpu = statistics.median(r["parent_cpu_ns"] for r in cold) / 1e6
            output.append(
                f"| {name} / {query['id']} | {latency(cold)} | {latency(warm)} | "
                f"{cpu:.2f} | {cold[0]['result_count']:,} | {len(cold)} |"
            )
    return "\n".join(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    print(summarize(parser.parse_args().directory))

"""Summarize measured controls; blocked SGTT records never become measurements."""

import argparse
import json
import statistics
from pathlib import Path


def summarize(observations: list[dict]) -> dict:
    def aggregate(values):
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}

    result = {}
    for pipeline in ("A", "B-test-only"):
        selected = [
            row
            for row in observations
            if row["pipeline"] == pipeline
            and not row.get("warmup", True)
            and row["status"] == "valid"
        ]
        if not selected:
            continue
        phases = {}
        for stage in ("encode", "restore"):
            phases[stage] = {
                key: aggregate([row[stage][key] for row in selected])
                for key, value in selected[0][stage].items()
                if key.endswith("_ns") and type(value) is int
            }
        phases["two_fresh_process_wall_ns"] = aggregate(
            [
                row["encode"]["fresh_process_wall_ns"] + row["restore"]["fresh_process_wall_ns"]
                for row in selected
            ]
        )
        phases["components_bytes"] = selected[0]["component_bytes"]
        phases["codec_compressed_bytes"] = (
            aggregate([row["encode"]["codec_compressed_bytes"] for row in selected])
            if pipeline == "B-test-only"
            else None
        )
        phases["warm_service_ns"] = None
        phases["actual_sgtt_initialization_ns"] = None
        phases["actual_sgtt_asset_bytes"] = None
        result[pipeline] = phases
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [json.loads(line) for line in args.observations.read_text().splitlines()]
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(summarize(records), indent=2) + "\n")

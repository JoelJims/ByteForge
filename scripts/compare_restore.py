"""Paired old/new restoration of the same archive and native executable."""

import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from byteforge.benchmark import environment
from byteforge.dataset import checksum, dump_json
from byteforge.native import worker_info
from byteforge.pipelines import load_archive


def run(args):
    if args.warmups < 0 or args.repetitions < 1:
        raise ValueError("Invalid trial counts")
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    root = Path.cwd()
    args.output.mkdir(parents=True, exist_ok=False)
    args.output = args.output.resolve()
    args.archive, args.worker = args.archive.resolve(), args.worker.resolve()
    expected = args.source.read_bytes()
    if load_archive(args.archive)["dataset"]["source_sha256"] != checksum(args.source):
        raise ValueError("Archive/source mismatch")
    revision = subprocess.check_output(["git", "rev-parse", args.reference], text=True).strip()
    snapshot = args.output / "reference-code"
    snapshot.mkdir()
    data = subprocess.check_output(["git", "archive", "--format=tar", revision, "byteforge"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(snapshot, filter="data")
    dump_json(
        args.output / "run.json",
        {
            "reference_revision": revision,
            "environment": environment(),
            "source_sha256": checksum(args.source),
            "archive": str(args.archive),
            "archive_manifest_sha256": checksum(args.archive / "manifest.json"),
            "worker": worker_info(args.worker),
            "warmups": args.warmups,
            "repetitions": args.repetitions,
            "scope": "same archive; fresh Python/native processes; exact physical export; "
            "OS cache uncontrolled; balanced alternating order",
        },
    )
    env = {
        **os.environ,
        **dict.fromkeys(
            ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ARROW_NUM_THREADS"), "1"
        ),
    }
    observations = []
    with (args.output / "observations.jsonl").open("x") as stream:
        for round_id in range(args.warmups + args.repetitions):
            order = ["before", "after"] if round_id % 2 == 0 else ["after", "before"]
            for variant in order:
                output = args.output / f"round-{round_id:03}-{variant}.log"
                start = time.perf_counter_ns()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "byteforge",
                        "restore",
                        str(args.archive),
                        "--output",
                        str(output),
                        "--worker",
                        str(args.worker),
                        "--raw",
                    ],
                    cwd=snapshot if variant == "before" else root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                wall = time.perf_counter_ns() - start
                record = {
                    "round": round_id,
                    "warmup": round_id < args.warmups,
                    "variant": variant,
                    "process_wall_ns": wall,
                    "returncode": result.returncode,
                    "stderr": result.stderr,
                }
                if result.returncode == 0 and output.read_bytes() == expected:
                    record.update(
                        status="valid", correctness=True, metrics=json.loads(result.stdout)
                    )
                    output.unlink()
                else:
                    record.update(status="failed", correctness=False, stdout=result.stdout)
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                observations.append(record)
    summary = {}
    for variant in ("before", "after"):
        selected = [
            r
            for r in observations
            if r["variant"] == variant and not r["warmup"] and r["status"] == "valid"
        ]
        summary[variant] = {"valid_trials": len(selected)}
        for name in ("restore_total_ns", "detokenization_validation_ns"):
            values = [r["metrics"][name] for r in selected]
            if values:
                summary[variant][name] = {
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                }
    failures = sum(r["status"] != "valid" for r in observations)
    dump_json(
        args.output / "summary.json",
        {
            "variants": summary,
            "failures": failures,
            "observations_sha256": checksum(args.output / "observations.jsonl"),
        },
    )
    return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", default="41f6ae6")
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=10)
    raise SystemExit(run(parser.parse_args()))

"""Fresh-process, paired archive-to-reconstruction benchmark; no SQL claims."""

import hashlib
import importlib.metadata
import json
import os
import platform
import random
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from byteforge.dataset import checksum, dump_json, prepare, reconstruct_raw
from byteforge.native import worker_info
from byteforge.pipelines import (
    TestByteEncoder,
    build_hybrid,
    build_parquet,
    read_json,
    restore,
    storage,
)


def exact_files(left: Path, right: Path) -> bool:
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            x, y = a.read(1 << 20), b.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


def environment() -> dict:
    versions = {}
    for package in ("pyarrow", "zstandard", "cmake"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "packages": versions,
        "cpu_count": os.cpu_count(),
        "cpuinfo": Path("/proc/cpuinfo").read_text() if Path("/proc/cpuinfo").exists() else None,
        "meminfo": Path("/proc/meminfo").read_text() if Path("/proc/meminfo").exists() else None,
        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--porcelain"], text=True),
        "git_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "HEAD"])
        ).hexdigest(),
        "cache_policy": "os_cache_uncontrolled; fresh Python/native processes",
        "thread_policy": "one CPU affinity per worker; Arrow/OMP/BLAS thread limits = 1",
        "clock": "perf_counter_ns monotonic wall; process and child CPU via getrusage",
        "memory_policy": "whole-tree peak unavailable; process peaks diagnostic only",
        "disk_free_bytes": shutil.disk_usage(".").free,
        "native_sampling": "upstream NumberStats uses random_device; no seed control; "
        "chosen schemes retained per leaf in archive index",
    }


def work(spec: dict) -> dict:
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    try:
        import pyarrow as pa

        pa.set_cpu_count(1)
        pa.set_io_thread_count(1)
    except ImportError:
        pass
    archive, native = Path(spec["archive"]), Path(spec["worker"])
    if spec["pipeline"] not in ("A", "B-test-only"):
        raise ValueError("Production B is blocked; no approved SGTT adapter is integrated")
    start = time.perf_counter_ns()
    if spec["operation"] == "encode":
        prepared = Path(spec["prepared"])
        result = (
            build_parquet(prepared, archive)
            if spec["pipeline"] == "A"
            else build_hybrid(prepared, archive, TestByteEncoder(), native)
        )
    else:
        result = restore(archive, Path(spec["restored"]), worker=native)
        result["archive_to_canonical_ns"] = result["restore_total_ns"]
        export_start = time.perf_counter_ns()
        reconstruct_raw(
            Path(spec["restored"]),
            read_json(archive / "manifest.json")["dataset"],
            Path(spec["restored"] + ".log"),
        )
        result["physical_raw_export_ns"] = time.perf_counter_ns() - export_start
        result["restore_total_ns"] = time.perf_counter_ns() - start
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(
        {
            "operation_wall_ns": time.perf_counter_ns() - start,
            "process_cpu_ns": int((self_usage.ru_utime + self_usage.ru_stime) * 1e9),
            "native_children_cpu_ns": int((child_usage.ru_utime + child_usage.ru_stime) * 1e9),
            "python_process_peak_rss_bytes": self_usage.ru_maxrss * 1024,
            "native_child_peak_rss_bytes": child_usage.ru_maxrss * 1024,
            "peak_rss_bytes": None,
            "peak_rss_reason": "whole-tree concurrent peak not measured",
            "observed_python_threads_end": len(list(Path("/proc/self/task").iterdir()))
            if Path("/proc/self/task").exists()
            else None,
        }
    )
    return result


def invoke(spec: dict, trial: Path, stage: str, timeout: float) -> dict:
    spec_path = trial / f"{stage}-job.json"
    dump_json(spec_path, spec)
    env = {
        **os.environ,
        **dict.fromkeys(
            ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ARROW_NUM_THREADS"), "1"
        ),
    }
    start = time.perf_counter_ns()
    with (
        (trial / f"{stage}.stdout").open("wb") as stdout,
        (trial / f"{stage}.stderr").open("wb") as stderr,
    ):
        process = subprocess.Popen(
            [sys.executable, "-m", "byteforge", "_work", str(spec_path)],
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    if process.returncode:
        raise RuntimeError(
            f"{stage} worker failed ({process.returncode}); see {trial}/{stage}.stderr"
        )
    result = read_json(trial / f"{stage}-metrics.json")
    result["fresh_process_wall_ns"] = time.perf_counter_ns() - start
    return result


def run(
    path: Path,
    output: Path,
    *,
    synthetic: bool = False,
    test_encoder: bool = False,
    worker: Path,
    warmups: int = 2,
    repetitions: int = 10,
    seed: int = 20261023,
    timeout: float = 3600,
) -> dict:
    if warmups < 0 or repetitions < 1 or timeout <= 0:
        raise ValueError("Invalid trial counts/timeout")
    if test_encoder and not synthetic:
        raise ValueError("Test encoder requires --synthetic; it cannot stand in for SGTT")
    output.mkdir(parents=True, exist_ok=False)
    prestart = time.perf_counter_ns()
    dataset = prepare(path, output / "input", synthetic=synthetic)
    preparation_ns = time.perf_counter_ns() - prestart
    native_info = None
    native_failure = None
    if test_encoder:
        try:
            native_info = worker_info(worker)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            native_failure = str(error)
    config = {
        "kind": "synthetic_development" if synthetic else "hdfs_primary",
        "track": "full_pipeline_archive_to_raw_reconstruction",
        "warmups": warmups,
        "repetitions": repetitions,
        "seed": seed,
        "test_encoder": test_encoder,
        "native": native_info,
        "dataset": dataset,
        "source_preparation_ns": preparation_ns,
        "environment": environment(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "timer_batching": "none; short synthetic timings are diagnostics only",
        "sgtt": {"status": "unsupported", "reason": "SGTT code/table reuse rights unresolved"},
    }
    dump_json(output / "run.json", config)
    rng = random.Random(seed)
    base_order = ["A", "B-test-only"] if test_encoder else ["A"]
    rng.shuffle(base_order)
    observations = []
    with (output / "observations.jsonl").open("x", encoding="utf-8") as raw:
        for round_id in range(warmups + repetitions):
            order = base_order.copy()
            if round_id % 2:
                order.reverse()
            for order_id, pipeline in enumerate(order):
                trial = output / f"round-{round_id:03}-{pipeline}"
                trial.mkdir()
                observation = {
                    "round": round_id,
                    "order": order_id,
                    "pipeline": pipeline,
                    "warmup": round_id < warmups,
                    "dataset_kind": dataset["kind"],
                    "measurement_origin": "measured_now",
                    "status": "incomplete",
                    "track": config["track"],
                    "correctness": False,
                }
                try:
                    if pipeline != "A" and native_failure:
                        raise FileNotFoundError(native_failure)
                    spec = {
                        "pipeline": pipeline,
                        "prepared": str((output / "input").resolve()),
                        "archive": str((trial / "archive").resolve()),
                        "worker": str(worker.resolve()),
                        "operation": "encode",
                    }
                    observation["encode"] = invoke(spec, trial, "encode", timeout)
                    restored = trial / "restored.bin"
                    spec.update(operation="restore", restored=str(restored.resolve()))
                    observation["restore"] = invoke(spec, trial, "restore", timeout)
                    if not exact_files(output / "input/rows.bin", restored):
                        raise ValueError(
                            "Exact ordered reconstruction differs from independent input"
                        )
                    if not exact_files(path, Path(str(restored) + ".log")):
                        raise ValueError("Exact physical raw-log reconstruction differs")
                    observation.update(storage(trial / "archive"))
                    observation.update(status="valid", correctness=True)
                    restored.unlink()  # Our validated temporary output, never user input.
                    Path(str(restored) + ".log").unlink()
                except FileNotFoundError as error:
                    observation.update(status="unavailable_dependency", failure_reason=str(error))
                except Exception as error:
                    observation.update(status="execution_failed", failure_reason=str(error))
                raw.write(json.dumps(observation) + "\n")
                raw.flush()
                observations.append(observation)
        blocked = {
            "pipeline": "B",
            "status": "unsupported",
            "correctness": None,
            "measurement_origin": "not_run",
            "failure_reason": config["sgtt"]["reason"],
            "archive_total_bytes": None,
            "encode": None,
            "restore": None,
        }
        raw.write(json.dumps(blocked) + "\n")
        observations.append(blocked)
    failures = sum(o["status"] != "valid" for o in observations if o["pipeline"] != "B")
    summary = {
        "execution_complete": True,
        "fully_qualified_primary_comparison": False,
        "status": "execution_failed" if failures else "complete_with_sgtt_blocked",
        "failed_trials": failures,
        "production_B": "blocked_license",
        "observations_sha256": checksum(output / "observations.jsonl"),
    }
    measured = [o for o in observations if not o.get("warmup", True) and o["status"] == "valid"]
    aggregates = {}
    for pipeline in sorted({o["pipeline"] for o in measured}):
        selected = [o for o in measured if o["pipeline"] == pipeline]
        aggregates[pipeline] = {
            "valid_measured_trials": len(selected),
            "dataset_kind": dataset["kind"],
            "performance_claim": None,
        }
        for stage, metric in (("encode", "encode_total_ns"), ("restore", "restore_total_ns")):
            values = [o[stage][metric] for o in selected]
            aggregates[pipeline][metric] = {
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
            }
        sizes = [o["archive_total_bytes"] for o in selected]
        aggregates[pipeline]["archive_total_bytes"] = {
            "median": statistics.median(sizes),
            "min": min(sizes),
            "max": max(sizes),
        }
    dump_json(
        output / "summary.json",
        {
            "pipelines": aggregates,
            "production_B": config["sgtt"],
            "note": "Warmups/failures excluded from summaries, retained in raw JSONL. "
            "No winner or SGTT inference; test-only timings are diagnostics.",
        },
    )
    dump_json(output / "complete.json", summary)
    return summary

"""Run `python -m byteforge --help`. No downloads or benchmark runs on install."""

import argparse
import json
import sys
import time
from pathlib import Path

from byteforge.dataset import (
    HDFS,
    HDFS_2K,
    dump_json,
    hdfs_path,
    prepare,
    reconstruct_raw,
    require_raw,
)
from byteforge.native import worker_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sgtt-status", help="show actual SGTT qualification and permission blocker")
    check = commands.add_parser("check-dataset")
    check.add_argument("--path", type=Path)
    prep = commands.add_parser("prepare")
    prep.add_argument("--path", type=Path)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--synthetic", action="store_true")
    bench = commands.add_parser("run")
    bench.add_argument("--path", type=Path)
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--synthetic", action="store_true")
    bench.add_argument(
        "--test-encoder",
        action="store_true",
        help="explicitly run test-only byte symbols on the supplied input; not SGTT",
    )
    bench.add_argument("--worker", type=Path, default=worker_path())
    bench.add_argument("--parquet-profile", choices=("standard", "fast-scan"), default="standard")
    bench.add_argument("--b-outer-codec", choices=("none", "zstd"), default="zstd")
    bench.add_argument(
        "--encoder",
        choices=("sgtt", "test-byte"),
        help="explicit encoder selection; actual SGTT fails until qualified; never falls back",
    )
    bench.add_argument("--warmups", type=int, default=2)
    bench.add_argument("--repetitions", type=int, default=10)
    bench.add_argument("--seed", type=int, default=20261023)
    bench.add_argument("--timeout", type=float, default=3600)
    decode = commands.add_parser("restore")
    decode.add_argument("archive", type=Path)
    decode.add_argument("--output", type=Path, required=True)
    decode.add_argument("--worker", type=Path, default=worker_path())
    decode.add_argument("--raw", action="store_true", help="reconstruct physical raw-log framing")
    query = commands.add_parser(
        "query", help="literal filters without exporting reconstructed logs"
    )
    query.add_argument("archive", type=Path)
    query.add_argument("--operation", choices=("equals", "contains"), required=True)
    query.add_argument("--value", required=True)
    query.add_argument("--worker", type=Path, default=worker_path())
    query.add_argument("--cache-bytes", type=int, default=0)
    private = commands.add_parser("_work", help="internal isolated measurement worker")
    private.add_argument("spec", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "sgtt-status":
            from byteforge.sgtt import status

            result = status()
        elif args.command == "check-dataset":
            path = hdfs_path(args.path)
            require_raw(path)
            with path.open("rb"):
                pass
            result = {
                "exists": True,
                "path": str(path),
                "bytes": path.stat().st_size,
                "content_validated": False,
                "expected_artifact": HDFS_2K if path.name == "HDFS_2k.log" else HDFS,
            }
        elif args.command == "prepare":
            result = prepare(hdfs_path(args.path), args.output, synthetic=args.synthetic)
        elif args.command == "run":
            from byteforge.benchmark import run

            if args.encoder == "sgtt":
                from byteforge.sgtt import require_sgtt

                require_sgtt()
            result = run(
                hdfs_path(args.path),
                args.output,
                synthetic=args.synthetic,
                test_encoder=args.test_encoder or args.encoder == "test-byte",
                worker=args.worker,
                warmups=args.warmups,
                repetitions=args.repetitions,
                seed=args.seed,
                timeout=args.timeout,
                parquet_profile=args.parquet_profile,
                b_outer_codec=args.b_outer_codec,
            )
        elif args.command == "query":
            from byteforge.query import QuerySession

            start = time.perf_counter_ns()
            with QuerySession(
                args.archive, worker=args.worker, cache_bytes=args.cache_bytes
            ) as session:
                result = session.filter(args.operation, args.value)
                result["archive_open_ns"] = session.open_ns
            result["request_ns"] = time.perf_counter_ns() - start
        elif args.command == "_work":
            from byteforge.benchmark import work
            from byteforge.pipelines import read_json

            spec = read_json(args.spec)
            result = work(spec)
            dump_json(
                args.spec.with_name(args.spec.name.replace("-job.json", "-metrics.json")), result
            )
        else:
            from byteforge.pipelines import read_json, restore

            if args.raw:
                import tempfile

                start = time.perf_counter_ns()
                with tempfile.TemporaryDirectory() as tmp:
                    canonical = Path(tmp) / "rows.bin"
                    result = restore(args.archive, canonical, worker=args.worker)
                    result["archive_to_canonical_ns"] = result["restore_total_ns"]
                    export_start = time.perf_counter_ns()
                    reconstruct_raw(
                        canonical, read_json(args.archive / "manifest.json")["dataset"], args.output
                    )
                    result["physical_raw_export_ns"] = time.perf_counter_ns() - export_start
                result["restore_total_ns"] = time.perf_counter_ns() - start
            else:
                result = restore(args.archive, args.output, worker=args.worker)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result.get("status") == "execution_failed")
    except Exception as error:
        print(f"ByteForge: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

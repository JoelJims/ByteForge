import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from byteforge.benchmark import run
from byteforge.dataset import checksum, prepare
from byteforge.native import NativeIntegers, worker_path
from byteforge.pipelines import TestByteEncoder, build_hybrid, build_parquet, restore, storage
from byteforge.representation import INT32_MAX, pack_unsigned, unpack_unsigned

HAS_ARROW = bool(importlib.util.find_spec("pyarrow"))
HAS_NATIVE = worker_path().is_file()
FIXTURE = Path(__file__).parent / "fixtures/hdfs-synthetic.log"


@unittest.skipUnless(HAS_ARROW, "PyArrow benchmark extra not installed")
class ParquetArchiveTests(unittest.TestCase):
    def test_test_encoder_does_not_relabel_input_as_synthetic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run(
                FIXTURE,
                root / "run",
                synthetic=False,
                test_encoder=True,
                worker=root / "missing",
                repetitions=1,
                warmups=0,
            )
            config = json.loads((root / "run/run.json").read_text())
            self.assertEqual(config["dataset"]["kind"], "hdfs_raw_candidate")
            self.assertEqual(config["kind"], "real_data_infrastructure_validation")
            self.assertEqual(config["sgtt"]["status"], "unsupported")

    def test_complete_archive_and_empty_inputs(self):
        for data in (FIXTURE.read_bytes(), b"", b"\n\n", b"x\r\n\r\nlast", b"x" * 100000):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "raw.log").write_bytes(data)
                prepare(root / "raw.log", root / "input", synthetic=True)
                expected = (root / "input/rows.bin").read_bytes()
                build_parquet(root / "input", root / "archive")
                restore(root / "archive", root / "restored.bin")
                self.assertEqual((root / "restored.bin").read_bytes(), expected)
                stats = storage(root / "archive")
                self.assertEqual(
                    stats["archive_total_bytes"], sum(stats["component_bytes"].values())
                )
                self.assertEqual((root / "archive/data.parquet").read_bytes()[:4], b"PAR1")

    def test_runner_missing_native_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run(
                FIXTURE,
                root / "run",
                synthetic=True,
                test_encoder=True,
                worker=root / "no-native",
                repetitions=1,
                warmups=0,
            )
            self.assertEqual(report["failed_trials"], 1)
            rows = [
                json.loads(line)
                for line in (root / "run/observations.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                {r["pipeline"]: r["status"] for r in rows},
                {"A": "valid", "B-test-only": "unavailable_dependency", "B": "unsupported"},
            )


@unittest.skipUnless(HAS_NATIVE, "Optional native worker not built")
class HybridArchiveTests(unittest.TestCase):
    def test_native_boundaries_and_repeated_requests(self):
        with NativeIntegers(worker_path()) as native:
            for values in ((0,), (0, 65535, INT32_MAX), tuple(range(65536))):
                raw = pack_unsigned(values, native_int32=True)
                compressed, _ = native.transform(1, len(values), raw)
                decoded, _ = native.transform(2, len(values), compressed)
                self.assertEqual(unpack_unsigned(decoded), values)

    def test_standalone_assets_and_boundaries(self):
        cases = (
            b"",
            b"\n\n",
            FIXTURE.read_bytes(),
            ("😀" * 17000 + "\n\nend").encode(),
            b"\n" * 65536,
        )
        for data in cases:
            with self.subTest(size=len(data)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "raw.log").write_bytes(data)
                prepare(root / "raw.log", root / "input", synthetic=True)
                expected = (root / "input/rows.bin").read_bytes()
                stats = build_hybrid(
                    root / "input", root / "archive", TestByteEncoder(), worker_path()
                )
                self.assertIsInstance(stats["codec_compressed_bytes"], int)
                (root / "raw.log").unlink()
                (root / "input/rows.bin").unlink()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "byteforge",
                        "restore",
                        str(root / "archive"),
                        "--output",
                        str(root / "decoded.bin"),
                        "--worker",
                        str(worker_path().resolve()),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "decoded.bin").read_bytes(), expected)
                table = root / "archive/vocabulary.bin"
                table.unlink()
                with self.assertRaises(ValueError):
                    restore(root / "archive", root / "absent.bin", worker=worker_path())

    def test_corruption_and_invalid_offsets_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(FIXTURE, root / "input", synthetic=True)
            build_hybrid(root / "input", root / "archive", TestByteEncoder(), worker_path())
            path = root / "archive/index.json"
            index = json.loads(path.read_text())
            index["windows"][0]["offsets"][0]["offset"] = -1
            path.write_text(json.dumps(index))
            with self.assertRaisesRegex(ValueError, "checksum"):
                restore(root / "archive", root / "bad.bin", worker=worker_path())
            manifest_path = root / "archive/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["index.json"] = {
                "bytes": path.stat().st_size,
                "sha256": checksum(path),
            }
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "leaf directory"):
                restore(root / "archive", root / "bad-offset.bin", worker=worker_path())

    def test_real_input_cannot_use_test_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(FIXTURE, root / "input", synthetic=False)
            with self.assertRaisesRegex(ValueError, "synthetic"):
                build_hybrid(root / "input", root / "archive", TestByteEncoder(), worker_path())

    def test_explicit_test_encoder_preserves_input_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(FIXTURE, root / "input", synthetic=False)
            build_hybrid(
                root / "input",
                root / "archive",
                TestByteEncoder(),
                worker_path(),
                allow_test_encoder_on_real=True,
            )
            manifest = json.loads((root / "archive/manifest.json").read_text())
            self.assertEqual(manifest["dataset"]["kind"], "hdfs_raw_candidate")
            self.assertTrue(manifest["tokenizer"]["test_only"])
            self.assertIn("not SGTT", manifest["tokenizer"]["validation_scope"])
            restore(root / "archive", root / "decoded.bin", worker=worker_path())
            self.assertEqual(
                (root / "input/rows.bin").read_bytes(), (root / "decoded.bin").read_bytes()
            )


class DatasetCommandTests(unittest.TestCase):
    def test_missing_dataset_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "byteforge",
                    "check-dataset",
                    "--path",
                    str(Path(tmp) / "missing.log"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("HDFS.zip", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Unavailability and independent infrastructure checks; no SGTT algorithm tests."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from byteforge.dataset import prepare
from byteforge.pipelines import TestByteEncoder, build_hybrid, load_archive, validate_index
from byteforge.sgtt import SGTTUnavailableError, status


class SGTTQualificationTests(unittest.TestCase):
    def test_explicit_selection_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "not-created"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "byteforge",
                    "run",
                    "--encoder",
                    "sgtt",
                    "--test-encoder",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("No test-only encoder substitution", result.stderr)
            self.assertFalse(output.exists())
        self.assertFalse(status()["available"])

    def test_custom_production_encoder_cannot_bypass_qualification(self):
        class UnqualifiedEncoder(TestByteEncoder):
            test_only = False
            identity = "sgtt"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw").write_bytes(b"hello\n")
            prepare(root / "raw", root / "input", synthetic=True)
            with self.assertRaises(SGTTUnavailableError):
                build_hybrid(
                    root / "input", root / "archive", UnqualifiedEncoder(), root / "missing"
                )
            self.assertFalse((root / "archive").exists())

    def test_archive_identity_and_dataset_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw").write_bytes(b"hello\n")
            dataset = prepare(root / "raw", root / "input", synthetic=True)
            archive = root / "archive"
            archive.mkdir()
            manifest = {
                "format": "byteforge-hdfs-archive-v1",
                "pipeline": "B",
                "dataset": dataset,
                "tokenizer": {"identity": "actual-sgtt", "test_only": False},
            }
            path = archive / "manifest.json"
            path.write_text(json.dumps(manifest))
            with patch("byteforge.pipelines.NativeIntegers") as native:
                with self.assertRaises(SGTTUnavailableError):
                    load_archive(archive)
                native.assert_not_called()
            for bad in (True, -1, 2**64, "2000"):
                changed = copy.deepcopy(manifest)
                changed["dataset"]["rows"] = bad
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_archive(archive)
            manifest["format"] = "byteforge-hdfs-archive-v999"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                load_archive(archive)


class DirectoryValidationTests(unittest.TestCase):
    def test_entire_directory_preflight(self):
        index = {
            "version": 1,
            "windows": [
                {
                    "rows": 1,
                    "tokens": 1,
                    "ids": [{"offset": 0, "bytes": 32, "elements": 1, "scheme": 0}],
                    "offsets": [{"offset": 32, "bytes": 32, "elements": 2, "scheme": 0}],
                }
            ],
        }
        validate_index(index, 64, 1)
        for field, invalids in {
            "offset": (-1, True, 2**64, 0),
            "bytes": (0, 31, (4 << 20) + 1, True),
            "elements": (0, 65537, True, 3),
            "scheme": (-1, 6, True),
        }.items():
            for invalid in invalids:
                changed = copy.deepcopy(index)
                changed["windows"][0]["offsets"][0][field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    validate_index(changed, 64, 1)
        for bytes_, rows in ((63, 1), (65, 1), (64, 0), (64, 2)):
            with self.assertRaises(ValueError):
                validate_index(index, bytes_, rows)
        validate_index({"version": 1, "windows": []}, 0, 0)


if __name__ == "__main__":
    unittest.main()

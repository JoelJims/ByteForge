import importlib.util
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from byteforge.dataset import checksum, prepare
from byteforge.native import worker_path
from byteforge.pipelines import TestByteEncoder, build_hybrid_blocked, build_parquet
from byteforge.query_index import PreparedTrigramQuery, build_filter, decode_filter, may_contain
from byteforge.word_index import IndexedQuerySession, build_word_index

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("pyarrow", "zstandard"))


@unittest.skipUnless(HAS_DEPS, "Optional dependencies unavailable")
class WordIndexTests(unittest.TestCase):
    def _build(self, root, values, *, native=False):
        source = root / "raw.log"
        source.write_bytes(b"".join(value.encode() + b"\n" for value in values))
        prepare(source, root / "prepared", synthetic=True)
        if native:
            build_hybrid_blocked(
                root / "prepared", root / "archive", TestByteEncoder(), worker_path(), block_rows=2
            )
        else:
            build_parquet(root / "prepared", root / "archive", profile="lz4")
        build_word_index(root / "prepared", root / "index")

    def test_exact_substrings_duplicates_unicode_and_fallback(self):
        values = [
            "warning WARN",
            "prewarning",
            "warn!ing",
            "éwarning漢字",
            "",
            "WARN",
            "a\x00b",
            "warn warning",
            "warning WARN",
            "x" * 70000,
            "1131523501",
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, values)
            with IndexedQuerySession(root / "archive", root / "index", cache_bytes=0) as s:
                for needle in ("warning", "WARN", "warn", "ing", "a", "x", "NotPresent"):
                    for bounds in (None, (1, 8), (0, 0)):
                        lo, hi = bounds or (0, len(values))
                        result = s.filter("contains", needle, row_range=bounds)
                        self.assertEqual(
                            result["row_ids"],
                            [i for i, r in enumerate(values) if lo <= i < hi and needle in r],
                        )
                        self.assertEqual(result["blocks_decoded"], 0)
                        self.assertIsNone(s.fallback)
                for op, needle in (
                    ("contains", ""),
                    ("contains", "é"),
                    ("contains", "warn!"),
                    ("contains", "1131523501"),
                    ("equals", "WARN"),
                    ("prefix", "warn"),
                ):
                    expected = [
                        i
                        for i, r in enumerate(values)
                        if (
                            r == needle
                            if op == "equals"
                            else r.startswith(needle)
                            if op == "prefix"
                            else needle in r
                        )
                    ]
                    self.assertEqual(s.filter(op, needle)["row_ids"], expected)
            with self.assertRaises(ValueError):
                s.filter("contains", "warning")

    def test_fresh_process_block_archive_and_native_fallback(self):
        if not worker_path().is_file():
            self.skipTest("Native worker unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, ["warning 12", "warning", "é漢字"], native=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "byteforge",
                    "query",
                    str(root / "archive"),
                    "--word-index",
                    str(root / "index"),
                    "--operation",
                    "contains",
                    "--value",
                    "warning",
                ],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONPATH": "."},
            )
            self.assertEqual(json.loads(result.stdout)["row_ids"], [0, 1])
            with IndexedQuerySession(root / "archive", root / "index", worker=worker_path()) as s:
                self.assertEqual(s.filter("contains", "12")["row_ids"], [0])

    def test_pages_and_random_substrings(self):
        rng = random.Random(42)
        values = ["warning", "alpha", "warn!ing", "WARN", "ébeta"] * 14000
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, values)
            with IndexedQuerySession(root / "archive", root / "index", cache_bytes=0) as s:
                for _ in range(25):
                    word = rng.choice(("warning", "alpha", "beta", "absent", "WARN"))
                    start = rng.randrange(len(word))
                    needle = word[start : rng.randrange(start + 1, len(word) + 1)]
                    result = s.filter("contains", needle)
                    self.assertEqual(
                        result["row_ids"], [i for i, r in enumerate(values) if needle in r]
                    )
                    self.assertEqual(result["cache_buffer_bytes"], 0)

    def test_corruption_and_changed_index_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, ["alpha", "beta"])
            with IndexedQuerySession(root / "archive", root / "index") as s:
                path = root / "index/postings.sqlite"
                with path.open("r+b") as f:
                    f.seek(-1, 2)
                    f.write(b"X")
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    s.filter("contains", "alpha")
            with self.assertRaisesRegex(ValueError, "checksum"):
                IndexedQuerySession(root / "archive", root / "index")

    def test_invalid_postings_even_with_updated_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, ["alpha"])
            path = root / "index/postings.sqlite"
            with sqlite3.connect(path) as db:
                db.execute("UPDATE pages SET count=65537")
            manifest = root / "index/manifest.json"
            obj = json.loads(manifest.read_text())
            obj["files"]["postings.sqlite"]["sha256"] = checksum(path)
            manifest.write_text(json.dumps(obj))
            with IndexedQuerySession(root / "archive", root / "index") as s:
                with self.assertRaisesRegex(ValueError, "bounds"):
                    s.filter("contains", "alpha")

    def test_mismatch_empty_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, [])
            with IndexedQuerySession(root / "archive", root / "index") as s:
                self.assertEqual(s.filter("contains", "alpha")["row_ids"], [])
                self.assertEqual(s.filter("contains", "")["row_ids"], [])
                for bounds in ((-1, 0), (0, 1), [0, 0], (False, 0)):
                    with self.assertRaises(ValueError):
                        s.filter("contains", "alpha", row_range=bounds)
                for op, value in (("regex", "a"), ("contains", None)):
                    with self.assertRaises(ValueError):
                        s.filter(op, value)
                with self.assertRaises(UnicodeEncodeError):
                    s.filter("contains", "\ud800")
            path = root / "index/manifest.json"
            obj = json.loads(path.read_text())
            obj["dataset"]["canonical_sha256"] = "0" * 64
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "mismatch"):
                IndexedQuerySession(root / "archive", root / "index")

    def test_truncation_unsupported_version_and_fallback_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._build(root, ["alpha 12"])
            path = root / "index/manifest.json"
            original = path.read_text()
            obj = json.loads(original)
            obj["format"] = "unsupported"
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "format"):
                IndexedQuerySession(root / "archive", root / "index")
            path.write_text(original)
            # Covered queries do not read payloads; a fallback must still verify them.
            data = root / "archive/data.parquet"
            raw = bytearray(data.read_bytes())
            raw[10] ^= 1
            data.write_bytes(raw)
            with IndexedQuerySession(root / "archive", root / "index") as session:
                self.assertEqual(session.filter("contains", "alpha")["row_ids"], [0])
                with self.assertRaisesRegex(ValueError, "checksum"):
                    session.filter("contains", "12")
            with (root / "index/postings.sqlite").open("r+b") as stream:
                stream.truncate(100)
            with self.assertRaisesRegex(ValueError, "checksum"):
                IndexedQuerySession(root / "archive", root / "index")


class PreparedBloomTests(unittest.TestCase):
    def test_prepared_hashes_preserve_filter_decisions(self):
        rng = random.Random(17)
        for size in (10, 1000, 10000):
            grams = {rng.randbytes(3) for _ in range(size)}
            metadata = build_filter(grams)
            for needle in (b"", b"a", b"aa", b"aaaaaa", b"1131523501", *list(grams)[:50]):
                self.assertEqual(
                    PreparedTrigramQuery(needle).may_contain(*decode_filter(metadata)),
                    may_contain(metadata, needle),
                )

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from byteforge.dataset import prepare
from byteforge.native import NativeIntegers, worker_path
from byteforge.pipelines import TestByteEncoder, build_hybrid, build_parquet, restore
from byteforge.query import QuerySession
from byteforge.representation import pack_unsigned, unpack_unsigned

HAS_ARROW = bool(importlib.util.find_spec("pyarrow"))


@unittest.skipUnless(HAS_ARROW, "Optional PyArrow not installed")
class QueryTests(unittest.TestCase):
    def test_lz4_parquet_queries_match_reference(self):
        values = ("WARN alpha", "no match", "WARN 😀", "WARN alpha")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw").write_text("\n".join(values), encoding="utf-8")
            prepare(root / "raw", root / "input", synthetic=True)
            build_parquet(root / "input", root / "archive", profile="lz4")
            with QuerySession(root / "archive", cache_bytes=0) as session:
                self.assertEqual(session.filter("contains", "WARN")["row_ids"], [0, 2, 3])
                self.assertEqual(session.filter("equals", "no match")["row_ids"], [1])

    def test_streamed_and_bounded_cached_queries_match_reference(self):
        values = ("", "alpha", "alpha", "😀é", "tail", "head", " whitespace\r", "x" * 70000)
        for pipeline in ("A", "B"):
            if pipeline == "B" and not worker_path().is_file():
                continue
            with self.subTest(pipeline=pipeline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "raw").write_text("\n".join(values), encoding="utf-8", newline="")
                prepare(root / "raw", root / "input", synthetic=True)
                if pipeline == "A":
                    build_parquet(root / "input", root / "archive", profile="fast-scan")
                else:
                    build_hybrid(root / "input", root / "archive", TestByteEncoder(), worker_path())
                for budget in (0, 1, 1 << 20):
                    with QuerySession(root / "archive", cache_bytes=budget) as session:
                        for operation in ("equals", "contains"):
                            for value in ("", "alpha", "é", "😀", "lhead", "\r", "not-present"):
                                expected = [
                                    i
                                    for i, row in enumerate(values)
                                    if (row == value if operation == "equals" else value in row)
                                ]
                                result = session.filter(operation, value)
                                self.assertEqual(result["row_ids"], expected)
                                self.assertEqual(result["count"], len(expected))
                                self.assertLessEqual(result["cache_buffer_bytes"], budget)
                        self.assertEqual(result["cache_hit"], budget == 1 << 20)
                        with self.assertRaises(ValueError):
                            session.filter("regex", "x")
                        with self.assertRaises(UnicodeEncodeError):
                            session.filter("contains", "\ud800")
                    with self.assertRaisesRegex(ValueError, "closed"):
                        session.filter("contains", "x")

    def test_empty_archives_and_invalid_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw").write_bytes(b"")
            prepare(root / "raw", root / "input", synthetic=True)
            build_parquet(root / "input", root / "archive")
            with QuerySession(root / "archive", cache_bytes=1024) as session:
                self.assertEqual(session.filter("contains", "")["row_ids"], [])
                self.assertTrue(session.filter("equals", "")["cache_hit"])
            with self.assertRaises(ValueError):
                QuerySession(root / "archive", cache_bytes=-1)

    @unittest.skipUnless(worker_path().is_file(), "Optional native worker not built")
    def test_fast_decoder_still_rejects_bad_ids_and_row_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw").write_bytes(b"a\nb\n")
            prepare(root / "raw", root / "input", synthetic=True)
            build_hybrid(root / "input", root / "archive", TestByteEncoder(), worker_path())
            original = NativeIntegers.transform
            for corrupt_call in (1, 2):
                calls = 0

                def corrupt(native, operation, count, data, which=corrupt_call):
                    nonlocal calls
                    raw, scheme = original(native, operation, count, data)
                    target_operation = 3 if which == 1 else 2
                    if operation == target_operation:
                        calls += 1
                        if calls == 1:
                            if which == 1 and operation == 3:
                                raw = bytes([255]) + raw[1:]
                            else:
                                values = list(unpack_unsigned(raw))
                                values[0] = 1
                                raw = pack_unsigned(values)
                    return raw, scheme

                with (
                    patch.object(NativeIntegers, "transform", corrupt),
                    self.assertRaises(ValueError),
                ):
                    restore(root / "archive", root / f"bad-{corrupt_call}", worker=worker_path())


if __name__ == "__main__":
    unittest.main()

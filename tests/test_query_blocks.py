import importlib.util
import tempfile
import unittest
from pathlib import Path

from byteforge.dataset import prepare
from byteforge.native import worker_path
from byteforge.pipelines import (
    TestByteEncoder,
    build_hybrid,
    build_hybrid_blocked,
    load_archive,
    restore,
)
from byteforge.query import QuerySession
from byteforge.query_index import build_filter, may_contain

HAS_ARROW = bool(importlib.util.find_spec("pyarrow"))
HAS_NATIVE = worker_path().is_file()


@unittest.skipUnless(HAS_ARROW and HAS_NATIVE, "Optional Arrow/native dependencies unavailable")
class QueryBlockTests(unittest.TestCase):
    def _build(self, root: Path, values: tuple[str, ...], *, block_rows: int = 2) -> Path:
        raw = root / "raw.log"
        raw.write_bytes("\n".join(values).encode("utf-8"))
        prepared = root / "prepared"
        prepare(raw, prepared, synthetic=True)
        archive = root / "archive"
        build_hybrid_blocked(
            prepared,
            archive,
            TestByteEncoder(),
            worker_path(),
            block_rows=block_rows,
        )
        return archive

    def test_multiple_blocks_exact_restore_and_reference_queries(self):
        values = ("WARN alpha WARN", "βeta", "WARN alpha", "", "absent", "WARN")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self._build(root, values)
            restore(archive, root / "restored.bin", worker=worker_path())
            self.assertEqual(
                (root / "restored.bin").read_bytes(), (root / "prepared/rows.bin").read_bytes()
            )
            queries = (
                ("contains", "WARN", None),
                ("contains", "WARN", (1, 5)),
                ("contains", "alpha", None),
                ("contains", "warn", None),
                ("contains", "β", None),
                ("contains", "definitely absent sentinel", None),
                ("equals", "WARN alpha", None),
                ("equals", "", None),
                ("prefix", "WARN", None),
                ("contains", "", None),
                ("prefix", "", None),
            )
            with QuerySession(archive, worker=worker_path(), cache_bytes=0) as session:
                for operation, value, row_range in queries:
                    with self.subTest(operation=operation, value=value, row_range=row_range):
                        result = session.filter(operation, value, row_range=row_range)
                        start, stop = row_range or (0, len(values))
                        reference = [
                            row_id
                            for row_id, row in enumerate(values)
                            if start <= row_id < stop
                            and (
                                row == value
                                if operation == "equals"
                                else row.startswith(value)
                                if operation == "prefix"
                                else value in row
                            )
                        ]
                        self.assertEqual(result["row_ids"], reference)
                        self.assertEqual(result["count"], len(reference))
                pruned = session.filter("contains", "definitely absent sentinel")
                self.assertEqual(pruned["blocks_decoded"], 0)
                self.assertEqual(pruned["blocks_pruned"], 3)
                self.assertEqual(pruned["compressed_bytes_read"], 0)
                short = session.filter("contains", "a")
                self.assertEqual(short["blocks_examined"], 3)
            with QuerySession(archive, worker=worker_path(), cache_bytes=0) as session:
                session.filter("contains", "definitely absent sentinel")
                self.assertIsNone(session.native)

    def test_lru_cache_is_bounded_and_reports_evictions(self):
        values = tuple(f"{'x' * 100}{i}" for i in range(6))
        with tempfile.TemporaryDirectory() as temp:
            archive = self._build(Path(temp), values)
            with QuerySession(archive, worker=worker_path(), cache_bytes=300) as session:
                first = session.filter("contains", "xxx")
                self.assertLessEqual(first["cache_buffer_bytes"], 300)
                self.assertGreater(first["cache_evictions"], 0)
                self.assertGreater(first["cache_misses"], 0)
                second = session.filter("contains", "xxx", row_range=(4, 6))
                self.assertEqual(second["row_ids"], [4, 5])
                self.assertGreater(second["cache_hits"], 0)
                self.assertEqual(second["cache_misses"], 0)
                self.assertLessEqual(second["cache_buffer_bytes"], 300)

    def test_changed_archive_invalidates_open_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = self._build(Path(temp), ("alpha", "beta"))
            with QuerySession(archive, worker=worker_path(), cache_bytes=1 << 20) as session:
                session.filter("contains", "alpha")
                data = archive / "data.bin"
                content = bytearray(data.read_bytes())
                content[0] ^= 1
                data.write_bytes(content)
                with self.assertRaisesRegex(RuntimeError, "changed during"):
                    session.filter("contains", "alpha")
                self.assertEqual(session.block_cache_size, 0)

    def test_candidate_frame_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = self._build(Path(temp), ("alpha", "beta"))
            block = load_archive(archive)["_validated_block_index"]["blocks"][0]
            data = archive / "data.bin"
            content = bytearray(data.read_bytes())
            content[block["data_offset"] + block["frame_bytes"] // 2] ^= 1
            data.write_bytes(content)
            with QuerySession(archive, worker=worker_path(), cache_bytes=0) as session:
                with self.assertRaises(ValueError):
                    session.filter("contains", "alpha")

    def test_index_checksum_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = self._build(Path(temp), ("alpha", "beta"))
            path = archive / "index.json.zst"
            compressed = bytearray(path.read_bytes())
            compressed[-1] ^= 1
            path.write_bytes(compressed)
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_archive(archive)

    def test_empty_archive_and_invalid_query_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = self._build(Path(temp), ())
            with QuerySession(archive, worker=worker_path()) as session:
                self.assertEqual(session.filter("contains", "")["row_ids"], [])
                for bad_range in ((-1, 0), (1, 0), (0, 1), [0, 0]):
                    with self.assertRaises(ValueError):
                        session.filter("contains", "a", row_range=bad_range)
                for operation, value in (("regex", "a"), ("contains", 5)):
                    with self.assertRaises(ValueError):
                        session.filter(operation, value)
                with self.assertRaises(UnicodeEncodeError):
                    session.filter("contains", "\ud800")

    def test_v1_archive_remains_queryable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.log"
            raw.write_text("alpha\nbeta\nalpha", encoding="utf-8")
            prepare(raw, root / "prepared", synthetic=True)
            build_hybrid(root / "prepared", root / "archive", TestByteEncoder(), worker_path())
            with QuerySession(root / "archive", worker=worker_path(), cache_bytes=0) as session:
                self.assertEqual(session.filter("equals", "alpha")["row_ids"], [0, 2])


class TrigramFilterTests(unittest.TestCase):
    def test_inserted_trigrams_are_never_pruned(self):
        values = ("ASCII punctuation: /blk_42!", "é🙂漢字", "combining e\u0301")
        grams = {
            value.encode()[index : index + 3]
            for value in values
            for index in range(len(value.encode()) - 2)
        }
        bloom = build_filter(grams)
        for value in values:
            encoded = value.encode()
            for start in range(len(encoded) - 2):
                self.assertTrue(may_contain(bloom, encoded[start : start + 3]))

    def test_large_filters_expand_instead_of_saturating_at_small_cap(self):
        grams = {index.to_bytes(3, "little") for index in range(9000)}
        bloom = build_filter(grams)
        self.assertEqual(bloom["bits"], 65536)
        for gram in grams:
            self.assertTrue(may_contain(bloom, gram))

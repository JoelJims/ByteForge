import tempfile
import unittest
from pathlib import Path

from byteforge.dataset import HDFS_2K, identify_input, prepare, reconstruct_raw, row_windows


class DatasetTests(unittest.TestCase):
    def test_sample_identity_requires_checksum_and_dimensions(self):
        identity = identify_input(Path("renamed.log"), HDFS_2K["official_sha256"], 2000, 285848)
        self.assertEqual(identity[0], "hdfs_2k_sample")
        for digest, count in (("0" * 64, 2000), (HDFS_2K["official_sha256"], 1999)):
            result = identify_input(Path("HDFS_2k.log"), digest, count, 285848)
            self.assertEqual(result[0], "hdfs_2k_candidate")

    def test_physical_and_logical_roundtrips(self):
        for data, expected in (
            (b"", ()),
            (b"\n", ("",)),
            (b"a\n", ("a",)),
            (b"a\n\n", ("a", "")),
            (b"a\r\nb\r", ("a\r", "b\r")),
            ("😀\n中文".encode(), ("😀", "中文")),
            (b" \x00 \n", (" \x00 ",)),
        ):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "raw.log").write_bytes(data)
                manifest = prepare(root / "raw.log", root / "prepared", synthetic=True)
                rows = tuple(
                    row for batch in row_windows(root / "prepared/rows.bin") for row in batch
                )
                self.assertEqual(rows, expected)
                self.assertEqual(manifest["payload_bytes"], sum(len(r.encode()) for r in expected))
                reconstruct_raw(root / "prepared/rows.bin", manifest, root / "restored.log")
                self.assertEqual((root / "restored.log").read_bytes(), data)

    def test_rejects_invalid_input_and_keeps_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw.log").write_bytes(b"okay\n\xff\n")
            with self.assertRaisesRegex(ValueError, "row 2, source byte 5"):
                prepare(root / "raw.log", root / "out")
            self.assertTrue((root / "out/failure.json").exists())
            with self.assertRaises(FileNotFoundError):
                prepare(root / "missing.log", root / "missing")

    def test_deterministic_manifest_and_universal_line_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw.log").write_bytes("a\u2028b\u0085c\n".encode())
            first = prepare(root / "raw.log", root / "one", synthetic=True)
            second = prepare(root / "raw.log", root / "two", synthetic=True)
            self.assertEqual(first, second)
            self.assertEqual(tuple(row_windows(root / "one/rows.bin")), (("a\u2028b\u0085c",),))


if __name__ == "__main__":
    unittest.main()

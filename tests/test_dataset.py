import tempfile
import unittest
from pathlib import Path

from byteforge.dataset import prepare, reconstruct_raw, row_windows


class DatasetTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

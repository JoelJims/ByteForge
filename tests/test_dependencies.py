import importlib.util
import unittest

from byteforge.qualification import FIXTURE, parquet_roundtrip, zstd_roundtrip


@unittest.skipUnless(importlib.util.find_spec("pyarrow"), "pyarrow not installed")
class ParquetTests(unittest.TestCase):
    def test_utf8_null_and_empty(self):
        for rows in ((), ("",), ("", "", ""), FIXTURE, (None, "", None, "中文")):
            with self.subTest(rows=rows):
                parquet_roundtrip(rows)

    def test_invalid_unicode(self):
        with self.assertRaises(UnicodeEncodeError):
            parquet_roundtrip(("\ud800",))


@unittest.skipUnless(importlib.util.find_spec("zstandard"), "zstandard not installed")
class ZstdTests(unittest.TestCase):
    def test_integer_frames(self):
        for width in (16, 32):
            for values in ((), (0, 65535), tuple(range(65536))):
                with self.subTest(width=width, count=len(values)):
                    zstd_roundtrip(values, width)


if __name__ == "__main__":
    unittest.main()

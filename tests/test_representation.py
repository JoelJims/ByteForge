"""Synthetic byte symbols test framing only; they are not an LLM tokenizer."""

import unittest

from byteforge.representation import (
    INT32_MAX,
    TokenRows,
    leaf_blocks,
    pack_unsigned,
    unpack_unsigned,
)
from byteforge.tokenizers import encode_rows


class FixtureEncoder:
    identity = "test-only-byte-symbols"
    vocabulary = tuple(bytes([value]) for value in range(256))

    def encode(self, value):
        return tuple(value.encode("utf-8"))


class RepresentationTests(unittest.TestCase):
    def test_explicit_little_endian_and_bounds(self):
        self.assertEqual(pack_unsigned((1, 65535), 16), b"\x01\x00\xff\xff")
        for width in (16, 32):
            values = (0, 1, (1 << width) - 1)
            self.assertEqual(unpack_unsigned(pack_unsigned(values, width), width), values)
            for bad in (-1, 1 << width, True, 1.0):
                with self.assertRaises(ValueError):
                    pack_unsigned((bad,), width)
        self.assertEqual(
            unpack_unsigned(pack_unsigned((INT32_MAX,), native_int32=True)), (INT32_MAX,)
        )
        with self.assertRaises(ValueError):
            pack_unsigned((INT32_MAX + 1,), native_int32=True)
        for width, data in ((8, b""), (32, b"abc"), (16, b"a")):
            with self.assertRaises(ValueError):
                unpack_unsigned(data, width)

    def test_exact_rows_and_token_stability(self):
        fixtures = (
            (),
            ("",),
            ("", "", ""),
            (None, "", None),
            ("\x00", "hello", "hello", "😀", "中文", "العربية", "é", "e\u0301", "a\nb"),
        )
        for rows in fixtures:
            with self.subTest(rows=rows):
                encoded = encode_rows(rows, FixtureEncoder())
                self.assertEqual(encoded.restore(FixtureEncoder.vocabulary), rows)
        for row in fixtures[-1]:
            a = encode_rows((row,), FixtureEncoder())
            b = encode_rows(("neighbor", row, ""), FixtureEncoder())
            self.assertEqual(a.ids, b.ids[b.offsets[1] : b.offsets[2]])

    def test_leaf_and_row_boundaries(self):
        for count in (65535, 65536, 65537):
            rows = ("x" * count, "", "é")
            encoded = encode_rows(rows, FixtureEncoder())
            chunks = leaf_blocks(encoded.ids)
            self.assertTrue(all(len(chunk) <= 65536 for chunk in chunks))
            self.assertEqual(tuple(value for chunk in chunks for value in chunk), encoded.ids)
            self.assertEqual(encoded.restore(FixtureEncoder.vocabulary), rows)
        many_rows = encode_rows(("",) * 65536, FixtureEncoder())
        self.assertEqual(tuple(map(len, leaf_blocks(many_rows.offsets))), (65536, 1))
        self.assertEqual(leaf_blocks(()), ())

    def test_invalid_metadata(self):
        for encoded in (
            TokenRows((), ()),
            TokenRows((), (1,)),
            TokenRows((0,), (0, 0)),
            TokenRows((0, 0), (0, 2, 1, 2)),
            TokenRows((256,), (0, 1)),
            TokenRows((), (0, 0), ()),
            TokenRows((0,), (0, 1), (False,)),
            TokenRows((), (0, 0), (1,)),
        ):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                encoded.validate(256)
        with self.assertRaises(UnicodeEncodeError):
            encode_rows(("\ud800",), FixtureEncoder())
        with self.assertRaises(UnicodeDecodeError):
            TokenRows((255,), (0, 1)).restore(FixtureEncoder.vocabulary)


if __name__ == "__main__":
    unittest.main()

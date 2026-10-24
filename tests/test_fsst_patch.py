"""Patch refuses source drift and silently reusing a patched pristine variant."""

import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "fsst_patch", Path(__file__).resolve().parents[1] / "probes/btrblocks/fsst_sample_patch.py"
)
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class FsstPatchTests(unittest.TestCase):
    def test_variant_and_idempotence(self):
        source = "before " + PATCH.ORIGINAL + " middle " + PATCH.BUFFER_ORIGINAL + " after"
        corrected = "before " + PATCH.FIXED + " middle " + PATCH.BUFFER_FIXED + " after"
        self.assertEqual(PATCH.qualify_source(source, False), source)
        self.assertEqual(PATCH.qualify_source(source, True), corrected)
        self.assertEqual(PATCH.qualify_source(corrected, True), corrected)
        with self.assertRaises(ValueError):
            PATCH.qualify_source(corrected, False)
        with self.assertRaises(ValueError):
            PATCH.qualify_source("unrecognized", True)


if __name__ == "__main__":
    unittest.main()

"""Opt-in buffer-bounds correction for pinned FSST, preserving sample selection.

The original bound assumes each sample chunk has 512 bytes, but a selected
short row contributes fewer. Each chosen row is nonempty, so the target byte
count bounds the number of sample entries. Scalar compression additionally
requires addressable padding for its eight-byte loads at the end of a chunk.
"""

import argparse
from pathlib import Path

ORIGINAL = "new size_t[nlines + FSST_SAMPLEMAXSZ/FSST_SAMPLELINE]"
FIXED = "new size_t[FSST_SAMPLETARGET]"
BUFFER_ORIGINAL = "u8 buf[512], *cur = NULL"
BUFFER_FIXED = "u8 buf[512 + 8] = {}, *cur = NULL"


def qualify_source(source: str, apply: bool) -> str:
    for original, fixed in ((ORIGINAL, FIXED), (BUFFER_ORIGINAL, BUFFER_FIXED)):
        if source.count(original) == 1 and fixed not in source:
            if apply:
                source = source.replace(original, fixed)
        elif original not in source and source.count(fixed) == 1 and apply:
            continue
        else:
            raise ValueError("Unexpected FSST source/variant; use a fresh build directory")
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("apply", choices=("ON", "OFF"))
    args = parser.parse_args()
    original = args.source.read_text()
    corrected = qualify_source(original, args.apply == "ON")
    if original != corrected:
        args.source.write_text(corrected)
    print("FSST buffer-bounds-v1 variant:", args.apply)

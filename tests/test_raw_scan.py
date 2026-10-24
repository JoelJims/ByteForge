"""Correctness tests for the optional native raw-log scanner."""

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "probes/raw_scan/raw_query.cpp"


@pytest.fixture(scope="module")
def raw_query_binary(tmp_path_factory):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required to build the optional native scanner")
    binary = tmp_path_factory.mktemp("raw-query") / "raw_query"
    subprocess.run(
        [
            compiler,
            "-O3",
            "-DNDEBUG",
            "-std=c++17",
            "-march=native",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            str(SOURCE),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    ("contents", "operation", "needle"),
    [
        (b"", "contains", b""),
        (b"a\n\nxxa\r\n", "contains", b"a"),
        (b"a\n\nxxa\r\n", "contains", b""),
        (b"a\n\nxxa\r\n", "equals", b"a"),
        (b"a\n\nxxa\r\n", "prefix", b"a"),
        ("café\nlast".encode(), "contains", "é".encode()),
        (b"abc\nxyz\n", "contains", b"bc\nx"),
        (b"needle\n" * 40 + b"needle", "contains", b"needle"),
        (b"x\n" * 41 + b"tail", "contains", b"tail"),
    ],
)
def test_native_raw_scan_matches_line_reference(
    raw_query_binary, tmp_path, contents, operation, needle
):
    source = tmp_path / "raw.log"
    source.write_bytes(contents)
    rows = contents.split(b"\n")
    if contents.endswith(b"\n"):
        rows.pop()
    if not contents:
        rows = []
    if operation == "contains":
        expected = [i for i, row in enumerate(rows) if needle in row]
    elif operation == "equals":
        expected = [i for i, row in enumerate(rows) if needle == row]
    else:
        expected = [i for i, row in enumerate(rows) if row.startswith(needle)]

    result = subprocess.run(
        [str(raw_query_binary), str(source), operation, needle.hex()],
        check=True,
        capture_output=True,
    )
    count = struct.unpack_from("<Q", result.stdout)[0]
    actual = list(struct.unpack_from(f"<{count}Q", result.stdout, 8)) if count else []
    diagnostics = json.loads(result.stderr)

    assert actual == expected
    assert diagnostics["rows"] == len(rows)
    assert diagnostics["payload_bytes"] == sum(map(len, rows))

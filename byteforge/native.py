"""Bounded subprocess adapter. No codec substitution if the worker is missing."""

import json
import os
import select
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from byteforge.dataset import checksum


def worker_path() -> Path:
    return Path(os.environ.get("BYTEFORGE_BTR_WORKER", "build/btrblocks/byteforge_integer_worker"))


def worker_info(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"BtrBlocks integer worker missing: {path}; see docs/hdfs_benchmark.md"
        )
    info = json.loads(subprocess.check_output([str(path.resolve()), "--info"], timeout=30))
    if info.get("backend") != "btrblocks" or info.get("protocol") != 2:
        raise ValueError("Unsupported native worker")
    info["executable_sha256"] = checksum(path)
    return info


class NativeIntegers:
    def __init__(self, path: Path, *, expected: dict | None = None):
        self.info = worker_info(path)
        if expected is not None and self.info != expected:
            raise ValueError("Native decoder build/ABI does not match archive")
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [str(path.resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            bufsize=0,
        )
        self.phase_ns = 0
        self.codec_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            code = -1
        self.process.stdout.close()
        self.stderr.seek(0)
        error = self.stderr.read(8192).decode(errors="replace")
        self.stderr.close()
        if code and exc_type is None:
            raise RuntimeError(f"Native worker failed ({code}): {error}")

    def _read(self, size: int) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + 60
        while len(result) < size:
            if not select.select(
                [self.process.stdout], [], [], max(0, deadline - time.monotonic())
            )[0]:
                self.process.kill()
                raise TimeoutError("Native worker response timed out")
            part = os.read(self.process.stdout.fileno(), size - len(result))
            if not part:
                self.stderr.seek(0)
                raise RuntimeError(
                    "Native worker ended: " + self.stderr.read(8192).decode(errors="replace")
                )
            result.extend(part)
        return bytes(result)

    def transform(self, operation: int, count: int, data: bytes) -> tuple[bytes, int]:
        if operation not in (1, 2) or not 1 <= count <= 65536 or len(data) > 4 << 20:
            raise ValueError("Invalid native request")
        start = time.perf_counter_ns()
        packet = memoryview(struct.pack("<BII", operation, count, len(data)) + data)
        while packet:
            written = self.process.stdin.write(packet)
            if not written:
                raise RuntimeError("Native request pipe closed")
            packet = packet[written:]
        size, scheme, codec_bytes = struct.unpack("<III", self._read(12))
        if size > 4 << 20 or codec_bytes > size or (operation == 2 and size != count * 4):
            raise ValueError("Invalid native response length")
        result = self._read(size)
        self.codec_bytes += codec_bytes
        self.phase_ns += time.perf_counter_ns() - start
        return result, scheme

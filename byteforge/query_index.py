"""Small, deterministic, no-false-negative byte-trigram Bloom filters."""

import base64
import hashlib
import math
import struct

TRIGRAM_BYTES = 3
MIN_BITS = 2048
MAX_BITS = 65536
HASHES = 5


def trigrams(data: bytes) -> set[bytes]:
    return {data[pos : pos + TRIGRAM_BYTES] for pos in range(len(data) - 2)}


def _positions(gram: bytes, bit_count: int):
    digest = hashlib.blake2b(gram, digest_size=16, person=b"BF-trigram-v1").digest()
    first, second = struct.unpack("<QQ", digest)
    second |= 1
    for index in range(HASHES):
        yield (first + index * second + index * index) % bit_count


def build_filter(values: set[bytes]) -> dict:
    # Eight bits per distinct gram targets a useful false-positive rate. The larger
    # cap keeps filters selective for larger row blocks; filters never false-negative.
    bit_count = max(MIN_BITS, math.ceil(len(values) * 8 / 64) * 64)
    bit_count = min(bit_count, MAX_BITS)
    bits = bytearray(bit_count // 8)
    for gram in values:
        if len(gram) != TRIGRAM_BYTES:
            raise ValueError("Trigram filter received an invalid gram")
        for position in _positions(gram, bit_count):
            bits[position >> 3] |= 1 << (position & 7)
    return {"bits": bit_count, "hashes": HASHES, "data": base64.b64encode(bits).decode("ascii")}


def decode_filter(metadata: dict) -> tuple[int, bytes]:
    if not isinstance(metadata, dict):
        raise ValueError("Invalid trigram filter")
    bit_count, hashes, encoded = (
        metadata.get("bits"),
        metadata.get("hashes"),
        metadata.get("data"),
    )
    if (
        type(bit_count) is not int
        or not MIN_BITS <= bit_count <= MAX_BITS
        or bit_count % 64
        or hashes != HASHES
        or not isinstance(encoded, str)
    ):
        raise ValueError("Unsupported trigram filter configuration")
    try:
        bits = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid trigram filter encoding") from error
    if len(bits) != bit_count // 8:
        raise ValueError("Invalid trigram filter length")
    return bit_count, bits


def may_contain(metadata: dict, query: bytes) -> bool:
    # Queries shorter than one full gram cannot be safely pruned by this index.
    if len(query) < TRIGRAM_BYTES:
        return True
    return may_contain_bits(*decode_filter(metadata), query)


def may_contain_bits(bit_count: int, bits: bytes, query: bytes) -> bool:
    if len(query) < TRIGRAM_BYTES:
        return True
    for pos in range(len(query) - 2):
        gram = query[pos : pos + TRIGRAM_BYTES]
        for bit in _positions(gram, bit_count):
            if not bits[bit >> 3] & (1 << (bit & 7)):
                return False
    return True

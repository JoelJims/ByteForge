"""Checked little-endian integer streams and row boundaries, before compression."""

import struct
from collections.abc import Sequence
from dataclasses import dataclass

INT32_MAX = (1 << 31) - 1
LEAF_ELEMENTS = 65_536


def pack_unsigned(values: Sequence[int], width: int = 32, *, native_int32: bool = False) -> bytes:
    """Pack D16/D32 streams; optionally enforce BtrBlocks' nonnegative int32 range."""
    if width not in (16, 32):
        raise ValueError("only uint16 and uint32 streams are supported")
    maximum = min((1 << width) - 1, INT32_MAX) if native_int32 else (1 << width) - 1
    if any(type(value) is not int or not 0 <= value <= maximum for value in values):
        raise ValueError(f"integer outside supported range 0..{maximum}")
    return struct.pack(f"<{len(values)}{'H' if width == 16 else 'I'}", *values)


def unpack_unsigned(data: bytes, width: int = 32) -> tuple[int, ...]:
    if width not in (16, 32) or len(data) % (width // 8):
        raise ValueError("invalid integer width or truncated stream")
    return struct.unpack(f"<{len(data) // (width // 8)}{'H' if width == 16 else 'I'}", data)


def leaf_blocks(values: Sequence[int], limit: int = LEAF_ELEMENTS) -> tuple[tuple[int, ...], ...]:
    if type(limit) is not int or not 1 <= limit <= LEAF_ELEMENTS:
        raise ValueError("leaf limit must be in 1..65536")
    return tuple(tuple(values[start : start + limit]) for start in range(0, len(values), limit))


@dataclass(frozen=True)
class TokenRows:
    """One local window. A validity tuple explicitly opts into the nullable extension."""

    ids: tuple[int, ...]
    offsets: tuple[int, ...]
    validity: tuple[bool, ...] | None = None

    def validate(self, vocabulary_size: int) -> None:
        if type(vocabulary_size) is not int or not 1 <= vocabulary_size <= INT32_MAX:
            raise ValueError("invalid vocabulary size")
        pack_unsigned(self.ids, native_int32=True)
        pack_unsigned(self.offsets, native_int32=True)
        if not self.offsets or self.offsets[0] != 0 or self.offsets[-1] != len(self.ids):
            raise ValueError("offsets must begin at zero and end at token count")
        if any(left > right for left, right in zip(self.offsets, self.offsets[1:], strict=False)):
            raise ValueError("offsets must be nondecreasing")
        if any(token >= vocabulary_size for token in self.ids):
            raise ValueError("token ID outside vocabulary")
        if self.validity is not None:
            if len(self.validity) != len(self.offsets) - 1:
                raise ValueError("validity length does not match row count")
            for index, present in enumerate(self.validity):
                if type(present) is not bool:
                    raise ValueError("validity entries must be bool")
                if not present and self.offsets[index] != self.offsets[index + 1]:
                    raise ValueError("null rows must not contain tokens")

    def restore(self, vocabulary: Sequence[bytes]) -> tuple[str | None, ...]:
        """Decode bytes before UTF-8: a token may split a multibyte character."""
        self.validate(len(vocabulary))
        if any(type(piece) is not bytes or not piece for piece in vocabulary):
            raise ValueError("vocabulary entries must be nonempty bytes")
        rows = []
        for index, (start, end) in enumerate(zip(self.offsets, self.offsets[1:], strict=False)):
            if self.validity is not None and not self.validity[index]:
                rows.append(None)
            else:
                rows.append(
                    b"".join(vocabulary[token] for token in self.ids[start:end]).decode("utf-8")
                )
        return tuple(rows)

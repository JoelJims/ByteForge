"""Replaceable tokenizer contract; SGTT is not bundled or silently replaced."""

from typing import Protocol

from byteforge.representation import TokenRows


class Tokenizer(Protocol):
    identity: str
    vocabulary: tuple[bytes, ...]

    def encode(self, value: str) -> tuple[int, ...]: ...


def encode_rows(rows: tuple[str | None, ...], tokenizer: Tokenizer) -> TokenRows:
    ids: list[int] = []
    offsets = [0]
    for value in rows:
        if value is not None:
            if type(value) is not str:
                raise ValueError("rows must contain strings or explicit nulls")
            value.encode("utf-8", errors="strict")
            if value:
                ids.extend(tokenizer.encode(value))
        offsets.append(len(ids))
    validity = tuple(value is not None for value in rows) if None in rows else None
    encoded = TokenRows(tuple(ids), tuple(offsets), validity)
    encoded.validate(len(tokenizer.vocabulary))
    if encoded.restore(tokenizer.vocabulary) != rows:
        raise ValueError("tokenizer failed exact row reconstruction")
    return encoded

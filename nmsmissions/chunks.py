"""LZ4 chunk reader and packer for Steam No Man's Sky saves.

The on-disk layout is public knowledge: a series of LZ4 blocks, each with a
16-byte header. Magic E5 A1 ED FE, then compressed size (u32 LE), uncompressed
size (u32 LE), then four zero bytes, then the block. Uncompressed pieces are
at most 524288 bytes. This module reimplements that layout. It does not come
from NMSSaveEditor or libNOM.

If the file does not start with the magic, it is treated as legacy plain JSON.
"""

from __future__ import annotations

import lz4.block

MAGIC = bytes.fromhex("E5A1EDFE")
HEADER_SIZE = 16
MAX_UNCOMPRESSED = 0x80000


class SaveFormatError(ValueError):
    """The bytes are not a No Man's Sky save payload."""


class SavePayload:
    """Decompressed save bytes, exactly as the chunks concatenated them."""

    def __init__(self, raw: bytes, chunked: bool) -> None:
        self.raw = raw
        self.chunked = chunked

    @property
    def json_text(self) -> str:
        body = self.raw
        if body.endswith(b"\x00"):
            body = body.rstrip(b"\x00")
        # Saves store raw bytes inside JSON strings (product ids and similar).
        # surrogateescape keeps those bytes so a later encode is exact.
        return body.decode("utf-8", errors="surrogateescape")


def unpack_save(data: bytes) -> SavePayload:
    if not data.startswith(MAGIC):
        return SavePayload(data, chunked=False)
    parts: list[bytes] = []
    offset = 0
    total = len(data)
    while offset < total:
        if total - offset < HEADER_SIZE:
            raise SaveFormatError(f"Truncated chunk header at offset {offset}.")
        if data[offset : offset + 4] != MAGIC:
            raise SaveFormatError(f"Bad chunk magic at offset {offset}.")
        compressed_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        uncompressed_size = int.from_bytes(data[offset + 8 : offset + 12], "little")
        if compressed_size < 1 or uncompressed_size < 1:
            raise SaveFormatError(
                f"Empty chunk size at offset {offset} "
                f"(compressed {compressed_size}, uncompressed {uncompressed_size})."
            )
        if uncompressed_size > MAX_UNCOMPRESSED:
            raise SaveFormatError(
                f"Uncompressed chunk is {uncompressed_size} bytes. "
                f"The save format stops at {MAX_UNCOMPRESSED}."
            )
        start = offset + HEADER_SIZE
        end = start + compressed_size
        if end > total:
            raise SaveFormatError(f"Truncated LZ4 block at offset {offset}.")
        block = data[start:end]
        try:
            plain = lz4.block.decompress(block, uncompressed_size=uncompressed_size)
        except Exception as exc:
            raise SaveFormatError(f"LZ4 decompress failed at offset {offset}: {exc}") from exc
        if len(plain) != uncompressed_size:
            raise SaveFormatError(
                f"Chunk at offset {offset} decompressed to {len(plain)} bytes, "
                f"header said {uncompressed_size}."
            )
        parts.append(plain)
        offset = end
    return SavePayload(b"".join(parts), chunked=True)


def pack_chunks(raw: bytes, chunk_size: int = MAX_UNCOMPRESSED) -> bytes:
    """Pack payload bytes into save chunks. Does not touch a real save file."""
    if chunk_size < 1 or chunk_size > MAX_UNCOMPRESSED:
        raise ValueError(f"chunk_size must be from 1 to {MAX_UNCOMPRESSED}.")
    if not raw:
        raise ValueError("Refusing to pack an empty payload.")
    out = bytearray()
    for start in range(0, len(raw), chunk_size):
        piece = raw[start : start + chunk_size]
        compressed = lz4.block.compress(piece, store_size=False)
        out += MAGIC
        out += len(compressed).to_bytes(4, "little")
        out += len(piece).to_bytes(4, "little")
        out += b"\x00\x00\x00\x00"
        out += compressed
    return bytes(out)

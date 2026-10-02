"""Read and rewrite the mf_saveN.hg manifest.

The file is one XXTEA buffer. Format 2004 is 432 bytes and uses 6 rounds.
A 104-byte file uses 8 rounds. Decrypting and encrypting an unchanged
buffer returns the same bytes. Size fields live at offsets 56 and 60.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0xEEEEEEBE
FORMAT_2004 = 0x7D4
DECOMPRESSED_OFFSET = 56
COMPRESSED_OFFSET = 60
PLAY_TIME_OFFSET = 76
META_LENGTH_2004 = 432
VANILLA_LENGTH = 104

_KEY_XOR = 0x1422CB8C
_KEY_ROTATE = 13
_KEY_MULTIPLY = 5
_KEY_ADD = 0xE6546B64
_DELTA = 0x9E3779B9
_REVERSE_DELTA = 0x61C88647
_KEY_TAIL = (0x44415645, 0x5259414E, 0x47524E54)
_SAVE_NAME = re.compile(r"^save(\d*)\.hg$", re.IGNORECASE)


class ManifestError(ValueError):
    """The mf_ file is missing, the wrong size, or did not decrypt."""


@dataclass
class Manifest:
    path: Path
    archive: int
    key_slot: int
    rounds: int
    plain: bytes
    magic: int
    format_version: int
    decompressed_size: int
    compressed_size: int
    total_play_time: int

    @property
    def format_ok(self) -> bool:
        return self.magic == MAGIC and self.format_version == FORMAT_2004


def manifest_path_for(save: Path) -> Path:
    return save.with_name("mf_" + save.name)


def archive_number(save: Path) -> int:
    """save7.hg is archive 6. save.hg and save1.hg are archive 0."""
    match = _SAVE_NAME.match(save.name)
    if match is None:
        raise ManifestError(f"{save.name} is not a saveN.hg file, so it has no archive number.")
    raw = match.group(1)
    if raw == "":
        return 0
    number = int(raw)
    if number < 1:
        raise ManifestError(f"{save.name} is not a save slot.")
    return number - 1


def key_slot_for(archive: int) -> int:
    """The cipher slot is the archive number plus 2. save1 uses slot 2."""
    return archive + 2


def derive_key(slot: int) -> tuple[int, int, int, int]:
    mixed = ((slot ^ _KEY_XOR) << _KEY_ROTATE) | ((slot ^ _KEY_XOR) >> (32 - _KEY_ROTATE))
    mixed &= 0xFFFFFFFF
    key0 = (mixed * _KEY_MULTIPLY + _KEY_ADD) & 0xFFFFFFFF
    return (key0, _KEY_TAIL[0], _KEY_TAIL[1], _KEY_TAIL[2])


def _words(data: bytes) -> list[int]:
    if len(data) % 4:
        raise ManifestError("The manifest length is not a multiple of 4.")
    return list(struct.unpack("<" + "I" * (len(data) // 4), data))


def _pack(words: list[int]) -> bytes:
    return struct.pack("<" + "I" * len(words), *[word & 0xFFFFFFFF for word in words])


def _mix(total: int, prev: int, current: int, key_word: int, acc: int) -> int:
    left = ((current >> 3) ^ ((prev << 4) & 0xFFFFFFFF)) & 0xFFFFFFFF
    right = ((current * 4) ^ (prev >> 5)) & 0xFFFFFFFF
    return (left + right) ^ (((prev ^ key_word) + (current ^ acc)) & 0xFFFFFFFF)


def xxtea_decrypt(data: bytes, key: tuple[int, int, int, int], rounds: int) -> bytes:
    words = _words(data)
    last = len(words) - 1
    acc = 0
    for _ in range(rounds):
        acc = (acc + _DELTA) & 0xFFFFFFFF
    for _ in range(rounds):
        key_index = (acc >> 2) & 3
        current = words[0]
        for index in range(last, 0, -1):
            prev = words[index - 1]
            mixed = _mix(0, prev, current, key[(index & 3) ^ key_index], acc)
            words[index] = (words[index] - mixed) & 0xFFFFFFFF
            current = words[index]
        prev = words[last]
        mixed = _mix(0, prev, current, key[key_index], acc)
        words[0] = (words[0] - mixed) & 0xFFFFFFFF
        acc = (acc + _REVERSE_DELTA) & 0xFFFFFFFF
    return _pack(words)


def xxtea_encrypt(data: bytes, key: tuple[int, int, int, int], rounds: int) -> bytes:
    words = _words(data)
    last = len(words) - 1
    acc = 0
    for _ in range(rounds):
        acc = (acc + _DELTA) & 0xFFFFFFFF
        key_index = (acc >> 2) & 3
        nxt = words[1]
        prev = words[last]
        mixed = _mix(0, prev, nxt, key[key_index], acc)
        words[0] = (words[0] + mixed) & 0xFFFFFFFF
        for index in range(1, last + 1):
            nxt = words[0] if index == last else words[index + 1]
            prev = words[index - 1]
            mixed = _mix(0, prev, nxt, key[(index & 3) ^ key_index], acc)
            words[index] = (words[index] + mixed) & 0xFFFFFFFF
    return _pack(words)


def rounds_for(length: int) -> int:
    return 8 if length == VANILLA_LENGTH else 6


def decrypt_manifest(data: bytes, archive: int) -> tuple[bytes, int]:
    """Decrypt with the archive's slot. If that misses, try the other slots."""
    if len(data) < 8:
        raise ManifestError("The manifest is too short to decrypt.")
    rounds = rounds_for(len(data))
    first = key_slot_for(archive)
    slots = [first] + [slot for slot in range(32) if slot != first]
    for slot in slots:
        plain = xxtea_decrypt(data, derive_key(slot), rounds)
        magic = struct.unpack_from("<I", plain, 0)[0]
        if magic == MAGIC:
            return plain, slot
    raise ManifestError("The manifest did not decrypt. The archive number may not match this file.")


def encrypt_manifest(plain: bytes, slot: int) -> bytes:
    return xxtea_encrypt(plain, derive_key(slot), rounds_for(len(plain)))


def _u32(plain: bytes, offset: int) -> int:
    return struct.unpack_from("<I", plain, offset)[0]


def read_manifest(path: Path, archive: int | None = None) -> Manifest:
    if not path.is_file():
        raise ManifestError(f"No manifest at {path}.")
    blob = path.read_bytes()
    archive_no = archive if archive is not None else archive_number(path.with_name(path.name[3:]))
    plain, slot = decrypt_manifest(blob, archive_no)
    return Manifest(
        path=path,
        archive=archive_no,
        key_slot=slot,
        rounds=rounds_for(len(blob)),
        plain=plain,
        magic=_u32(plain, 0),
        format_version=_u32(plain, 4),
        decompressed_size=_u32(plain, DECOMPRESSED_OFFSET),
        compressed_size=_u32(plain, COMPRESSED_OFFSET),
        total_play_time=_u32(plain, PLAY_TIME_OFFSET) if len(plain) > PLAY_TIME_OFFSET + 4 else 0,
    )


def with_sizes(manifest: Manifest, decompressed: int, compressed: int) -> bytes:
    """Return a new encrypted manifest. Only the two size fields change in the plain bytes."""
    plain = bytearray(manifest.plain)
    struct.pack_into("<I", plain, DECOMPRESSED_OFFSET, decompressed)
    struct.pack_into("<I", plain, COMPRESSED_OFFSET, compressed)
    return encrypt_manifest(bytes(plain), manifest.key_slot)


def size_bytes_differ_only_at_sizes(before: bytes, after: bytes) -> bool:
    """True when two plain manifests differ only at offsets 56 through 63."""
    if len(before) != len(after):
        return False
    for index, (left, right) in enumerate(zip(before, after)):
        if left != right and not DECOMPRESSED_OFFSET <= index < COMPRESSED_OFFSET + 4:
            return False
    return True

"""Patch values inside raw save JSON without rewriting the rest of the text.

json.dumps changes how the game spells some floats. These patches replace
only the spans they were given, from the end of the text backward, so every
untouched character stays identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class SpanError(ValueError):
    """A path was missing or the bytes at a span were not the expected value."""


@dataclass(frozen=True)
class Patch:
    start: int
    end: int
    old: str
    new: str
    label: str


def dumps(value: object) -> str:
    """Compact JSON. A zero float is the two characters the game writes: 0.0."""
    if isinstance(value, float) and value == 0.0:
        return "0.0"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _skip_ws(text: str, index: int) -> int:
    length = len(text)
    while index < length and text[index] in " \t\r\n":
        index += 1
    return index


def _skip_value(text: str, index: int) -> int:
    try:
        _parsed, end = json.JSONDecoder().raw_decode(text, index)
    except json.JSONDecodeError as exc:
        raise SpanError(f"Save JSON could not be read at character {index}.") from exc
    return end


def locate(text: str, path: list[object]) -> tuple[int, int]:
    """Return the character span of the value at path. Keys are str, indexes are int."""
    index = _skip_ws(text, 0)
    if not path:
        end = _skip_value(text, index)
        return index, end
    for depth, segment in enumerate(path):
        index = _skip_ws(text, index)
        last = depth == len(path) - 1
        if isinstance(segment, int):
            if index >= len(text) or text[index] != "[":
                raise SpanError(f"Expected a list at {path[:depth]}.")
            index += 1
            for _ignored in range(segment):
                index = _skip_ws(text, index)
                index = _skip_value(text, index)
                index = _skip_ws(text, index)
                if index >= len(text) or text[index] != ",":
                    raise SpanError(f"List index {segment} is past the end of {path[:depth]}.")
                index += 1
            index = _skip_ws(text, index)
            if last:
                end = _skip_value(text, index)
                return index, end
            continue
        if index >= len(text) or text[index] != "{":
            raise SpanError(f"Expected an object at {path[:depth]}.")
        index += 1
        found = False
        while True:
            index = _skip_ws(text, index)
            if index < len(text) and text[index] == "}":
                break
            key_start = index
            key_end = _skip_value(text, index)
            try:
                key = json.loads(text[key_start:key_end])
            except json.JSONDecodeError as exc:
                raise SpanError("A save object key was not a string.") from exc
            index = _skip_ws(text, key_end)
            if index >= len(text) or text[index] != ":":
                raise SpanError("A save object is missing a colon.")
            index = _skip_ws(text, index + 1)
            if key == segment:
                found = True
                if last:
                    end = _skip_value(text, index)
                    return index, end
                break
            index = _skip_value(text, index)
            index = _skip_ws(text, index)
            if index < len(text) and text[index] == ",":
                index += 1
                continue
            break
        if not found:
            raise SpanError(f"Save JSON has no {segment!r} at {path[:depth]}.")
    raise SpanError(f"Could not locate {path}.")


def replace_patch(text: str, path: list[object], new_text: str, label: str) -> Patch:
    start, end = locate(text, path)
    return Patch(start, end, text[start:end], new_text, label)


def append_patch(text: str, path: list[object], item_text: str, label: str) -> Patch:
    """Insert item_text before the closing bracket of the array at path."""
    start, end = locate(text, path)
    if end <= start or text[end - 1] != "]":
        raise SpanError(f"{label} is not a list.")
    inner = text[start + 1 : end - 1].strip()
    piece = item_text if not inner else "," + item_text
    at = end - 1
    return Patch(at, at, "", piece, label)


def apply(text: str, patches: list[Patch]) -> str:
    """Apply patches from the end of the text so earlier spans stay put."""
    ordered = sorted(patches, key=lambda patch: (patch.start, patch.end), reverse=True)
    for earlier, later in zip(ordered, ordered[1:]):
        if later.end > earlier.start:
            raise SpanError(f"Patches overlap: {later.label} and {earlier.label}.")
    result = text
    for patch in ordered:
        current = result[patch.start : patch.end]
        if current != patch.old:
            raise SpanError(f"{patch.label} no longer matches the save. Reload it.")
        result = result[: patch.start] + patch.new + result[patch.end :]
    return result


def unchanged_outside(original: str, patched: str, patches: list[Patch]) -> bool:
    """The characters outside every patch span are still the original characters."""
    if not patches:
        return original == patched
    ordered = sorted(patches, key=lambda patch: patch.start)
    cursor_old = 0
    cursor_new = 0
    for patch in ordered:
        if original[cursor_old : patch.start] != patched[cursor_new : cursor_new + (patch.start - cursor_old)]:
            return False
        cursor_new += patch.start - cursor_old
        if patched[cursor_new : cursor_new + len(patch.new)] != patch.new:
            return False
        cursor_old = patch.end
        cursor_new += len(patch.new)
    return original[cursor_old:] == patched[cursor_new:]

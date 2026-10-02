"""Read a Steam save, deobfuscate keys, and index one player context.

Version 1 never writes the save it was given. `verify_save` repacks into a
temp file and deletes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from nmsmissions.chunks import SaveFormatError, SavePayload, pack_chunks, unpack_save
from nmsmissions.guard import refuse_live_save

FLAG_NAMES = (
    "HasDiscoveredPurpleSystems",
    "PurpleSystemsUnlocked",
    "FirstPurpleSystemUA",
    "HasGalacticMapRequestFirstPurple",
    "HasGalacticMapRequestAllPurples",
    "CurrentMissionID",
    "PreviousMissionID",
)

CONTEXT_NAMES = {
    "base": "BaseContext",
    "expedition": "ExpeditionContext",
}
# A save this large is re-parsed in another process so the window can keep drawing.
_POOL_SAVE_BYTES = 256 * 1024
_breaths = 0


@dataclass
class FlagHit:
    name: str
    path: str
    value: object


@dataclass
class PlayerContext:
    name: str
    path: str
    missions: dict[str, dict]
    current_mission: str | None
    previous_mission: str | None
    time_alive: int | None = None
    reality_index: int | None = None
    mission_version: int | None = None


@dataclass
class LoadedSave:
    path: Path
    payload: SavePayload
    sha256: str
    mtime_ns: int
    data: OrderedDict
    mapping_source: str
    active_context: object
    contexts: dict[str, PlayerContext] = field(default_factory=dict)
    flags: list[FlagHit] = field(default_factory=list)
    total_play_time: int | None = None


def _breathe() -> None:
    """Release the interpreter during a long tree walk."""
    global _breaths
    _breaths += 1
    if _breaths < 400:
        return
    _breaths = 0
    time.sleep(0)


def deobfuscate(node: object, table: dict[str, str]) -> object:
    """Rename known 3-character keys. Unknown keys stay. Order stays."""
    if isinstance(node, list):
        _breathe()
        return [deobfuscate(item, table) for item in node]
    if isinstance(node, dict):
        _breathe()
        out: OrderedDict[str, object] = OrderedDict()
        for key, value in node.items():
            name = table.get(str(key), str(key))
            child = deobfuscate(value, table)
            final = name
            if final in out:
                final = f"{name}#{key}"
                spare = 2
                while final in out:
                    final = f"{name}#{key}#{spare}"
                    spare += 1
            out[final] = child
        return out
    return node


def reobfuscate(node: object, reverse: dict[str, str]) -> object:
    """Rename plain field names back to the 3-character keys."""
    if isinstance(node, list):
        return [reobfuscate(item, reverse) for item in node]
    if isinstance(node, dict):
        out: OrderedDict[str, object] = OrderedDict()
        for key, value in node.items():
            child = reobfuscate(value, reverse)
            text = str(key)
            if "#" in text:
                _base, _, rest = text.partition("#")
                orig = rest.split("#", 1)[0]
            else:
                orig = reverse.get(text, text)
            if orig in out:
                orig = f"{orig}#{text}"
            out[orig] = child
        return out
    return node


def same_structure(left: object, right: object) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if list(left.keys()) != list(right.keys()):
            return False
        return all(same_structure(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_structure(a, b) for a, b in zip(left, right))
    return left == right


def _whole_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    return int(value)


def _walk(node: object, path: tuple[str, ...] = ()):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (str(index),))


def _context_name(path: tuple[str, ...]) -> str:
    for part in reversed(path):
        if part in ("BaseContext", "ExpeditionContext"):
            return part
    return "root"


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _missions(state: dict) -> dict[str, dict]:
    rows = state.get("MissionProgress")
    found: dict[str, dict] = {}
    if not isinstance(rows, list):
        return found
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        mission_id = entry.get("Mission")
        if isinstance(mission_id, str) and mission_id:
            found[mission_id] = entry
    return found


def _index(data: OrderedDict) -> tuple[object, dict[str, PlayerContext], list[FlagHit]]:
    active = None
    contexts: dict[str, PlayerContext] = {}
    flags: list[FlagHit] = []
    for path, node in _walk(data):
        for name in FLAG_NAMES:
            if name not in node:
                continue
            value = node[name]
            if isinstance(value, (dict, list)):
                continue
            flags.append(FlagHit(name, "/".join(path + (name,)), value))
        if "ActiveContext" in node and not isinstance(node["ActiveContext"], (dict, list)):
            if active is None:
                active = node["ActiveContext"]
        if not path:
            continue
        leaf = path[-1]
        if leaf != "PlayerStateData" and not leaf.startswith("PlayerStateData#"):
            continue
        if "MissionProgress" not in node:
            continue
        context_name = _context_name(path)
        if context_name in contexts:
            continue
        version = _whole_number(node.get("MissionVersion"))
        if version is None:
            version = _whole_number(node.get("yq:"))
        contexts[context_name] = PlayerContext(
            name=context_name,
            path="/".join(path),
            missions=_missions(node),
            current_mission=_string_or_none(node.get("CurrentMissionID")),
            previous_mission=_string_or_none(node.get("PreviousMissionID")),
            time_alive=_whole_number(node.get("TimeAlive")),
            reality_index=_reality_index(node),
            mission_version=version,
        )
    return active, contexts, flags


def load_save(
    path: Path | str,
    mapping: dict[str, str],
    mapping_source: str,
    allow: bool = False,
) -> LoadedSave:
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0
    if size >= _POOL_SAVE_BYTES:
        from nmsmissions.gamedata import call_in_pool

        return call_in_pool(
            _load_save_slim,
            (str(file_path), dict(mapping), mapping_source, allow),
        )
    return _load_save_body(file_path, mapping, mapping_source, allow)


def _load_save_slim(path: str, mapping: dict[str, str], mapping_source: str, allow: bool) -> LoadedSave:
    """Parse in a worker process and return the mission index, not the whole tree."""
    full = _load_save_body(Path(path), mapping, mapping_source, allow)
    return LoadedSave(
        path=full.path,
        payload=SavePayload(b"", chunked=full.payload.chunked),
        sha256=full.sha256,
        mtime_ns=full.mtime_ns,
        data=OrderedDict(),
        mapping_source=full.mapping_source,
        active_context=full.active_context,
        contexts=full.contexts,
        flags=full.flags,
        total_play_time=full.total_play_time,
    )


def _load_save_body(
    file_path: Path,
    mapping: dict[str, str],
    mapping_source: str,
    allow: bool = False,
) -> LoadedSave:
    refuse_live_save(file_path, allow)
    blob = file_path.read_bytes()
    mtime_ns = file_path.stat().st_mtime_ns
    payload = unpack_save(blob)
    try:
        parsed = json.loads(payload.json_text, object_pairs_hook=OrderedDict)
    except json.JSONDecodeError as exc:
        raise SaveFormatError(f"Save JSON did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SaveFormatError("Save JSON is not an object.")
    data = deobfuscate(parsed, mapping)
    if not isinstance(data, OrderedDict):
        raise SaveFormatError("Save JSON is not an object.")
    active, contexts, flags = _index(data)
    return LoadedSave(
        path=file_path,
        payload=payload,
        sha256=hashlib.sha256(blob).hexdigest(),
        mtime_ns=mtime_ns,
        data=data,
        mapping_source=mapping_source,
        active_context=active,
        contexts=contexts,
        flags=flags,
        total_play_time=_total_play_time(data),
    )


def context_key(name: str) -> str:
    return CONTEXT_NAMES.get(name, name)


def require_context(save: LoadedSave, name: str) -> PlayerContext:
    key = context_key(name)
    found = save.contexts.get(key)
    if found is not None:
        return found
    available = ", ".join(sorted(save.contexts)) or "(none)"
    raise SaveFormatError(
        f"This save has no {key} player state with MissionProgress. Found: {available}."
    )


def _total_play_time(data: OrderedDict) -> int | None:
    """CommonStateData/TotalPlayTime. TimeAlive on the player state is not used."""
    for path, node in _walk(data):
        if not path or not isinstance(node, dict):
            continue
        leaf = str(path[-1])
        if leaf != "CommonStateData" and not leaf.startswith("CommonStateData#"):
            continue
        if "TotalPlayTime" not in node or isinstance(node.get("TotalPlayTime"), (dict, list)):
            continue
        number = _play_number(node.get("TotalPlayTime"))
        if number is not None:
            return number
    return None


def _play_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        return int(round(value))
    return int(value)


def _reality_index(state: dict) -> int | None:
    address = state.get("UniverseAddress")
    if isinstance(address, dict):
        return _whole_number(address.get("RealityIndex"))
    return None


def verify_save(path: Path | str, allow: bool = False, mapping: dict[str, str] | None = None) -> tuple[bool, str]:
    """Repack to a temp file and compare the JSON payload, including a trailing NUL.

    The source file's bytes and mtime are checked before and after. The temp
    file is removed. A mismatch returns ok=False and does not raise.
    """
    file_path = Path(path)
    refuse_live_save(file_path, allow)
    before = file_path.read_bytes()
    before_mtime = file_path.stat().st_mtime_ns
    digest = hashlib.sha256(before).hexdigest()
    payload = unpack_save(before)
    try:
        parsed = json.loads(payload.json_text, object_pairs_hook=OrderedDict)
    except json.JSONDecodeError as exc:
        return False, f"verify failed: payload is not JSON ({exc}). The save was not modified."
    if not isinstance(parsed, dict):
        return False, "verify failed: payload JSON is not an object. The save was not modified."
    dumped = json.dumps(parsed, ensure_ascii=False)
    try:
        encoded = dumped.encode("utf-8", errors="surrogateescape")
        restored = json.loads(
            encoded.decode("utf-8", errors="surrogateescape"),
            object_pairs_hook=OrderedDict,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        return False, f"verify failed: json loads/dumps round trip failed ({exc}). The save was not modified."
    if not same_structure(parsed, restored):
        return False, "verify failed: json loads/dumps changed the payload. The save was not modified."

    obfuscation = "deobfuscate/re-obfuscate skipped (no mapping)"
    if mapping is not None:
        plain = deobfuscate(parsed, mapping)
        if same_structure(plain, parsed):
            obfuscation = "deobfuscate/re-obfuscate round trip ok (keys were already plain)"
        else:
            reverse: dict[str, str] = {}
            for key, value in mapping.items():
                reverse.setdefault(value, key)
            back = reobfuscate(plain, reverse)
            if not same_structure(back, parsed):
                return (
                    False,
                    "verify failed: deobfuscate/re-obfuscate did not restore the original keys. "
                    "The save was not modified.",
                )
            obfuscation = "deobfuscate/re-obfuscate round trip ok"

    packed = pack_chunks(payload.raw) if payload.chunked else payload.raw
    handle, temp_name = tempfile.mkstemp(prefix="nmsmissions-", suffix=".hg")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_bytes(packed)
        again = unpack_save(temp_path.read_bytes())
        same = again.raw == payload.raw
    finally:
        temp_path.unlink(missing_ok=True)

    after = file_path.read_bytes()
    after_mtime = file_path.stat().st_mtime_ns
    after_digest = hashlib.sha256(after).hexdigest()
    if after != before or after_mtime != before_mtime or after_digest != digest:
        return (
            False,
            "verify failed: the source save changed while it was being read. "
            "Version 1 does not write that file on purpose.",
        )
    nul = "yes" if payload.raw.endswith(b"\x00") else "no"
    kind = "lz4 chunks" if payload.chunked else "plain JSON"
    if not same:
        return (
            False,
            "verify failed: the repacked payload does not match the original JSON bytes. "
            f"sha256 {digest}. The save was not modified.",
        )
    return (
        True,
        "\n".join(
            [
                "verify ok",
                f"source unchanged  sha256 {digest}",
                f"format: {kind}   trailing NUL: {nul}   payload bytes: {len(payload.raw)}",
                "repacked JSON bytes match, including a trailing NUL when one was present",
                "json loads/dumps round trip ok",
                obfuscation,
                "version 1 did not modify the save",
            ]
        ),
    )

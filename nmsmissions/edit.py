"""Finish, reset, unlock, and purple-star edits.

Patches touch only the JSON spans they name. The rest of the save, including
the way the game spells floats, stays byte for byte. A write also updates the
two size fields in mf_saveN.hg. Dry-run is the default.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from nmsmissions import __version__
from nmsmissions.timing import timed
from nmsmissions.chains import RETIRED_PROGRESS, completion_for, load_chains
from nmsmissions.chunks import SaveFormatError, pack_chunks, unpack_save
from nmsmissions.diffing import save_slot
from nmsmissions.gamedata import GameTables, final_progress, load_tables
from nmsmissions.guard import is_live_nms_save
from nmsmissions.manifest import (
    Manifest,
    ManifestError,
    archive_number,
    decrypt_manifest,
    encrypt_manifest,
    key_slot_for,
    manifest_path_for,
    read_manifest,
    size_bytes_differ_only_at_sizes,
    with_sizes,
)
from nmsmissions.rewards import (
    CURRENCY_CAP,
    ItemNeed,
    RewardPlan,
    caret_id,
    limits_from_save,
    rewards_for_finish,
    stack_size,
    stored_to_true,
    true_to_stored,
)
from nmsmissions.spanpatch import Patch, SpanError, append_patch, apply, dumps, locate, replace_patch, unchanged_outside
from nmsmissions.station import (
    CHOOSE_STANDING,
    MISSING_SYSTEM,
    chosen_stat_ids,
    label_for,
    meet_does,
    raise_changes,
    read_station,
    units_warning_lines,
)

PURPLE_MISSIONS = (
    "^ROBOMISS_0",
    "^ROBOMISS_1",
    "^ROBOMISS_1A",
    "^ROBOMISS_2",
    "^ROBOMISS_3_NADA",
    "^ROBOMISS_3_BUI",
    "^ROBOMISS_4",
    "^ROBOMISS_BOAT",
    "^PURPM1",
    "^PURPM2",
    "^PURPM3",
    "^PURPM_BOAT",
    "^POI_PURPM_BOAT",
)
DRIVE_TECH = "^HDRIVEBOOST4"
STEAM_WARNING = (
    "If Steam Cloud is on for No Man's Sky, it can put an old file back. "
    "Turn Cloud off before you play this save."
)


class EditError(Exception):
    """A finish, reset, or restore stopped before a bad write."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Change:
    label: str
    path: list
    op: str
    value: object


@dataclass
class Check:
    level: str
    message: str


@dataclass
class Plan:
    summary: str = ""
    changes: list[Change] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)
    choices: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    mission_version: int | None = None
    backup_path: str = ""

    def failing(self) -> bool:
        return any(check.level == "fail" for check in self.checks)


@dataclass
class EditRequest:
    action: str
    save: Path | None = None
    mission: str | None = None
    chain: str | None = None
    target: str | None = None
    whole_line: bool = False
    flag_only: bool = False
    apply: bool = False
    live: bool = False
    confirm_slot: int | None = None
    backup_dir: Path | None = None
    game_files: Path | None = None
    pcbanks: Path | None = None
    rewards: bool = True
    choices: dict[str, int] = field(default_factory=dict)
    amount: str = "min"
    patch_out: Path | None = None
    as_json: bool = False
    restore_zip: Path | None = None
    restore_to: Path | None = None
    restore_name: str | None = None
    install_folder: Path | None = None
    install_slot: int | None = None
    install_to: Path | None = None
    break_after_replace: bool = False
    quiet: bool = False
    running: bool | None = None
    station_race: str | None = None
    station_guild: str | None = None


@dataclass
class Snapshot:
    path: Path
    mf_path: Path
    save_bytes: bytes
    mf_bytes: bytes | None
    save_sha: str
    mf_sha: str | None
    save_mtime_ns: int
    mf_mtime_ns: int | None
    text: str
    suffix: bytes
    chunked: bool
    parsed: object
    manifest: Manifest | None
    player_path: list
    player: dict
    pending_appends: list = field(default_factory=list)
    raw_len: int = 0
    active: tuple | None = None
    active_known: bool = False
    play_time: object = None
    play_time_known: bool = False


def loads_save(text: str) -> object:
    """Parse save JSON. Lone bytes kept by surrogateescape still load."""
    safe = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return json.loads(safe)


def hidden_window_kwargs() -> dict:
    """Stop a console window flashing when the editor was started with pythonw.

    The running-game check calls tasklist every few seconds. On Windows that
    needs CREATE_NO_WINDOW, or a console appears and closes.
    """
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    kwargs: dict = {"creationflags": flags}
    startup_cls = getattr(subprocess, "STARTUPINFO", None)
    if startup_cls is not None:
        info = startup_cls()
        info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        info.wShowWindow = 0
        kwargs["startupinfo"] = info
    return kwargs


_nms_lock = threading.Lock()
_nms_seen: dict[str, object] = {"value": None, "at": 0.0}


def note_nms_running(value: bool) -> None:
    """Remember a running-game check so the window can paint without waiting."""
    with _nms_lock:
        _nms_seen["value"] = bool(value)
        _nms_seen["at"] = time.monotonic()


def peek_nms_running(max_age: float = 8.0) -> bool | None:
    """Last running-game check, or None when it is missing or stale."""
    with _nms_lock:
        value = _nms_seen["value"]
        seen_at = float(_nms_seen["at"])
    if value is None or time.monotonic() - seen_at > max_age:
        return None
    return bool(value)


def nms_is_running() -> bool:
    """True when NMS.exe is running, or when tests set NMSMISSIONS_NMS_RUNNING=1."""
    if os.environ.get("NMSMISSIONS_NMS_RUNNING") == "1":
        return True
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq NMS.exe", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True,
            **hidden_window_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return "NMS.exe" in out


def preview_running() -> bool:
    """Running-game flag for the status line and a preview. This never calls tasklist.

    Put, Undo, Write, and a restore into the game folder do not use this.
    They call nms_is_running at the moment of the write.
    """
    if os.environ.get("NMSMISSIONS_NMS_RUNNING") == "1":
        return True
    known = peek_nms_running()
    return bool(known)


def game_is_running(known: bool | None = None) -> bool:
    """The test flag, a check just made for this write, or a fresh tasklist.

    known is that fresh answer. It is not the status-line cache.
    """
    if os.environ.get("NMSMISSIONS_NMS_RUNNING") == "1":
        return True
    if known is not None:
        return bool(known)
    return nms_is_running()


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path.parent).free


def default_backup_dir(slot: int | None) -> Path:
    """Zip folder for a slot. NMSMISSIONS_CACHE_DIR redirects it, including in tests."""
    from nmsmissions import cache_root

    base = cache_root() / "backups"
    if slot is None:
        return base
    return base / f"slot{slot}"


def run(request: EditRequest) -> int:
    try:
        return _run(request)
    except EditError as exc:
        if not request.quiet:
            print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (SpanError, ManifestError, SaveFormatError) as exc:
        if not request.quiet:
            print(f"error: {exc}", file=sys.stderr)
        return 2


def _say(request: EditRequest, *args: object) -> None:
    if request.quiet:
        return
    print(*args)


def _run(request: EditRequest) -> int:
    if request.action == "gamedata-check":
        _say(request, _gamedata_report(request, refresh=False))
        return 0
    if request.action == "gamedata-refresh":
        _say(request, _gamedata_report(request, refresh=True))
        return 0
    if request.action == "restore":
        return _restore(request)
    if request.action == "install":
        return _install(request)
    if request.action == "backup":
        return _backup_only(request)
    if request.save is None:
        raise EditError("Name a save file.", code=2)
    snap = take_snapshot(request.save)
    tables = _tables(request)
    plan = build_plan(request, snap, tables)
    plan.checks = collect_checks(request, snap, tables)
    if request.action == "rewards":
        request.apply = False
        plan.warnings.insert(0, "Rewards are a preview. This command does not write the save.")
    _note_backup(request, snap, plan)
    _present(request, plan, snap)
    if request.patch_out is not None and plan.changes and not plan.failing():
        _write_patch_out(request.patch_out, snap.text, plan.changes)
    if plan.failing():
        return 2
    if request.action == "rewards":
        if plan.granted or plan.choices or plan.skipped or plan.warnings:
            return 0
        return 3
    if not plan.changes:
        return 3
    if not request.apply:
        return 0
    planned_backup = plan.backup_path
    code, backup = commit(request, snap, plan, tables)
    if backup is not None and code == 0 and str(backup) != planned_backup:
        _say(request, f"Backup: {backup}")
    if code == 4 and not request.quiet:
        print("The new file failed its check. The backup was written back.", file=sys.stderr)
    return code


def take_snapshot(path: Path) -> Snapshot:
    path = Path(path).resolve()
    if not path.is_file():
        raise EditError(f"No save at {path}.", code=2)
    with timed("snapshot-read"):
        data = path.read_bytes()
    try:
        with timed("snapshot-unpack"):
            payload = unpack_save(data)
    except SaveFormatError as exc:
        raise EditError(str(exc), code=2) from exc
    raw = payload.raw
    suffix = b""
    body = raw
    while body.endswith(b"\x00"):
        suffix = b"\x00" + suffix
        body = body[:-1]
    text = body.decode("utf-8", errors="surrogateescape")
    try:
        with timed("snapshot-json"):
            parsed = loads_save(text)
    except json.JSONDecodeError as exc:
        raise EditError(f"The save is not JSON ({exc}).", code=2) from exc
    if not isinstance(parsed, dict):
        raise EditError("The save JSON is not an object.", code=2)
    with timed("snapshot-player"):
        player_path, player = find_player(parsed)
        active = find_value(parsed, "XTp")
        play = find_value(parsed, "b@r")
    mf_path = manifest_path_for(path)
    manifest = None
    mf_bytes = None
    mf_sha = None
    mf_mtime = None
    if mf_path.is_file():
        try:
            manifest = read_manifest(mf_path)
        except ManifestError as exc:
            raise EditError(str(exc), code=2) from exc
        mf_bytes = mf_path.read_bytes()
        mf_sha = hashlib.sha256(mf_bytes).hexdigest()
        mf_mtime = mf_path.stat().st_mtime_ns
    return Snapshot(
        path=path,
        mf_path=mf_path,
        save_bytes=data,
        mf_bytes=mf_bytes,
        save_sha=hashlib.sha256(data).hexdigest(),
        mf_sha=mf_sha,
        save_mtime_ns=path.stat().st_mtime_ns,
        mf_mtime_ns=mf_mtime,
        text=text,
        suffix=suffix,
        chunked=payload.chunked,
        parsed=parsed,
        manifest=manifest,
        player_path=player_path,
        player=player,
        raw_len=len(raw),
        active=active,
        active_known=True,
        play_time=None if play is None else play[1],
        play_time_known=True,
    )


def _snapshot_job(path_str: str) -> Snapshot:
    """Parse a save in a pool worker. The window thread does not run this."""
    return take_snapshot(Path(path_str))


def find_player(parsed: dict) -> tuple[list, dict]:
    found: list[tuple[list, dict]] = []

    def walk(node: object, path: list) -> None:
        if isinstance(node, dict):
            missions = node.get("dwb")
            if isinstance(missions, list) and "vLc" in path and "2YS" not in path:
                found.append((list(path), node))
            for key, value in node.items():
                walk(value, path + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [index])

    walk(parsed, [])
    if not found:
        raise EditError("This save has no base-context mission list.", code=2)
    return found[0]


def find_value(parsed: object, key: str) -> tuple[list, object] | None:
    found: list[tuple[list, object]] = []

    def walk(node: object, path: list) -> None:
        if found:
            return
        if isinstance(node, dict):
            if key in node and not isinstance(node[key], (dict, list)):
                found.append((path + [key], node[key]))
                return
            for name, value in node.items():
                walk(value, path + [name])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [index])

    walk(parsed, [])
    return found[0] if found else None


def _active_value(snap: Snapshot) -> tuple | None:
    if snap.active_known:
        return snap.active
    return find_value(snap.parsed, "XTp")


def _play_time_value(snap: Snapshot) -> object:
    if snap.play_time_known:
        return snap.play_time
    found = find_value(snap.parsed, "b@r")
    return None if found is None else found[1]


def _detach_player(player: dict) -> dict:
    """Copy the containers a preview mutates. The parsed save stays shared."""
    cloned = dict(player)
    for key, value in list(cloned.items()):
        if not isinstance(value, list):
            continue
        if key == "dwb":
            cloned[key] = [dict(row) if isinstance(row, dict) else row for row in value]
        else:
            cloned[key] = list(value)
    return cloned


def fork_snapshot(snap: Snapshot) -> Snapshot:
    """A preview copy. Mutations stay here so the cached save can be reused."""
    return replace(
        snap,
        player=_detach_player(snap.player),
        player_path=list(snap.player_path),
        pending_appends=[],
    )


def build_plan(request: EditRequest, snap: Snapshot, tables: GameTables | None) -> Plan:
    plan = Plan()
    active = _active_value(snap)
    if active is None or active[1] != "Main":
        plan.summary = "This save is not the main game, so nothing was changed."
        return plan
    version = _version(snap.player)
    plan.mission_version = version
    if request.action == "finish":
        _finish(request, snap, tables, plan, version)
    elif request.action == "reset":
        _reset(request, snap, plan)
    elif request.action == "rewards":
        _preview_rewards(request, snap, tables, plan, version)
    elif request.action == "purple":
        _purple(request, snap, tables, plan, version)
    elif request.action == "unlock":
        _unlock(request, snap, tables, plan, version)
    elif request.action == "station":
        _station(request, snap, plan)
    else:
        raise EditError(f"Unknown edit {request.action}.", code=2)
    return plan


def collect_checks(
    request: EditRequest,
    snap: Snapshot,
    tables: GameTables | None,
    running: bool | None = None,
    quick: bool = False,
) -> list[Check]:
    checks: list[Check] = []
    running = game_is_running(running)
    if running:
        checks.append(Check("fail", "No Man's Sky is running. Close it before writing a save."))
    else:
        checks.append(Check("ok", "No Man's Sky is not running."))
    slot = save_slot(snap.path)
    if is_live_nms_save(snap.path):
        if request.live and request.confirm_slot == slot:
            checks.append(Check("ok", f"Live slot {slot} confirmed."))
        else:
            checks.append(
                Check(
                    "fail",
                    f"This is a live HelloGames save. Pass --apply --live --confirm-slot {slot}.",
                )
            )
    else:
        checks.append(Check("ok", "This path is a copy."))
    if _snapshot_matches(snap, quick=quick):
        checks.append(Check("ok", "The save matches the snapshot just taken."))
    else:
        checks.append(Check("fail", "The game saved again. Reload first."))
    partner = _partner_problem(snap, fresh=not quick)
    if partner:
        checks.append(Check("fail", partner))
    else:
        checks.append(Check("ok", "The paired save is not newer."))
    needs_tables = request.action == "finish" or (request.action == "purple" and not request.flag_only)
    if needs_tables and tables is None:
        checks.append(Check("warn", REWARDS_LATER))
    if tables is not None and tables.stale:
        checks.append(Check("warn", tables.warning))
    version = _version(snap.player)
    if (
        tables is not None
        and not tables.files_match
        and version is not None
        and tables.highest_version()
        and version > tables.highest_version()
    ):
        checks.append(
            Check(
                "warn",
                "Game files are older than this save's mission version. "
                "Finals use the newest row. Rewards are still allowed.",
            )
        )
    pair = _pair_problem(snap)
    if pair:
        level, message = pair
        checks.append(Check(level, message))
    else:
        checks.append(Check("ok", "The save unpacks and the manifest is format 2004."))
    if request.action == "station":
        if not read_station(snap.player).found:
            checks.append(Check("fail", MISSING_SYSTEM))
        elif chosen_stat_ids(request.station_race, request.station_guild) is None:
            checks.append(Check("fail", CHOOSE_STANDING))
        else:
            checks.append(Check("ok", "This system is in the save."))
    active = _active_value(snap)
    if active is not None and active[1] == "Main":
        checks.append(Check("ok", "The active context is Main, so the edit stays on the main save."))
    else:
        shown = "missing" if active is None else str(active[1])
        checks.append(Check("fail", f"The active context is {shown}. Expedition saves are left alone."))
    need = 3 * max(1, len(snap.save_bytes))
    if _free_bytes(snap.path) >= need:
        checks.append(Check("ok", "There is enough free disk for a backup and the new files."))
    else:
        checks.append(Check("fail", "Not enough free disk. Need at least three times the save size."))
    checks.append(Check("warn", STEAM_WARNING))
    return checks


def format_plan(plan: Plan, dry_run: bool, apply_hint: str | None = None) -> str:
    lines = [plan.summary or "No edit.", ""]
    if plan.changes:
        lines.append("Changes:")
        for change in plan.changes:
            lines.append(f"- {change.label}")
    elif not plan.granted:
        lines.append("No changes.")
    if plan.granted:
        lines.extend(["", "Granted:"])
        for item in plan.granted:
            lines.append(f"- {item}")
    if plan.choices:
        lines.extend(["", "Choices, not granted:"])
        for item in plan.choices:
            lines.append(f"- {item}")
    if plan.skipped:
        lines.extend(["", "Skipped:"])
        for item in plan.skipped:
            lines.append(f"- {item}")
    if plan.warnings:
        lines.extend(["", "Warnings:"])
        for item in plan.warnings:
            lines.append(f"- {item}")
    lines.extend(["", "Checks:"])
    marks = {"ok": "OK", "warn": "WARN", "fail": "NO"}
    for check in plan.checks:
        lines.append(f"{marks.get(check.level, check.level)}  {check.message}")
    lines.append("")
    lines.append("Details:")
    if not plan.changes:
        lines.append("(none)")
    for change in plan.changes:
        lines.append(f"- {_detail_path(change.path)}")
    lines.append("")
    if plan.backup_path:
        lines.append(f"Backup: {plan.backup_path}")
    if dry_run:
        lines.append(apply_hint or "Dry run. Nothing is written. Pass --apply to write.")
    else:
        lines.append("Writing the save and its manifest.")
    return "\n".join(lines) + "\n"


_DETAIL_NAMES = {
    "vLc": "BaseContext",
    "6f=": "PlayerStateData",
    "dwb": "MissionProgress",
    "p0c": "Mission",
    "tW6": "Progress",
    "8?J": "Data",
    "Kex": "Stat",
    "@EL": "Seed",
    "eZ7": "Participants",
    ";R7": "CurrentMission",
    "Mg<": "PreviousMission",
    "yq:": "MissionVersion",
    "Kg6": "HasDiscoveredPurpleSystems",
    "cPt": "BuildersKnown",
    "4kj": "KnownTech",
    "eZ<": "KnownProducts",
    "24<": "KnownSpecials",
    "wGS": "Units",
    "gUR": "Stats",
    ":rc": "GroupId",
    "2Ak": "Address",
    ">MX": "Value",
    ">vs": "IntValue",
    "yhJ": "UniverseAddress",
    "oZw": "GalacticAddress",
    "7QL": "Nanites",
    "kN;": "Quicksilver",
    ";l5": "Inventory",
    ":No": "Slots",
    "hl?": "ValidSlots",
    "1o9": "Amount",
    "F9q": "MaxAmount",
    "b2n": "Id",
    "aBE": "PrimaryShip",
    "@Cs": "Ships",
    "8ZP": "Freighter",
    "PMT": "TechInventory",
    "3ZH": "Index",
    ">Qh": "X",
    "XJ>": "Y",
    "Vn8": "Type",
    "elv": "InventoryType",
}


def _detail_path(path: list) -> str:
    """Field names in the Details list. Unknown keys stay as stored."""
    parts: list[str] = []
    for part in path:
        if isinstance(part, int):
            parts.append(str(part))
            continue
        parts.append(_DETAIL_NAMES.get(str(part), str(part)))
    return "/".join(parts)


def commit(
    request: EditRequest,
    snap: Snapshot,
    plan: Plan,
    tables: GameTables | None,
    running: bool | None = None,
) -> tuple[int, Path | None]:
    """Write the plan. Returns an exit code and the backup zip, if one was made."""
    if plan.failing():
        return 2, None
    if not plan.changes:
        return 3, None
    if not _snapshot_matches(snap):
        raise EditError("The game saved again. Reload first.", code=2)
    if game_is_running(running):
        raise EditError("No Man's Sky is running. Close it before writing a save.", code=2)
    patched, patches = _patched_text(snap.text, plan.changes)
    _self_check(snap, patched, patches, plan.changes)
    raw = patched.encode("utf-8", errors="surrogateescape") + snap.suffix
    if unpack_save(pack_chunks(raw)).raw != raw:
        raise EditError("Repack changed the payload, so nothing was written.", code=2)
    if snap.manifest is None:
        raise EditError("There is no mf_ file beside this save, so nothing was written.", code=2)
    packed = pack_chunks(raw) if snap.chunked else raw
    encrypted = with_sizes(snap.manifest, len(raw), len(packed))
    plain, _slot = decrypt_manifest(encrypted, snap.manifest.archive)
    if not size_bytes_differ_only_at_sizes(snap.manifest.plain, plain):
        raise EditError("The manifest edit touched more than the size fields.", code=2)
    directory = request.backup_dir or default_backup_dir(save_slot(snap.path))
    backup = write_backup(directory, snap, plan, tables)
    if not _snapshot_matches(snap) or game_is_running(running):
        raise EditError("The game saved again. Reload first.", code=2)
    try:
        _atomic_replace(snap.path, packed)
    except OSError as exc:
        raise EditError(f"The save could not be replaced ({exc}).", code=2) from exc
    try:
        _atomic_replace(snap.mf_path, encrypted)
    except OSError as exc:
        try:
            _restore_zip(backup, snap.path.parent)
        except (OSError, EditError) as restore_exc:
            raise EditError(
                "The manifest could not be replaced, and the backup could not be written back.",
                code=4,
            ) from restore_exc
        raise EditError(
            "The manifest could not be replaced. Both files were restored.",
            code=4,
        ) from exc
    if request.break_after_replace or not _written_ok(snap, packed, encrypted, raw):
        _restore_zip(backup, snap.path.parent)
        return 4, backup
    prune_backups(directory)
    return 0, backup


def write_backup(directory: Path, snap: Snapshot, plan: Plan, tables: GameTables | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    planned = Path(plan.backup_path) if plan.backup_path else None
    if planned is not None and not planned.exists():
        planned.parent.mkdir(parents=True, exist_ok=True)
        dest = planned
    else:
        dest = _unique_zip(directory, snap.path.name)
    plan.backup_path = str(dest)
    info = {
        "archive": snap.manifest.archive if snap.manifest else None,
        "save": snap.path.name,
        "manifest": snap.mf_path.name if snap.mf_bytes is not None else None,
        "save_mtime_ns": snap.save_mtime_ns,
        "mf_mtime_ns": snap.mf_mtime_ns,
        "save_sha256": snap.save_sha,
        "mf_sha256": snap.mf_sha,
        "tool": __version__,
        "git": _git_head(),
        "plan": [change.label for change in plan.changes],
        "mission_version": plan.mission_version,
        "game_data_stamp": tables.warning if tables is not None else "",
        "grants": list(plan.granted),
    }
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(snap.path.name, snap.save_bytes)
        if snap.mf_bytes is not None:
            handle.writestr(snap.mf_path.name, snap.mf_bytes)
        handle.writestr("saveinfo.json", json.dumps(info, indent=2))
    if not _zip_matches(dest, snap):
        dest.unlink(missing_ok=True)
        raise EditError("The backup could not be verified, so nothing was written.", code=2)
    return dest


def prune_backups(directory: Path, keep: int = 30) -> list[Path]:
    """Keep the newest backups, and never delete the first backup of a calendar day."""
    zips = sorted(path for path in directory.glob("*.zip") if path.is_file())
    first_of_day: dict[str, Path] = {}
    for path in zips:
        day = _zip_day(path.name)
        if day and day not in first_of_day:
            first_of_day[day] = path
    protected = set(first_of_day.values())
    extras = [path for path in zips if path not in protected]
    room = max(0, keep - len(protected))
    keep_extra = set(extras[-room:]) if room else set()
    removed: list[Path] = []
    for path in extras:
        if path in keep_extra:
            continue
        path.unlink()
        removed.append(path)
    return removed


def _station(request: EditRequest, snap: Snapshot, plan: Plan) -> None:
    """Raise the chosen race, the chosen guild, and salvage contracts. Never lowers."""
    view = read_station(snap.player)
    plan.warnings.extend(units_warning_lines(view.units))
    if not view.found:
        plan.summary = MISSING_SYSTEM
        return
    chosen = chosen_stat_ids(request.station_race, request.station_guild)
    if chosen is None:
        plan.summary = CHOOSE_STANDING
        return
    plan.summary = meet_does(request.station_race, request.station_guild)
    present = {row.stat_id for row in view.rows}
    for stat_id in chosen:
        if stat_id not in present:
            plan.warnings.append(f"{label_for(stat_id)} is not in this system, so it was not changed.")
    for label, path, value in raise_changes(view, snap.player_path, chosen):
        plan.changes.append(Change(label, path, "set", value))


def _finish(
    request: EditRequest,
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    version: int | None,
) -> None:
    missions = _mission_ids(request, finish=True)
    plan.summary = "Finish " + ", ".join(missions) + "."
    if request.whole_line and _required_steps_done(snap, tables, missions, version):
        _apply_missing_unlocks(snap, tables, plan, missions)
        return
    if tables is None:
        plan.warnings.append(REWARDS_LATER)
    elif not request.rewards:
        plan.warnings.append("Rewards are off. Progress still updates.")
    finished: list[str] = []
    seen: set[str] = set()
    reward_items: list[ItemNeed] = []
    reward_plan = RewardPlan()
    for mission_id in missions:
        done, outcome = _finish_one(snap, tables, plan, mission_id, version, request, seen)
        if done:
            finished.append(mission_id)
        if outcome is None:
            continue
        reward_items.extend(outcome.items)
        reward_plan.others.extend(outcome.others)
        reward_plan.granted.extend(outcome.granted)
        reward_plan.choices.extend(outcome.choices)
        reward_plan.skipped.extend(outcome.skipped)
    if tables is None:
        _flush_pending(snap, plan)
    else:
        _emit_reward_edits(snap, tables, plan, reward_items, reward_plan)
    _retarget_current(snap, tables, plan, finished, version)
    # The same unlocks Unlock only applies. A step already at its final still
    # gets a missing effect, so a finish that only moved progress can be repaired.
    for mission_id in finished:
        pending = _pending_effects(snap.player, _unlock_effects(mission_id, tables))
        if pending:
            _write_unlock_effects(snap, plan, pending)
    if not plan.changes:
        if any(READ_GAME_FILES_TIP in item for item in plan.warnings):
            plan.summary = READ_GAME_FILES_TIP
        else:
            plan.summary = "Those missions are already finished."


def _required_steps_done(
    snap: Snapshot,
    tables: GameTables | None,
    mission_ids: list[str],
    version: int | None,
) -> bool:
    """True when every required step is already at its final. Optional steps do not count."""
    if not mission_ids:
        return False
    from nmsmissions.chains import is_lore_step

    chain = _chain_of(mission_ids[0])
    optional = set()
    if chain is not None:
        for step in chain.steps:
            if step.optional or is_lore_step(step.mission_id):
                optional.add(step.mission_id)
    saw_required = False
    for mission_id in mission_ids:
        if mission_id in optional:
            continue
        saw_required = True
        final = _known_final(tables, mission_id, version)
        if final is None:
            return False
        found = _row(snap.player, mission_id)
        progress = None if found is None else found[1].get("tW6")
        if not _progress_done(progress, final):
            return False
    return saw_required


def _apply_missing_unlocks(
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    mission_ids: list[str],
) -> None:
    """Write unlocks that are still off. Does not change step progress."""
    plan.summary = "Apply the missing unlocks. The steps stay as they are."
    for mission_id in mission_ids:
        pending = _pending_effects(snap.player, _unlock_effects(mission_id, tables))
        if pending:
            _write_unlock_effects(snap, plan, pending)
    if not plan.changes:
        plan.summary = "Those unlocks are already on."


def chain_unlocks_pending(player: dict, tables: GameTables | None, chain_id: str) -> bool:
    """True when a step of this line still has an unlock effect that is off."""
    from nmsmissions.chains import load_chains

    chain = next((item for item in load_chains() if item.chain_id == chain_id), None)
    if chain is None:
        return False
    for step in chain.steps:
        if _pending_effects(player, _unlock_effects(step.mission_id, tables)):
            return True
    return False


def _reset(request: EditRequest, snap: Snapshot, plan: Plan) -> None:
    missions = _mission_ids(request, finish=False)
    chain = _chain_of(missions[0]) if missions else None
    if chain is not None:
        for other in load_chains():
            if chain.chain_id in other.requires:
                plan.warnings.append(
                    f"{other.title} still requires {chain.title}. Reset does not change that chain."
                )
    changed: list[str] = []
    for mission_id in missions:
        before = len(plan.changes)
        _reset_one(snap, plan, mission_id)
        if len(plan.changes) > before:
            changed.append(mission_id)
    if not changed:
        plan.summary = "Those missions are already not started."
        return
    noun = "step" if len(changed) == 1 else "steps"
    plan.summary = (
        f"Reset {len(changed)} {noun}: "
        + ", ".join(changed)
        + ". Rewards already granted stay in the inventories."
    )


def _preview_rewards(
    request: EditRequest,
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    version: int | None,
) -> None:
    if not request.mission:
        raise EditError("Name the mission whose rewards you want to preview.", code=2)
    mission_id = _norm_mission(request.mission)
    plan.summary = f"Reward preview for {mission_id}."
    if tables is None:
        plan.warnings.append(READ_GAME_FILES_TIP)
        return
    info = tables.missions.get(mission_id)
    final, note = final_progress(info, version if version is not None else 0)
    if note:
        plan.warnings.append(note)
    if final is None:
        plan.skipped.append(f"{mission_id}: no final progress, so rewards were not listed.")
        return
    row = _row(snap.player, mission_id)
    current = -1 if row is None else int(row[1].get("tW6") or -1)
    if info is None:
        return
    preview = rewards_for_finish(
        info,
        current,
        final,
        tables,
        amount_mode=request.amount,
        choices=request.choices,
        save_version=version or 0,
    )
    plan.granted.extend(preview.granted)
    plan.choices.extend(preview.choices)
    plan.skipped.extend(preview.skipped)
    _hide_already_known(plan, snap.player, preview.others)
    for item in preview.items:
        where = "exosuit, then the current ship, then the freighter"
        plan.granted.append(f"{item.item_id} would go to the {where}.")


_ISM_IDS: set[str] | None = None
_PURPLE_GRANTED = "R_PURPLESYSTEMS: purple systems discovered."
_DRIVE_WARNING = (
    f"{DRIVE_TECH} is not installed in the exosuit or a ship's technology inventory. This tool does not install it."
)


def _story_purple_ids() -> set[str]:
    """Steps of In Stellar Multitudes. That story reveals purple stars."""
    global _ISM_IDS
    if _ISM_IDS is None:
        found: set[str] = set()
        for chain in load_chains():
            if chain.chain_id == "ism":
                found = {step.mission_id for step in chain.steps}
                break
        _ISM_IDS = found
    return _ISM_IDS


def _mark_purple_unlocked(snap: Snapshot, plan: Plan) -> bool:
    """Set the discovered flag and known drive tech. True when the tech list grew."""
    _set_flag(snap, plan, "Kg6", True, "Set purple systems discovered.")
    if _PURPLE_GRANTED not in plan.granted:
        plan.granted.append(_PURPLE_GRANTED)
    before = len(plan.changes)
    _append_known(snap, plan, "4kj", DRIVE_TECH, f"Add {DRIVE_TECH} to known technology.", announce=False)
    if not _tech_installed(snap.player, DRIVE_TECH) and _DRIVE_WARNING not in plan.warnings:
        plan.warnings.append(_DRIVE_WARNING)
    return len(plan.changes) > before


def _linked_rewards(info, tables: GameTables | None):
    found = []
    seen: set[str] = set()
    for reward in info.rewards.values():
        if reward.reward_id in seen:
            continue
        seen.add(reward.reward_id)
        found.append(reward)
    if tables is None:
        return found
    for stage in info.stages:
        if stage.reward_id in seen:
            continue
        reward = info.rewards.get(stage.reward_id) or tables.reward_table.get(stage.reward_id)
        if reward is None:
            continue
        seen.add(stage.reward_id)
        found.append(reward)
    return found


def _unlock_effects(mission_id: str, tables: GameTables | None) -> list[tuple[str, str, str]]:
    """Unlocks that are not 'finish the mission'. Each row is (kind, key, label)."""
    effects: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(kind: str, key: str, label: str) -> None:
        token = kind if kind == "purple" else f"{kind}:{key}"
        if token in seen:
            return
        seen.add(token)
        effects.append((kind, key, label))

    if mission_id in _story_purple_ids():
        add("purple", "Kg6", "Set purple systems discovered.")
    info = None if tables is None else tables.missions.get(mission_id)
    if info is None:
        return effects
    for key in info.requires_flags:
        add("flag", key, _flag_label(key))
    for reward in _linked_rewards(info, tables):
        for part in reward.body.get("parts") or []:
            kind = part.get("kind") or ""
            if kind == "GcRewardPurpleSystems":
                add("purple", "Kg6", "Set purple systems discovered.")
            elif kind == "GcRewardBuildersKnown":
                add("flag", "cPt", "Set builders known.")
    return effects


def _flag_label(key: str) -> str:
    if key == "Kg6":
        return "Set purple systems discovered."
    if key == "cPt":
        return "Set builders known."
    return f"Set {key}."


def _purple_pending(player: dict) -> bool:
    if player.get("Kg6") is not True:
        return True
    known = player.get("4kj")
    if not isinstance(known, list) or not _known_already(known, DRIVE_TECH):
        return True
    return False


def _pending_effects(player: dict, effects: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    pending: list[tuple[str, str, str]] = []
    for kind, key, label in effects:
        if kind == "purple":
            if _purple_pending(player):
                pending.append((kind, key, label))
        elif player.get(key) is not True:
            pending.append((kind, key, label))
    return pending


READ_GAME_FILES_TIP = "Click Read my game files first."
REWARDS_LATER = "Items and money are skipped until you click Read my game files."

_CATALOG: dict[str, int] | None = None


def completion_catalog() -> dict[str, int]:
    """Built-in completion numbers. Used when the player's tables are not loaded."""
    global _CATALOG
    if _CATALOG is None:
        from nmsmissions.catalog import load_completion_catalog

        _CATALOG = load_completion_catalog()
    return _CATALOG


def catalog_final(mission_id: str) -> int | None:
    return completion_for(completion_catalog(), mission_id)


def missions_need_game_files(mission_ids: list[str]) -> bool:
    """True when a finish target has no built-in completion number."""
    return any(mission_id and catalog_final(mission_id) is None for mission_id in mission_ids)


def chain_ids_through(chain_id: str | None, mission_id: str) -> list[str]:
    """This step and the steps before it on the same quest line."""
    if not chain_id:
        return [mission_id]
    chain = next((item for item in load_chains() if item.chain_id == chain_id), None)
    if chain is None:
        return [mission_id]
    ids = [step.mission_id for step in chain.steps]
    if mission_id not in ids:
        return [mission_id]
    return ids[: ids.index(mission_id) + 1]


def _known_final(tables: GameTables | None, mission_id: str, version: int | None) -> int | None:
    """Table final when game files are loaded. Otherwise the built-in completion list."""
    if tables is not None:
        info = tables.missions.get(mission_id)
        if info is None:
            return None
        final, _note = final_progress(info, version if version is not None else 0)
        return final
    return catalog_final(mission_id)


def _progress_done(progress: object, final: int | None) -> bool:
    if progress == RETIRED_PROGRESS:
        return True
    if isinstance(progress, bool) or not isinstance(progress, int):
        return False
    if final is None:
        return False
    if final > 0:
        return progress >= final
    return progress >= 0 and progress >= final


def unlock_block_reason(
    player: dict,
    tables: GameTables | None,
    mission_id: str | None,
    version: int | None = None,
) -> str:
    """Why Unlock only cannot change this mission. Empty when the button should work."""
    if not mission_id or not str(mission_id).strip():
        return "Select a mission first."
    try:
        mission_id = _norm_mission(mission_id)
        effects = _unlock_effects(mission_id, tables)
        if _pending_effects(player, effects):
            return ""
        if effects:
            found = _row(player, mission_id)
            progress = None if found is None else found[1].get("tW6")
            final = _known_final(tables, mission_id, version)
            if _progress_done(progress, final):
                return "This unlock is already on. The mission is already done."
            return "This unlock is already on. The mission is not finished, so you can still play it."
        return _start_block_reason(player, tables, mission_id, version)
    except EditError as exc:
        return str(exc)


def _start_block_reason(
    player: dict,
    tables: GameTables | None,
    mission_id: str,
    version: int | None,
) -> str:
    final = _known_final(tables, mission_id, version)
    found = _row(player, mission_id)
    if found is None:
        if final is not None and final <= 0:
            return "This mission's table cannot tell a start from a finish."
        if _template_row(player) is None:
            return "This mission is not in the save, and there is no empty mission row to copy."
        return ""
    index_row = found[1]
    if _seed_blocks(index_row.get("@EL", 0)):
        return "This mission has a seed other than 0. It is left alone."
    progress = index_row.get("tW6")
    if progress == RETIRED_PROGRESS:
        return "This mission is marked done (retired). It is left alone."
    if _progress_done(progress, final):
        return "This mission is already done."
    if not isinstance(progress, int) or progress < 0:
        if final is not None and final <= 0:
            return "This mission's table cannot tell a start from a finish."
        return ""
    if player.get(";R7") == mission_id:
        return "This mission is already started. There is no separate unlock."
    return ""


def _unlock(
    request: EditRequest,
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    version: int | None,
) -> None:
    if not request.mission:
        raise EditError("Select a mission first.", code=2)
    mission_id = _norm_mission(request.mission)
    reason = unlock_block_reason(snap.player, tables, mission_id, version)
    if reason:
        plan.summary = reason
        plan.warnings.append(reason)
        return
    pending = _pending_effects(snap.player, _unlock_effects(mission_id, tables))
    if pending:
        _apply_unlock_effects(snap, plan, pending)
        return
    _start_or_activate(snap, plan, mission_id)


def _write_unlock_effects(snap: Snapshot, plan: Plan, pending: list[tuple[str, str, str]]) -> bool:
    """Flag and known-tech edits Unlock only writes. Does not change the summary.

    Returns whether the purple path added the drive to known technology.
    HasGalacticMapRequestFirstPurple and HasGalacticMapRequestAllPurples are
    not written. A finished quest leaves those map-animation requests false.
    """
    purple = any(kind == "purple" for kind, _key, _label in pending)
    added_drive = False
    if purple:
        added_drive = _mark_purple_unlocked(snap, plan)
    for kind, key, label in pending:
        if kind == "flag" and not (purple and key == "Kg6"):
            _set_flag(snap, plan, key, True, label)
    return added_drive


def _apply_unlock_effects(snap: Snapshot, plan: Plan, pending: list[tuple[str, str, str]]) -> None:
    purple = any(kind == "purple" for kind, _key, _label in pending)
    added_drive = _write_unlock_effects(snap, plan, pending)
    if purple and added_drive:
        plan.summary = (
            "Unlock this mission. Purple stars are discovered, and the drive technology is known. "
            "It stays unfinished, so you can still play it."
        )
    elif purple:
        plan.summary = (
            "Unlock this mission. Purple stars are discovered. It stays unfinished, so you can still play it."
        )
    else:
        plan.summary = "Unlock this mission. It stays unfinished, so you can still play it."


def _start_or_activate(snap: Snapshot, plan: Plan, mission_id: str) -> None:
    found = _row(snap.player, mission_id)
    progress = None if found is None else found[1].get("tW6")
    starting = found is None or not isinstance(progress, int) or progress < 0
    if found is None:
        _require_row(snap, mission_id, create=True, progress=0, data=0, stat=None)
        _flush_pending(snap, plan)
    elif starting:
        index, row = found
        _set_row_field(snap, plan, index, "tW6", 0, f"Start {mission_id}. It is not finished.")
        row["tW6"] = 0
    if snap.player.get(";R7") != mission_id:
        plan.changes.append(
            Change(
                f"Make {mission_id} the tracked mission.",
                snap.player_path + [";R7"],
                "set",
                mission_id,
            )
        )
        snap.player[";R7"] = mission_id
    if starting:
        plan.summary = "Start this mission. It is not finished, so you can still play it."
    else:
        plan.summary = "Make this the tracked mission. It stays unfinished, so you can still play it."


def _purple(
    request: EditRequest,
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    version: int | None,
) -> None:
    if not request.flag_only:
        plan.summary = (
            "Unlock purple stars, then finish the They Who Returned and "
            "In Stellar Multitudes steps that reveal them."
        )
    added = _mark_purple_unlocked(snap, plan)
    if request.flag_only:
        if added:
            plan.summary = "Unlock purple stars. Only the discovered flag and the drive technology."
        else:
            plan.summary = "Unlock purple stars. Only the discovered flag."
        return
    if tables is None:
        plan.warnings.append(REWARDS_LATER)
    fake = EditRequest(
        action="finish",
        rewards=bool(request.rewards and tables is not None and not tables.stale),
        choices=request.choices,
        amount=request.amount,
    )
    finished: list[str] = []
    seen: set[str] = set()
    reward_items: list[ItemNeed] = []
    reward_plan = RewardPlan()
    for mission_id in PURPLE_MISSIONS:
        done, outcome = _finish_one(snap, tables, plan, mission_id, version, fake, seen)
        if done:
            finished.append(mission_id)
        if outcome is None:
            continue
        reward_items.extend(outcome.items)
        reward_plan.others.extend(outcome.others)
        reward_plan.granted.extend(outcome.granted)
        reward_plan.choices.extend(outcome.choices)
        reward_plan.skipped.extend(outcome.skipped)
    if tables is None or tables.stale or not request.rewards:
        _flush_pending(snap, plan)
        if tables is not None and tables.stale:
            plan.warnings.append("Rewards are skipped because the extracted tables are older than the paks.")
    else:
        _emit_reward_edits(snap, tables, plan, reward_items, reward_plan)
    _retarget_current(snap, tables, plan, finished, version)


def _finish_one(
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    mission_id: str,
    version: int | None,
    request: EditRequest,
    seen: set[str],
) -> tuple[bool, RewardPlan | None]:
    info = None if tables is None else tables.missions.get(mission_id)
    if tables is None:
        final = _known_final(tables, mission_id, version)
        note = None
    else:
        final, note = final_progress(info, version if version is not None else 0)
    if note and "older than this save" not in note:
        plan.warnings.append(f"{mission_id}: {note}")
    if final is None:
        if tables is None:
            plan.warnings.append(f"{mission_id}: {READ_GAME_FILES_TIP}")
            return False, None
        raise EditError(f"{mission_id}: {note or 'no final progress.'}", code=2)
    if info is not None:
        for required in info.requires_missions:
            required_row = _row(snap.player, required)
            progress = None if required_row is None else required_row[1].get("tW6")
            if not isinstance(progress, int) or progress < 0:
                plan.warnings.append(
                    f"{mission_id} lists {required} as a starting condition. It is not done. Finish is not blocked."
                )
    found = _require_row(snap, mission_id, create=True, progress=final, data=0, stat=None)
    index, row, created = found
    current = -1 if created else int(row.get("tW6"))
    if not created and isinstance(row.get("tW6"), int) and int(row["tW6"]) >= final:
        return True, None
    if not created:
        _set_row_field(snap, plan, index, "tW6", final, f"Set {mission_id} progress to {final}.")
        if row.get("8?J") != 0:
            _set_row_field(snap, plan, index, "8?J", 0, f"Set {mission_id} mission data to 0.")
        row["tW6"] = final
        row["8?J"] = 0
    if not request.rewards or tables is None or tables.stale or info is None:
        return True, RewardPlan()
    return True, rewards_for_finish(
        info,
        current,
        final,
        tables,
        amount_mode=request.amount,
        choices=request.choices,
        seen=seen,
        save_version=version or 0,
    )


def _reset_one(snap: Snapshot, plan: Plan, mission_id: str) -> None:
    found = _row(snap.player, mission_id)
    if found is None:
        plan.skipped.append(f"{mission_id}: not in the save.")
        return
    _refuse_locked_row(mission_id, found[1])
    index, row = found
    if row.get("tW6") != -1:
        _set_row_field(snap, plan, index, "tW6", -1, f"Set {mission_id} progress to not started.")
    if row.get("8?J") != 0:
        _set_row_field(snap, plan, index, "8?J", 0, f"Set {mission_id} mission data to 0.")
    if row.get("Kex") != 0:
        _set_row_field(snap, plan, index, "Kex", 0, f"Set {mission_id} stat to 0.")


def _mission_ids(request: EditRequest, finish: bool) -> list[str]:
    chains = {chain.chain_id: chain for chain in load_chains()}
    if request.chain:
        chain = chains.get(request.chain)
        if chain is None:
            known = ", ".join(sorted(chains))
            raise EditError(f"Unknown chain {request.chain}. Known chains: {known}.", code=2)
        ids = [step.mission_id for step in chain.steps]
        if request.whole_line:
            return ids
        if not request.target:
            which = "finish up to" if finish else "reset from"
            raise EditError(f"Name the step to {which}.", code=2)
        target = _norm_mission(request.target)
        if target not in ids:
            raise EditError(f"{target} is not a step of {chain.title}.", code=2)
        index = ids.index(target)
        return ids[: index + 1] if finish else ids[index:]
    if not request.mission:
        raise EditError("Name a mission id.", code=2)
    mission_id = _norm_mission(request.mission)
    if not finish:
        chain = _chain_of(mission_id)
        if chain is not None:
            ids = [step.mission_id for step in chain.steps]
            if mission_id in ids and ids.index(mission_id) < len(ids) - 1:
                raise EditError(
                    "This step is in the middle of a quest line. Use Reset from this step, so the later steps reset too.",
                    code=2,
                )
    return [mission_id]


def _chain_of(mission_id: str):
    for chain in load_chains():
        if any(step.mission_id == mission_id for step in chain.steps):
            return chain
    return None


def _norm_mission(text: str) -> str:
    mission_id = text.strip()
    if not mission_id.startswith("^"):
        mission_id = "^" + mission_id
    return mission_id


def _version(player: dict) -> int | None:
    raw = player.get("yq:")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _rows(player: dict) -> list[tuple[int, dict]]:
    rows = player.get("dwb")
    if not isinstance(rows, list):
        return []
    return [(index, row) for index, row in enumerate(rows) if isinstance(row, dict)]


def _row(player: dict, mission_id: str) -> tuple[int, dict] | None:
    matches = [(index, row) for index, row in _rows(player) if row.get("p0c") == mission_id]
    if len(matches) > 1:
        raise EditError(f"{mission_id} appears more than once. Duplicate missions are left alone.", code=2)
    if not matches:
        return None
    return matches[0]


def _refuse_locked_row(mission_id: str, row: dict) -> None:
    if _seed_blocks(row.get("@EL", 0)):
        raise EditError(f"{mission_id} has a seed other than 0. It is left alone.", code=2)
    if row.get("tW6") == RETIRED_PROGRESS:
        raise EditError(f"{mission_id} is marked done (retired). It is left alone.", code=2)


def _seed_blocks(seed: object) -> bool:
    if seed is None or isinstance(seed, bool):
        return seed is not None
    if isinstance(seed, (int, float)):
        return int(seed) != 0
    return True


def _require_row(
    snap: Snapshot,
    mission_id: str,
    create: bool,
    progress: int,
    data: int,
    stat: int | None,
) -> tuple[int, dict, bool]:
    found = _row(snap.player, mission_id)
    if found is not None:
        _refuse_locked_row(mission_id, found[1])
        return found[0], found[1], False
    if not create:
        raise EditError(f"{mission_id} is not in the save.", code=2)
    template = _template_row(snap.player)
    if template is None:
        raise EditError("No never-started mission row to copy.", code=2)
    template_index, template_row = template
    snippet = _object_text(snap.text, snap.player_path + ["dwb", template_index])
    stat_value = template_row.get("Kex", 0) if stat is None else stat
    rewritten = _rewrite_row(snippet, mission_id, progress, data, stat_value)
    label = f"Add {mission_id}, copied from a mission that has not started."
    stand_in = json.loads(rewritten.encode("utf-8", "backslashreplace").decode("utf-8"))
    snap.player.setdefault("dwb", []).append(stand_in)
    snap.pending_appends.append((snap.player_path + ["dwb"], rewritten, label))
    return len(snap.player["dwb"]) - 1, stand_in, True


def _template_row(player: dict) -> tuple[int, dict] | None:
    for index, row in _rows(player):
        if row.get("tW6") == -1 and not _seed_blocks(row.get("@EL", 0)):
            return index, row
    return None


def _object_text(text: str, path: list) -> str:
    start, end = locate(text, path)
    return text[start:end]


def _rewrite_row(snippet: str, mission_id: str, progress: int, data: int, stat: object) -> str:
    patches = [
        replace_patch(snippet, ["p0c"], dumps(mission_id), "mission"),
        replace_patch(snippet, ["tW6"], dumps(progress), "progress"),
        replace_patch(snippet, ["8?J"], dumps(data), "data"),
        replace_patch(snippet, ["Kex"], dumps(stat), "stat"),
    ]
    return apply(snippet, patches)


def _set_row_field(snap: Snapshot, plan: Plan, index: int, key: str, value: object, label: str) -> None:
    plan.changes.append(Change(label, snap.player_path + ["dwb", index, key], "set", value))


def _flush_pending(snap: Snapshot, plan: Plan) -> None:
    for path, raw, label in snap.pending_appends:
        plan.changes.append(Change(label, path, "append-raw", raw))
    snap.pending_appends = []


def _emit_reward_edits(
    snap: Snapshot,
    tables: GameTables,
    plan: Plan,
    items: list[ItemNeed],
    rewards: RewardPlan,
) -> None:
    _flush_pending(snap, plan)
    limits = limits_from_save(snap.parsed)
    placed, notes = _place_items(snap, items, tables, limits)
    plan.changes.extend(placed)
    for note in notes:
        if note.startswith("granted "):
            continue
        plan.skipped.append(note)
    plan.granted.extend(rewards.granted)
    plan.choices.extend(rewards.choices)
    plan.skipped.extend(rewards.skipped)
    _emit_others(snap, plan, rewards)


def _emit_others(snap: Snapshot, plan: Plan, rewards: RewardPlan) -> None:
    currency: dict[str, tuple[int, str]] = {}
    for other in rewards.others:
        if other.sort == "currency":
            amount, label = currency.get(other.key, (0, other.label))
            currency[other.key] = (amount + other.amount, label)
        elif other.sort == "list":
            _append_known(snap, plan, other.key, other.item_id, other.label, announce=True)
        elif other.sort == "flag":
            _set_flag(snap, plan, other.key, other.flag, other.label)
    for key, (amount, label) in currency.items():
        _add_currency(snap, plan, key, amount, label)


def _set_flag(snap: Snapshot, plan: Plan, key: str, value: bool, label: str) -> None:
    if snap.player.get(key) is value:
        return
    plan.changes.append(Change(label, snap.player_path + [key], "set", value))
    snap.player[key] = value


def _id_forms(item_id: str) -> set[str]:
    bare = item_id[1:] if item_id.startswith("^") else item_id
    if not bare:
        return set()
    return {bare, "^" + bare}


def _known_already(current: list, item_id: str) -> bool:
    """Exact id only. FRIGATE_FUEL does not count as FRIGATE_FUEL_1."""
    wanted = _id_forms(item_id)
    if not wanted:
        return False
    for item in current:
        if isinstance(item, str) and item in wanted:
            return True
    return False


def _mentions_id(line: str, item_id: str) -> bool:
    import re

    wanted = _id_forms(item_id)
    if not wanted:
        return False
    for token in re.findall(r"\^?[A-Za-z0-9_]+", line):
        if token in wanted:
            return True
    return False


def _drop_known_lines(plan: Plan, item_id: str) -> None:
    plan.granted = [
        line
        for line in plan.granted
        if not (("known " in line or "new recipe" in line) and _mentions_id(line, item_id))
    ]


def _hide_already_known(plan: Plan, player: dict, others: list) -> None:
    for other in others:
        if getattr(other, "sort", "") != "list":
            continue
        current = player.get(other.key)
        if not isinstance(current, list):
            continue
        if not _known_already(current, other.item_id):
            continue
        _drop_known_lines(plan, other.item_id)
        note = f"{other.item_id}: already known."
        if note not in plan.skipped:
            plan.skipped.append(note)


def _append_known(
    snap: Snapshot,
    plan: Plan,
    key: str,
    item_id: str,
    label: str,
    announce: bool = False,
) -> None:
    current = snap.player.get(key)
    if not isinstance(current, list):
        plan.skipped.append(f"{item_id}: {key} is not a list in this save.")
        return
    if _known_already(current, item_id):
        if announce:
            _drop_known_lines(plan, item_id)
            note = f"{item_id}: already known."
            if note not in plan.skipped:
                plan.skipped.append(note)
        return
    plan.changes.append(Change(label, snap.player_path + [key], "append", item_id))
    current.append(item_id)


def _add_currency(snap: Snapshot, plan: Plan, key: str, amount: int, label: str) -> None:
    current = snap.player.get(key, 0)
    if isinstance(current, bool) or not isinstance(current, int):
        plan.skipped.append(f"{label} The current value is not a whole number.")
        return
    true_value = stored_to_true(current) + amount
    updated = true_to_stored(true_value)
    if true_value > CURRENCY_CAP:
        label = f"{label} Clamped to {CURRENCY_CAP}."
    if updated == current:
        plan.skipped.append(f"{label} Already at the cap.")
        return
    plan.changes.append(Change(label, snap.player_path + [key], "set", updated))
    snap.player[key] = updated


def _retarget_current(
    snap: Snapshot,
    tables: GameTables | None,
    plan: Plan,
    finished: list[str],
    version: int | None,
) -> None:
    if not finished:
        return
    current = snap.player.get(";R7")
    if current not in finished:
        return
    previous = snap.player.get("Mg<")
    if not isinstance(previous, str) or not previous or _mission_done(snap, tables, previous, finished, version):
        new_value = "^"
    else:
        new_value = previous
    if new_value == current:
        return
    plan.changes.append(Change("Set the tracked mission aside.", snap.player_path + [";R7"], "set", new_value))


def _mission_done(
    snap: Snapshot,
    tables: GameTables | None,
    mission_id: str,
    finished: list[str],
    version: int | None,
) -> bool:
    if mission_id in finished or mission_id == "^":
        return True
    found = _row(snap.player, mission_id)
    if found is None:
        return False
    final = _known_final(tables, mission_id, version)
    progress = found[1].get("tW6")
    return final is not None and isinstance(progress, int) and progress >= final


def _tech_installed(player: dict, tech_id: str) -> bool:
    wanted = {tech_id, tech_id.lstrip("^"), caret_id(tech_id)}
    if _bag_has_tech(player.get(";l5"), wanted) or _bag_has_tech(player.get("PMT"), wanted):
        return True
    ships = player.get("@Cs")
    if not isinstance(ships, list):
        return False
    for ship in ships:
        if not isinstance(ship, dict):
            continue
        if _bag_has_tech(ship.get("PMT"), wanted) or _bag_has_tech(ship.get(";l5"), wanted):
            return True
    return False


def _bag_has_tech(bag: object, wanted: set[str]) -> bool:
    if not isinstance(bag, dict):
        return False
    slots = bag.get(":No")
    if not isinstance(slots, list):
        return False
    return any(isinstance(slot, dict) and slot.get("b2n") in wanted for slot in slots)


def _place_items(
    snap: Snapshot,
    items: list[ItemNeed],
    tables: GameTables,
    limits: dict,
) -> tuple[list[Change], list[str]]:
    bags = _bags(snap)
    changes: list[Change] = []
    notes: list[str] = []
    for item in _merge_items(items):
        if item.amount <= 0:
            continue
        if "#" in item.item_id:
            notes.append(f"{item.item_id}: not granted, procedural items are not created.")
            continue
        known = _item_known(item.item_id, tables)
        remaining = item.amount
        touched = False
        for bag in bags:
            if remaining <= 0:
                break
            if not bag["usable"]:
                continue
            remaining, did = _top_up(bag, item, remaining, changes)
            touched = touched or did
            if remaining <= 0:
                break
            if not known:
                continue
            remaining, did = _new_slots(bag, item, remaining, tables, limits, changes)
            touched = touched or did
        if remaining <= 0:
            notes.append(f"granted {item.amount} {item.item_id}.")
        elif not known and not touched:
            notes.append(f"{item.item_id} is not in the product or substance tables. It will be skipped.")
        elif not known:
            notes.append(
                f"{item.item_id}: {remaining} left. It is not in the product or substance tables. It will be skipped."
            )
        else:
            notes.append(f"{item.item_id}: {remaining} not granted, no space.")
        if touched and remaining <= 0:
            pass
    return changes, notes


def _merge_items(items: list[ItemNeed]) -> list[ItemNeed]:
    """Two copies of one item fill a single stack, up to that stack's max."""
    merged: list[ItemNeed] = []
    index: dict[tuple[str, str], int] = {}
    for item in items:
        key = (item.item_id, item.kind)
        found = index.get(key)
        if found is None:
            index[key] = len(merged)
            merged.append(ItemNeed(item.item_id, item.kind, item.amount, item.reward_id))
            continue
        merged[found].amount += item.amount
    return merged


def _item_known(item_id: str, tables: GameTables) -> bool:
    bare = item_id[1:] if item_id.startswith("^") else item_id
    return item_id in tables.products or bare in tables.products or item_id in tables.substances or bare in tables.substances


def _bags(snap: Snapshot) -> list[dict]:
    player = snap.player
    base = snap.player_path
    bags: list[dict] = []
    suit = _bag(player.get(";l5"), base + [";l5"], "exosuit", True)
    if suit is not None:
        bags.append(suit)
    ships = player.get("@Cs")
    index = player.get("aBE", 0)
    ship_bag = None
    if isinstance(ships, list) and isinstance(index, int) and 0 <= index < len(ships) and isinstance(ships[index], dict):
        ship = ships[index]
        ship_bag = _bag(ship.get(";l5"), base + ["@Cs", index, ";l5"], "ship", _ship_ready(ship))
    if ship_bag is not None:
        bags.append(ship_bag)
    freighter = player.get("8ZP")
    if isinstance(freighter, dict):
        node = freighter.get(";l5") if isinstance(freighter.get(";l5"), dict) else freighter
        path = base + ["8ZP", ";l5"] if node is not freighter else base + ["8ZP"]
        valid = node.get("hl?") if isinstance(node, dict) else None
        usable = isinstance(valid, list) and len(valid) > 0
        freighter_bag = _bag(node, path, "freighter", usable)
        if freighter_bag is not None:
            bags.append(freighter_bag)
    return bags


def _ship_ready(ship: dict) -> bool:
    """A real ship stores the model filename inside the resource entry, one level down."""
    resource = ship.get("NTx")
    filename = ship.get("93M")
    if isinstance(resource, dict):
        nested = resource.get("93M")
        if isinstance(nested, str) and nested:
            filename = nested
        resource_ok = bool(resource)
    else:
        resource_ok = isinstance(resource, str) and resource != ""
    return resource_ok and isinstance(filename, str) and filename != ""


def _bag(node: object, path: list, group: str, usable: bool) -> dict | None:
    if not isinstance(node, dict):
        return None
    slots = node.get(":No")
    if not isinstance(slots, list):
        slots = []
    valid = []
    raw_valid = node.get("hl?")
    if isinstance(raw_valid, list):
        for entry in raw_valid:
            coord = _coord(entry)
            if coord is not None:
                valid.append(coord)
    occupied = set()
    working = []
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            continue
        copy = dict(slot)
        working.append((index, copy))
        coord = _coord(copy)
        if coord is not None:
            occupied.add(coord)
    empty = sorted((coord for coord in valid if coord not in occupied), key=lambda item: (item[1], item[0]))
    return {"path": path, "group": group, "usable": usable, "slots": working, "empty": empty}


def _coord(entry: object) -> tuple[int, int] | None:
    if not isinstance(entry, dict):
        return None
    point = entry.get("3ZH", entry)
    if not isinstance(point, dict):
        return None
    if ">Qh" not in point or "XJ>" not in point:
        return None
    try:
        return int(point[">Qh"]), int(point["XJ>"])
    except (TypeError, ValueError):
        return None


def _top_up(bag: dict, item: ItemNeed, remaining: int, changes: list[Change]) -> tuple[int, bool]:
    did = False
    for index, slot in bag["slots"]:
        if remaining <= 0:
            break
        if not _same_item(slot.get("b2n"), item.item_id):
            continue
        amount = slot.get("1o9")
        cap = slot.get("F9q")
        if not isinstance(amount, int) or isinstance(amount, bool):
            continue
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            cap = amount
        space = cap - amount
        if space <= 0:
            continue
        put = min(space, remaining)
        slot["1o9"] = amount + put
        remaining -= put
        did = True
        changes.append(
            Change(
                f"Top up {item.item_id} in the {bag['group']} by {put}.",
                bag["path"] + [":No", index, "1o9"],
                "set",
                amount + put,
            )
        )
    return remaining, did


def _new_slots(
    bag: dict,
    item: ItemNeed,
    remaining: int,
    tables: GameTables,
    limits: dict,
    changes: list[Change],
) -> tuple[int, bool]:
    multiplier = _multiplier(item, tables)
    cap = stack_size(item.kind, bag["group"], multiplier, limits)
    did = False
    while remaining > 0 and bag["empty"]:
        x_pos, y_pos = bag["empty"].pop(0)
        put = min(cap, remaining)
        slot = {
            "Vn8": {"elv": item.kind},
            "b2n": item.item_id,
            "1o9": put,
            "F9q": cap,
            "eVk": 0.0,
            "b76": True,
            "5tH": False,
            "3ZH": {">Qh": x_pos, "XJ>": y_pos},
        }
        changes.append(
            Change(
                f"Add {put} {item.item_id} to the {bag['group']}.",
                bag["path"] + [":No"],
                "append",
                slot,
            )
        )
        remaining -= put
        did = True
    return remaining, did


def _same_item(found: object, item_id: str) -> bool:
    if not isinstance(found, str) or not item_id:
        return False
    if found == item_id:
        return True
    return found.lstrip("^") == item_id.lstrip("^")


def _multiplier(item: ItemNeed, tables: GameTables) -> int:
    bare = item.item_id[1:] if item.item_id.startswith("^") else item.item_id
    if item.kind == "Substance":
        if item.item_id in tables.substances:
            return tables.substances[item.item_id]
        return tables.substances.get(bare, 1)
    if item.item_id in tables.products:
        return tables.products[item.item_id]
    return tables.products.get(bare, 1)


def _patched_text(text: str, changes: list[Change]) -> tuple[str, list[Patch]]:
    patches = _patches_for(text, changes)
    return apply(text, patches), patches


def _patches_for(text: str, changes: list[Change]) -> list[Patch]:
    sets: dict[tuple, Change] = {}
    appends: dict[tuple, list[Change]] = {}
    set_order: list[tuple] = []
    append_order: list[tuple] = []
    for change in changes:
        key = tuple(change.path)
        if change.op == "set":
            if key not in sets:
                set_order.append(key)
            sets[key] = change
        else:
            if key not in appends:
                append_order.append(key)
                appends[key] = []
            appends[key].append(change)
    patches: list[Patch] = []
    for key in set_order:
        change = sets[key]
        patches.append(replace_patch(text, list(key), dumps(change.value), change.label))
    for key in append_order:
        pieces = []
        labels = []
        for change in appends[key]:
            labels.append(change.label)
            if change.op == "append-raw":
                pieces.append(str(change.value))
            else:
                pieces.append(dumps(change.value))
        label = labels[0] if len(labels) == 1 else f"Add {len(labels)} entries."
        patches.append(append_patch(text, list(key), ",".join(pieces), label))
    return patches


def _self_check(snap: Snapshot, patched: str, patches: list[Patch], changes: list[Change]) -> None:
    if not unchanged_outside(snap.text, patched, patches):
        raise EditError("A patch changed bytes outside its span.", code=2)
    parsed = loads_save(patched)
    sets: dict[tuple, Change] = {}
    for change in changes:
        if change.op == "set":
            sets[tuple(change.path)] = change
    for change in sets.values():
        if _at(parsed, change.path) != change.value:
            raise EditError(f"Self-check failed for {change.label}.", code=2)
    for change in changes:
        if change.op == "append":
            array = _at(parsed, change.path)
            if not isinstance(array, list) or change.value not in array:
                raise EditError(f"Self-check failed for {change.label}.", code=2)
        if change.op == "append-raw":
            array = _at(parsed, change.path)
            expected = json.loads(str(change.value).encode("utf-8", "backslashreplace").decode("utf-8"))
            if not isinstance(array, list) or expected not in array:
                raise EditError(f"Self-check failed for {change.label}.", code=2)


def _at(node: object, path: list) -> object:
    current = node
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list):
                return None
            current = current[part]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
    return current


def _snapshot_matches(snap: Snapshot, *, quick: bool = False) -> bool:
    if not snap.path.is_file():
        return False
    st = snap.path.stat()
    if st.st_mtime_ns != snap.save_mtime_ns or st.st_size != len(snap.save_bytes):
        return False
    if snap.mf_bytes is not None:
        if not snap.mf_path.is_file():
            return False
        mf = snap.mf_path.stat()
        if mf.st_mtime_ns != snap.mf_mtime_ns or mf.st_size != len(snap.mf_bytes):
            return False
    if quick:
        return True
    if hashlib.sha256(snap.path.read_bytes()).hexdigest() != snap.save_sha:
        return False
    if snap.mf_bytes is None:
        return True
    return hashlib.sha256(snap.mf_path.read_bytes()).hexdigest() == snap.mf_sha


def _partner_paths(path: Path) -> list[Path]:
    import re

    if re.fullmatch(r"save\.hg", path.name, re.IGNORECASE):
        return [path.with_name("save2.hg"), path.with_name("save1.hg")]
    match = re.search(r"save(\d+)\.hg$", path.name, re.IGNORECASE)
    if match is None:
        return []
    number = int(match.group(1))
    other = number + 1 if number % 2 else number - 1
    names = [path.with_name(f"save{other}.hg")]
    if number in {1, 2}:
        names.append(path.with_name("save.hg"))
    return names


_PARTNER_PLAY: dict[tuple, object] = {}


def _partner_play_time(path: Path, *, fresh: bool = False) -> object:
    """In-save timestamp of the other slot file, cached by size and mtime."""
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if fresh:
        _PARTNER_PLAY.pop(key, None)
    elif key in _PARTNER_PLAY:
        return _PARTNER_PLAY[key]
    try:
        with timed("partner-parse"):
            payload = unpack_save(path.read_bytes())
            parsed = loads_save(payload.json_text)
    except (SaveFormatError, json.JSONDecodeError, OSError):
        return None
    found = find_value(parsed, "b@r")
    value = None if found is None else found[1]
    if len(_PARTNER_PLAY) > 8:
        _PARTNER_PLAY.clear()
    _PARTNER_PLAY[key] = value
    return value


def partner_load_problem(
    copy_path: Path,
    live_dir: Path,
    source_mtime_ns: int | None = None,
) -> str | None:
    """The other live file, when Put would not change what the game loads.

    Write and Undo already refuse a newer partner. Put used to compare only
    the file of the same name. The baseline is this copy, and the earlier
    source mtime from when the copy was taken, when that is older.
    """
    copy_path = Path(copy_path)
    if not copy_path.is_file():
        return None
    anchor = Path(live_dir) / copy_path.name
    siblings = [item for item in _partner_paths(anchor) if item.is_file()]
    if not siblings:
        return None
    partner = max(siblings, key=lambda item: item.stat().st_mtime_ns)
    try:
        copy_mtime = copy_path.stat().st_mtime_ns
        partner_mtime = partner.stat().st_mtime_ns
    except OSError:
        return None
    baseline = copy_mtime
    if source_mtime_ns:
        baseline = min(baseline, int(source_mtime_ns))
    newer_file = partner_mtime > baseline
    theirs = _save_timestamp(partner)
    ours = _save_timestamp(copy_path)
    later_stamp = theirs is not None and ours is not None and theirs > ours
    if not newer_file and not later_stamp:
        return None
    if source_mtime_ns and partner_mtime > int(source_mtime_ns) and partner_mtime <= copy_mtime and not later_stamp:
        return (
            f"{partner.name} is newer than this copy's source. "
            f"The game will load {partner.name}."
        )
    return f"{partner.name} is newer than {copy_path.name}. The game will load {partner.name}."


def partner_after_put(live_dir: Path, installed_name: str) -> str | None:
    """After the write, name the other file when it is still the one the game will load.

    copy2 keeps the copy's mtime. A copy that is newer than the partner becomes
    the file the game loads, so the warning from before the write is stale.
    """
    installed = Path(live_dir) / installed_name
    if not installed.is_file():
        return None
    siblings = [item for item in _partner_paths(installed) if item.is_file()]
    if not siblings:
        return None
    partner = max(siblings, key=lambda item: item.stat().st_mtime_ns)
    try:
        installed_mtime = installed.stat().st_mtime_ns
        partner_mtime = partner.stat().st_mtime_ns
    except OSError:
        return None
    theirs = _save_timestamp(partner)
    ours = _save_timestamp(installed)
    later_stamp = theirs is not None and ours is not None and theirs > ours
    if partner_mtime > installed_mtime or (later_stamp and partner_mtime >= installed_mtime):
        return f"The game will load {partner.name}."
    return None


def _partner_problem(snap: Snapshot, *, fresh: bool = False) -> str | None:
    siblings = [item for item in _partner_paths(snap.path) if item.is_file()]
    if not siblings:
        return None
    partner = max(siblings, key=lambda item: item.stat().st_mtime_ns)
    if partner.stat().st_mtime_ns > snap.save_mtime_ns:
        return f"{partner.name} is newer than {snap.path.name}. The game would load the other file."
    theirs = _partner_play_time(partner, fresh=fresh)
    ours = _play_time_value(snap)
    if ours is None or theirs is None:
        return None
    if isinstance(ours, (int, float)) and isinstance(theirs, (int, float)) and theirs > ours:
        return f"{partner.name} has a later timestamp than {snap.path.name}. The game would load the other file."
    return None


def _pair_problem(snap: Snapshot) -> tuple[str, str] | None:
    if snap.manifest is None:
        return ("fail", "There is no mf_ file beside this save, so the size fields cannot be updated.")
    if not snap.manifest.format_ok:
        return ("fail", "The manifest is not format 2004, so it was not rewritten.")
    if snap.raw_len:
        raw_len = snap.raw_len
    else:
        try:
            raw_len = len(unpack_save(snap.save_bytes).raw)
        except SaveFormatError as exc:
            return ("fail", str(exc))
    if snap.manifest.decompressed_size != raw_len:
        return ("fail", "The manifest decompressed size does not match the save.")
    packed_len = len(snap.save_bytes) if snap.chunked else raw_len
    if snap.manifest.compressed_size != packed_len:
        return ("warn", "The manifest compressed size does not match the file. Writing will set it from the new file.")
    return None


def _written_ok(snap: Snapshot, packed: bytes, encrypted: bytes, raw: bytes) -> bool:
    if snap.path.read_bytes() != packed or snap.mf_path.read_bytes() != encrypted:
        return False
    try:
        if unpack_save(snap.path.read_bytes()).raw != raw:
            return False
        manifest = read_manifest(snap.mf_path)
    except (OSError, SaveFormatError, ManifestError):
        return False
    return manifest.decompressed_size == len(raw) and manifest.compressed_size == len(packed)


def _atomic_replace(path: Path, data: bytes, suffix: str = ".nmsmissions-tmp") -> None:
    temporary = path.with_name(path.name + suffix)
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _tables(request: EditRequest, progress=None) -> GameTables | None:
    if request.game_files is None:
        return None
    from nmsmissions.gamedata import cache_path_for_tables

    root = Path(request.game_files)
    return load_tables(
        root,
        Path(request.pcbanks) if request.pcbanks else None,
        cache=cache_path_for_tables(root),
        progress=progress,
    )


def _source_stamp(game_files: Path | None, pcbanks: Path | None) -> tuple:
    rows: list[tuple[str, int, int]] = []
    for root in (game_files, pcbanks):
        if root is None or not Path(root).exists():
            continue
        path = Path(root)
        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
        for item in files:
            if item.suffix.lower() not in {".mxml", ".exml", ".xml", ".pak"}:
                continue
            stat = item.stat()
            rows.append((str(item), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(rows))


def _note_backup(request: EditRequest, snap: Snapshot, plan: Plan) -> None:
    if not plan.changes or plan.backup_path:
        return
    directory = request.backup_dir or default_backup_dir(save_slot(snap.path))
    plan.backup_path = str(_unique_zip(directory, snap.path.name))


def _present(request: EditRequest, plan: Plan, snap: Snapshot) -> None:
    if request.quiet:
        return
    if request.as_json:
        print(json.dumps(_plan_json(plan), indent=2))
        return
    dry = not request.apply or request.action == "rewards"
    hint = "This preview does not write the save." if request.action == "rewards" else None
    print(format_plan(plan, dry_run=dry, apply_hint=hint))
    if snap.manifest is None:
        return


def _plan_json(plan: Plan) -> dict:
    return {
        "summary": plan.summary,
        "changes": [
            {"label": change.label, "op": change.op, "path": change.path, "value": change.value}
            for change in plan.changes
            if change.op != "append-raw"
        ],
        "granted": plan.granted,
        "choices": plan.choices,
        "skipped": plan.skipped,
        "warnings": plan.warnings,
        "checks": [{"level": check.level, "message": check.message} for check in plan.checks],
    }


def _write_patch_out(path: Path, text: str, changes: list[Change]) -> None:
    patches = _patches_for(text, changes)
    lines = []
    for patch in patches:
        lines.append(f"{patch.start}\t{patch.end}\t{patch.label}\t{patch.old}\t{patch.new}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _unique_zip(directory: Path, save_name: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{save_name}.{stamp}.zip"
    extra = 2
    while candidate.exists():
        candidate = directory / f"{save_name}.{stamp}-{extra}.zip"
        extra += 1
    return candidate


def _zip_day(name: str) -> str | None:
    import re

    match = re.search(r"\.(\d{8})-\d{6}", name)
    return match.group(1) if match else None


def _zip_matches(path: Path, snap: Snapshot) -> bool:
    try:
        with zipfile.ZipFile(path) as handle:
            if handle.testzip() is not None:
                return False
            save = handle.read(snap.path.name)
            if hashlib.sha256(save).hexdigest() != snap.save_sha or save != snap.save_bytes:
                return False
            if snap.mf_bytes is not None:
                manifest = handle.read(snap.mf_path.name)
                if manifest != snap.mf_bytes:
                    return False
            json.loads(handle.read("saveinfo.json"))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        return False
    return True


def _rekey_manifest(manifest_bytes: bytes, source_name: str, target: Path) -> bytes:
    """Encrypt the same plain manifest for the file it is being written as."""
    plain, _slot = decrypt_manifest(manifest_bytes, archive_number(Path(source_name)))
    return encrypt_manifest(plain, key_slot_for(archive_number(target)))


def _restore_zip(zip_path: Path, dest: Path, save_name: str | None = None) -> None:
    with zipfile.ZipFile(zip_path) as handle:
        info = json.loads(handle.read("saveinfo.json"))
        stored_name = info["save"]
        save_bytes = handle.read(stored_name)
        target_name = save_name or stored_name
        target = dest / target_name
        _atomic_replace(target, save_bytes, suffix=".nmsmissions-restore")
        os.utime(target, ns=(int(info["save_mtime_ns"]), int(info["save_mtime_ns"])))
        manifest_name = info.get("manifest")
        if manifest_name:
            manifest_bytes = handle.read(manifest_name)
            if target_name != stored_name:
                manifest_bytes = _rekey_manifest(manifest_bytes, stored_name, target)
                manifest_name = "mf_" + target_name
            manifest_path = dest / manifest_name
            _atomic_replace(manifest_path, manifest_bytes, suffix=".nmsmissions-restore")
            if info.get("mf_mtime_ns"):
                os.utime(manifest_path, ns=(int(info["mf_mtime_ns"]), int(info["mf_mtime_ns"])))


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            **hidden_window_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return out.strip()


def backup_now(path: Path, backup_dir: Path | None = None, tables: GameTables | None = None) -> Path:
    """Zip the save and its mf_ file. A working copy can be backed up while the game is open."""
    path = Path(path)
    if nms_is_running() and is_live_nms_save(path):
        raise EditError("No Man's Sky is running. Close it before backing up the game folder.", code=2)
    snap = take_snapshot(path)
    plan = Plan(summary="Backup only.", mission_version=_version(snap.player))
    directory = backup_dir or default_backup_dir(save_slot(snap.path))
    return write_backup(directory, snap, plan, tables)


def _backup_only(request: EditRequest) -> int:
    if request.save is None:
        raise EditError("Name a save file.", code=2)
    backup = backup_now(request.save, request.backup_dir, _tables(request))
    _say(request, f"Backup: {backup}")
    _say(request, "The save was not modified.")
    return 0


def _restore(request: EditRequest) -> int:
    if request.restore_zip is None:
        raise EditError("Name the backup zip.", code=2)
    zip_path = Path(request.restore_zip)
    if not zip_path.is_file():
        raise EditError(f"No backup at {zip_path}.", code=2)
    with zipfile.ZipFile(zip_path) as handle:
        if handle.testzip() is not None:
            raise EditError("That backup zip is damaged.", code=2)
        info = json.loads(handle.read("saveinfo.json"))
    dest = Path(request.restore_to) if request.restore_to else zip_path.parent
    target_name = request.restore_name or info["save"]
    save_path = dest / target_name
    if target_name == info["save"]:
        _say(request, f"Restore {info['save']} and its manifest into {dest}.")
    else:
        _say(request, f"Restore {info['save']} into {target_name} in {dest}.")
    if game_is_running(request.running) and is_live_nms_save(save_path):
        raise EditError("No Man's Sky is running. Close it before restoring into the game folder.", code=2)
    if is_live_nms_save(save_path) and not (request.live and request.confirm_slot == save_slot(save_path)):
        raise EditError(
            f"This restore lands in a live HelloGames save. Pass --apply --live --confirm-slot {save_slot(save_path)}.",
            code=2,
        )
    if not request.apply:
        _say(request, "Dry run. Nothing is written. Pass --apply to write.")
        return 0
    if save_path.is_file():
        current = take_snapshot(save_path)
        write_backup(request.backup_dir or default_backup_dir(save_slot(save_path)), current, Plan(summary="Before restore."), None)
    _restore_zip(zip_path, dest, save_name=request.restore_name)
    _say(request, "Restored both files.")
    if is_live_nms_save(save_path):
        from nmsmissions.slots import align_copies_for_live

        align_copies_for_live(dest, save_slot(save_path), request.backup_dir)
    else:
        from nmsmissions.slots import load_state, retain_tracked_save

        if load_state(dest) is not None:
            retain_tracked_save(dest, target_name, request.backup_dir)
    return 0


def _install(request: EditRequest) -> int:
    if request.install_folder is None or request.install_slot is None or request.install_to is None:
        raise EditError("install needs a copy folder, --slot, and --to.", code=2)
    slot = request.install_slot
    folder = Path(request.install_folder)
    sources = _slot_sources(folder, slot)
    dest = Path(request.install_to).resolve()
    _say(request, f"Install slot {slot} into {dest}.")
    for save, manifest in sources:
        _say(request, f"- {save.name}")
        _say(request, f"- {manifest.name}")
    if nms_is_running():
        raise EditError("No Man's Sky is running. Close it before installing a save.", code=2)
    if is_live_nms_save(dest) and not (request.live and request.confirm_slot == slot):
        raise EditError(
            f"The destination is a live HelloGames folder. Pass --apply --live --confirm-slot {slot}.",
            code=2,
        )
    for save, manifest in sources:
        for source, name in ((save, save.name), (manifest, manifest.name)):
            target = dest / name
            if not target.is_file():
                continue
            reason = _newer_than_copy(target, source)
            if reason:
                raise EditError(reason, code=2)
    if not request.apply:
        _say(request, "Dry run. Nothing is written. Pass --apply to write.")
        return 0
    backups = _backup_install_targets(request, dest, sources, slot)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for save, manifest in sources:
            shutil.copy2(save, dest / save.name)
            shutil.copy2(manifest, dest / manifest.name)
        for save, manifest in sources:
            problem = _install_mismatch(save, manifest, dest)
            if problem:
                raise EditError(problem, code=4)
    except EditError as exc:
        if exc.code == 4:
            _rollback_install(backups, dest, sources)
        raise
    except OSError as exc:
        _rollback_install(backups, dest, sources)
        raise EditError(f"Install could not copy the files ({exc}).", code=2) from exc
    _say(request, "Copied both files of the pair.")
    return 0


def _slot_sources(folder: Path, slot: int) -> list[tuple[Path, Path]]:
    """Files to install for a slot. Slot 1 may be save.hg plus save2.hg."""
    names = [f"save{slot * 2 - 1}.hg", f"save{slot * 2}.hg"]
    if slot == 1:
        names = ["save.hg", *names]
    found: list[tuple[Path, Path]] = []
    for name in names:
        save = folder / name
        manifest = folder / f"mf_{name}"
        if save.is_file() and manifest.is_file():
            found.append((save, manifest))
    if slot == 1:
        have = {save.name.lower() for save, _manifest in found}
        odd = "save.hg" in have or "save1.hg" in have
        even = "save2.hg" in have
        if not odd or not even:
            raise EditError(
                "Slot 1 needs save.hg or save1.hg, plus save2.hg, each with its mf_ file.",
                code=2,
            )
        return found
    if len(found) != 2:
        left, right = slot * 2 - 1, slot * 2
        raise EditError(
            f"Slot {slot} needs save{left}.hg and mf_save{left}.hg, and save{right}.hg and mf_save{right}.hg, in the copy folder.",
            code=2,
        )
    return found


def install_one_save(
    source: Path,
    dest_dir: Path,
    slot: int,
    backup_dir: Path | None,
    *,
    apply: bool,
    live: bool,
    confirm_slot: int | None,
    allow_partner: bool = False,
    source_mtime_ns: int | None = None,
    running: bool | None = None,
) -> tuple[int, str, Path | None]:
    """Copy one save and its mf_ file into the game folder.

    Same checks as install: the game must be closed, a newer live file is
    refused, a backup is written first, and a failed check is rolled back.
    The third value is the backup of the live file from just before the copy.
    """
    source = Path(source)
    manifest = manifest_path_for(source)
    dest = Path(dest_dir).resolve()
    if not source.is_file() or not manifest.is_file():
        raise EditError(f"Need {source.name} and {manifest.name}.", code=2)
    if game_is_running(running):
        raise EditError("No Man's Sky is running. Close it before putting a save into the game.", code=2)
    partner_note = partner_load_problem(source, dest, source_mtime_ns)
    if partner_note and not allow_partner:
        raise EditError(partner_note, code=2)
    if is_live_nms_save(dest) and not (live and confirm_slot == slot):
        raise EditError(
            f"This is the live game folder. Confirm slot {slot} before writing.",
            code=2,
        )
    target = dest / source.name
    target_mf = dest / manifest.name
    for found, name in ((target, source.name), (target_mf, manifest.name)):
        if not found.is_file():
            continue
        reason = _newer_than_copy(found, source if name == source.name else manifest)
        if reason:
            raise EditError(reason, code=2)
    if not apply:
        return 0, "Dry run. Nothing is written.", None
    request = EditRequest(action="install", backup_dir=backup_dir, confirm_slot=slot, live=live, apply=True)
    backups = _backup_install_targets(request, dest, [(source, manifest)], slot)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
        shutil.copy2(manifest, target_mf)
        problem = _install_mismatch(source, manifest, dest)
        if problem:
            raise EditError(problem, code=4)
    except EditError as exc:
        if exc.code == 4:
            _rollback_install(backups, dest, [(source, manifest)])
        raise
    except OSError as exc:
        _rollback_install(backups, dest, [(source, manifest)])
        raise EditError(f"The save could not be copied ({exc}).", code=2) from exc
    message = f"Slot {slot} now has {source.name}."
    outcome = partner_after_put(dest, source.name)
    if outcome:
        message += " " + outcome
    return 0, message, backups[0] if backups else None


def installed_live_path(state) -> Path:
    """The game file that was put, even if the window is now tracking the other file."""
    recorded = getattr(state, "installed_source", "") or ""
    if recorded:
        return Path(recorded)
    source = Path(state.source) if getattr(state, "source", "") else Path(".")
    zip_path = Path(state.undo_zip) if getattr(state, "undo_zip", "") else None
    if zip_path is not None and zip_path.is_file():
        try:
            with zipfile.ZipFile(zip_path) as handle:
                info = json.loads(handle.read("saveinfo.json"))
            name = str(info.get("save") or "")
            if name:
                return source.with_name(name)
        except (OSError, KeyError, json.JSONDecodeError, ValueError, zipfile.BadZipFile):
            pass
    return source


def _slot_save_paths(anchor: Path) -> list[Path]:
    """Every save file in this slot. The tracked file does not change the set."""
    found: list[Path] = []
    seen: set[str] = set()
    for path in (anchor, *_partner_paths(anchor)):
        key = path.name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def newer_slot_files(state) -> tuple[str, list[Path]]:
    """'ok', 'partner', or 'partner_lost', plus slot files newer than the put.

    The check uses the file that was put, not whichever file the window is tracking.
    The game loads the newer file of the pair. Undo has to see that file too.
    """
    installed = installed_live_path(state)
    put_ns = int(state.installed_at_ns or state.source_mtime_ns or 0)
    installed_key = installed.resolve() if installed.is_file() else installed
    newer: list[Path] = []
    for path in _slot_save_paths(installed):
        if not path.is_file():
            continue
        if path.resolve() == installed_key:
            continue
        if put_ns and path.stat().st_mtime_ns > put_ns:
            newer.append(path)
    if not newer:
        return "ok", []
    zips = state.partner_zips or {}
    missing = [path for path in newer if not zips.get(path.name) or not Path(zips[path.name]).is_file()]
    if missing:
        return "partner_lost", newer
    return "partner", newer


def _partner_undo_message(kind: str, names: str, installed_name: str) -> str:
    if kind == "partner_lost":
        return (
            f"The game saved {names} after the put. The game will load that file. "
            "There is no copy of it from before the put, so Undo cannot change what the game will load. "
            "Use Restore a backup."
        )
    return (
        f"The game saved {names} after the put. The game will load that file. "
        f"Restoring {installed_name} alone does not undo the slot. "
        f"Put {names} back to how it was before the put? The current file is backed up first."
    )


def undo_last_install(
    copy_dir: Path,
    backup_dir: Path | None,
    *,
    apply: bool,
    live: bool,
    confirm_slot: int | None,
    allow_changed: bool,
    allow_partner: bool = False,
    running: bool | None = None,
) -> tuple[int, str]:
    """Put the game slot back to the backup taken before the last install.

    The current live file is backed up first. The game must be closed.
    A live file that changed after the install is refused unless allow_changed.
    If the other file of the slot is newer than the put, that file is restored
    too when allow_partner is set and a pre-put copy was kept.
    """
    from nmsmissions.slots import load_state, save_state

    state = load_state(copy_dir)
    if state is None or not state.undo_zip:
        raise EditError("There is no put-into-game to undo.", code=2)
    zip_path = Path(state.undo_zip)
    if not zip_path.is_file():
        raise EditError(f"The undo backup is missing ({zip_path.name}).", code=2)
    live_path = installed_live_path(state)
    dest = live_path.parent
    slot = state.slot
    if game_is_running(running):
        raise EditError("No Man's Sky is running. Close it before undoing.", code=2)
    if is_live_nms_save(dest) and not (live and confirm_slot == slot):
        raise EditError(f"This undo writes the live game folder. Confirm slot {slot}.", code=2)
    kind, newer = newer_slot_files(state)
    if kind != "ok":
        names = " and ".join(path.name for path in newer)
        if kind == "partner_lost" or not allow_partner:
            raise EditError(_partner_undo_message(kind, names, live_path.name), code=2)
    changed = False
    if live_path.is_file() and state.installed_sha256:
        changed = hashlib.sha256(live_path.read_bytes()).hexdigest() != state.installed_sha256
    if changed and not allow_changed:
        raise EditError(
            f"{live_path.name} changed after the last put-into-game. Undo would replace that newer file.",
            code=2,
        )
    if not apply:
        return 0, "Dry run. Nothing is written."
    safeties: list[Path] = []
    directory = backup_dir or default_backup_dir(slot)
    if live_path.is_file():
        snap = take_snapshot(live_path)
        safeties.append(write_backup(directory, snap, Plan(summary="Before undo."), None))
    if kind == "partner":
        for partner in newer:
            if partner.is_file():
                snap = take_snapshot(partner)
                safeties.append(write_backup(directory, snap, Plan(summary=f"Before undo of {partner.name}."), None))
    try:
        _restore_zip(zip_path, dest)
        _undo_matches(zip_path, dest)
        if kind == "partner":
            for partner in newer:
                partner_zip = Path(state.partner_zips[partner.name])
                _restore_zip(partner_zip, dest)
                _undo_matches(partner_zip, dest)
    except EditError as exc:
        if exc.code == 4:
            _rollback_undo(safeties, dest)
        raise
    except OSError as exc:
        _rollback_undo(safeties, dest)
        raise EditError(f"Undo could not write the save ({exc}).", code=2) from exc
    state.undo_zip = ""
    state.installed_sha256 = ""
    state.installed_at_ns = 0
    state.installed_source = ""
    state.partner_zips = {}
    if live_path.is_file():
        state.source = str(live_path.resolve())
    save_state(copy_dir, state)
    from nmsmissions.slots import align_working_copy_to_game

    align_working_copy_to_game(copy_dir, backup_dir)
    message = f"Slot {slot} is back to the file from before the last put-into-game."
    if kind == "partner":
        names = " and ".join(path.name for path in newer)
        verb = "is" if len(newer) == 1 else "are"
        message += f" {names} {verb} back too, so the game loads the old slot."
    return 0, message


def _undo_matches(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as handle:
        info = json.loads(handle.read("saveinfo.json"))
        expected = handle.read(info["save"])
        expected_mf = handle.read(info["manifest"]) if info.get("manifest") else None
    restored = dest / info["save"]
    if restored.read_bytes() != expected:
        raise EditError("Undo failed its check. The backup was written back.", code=4)
    if expected_mf is not None and (dest / info["manifest"]).read_bytes() != expected_mf:
        raise EditError("Undo failed its check. The backup was written back.", code=4)


def _rollback_undo(safeties: list[Path], dest: Path) -> None:
    try:
        for safety in reversed(safeties):
            _restore_zip(safety, dest)
    except (OSError, EditError) as exc:
        raise EditError("Undo could not finish, and the backup could not be written back.", code=4) from exc


def _save_timestamp(path: Path) -> int | None:
    try:
        payload = unpack_save(path.read_bytes())
        parsed = loads_save(payload.json_text)
    except (OSError, SaveFormatError, json.JSONDecodeError):
        return None
    found = find_value(parsed, "b@r")
    if found is None or isinstance(found[1], bool) or not isinstance(found[1], (int, float)):
        return None
    return int(found[1])


def _newer_than_copy(dest: Path, source: Path) -> str | None:
    if dest.stat().st_mtime_ns > source.stat().st_mtime_ns:
        return f"{dest.name} is newer than the copy. Install will not overwrite it."
    if dest.name.startswith("mf_"):
        return None
    dest_stamp = _save_timestamp(dest)
    source_stamp = _save_timestamp(source)
    if dest_stamp is not None and source_stamp is not None and dest_stamp > source_stamp:
        return f"{dest.name} has a later timestamp than the copy. Install will not overwrite it."
    return None


def _backup_install_targets(
    request: EditRequest,
    dest: Path,
    sources: list[tuple[Path, Path]],
    slot: int,
) -> list[Path]:
    backups: list[Path] = []
    directory = request.backup_dir or default_backup_dir(slot)
    for save, _manifest in sources:
        target = dest / save.name
        if not target.is_file():
            continue
        snap = take_snapshot(target)
        backup = write_backup(directory, snap, Plan(summary=f"Before install of {save.name}."), None)
        backups.append(backup)
        _say(request, f"Backup: {backup}")
    return backups


def _install_mismatch(save: Path, manifest: Path, dest: Path) -> str | None:
    dest_save = dest / save.name
    dest_manifest = dest / manifest.name
    if dest_save.read_bytes() != save.read_bytes() or dest_manifest.read_bytes() != manifest.read_bytes():
        return f"{save.name} failed its check after install. The backup was written back."
    try:
        raw = unpack_save(dest_save.read_bytes()).raw
        written = read_manifest(dest_manifest)
    except (OSError, SaveFormatError, ManifestError):
        return f"{save.name} failed its check after install. The backup was written back."
    if written.decompressed_size != len(raw):
        return f"{save.name} failed its check after install. The backup was written back."
    return None


def _rollback_install(backups: list[Path], dest: Path, sources: list[tuple[Path, Path]]) -> None:
    try:
        if backups:
            for backup in reversed(backups):
                _restore_zip(backup, dest)
            return
        for save, manifest in sources:
            (dest / save.name).unlink(missing_ok=True)
            (dest / manifest.name).unlink(missing_ok=True)
    except (OSError, EditError) as exc:
        raise EditError(
            "Install failed its check, and the backup could not be written back.",
            code=4,
        ) from exc


def _gamedata_report(request: EditRequest, refresh: bool) -> str:
    lines = [
        "Extracted mission tables are not shipped with this tool.",
        'hgpaktool extract --input "<PCBANKS>" --output "<game-files>"',
        'MBINCompiler "<game-files>"',
    ]
    if refresh:
        ran = False
        for program in ("hgpaktool", "MBINCompiler"):
            if shutil.which(program):
                lines.append(f"{program} is on PATH. This environment does not run it against game files.")
                ran = True
        if not ran:
            lines.append("hgpaktool and MBINCompiler are not on PATH here, so refresh only printed the commands.")
    if request.game_files:
        tables = load_tables(Path(request.game_files), Path(request.pcbanks) if request.pcbanks else None)
        lines.append(f"Missions with a final stage: {len(tables.missions)}.")
        lines.append(f"Highest final version in the tables: {tables.highest_version()}.")
        lines.append(f"Products: {len(tables.products)}. Substances: {len(tables.substances)}.")
        if tables.stale:
            lines.append(tables.warning)
        elif tables.warning:
            lines.append(tables.warning)
        else:
            lines.append("The extracted tables are not older than the paks that were checked.")
    else:
        lines.append("No extracted folder was given. In the window, click Read my game files.")
    return "\n".join(lines)


class EditorSession:
    """Holds one save open for the window. Preview never writes. Write uses the last preview."""

    def __init__(
        self,
        save: Path,
        game_files: Path | None = None,
        pcbanks: Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self.save = Path(save)
        self.game_files = game_files
        self.pcbanks = pcbanks
        self.backup_dir = backup_dir
        self.version_note = ""
        self.live = is_live_nms_save(self.save)
        self.rewards_on = True
        self.flag_only = True
        self.confirm_slot: int | None = None
        self._last: tuple[EditRequest, Snapshot, Plan, GameTables | None] | None = None
        self._tables_cache: GameTables | None = None
        self._tables_stamp: tuple | None = None
        self._tables_lock = threading.Lock()
        self._loader: threading.Thread | None = None
        self._table_error: Exception | None = None
        self._table_progress = ""
        self._names_report = None
        self._name_progress = ""
        self._name_error: Exception | None = None
        self._name_loader: threading.Thread | None = None
        self._table_events: queue.Queue = queue.Queue()
        self.origin: Path | None = None
        self.copy_dir: Path | None = None
        self.slot: int | None = None
        self.account: str = ""
        self.mapping: dict = {}
        self.copy_root: Path | None = None
        self.on_save_path = None
        self._snap: Snapshot | None = None
        self._snap_key: tuple | None = None
        self._reasons: dict[tuple, str] = {}
        self._snap_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._owns_window = False
        self._snap_gen = 0
        self._warm: threading.Thread | None = None
        self._warm_gen = 0
        self._warm_error: Exception | None = None

    def _save_key(self) -> tuple:
        path = Path(self.save).resolve()
        st = path.stat()
        return (str(path), st.st_size, st.st_mtime_ns)

    def invalidate_snapshot(self) -> None:
        """Drop the parsed save after Write, Undo, Restore, or Change save.

        Undo puts the old mtime back, so size and mtime can match a snapshot
        that is no longer the file on disk.
        """
        with self._snap_lock:
            self._snap = None
            self._snap_key = None
            self._reasons = {}
            self._snap_gen += 1

    def peek_snapshot(self) -> Snapshot | None:
        """The snapshot already in memory, or None. This does not read the file."""
        with self._snap_lock:
            if self._snap is not None and self._snap_key == self._save_key():
                return self._snap
        return None

    def cached_snapshot(self) -> Snapshot:
        """Parsed save for this path, reused until the file's size or mtime changes.

        The window thread never reads the file. A miss there means the worker
        has not installed the snapshot yet.
        """
        with timed("snapshot-cache"):
            ready = self.peek_snapshot()
            if ready is not None:
                return ready
            if self._owns_window and threading.current_thread() is threading.main_thread():
                raise EditError("The save is still loading.", code=2)
            with self._snap_lock:
                generation = self._snap_gen
            with timed("snapshot-load"):
                snap = take_snapshot(self.save)
            with self._snap_lock:
                if generation != self._snap_gen:
                    return snap
                self._snap = snap
                self._snap_key = self._save_key()
                self._reasons = {}
            return snap

    def ensure_snapshot(self) -> Snapshot:
        """Parse the save on this thread and store it. The window thread must not call this."""
        ready = self.peek_snapshot()
        if ready is not None:
            return ready
        if threading.current_thread() is threading.main_thread():
            raise EditError("The save is still loading.", code=2)
        with self._load_lock:
            ready = self.peek_snapshot()
            if ready is not None:
                return ready
            with self._snap_lock:
                generation = self._snap_gen
            path = Path(self.save)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            from nmsmissions.saveio import _POOL_SAVE_BYTES

            if size >= _POOL_SAVE_BYTES:
                from nmsmissions.gamedata import call_in_pool

                snap = call_in_pool(_snapshot_job, (str(path.resolve()),))
            else:
                snap = take_snapshot(path)
            with self._snap_lock:
                if generation != self._snap_gen:
                    return snap
                self._snap = snap
                self._snap_key = self._save_key()
                self._reasons = {}
            _partner_problem(snap)
            return snap

    def working_snapshot(self) -> Snapshot:
        return fork_snapshot(self.cached_snapshot())

    def start_snapshot_warm(self) -> None:
        """Read the save off the window thread so the first click does not parse it."""
        if self._warm is not None and self._warm.is_alive() and self._warm_gen == self._snap_gen:
            return
        self._warm_error = None
        generation = self._snap_gen

        def work() -> None:
            try:
                if generation != self._snap_gen:
                    return
                self.ensure_snapshot()
            except Exception as exc:
                if generation == self._snap_gen:
                    self._warm_error = exc

        self._warm_gen = generation
        self._warm = threading.Thread(target=work, name="nms-snapshot", daemon=True)
        self._warm.start()

    def wait_snapshot_warm(self) -> None:
        warm = self._warm
        if warm is not None and warm.is_alive():
            warm.join()
        error = self._warm_error
        self._warm_error = None
        if error is not None:
            raise error

    def warm_snapshot(self) -> None:
        self.start_snapshot_warm()
        self.wait_snapshot_warm()

    def drop_game_files(self) -> None:
        """Forget the copied game files after the cache is cleared."""
        self.game_files = None
        self._names_report = None
        self._name_error = None
        self._name_progress = ""
        self._name_loader = None
        self._tables_cache = None
        self._tables_stamp = None
        self._table_error = None
        self._table_progress = ""
        self._loader = None

    def adopt_game_files(self, path: Path) -> None:
        """Point this session at a freshly read cache and load names again."""
        self.game_files = Path(path)
        self._names_report = None
        self._name_error = None
        self._name_progress = ""
        self._name_loader = None
        self._tables_cache = None
        self._tables_stamp = None
        self._table_error = None
        self._table_progress = ""
        self._loader = None
        while True:
            try:
                self._table_events.get_nowait()
            except queue.Empty:
                break

    def tables_cached(self) -> bool:
        if self.game_files is None:
            return True
        return self._tables_cache is not None and self._tables_stamp == _source_stamp(self.game_files, self.pcbanks)

    def cached_tables(self) -> GameTables | None:
        return self._tables_cache

    def adopt_cached_assets(self) -> None:
        """Install name and table caches before the first paint.

        A miss returns without parsing XML. The background loaders do that work.
        """
        if self.game_files is None:
            return
        if self._names_report is None:
            from nmsmissions.gamenames import peek_game_names

            report = peek_game_names(self.game_files)
            if report is not None:
                self._names_report = report
        if self.tables_cached():
            return
        from nmsmissions.gamedata import cache_path_for_tables, peek_tables

        tables = peek_tables(self.game_files, self.pcbanks, cache_path_for_tables(self.game_files))
        if tables is None:
            return
        with self._tables_lock:
            self._tables_cache = tables
            self._tables_stamp = _source_stamp(self.game_files, self.pcbanks)

    def start_name_load(self) -> None:
        """Read mission names off the window thread. A warm cache returns at once."""
        if self.game_files is None or self._names_report is not None:
            return
        if self._name_loader is not None and self._name_loader.is_alive():
            return
        root = self.game_files

        def work() -> None:
            from nmsmissions.gamenames import load_game_names

            def progress(message: str) -> None:
                self._name_progress = message

            try:
                started = time.perf_counter()
                self._names_report = load_game_names(root, progress=progress)
                from nmsmissions.timing import record

                record("post-read-cache", (time.perf_counter() - started) * 1000.0)
            except Exception as exc:
                self._name_error = exc

        self._name_loader = threading.Thread(target=work, name="nms-mission-names", daemon=True)
        self._name_loader.start()

    def start_table_load(self) -> None:
        """Parse extracted tables on a background thread so the first preview does not sit silent."""
        if self.game_files is None or self.tables_cached():
            return
        if self._loader is not None and self._loader.is_alive():
            return
        request = EditRequest(action="finish", game_files=self.game_files, pcbanks=self.pcbanks)

        def work() -> None:
            def progress(message: str) -> None:
                self._table_progress = message
                self._table_events.put(message)

            try:
                self._cached_tables(request, progress=progress)
            except Exception as exc:
                self._table_error = exc

        self._loader = threading.Thread(target=work, name="nms-mission-tables", daemon=True)
        self._loader.start()

    def wait_for_tables(self) -> None:
        if self._loader is not None and self._loader.is_alive():
            self._loader.join()
        if self.game_files is not None and not self.tables_cached():
            self._cached_tables(EditRequest(action="finish", game_files=self.game_files, pcbanks=self.pcbanks))

    def line_needs_unlocks(self, chain_id: str) -> bool:
        """True when this line is missing an unlock. Uses the snapshot already in memory."""
        with self._snap_lock:
            snap = self._snap
            current = self._snap_key == self._save_key() if snap is not None else False
        if snap is None or not current:
            return False
        return chain_unlocks_pending(snap.player, self._tables_cache, chain_id)

    def unlock_reason(self, mission_id: str | None, *, load: bool = True) -> str:
        """Plain tooltip when Unlock only should stay off. Empty when it can run.

        load=False reads the snapshot the worker already stored. It does not
        open the save. The window's selection refresh uses that.
        """
        if not mission_id:
            return "Select a mission first."
        with timed("unlock-reason"):
            if load:
                snap = self.cached_snapshot()
            else:
                snap = self.peek_snapshot()
                if snap is None:
                    return "The save is still loading."
            tables = self._tables_cache if self.game_files is not None and self.tables_cached() else None
            version = _version(snap.player)
            key = (id(snap), str(mission_id), id(tables) if tables is not None else 0, version)
            if key in self._reasons:
                return self._reasons[key]
            reason = unlock_block_reason(snap.player, tables, mission_id, version)
            self._reasons[key] = reason
            return reason

    def plan_for(
        self,
        action: str,
        mission: str | None,
        chain: str | None,
        flag_only: bool,
        whole_line: bool = False,
        *,
        station_race: str | None = None,
        station_guild: str | None = None,
    ) -> Plan:
        request = EditRequest(
            action="purple" if action == "purple" else action,
            save=self.save,
            mission=None if whole_line else mission,
            chain=chain,
            target=None if whole_line else mission,
            whole_line=whole_line,
            flag_only=flag_only,
            apply=False,
            live=self.live,
            confirm_slot=self.confirm_slot,
            backup_dir=self.backup_dir,
            game_files=self.game_files,
            pcbanks=self.pcbanks,
            rewards=self.rewards_on,
            station_race=station_race,
            station_guild=station_guild,
        )
        with timed("plan-for"):
            snap = self.working_snapshot()
            # Station standing does not use mission tables. Joining that loader
            # here would freeze the window until the read finishes.
            tables = None if request.action == "station" else self._cached_tables(request)
            plan = build_plan(request, snap, tables)
            plan.checks = collect_checks(request, snap, tables, running=preview_running(), quick=True)
            _note_backup(request, snap, plan)
            self._last = (request, snap, plan, tables)
            return plan

    def _cached_tables(self, request: EditRequest, progress=None) -> GameTables | None:
        if request.game_files is None:
            return None
        stamp = _source_stamp(self.game_files, self.pcbanks)
        with self._tables_lock:
            if self._tables_cache is not None and self._tables_stamp == stamp:
                return self._tables_cache
        loader = self._loader
        if loader is not None and loader.is_alive() and threading.current_thread() is not loader:
            loader.join()
            with self._tables_lock:
                if self._tables_cache is not None and self._tables_stamp == stamp:
                    return self._tables_cache
        tables = _tables(request, progress=progress)
        with self._tables_lock:
            self._tables_cache = tables
            self._tables_stamp = stamp
        return tables

    def apply_last(self, running: bool | None = None) -> tuple[int, str]:
        if self._last is None:
            raise EditError("Preview the change before writing.", code=2)
        request, snap, plan, tables = self._last
        request.apply = True
        request.live = self.live
        request.confirm_slot = self.confirm_slot
        plan.checks = collect_checks(request, snap, tables, running=running)
        if plan.failing():
            return 2, format_plan(plan, dry_run=True)
        code, backup = commit(request, snap, plan, tables, running=running)
        if code in (0, 4):
            self.invalidate_snapshot()
        if code == 0 and backup is not None:
            return 0, f"Backup: {backup}"
        if code == 4:
            return 4, "The new file failed its check. The backup was written back."
        return code, "Nothing was written."


def manifest_verify_note(save: Path, payload_len: int) -> tuple[bool, str]:
    """Extra verify lines. A missing mf_ file does not fail version-1 verify."""
    path = manifest_path_for(save)
    if not path.is_file():
        return True, "manifest: no mf_ file"
    try:
        manifest = read_manifest(path)
    except ManifestError as exc:
        return False, f"manifest: {exc}"
    problems = []
    ok = True
    if not manifest.format_ok:
        ok = False
        problems.append("manifest: format is not 2004")
    if manifest.decompressed_size != payload_len:
        ok = False
        problems.append("manifest: decompressed size does not match the save")
    if manifest.compressed_size != save.stat().st_size:
        problems.append("manifest: compressed size does not match the file (warning)")
    if not problems:
        problems.append("manifest: size fields match")
    return ok, "\n".join(problems)

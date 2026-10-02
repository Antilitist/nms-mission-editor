"""Find No Man's Sky save slots and keep a working copy next to the app.

The game folder is left alone until the user puts a copy back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from nmsmissions.chunks import SaveFormatError, unpack_save
from nmsmissions.manifest import manifest_path_for
from nmsmissions.saveio import deobfuscate
from nmsmissions.timing import timed

_SAVE_NAME = re.compile(r"^save(\d*)\.hg$", re.IGNORECASE)
_ZIP_STAMP = re.compile(r"\.(\d{8})-(\d{6})(?:-(\d+))?")


def app_root() -> Path:
    """Folder that holds run_gui.pyw."""
    return Path(__file__).resolve().parents[1]


def working_root(root: Path | None = None) -> Path:
    return (root or app_root()) / "working_copies"


def nms_save_folders(env: dict[str, str] | None = None) -> list[Path]:
    """%APPDATA%\\HelloGames\\NMS\\st_* folders. NMS_SAVE_DIR overrides that for tests."""
    values = os.environ if env is None else env
    override = values.get("NMS_SAVE_DIR") or values.get("NMS_TEST_SAVEDIR")
    if override:
        path = Path(override)
        if not path.is_dir():
            return []
        accounts = sorted(child for child in path.glob("st_*") if child.is_dir())
        return accounts or [path]
    appdata = values.get("APPDATA")
    if not appdata:
        return []
    root = Path(appdata) / "HelloGames" / "NMS"
    if not root.is_dir():
        return []
    return sorted(child for child in root.glob("st_*") if child.is_dir())


def slot_of(name: str) -> int | None:
    """saveN.hg uses (N+1)//2. save.hg and save2.hg are slot 1."""
    match = _SAVE_NAME.match(name)
    if match is None:
        return None
    raw = match.group(1)
    if raw == "":
        return 1
    number = int(raw)
    if number < 1:
        return None
    return (number + 1) // 2


@dataclass
class SlotGroup:
    slot: int
    account: str
    folder: Path
    files: list[Path] = field(default_factory=list)

    @property
    def newer(self) -> Path:
        newest = max(path.stat().st_mtime_ns for path in self.files)
        tied = [path for path in self.files if path.stat().st_mtime_ns == newest]
        if len(tied) == 1:
            return tied[0]
        even = [path for path in tied if _even_save(path.name)]
        return even[-1] if even else tied[-1]

    def newer_note(self) -> str:
        if len(self.files) == 1:
            return f"only {self.files[0].name}"
        names = ", ".join(path.name for path in self.files)
        return f"{names}; {self.newer.name} is newer"


def _even_save(name: str) -> bool:
    match = _SAVE_NAME.match(name)
    if match is None or match.group(1) == "":
        return False
    return int(match.group(1)) % 2 == 0


def group_slots(folder: Path) -> list[SlotGroup]:
    """One row per slot. Manifest files are not rows."""
    if not folder.is_dir():
        return []
    found: dict[int, list[Path]] = {}
    for path in folder.iterdir():
        if not path.is_file():
            continue
        slot = slot_of(path.name)
        if slot is None:
            continue
        found.setdefault(slot, []).append(path)
    groups = []
    for slot, files in sorted(found.items()):
        groups.append(SlotGroup(slot=slot, account=folder.name, folder=folder, files=sorted(files, key=lambda item: item.name.lower())))
    return groups


def play_text(value: int | None) -> str:
    if value is None:
        return "—"
    hours = value / 3_600_000 if value > 10_000_000 else value / 3600
    return f"{hours:.1f} h"


def saved_text(mtime_ns: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime_ns / 1_000_000_000))


def summarize_player(data: dict) -> tuple[str, int | None]:
    """Game mode and TotalPlayTime when those names are readable. Otherwise a dash."""
    mode = _mode_text(data)
    return mode or "—", _play_time(data)


def read_summary(path: Path, mapping: dict[str, str] | None = None) -> tuple[str, int | None]:
    try:
        payload = unpack_save(path.read_bytes())
        parsed = json.loads(payload.json_text)
    except (OSError, SaveFormatError, json.JSONDecodeError, UnicodeError):
        return "—", None
    if not isinstance(parsed, dict):
        return "—", None
    if mapping:
        parsed = deobfuscate(parsed, mapping)
    if not isinstance(parsed, dict):
        return "—", None
    return summarize_player(parsed)


def _mode_text(data: dict) -> str:
    preset = _first_string(data, "PresetGameMode")
    game = _first_string(data, "GameMode")
    difficulty = _first_string(data, "DifficultyPreset") or _first_string(data, "ActivePreset")
    mode = preset or game
    if mode and difficulty and difficulty != mode:
        return f"{mode} / {difficulty}"
    return mode or difficulty or ""


def _first_string(data: dict, key: str) -> str:
    for node in _dicts(data):
        if key not in node:
            continue
        text = _plain(node.get(key))
        if text:
            return text
    return ""


def _plain(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text and not text.startswith("Gc") and not text.endswith(".xml"):
            return text
        return ""
    if isinstance(value, dict):
        for inner in value.values():
            text = _plain(inner)
            if text:
                return text
    return ""


def _play_time(data: dict) -> int | None:
    for node in _dicts(data):
        if "TotalPlayTime" not in node:
            continue
        value = node.get("TotalPlayTime")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(round(value)) if isinstance(value, float) else int(value)
    return None


def _dicts(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _dicts(item)


def slot_row(group: SlotGroup, mode: str, play: int | None) -> str:
    when = saved_text(group.newer.stat().st_mtime_ns)
    account = f"{group.account}  " if group.account.startswith("st_") else ""
    return f"{account}Slot {group.slot}   {mode}   {play_text(play)}   {when}   {group.newer_note()}"


@dataclass
class SyncState:
    source: str
    source_sha256: str
    source_mtime_ns: int
    synced_sha256: str
    save_name: str
    slot: int
    account: str
    ignored_live_sha: str = ""
    undo_zip: str = ""
    installed_sha256: str = ""
    installed_at_ns: int = 0
    installed_source: str = ""
    partner_zips: dict[str, str] = field(default_factory=dict)


def copy_dir_for(root: Path, account: str, slot: int) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in account) or "account"
    return root / f"{safe}-slot{slot}"


def _state_path(copy_dir: Path) -> Path:
    return copy_dir / "state.json"


def load_state(copy_dir: Path) -> SyncState | None:
    path = _state_path(copy_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "source_sha256" not in raw:
        return None
    return SyncState(
        source=str(raw.get("source") or ""),
        source_sha256=str(raw.get("source_sha256") or ""),
        source_mtime_ns=int(raw.get("source_mtime_ns") or 0),
        synced_sha256=str(raw.get("synced_sha256") or ""),
        save_name=str(raw.get("save_name") or ""),
        slot=int(raw.get("slot") or 0),
        account=str(raw.get("account") or ""),
        ignored_live_sha=str(raw.get("ignored_live_sha") or ""),
        undo_zip=str(raw.get("undo_zip") or ""),
        installed_sha256=str(raw.get("installed_sha256") or ""),
        installed_at_ns=int(raw.get("installed_at_ns") or 0),
        installed_source=str(raw.get("installed_source") or ""),
        partner_zips=_partner_zips(raw.get("partner_zips")),
    )


def save_state(copy_dir: Path, state: SyncState) -> None:
    copy_dir.mkdir(parents=True, exist_ok=True)
    _state_path(copy_dir).write_text(
        json.dumps(
            {
                "source": state.source,
                "source_sha256": state.source_sha256,
                "source_mtime_ns": state.source_mtime_ns,
                "synced_sha256": state.synced_sha256,
                "save_name": state.save_name,
                "slot": state.slot,
                "account": state.account,
                "ignored_live_sha": state.ignored_live_sha,
                "undo_zip": state.undo_zip,
                "installed_sha256": state.installed_sha256,
                "installed_at_ns": state.installed_at_ns,
                "installed_source": state.installed_source,
                "partner_zips": state.partner_zips,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decide_sync(
    *,
    live_sha: str,
    live_mtime_ns: int,
    copy_exists: bool,
    copy_sha: str,
    state: SyncState | None,
) -> str:
    """copy, keep, refresh, or ask.

    Refresh only when the live file is newer and the copy has no edits.
    Ask when both the live file and the copy changed.
    """
    if not copy_exists or state is None:
        return "copy"
    if live_sha == state.ignored_live_sha and copy_sha != state.synced_sha256:
        return "keep"
    live_changed = live_sha != state.source_sha256
    copy_edited = copy_sha != state.synced_sha256
    if not live_changed:
        return "keep"
    if copy_edited:
        return "ask"
    if live_mtime_ns > state.source_mtime_ns:
        return "refresh"
    return "ask"


_STATUS_CACHE: dict[tuple, str] = {}


def _status_token(path: Path | None) -> tuple:
    if path is None:
        return ("missing",)
    try:
        st = Path(path).stat()
    except OSError:
        return (str(path), "unreadable")
    return (str(Path(path).resolve()), st.st_size, st.st_mtime_ns)


def copy_status(copy_path: Path, origin: Path | None) -> str:
    """'matches game' when the bytes are the same, otherwise the edited line.

    Repeated checks reuse the answer while both files keep their size and mtime.
    """
    key = (_status_token(copy_path), _status_token(origin))
    cached = _STATUS_CACHE.get(key)
    if cached is not None:
        return cached
    with timed("copy-status"):
        if origin is None or not origin.is_file() or not copy_path.is_file():
            text = "This file was opened from a path."
        else:
            try:
                same = file_sha(copy_path) == file_sha(origin)
            except OSError:
                text = "Working copy: edited, not yet in game"
            else:
                text = "matches game" if same else "Working copy: edited, not yet in game"
    if len(_STATUS_CACHE) > 64:
        _STATUS_CACHE.clear()
    _STATUS_CACHE[key] = text
    return text


def partner_paths(path: Path) -> list[Path]:
    """The other save files in this slot. save.hg pairs with save2.hg and save1.hg."""
    name = path.name
    if name.lower() == "save.hg":
        return [path.with_name("save2.hg"), path.with_name("save1.hg")]
    match = re.search(r"save(\d+)\.hg$", name, re.IGNORECASE)
    if match is None:
        return []
    number = int(match.group(1))
    other = number + 1 if number % 2 else number - 1
    names = [path.with_name(f"save{other}.hg")]
    if number in {1, 2}:
        names.append(path.with_name("save.hg"))
    return names


def newer_partner(path: Path, than_ns: int) -> Path | None:
    """The newest other file of this slot whose mtime is after than_ns."""
    newer = []
    for candidate in partner_paths(path):
        if not candidate.is_file():
            continue
        try:
            stamp = candidate.stat().st_mtime_ns
        except OSError:
            continue
        if stamp > than_ns:
            newer.append((stamp, candidate))
    if not newer:
        return None
    return max(newer)[1]


def game_load_note(origin: Path | None) -> str | None:
    """Name the other file when the game would load it instead of this one."""
    if origin is None:
        return None
    origin = Path(origin)
    if not origin.is_file():
        return None
    try:
        stamp = origin.stat().st_mtime_ns
    except OSError:
        return None
    partner = newer_partner(origin, stamp)
    if partner is None:
        return None
    return f"The game will load {partner.name}."


def slot_status_line(copy_path: Path, origin: Path | None) -> str:
    """Byte match against the same-name file, plus the other file when it is newer.

    'matches game' only while the game will load this file. The background
    poll calls this, so a partner written later replaces that line.
    """
    base = copy_status(copy_path, origin)
    note = game_load_note(origin)
    if note is None:
        return base
    if base == "matches game":
        return note
    return base + "\n" + note


def already_in_game_note(copy_path: Path, origin: Path | None) -> str | None:
    """Put is skipped when the working copy is already the game file."""
    if copy_status(copy_path, origin) != "matches game":
        return None
    return f"{Path(copy_path).name} is already in the game."


@dataclass
class PrepareResult:
    action: str
    copy_path: Path
    origin: Path
    copy_dir: Path
    slot: int
    account: str
    message: str


def prepare_working_copy(
    live: Path,
    copy_dir: Path,
    *,
    slot: int,
    account: str,
    choice: str | None = None,
    backup_dir: Path | None = None,
) -> PrepareResult:
    """Copy the live save and its mf_ file, or keep the edited copy.

    Edits belong to the slot. A newer file of the same pair does not replace
    an edited copy until the user says to use the game save. That choice
    backs up the edited copy first.

    choice is 'game' or 'edits' when the caller already asked the user.
    """
    live = Path(live)
    copy_dir = Path(copy_dir)
    state = load_state(copy_dir)
    tracked = None
    if state is not None and state.save_name:
        named = copy_dir / state.save_name
        if named.is_file():
            tracked = named
    copy_path = tracked if tracked is not None else copy_dir / live.name
    live_sha = file_sha(live)
    live_mtime = live.stat().st_mtime_ns
    copy_exists = copy_path.is_file()
    copy_sha = file_sha(copy_path) if copy_exists else ""
    other_file = tracked is not None and state is not None and state.save_name != live.name
    if other_file and state is not None:
        copy_edited = copy_sha != state.synced_sha256
        if live_sha == state.ignored_live_sha and copy_edited:
            action = "keep"
        elif copy_edited:
            action = "ask"
        else:
            action = "copy"
    else:
        action = decide_sync(
            live_sha=live_sha,
            live_mtime_ns=live_mtime,
            copy_exists=copy_exists,
            copy_sha=copy_sha,
            state=state if state is not None and state.save_name == live.name else None,
        )
    if action == "ask" and choice == "game":
        action = "refresh"
    elif action == "ask" and choice == "edits":
        action = "keep"
        if state is None:
            state = SyncState(str(live), live_sha, live_mtime, copy_sha, live.name, slot, account)
        state.ignored_live_sha = live_sha
        save_state(copy_dir, state)
    backed: Path | None = None
    previous: Path | None = None
    replaced_zip: Path | None = None
    if action in {"copy", "refresh"}:
        if copy_exists and _copy_is_edited(state, copy_sha):
            backed = backup_edited_copy(copy_path, slot, backup_dir)
            previous = copy_path
        already = previous if previous is not None and previous.name.lower() == live.name.lower() else None
        replaced_zip = _copy_pair(
            live,
            copy_dir,
            slot=slot,
            backup_dir=backup_dir,
            already_backed=already,
        )
        copy_path = copy_dir / live.name
        copied = file_sha(copy_path)
        fresh = SyncState(
            source=str(live),
            source_sha256=live_sha,
            source_mtime_ns=live_mtime,
            synced_sha256=copied,
            save_name=live.name,
            slot=slot,
            account=account,
        )
        if state is not None:
            fresh.undo_zip = state.undo_zip
            fresh.installed_sha256 = state.installed_sha256
            fresh.installed_at_ns = state.installed_at_ns
            fresh.installed_source = state.installed_source
            fresh.partner_zips = dict(state.partner_zips)
        save_state(copy_dir, fresh)
        message = f"Copied {live.name} from slot {slot}."
        if backed is not None:
            message += f" Your edited copy was backed up: {backed.name}."
        if replaced_zip is not None:
            message += f" The old copy was backed up: {replaced_zip.name}."
    elif action == "ask":
        message = "The game save and your copy both changed."
    else:
        message = f"Keeping your copy of slot {slot}."
    if action != "ask":
        synced = state.synced_sha256 if state is not None else ""
        _forget_other_copies(
            copy_dir,
            copy_path.name,
            slot=slot,
            backup_dir=backup_dir,
            synced_sha=synced,
            already_backed=previous,
        )
    return PrepareResult(action, copy_path, live, copy_dir, slot, account, message)


def _copy_is_edited(state: SyncState | None, copy_sha: str) -> bool:
    if state is None:
        return True
    return copy_sha != state.synced_sha256


def backup_edited_copy(
    copy_path: Path,
    slot: int,
    backup_dir: Path | None = None,
    note: str | None = None,
) -> Path:
    """Zip a save before it is replaced. note is the line stored in the zip."""
    import zipfile

    from nmsmissions.edit import default_backup_dir

    directory = backup_dir or default_backup_dir(slot)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = directory / f"{copy_path.name}.{stamp}.zip"
    extra = 2
    while dest.exists():
        dest = directory / f"{copy_path.name}.{stamp}-{extra}.zip"
        extra += 1
    manifest = copy_path.with_name("mf_" + copy_path.name)
    info = {
        "save": copy_path.name,
        "manifest": manifest.name if manifest.is_file() else None,
        "save_mtime_ns": copy_path.stat().st_mtime_ns,
        "mf_mtime_ns": manifest.stat().st_mtime_ns if manifest.is_file() else None,
        "note": note or "Working copy backed up before it was replaced.",
    }
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        _zip_bytes(handle, copy_path.name, copy_path.read_bytes())
        if manifest.is_file():
            _zip_bytes(handle, manifest.name, manifest.read_bytes())
        handle.writestr("saveinfo.json", json.dumps(info, indent=2))
    return dest


def _zip_bytes(handle, name: str, data: bytes) -> None:
    """Zip entry with a fixed date. A save from before 1980 still packs."""
    import zipfile

    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    handle.writestr(info, data)


def _partner_zips(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def note_installed(
    copy_dir: Path,
    live: Path,
    copy_path: Path,
    undo_zip: Path | None = None,
    backup_dir: Path | None = None,
) -> None:
    """After a successful install the copy matches the game.

    undo_zip is the backup of the live file from just before that install.
    The other file of the slot is zipped too, so a later undo can put it back.
    """
    from nmsmissions.edit import _partner_paths

    state = load_state(copy_dir) or SyncState(
        source=str(live),
        source_sha256="",
        source_mtime_ns=0,
        synced_sha256="",
        save_name=copy_path.name,
        slot=0,
        account="",
    )
    state.source = str(live)
    state.source_sha256 = file_sha(live)
    state.source_mtime_ns = live.stat().st_mtime_ns
    state.synced_sha256 = file_sha(copy_path)
    state.save_name = copy_path.name
    state.ignored_live_sha = ""
    state.installed_sha256 = file_sha(live)
    state.undo_zip = str(undo_zip) if undo_zip else ""
    state.installed_at_ns = time.time_ns()
    state.installed_source = str(Path(live).resolve())
    partners: dict[str, str] = {}
    for partner in _partner_paths(Path(live)):
        if not partner.is_file() or partner.resolve() == Path(live).resolve():
            continue
        zipped = backup_edited_copy(
            partner,
            state.slot or 0,
            backup_dir,
            note="Game file backed up at put time, so Undo can put it back.",
        )
        partners[partner.name] = str(zipped)
    state.partner_zips = partners
    save_state(copy_dir, state)
    _forget_other_copies(
        copy_dir,
        copy_path.name,
        slot=state.slot,
        backup_dir=backup_dir,
        synced_sha=state.synced_sha256,
    )


def filter_backups(items: list[BackupInfo], slot: int | None, show_all: bool) -> list[BackupInfo]:
    """Backups for the open slot. show_all keeps every zip."""
    if show_all or slot is None:
        return list(items)
    return [item for item in items if item.slot == slot]


def restore_slot_warning(
    backup_slot: int | None,
    open_slot: int | None,
    when_text: str = "",
    save_name: str = "",
) -> str | None:
    """Plain warning when a backup is not for the slot that is open."""
    if open_slot is None or backup_slot == open_slot:
        return None
    shown = str(backup_slot) if backup_slot is not None else "unknown"
    named = " ".join(part for part in (when_text, save_name) if part)
    detail = f" ({named})" if named else ""
    return (
        f"This backup is slot {shown}{detail}. This window is slot {open_slot}. "
        "Restoring it replaces this copy with the other slot's file. The game save is not changed."
    )


def _copy_pair(
    live: Path,
    copy_dir: Path,
    *,
    slot: int = 0,
    backup_dir: Path | None = None,
    already_backed: Path | None = None,
) -> Path | None:
    """Copy a game save into the working folder.

    An existing copy of that same name is zipped first when its bytes differ
    from the game file. The archive step runs too late to save that file.
    """
    copy_dir.mkdir(parents=True, exist_ok=True)
    existing = copy_dir / live.name
    backed: Path | None = None
    if existing.is_file():
        try:
            differs = file_sha(existing) != file_sha(live)
        except OSError:
            differs = True
        skipped = (
            already_backed is not None
            and already_backed.exists()
            and existing.resolve() == already_backed.resolve()
        )
        if differs and not skipped:
            backed = backup_edited_copy(
                existing,
                slot or slot_of(live.name) or 0,
                backup_dir,
                note="Working copy backed up before the game file replaced it.",
            )
    shutil.copy2(live, existing)
    manifest = manifest_path_for(live)
    if manifest.is_file():
        shutil.copy2(manifest, copy_dir / manifest.name)
    return backed


def _save_copies(copy_dir: Path) -> list[Path]:
    """Save files in a working-copy folder. Manifests are not included."""
    if not copy_dir.is_dir():
        return []
    return [path for path in copy_dir.iterdir() if path.is_file() and slot_of(path.name) is not None]


def _forget_other_copies(
    copy_dir: Path,
    keep_name: str,
    *,
    slot: int,
    backup_dir: Path | None,
    synced_sha: str = "",
    already_backed: Path | None = None,
) -> None:
    """One tracked save per slot. A different file's copy is archived, then removed."""
    keep = copy_dir / keep_name
    keep_sha = file_sha(keep) if keep.is_file() else ""
    backed = already_backed.resolve() if already_backed is not None and already_backed.exists() else None
    for path in _save_copies(copy_dir):
        if path.name.lower() == keep_name.lower():
            continue
        if backed is None or path.resolve() != backed:
            try:
                sha = file_sha(path)
            except OSError:
                sha = ""
            if sha and sha != keep_sha and sha != synced_sha:
                backup_edited_copy(
                    path,
                    slot,
                    backup_dir,
                    note="Other file's copy archived because this slot now tracks one file.",
                )
        manifest = path.with_name("mf_" + path.name)
        path.unlink(missing_ok=True)
        if manifest.is_file():
            manifest.unlink()


def live_file_for_state(state: SyncState) -> Path | None:
    """The newest live file of this slot. The game loads that file."""
    folder = None
    anchor = None
    for recorded in (state.source, state.installed_source):
        if not recorded:
            continue
        path = Path(recorded)
        if path.parent.is_dir():
            folder = path.parent
            anchor = path
            break
    if folder is None:
        return None
    wanted = state.slot or (slot_of(anchor.name) if anchor is not None else None)
    for group in group_slots(folder):
        if group.slot == wanted and group.files:
            return group.newer
    if anchor is not None and anchor.is_file():
        return anchor
    return None


def align_working_copy_to_game(copy_dir: Path, backup_dir: Path | None = None) -> Path | None:
    """Make the working copy match the newest live file.

    A copy whose bytes differ is backed up first. Other save files in the
    copy folder are removed, so a stale edit cannot look newer than the game.
    """
    state = load_state(copy_dir)
    if state is None:
        return None
    live = live_file_for_state(state)
    if live is None or not live.is_file():
        return None
    live_sha = file_sha(live)
    slot = state.slot or slot_of(live.name) or 0
    for path in _save_copies(copy_dir):
        if path.name.lower() == live.name.lower():
            continue
        try:
            sha = file_sha(path)
        except OSError:
            continue
        if sha != live_sha:
            backup_edited_copy(
                path,
                slot,
                backup_dir,
                note="Working copy backed up before it was replaced with the game file.",
            )
    _copy_pair(live, copy_dir, slot=slot, backup_dir=backup_dir)
    for path in _save_copies(copy_dir):
        if path.name.lower() == live.name.lower():
            continue
        manifest = path.with_name("mf_" + path.name)
        path.unlink(missing_ok=True)
        if manifest.is_file():
            manifest.unlink()
    copied = copy_dir / live.name
    state.source = str(live.resolve())
    state.source_sha256 = live_sha
    state.source_mtime_ns = live.stat().st_mtime_ns
    state.synced_sha256 = file_sha(copied)
    state.save_name = live.name
    state.ignored_live_sha = ""
    save_state(copy_dir, state)
    return copied


def align_copies_for_live(live_dir: Path, slot: int, backup_dir: Path | None = None) -> None:
    """After a restore into the game folder, refresh every working copy of that slot."""
    root = working_root()
    if not root.is_dir():
        return
    live_dir = Path(live_dir).resolve()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        state = load_state(child)
        if state is None or state.slot != slot:
            continue
        source = Path(state.source) if state.source else None
        installed = Path(state.installed_source) if state.installed_source else None
        parents = [path.parent for path in (source, installed) if path is not None]
        same_folder = any(parent.is_dir() and parent.resolve() == live_dir for parent in parents)
        if not same_folder and state.account != live_dir.name:
            continue
        if not same_folder:
            for group in group_slots(live_dir):
                if group.slot == slot and group.files:
                    state.source = str(group.newer)
                    save_state(child, state)
                    break
        align_working_copy_to_game(child, backup_dir)


def retain_tracked_save(copy_dir: Path, keep_name: str, backup_dir: Path | None = None) -> None:
    """Drop every save in the copy folder except keep_name. Does not mark it as matching the game."""
    state = load_state(copy_dir)
    synced = state.synced_sha256 if state is not None else ""
    slot = state.slot if state is not None and state.slot else (slot_of(keep_name) or 0)
    _forget_other_copies(copy_dir, keep_name, slot=slot, backup_dir=backup_dir, synced_sha=synced)
    if state is not None and state.save_name != keep_name:
        state.save_name = keep_name
        save_state(copy_dir, state)


def point_session_at_state(session) -> None:
    """Point an open window at the working copy the state file now tracks."""
    copy_dir = getattr(session, "copy_dir", None)
    if copy_dir is None:
        return
    state = load_state(copy_dir)
    if state is None or not state.save_name:
        return
    refreshed = Path(copy_dir) / state.save_name
    if not refreshed.is_file():
        return
    session.save = refreshed
    if state.source:
        session.origin = Path(state.source)
    hook = getattr(session, "on_save_path", None)
    if hook is not None:
        hook(refreshed)


def restored_file_name(
    backup_name: str,
    open_name: str,
    backup_slot: int | None,
    open_slot: int | None,
) -> str:
    """The file a restore replaced. A cross-slot restore writes the open file."""
    if open_slot is not None and backup_slot is not None and backup_slot != open_slot:
        return open_name
    return backup_name


def remember_slot(root: Path, account: str, slot: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "last.json").write_text(
        json.dumps({"account": account, "slot": slot}),
        encoding="utf-8",
    )


def remembered_slot(root: Path) -> tuple[str, int] | None:
    path = root / "last.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "slot" not in raw:
        return None
    try:
        return str(raw.get("account") or ""), int(raw["slot"])
    except (TypeError, ValueError):
        return None


@dataclass
class BackupInfo:
    path: Path
    when_text: str
    slot: int | None
    save_name: str

    def label(self) -> str:
        slot = f"slot {self.slot}" if self.slot is not None else "slot unknown"
        return f"{self.when_text}   {slot}   {self.save_name}"


def list_backups(directory: Path) -> list[BackupInfo]:
    """Backup zips, newest first, with the date in the name and the slot inside."""
    if not directory.is_dir():
        return []
    found: list[BackupInfo] = []
    for path in directory.glob("*.zip"):
        if not path.is_file():
            continue
        save_name, slot = _zip_identity(path)
        found.append(BackupInfo(path, _zip_when(path), slot, save_name))
    found.sort(key=lambda item: item.path.stat().st_mtime_ns, reverse=True)
    return found


def _zip_when(path: Path) -> str:
    match = _ZIP_STAMP.search(path.name)
    if match:
        day, clock = match.group(1), match.group(2)
        text = f"{day[0:4]}-{day[4:6]}-{day[6:8]} {clock[0:2]}:{clock[2:4]}:{clock[4:6]}"
        if match.group(3):
            text += f" ({match.group(3)})"
        return text
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime_ns / 1_000_000_000))


def _zip_identity(path: Path) -> tuple[str, int | None]:
    save_name = path.name.split(".hg")[0] + ".hg" if ".hg" in path.name.lower() else path.name
    slot = slot_of(save_name)
    try:
        import zipfile

        with zipfile.ZipFile(path) as handle:
            info = json.loads(handle.read("saveinfo.json"))
        if isinstance(info, dict) and info.get("save"):
            save_name = str(info["save"])
            slot = slot_of(save_name)
    except (OSError, KeyError, json.JSONDecodeError, ValueError):
        pass
    return save_name, slot

"""Mission log titles from the player's own extracted game files.

Language files and mission tables are not shipped with this tool. Read my
game files copies every NMS_*_ENGLISH table and the mission title and
description keys into the cache. Binary MBIN is reported and skipped.

Parsed names are cached under ~/.cache/nms_mission_editor/ (or
NMSMISSIONS_CACHE_DIR) and reused when every source file still has the same
path, size, and mtime. A newer tool build keeps that cache. Bump CACHE_VERSION
when the parsed names change.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from nmsmissions import __version__, cache_root
from nmsmissions.chains import is_class_name, is_generated_title
from nmsmissions.timing import record, timed

_LOC_ID = re.compile(r"^[A-Z0-9_]+$")
_TRANSLATOR = re.compile(r"^([A-Za-z0-9_]+)\s+\{(.*)\}\s*$")
_TAG = re.compile(r"<[^<>]*>")
_TOKEN = re.compile(r"%[^%\s]+%")
_SLASH = re.compile(r"%SLASH%|\bSLASH\b")
_OBJECTIVE_NAMES = ("ObjectiveID", "Objective", "ObjectiveMessage")
_MESSAGE_NAMES = ("Message", "MessageLocID", "OSDMessage")
# The comms title is Dialog > GcAlienPuzzleEntry > Title. It is not a stage Title.
# One file this large holds the interpreter long enough to gap the window.
_NAME_POOL_BYTES = 200_000
_SKIP_VALUES = {"", "true", "false", "none", "invalid", "invalid_event"}
_BUTTON = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
# "A T L A S" is letter-spacing, not separate words.
_SPACED_CAPS = re.compile(r"\b(?:[A-Z]\s+){2,}[A-Z]\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
# A message used by this many missions is a shared prompt, not that mission's name.
_SHARED_MESSAGE = 3
NAME_LIMIT = 60
SUBTITLE_LIMIT = 40
# Bump when the cached shape changes. The source hash covers parser tweaks.
CACHE_VERSION = 8


@dataclass
class _MissionLink:
    title_key: str = ""
    description_key: str = ""
    subtitle_key: str = ""
    objective_key: str = ""
    message_key: str = ""
    dialog_title_keys: tuple[str, ...] = ()
    page_key: str = ""
    title_was_type: bool = False


@dataclass
class GameNameReport:
    names: dict[str, str] = field(default_factory=dict)
    subtitles: dict[str, str] = field(default_factory=dict)
    files_read: int = 0
    loc_entries: int = 0
    missions_linked: int = 0
    binary_skipped: int = 0
    unnamed: int = 0
    from_cache: bool = False
    log_ids: set[str] = field(default_factory=set)
    alerts: dict[str, str] = field(default_factory=dict)

    def unique_titles(self) -> int:
        """One count per mission id. A tidied id is not a log title."""
        seen: set[str] = set()
        for key, value in self.names.items():
            text = str(value)
            if is_class_name(text) or text.endswith("(id)"):
                continue
            seen.add(_canonical(key))
        return len(seen)

    def summary_for_list(self, rows: list[dict]) -> str:
        """Name line for the banner. Counts the rows in this list, not every game mission."""
        mission_rows = [row for row in rows if row.get("mission_id")]
        unnamed = _unnamed_rows(mission_rows)
        count = self.unique_titles()
        noun = "title" if count == 1 else "titles"
        text = (
            f"Names from game files: {count} {noun}, "
            f"{self.loc_entries} language rows, {len(mission_rows)} mission links, "
            f"{self.files_read} files read."
        )
        text += f" {unnamed} still have no name."
        if self.binary_skipped:
            text += (
                f" Skipped {self.binary_skipped} binary MBIN file(s). "
                "They were not turned into text, so those names are missing."
            )
        if self.from_cache:
            text += " Loaded from cache."
        return text

    def summary(self) -> str:
        count = self.unique_titles()
        noun = "title" if count == 1 else "titles"
        text = (
            f"Names from game files: {count} {noun}, "
            f"{self.loc_entries} language rows, {self.missions_linked} mission links, "
            f"{self.files_read} files read."
        )
        text += f" {self.unnamed} still have no name."
        if self.binary_skipped:
            text += (
                f" Skipped {self.binary_skipped} binary MBIN file(s). "
                "They were not turned into text, so those names are missing."
            )
        if self.from_cache:
            text += " Loaded from cache."
        return text


def _unnamed_rows(mission_rows: list[dict]) -> int:
    return sum(1 for row in mission_rows if is_generated_title(str(row.get("name") or "")))


def list_name_line(rows: list[dict], report: GameNameReport | None = None) -> str:
    """Banner name line. After Clear cache there is no report, so the rows are the count."""
    if report is not None:
        return report.summary_for_list(rows)
    mission_rows = [row for row in rows if row.get("mission_id")]
    unnamed = _unnamed_rows(mission_rows)
    return (
        f"Names from game files: 0 titles, 0 language rows, "
        f"{len(mission_rows)} mission links, 0 files read. "
        f"{unnamed} still have no name."
    )


def cache_path_for(root: Path) -> Path:
    """One cache file per game-files folder, so switching folders does not rebuild the other."""
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_root() / f"game-names-{digest}.json"


def default_names_cache(root: Path | None = None) -> Path:
    if root is None:
        return cache_root() / "game-names.json"
    return cache_path_for(root)


def peek_game_names(root: Path, cache: Path | None = None) -> GameNameReport | None:
    """Return a disk-cache hit. A miss does not parse XML."""
    if not root.exists():
        return None
    started = time.perf_counter()
    files = list(_candidate_files(root))
    fingerprint = _fingerprint(files)
    cache_path = cache if cache is not None else cache_path_for(root)
    cached = _read_cache(cache_path, fingerprint, root)
    if cached is None:
        return None
    record("name-load", (time.perf_counter() - started) * 1000.0)
    return cached


def load_game_names(root: Path, cache: Path | None = None, progress=None) -> GameNameReport:
    with timed("name-load"):
        return _load_game_names_body(root, cache, progress)


def _load_game_names_body(root: Path, cache: Path | None = None, progress=None) -> GameNameReport:
    if not root.exists():
        raise FileNotFoundError(f"Game files path does not exist: {root}")
    files = list(_candidate_files(root))
    fingerprint = _fingerprint(files)
    cache_path = cache if cache is not None else cache_path_for(root)
    cached = _read_cache(cache_path, fingerprint, root)
    if cached is not None:
        _report_progress(progress, "Loading mission names… 100%")
        return cached

    report = GameNameReport()
    loc: dict[str, str] = {}
    links: dict[str, _MissionLink] = {}
    parsed = _parse_name_files(files, progress)
    for kind, payload in parsed:
        if kind == "binary":
            report.binary_skipped += 1
        elif kind == "loc":
            report.files_read += 1
            loc.update(payload)
            report.loc_entries += len(payload)
        elif kind == "mission":
            report.files_read += 1
            links.update(payload)
    _resolve_loc_refs(loc)
    report.names, report.subtitles, report.log_ids, report.alerts = _names_from_links(loc, links)
    report.missions_linked = len(links)
    named = {_canonical(key) for key in report.names}
    report.unnamed = sum(1 for mission_id in links if _canonical(mission_id) not in named)
    _write_cache(cache_path, fingerprint, report, root)
    return report


def _report_progress(progress, message: str) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        return


def _source_id() -> str:
    """Short hash of this module so a parser change rebuilds the name cache."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _canonical(mission_id: str) -> str:
    return mission_id[1:] if mission_id.startswith("^") else mission_id


def _candidate_files(root: Path):
    paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
    for path in paths:
        if _kind(path) is not None:
            yield path


def _kind(path: Path) -> str | None:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix not in {".mxml", ".exml", ".xml", ".txt", ".mbin"} and not name.endswith(".mbin.pc"):
        return None
    folded = "/".join(part.lower() for part in path.parts)
    if "usenglish" in name:
        return None
    if "english" in name and (name.startswith("nms_") or "language" in folded or "loc" in name):
        return "loc"
    if "missions" in folded or name.endswith("missiontable.mxml") or name.endswith("missiontable.exml"):
        return "mission"
    return None


def _fingerprint(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        stat = path.stat()
        rows.append(
            {
                "path": str(path.resolve()),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        )
    rows.sort(key=lambda row: row["path"])
    return rows


def _read_cache(path: Path, fingerprint: list[dict], root: Path) -> GameNameReport | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    # File mtimes and CACHE_VERSION decide a hit. A tool-version bump does not
    # throw the cache away. Bump CACHE_VERSION when the parser output changes.
    if document.get("version") != CACHE_VERSION:
        return None
    if document.get("root") != str(root.resolve()):
        return None
    if document.get("files") != fingerprint:
        return None
    names = document.get("names")
    subtitles = document.get("subtitles")
    if not isinstance(names, dict) or not isinstance(subtitles, dict):
        return None
    report = GameNameReport(
        names={str(key): str(value) for key, value in names.items()},
        subtitles={str(key): str(value) for key, value in subtitles.items()},
        files_read=int(document.get("files_read") or 0),
        loc_entries=int(document.get("loc_entries") or 0),
        missions_linked=int(document.get("missions_linked") or 0),
        binary_skipped=int(document.get("binary_skipped") or 0),
        unnamed=int(document.get("unnamed") or 0),
        from_cache=True,
        log_ids={str(item) for item in document.get("log_ids") or []},
        alerts=(
            {str(key): str(value) for key, value in document["alerts"].items()}
            if isinstance(document.get("alerts"), dict)
            else {}
        ),
    )
    return report


def _write_cache(path: Path, fingerprint: list[dict], report: GameNameReport, root: Path) -> None:
    payload = {
        "version": CACHE_VERSION,
        "tool": __version__,
        "source": _source_id(),
        "root": str(root.resolve()),
        "files": fingerprint,
        "names": report.names,
        "subtitles": report.subtitles,
        "files_read": report.files_read,
        "loc_entries": report.loc_entries,
        "missions_linked": report.missions_linked,
        "binary_skipped": report.binary_skipped,
        "unnamed": report.unnamed,
        "log_ids": sorted(report.log_ids),
        "alerts": report.alerts,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return


def _is_binary_file(path: Path) -> bool:
    with path.open("rb") as handle:
        sample = handle.read(400).lstrip()
    if sample.startswith(b"<") or sample.startswith(b"\xef\xbb\xbf<"):
        return False
    return b"\x00" in sample


def _parse_loc_file(path: Path) -> dict[str, str]:
    if not _looks_like_xml(path):
        return _parse_loc_text(path.read_text(encoding="utf-8", errors="surrogateescape"))
    return _parse_loc_xml(path)


def _looks_like_xml(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(200)
    text = head.decode("utf-8", errors="surrogateescape")
    return text.lstrip("\ufeff").lstrip().startswith("<")


def _parse_loc_text(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _TRANSLATOR.match(line.strip())
        if not match:
            continue
        cleaned = _clean(match.group(2))
        if cleaned:
            found[match.group(1)] = cleaned
    return found


def _parse_name_files(files: list[Path], progress) -> list[tuple[str, dict]]:
    """Parse language and mission files. Large extracts run in other processes."""
    total = len(files)
    if _names_need_pool(files):
        from nmsmissions.gamedata import map_in_pool

        _report_progress(progress, "Loading mission names… 0%")
        parsed = map_in_pool(_parse_name_source, [str(path) for path in files])
        _report_progress(progress, "Loading mission names… 100%")
        return [item if item is not None else ("skip", {}) for item in parsed]
    found: list[tuple[str, dict]] = []
    last_percent = -1
    for index, path in enumerate(files, start=1):
        percent = int(index * 100 / total) if total else 100
        if percent != last_percent or index == total:
            _report_progress(progress, f"Loading mission names… {percent}%")
            last_percent = percent
        found.append(_parse_name_source(str(path)))
        time.sleep(0)
    return found


def _names_need_pool(files: list[Path]) -> bool:
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        if size >= _NAME_POOL_BYTES or total >= _NAME_POOL_BYTES:
            return True
    return False


def _parse_name_source(path_str: str) -> tuple[str, dict]:
    """One file, in this process or a pool worker. Returns a kind and a dict."""
    path = Path(path_str)
    if _is_binary_file(path):
        return "binary", {}
    kind = _kind(path)
    if kind == "loc":
        return "loc", _parse_loc_file(path)
    if kind == "mission":
        return "mission", _parse_mission_file(path)
    return "skip", {}


def _parse_loc_xml(source: Path | io.BytesIO) -> dict[str, str]:
    from nmsmissions.timing import breathe

    found: dict[str, str] = {}
    stack: list[ET.Element] = []
    try:
        events = ET.iterparse(source, events=("start", "end"))
    except ET.ParseError:
        return found
    seen = 0
    for event, elem in events:
        seen += 1
        if seen % 500 == 0:
            breathe(1)
        if event == "start":
            stack.append(elem)
            continue
        if stack:
            stack.pop()
        captured = _loc_pair(elem)
        if captured is None:
            continue
        key, value = captured
        found[key] = value
        elem.clear()
        if stack:
            _remove_child(stack[-1], elem)
    return found


def _loc_pair(elem: ET.Element) -> tuple[str, str] | None:
    ident = None
    english = None
    for child in list(elem):
        name = child.attrib.get("name")
        if name == "Id" and ident is None:
            ident = _value(child)
        elif name == "English" and english is None:
            english = _value(child)
    if not ident or not english:
        return None
    cleaned = _clean(english)
    if not cleaned:
        return None
    return ident, cleaned


def _parse_mission_file(path: Path) -> dict[str, _MissionLink]:
    from nmsmissions.timing import breathe

    links: dict[str, _MissionLink] = {}
    stack: list[ET.Element] = []
    try:
        events = ET.iterparse(path, events=("start", "end"))
    except ET.ParseError:
        return links
    seen = 0
    for event, elem in events:
        seen += 1
        if seen % 500 == 0:
            breathe(1)
        if event == "start":
            stack.append(elem)
            continue
        if stack:
            stack.pop()
        mission_id = _direct_value(elem, "MissionID")
        if not mission_id:
            continue
        # Only a real mission entry carries MissionTitles. Later conditions
        # and start-mission events also have a MissionID and would wipe the name.
        if not any(child.attrib.get("name") == "MissionTitles" for child in list(elem)):
            continue
        links[mission_id] = _MissionLink(
            title_key=_list_key(elem, "MissionTitles"),
            description_key=_list_key(elem, "MissionDescriptions"),
            subtitle_key=_list_key(elem, "MissionSubtitles"),
            objective_key=_first_in(elem, "Stages", _OBJECTIVE_NAMES),
            message_key=_first_in(elem, "Stages", _MESSAGE_NAMES),
            dialog_title_keys=_dialog_title_keys(elem),
            page_key=_direct_value(elem, "MissionPageLocID") or "",
            title_was_type=_list_is_type(elem, "MissionTitles"),
        )
        elem.clear()
        if stack:
            _remove_child(stack[-1], elem)
    return links


def _remove_child(parent: ET.Element, child: ET.Element) -> None:
    try:
        parent.remove(child)
    except ValueError:
        return


def _direct_value(elem: ET.Element, name: str) -> str | None:
    for child in list(elem):
        if child.attrib.get("name") == name:
            return _usable(_value(child))
    return None


def _list_key(elem: ET.Element, name: str) -> str:
    for child in list(elem):
        if child.attrib.get("name") == name:
            found = _first_title_key(child)
            if found and not _is_type_name(found):
                return found
            return _usable(_value(child)) or ""
    return ""


def _list_is_type(elem: ET.Element, name: str) -> bool:
    """True when the list's own value is a class name and it has no title text."""
    for child in list(elem):
        if child.attrib.get("name") != name:
            continue
        if is_class_name(child.attrib.get("value")):
            return True
        for node in list(child):
            if node.attrib.get("name") == "Value" and is_class_name(node.attrib.get("value")):
                return True
    return False


def _dialog_title_keys(elem: ET.Element) -> tuple[str, ...]:
    """Title values under Dialog > GcAlienPuzzleEntry, in file order.

    Empty values are skipped here. A key that does not resolve in the language
    cache is skipped later, and the next Title is used.
    """
    keys: list[str] = []
    for child in list(elem):
        if child.attrib.get("name") == "Dialog":
            _collect_puzzle_titles(child, keys, inside=False)
    return tuple(keys)


def _puzzle_entry(node: ET.Element) -> bool:
    name = node.attrib.get("name") or ""
    value = node.attrib.get("value") or ""
    return "GcAlienPuzzleEntry" in name or "GcAlienPuzzleEntry" in value


def _collect_puzzle_titles(node: ET.Element, keys: list[str], inside: bool) -> None:
    here = inside or _puzzle_entry(node)
    if here and node.attrib.get("name") == "Title":
        raw = _usable(_value(node))
        if raw:
            keys.append(raw)
        return
    for child in list(node):
        _collect_puzzle_titles(child, keys, here)


def _first_in(elem: ET.Element, container: str, names: tuple[str, ...]) -> str:
    for child in list(elem):
        if child.attrib.get("name") == container:
            return _first_named(child, names) or ""
    return ""


def _first_named(elem: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in list(elem):
        if child.attrib.get("name") in names:
            raw = _usable(_value(child))
            if raw:
                return raw
        found = _first_named(child, names)
        if found:
            return found
    return None


def _first_title_key(node: ET.Element) -> str:
    fmt = ""
    count = 1
    for child in list(node):
        name = child.attrib.get("name")
        if name == "Format":
            fmt = _value(child) or ""
        elif name == "Count":
            raw = _value(child) or "1"
            try:
                count = int(raw)
            except ValueError:
                count = 1
    return _pick_key(fmt, count)


def _pick_key(fmt: str, count: int) -> str:
    fmt = fmt.strip()
    if not fmt:
        return ""
    if "%d" in fmt:
        return fmt.replace("%d", "1")
    if count > 1:
        return f"{fmt}_1"
    return fmt


def _usable(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text or text.lower() in _SKIP_VALUES:
        return None
    if _is_type_name(text):
        return None
    return text


def _resolve_loc_refs(table: dict[str, str]) -> None:
    for key in list(table):
        seen = {key}
        current = table[key].strip()
        while current in table and current not in seen:
            seen.add(current)
            current = table[current].strip()
        table[key] = current


def _as_text(loc: dict[str, str], key: str) -> str | None:
    if not key:
        return None
    resolved = _english_for(loc, key)
    if resolved:
        return resolved
    text = _clean(key)
    if not text or _LOC_ID.fullmatch(text) or _is_type_name(text):
        return None
    if any(char.islower() for char in text) or " " in text:
        return text
    return None


def _english_for(loc: dict[str, str], key: str) -> str | None:
    if not key:
        return None
    candidates = [key]
    if "%d" in key:
        candidates.append(key.replace("%d", "1"))
    for candidate in candidates:
        text = loc.get(candidate, "").strip()
        if text and not _LOC_ID.fullmatch(text) and not _is_type_name(text):
            return text
    return None


def display_names(
    report: GameNameReport | None, fallback: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Log titles for the list, subtitles, and notify lines that are not titles.

    A tidied id is not a title. Chain steps keep their step label.
    """
    titles = dict(fallback)
    alerts: dict[str, str] = {}
    if report is None:
        return titles, {}, alerts
    for key, value in report.names.items():
        text = str(value)
        if is_class_name(text) or is_generated_title(text):
            continue
        if _canonical(key) in report.log_ids:
            titles[key] = text
        else:
            alerts.setdefault(key, text)
    for key, value in report.alerts.items():
        alerts.setdefault(key, str(value))
    return titles, dict(report.subtitles), alerts


def _first_dialog_title(loc: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """First non-empty Dialog Title that the language cache can resolve."""
    for key in keys:
        text = _tidy_fallback(_as_text(loc, key))
        if text:
            return text
    return None


def _names_from_links(
    loc: dict[str, str], links: dict[str, _MissionLink]
) -> tuple[dict[str, str], dict[str, str], set[str], dict[str, str]]:
    message_uses: dict[str, int] = defaultdict(int)
    for link in links.values():
        if link.message_key:
            message_uses[link.message_key] += 1
    message_text: dict[str, str | None] = {}
    text_uses: dict[str, int] = defaultdict(int)
    for mission_id, link in links.items():
        if not link.message_key or message_uses[link.message_key] >= _SHARED_MESSAGE:
            message_text[mission_id] = None
            continue
        resolved = _tidy_fallback(_as_text(loc, link.message_key))
        message_text[mission_id] = resolved
        if resolved:
            text_uses[resolved] += 1
    chosen: dict[str, str] = {}
    subtitles: dict[str, str | None] = {}
    titled: dict[str, bool] = {}
    log_ids: set[str] = set()
    alerts: dict[str, str] = {}
    for mission_id, link in links.items():
        title = _as_text(loc, link.title_key)
        description = _tidy_fallback(_as_text(loc, link.description_key))
        subtitle = _tidy_fallback(_as_text(loc, link.subtitle_key))
        objective = _tidy_fallback(_as_text(loc, link.objective_key))
        message = message_text.get(mission_id)
        if message and text_uses[message] >= _SHARED_MESSAGE:
            message = None
        # Message is often an event id. The words are the first Dialog Title that resolves.
        notify = _first_dialog_title(loc, link.dialog_title_keys) or message
        page = _tidy_fallback(_as_text(loc, link.page_key))
        # ObjectiveID before a stage Message. Shared prompts such as
        # "Inventory Full..." are not a mission's name.
        primary = title or description or subtitle or objective or notify or page
        if is_class_name(primary):
            primary = None
        real_title = bool(title) and not is_class_name(title)
        if real_title:
            chosen[mission_id] = title
            log_ids.add(_canonical(mission_id))
        elif primary:
            # A description, objective, or notify line is not the log title.
            # Chain steps keep "Chain - step N" and may show this after it.
            chosen[mission_id] = primary
            alerts[mission_id] = primary
        else:
            continue
        subtitles[mission_id] = subtitle
        titled[mission_id] = real_title

    # Subtitles stay beside the log title. The chain view joins them only when
    # that makes every copy in that chain unique.
    names: dict[str, str] = {}
    subtitle_out: dict[str, str] = {}
    alert_out: dict[str, str] = {}
    for mission_id, text in chosen.items():
        _store(names, mission_id, text)
        extra = subtitles.get(mission_id)
        if titled.get(mission_id) and extra and extra != text:
            _store(subtitle_out, mission_id, _cap(extra, SUBTITLE_LIMIT))
    for mission_id, text in alerts.items():
        _store(alert_out, mission_id, text)
    return names, subtitle_out, log_ids, alert_out


def _store(names: dict[str, str], mission_id: str, title: str) -> None:
    names[mission_id] = title
    if mission_id.startswith("^"):
        names.setdefault(mission_id[1:], title)
    else:
        names.setdefault("^" + mission_id, title)


def _fallback_title(text: str) -> bool:
    return text.endswith(" (id)")


def _unnamed_count(links: dict[str, _MissionLink], names: dict[str, str]) -> int:
    """Missions with no real log title. A tidied id still counts as unnamed."""
    real = {
        _canonical(key)
        for key, value in names.items()
        if not _fallback_title(str(value))
    }
    return sum(1 for mission_id in links if _canonical(mission_id) not in real)


def _is_type_name(raw: str) -> bool:
    text = raw.strip()
    if text.endswith(".xml") or text.endswith(".mxml"):
        return True
    return is_class_name(text)


def _value(node: ET.Element) -> str | None:
    """Read a flat value, or the nested Value used by current MXML."""
    nested = None
    for child in list(node):
        if child.attrib.get("name") == "Value":
            nested = _value(child)
            if nested:
                break
    raw = node.attrib.get("value")
    if raw and not _is_type_name(raw):
        return raw
    if nested:
        return nested
    if node.text and node.text.strip():
        return node.text.strip()
    return None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _collapse_spaced_caps(text: str) -> str:
    return _SPACED_CAPS.sub(lambda match: match.group(0).replace(" ", ""), text)


def _clean(text: str) -> str:
    """Keep the first line, then drop tags and tokens. Letter-spaced words stay words."""
    text = _first_line(text)
    text = _TAG.sub("", text)
    text = _SLASH.sub("/", text)
    text = _TOKEN.sub("…", text)
    text = _collapse_spaced_caps(text)
    return " ".join(text.split())


def _first_sentence(text: str) -> str:
    parts = _SENTENCE_BREAK.split(text, maxsplit=1)
    return parts[0].strip()


def _tidy_fallback(text: str | None) -> str | None:
    """Shorten a subtitle or objective. Official log titles are left alone.

    The first sentence is kept. Button tokens such as QUICK_MENU are dropped.
    A word like ATLAS stays ATLAS.
    """
    if not text:
        return None
    cleaned = _first_sentence(text)
    cleaned = _BUTTON.sub(" ", cleaned)
    cleaned = _collapse_spaced_caps(cleaned)
    cleaned = " ".join(cleaned.split()).strip(" -–—:;,.")
    if not cleaned or _LOC_ID.fullmatch(cleaned):
        return None
    return _cap(cleaned, NAME_LIMIT)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

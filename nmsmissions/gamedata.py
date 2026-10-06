"""Mission finals and reward tables from the player's extracted MXML.

Game text is not shipped here. Read my game files copies it into the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from nmsmissions import cache_root
from nmsmissions.timing import timed


class GameDataError(ValueError):
    """The extracted mission tables could not be read."""


@dataclass
class RewardDef:
    reward_id: str
    choice: str
    kind: str
    body: dict


@dataclass
class StageReward:
    reward_id: str
    stage_progress: int | None
    dialog: bool
    versions: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class MissionInfo:
    mission_id: str
    finals: list[tuple[int, int]] = field(default_factory=list)
    rewards: dict[str, RewardDef] = field(default_factory=dict)
    stages: list[StageReward] = field(default_factory=list)
    requires_missions: list[str] = field(default_factory=list)
    requires_flags: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class GameTables:
    missions: dict[str, MissionInfo] = field(default_factory=dict)
    products: dict[str, int] = field(default_factory=dict)
    substances: dict[str, int] = field(default_factory=dict)
    tech_ids: set[str] = field(default_factory=set)
    reward_table: dict[str, RewardDef] = field(default_factory=dict)
    max_version: int = 0
    stale: bool = False
    warning: str = ""
    from_cache: bool = False
    files_match: bool = False
    corvette_parts: list = field(default_factory=list)
    corvette_stack_cap: int = 0
    corvette_needs_reread: bool = False

    def highest_version(self) -> int:
        versions = [version for info in self.missions.values() for version, _progress in info.finals]
        return max(versions) if versions else self.max_version


def final_progress(info: MissionInfo | None, save_version: int) -> tuple[int | None, str | None]:
    """Progress for the highest FinalStageVersions row at or below the save version."""
    if info is None or not info.finals:
        return None, "This mission has no FinalStageVersions, so it was not finished."
    eligible = [(version, progress) for version, progress in info.finals if version <= save_version]
    if not eligible:
        version, progress = min(info.finals)
        return progress, (
            f"The save version {save_version} is below every table row. Using version {version}."
        )
    version, progress = max(eligible)
    if save_version > max(item[0] for item in info.finals):
        return progress, (
            "Game files are older than this save's mission version. Using the newest row in the table."
        )
    return progress, None


def stage_progress_for(stage: StageReward, save_version: int) -> int | None:
    """Progress for this stage at the save's mission version.

    Real stages store that number in a Versions list. An explicit Progress
    property, used by the older test files, is kept when Versions is absent.
    """
    if stage.versions:
        eligible = [(version, progress) for version, progress in stage.versions if version <= save_version]
        if not eligible:
            return min(stage.versions)[1]
        return max(eligible)[1]
    return stage.stage_progress


def finals_for_catalog(tables: GameTables, save_version: int | None) -> dict[str, int]:
    """Final progress from extracted tables, for missions the yaml catalog does not list."""
    version = 0 if save_version is None else save_version
    found: dict[str, int] = {}
    for mission_id, info in tables.missions.items():
        progress, _note = final_progress(info, version)
        if progress is None:
            continue
        bare = mission_id[1:] if mission_id.startswith("^") else mission_id
        marked = mission_id if mission_id.startswith("^") else "^" + mission_id
        found[mission_id] = progress
        found.setdefault(bare, progress)
        found.setdefault(marked, progress)
    return found


# Bump when the cached JSON shape changes. The parser hash is stored and not
# compared, so a faster parser still reads a cache written by 0.5.0.
TABLES_CACHE_VERSION = 4
_TABLE_SUFFIXES = {".mxml", ".exml", ".xml"}
# A cold extract is a few dozen large files, not 64 small ones. The pool
# starts when the XML to parse is at least this many bytes, or one file is
# large enough to hold the interpreter for more than a short gap. Parsing
# those files in-process holds the GIL and freezes the window.
_POOL_BYTES = 2 * 1024 * 1024
_POOL_FILE_BYTES = 200_000
_MARKERS = (
    b"FinalStageVersions",
    b"StackMultiplier",
    b"RewardTable",
    b"GcRewardTable",
    b"GcGenericRewardTableEntry",
    b"GcRewardMoney",
    b"GcRewardSpecificProduct",
    b"TechId",
    b"TechnologyID",
)


def cache_path_for_tables(root: Path) -> Path:
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_root() / f"game-tables-{digest}.json"


def _is_language_file(path: Path) -> bool:
    """Language rows are names, not finals or rewards. Skipping them is the slow part."""
    name = path.name.lower()
    if "english" not in name and "language" not in name:
        return False
    return path.suffix.lower() in _TABLE_SUFFIXES or name.endswith(".mbin") or name.endswith(".mbin.pc")


def _table_files(root: Path) -> list[Path]:
    paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
    found = []
    for path in paths:
        if path.suffix.lower() not in _TABLE_SUFFIXES:
            continue
        if _is_language_file(path):
            continue
        found.append(path)
    found.sort(key=lambda item: str(item))
    return found


def _file_rows(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({"path": str(path.resolve()), "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
    rows.sort(key=lambda row: row["path"])
    return rows


def _pak_files(pcbanks: Path | None) -> list[Path]:
    if pcbanks is None or not Path(pcbanks).exists():
        return []
    root = Path(pcbanks)
    paths = [root] if root.is_file() else [path for path in root.rglob("*.pak") if path.is_file()]
    return paths


def _parser_id() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _progress(progress, message: str) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        return


def peek_tables(root: Path, pcbanks: Path | None = None, cache: Path | None = None) -> GameTables | None:
    """Return a disk-cache hit. A miss does not parse XML."""
    if cache is None or not root.exists():
        return None
    from nmsmissions.timing import record

    started = time.perf_counter() if _timing_on() else 0.0
    files = _table_files(root)
    paks = _pak_files(pcbanks)
    cached = _read_table_cache(cache, _file_rows(files), _file_rows(paks), root)
    if cached is None:
        return None
    if started:
        record("table-load", (time.perf_counter() - started) * 1000.0)
    return cached


def _timing_on() -> bool:
    from nmsmissions.timing import enabled

    return enabled()


def load_tables(
    root: Path,
    pcbanks: Path | None = None,
    cache: Path | None = None,
    progress=None,
) -> GameTables:
    with timed("table-load"):
        return _load_tables_body(root, pcbanks, cache, progress)


def _load_tables_body(
    root: Path,
    pcbanks: Path | None = None,
    cache: Path | None = None,
    progress=None,
) -> GameTables:
    if not root.exists():
        raise GameDataError(f"Game files path does not exist: {root}")
    files = _table_files(root)
    paks = _pak_files(pcbanks)
    fingerprint = _file_rows(files)
    pak_print = _file_rows(paks)
    if cache is not None:
        cached = _read_table_cache(cache, fingerprint, pak_print, root)
        if cached is not None:
            _progress(progress, "Loading mission tables… 100%")
            return cached
    chosen = _files_to_parse(files, progress)
    tables = _parse_chosen(chosen, progress)
    from nmsmissions.corvette import attach_catalog

    attach_catalog(tables, root)
    newest_table = max((row["mtime_ns"] for row in fingerprint), default=0)
    tables.max_version = tables.highest_version()
    tables.files_match = False
    if paks:
        from nmsmissions.gameread import source_matches

        matched = source_matches(Path(root), paks)
        if matched is True:
            tables.stale = False
            tables.warning = ""
            tables.files_match = True
        elif matched is False:
            tables.stale = True
            tables.warning = (
                "The copied game files are from a different PCBANKS than the one on this PC. "
                "Rewards are skipped. Finish still updates progress."
            )
        else:
            newest_pak = max(path.stat().st_mtime_ns for path in paks)
            if newest_table and newest_table < newest_pak:
                tables.stale = True
                tables.warning = (
                    "Extracted mission tables are older than the newest PCBANKS pak. "
                    "Rewards are skipped. Finish still updates progress."
                )
    if cache is not None:
        _write_table_cache(cache, fingerprint, pak_print, tables, root)
    return tables


def _path_needs_table(path: Path) -> bool:
    """Mission, reward, product, substance, and technology files are parsed whole."""
    stem = path.stem.lower()
    folded = "/" + "/".join(part.lower() for part in path.parts) + "/"
    if stem in {"mission", "missions"} or stem.startswith("mission") or "missiontable" in stem:
        return True
    if "/missions/" in folded:
        return True
    if "reward" in stem:
        return True
    return any(token in stem for token in ("product", "substance", "technology", "techtable"))


def _bytes_need_table(path: Path) -> bool:
    """A file with another name is parsed only when it carries a table marker."""
    tail = 48
    try:
        with path.open("rb") as handle:
            pending = b""
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    return False
                blob = pending + chunk
                if any(marker in blob for marker in _MARKERS):
                    return True
                pending = blob[-tail:]
    except OSError:
        return False


def _files_to_parse(files: list[Path], progress) -> list[Path]:
    """Keep the fingerprint file list, and parse only the files that can hold tables."""
    chosen: list[Path | None] = [None] * len(files)
    scan_at: list[int] = []
    for index, path in enumerate(files):
        if _path_needs_table(path):
            chosen[index] = path
        else:
            scan_at.append(index)
    total = len(files)
    done = total - len(scan_at)
    last = -1

    def tick(extra: int = 0) -> None:
        nonlocal done, last
        done += extra
        percent = int(done * 40 / total) if total else 40
        if percent == last:
            return
        last = percent
        _progress(progress, f"Loading mission tables… {percent}%")

    tick(0)
    if not scan_at:
        return [path for path in chosen if path is not None]

    def probe(index: int) -> tuple[int, Path | None]:
        path = files[index]
        if _bytes_need_table(path):
            return index, path
        return index, None

    if len(scan_at) >= 32:
        from concurrent.futures import ThreadPoolExecutor

        lock = threading.Lock()

        def locked_tick() -> None:
            with lock:
                tick(1)

        workers = min(8, os.cpu_count() or 2)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, path in pool.map(probe, scan_at):
                chosen[index] = path
                locked_tick()
    else:
        for index in scan_at:
            found_index, path = probe(index)
            chosen[found_index] = path
            tick(1)
    return [path for path in chosen if path is not None]


def _parse_table_file(path_str: str) -> GameTables | None:
    path = Path(path_str)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    tables = GameTables()
    _absorb(root, tables, str(path))
    return tables


def _merge_into(base: GameTables, extra: GameTables) -> None:
    """Fold one file into the running tables. Later files win, matching a sequential parse."""
    for mission_id, info in extra.missions.items():
        current = base.missions.get(mission_id)
        if current is None:
            base.missions[mission_id] = info
            continue
        current.finals = info.finals
        current.rewards.update(info.rewards)
        current.stages = info.stages
        current.requires_missions = list(info.requires_missions)
        current.requires_flags = list(info.requires_flags)
        if info.source:
            current.source = info.source
    base.products.update(extra.products)
    base.substances.update(extra.substances)
    base.tech_ids.update(extra.tech_ids)
    for reward_id, reward in extra.reward_table.items():
        base.reward_table.setdefault(reward_id, reward)


def _file_sizes(paths: list[Path]) -> list[int]:
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            sizes.append(0)
    return sizes


def _submit_order(sizes: list[int]) -> list[int]:
    """Largest file first. Equal sizes keep the original order."""
    return sorted(range(len(sizes)), key=lambda index: (-sizes[index], index))


def _parse_chosen(paths: list[Path], progress, pool_bytes: int | None = None) -> GameTables:
    """Parse mission files. Large extracts run in other processes so Tk keeps the GIL."""
    tables = GameTables()
    total = len(paths)
    sizes = _file_sizes(paths)
    limit = _POOL_BYTES if pool_bytes is None else pool_bytes
    largest = max(sizes) if sizes else 0
    if total == 0 or (sum(sizes) < limit and largest < _POOL_FILE_BYTES):
        for index, path in enumerate(paths, start=1):
            _take_table(tables, _parse_table_file(str(path)), index, total, progress)
            time.sleep(0)
        _progress(progress, "Loading mission tables… 100%")
        return tables
    parsed = _parse_in_processes(paths, sizes, progress)
    if parsed is None:
        tables = GameTables()
        for index, path in enumerate(paths, start=1):
            _take_table(tables, _parse_table_file(str(path)), index, total, progress)
        _progress(progress, "Loading mission tables… 100%")
        return tables
    for partial in parsed:
        if partial is not None:
            _merge_into(tables, partial)
        time.sleep(0)
    _progress(progress, "Loading mission tables… 100%")
    return tables


def _take_table(tables: GameTables, partial: GameTables | None, index: int, total: int, progress) -> None:
    if partial is not None:
        _merge_into(tables, partial)
    if total:
        percent = 40 + int(index * 60 / total)
        _progress(progress, f"Loading mission tables… {min(percent, 100)}%")


def _parse_in_processes(paths: list[Path], sizes: list[int], progress) -> list[GameTables | None] | None:
    """Parse largest-first. Results come back in the original file order."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    workers = min(8, os.cpu_count() or 2, len(paths))
    order = _submit_order(sizes)
    results: list[GameTables | None] = [None] * len(paths)
    try:
        context = multiprocessing.get_context("spawn")
        with _hidden_children():
            with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                futures = {
                    pool.submit(_parse_table_file, str(paths[index])): index for index in order
                }
                done = 0
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    done += 1
                    percent = 40 + int(done * 60 / len(paths))
                    _progress(progress, f"Loading mission tables… {min(percent, 100)}%")
                    time.sleep(0)
    except (OSError, BrokenProcessPool, Exception):
        return None
    return results


@contextmanager
def _hidden_children():
    """On Windows, pool workers start with no console window."""
    if os.name != "nt":
        yield
        return
    import subprocess

    real = subprocess.Popen

    def wrapped(*args, **kwargs):
        flags = int(kwargs.get("creationflags") or 0)
        kwargs["creationflags"] = flags | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startup_cls = getattr(subprocess, "STARTUPINFO", None)
        if startup_cls is not None and kwargs.get("startupinfo") is None:
            info = startup_cls()
            info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            info.wShowWindow = 0
            kwargs["startupinfo"] = info
        return real(*args, **kwargs)

    subprocess.Popen = wrapped
    try:
        yield
    finally:
        subprocess.Popen = real


def call_in_pool(func, args: tuple):
    """Run one picklable job in a spawned process so this thread can release the GIL.

    A pool that cannot start runs the job here instead.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    context = multiprocessing.get_context("spawn")
    try:
        with _hidden_children():
            with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
                return pool.submit(func, *args).result()
    except (OSError, BrokenProcessPool):
        return func(*args)


def map_in_pool(func, items: list):
    """Run each item in the process pool. Results stay in the original order."""
    if not items:
        return []
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    workers = min(8, os.cpu_count() or 2, len(items))
    context = multiprocessing.get_context("spawn")
    try:
        with _hidden_children():
            with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                futures = {pool.submit(func, item): index for index, item in enumerate(items)}
                results: list = [None] * len(items)
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    time.sleep(0)
                return results
    except (OSError, BrokenProcessPool):
        found = []
        for item in items:
            found.append(func(item))
            time.sleep(0)
        return found


def _read_table_cache(path: Path, fingerprint: list[dict], paks: list[dict], root: Path) -> GameTables | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("version") != TABLES_CACHE_VERSION:
        return None
    if document.get("root") != str(Path(root).resolve()):
        return None
    if document.get("files") != fingerprint or document.get("paks") != paks:
        return None
    payload = document.get("tables")
    if not isinstance(payload, dict):
        return None
    try:
        tables = _tables_from_json(payload)
    except (KeyError, TypeError, ValueError):
        return None
    tables.from_cache = True
    return tables


def _write_table_cache(
    path: Path,
    fingerprint: list[dict],
    paks: list[dict],
    tables: GameTables,
    root: Path,
) -> None:
    document = {
        "version": TABLES_CACHE_VERSION,
        "parser": _parser_id(),
        "root": str(Path(root).resolve()),
        "files": fingerprint,
        "paks": paks,
        "tables": _tables_to_json(tables),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(document), encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        return


def _reward_to_json(reward: RewardDef) -> dict:
    return {
        "reward_id": reward.reward_id,
        "choice": reward.choice,
        "kind": reward.kind,
        "body": reward.body,
    }


def _reward_from_json(payload: dict) -> RewardDef:
    return RewardDef(
        reward_id=str(payload["reward_id"]),
        choice=str(payload.get("choice") or ""),
        kind=str(payload.get("kind") or ""),
        body=dict(payload.get("body") or {}),
    )


def _tables_to_json(tables: GameTables) -> dict:
    missions = {}
    for mission_id, info in tables.missions.items():
        missions[mission_id] = {
            "mission_id": info.mission_id,
            "finals": [[version, progress] for version, progress in info.finals],
            "rewards": {key: _reward_to_json(reward) for key, reward in info.rewards.items()},
            "stages": [
                {
                    "reward_id": stage.reward_id,
                    "stage_progress": stage.stage_progress,
                    "dialog": stage.dialog,
                    "versions": [[version, progress] for version, progress in stage.versions],
                }
                for stage in info.stages
            ],
            "requires_missions": list(info.requires_missions),
            "requires_flags": list(info.requires_flags),
            "source": info.source,
        }
    return {
        "missions": missions,
        "products": dict(tables.products),
        "substances": dict(tables.substances),
        "tech_ids": sorted(tables.tech_ids),
        "reward_table": {key: _reward_to_json(reward) for key, reward in tables.reward_table.items()},
        "max_version": tables.max_version,
        "stale": tables.stale,
        "warning": tables.warning,
        "files_match": tables.files_match,
        "corvette_parts": list(tables.corvette_parts),
        "corvette_stack_cap": tables.corvette_stack_cap,
        "corvette_needs_reread": tables.corvette_needs_reread,
    }


def _tables_from_json(payload: dict) -> GameTables:
    missions = {}
    for mission_id, info in dict(payload.get("missions") or {}).items():
        missions[str(mission_id)] = MissionInfo(
            mission_id=str(info.get("mission_id") or mission_id),
            finals=[(int(version), int(progress)) for version, progress in info.get("finals") or []],
            rewards={key: _reward_from_json(reward) for key, reward in dict(info.get("rewards") or {}).items()},
            stages=[
                StageReward(
                    reward_id=str(stage.get("reward_id") or ""),
                    stage_progress=None if stage.get("stage_progress") is None else int(stage["stage_progress"]),
                    dialog=bool(stage.get("dialog")),
                    versions=[(int(version), int(progress)) for version, progress in stage.get("versions") or []],
                )
                for stage in info.get("stages") or []
            ],
            requires_missions=[str(item) for item in info.get("requires_missions") or []],
            requires_flags=[str(item) for item in info.get("requires_flags") or []],
            source=str(info.get("source") or ""),
        )
    products = {str(key): int(value) for key, value in dict(payload.get("products") or {}).items()}
    substances = {str(key): int(value) for key, value in dict(payload.get("substances") or {}).items()}
    reward_table = {
        str(key): _reward_from_json(reward) for key, reward in dict(payload.get("reward_table") or {}).items()
    }
    return GameTables(
        missions=missions,
        products=products,
        substances=substances,
        tech_ids={str(item) for item in payload.get("tech_ids") or []},
        reward_table=reward_table,
        max_version=int(payload.get("max_version") or 0),
        stale=bool(payload.get("stale")),
        warning=str(payload.get("warning") or ""),
        files_match=bool(payload.get("files_match")),
        corvette_parts=list(payload.get("corvette_parts") or []),
        corvette_stack_cap=int(payload.get("corvette_stack_cap") or 0),
        corvette_needs_reread=bool(payload.get("corvette_needs_reread")),
    )


def _caret(text: str | None) -> str:
    """Mission and item ids in extracted MXML often omit the leading ^."""
    if not text:
        return ""
    cleaned = text.strip()
    if not cleaned or cleaned.startswith("^"):
        return cleaned
    return "^" + cleaned


def _file_kind(source: str) -> str:
    name = Path(source).name.lower()
    if "substance" in name:
        return "substance"
    if "product" in name:
        return "product"
    return ""


def _absorb(root: ET.Element, tables: GameTables, source: str) -> None:
    parents = _parent_map(root)
    file_kind = _file_kind(source)
    for elem in root.iter():
        mission_id = _caret(_direct(elem, "MissionID"))
        if mission_id and _has_child(elem, "FinalStageVersions"):
            info = tables.missions.setdefault(mission_id, MissionInfo(mission_id=mission_id, source=source))
            info.finals = _finals(elem)
            info.rewards.update(_reward_defs(elem))
            info.stages = _stage_rewards(elem, parents)
            info.requires_missions = _completed_conditions(elem)
            info.requires_flags = _flag_conditions(elem)
        _absorb_item(elem, tables, file_kind)
        tech = _direct(elem, "TechId") or _direct(elem, "TechnologyID")
        if tech:
            tables.tech_ids.add(_caret(tech))
    if "rewardtable" in Path(source).name.lower():
        for reward_id, reward in _reward_defs(root).items():
            tables.reward_table.setdefault(reward_id, reward)


def _absorb_item(elem: ET.Element, tables: GameTables, file_kind: str) -> None:
    multiplier = _direct(elem, "StackMultiplier")
    if multiplier is None:
        return
    try:
        value = int(float(multiplier))
    except ValueError:
        return
    item_id = _direct(elem, "ID") or _direct(elem, "Id")
    if item_id and _looks_like_item(elem):
        marked = _caret(item_id)
        if _item_kind(elem, file_kind) == "substance":
            tables.substances[marked] = value
        else:
            tables.products[marked] = value
    substance = _direct(elem, "SubstanceId") or _direct(elem, "SubstanceID")
    if substance:
        tables.substances[_caret(substance)] = value


def _item_kind(elem: ET.Element, file_kind: str) -> str:
    if file_kind in {"substance", "product"}:
        return file_kind
    blob = f"{elem.attrib.get('name') or ''} {elem.attrib.get('value') or ''}"
    if "GcRealitySubstanceData" in blob or "SubstanceData" in blob:
        return "substance"
    return "product"


def _looks_like_item(elem: ET.Element) -> bool:
    names = {child.attrib.get("name") for child in list(elem)}
    return "StackMultiplier" in names and ("ID" in names or "Id" in names)


def _has_child(elem: ET.Element, name: str) -> bool:
    return any(child.attrib.get("name") == name for child in list(elem))


def _direct(elem: ET.Element, name: str) -> str | None:
    for child in list(elem):
        if child.attrib.get("name") == name and "value" in child.attrib:
            return child.attrib["value"]
    return None


def _finals(elem: ET.Element) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for child in list(elem):
        if child.attrib.get("name") != "FinalStageVersions":
            continue
        for row in list(child):
            version = _direct(row, "Version")
            progress = _direct(row, "Progress")
            if version is None or progress is None:
                continue
            try:
                found.append((int(version), int(progress)))
            except ValueError:
                continue
    return found


def _reward_defs(elem: ET.Element) -> dict[str, RewardDef]:
    found: dict[str, RewardDef] = {}
    for node in elem.iter():
        reward_id = _direct(node, "Id")
        if not reward_id or not reward_id.startswith("R_"):
            continue
        kind, body = _reward_body(node)
        if not kind:
            continue
        choice = str(body.get("choice") or _direct(node, "RewardChoice") or "GiveAll")
        found[reward_id] = RewardDef(reward_id=reward_id, choice=choice, kind=kind, body=body)
    return found


_SKIP_REWARD_KINDS = {"GcRewardTableItem", "GcRewardTableItemList", "GcGenericRewardTableEntry"}


def _reward_kind(value: str) -> str:
    kind = value.split(".", 1)[0]
    if not kind.startswith("GcReward"):
        return ""
    if kind in _SKIP_REWARD_KINDS or kind.endswith("List") or "TableItem" in kind:
        return ""
    return kind


def _reward_body(row: ET.Element) -> tuple[str, dict]:
    """Read the reward inside List → List → GcRewardTableItem, and the flat test shape."""
    parents = _parent_map(row)
    parts: list[dict] = []
    choice = _direct(row, "RewardChoice") or "GiveAll"
    for node in row.iter():
        kind = _reward_kind(node.attrib.get("value") or "")
        if not kind:
            continue
        if _has_same_kind_child(node, kind):
            continue
        picked = _choice_for(node, parents, choice)
        if picked:
            choice = picked
        fields = _fields_of(node, kind)
        parts.append(
            {
                "kind": kind,
                "chance": _chance_for(node, parents),
                "amount_min": _optional_int(_field(fields, node, "AmountMin")),
                "amount_max": _optional_int(_field(fields, node, "AmountMax")),
                "currency": _currency(fields) or _currency(node),
                "ids": _ids(fields) or _ids(node),
                "entries": _listed_items(fields) or _listed_items(node),
            }
        )
    if not parts:
        return "", {}
    return parts[0]["kind"], {
        "parts": parts,
        "chance": _optional_int(_direct(row, "PercentageChance")),
        "choice": choice,
    }


def _choice_for(node: ET.Element, parents: dict[int, ET.Element], fallback: str) -> str:
    current: ET.Element | None = node
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        choice = _direct(current, "RewardChoice")
        if choice:
            return choice
        current = parents.get(id(current))
    return fallback


def _chance_for(node: ET.Element, parents: dict[int, ET.Element]) -> int | None:
    direct = _optional_int(_direct(node, "PercentageChance") or node.attrib.get("chance"))
    if direct is not None:
        return direct
    current = parents.get(id(node))
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        raw = _direct(current, "PercentageChance")
        if raw is not None:
            return _optional_int(raw)
        current = parents.get(id(current))
    return None


def _optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _has_same_kind_child(node: ET.Element, kind: str) -> bool:
    """The outer reward node wraps a second node of the same type. Read that one."""
    for child in list(node):
        child_kind = _reward_kind(child.attrib.get("value") or "")
        if child_kind == kind:
            return True
    return False


def _fields_of(node: ET.Element, kind: str) -> ET.Element:
    """Amount and currency sit in the child named after the reward type."""
    for child in list(node):
        name = (child.attrib.get("name") or "").split(".", 1)[0]
        if name == kind:
            return child
    return node


def _field(preferred: ET.Element, fallback: ET.Element, name: str) -> str | None:
    found = _direct(preferred, name)
    if found is not None:
        return found
    if preferred is not fallback:
        return _direct(fallback, name)
    return None


def _currency(node: ET.Element) -> str:
    """Currency is either Units itself, or GcCurrency with Units nested inside."""
    for sub in node.iter():
        if sub.attrib.get("name") != "Currency":
            continue
        value = (sub.attrib.get("value") or "").split(".", 1)[0]
        if not value:
            continue
        if value == "GcCurrency":
            nested = _direct(sub, "Currency")
            if nested:
                return nested.split(".", 1)[0]
            continue
        return value
    return ""


def _ids(node: ET.Element) -> list[str]:
    found: list[str] = []
    names = {
        "ID",
        "Id",
        "ProductId",
        "ProductIds",
        "SubstanceId",
        "SubstanceID",
        "TechId",
        "TechnologyID",
    }
    for sub in node.iter():
        if sub is node:
            continue
        if sub.attrib.get("name") in names and sub.attrib.get("value") and sub.attrib["value"] not in found:
            found.append(sub.attrib["value"])
    return found


def _listed_items(node: ET.Element) -> list[dict]:
    """Items/Items rows. Each entry has its own Id and Amount."""
    found: list[dict] = []
    seen: set[int] = set()
    for sub in node.iter():
        if sub.attrib.get("name") != "Items":
            continue
        for entry in list(sub):
            if id(entry) in seen:
                continue
            item_id = _direct(entry, "Id") or _direct(entry, "ID") or _direct(entry, "ProductId")
            if not item_id:
                continue
            seen.add(id(entry))
            amount = _optional_int(_direct(entry, "Amount"))
            if amount is None:
                amount = _optional_int(_direct(entry, "AmountMin"))
            found.append({"id": item_id, "amount": amount})
    return found


def _parent_map(root: ET.Element) -> dict[int, ET.Element]:
    parents: dict[int, ET.Element] = {}
    stack: list[tuple[ET.Element, ET.Element | None]] = [(root, None)]
    while stack:
        node, parent = stack.pop()
        if parent is not None:
            parents[id(node)] = parent
        children = list(node)
        for child in reversed(children):
            stack.append((child, node))
    return parents


def _stage_rewards(elem: ET.Element, parents: dict[int, ET.Element]) -> list[StageReward]:
    found: list[StageReward] = []
    for node in elem.iter():
        if node.attrib.get("name") != "Reward":
            continue
        reward_id = node.attrib.get("value") or ""
        if not reward_id.startswith("R_"):
            continue
        versions = _stage_versions(node, parents)
        progress = None if versions else _nearest_progress(node, parents)
        if progress is None and not versions:
            progress = _stage_index(node, parents)
        found.append(
            StageReward(
                reward_id=reward_id,
                stage_progress=progress,
                dialog=_under_dialog(node, parents),
                versions=versions,
            )
        )
    return found


def _stage_versions(node: ET.Element, parents: dict[int, ET.Element]) -> list[tuple[int, int]]:
    """Nearest Versions list on the stage: game version -> progress number."""
    current: ET.Element | None = node
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for child in list(current):
            if child.attrib.get("name") != "Versions":
                continue
            rows = _version_rows(child)
            if rows:
                return rows
        current = parents.get(id(current))
    return []


def _version_rows(node: ET.Element) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for row in list(node):
        version = _direct(row, "Version")
        progress = _direct(row, "Progress")
        if version is None or progress is None:
            continue
        try:
            found.append((int(float(version)), int(float(progress))))
        except ValueError:
            continue
    return found


def _is_stage_element(elem: ET.Element) -> bool:
    """The stage element, not the inner Stage child.

    Real files name that element Stages as well, with value GcGenericMissionStage.
    """
    value = (elem.attrib.get("value") or "").split(".", 1)[0]
    if value == "GcGenericMissionStage":
        return True
    if elem.attrib.get("name") != "Stages":
        return False
    for child in list(elem):
        if child.attrib.get("name") in {"Stage", "Versions"}:
            return True
    return False


def _stage_index(node: ET.Element, parents: dict[int, ET.Element]) -> int | None:
    """Place of the stage element under the Stages list.

    Counting the inner Stage child always returns 1, because Versions is child 0.
    """
    current: ET.Element | None = node
    seen: set[int] = set()
    stage_elem: ET.Element | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _is_stage_element(current):
            stage_elem = current
            break
        current = parents.get(id(current))
    if stage_elem is None:
        return None
    parent = parents.get(id(stage_elem))
    if parent is None or parent.attrib.get("name") != "Stages":
        return None
    siblings = [child for child in list(parent) if _is_stage_element(child)]
    try:
        return siblings.index(stage_elem)
    except ValueError:
        return None


def _nearest_progress(node: ET.Element, parents: dict[int, ET.Element]) -> int | None:
    current: ET.Element | None = node
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        raw = _direct(current, "Progress")
        if raw is not None:
            try:
                return int(float(raw))
            except ValueError:
                return None
        current = parents.get(id(current))
    return None


def _under_dialog(node: ET.Element, parents: dict[int, ET.Element]) -> bool:
    current: ET.Element | None = node
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        blob = (current.attrib.get("name") or "") + (current.attrib.get("value") or "")
        if "Dialog" in blob or "Puzzle" in blob or "Interaction" in blob:
            return True
        current = parents.get(id(current))
    return False


_FLAG_NAMES = {
    "HasDiscoveredPurpleSystems": "Kg6",
    "BuildersKnown": "cPt",
}


def _flag_conditions(elem: ET.Element) -> list[str]:
    """Boolean flags named under StartingConditions. Mission-completed checks are not flags."""
    found: list[str] = []
    for node in elem.iter():
        name = node.attrib.get("name") or ""
        value = node.attrib.get("value") or ""
        if name != "StartingConditions" and "StartingConditions" not in value:
            continue
        for inner in node.iter():
            if inner is node:
                continue
            blob = f"{inner.attrib.get('name') or ''} {inner.attrib.get('value') or ''}"
            if "MissionCompleted" in blob:
                continue
            for label, key in _FLAG_NAMES.items():
                if label in blob and key not in found:
                    found.append(key)
    return found


def _completed_conditions(elem: ET.Element) -> list[str]:
    """Only MissionCompleted nodes under StartingConditions. Later stages are not prerequisites."""
    found: list[str] = []
    own = _caret(_direct(elem, "MissionID"))
    for node in elem.iter():
        name = node.attrib.get("name") or ""
        value = node.attrib.get("value") or ""
        if name != "StartingConditions" and "StartingConditions" not in value:
            continue
        for inner in node.iter():
            blob = (inner.attrib.get("value") or "") + (inner.attrib.get("name") or "")
            if "MissionCompleted" not in blob:
                continue
            mission_id = _caret(_direct(inner, "MissionID"))
            if mission_id and mission_id not in found and mission_id != own:
                found.append(mission_id)
    return found

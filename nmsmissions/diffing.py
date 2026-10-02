"""Compare mission progress, chain position, and the known story flags."""

from __future__ import annotations

import re
from pathlib import Path

from nmsmissions.chains import (
    ChainDef,
    ChainView,
    OtherView,
    StepView,
    build_views,
    flatten_chains,
    friendly_cell,
)
from nmsmissions.saveio import FLAG_NAMES, LoadedSave, PlayerContext, require_context

PURPLE_FLAG_NAMES = (
    "PurpleSystemsUnlocked",
    "HasDiscoveredPurpleSystems",
    "FirstPurpleSystemUA",
    "HasGalacticMapRequestFirstPurple",
    "HasGalacticMapRequestAllPurples",
)
_SLOT = re.compile(r"save(\d+)", re.IGNORECASE)
_GALAXIES = {
    0: "Euclid",
    1: "Hilbert Dimension",
    2: "Calypso",
    3: "Hesperius Dimension",
    4: "Hyades",
    5: "Ickjamatew",
    6: "Budullangr",
    7: "Kikolgallr",
    8: "Eltiensleen",
    9: "Eissentam",
}


def _next_label(chain: ChainView) -> str:
    if chain.next_step is None:
        return "(chain complete)"
    blocked = ""
    if chain.blocked_by:
        blocked = " blocked until " + ", ".join(chain.blocked_by)
    return (
        f"{_step_name(chain.next_step)} ({chain.next_step.status_label}){blocked}"
    )


def _step_name(step: StepView) -> str:
    return f"{step.mission_id}  {friendly_cell(step.title)}"


def _progress(step_progress: int | None) -> str:
    if step_progress is None:
        return "absent"
    if isinstance(step_progress, int) and step_progress < 0:
        return "not started"
    return str(step_progress)


def diff_saves(
    left: LoadedSave,
    right: LoadedSave,
    context: str,
    chains: list[ChainDef],
    catalog: dict[str, int],
    names: dict[str, str],
    subtitles: dict[str, str] | None = None,
) -> str:
    """Differences only. Story flags are always included."""
    title_subs = subtitles or {}
    left_ctx = require_context(left, context)
    right_ctx = require_context(right, context)
    left_roots, left_other = build_views(
        chains, left_ctx.missions, catalog, left_ctx.current_mission, names, title_subs
    )
    right_roots, right_other = build_views(
        chains, right_ctx.missions, catalog, right_ctx.current_mission, names, title_subs
    )
    lines: list[str] = [
        f"Comparing {left_ctx.name}",
        f"  {left.path}",
        f"  {right.path}",
        "",
    ]
    lines.extend(_identity_lines(left, right, left_ctx, right_ctx))
    lines.append("")
    body: list[str] = []
    body.extend(_purple_lines(left, right))
    body.extend(_flag_lines(left, right))
    body.extend(_chain_lines(left_roots, right_roots))
    body.extend(_other_lines(left_other, right_other))
    if not body:
        lines.append("No mission, chain, or story-flag differences.")
    else:
        lines.extend(body)
    lines.append("")
    lines.append("Story flags are part of this diff. Version 1 does not dump the rest of the save.")
    return "\n".join(lines).rstrip() + "\n"


def _flag_lines(left: LoadedSave, right: LoadedSave) -> list[str]:
    wanted = set(FLAG_NAMES) - set(PURPLE_FLAG_NAMES)
    left_map = {(hit.name, hit.path): hit.value for hit in left.flags if hit.name in wanted}
    right_map = {(hit.name, hit.path): hit.value for hit in right.flags if hit.name in wanted}
    lines: list[str] = []
    for key in sorted(set(left_map) | set(right_map)):
        before = left_map.get(key, "<absent>")
        after = right_map.get(key, "<absent>")
        if before == after:
            continue
        name, path = key
        lines.append(f"{name} [{path}]: {before} -> {after}")
    return lines


def _chain_lines(left_roots: list[ChainView], right_roots: list[ChainView]) -> list[str]:
    left_chains = {chain.chain_id: chain for chain in flatten_chains(left_roots)}
    right_chains = {chain.chain_id: chain for chain in flatten_chains(right_roots)}
    lines: list[str] = []
    for chain_id in left_chains:
        left = left_chains[chain_id]
        right = right_chains[chain_id]
        header: list[str] = []
        left_next = _next_label(left)
        right_next = _next_label(right)
        if left_next != right_next:
            header.append(f"  next: {left_next} -> {right_next}")
        if left.blocked_by != right.blocked_by:
            header.append(
                "  blocked: "
                f"{', '.join(left.blocked_by) or '(not blocked)'} -> "
                f"{', '.join(right.blocked_by) or '(not blocked)'}"
            )
        step_lines: list[str] = []
        for left_step, right_step in zip(left.steps, right.steps):
            if (left_step.status, left_step.progress) == (right_step.status, right_step.progress):
                continue
            step_lines.append(
                f"  {_step_name(left_step)}: {left_step.status_label} {_progress(left_step.progress)}"
                f" -> {right_step.status_label} {_progress(right_step.progress)}"
            )
        if header or step_lines:
            lines.append(left.title)
            lines.extend(header)
            lines.extend(step_lines)
    return lines


def _other_lines(left: OtherView, right: OtherView) -> list[str]:
    left_steps = {step.mission_id: step for step in left.steps}
    right_steps = {step.mission_id: step for step in right.steps}
    lines: list[str] = []
    for mission_id in sorted(set(left_steps) | set(right_steps)):
        before = left_steps.get(mission_id)
        after = right_steps.get(mission_id)
        left_text = "absent" if before is None else f"{before.status_label} {_progress(before.progress)}"
        right_text = "absent" if after is None else f"{after.status_label} {_progress(after.progress)}"
        if left_text == right_text:
            continue
        named = before if before is not None else after
        name = friendly_cell(named.title if named is not None else None)
        lines.append(f"Other {mission_id}  {name}: {left_text} -> {right_text}")
    return lines


def save_slot(path: Path) -> int | None:
    """save.hg, save1, and save2 are slot 1. save3/save4 are slot 2, and so on."""
    if path.name.lower() in {"save.hg", "mf_save.hg"}:
        return 1
    match = _SLOT.search(path.name)
    if not match:
        return None
    number = int(match.group(1))
    if number < 1:
        return None
    return (number + 1) // 2


def _galaxy(index: int | None) -> str:
    if index is None:
        return "unknown"
    name = _GALAXIES.get(index)
    return f"{index} ({name})" if name else str(index)


def _hours(value: int) -> float:
    if value > 10_000_000:
        return value / 3_600_000
    return value / 3600


def _play_text(value: int | None) -> str:
    if value is None:
        return "unknown"
    unit = "ms" if value > 10_000_000 else "s"
    return f"{value} {unit} (~{_hours(value):.1f} h)"


def _identity_lines(
    left: LoadedSave,
    right: LoadedSave,
    left_ctx: PlayerContext,
    right_ctx: PlayerContext,
) -> list[str]:
    left_slot = save_slot(left.path)
    right_slot = save_slot(right.path)
    lines = [
        "Save check",
        (
            f"  {left.path.name}: slot {left_slot if left_slot is not None else 'unknown'}, "
            f"galaxy {_galaxy(left_ctx.reality_index)}, play time {_play_text(left.total_play_time)}"
        ),
        (
            f"  {right.path.name}: slot {right_slot if right_slot is not None else 'unknown'}, "
            f"galaxy {_galaxy(right_ctx.reality_index)}, play time {_play_text(right.total_play_time)}"
        ),
    ]
    reasons: list[str] = []
    if left_slot is not None and right_slot is not None and left_slot != right_slot:
        reasons.append(
            f"save slot {left_slot} vs {right_slot} (save1/2 = slot 1, save3/4 = slot 2)"
        )
    if left_ctx.reality_index != right_ctx.reality_index:
        reasons.append(
            f"galaxy {_galaxy(left_ctx.reality_index)} vs {_galaxy(right_ctx.reality_index)}"
        )
    if _play_far(left.total_play_time, right.total_play_time):
        reasons.append("play time differs by more than an hour")
    if reasons:
        lines.append("warning: these look like different games: " + "; ".join(reasons))
    return lines


def _play_far(left: int | None, right: int | None) -> bool:
    if left is None and right is None:
        return False
    if left is None or right is None:
        return True
    return abs(_hours(left) - _hours(right)) > 1


def _purple_lines(left: LoadedSave, right: LoadedSave) -> list[str]:
    wanted = set(PURPLE_FLAG_NAMES)
    left_map = {(hit.name, hit.path): hit.value for hit in left.flags if hit.name in wanted}
    right_map = {(hit.name, hit.path): hit.value for hit in right.flags if hit.name in wanted}
    lines = ["Purple flags"]
    keys = sorted(set(left_map) | set(right_map))
    if not keys:
        lines.append("  (none found)")
        return lines
    for key in keys:
        name, path = key
        before = left_map.get(key, "<absent>")
        after = right_map.get(key, "<absent>")
        if before == after:
            lines.append(f"  {name} [{path}]: {before}")
        else:
            lines.append(f"  {name} [{path}]: {before} -> {after}")
    return lines


_SAVE_NAME = re.compile(r"^save(\d+)\.hg$", re.IGNORECASE)
_PLAIN_SAVE = re.compile(r"^save\.hg$", re.IGNORECASE)


def choose_save_file(path: Path, save_slot_number: int | None = None, auto: bool = True) -> tuple[Path, str | None]:
    """Pick a Steam save file.

    --save-slot N reads save(2N-1).hg and save(2N).hg and keeps the newer
    mtime. Slot 1 also accepts save.hg. Otherwise, when auto is on and the
    path is a save file, a newer paired file in the same folder is used.

    A working copy already names the file taken from the live slot. A newer
    sibling left in that folder is not the file the game loads.
    """
    path = Path(path)
    if path.is_file() and (path.parent / "state.json").is_file():
        return path, None
    if save_slot_number is not None:
        if save_slot_number < 1:
            raise ValueError("Save slot must be 1 or greater.")
        folder = path if path.is_dir() else path.parent
        first, second = save_slot_number * 2 - 1, save_slot_number * 2
        candidates = [_find_save(folder, first), _find_save(folder, second)]
        if save_slot_number == 1:
            candidates.append(_find_plain_save(folder))
        chosen = _newer_file(candidates, prefer=second)
        if chosen is None:
            extra = " or save.hg" if save_slot_number == 1 else ""
            raise ValueError(
                f"No save{first}.hg or save{second}.hg{extra} in {folder} for slot {save_slot_number}."
            )
        return chosen, f"save slot {save_slot_number}: using {chosen.name} (newer mtime)"
    if not auto or path.is_dir():
        return path, None
    if _PLAIN_SAVE.match(path.name):
        newer = _newer_than(path, [_find_save(path.parent, 1), _find_save(path.parent, 2)])
        if newer is None:
            return path, None
        return newer, f"using {newer.name} (newer than {path.name})"
    if not _SAVE_NAME.match(path.name):
        return path, None
    match = _SAVE_NAME.match(path.name)
    if match is None:
        return path, None
    number = int(match.group(1))
    pair = number + 1 if number % 2 == 1 else number - 1
    siblings = [_find_save(path.parent, pair)]
    if number in {1, 2}:
        siblings.append(_find_plain_save(path.parent))
    newer = _newer_than(path, siblings)
    if newer is None:
        return path, None
    return newer, f"using {newer.name} (newer than {path.name})"


def _find_plain_save(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    direct = folder / "save.hg"
    if direct.is_file():
        return direct
    for child in folder.iterdir():
        if child.is_file() and child.name.lower() == "save.hg":
            return child
    return None


def _newer_than(path: Path, siblings: list[Path | None]) -> Path | None:
    newer = [item for item in siblings if item is not None and item.is_file() and item.resolve() != path.resolve()]
    if not newer:
        return None
    best = max(newer, key=lambda item: item.stat().st_mtime_ns)
    if best.stat().st_mtime_ns <= path.stat().st_mtime_ns:
        return None
    return best


def _find_save(folder: Path, number: int) -> Path | None:
    if not folder.is_dir():
        return None
    target = f"save{number}.hg"
    direct = folder / target
    if direct.is_file():
        return direct
    for child in folder.iterdir():
        if child.is_file() and child.name.lower() == target:
            return child
    return None


def _newer_file(candidates: list[Path | None], prefer: int) -> Path | None:
    existing = [path for path in candidates if path is not None and path.is_file()]
    if not existing:
        return None
    newest = max(path.stat().st_mtime_ns for path in existing)
    tied = [path for path in existing if path.stat().st_mtime_ns == newest]
    if len(tied) == 1:
        return tied[0]
    preferred = f"save{prefer}.hg"
    for path in tied:
        if path.name.lower() == preferred:
            return path
    return tied[-1]

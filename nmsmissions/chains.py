"""Chain order, prerequisites, and the indented progression view.

The sequence lives in data/chains.yaml. Code loads that file and does not
hard-code quest order.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

from nmsmissions.catalog import DATA_DIR

CHAINS_PATH = DATA_DIR / "chains.yaml"
NAMES_PATH = DATA_DIR / "names.yaml"

STATUS_DONE = "done"
STATUS_PROGRESS = "in_progress"
STATUS_NOT_STARTED = "not_started"
STATUS_UNKNOWN = "unknown"
STATUS_REACHED = "reached"
_TOKEN = re.compile(r"%[^%\s]+%")
_SLASH = re.compile(r"%SLASH%|\bSLASH\b")
# Game class names are not log titles. GcNumberedTextList is one of these.
_CLASS_NAME = re.compile(r"^(?:Gc|Tk|cTk)[A-Z]\w*$")


def is_class_name(text: str | None) -> bool:
    """True for a type or class name such as GcNumberedTextList, TkFoo, or cTkFoo."""
    if not isinstance(text, str):
        return False
    return _CLASS_NAME.fullmatch(text.strip()) is not None


def is_generated_title(text: str | None) -> bool:
    """A tidied id or a chain step label, not a log title from the game."""
    if not isinstance(text, str):
        return True
    stripped = text.strip()
    if not stripped or stripped == "-":
        return True
    if stripped.endswith("(id)") or is_class_name(stripped):
        return True
    return " - step " in stripped


def is_lore_step(mission_id: str) -> bool:
    """Codex rows. They are not the story step that finishes a chain."""
    return "LORE" in mission_id


WIKI_SITE = "https://nomanssky.fandom.com/wiki/"
WIKI_SEARCH = "https://nomanssky.fandom.com/wiki/Special:Search?query="


@dataclass(frozen=True)
class StepDef:
    mission_id: str
    title: str | None = None
    optional: bool = False
    wiki: str | None = None
    wiki_confirmed: bool = False


@dataclass(frozen=True)
class ChainDef:
    chain_id: str
    title: str
    group: str
    requires: tuple[str, ...]
    steps: tuple[StepDef, ...]
    note: str | None = None
    caution: str | None = None
    wiki: str | None = None
    wiki_confirmed: bool = False


@dataclass
class StepView:
    mission_id: str
    title: str | None
    progress: int | None
    complete: int | None
    status: str
    status_label: str
    tracked: bool
    optional: bool
    is_next: bool
    before_id: str | None
    after_id: str | None
    before_status: str | None
    after_status: str | None
    raw: dict | None = None
    wiki: str = ""


@dataclass
class ChainView:
    chain_id: str
    title: str
    group: str
    depth: int
    note: str | None
    caution: str | None
    requires: list[tuple[str, str, bool]]
    unlocks: list[str]
    blocked_by: list[str]
    complete: bool
    done_count: int = 0
    required_count: int = 0
    state_label: str = ""
    steps: list[StepView] = field(default_factory=list)
    next_step: StepView | None = None
    children: list[ChainView] = field(default_factory=list)
    wiki: str = ""
    wiki_confirmed: bool = False


@dataclass
class OtherView:
    steps: list[StepView]


def wiki_page(title: str) -> str:
    """Turn a page title into a No Man's Sky Wiki URL. Spaces become underscores."""
    page = "_".join(title.split())
    return WIKI_SITE + quote(page, safe="_-'()")


def wiki_search(title: str) -> str:
    return WIKI_SEARCH + quote(title)


def mission_wiki(explicit: str | None, display_title: str | None, chain_title: str, mission_id: str) -> str:
    """A mission uses its own page when one is set. Otherwise it searches by title."""
    if explicit:
        return explicit
    title = (display_title or "").strip()
    generated = (not title) or title.endswith("(id)") or " - step " in title
    query = chain_title if generated else title
    return wiki_search(query or mission_id)


def chain_wiki(chain: ChainDef) -> tuple[str, bool]:
    if chain.wiki:
        return chain.wiki, chain.wiki_confirmed
    return wiki_page(chain.title), False


def lookup_wiki(text: str, chains: list[ChainDef], names: dict[str, str] | None = None) -> tuple[str, str]:
    """Resolve a mission id or chain id to a URL and a short note."""
    titles = names or {}
    wanted = text[1:] if text.startswith("^") else text
    for chain in chains:
        if chain.chain_id == text or chain.chain_id == wanted or chain.title == text:
            url, confirmed = chain_wiki(chain)
            note = "confirmed page" if confirmed else "unconfirmed page"
            return url, note
        for step in chain.steps:
            step_key = step.mission_id[1:] if step.mission_id.startswith("^") else step.mission_id
            if step.mission_id != text and step_key != wanted:
                continue
            if step.wiki:
                note = "confirmed page" if step.wiki_confirmed else "unconfirmed page"
                return step.wiki, note
            display = titles.get(step.mission_id) or titles.get(step_key) or step.title
            return mission_wiki(None, display, chain.title, step.mission_id), "search"
    query = titles.get(text) or titles.get("^" + wanted) or text
    return wiki_search(query), "search"


def open_wiki(url: str, opener=None) -> None:
    """Open a wiki URL in the browser. Tests pass opener so nothing is launched."""
    if opener is None:
        import webbrowser

        opener = webbrowser.open
    opener(url)


def load_names(path: Path | None = None) -> dict[str, str]:
    file_path = path if path is not None else NAMES_PATH
    if not file_path.is_file():
        return {}
    document = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    missions = document.get("missions") or {}
    return {str(key): str(value) for key, value in missions.items()}


def load_chains(path: Path | None = None) -> list[ChainDef]:
    file_path = path if path is not None else CHAINS_PATH
    document = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    rows = document.get("chains")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{file_path} has no chains list.")
    chains: list[ChainDef] = []
    seen: set[str] = set()
    claimed: dict[str, str] = {}
    for row in rows:
        chain_id = str(row["id"])
        if chain_id in seen:
            raise ValueError(f"Duplicate chain id {chain_id}")
        seen.add(chain_id)
        steps: list[StepDef] = []
        for entry in row.get("steps") or []:
            if isinstance(entry, str):
                step = StepDef(mission_id=entry, optional=is_lore_step(entry))
            elif isinstance(entry, dict):
                mission_id = str(entry["id"])
                step = StepDef(
                    mission_id=mission_id,
                    title=entry.get("title"),
                    optional=bool(entry.get("optional", False)) or is_lore_step(mission_id),
                    wiki=entry.get("wiki"),
                    wiki_confirmed=bool(entry.get("wiki_confirmed", False)),
                )
            else:
                raise ValueError(f"Bad step in {chain_id}: {entry!r}")
            owner = claimed.get(step.mission_id)
            if owner is not None:
                raise ValueError(f"{step.mission_id} is in both {owner} and {chain_id}")
            claimed[step.mission_id] = chain_id
            steps.append(step)
        if not steps:
            raise ValueError(f"Chain {chain_id} has no steps.")
        chains.append(
            ChainDef(
                chain_id=chain_id,
                title=str(row["title"]),
                group=str(row.get("group") or "story"),
                requires=tuple(str(item) for item in (row.get("requires") or [])),
                steps=tuple(steps),
                note=row.get("note"),
                caution=row.get("caution"),
                wiki=None if row.get("wiki") is None else str(row.get("wiki")),
                wiki_confirmed=bool(row.get("wiki_confirmed", False)),
            )
        )
    known = {chain.chain_id for chain in chains}
    for chain in chains:
        for requirement in chain.requires:
            if requirement not in known:
                raise ValueError(f"{chain.chain_id} requires unknown chain {requirement}")
            if requirement == chain.chain_id:
                raise ValueError(f"{chain.chain_id} requires itself")
    return chains


RETIRED_PROGRESS = 2147483647


def completion_for(catalog: dict[str, int], mission_id: str) -> int | None:
    """Yaml and extracted finals, whether or not the id has a leading ^."""
    if mission_id in catalog:
        return catalog[mission_id]
    marked = mission_id if mission_id.startswith("^") else "^" + mission_id
    bare = marked[1:]
    if marked in catalog:
        return catalog[marked]
    if bare in catalog:
        return catalog[bare]
    return None


def mission_status(progress: int | None, complete: int | None) -> tuple[str, str]:
    """Return (status code, label).

    Progress -1 is not started, even when the table's completion value is
    also -1. Those branches are optional and must not look "done".
    Progress 2147483647 is a retired row: done, and version 2 will not edit it.
    """
    if progress is None:
        return STATUS_NOT_STARTED, "not started"
    if progress == -1:
        return STATUS_NOT_STARTED, "not started"
    if progress == RETIRED_PROGRESS:
        return STATUS_DONE, "done (retired)"
    if complete is None:
        return STATUS_UNKNOWN, f"unknown/raw {progress}"
    # A table value of 0 cannot tell "started" from "finished".
    if complete == 0:
        return STATUS_REACHED, "reached/unknown"
    if progress >= complete:
        return STATUS_DONE, "done"
    return STATUS_PROGRESS, "in progress"


def _blocks(step: StepDef, complete: int | None) -> bool:
    if step.optional or is_lore_step(step.mission_id) or complete in (-1, 0):
        return False
    return True


def chain_state_label(complete: bool, blocked: bool, advanced: bool, done: int, total: int) -> str:
    """Status word plus how many required steps are done."""
    if complete:
        word = "complete"
    elif advanced:
        word = "in progress"
    elif blocked:
        word = "blocked"
    else:
        word = "not started"
    return f"{word}, {done}/{total} done"


def _progress_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    return int(value)


def build_views(
    chains: list[ChainDef],
    entries: dict[str, dict],
    catalog: dict[str, int],
    current_mission: str | None,
    names: dict[str, str] | None = None,
    subtitles: dict[str, str] | None = None,
    alerts: dict[str, str] | None = None,
) -> tuple[list[ChainView], OtherView]:
    """Build the chain tree and the Other group from one context's missions."""
    titles = names or {}
    title_subs = subtitles or {}
    title_alerts = alerts or {}
    by_id = {chain.chain_id: chain for chain in chains}
    step_status: dict[str, str] = {}
    prepared: dict[str, list[StepView]] = {}
    next_of: dict[str, StepView | None] = {}
    complete_of: dict[str, bool] = {}
    done_of: dict[str, int] = {}
    required_of: dict[str, int] = {}
    advanced_of: dict[str, bool] = {}

    for chain in chains:
        views: list[StepView] = []
        for index, step in enumerate(chain.steps):
            raw = entries.get(step.mission_id)
            progress = None if raw is None else _progress_number(raw.get("Progress"))
            complete = completion_for(catalog, step.mission_id)
            status, label = mission_status(progress, complete)
            step_status[step.mission_id] = status
            title = _lookup_name(step.mission_id, step.title, titles)
            before = chain.steps[index - 1] if index else None
            after = chain.steps[index + 1] if index + 1 < len(chain.steps) else None
            views.append(
                StepView(
                    mission_id=step.mission_id,
                    title=title,
                    progress=progress,
                    complete=complete,
                    status=status,
                    status_label=label,
                    tracked=current_mission == step.mission_id,
                    optional=step.optional or is_lore_step(step.mission_id) or complete in (-1, 0),
                    is_next=False,
                    before_id=None if before is None else before.mission_id,
                    after_id=None if after is None else after.mission_id,
                    before_status=None,
                    after_status=None,
                    raw=raw,
                )
            )
        for view in views:
            if view.before_id is not None:
                view.before_status = step_status.get(view.before_id)
            if view.after_id is not None:
                view.after_status = step_status.get(view.after_id)
        next_step = None
        for view, step in zip(views, chain.steps):
            if _blocks(step, completion_for(catalog, step.mission_id)) and view.status != STATUS_DONE:
                next_step = view
                view.is_next = True
                break
        required = [
            view
            for view, step in zip(views, chain.steps)
            if _blocks(step, completion_for(catalog, step.mission_id))
        ]
        _finish_titles(views, title_subs, chain.title, title_alerts)
        for view, step in zip(views, chain.steps):
            view.wiki = mission_wiki(step.wiki, view.title, chain.title, view.mission_id)
        prepared[chain.chain_id] = views
        next_of[chain.chain_id] = next_step
        complete_of[chain.chain_id] = next_step is None
        done_of[chain.chain_id] = sum(view.status == STATUS_DONE for view in required)
        required_of[chain.chain_id] = len(required)
        advanced_of[chain.chain_id] = any(
            view.status in (STATUS_DONE, STATUS_PROGRESS, STATUS_REACHED) for view in views
        )

    def depth_of(chain_id: str, trail: tuple[str, ...] = ()) -> int:
        if chain_id in trail:
            raise ValueError("Cycle in chain prerequisites: " + " -> ".join(trail + (chain_id,)))
        chain = by_id[chain_id]
        if not chain.requires:
            return 0
        return 1 + max(depth_of(item, trail + (chain_id,)) for item in chain.requires)

    unlocks: dict[str, list[str]] = {chain.chain_id: [] for chain in chains}
    for chain in chains:
        for requirement in chain.requires:
            unlocks[requirement].append(chain.title)

    placed: set[str] = set()

    def make(chain_id: str) -> ChainView:
        chain = by_id[chain_id]
        placed.add(chain_id)
        blocked = [by_id[item].title for item in chain.requires if not complete_of[item]]
        view = ChainView(
            chain_id=chain.chain_id,
            title=chain.title,
            group=chain.group,
            depth=depth_of(chain.chain_id),
            note=chain.note,
            caution=chain.caution,
            requires=[
                (item, by_id[item].title, complete_of[item]) for item in chain.requires
            ],
            unlocks=list(unlocks[chain.chain_id]),
            blocked_by=blocked,
            complete=complete_of[chain.chain_id],
            done_count=done_of[chain.chain_id],
            required_count=required_of[chain.chain_id],
            state_label=chain_state_label(
                complete_of[chain.chain_id],
                bool(blocked),
                advanced_of[chain.chain_id],
                done_of[chain.chain_id],
                required_of[chain.chain_id],
            ),
            steps=prepared[chain.chain_id],
            next_step=next_of[chain.chain_id],
            wiki=chain_wiki(chain)[0],
            wiki_confirmed=chain_wiki(chain)[1],
        )
        for child in chains:
            if child.requires and child.requires[0] == chain_id and child.chain_id not in placed:
                view.children.append(make(child.chain_id))
        return view

    roots = [
        make(chain.chain_id)
        for chain in chains
        if not chain.requires or chain.requires[0] not in by_id
    ]
    # Chains whose first parent was not walked (should not happen) still show.
    for chain in chains:
        if chain.chain_id not in placed:
            roots.append(make(chain.chain_id))

    in_a_chain = {step.mission_id for chain in chains for step in chain.steps}
    other_steps: list[StepView] = []
    for mission_id in sorted(entries):
        if mission_id in in_a_chain:
            continue
        raw = entries[mission_id]
        progress = _progress_number(raw.get("Progress"))
        complete = completion_for(catalog, mission_id)
        status, label = mission_status(progress, complete)
        other_steps.append(
            StepView(
                mission_id=mission_id,
                title=_lookup_name(mission_id, None, titles) or tidy_mission_id(mission_id),
                progress=progress,
                complete=complete,
                status=status,
                status_label=label,
                tracked=current_mission == mission_id,
                optional=False,
                is_next=False,
                before_id=None,
                after_id=None,
                before_status=None,
                after_status=None,
                raw=raw,
                wiki="",
            )
        )
    _disambiguate_other(other_steps)
    for step in other_steps:
        step.wiki = mission_wiki(None, step.title, "Other", step.mission_id)
    return roots, OtherView(steps=other_steps)


def progress_cell(progress: int | None, complete: int | None) -> str:
    """Progress column. -1 is not started, even when a final number is known."""
    if progress is None:
        return "absent"
    if isinstance(progress, int) and progress < 0:
        return "not started"
    if complete is None:
        return str(progress)
    shown = progress
    if isinstance(progress, int) and isinstance(complete, int) and complete > 0 and progress > complete:
        shown = complete
    return f"{shown}/{complete}"


def _step_progress(step: StepView) -> str:
    return progress_cell(step.progress, step.complete)


def _neighbor(mission_id: str | None, status: str | None, fallback: str) -> str:
    if mission_id is None:
        return fallback
    label = {
        STATUS_DONE: "done",
        STATUS_PROGRESS: "in progress",
        STATUS_NOT_STARTED: "not started",
        STATUS_UNKNOWN: "unknown",
        STATUS_REACHED: "reached/unknown",
    }.get(status or "", status or "")
    return f"{mission_id} ({label})" if label else mission_id


def format_forest(roots: list[ChainView], other: OtherView, brief: bool = False) -> str:
    lines: list[str] = []
    lines.append("Story map")
    lines.extend(_map_lines(roots))
    lines.append("")
    last_group = None
    for root in roots:
        if root.group != last_group and root.depth == 0:
            lines.append(_group_heading(root.group))
            last_group = root.group
        lines.extend(_format_chain(root, brief))
        lines.append("")
    lines.append("Other")
    lines.append("  Missions in this save that are not in a known chain.")
    if not other.steps:
        lines.append("  (none)")
    elif brief:
        count = len(other.steps)
        noun = "mission" if count == 1 else "missions"
        lines.append(f"  {count} {noun}. Use list without --brief to see them.")
    else:
        lines.extend(_mission_table(other.steps, "  "))
    return "\n".join(lines).rstrip() + "\n"


def _group_heading(group: str) -> str:
    return {"story": "Story chains", "side": "Side chains", "base": "Base chains"}.get(
        group, group
    )


def _map_lines(roots: list[ChainView]) -> list[str]:
    lines: list[str] = []

    def walk(node: ChainView, indent: int) -> None:
        if node.requires:
            left = " + ".join(title for _cid, title, _done in node.requires)
            lines.append(f"{'  ' * indent}{left} -> {node.title}")
        else:
            lines.append(f"{'  ' * indent}{node.title}")
        for child in node.children:
            walk(child, indent + 1)

    for root in roots:
        if root.group == "story":
            walk(root, 1)
    return lines


def _format_chain(node: ChainView, brief: bool) -> list[str]:
    pad = "  " * node.depth
    lines = [f"{pad}{node.title}  [{node.state_label}]"]
    if node.requires:
        bits = [
            f"{title} ({'done' if done else 'not done'})" for _cid, title, done in node.requires
        ]
        lines.append(f"{pad}  requires: {', '.join(bits)}")
    if node.blocked_by:
        lines.append(f"{pad}  blocked until: {', '.join(node.blocked_by)}")
    if node.unlocks:
        lines.append(f"{pad}  unlocks: {', '.join(node.unlocks)}")
    if node.wiki:
        mark = "" if node.wiki_confirmed else "  (page not confirmed)"
        lines.append(f"{pad}  wiki: {node.wiki}{mark}")
    if node.note:
        lines.append(f"{pad}  note: {node.note}")
    if node.caution:
        lines.append(f"{pad}  caution: {node.caution}")
    if node.next_step is None:
        lines.append(f"{pad}  next: (chain complete)")
    else:
        blocked = ""
        if node.blocked_by:
            blocked = "  blocked until " + ", ".join(node.blocked_by)
        lines.append(
            f"{pad}  >>> NEXT {node.next_step.mission_id}  "
            f"{friendly_cell(node.next_step.title)}{blocked}"
        )
        lines.append(f"{pad}  {_neighborhood(node)}")
    shown = [node.next_step] if brief and node.next_step is not None else node.steps
    if shown and not (brief and node.next_step is None):
        lines.extend(_mission_table(shown, pad + "  "))
    for child in node.children:
        lines.append("")
        lines.extend(_format_chain(child, brief))
    return lines


def _neighborhood(node: ChainView) -> str:
    step = node.next_step
    if step is None:
        return ""
    before = _neighbor(step.before_id, step.before_status, "(start of chain)")
    after = _neighbor(step.after_id, step.after_status, "(end of chain)")
    return f"{before} -> >>> {step.mission_id} ({step.status_label}) -> {after}"


def polish_name(text: str | None) -> str | None:
    """Drop empty text and turn %PLANET% style tokens into an ellipsis."""
    if text is None:
        return None
    cleaned = _SLASH.sub("/", text)
    cleaned = _TOKEN.sub("…", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _lookup_name(mission_id: str, explicit: str | None, titles: dict[str, str]) -> str | None:
    found = explicit or titles.get(mission_id)
    if not found and mission_id.startswith("^"):
        found = titles.get(mission_id[1:])
    elif not found:
        found = titles.get("^" + mission_id)
    if not isinstance(found, str):
        return None
    cleaned = polish_name(found)
    if is_class_name(found) or is_class_name(cleaned) or is_generated_title(cleaned):
        return None
    return cleaned


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _lookup_subtitle(mission_id: str, subtitles: dict[str, str], base: str) -> str | None:
    found = subtitles.get(mission_id)
    if not found and mission_id.startswith("^"):
        found = subtitles.get(mission_id[1:])
    elif not found:
        found = subtitles.get("^" + mission_id)
    if not isinstance(found, str):
        return None
    text = polish_name(found)
    if not text or text == base:
        return None
    return _clip(text, 40)


def _lookup_alert(mission_id: str, alerts: dict[str, str]) -> str | None:
    found = alerts.get(mission_id)
    if not found and mission_id.startswith("^"):
        found = alerts.get(mission_id[1:])
    elif not found:
        found = alerts.get("^" + mission_id)
    if not isinstance(found, str):
        return None
    text = polish_name(found)
    if not text or is_class_name(text) or is_generated_title(text):
        return None
    return text


def _finish_titles(
    views: list[StepView],
    subtitles: dict[str, str],
    chain_title: str,
    alerts: dict[str, str] | None = None,
) -> None:
    """Fill a blank log title, then separate steps that still share a name.

    A chain step with no log title stays "Chain - step N", including after it
    is finished. A notify line from the game files may follow that label.
    The tidied id is only for missions that are not in a chain.
    A title that appears once in the chain is shown as-is. A subtitle is joined on only
    when it makes every copy in this chain unique. Otherwise the copies are
    numbered from 1 in the order they appear.
    """
    notify = alerts or {}
    for index, view in enumerate(views, start=1):
        if view.title and not is_generated_title(view.title):
            continue
        label = f"{chain_title} - step {index}"
        alert = _lookup_alert(view.mission_id, notify)
        view.title = f"{label} — {alert}" if alert else label

    groups: dict[str, list[int]] = {}
    for index, view in enumerate(views):
        if view.title:
            groups.setdefault(view.title, []).append(index)

    for title, indexes in groups.items():
        if len(indexes) < 2:
            continue
        extras = [_lookup_subtitle(views[index].mission_id, subtitles, title) for index in indexes]
        labels = [f"{title} ({extra})" if extra else title for extra in extras]
        if all(extras) and len(set(labels)) == len(labels):
            for index, label in zip(indexes, labels):
                views[index].title = label
            continue
        for offset, index in enumerate(indexes, start=1):
            views[index].title = f"{title} ({offset})"


OTHER_NAME_LIMIT = 60


def _fit_other_name(name: str, marker: str, limit: int = OTHER_NAME_LIMIT) -> str:
    """Keep the whole Other label, id included, inside the character cap."""
    suffix = ""
    if marker and name != marker and not name.endswith("— " + marker):
        suffix = " — " + marker
    full = name + suffix
    if len(full) <= limit:
        return full
    if suffix and len(suffix) + 1 < limit:
        room = limit - len(suffix)
        head = (name[: room - 1].rstrip() + "…") if room >= 2 else "…"
        fitted = head + suffix
        if len(fitted) <= limit:
            return fitted
    return full[: limit - 1].rstrip() + "…"


def _disambiguate_other(steps: list[StepView]) -> None:
    """A repeated Other name gains the tidied id. Every Other name stays within 60 characters."""
    counts = Counter(step.title for step in steps if step.title)
    for step in steps:
        if not step.title:
            step.title = tidy_mission_id(step.mission_id)
        marker = tidy_mission_id(step.mission_id) if counts[step.title] > 1 else ""
        step.title = _fit_other_name(step.title, marker)


def tidy_mission_id(mission_id: str) -> str:
    """^DROPPOD_GUIDE -> 'Droppod Guide (id)' when no log title exists."""
    raw = mission_id[1:] if mission_id.startswith("^") else mission_id
    words = [part.capitalize() for part in raw.replace("-", "_").split("_") if part]
    label = " ".join(words) if words else mission_id
    return f"{label} (id)"


def friendly_cell(title: str | None) -> str:
    """Display text for the name column. Unknown titles stay a dash."""
    if title is None:
        return "-"
    text = title.strip()
    return text if text else "-"


def _mission_table(steps: list[StepView], pad: str) -> list[str]:
    """Mission ID | Friendly Name | Status | Progress, for every mission row."""
    body: list[tuple[str, str, str, str]] = []
    for step in steps:
        status = step.status_label
        if step.is_next:
            status = "NEXT  " + status
        if step.tracked:
            status += " (tracked)"
        if step.optional and not step.is_next:
            status += " optional"
        body.append((step.mission_id, friendly_cell(step.title), status, _step_progress(step)))
    id_width = max([len("Mission ID"), *(len(row[0]) for row in body)])
    name_width = max([len("Friendly Name"), *(len(row[1]) for row in body)])
    status_width = max([len("Status"), *(len(row[2]) for row in body)])
    lines = [
        f"{pad}{'Mission ID'.ljust(id_width)}  {'Friendly Name'.ljust(name_width)}  "
        f"{'Status'.ljust(status_width)}  Progress"
    ]
    for mission_id, name, status, progress in body:
        lines.append(
            f"{pad}{mission_id.ljust(id_width)}  {name.ljust(name_width)}  "
            f"{status.ljust(status_width)}  {progress}"
        )
    return lines


def flatten_chains(roots: list[ChainView]) -> list[ChainView]:
    found: list[ChainView] = []

    def walk(node: ChainView) -> None:
        found.append(node)
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)
    return found

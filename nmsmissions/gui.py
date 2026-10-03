"""Tkinter tree of the same chain view the CLI prints.

With an editor session, Finish and Unlock only open a preview and write only from that window.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

from nmsmissions.chains import (
    STATUS_DONE,
    STATUS_NOT_STARTED,
    STATUS_REACHED,
    ChainView,
    OtherView,
    StepView,
    friendly_cell,
    open_wiki,
    progress_cell,
)
from nmsmissions.timing import record, timed


FINISH_LABEL = "Finish this mission"
UNLOCK_LABEL = "Unlock only (you can still play it)"
UNLOCK_TIP = "Make this mission available, but leave it unfinished so you can still play it."
CHAIN_FINISH_LABEL = "Finish whole quest line"
CHAIN_UNLOCK_LABEL = "Unlock next step"
CHAIN_RESET_LABEL = "Reset whole quest line"
RESET_LABEL = "Reset this mission"
FINISH_TO_LABEL = "Finish up to this step"
RESET_FROM_LABEL = "Reset from this step"
FINISHED_LINE = "This quest line is already finished."
CELL_CHUNK = 40


def prepare_dpi() -> None:
    """Tell Windows the process is DPI-aware before the first Tk window.

    An unaware process is bitmap-stretched, and the banner lines draw at half height.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        return


def apply_tk_scaling(window) -> None:
    try:
        pixels = float(window.winfo_fpixels("1i"))
    except Exception:
        return
    if pixels <= 0:
        return
    try:
        window.tk.call("tk", "scaling", pixels / 72.0)
    except Exception:
        return


def _follow_width(window, *labels) -> None:
    """Wrap banner text to the window. The rows keep their natural height."""

    def apply(_event=None) -> None:
        width = window.winfo_width()
        if width < 80:
            return
        wrap = max(240, width - 32)
        for label in labels:
            try:
                label.configure(wraplength=wrap)
            except Exception:
                return

    window.bind("<Configure>", apply, add="+")
    apply()


_ROW_PX = 24
_CHROME = 220
_MIN_ROWS = 25


def window_settings_path() -> Path:
    """Size and position. NMSMISSIONS_WINDOW_FILE overrides the app folder."""
    override = os.environ.get("NMSMISSIONS_WINDOW_FILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "window.json"


def load_window_settings() -> dict | None:
    path = window_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_window_settings(window, remembered: dict | None = None) -> None:
    """Write width, height, and position. A withdrawn window (under 200px) is skipped."""
    width = height = x = y = 0
    zoomed = False
    if remembered:
        width = int(remembered.get("width") or 0)
        height = int(remembered.get("height") or 0)
        x = int(remembered.get("x") or 0)
        y = int(remembered.get("y") or 0)
        zoomed = bool(remembered.get("zoomed"))
    if width < 200 or height < 200:
        try:
            width = int(window.winfo_width())
            height = int(window.winfo_height())
            x = int(window.winfo_x())
            y = int(window.winfo_y())
            zoomed = str(window.state()) == "zoomed"
        except Exception:
            return
    if width < 200 or height < 200:
        return
    path = window_settings_path()
    payload = {"width": width, "height": height, "x": x, "y": y, "zoomed": zoomed}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def apply_window_size(window) -> None:
    """About 85% of the screen, at least ~25 rows, or the size saved last time.

    The window stays resizable and has no maximum, so maximize still works.
    """
    window.resizable(True, True)
    try:
        screen_w = int(window.winfo_screenwidth())
        screen_h = int(window.winfo_screenheight())
    except Exception:
        return
    screen_w = max(screen_w, 1)
    screen_h = max(screen_h, 1)
    window.minsize(min(640, screen_w), min(420, screen_h))
    saved = load_window_settings()
    if saved and saved.get("width") and saved.get("height"):
        width = max(200, min(int(saved["width"]), screen_w))
        height = max(200, min(int(saved["height"]), screen_h))
        x = int(saved.get("x") or 0)
        y = int(saved.get("y") or 0)
        x = max(0, min(x, max(screen_w - 80, 0)))
        y = max(0, min(y, max(screen_h - 80, 0)))
        window.geometry(f"{width}x{height}+{x}+{y}")
        if saved.get("zoomed"):
            try:
                window.state("zoomed")
            except Exception:
                pass
        return
    height = min(max(int(screen_h * 0.85), _MIN_ROWS * _ROW_PX + _CHROME), max(screen_h - 40, 420))
    width = min(max(int(screen_w * 0.72), 1100), screen_w)
    window.geometry(f"{width}x{height}")


def _fresh_running(window, callback, banner=None) -> None:
    """Call callback with a new running-game check. The status cache is not read.

    tasklist runs on a worker. The window shows a short wait line until it returns.
    """
    from nmsmissions.edit import nms_is_running, note_nms_running

    if banner is not None:
        _keep_banner(banner, "Checking the game is closed…", None)
    if os.environ.get("NMSMISSIONS_NMS_RUNNING") == "1":
        note_nms_running(True)
        callback(True)
        return
    box: dict = {}

    def work() -> None:
        running = nms_is_running()
        note_nms_running(running)
        box["running"] = running

    threading.Thread(target=work, name="nms-running", daemon=True).start()

    def poll() -> None:
        if "running" not in box:
            try:
                window.after(30, poll)
            except Exception:
                return
            return
        callback(bool(box["running"]))

    try:
        window.after(30, poll)
    except Exception:
        return


def schedule_cell_patch(window, tree, holder, urls, by_iid, new_rows, on_done=None) -> None:
    """Update name, status, and progress on the rows already in the tree.

    Names and tables do not add or remove missions. A full delete and insert
    freezes the window, so each batch is one after() chunk.
    """
    started = time.perf_counter()
    previous = {row["iid"]: row for row in holder["rows"]}
    updates = []
    for row in new_rows:
        old = previous.get(row["iid"])
        if old is None:
            continue
        if (
            old.get("text") != row.get("text")
            or old.get("name") != row.get("name")
            or old.get("status") != row.get("status")
            or old.get("progress") != row.get("progress")
            or old.get("tag") != row.get("tag")
        ):
            updates.append(row)
    fresh = {row["iid"]: row for row in new_rows}
    merged = [fresh.get(row["iid"], row) for row in holder["rows"]]
    holder["rows"] = merged
    urls.clear()
    urls.update({row["iid"]: row.get("wiki") or "" for row in merged})
    by_iid.clear()
    by_iid.update({row["iid"]: row for row in merged})
    token = holder.get("gen", 0)
    pending: list[str] = []

    def finish() -> None:
        record("tree-reload", (time.perf_counter() - started) * 1000.0)
        if on_done is not None:
            on_done()

    def cancel(_event=None) -> None:
        while pending:
            job = pending.pop()
            try:
                window.after_cancel(job)
            except Exception:
                return

    def apply(index: int = 0) -> None:
        try:
            alive = window.winfo_exists()
        except Exception:
            return
        if not alive:
            return
        if holder.get("gen", 0) != token:
            if on_done is not None:
                on_done()
            return
        end = index + CELL_CHUNK
        if end > len(updates):
            end = len(updates)
        for row in updates[index:end]:
            try:
                if not tree.exists(row["iid"]):
                    continue
                tree.item(
                    row["iid"],
                    text=row["text"],
                    values=(row["name"], row["status"], row["progress"]),
                    tags=(row["tag"],),
                )
            except Exception:
                continue
        if end < len(updates):
            _queue_paint(window, lambda next_index=end: apply(next_index))
            return
        finish()

    window.bind("<Destroy>", cancel, add="+")
    apply()


def _letter_stem(mission_id: str) -> str:
    """The leading letters of an id, such as ^PURPM from ^PURPM3."""
    match = re.match(r"\^[A-Za-z]+", mission_id)
    return match.group(0) if match else ""


def _chain_nodes(roots: list[ChainView]) -> list[ChainView]:
    found: list[ChainView] = []

    def walk(node: ChainView) -> None:
        found.append(node)
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)
    return found


def _line_prefixes(nodes: list[ChainView]) -> dict[str, set[str]]:
    """Letter stems that name a line.

    A stem counts only when at least two steps of that line use it, and no
    other line has a step that starts with it. One odd step, such as
    ^SENTMISS_ATLAS on the Atlas line, does not claim ^SENTMISS_ missions.
    """
    steps = [(node.chain_id, step.mission_id) for node in nodes for step in node.steps]
    counts: dict[str, dict[str, int]] = {}
    for chain_id, mission_id in steps:
        stem = _letter_stem(mission_id)
        if len(stem) < 6:
            continue
        bag = counts.setdefault(chain_id, {})
        bag[stem] = bag.get(stem, 0) + 1
    owned: dict[str, set[str]] = {}
    for chain_id, bag in counts.items():
        for stem, count in bag.items():
            if count < 2:
                continue
            users = {owner for owner, mission_id in steps if mission_id.startswith(stem)}
            if users != {chain_id}:
                continue
            owned.setdefault(chain_id, set()).add(stem)
    return owned


def _extra_owner(mission_id: str, prefixes: dict[str, set[str]]) -> str | None:
    hits = {
        chain_id
        for chain_id, stems in prefixes.items()
        if any(mission_id.startswith(stem) for stem in stems)
    }
    if len(hits) != 1:
        return None
    return next(iter(hits))


def forest_rows(roots: list[ChainView], other: OtherView) -> list[dict]:
    """Flat rows a tree widget can insert. Tests use this without opening a window.

    Story quest lines are top-level rows. A line that waits on another keeps
    Needs: and that line's name in Friendly Name. Helper missions that share
    one quest line's id prefix sit under Extra steps (optional).
    """
    rows: list[dict] = []
    reliable = _line_prefixes(_chain_nodes(roots))
    extras: dict[str, list[StepView]] = {}
    leftover: list[StepView] = []
    for step in other.steps:
        owner = _extra_owner(step.mission_id, reliable)
        if owner is None:
            leftover.append(step)
        else:
            extras.setdefault(owner, []).append(step)

    def add_step(step: StepView, parent: str, chain_title: str, chain_id: str, requires: str) -> None:
        tag = "next" if step.is_next else step.status
        rows.append(
            {
                "iid": f"{parent}/{step.mission_id}",
                "parent": parent,
                "text": step.mission_id,
                "name": friendly_cell(step.title),
                "status": _step_status(step),
                "progress": _progress(step),
                "tag": tag,
                "chain": chain_title,
                "chain_id": chain_id,
                "mission_id": step.mission_id,
                "requires": requires,
                "wiki": step.wiki,
            }
        )

    def add_chain(node: ChainView, parent: str) -> None:
        iid = f"chain:{node.chain_id}"
        if node.complete:
            tag = "done"
        elif node.state_label.startswith("in progress"):
            tag = "in_progress"
        elif node.blocked_by:
            tag = "blocked"
        else:
            tag = "not_started"
        requires = " ".join(title for _chain_id, title, _done in node.requires)
        if node.requires:
            chain_name = "Needs: " + ", ".join(title for _chain_id, title, _done in node.requires)
        else:
            chain_name = "-"
        shown_parent = "" if node.group == "story" else parent
        main_left, optional_left = _finish_groups(node)
        unfinished = main_left + optional_left
        opened = [step.mission_id for step in node.steps if step.status != STATUS_NOT_STARTED]
        if node.complete or not unfinished:
            next_id = ""
        elif node.next_step is not None:
            next_id = node.next_step.mission_id
        elif main_left:
            next_id = main_left[0]
        else:
            next_id = ""
        rows.append(
            {
                "iid": iid,
                "parent": shown_parent,
                "text": node.title,
                "name": chain_name,
                "status": _chain_status(node),
                "progress": f"{node.done_count}/{node.required_count}",
                "tag": tag,
                "chain": node.title,
                "chain_id": node.chain_id,
                "mission_id": "",
                "requires": requires,
                "wiki": node.wiki,
                "step_ids": [step.mission_id for step in node.steps],
                "unfinished_ids": unfinished,
                "main_unfinished": main_left,
                "optional_unfinished": optional_left,
                "open_ids": opened,
                "next_id": next_id,
            }
        )
        for step in node.steps:
            add_step(step, iid, node.title, node.chain_id, requires)
        extra_steps = extras.get(node.chain_id) or []
        if extra_steps:
            extra_id = f"{iid}/extra"
            rows.append(
                {
                    "iid": extra_id,
                    "parent": iid,
                    "text": "Extra steps (optional)",
                    "name": "-",
                    "status": f"{len(extra_steps)} in this save",
                    "progress": "",
                    "tag": "other",
                    "chain": node.title,
                    "chain_id": "",
                    "mission_id": "",
                    "requires": "",
                    "wiki": "",
                }
            )
            for step in extra_steps:
                status = _step_status(step)
                if "optional" not in status:
                    status += " optional"
                rows.append(
                    {
                        "iid": f"{extra_id}/{step.mission_id}",
                        "parent": extra_id,
                        "text": step.mission_id,
                        "name": friendly_cell(step.title),
                        "status": status,
                        "progress": _progress(step),
                        "tag": step.status,
                        "chain": node.title,
                        "chain_id": "",
                        "mission_id": step.mission_id,
                        "requires": "",
                        "wiki": step.wiki,
                    }
                )
        for child in node.children:
            add_chain(child, iid)

    for root in roots:
        add_chain(root, "")
    other_id = "chain:other"
    rows.append(
        {
            "iid": other_id,
            "parent": "",
            "text": "Other",
            "name": "-",
            "status": f"{len(leftover)} in this save",
            "progress": "",
            "tag": "other",
            "chain": "Other",
            "chain_id": "",
            "mission_id": "",
            "requires": "",
            "wiki": "",
        }
    )
    for step in leftover:
        rows.append(
            {
                "iid": f"{other_id}/{step.mission_id}",
                "parent": other_id,
                "text": step.mission_id,
                "name": friendly_cell(step.title),
                "status": _step_status(step),
                "progress": _progress(step),
                "tag": step.status,
                "chain": "Other",
                "chain_id": "",
                "mission_id": step.mission_id,
                "requires": "",
                "wiki": step.wiki,
            }
        )
    return rows


def _step_can_change(step: StepView) -> bool:
    """False when Finish would leave this step as it is.

    A final progress of 0 cannot tell started from finished. Once that step
    has been reached, another Finish does not change it.
    """
    if step.status in (STATUS_DONE, STATUS_REACHED):
        return False
    return True


def _finish_groups(node: ChainView) -> tuple[list[str], list[str]]:
    """Main steps still to finish, then optional steps still to finish.

    A complete line has neither. The row's 14/14 count is the main steps.
    """
    if node.complete:
        return [], []
    main: list[str] = []
    optional: list[str] = []
    for step in node.steps:
        if not _step_can_change(step):
            continue
        if step.optional:
            optional.append(step.mission_id)
        else:
            main.append(step.mission_id)
    return main, optional


def _step_status(step: StepView) -> str:
    """Same status text as the CLI table, including optional and tracked."""
    status = "NEXT  " + step.status_label if step.is_next else step.status_label
    if step.tracked:
        status += " (tracked)"
    if step.optional and not step.is_next:
        status += " optional"
    return status


def _chain_status(node: ChainView) -> str:
    status = node.state_label
    if node.blocked_by:
        status += "  blocked until: " + ", ".join(node.blocked_by)
    return status


_SEARCH_KEYS = ("text", "name", "status", "progress", "chain", "requires")


def _row_blob(row: dict) -> str:
    return " ".join(str(row.get(key, "")) for key in _SEARCH_KEYS).lower()


def search_hits(rows: list[dict], needle: str) -> list[str]:
    """Rows whose own text matches. Ancestors kept only so the row can be seen are not hits."""
    text = needle.strip().lower()
    if not text:
        return []
    return [row["iid"] for row in rows if text in _row_blob(row)]


def group_open_ids(rows: list[dict], needle: str, user_open: set[str]) -> set[str]:
    """Groups that should be open. A search opens the path to each hit and does not remember it."""
    opened = set(user_open)
    hits = search_hits(rows, needle)
    if not hits:
        return opened
    by_iid = {row["iid"]: row for row in rows}
    parents = {row["parent"] for row in rows if row["parent"]}
    for iid in hits:
        if iid in parents:
            opened.add(iid)
        parent = by_iid[iid]["parent"]
        while parent:
            opened.add(parent)
            parent = by_iid[parent]["parent"]
    return opened


def matching_rows(rows: list[dict], needle: str) -> list[dict]:
    """Rows the filter keeps.

    A match keeps that row, its parents, and its children. A quest line also
    matches the titles named in its Needs note, so those lines stay visible.
    """
    text = needle.strip().lower()
    if not text:
        return rows
    by_iid = {row["iid"]: row for row in rows}
    children: dict[str, list[str]] = {}
    for row in rows:
        children.setdefault(row["parent"], []).append(row["iid"])
    matched = search_hits(rows, text)
    visible: set[str] = set()

    def add_down(iid: str) -> None:
        stack = list(children.get(iid, ()))
        while stack:
            current = stack.pop()
            if current in visible:
                continue
            visible.add(current)
            stack.extend(children.get(current, ()))

    for iid in matched:
        parent = iid
        while parent:
            visible.add(parent)
            parent = by_iid[parent]["parent"]
        add_down(iid)
    return [row for row in rows if row["iid"] in visible]


def _tree_iids(tree) -> list[str]:
    found: list[str] = []

    def walk(parent: str) -> None:
        for iid in tree.get_children(parent):
            found.append(iid)
            walk(iid)

    walk("")
    return found


def _group_is_open(tree, iid: str) -> bool:
    value = tree.item(iid, "open")
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _apply_group_open(tree, rows: list[dict], open_ids: set[str]) -> None:
    """Open or close groups only. Leaves are not walked, so a large save stays quick."""
    parents = {row["parent"] for row in rows if row["parent"]}
    previous = getattr(tree, "_open_lock", False)
    tree._open_lock = True
    try:
        for iid in parents:
            if not tree.exists(iid):
                continue
            want = iid in open_ids
            if _group_is_open(tree, iid) != want:
                tree.item(iid, open=want)
    finally:
        tree._open_lock = previous


def _paint_token(tree) -> int:
    """Drop a previous chunked paint so a newer list cannot be overwritten."""
    tree._paint_queue = []
    jobs = list(getattr(tree, "_paint_jobs", []) or [])
    tree._paint_jobs = []
    for job in jobs:
        try:
            tree.after_cancel(job)
        except Exception:
            pass
    token = getattr(tree, "_paint_gen", 0) + 1
    tree._paint_gen = token
    return token


def _queue_paint(widget, callback) -> None:
    """Queue one paint chunk. flush_paint_chunks runs it without waiting on the clock."""
    queue = getattr(widget, "_paint_queue", None)
    if queue is None:
        queue = []
        widget._paint_queue = queue
    queue.append(callback)
    jobs = getattr(widget, "_paint_jobs", None)
    if jobs is None:
        jobs = []
        widget._paint_jobs = jobs
    try:
        jobs.append(widget.after(1, callback))
    except Exception:
        return


def flush_paint_chunks(widget, limit: int = 10000) -> None:
    """Run every queued paint chunk now. Stops when the queue is empty."""
    for _step in range(limit):
        queued = list(getattr(widget, "_paint_queue", []) or [])
        if not queued:
            return
        widget._paint_queue = []
        for job in list(getattr(widget, "_paint_jobs", []) or []):
            try:
                widget.after_cancel(job)
            except Exception:
                pass
        widget._paint_jobs = []
        for callback in queued:
            callback()
    raise RuntimeError("paint chunks did not finish")


def _write_tree_row(tree, row: dict) -> None:
    tree.item(
        row["iid"],
        text=row["text"],
        values=(row["name"], row["status"], row["progress"]),
        tags=(row["tag"],),
    )


def _row_needs_write(tree, row: dict) -> bool:
    values = (row["name"], row["status"], row["progress"])
    current = tree.item(row["iid"])
    got = tuple(str(part) for part in (current.get("values") or ()))
    tags = current.get("tags") or ()
    if isinstance(tags, str):
        tag = tags.split()[0] if tags else ""
    else:
        tag = str(tags[0]) if tags else ""
    return (
        current.get("text") != row["text"]
        or got != tuple(str(part) for part in values)
        or tag != row["tag"]
    )


def paint_rows(tree, rows: list[dict], open_ids: set[str] | None = None) -> None:
    """Insert rows, or rewrite only the ones whose text changed.

    A filter that adds or removes rows still rebuilds. An edit that changes
    status keeps the same items, so the selection stays put. A large name
    refresh writes the first chunk immediately and the rest via after(), so
    the window is not stuck for the whole list. Groups stay closed unless
    open_ids names them.
    """
    with timed("tree-paint"):
        token = _paint_token(tree)
        wanted_ids = [row["iid"] for row in rows]
        if _tree_iids(tree) == wanted_ids:
            changed = [row for row in rows if _row_needs_write(tree, row)]
            # A one-cell edit stays synchronous so the new status is visible now.
            if len(changed) <= CELL_CHUNK:
                for row in changed:
                    _write_tree_row(tree, row)
                if open_ids is not None:
                    _apply_group_open(tree, rows, open_ids)
                return
            for row in changed[:CELL_CHUNK]:
                _write_tree_row(tree, row)
            if open_ids is not None:
                _apply_group_open(tree, rows, open_ids)
            rest = changed[CELL_CHUNK:]

            def apply_rest(index: int = 0) -> None:
                if getattr(tree, "_paint_gen", 0) != token:
                    return
                try:
                    if not tree.winfo_exists():
                        return
                except Exception:
                    return
                end = index + CELL_CHUNK
                if end > len(rest):
                    end = len(rest)
                for row in rest[index:end]:
                    try:
                        if tree.exists(row["iid"]):
                            _write_tree_row(tree, row)
                    except Exception:
                        return
                if end < len(rest):
                    _queue_paint(tree, lambda index=end: apply_rest(index))

            _queue_paint(tree, apply_rest)
            return
        opened = open_ids or set()
        previous = getattr(tree, "_open_lock", False)
        tree._open_lock = True
        try:
            tree.delete(*tree.get_children(""))
            for row in rows:
                tree.insert(
                    row["parent"],
                    "end",
                    iid=row["iid"],
                    text=row["text"],
                    values=(row["name"], row["status"], row["progress"]),
                    tags=(row["tag"],),
                    open=row["iid"] in opened,
                )
        finally:
            tree._open_lock = previous


def _progress(step: StepView) -> str:
    return progress_cell(step.progress, step.complete)


PROGRESS_COLUMN = {"width": 160, "minwidth": 120, "stretch": True}


def begin_loading():
    """Open a small window immediately, before the save and names are parsed."""
    prepare_dpi()
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        raise RuntimeError(
            "Tkinter is not available in this Python. Use the list command instead. "
            f"({exc})"
        ) from exc
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(
            "No display is available, so the chain window cannot open. "
            f"Use the list command. ({exc})"
        ) from exc
    apply_tk_scaling(window)
    window.title("No Man's Sky mission chains")
    window.minsize(360, 72)
    label = ttk.Label(window, text="Loading missions... please wait", padding=24, justify="left")
    label.pack(fill="both", expand=True)
    _follow_width(window, label)
    window._loading_label = label
    window.update_idletasks()
    window.update()
    return window


def apply_name_refresh(window, tree, holder, urls, by_iid, banner, subtitle: str, rows: list[dict], session=None) -> dict[str, float]:
    """Put a prepared list on screen. The name map and rows are already built.

    Returns the milliseconds spent on the Tk thread for the banner and the
    first tree chunk. Later chunks stay on the paint queue.
    """
    phases: dict[str, float] = {}
    started = time.perf_counter()
    set_banner_mode(
        banner,
        subtitle,
        live=bool(session and getattr(session, "live", False)),
        editing=session is not None,
    )
    phases["post-read-banner"] = (time.perf_counter() - started) * 1000.0
    record("post-read-banner", phases["post-read-banner"])
    started = time.perf_counter()
    same = _tree_iids(tree) == [row["iid"] for row in rows]
    if same:
        schedule_cell_patch(window, tree, holder, urls, by_iid, rows)
    else:
        holder["rows"] = rows
        urls.clear()
        urls.update({row["iid"]: row.get("wiki") or "" for row in rows})
        by_iid.clear()
        by_iid.update({row["iid"]: row for row in rows})
        paint_rows(tree, rows)
    phases["post-read-tree"] = (time.perf_counter() - started) * 1000.0
    record("post-read-tree", phases["post-read-tree"])
    return phases


def set_status(window, text: str) -> None:
    label = getattr(window, "_loading_label", None)
    if label is not None:
        try:
            label.configure(text=text)
            window.update_idletasks()
            window.update()
        except Exception:
            return


def launch(
    title: str,
    subtitle: str,
    roots: list[ChainView],
    other: OtherView,
    window=None,
    session=None,
    reload_cb=None,
    asset_rows=None,
) -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        raise RuntimeError(
            "Tkinter is not available in this Python. Use the list command instead. "
            f"({exc})"
        ) from exc

    holder = {"rows": forest_rows(roots, other), "subtitle": subtitle, "gen": 0}
    if window is None:
        prepare_dpi()
        try:
            window = tk.Tk()
        except tk.TclError as exc:
            raise RuntimeError(
                "No display is available, so the chain window cannot open. "
                f"Use the list command. ({exc})"
            ) from exc
    else:
        prepare_dpi()
        for child in list(window.winfo_children()):
            child.destroy()
        window._loading_label = None
    apply_tk_scaling(window)

    if session is None:
        window.title(title)
    else:
        kind = "LIVE" if session.live else "COPY"
        window.title(f"{kind} — {title}")
    banner_text = banner_mode_line(subtitle, live=bool(session and session.live), editing=session is not None)
    window.minsize(640, 420)
    banner = tk.Label(
        window,
        text=banner_text,
        wraplength=800,
        justify="left",
        anchor="w",
        padx=8,
        pady=8,
    )
    banner.mode_line = banner_text
    banner.pack(fill="x")
    if session is not None and session.live:
        banner.configure(fg="#9b1c1c")
    from nmsmissions.locate import SAFETY_LINE

    version_note = "" if session is None else str(getattr(session, "version_note", "") or "")
    safety_text = SAFETY_LINE if not version_note else SAFETY_LINE + "\n" + version_note
    safety = tk.Label(
        window,
        text=safety_text,
        wraplength=800,
        justify="left",
        anchor="w",
        padx=8,
        pady=4,
        fg="#8a3b12",
    )
    safety.pack(fill="x")
    status = tk.Label(window, text="", wraplength=800, justify="left", anchor="w", padx=8, pady=4)
    status.pack(fill="x")
    _follow_width(window, banner, safety, status)
    status_box = {"error": ""}
    status_stop = {"flag": False}
    armed: list = []

    def _disarm() -> None:
        while armed:
            token = armed.pop()
            try:
                window.after_cancel(token)
            except Exception:
                pass

    def _soon(delay: int, fn):
        try:
            token = window.after(delay, fn)
        except Exception:
            return None
        armed.append(token)
        return token
    if session is not None:
        session._owns_window = True
        session.start_snapshot_warm()

    def _status_lines() -> str:
        from nmsmissions.edit import STEAM_WARNING, peek_nms_running
        from nmsmissions.slots import slot_status_line

        lines = [slot_status_line(session.save, session.origin)]
        error = status_box.get("error") or ""
        running = peek_nms_running()
        if error and running is None:
            lines.append(error)
        elif running is None:
            lines.append("Checking whether No Man's Sky is running.")
        elif running:
            lines.append("No Man's Sky is running. Close it before you put a save into the game.")
        else:
            lines.append("No Man's Sky is not running.")
        lines.append(STEAM_WARNING)
        return "\n".join(lines)

    def refresh_status() -> None:
        if session is None:
            status.configure(text="")
            return
        try:
            status.configure(text=_status_lines())
        except tk.TclError:
            return

    if session is not None:

        def _watch_status() -> None:
            while not status_stop["flag"]:
                started = time.monotonic()
                try:
                    from nmsmissions.edit import nms_is_running, note_nms_running

                    with timed("status-poll"):
                        note_nms_running(nms_is_running())
                    status_box["error"] = ""
                except Exception as exc:
                    status_box["error"] = str(exc)
                remain = 4.0 - (time.monotonic() - started)
                while remain > 0 and not status_stop["flag"]:
                    step = min(0.1, remain)
                    time.sleep(step)
                    remain -= step

        threading.Thread(target=_watch_status, name="nms-status", daemon=True).start()

    refresh_status()

    bar = ttk.Frame(window, padding=(8, 0, 8, 8))
    bar.pack(fill="x")
    find_label = ttk.Label(bar, text="Find")
    find_label.pack(side="left")
    add_tip(find_label, "Type part of a mission or chain name. Matches open, even if the group was closed.")
    query = tk.StringVar()
    entry = ttk.Entry(bar, textvariable=query)
    entry.pack(side="left", fill="x", expand=True, padx=8)
    user_open: set[str] = set()
    urls = {row["iid"]: row.get("wiki") or "" for row in holder["rows"]}
    by_iid = {row["iid"]: row for row in holder["rows"]}

    def selected_wiki() -> str:
        picked = tree.selection()
        if not picked:
            return ""
        return urls.get(picked[0], "")

    def open_selected(_event=None) -> None:
        url = selected_wiki()
        if url:
            open_wiki(url)

    def open_from_event(event) -> None:
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        tree.selection_set(row_id)
        url = urls.get(row_id, "")
        if url:
            open_wiki(url)

    wiki_button = ttk.Button(bar, text="Open wiki page", command=open_selected)
    wiki_button.pack(side="left")
    add_tip(wiki_button, "Open the wiki page for the selected mission.")

    notebook = ttk.Notebook(window, padding=(8, 0, 8, 8))
    notebook.pack(fill="both", expand=True)
    missions_page = ttk.Frame(notebook)
    station_page = ttk.Frame(notebook, padding=8)
    notebook.add(missions_page, text="Missions")
    notebook.add(station_page, text="Station and standing")
    frame = ttk.Frame(missions_page)
    frame.pack(fill="both", expand=True)
    columns = ("name", "status", "progress")
    tree = ttk.Treeview(frame, columns=columns, selectmode="browse", height=25)
    window._prepare_rows = asset_rows
    window._list_tree = tree
    window._list_holder = holder
    window._list_urls = urls
    window._list_by_iid = by_iid
    tree.heading("#0", text="Mission ID")
    tree.heading("name", text="Friendly Name")
    tree.heading("status", text="Status")
    tree.heading("progress", text="Progress")
    tree.column("#0", width=280, stretch=True)
    tree.column("name", width=320, stretch=True)
    tree.column("status", width=420, stretch=False)
    tree.column("progress", **PROGRESS_COLUMN)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    tree.tag_configure("next", background="#c8f5d4")
    tree.tag_configure("done", foreground="#5c6570")
    tree.tag_configure("in_progress", foreground="#0b6e4f")
    tree.tag_configure("blocked", foreground="#8a3b12")
    tree.tag_configure("not_started", foreground="#1c2430")
    tree.tag_configure("unknown", foreground="#6b4c9a")

    def refill(*_args: object) -> None:
        rows = holder["rows"]
        needle = query.get()
        shown = matching_rows(rows, needle)
        allowed = {row["iid"] for row in shown}
        visible = [row for row in rows if row["iid"] in allowed]
        paint_rows(tree, visible, group_open_ids(visible, needle, user_open))
        hits = search_hits(visible, needle)
        if hits and tree.exists(hits[0]):
            tree.selection_set(hits[0])
            tree.see(hits[0])

    def expand_all() -> None:
        user_open.update(row["parent"] for row in holder["rows"] if row["parent"])
        refill()

    def collapse_all() -> None:
        user_open.clear()
        refill()

    expand_button = ttk.Button(bar, text="Expand all", command=expand_all)
    expand_button.pack(side="left", before=wiki_button, padx=(0, 4))
    add_tip(expand_button, "Open every group.")
    collapse_button = ttk.Button(bar, text="Collapse all", command=collapse_all)
    collapse_button.pack(side="left", before=wiki_button, padx=(0, 8))
    add_tip(collapse_button, "Close every group.")

    def reload_rows() -> None:
        holder["gen"] = holder.get("gen", 0) + 1
        token = holder["gen"]
        window._reload_busy = True
        box: dict = {}

        def work() -> None:
            started = time.perf_counter()
            try:
                if reload_cb is None:
                    box["result"] = None
                else:
                    result = reload_cb()
                    # Install the snapshot before the list is published. The
                    # selection refresh then reads this object and does not
                    # open the save on the window thread. A second warm would
                    # parse the same file again.
                    if session is not None and result:
                        try:
                            session.ensure_snapshot()
                        except Exception as exc:
                            box["snap_error"] = exc
                    box["result"] = result
            except Exception as exc:
                box["error"] = exc
            record("post-write-parse", (time.perf_counter() - started) * 1000.0)

        def apply() -> None:
            job = box.get("job")
            if job is not None:
                box["job"] = None
                try:
                    window.after_cancel(job)
                except Exception:
                    pass
            try:
                alive = window.winfo_exists()
            except Exception:
                window._reload_busy = False
                return
            if not alive:
                if holder.get("gen") == token:
                    window._reload_busy = False
                return
            if holder.get("gen") != token or box.get("used"):
                return
            if "result" not in box and "error" not in box:
                try:
                    box["job"] = window.after(20, apply)
                except Exception:
                    window._reload_busy = False
                return
            box["used"] = True
            window._reload_busy = False
            if "error" in box and "result" not in box:
                _keep_banner(banner, f"The mission list could not be updated. {box['error']}", None)
                return
            result = box.get("result")
            if not result:
                return
            if len(result) >= 4:
                _roots, _other, new_subtitle, rows = result[0], result[1], result[2], result[3]
            else:
                _roots, _other, new_subtitle = result[0], result[1], result[2]
                rows = forest_rows(_roots, _other)
            previous = tree.selection()
            holder["rows"] = rows
            holder["subtitle"] = new_subtitle
            urls.clear()
            urls.update({row["iid"]: row.get("wiki") or "" for row in rows})
            by_iid.clear()
            by_iid.update({row["iid"]: row for row in rows})
            set_banner_mode(
                banner,
                new_subtitle,
                live=bool(session and session.live),
                editing=session is not None,
            )
            refill()
            for iid in previous:
                if tree.exists(iid):
                    tree.selection_set(iid)
                    tree.see(iid)
                    break
            if session is not None:
                refresh_status()
            refresh = getattr(window, "_refresh_actions", None)
            if refresh is not None:
                refresh()
            refresh_station = getattr(window, "_refresh_station", None)
            if refresh_station is not None:
                refresh_station()

        window._reload_apply = apply
        worker = threading.Thread(target=work, name="nms-reload", daemon=True)
        window._reload_worker = worker
        worker.start()
        try:
            box["job"] = window.after(20, apply)
        except Exception:
            window._reload_busy = False

    def _note_open(_event=None) -> None:
        if getattr(tree, "_open_lock", False):
            return
        iid = tree.focus()
        if iid:
            user_open.add(iid)

    def _note_close(_event=None) -> None:
        if getattr(tree, "_open_lock", False):
            return
        iid = tree.focus()
        if iid:
            user_open.discard(iid)

    tree.bind("<<TreeviewOpen>>", _note_open)
    tree.bind("<<TreeviewClose>>", _note_close)
    query.trace_add("write", refill)
    refill()
    tree.bind("<Double-Button-1>", open_from_event)
    tree.bind("<Button-3>", open_from_event)
    if session is not None:
        session.wait_snapshot_warm()
        _save_menu(window, session, banner, refresh_status, reload_rows)
        _station_page(window, station_page, session, banner, reload_rows, refresh_status)
        edit_buttons = _editor_bar(
            window, tree, session, by_iid, banner, reload_rows, refresh_status, before=notebook
        )
        need_names = session.game_files is not None and getattr(session, "_names_report", None) is None
        need_tables = bool(session.game_files) and not session.tables_cached()
        if need_names or need_tables:
            for button in edit_buttons:
                if need_tables:
                    button.state(["disabled"])
            if need_names:
                session.start_name_load()
            if need_tables:
                session.start_table_load()
            shown = {
                "names": getattr(session, "_names_report", None),
                "tables": session.tables_cached(),
            }
            patching = {"busy": False}

            def _loading_line() -> str:
                parts = []
                if getattr(session, "_names_report", None) is None and getattr(session, "_name_error", None) is None:
                    parts.append(getattr(session, "_name_progress", "") or "Loading mission names… 0%")
                if not session.tables_cached() and session._table_error is None:
                    parts.append(getattr(session, "_table_progress", "") or "Loading mission tables… 0%")
                return "\n".join(parts)

            def _drain_table_events() -> None:
                """Apply a few progress strings. Parsing stays on the table thread."""
                try:
                    alive = window.winfo_exists()
                except Exception:
                    alive = False
                if not alive:
                    return
                latest = None
                for _ignored in range(8):
                    try:
                        latest = session._table_events.get_nowait()
                    except queue.Empty:
                        break
                if latest:
                    session._table_progress = latest
                waiting = _loading_line()
                mode_line = getattr(banner, "mode_line", "")
                if waiting:
                    _banner_text(banner, (mode_line + "\n" if mode_line else "") + waiting)
                if session.tables_cached() and session._table_events.empty():
                    return
                if session._table_error is not None and session._table_events.empty():
                    return
                _soon(50, _drain_table_events)

            if need_tables:
                _soon(50, _drain_table_events)

            def _assets_ready() -> None:
                try:
                    if not window.winfo_exists():
                        return
                except Exception:
                    return
                if session._table_error is not None:
                    mode_line = getattr(banner, "mode_line", "")
                    failed = f"Mission tables failed to load. {session._table_error}"
                    _banner_text(banner, (mode_line + "\n" if mode_line else "") + failed)
                    from tkinter import messagebox

                    messagebox.showerror("Mission tables", failed)
                    return
                if patching["busy"]:
                    _soon(50, _assets_ready)
                    return
                report = getattr(session, "_names_report", None)
                names_new = report is not None and shown["names"] is not report
                tables_new = session.tables_cached() and not shown["tables"]
                if names_new or tables_new:
                    if names_new:
                        shown["names"] = report
                    if tables_new:
                        shown["tables"] = True
                        for button in edit_buttons:
                            button.state(["!disabled"])
                        for refresh in getattr(edit_buttons, "refreshers", ()):
                            refresh()
                    if asset_rows is None:
                        waiting = _loading_line()
                        mode_line = getattr(banner, "mode_line", "")
                        if waiting:
                            _banner_text(banner, (mode_line + "\n" if mode_line else "") + waiting)
                            _soon(200, _assets_ready)
                        return
                    patching["busy"] = True
                    token = holder.get("gen", 0)
                    box: dict = {}

                    def work() -> None:
                        try:
                            box["result"] = asset_rows()
                        except Exception as exc:
                            box["error"] = exc

                    def apply_asset() -> None:
                        try:
                            alive = window.winfo_exists()
                        except Exception:
                            alive = False
                        if not alive or holder.get("gen", 0) != token:
                            patching["busy"] = False
                            return
                        if "error" in box:
                            patching["busy"] = False
                            mode_line = getattr(banner, "mode_line", "")
                            _banner_text(
                                banner,
                                (mode_line + "\n" if mode_line else "")
                                + f"The mission list could not be updated. {box['error']}",
                            )
                            _soon(200, _assets_ready)
                            return
                        if "result" not in box:
                            _soon(30, apply_asset)
                            return
                        result = box["result"]
                        if len(result) == 4:
                            _roots, _other, new_subtitle, rows = result
                        else:
                            _roots, _other, new_subtitle = result
                            rows = forest_rows(_roots, _other)
                        holder["subtitle"] = new_subtitle

                        def done() -> None:
                            patching["busy"] = False
                            waiting = _loading_line()
                            mode_line = getattr(banner, "mode_line", "")
                            if waiting:
                                _banner_text(banner, (mode_line + "\n" if mode_line else "") + waiting)
                            _soon(1, _assets_ready)

                        apply_name_refresh(
                            window, tree, holder, urls, by_iid, banner, new_subtitle, rows, session
                        )
                        done()

                    threading.Thread(target=work, name="nms-tree-cells", daemon=True).start()
                    _soon(30, apply_asset)
                    return
                waiting = _loading_line()
                mode_line = getattr(banner, "mode_line", "")
                if waiting:
                    _banner_text(banner, (mode_line + "\n" if mode_line else "") + waiting)
                    _soon(200, _assets_ready)
                    return
                if mode_line:
                    _banner_text(banner, mode_line)
                else:
                    _banner_text(banner, banner.cget("text"))

            _soon(200, _assets_ready)

        def _poll_game() -> None:
            if status_stop["flag"]:
                return
            try:
                alive = window.winfo_exists()
            except tk.TclError:
                alive = False
            if not alive:
                status_stop["flag"] = True
                return
            refresh_status()
            from nmsmissions.edit import peek_nms_running

            waiting = peek_nms_running() is None and not status_box.get("error")
            armed.append(window.after(200 if waiting else 1000, _poll_game))

        def _stop_status() -> None:
            status_stop["flag"] = True
            _disarm()
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", _stop_status)
        armed.append(window.after(200, _poll_game))
    window._reload_rows = reload_rows
    apply_window_size(window)
    remembered = {"width": 0, "height": 0, "x": 0, "y": 0, "zoomed": False}

    def _track_window(_event=None) -> None:
        try:
            width = int(window.winfo_width())
            height = int(window.winfo_height())
        except Exception:
            return
        if width < 200 or height < 200:
            return
        remembered["width"] = width
        remembered["height"] = height
        remembered["x"] = int(window.winfo_x())
        remembered["y"] = int(window.winfo_y())
        try:
            remembered["zoomed"] = str(window.state()) == "zoomed"
        except Exception:
            remembered["zoomed"] = False

    def _remember_window(event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not window:
            return
        _disarm()
        _track_window()
        save_window_settings(window, remembered)

    window.bind("<Configure>", _track_window, add="+")
    window.bind("<Destroy>", _remember_window)
    if session is not None and os.environ.get("NMSMISSIONS_SKIP_SAFETY") != "1":
        if session.game_files is None and getattr(session, "pcbanks", None):
            _soon(300, lambda: begin_game_file_read(window, session, banner, reload_rows, ask=True))
    if session is None:
        _station_page(window, station_page, None, banner, None, None)
    entry.focus_set()
    window.mainloop()


def is_quest_line(row: dict | None) -> bool:
    """A quest-line title row. A mission step and the Other group are not."""
    return bool(row and row.get("chain_id") and not row.get("mission_id"))


def button_labels(line: bool) -> dict[str, str]:
    """Button words. A quest-line row names the whole line."""
    if line:
        return {
            "finish": CHAIN_FINISH_LABEL,
            "unlock": CHAIN_UNLOCK_LABEL,
            "reset": CHAIN_RESET_LABEL,
            "finish_tip": "Finish every unfinished step in this quest line, in order, and apply each unlock.",
            "unlock_tip": "Unlock or start the next step in this quest line. It stays unfinished.",
            "reset_tip": "Set every step in this quest line back to not started.",
        }
    return {
        "finish": FINISH_LABEL,
        "unlock": UNLOCK_LABEL,
        "reset": RESET_LABEL,
        "finish_tip": "Mark the selected mission done and apply its unlock.",
        "unlock_tip": UNLOCK_TIP,
        "reset_tip": "Set the selected mission back to not started.",
    }


def finish_line_counts(row: dict) -> tuple[list[str], list[str]]:
    """Main steps and optional steps the Finish confirm should name."""
    if "main_unfinished" in row or "optional_unfinished" in row:
        return list(row.get("main_unfinished") or []), list(row.get("optional_unfinished") or [])
    return list(row.get("unfinished_ids") or []), []


def finish_count_phrase(main: list[str], optional: list[str]) -> str:
    """'13 main steps (+5 optional)', matching the chain row."""
    if main and optional:
        noun = "step" if len(main) == 1 else "steps"
        return f"{len(main)} main {noun} (+{len(optional)} optional)"
    if main:
        noun = "step" if len(main) == 1 else "steps"
        return f"{len(main)} main {noun}"
    noun = "step" if len(optional) == 1 else "steps"
    return f"{len(optional)} optional {noun}"


def quest_line_prompt(action: str, row: dict, rewards: bool) -> tuple[str, str, str]:
    """confirm, info, or go. The last two strings are the dialog title and body."""
    title_name = str(row.get("text") or "this quest line")
    if action == "finish":
        main, optional = finish_line_counts(row)
        if not main and not optional:
            if row.get("missing_unlocks"):
                return (
                    "confirm",
                    CHAIN_FINISH_LABEL,
                    f"Apply the missing unlocks for {title_name}?\nThe steps stay as they are.",
                )
            return ("info", CHAIN_FINISH_LABEL, f"{title_name} is already finished.")
        if rewards:
            reward = "Give items and money is on, so those steps grant their rewards."
        else:
            reward = "Give items and money is off, so rewards are not added."
        return (
            "confirm",
            CHAIN_FINISH_LABEL,
            f"Finish {finish_count_phrase(main, optional)} of {title_name}, in order?\n{reward}",
        )
    if action == "reset":
        count = len(row.get("open_ids") or [])
        if count == 0:
            return ("info", CHAIN_RESET_LABEL, f"{title_name} is already not started.")
        noun = "step" if count == 1 else "steps"
        return (
            "confirm",
            CHAIN_RESET_LABEL,
            f"Reset {count} {noun} of {title_name}?\n"
            "This sets the quest line back to not started. Rewards already granted stay.",
        )
    if not row.get("next_id"):
        return ("info", CHAIN_UNLOCK_LABEL, f"{title_name} has no next step. The quest line is already finished.")
    return ("go", "", "")


def _station_page(window, page, session, banner, reload_rows, refresh_status) -> None:
    """Current-system standing. The text comes from the snapshot already in memory."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    from nmsmissions.station import CHOOSE_STANDING, GUILD_SPECS, MEET_LABEL, RACE_SPECS, meet_does, panel_text

    body = tk.Label(page, text="", justify="left", anchor="nw")
    body.pack(fill="x", anchor="nw")
    _follow_width(window, body)

    race_heading = ttk.Label(page, text="Race standing this station uses. Only this one is raised to 30.")
    race_heading.pack(fill="x", anchor="w", pady=(8, 0))
    race_var = tk.StringVar(value="")
    for stat_id, _label, _target, choice in RACE_SPECS:
        ttk.Radiobutton(page, text=choice, variable=race_var, value=stat_id).pack(anchor="w")

    guild_heading = ttk.Label(page, text="Guild standing this station uses. Only this one is raised to 15.")
    guild_heading.pack(fill="x", anchor="w", pady=(8, 0))
    guild_var = tk.StringVar(value="")
    for stat_id, _label, _target, choice in GUILD_SPECS:
        ttk.Radiobutton(page, text=choice, variable=guild_var, value=stat_id).pack(anchor="w")

    salvage_heading = ttk.Label(page, text="Salvage contracts in this system are raised to 5 as well.")
    salvage_heading.pack(fill="x", anchor="w", pady=(8, 0))

    choice = tk.Label(page, text="", justify="left", anchor="nw")
    choice.pack(fill="x", anchor="nw", pady=(8, 0))
    _follow_width(window, choice)
    actions = ttk.Frame(page)
    actions.pack(fill="x", pady=(8, 0))

    def refresh(*_args: object) -> None:
        if session is None:
            body.configure(text=panel_text(None, opened=False))
            return
        snap = session.peek_snapshot()
        if snap is None:
            body.configure(text=panel_text(None, loading=True))
            return
        body.configure(text=panel_text(snap.player))

    def refresh_choice(*_args: object) -> None:
        text = meet_does(race_var.get() or None, guild_var.get() or None)
        choice.configure(text=text)
        set_tip(tip, text)

    def meet() -> None:
        if session is None:
            messagebox.showinfo("Station and standing", "Open a save first.")
            return
        race_id = race_var.get()
        guild_id = guild_var.get()
        if not race_id or not guild_id:
            messagebox.showinfo("Station and standing", CHOOSE_STANDING)
            return
        try:
            plan = session.plan_for(
                "station",
                None,
                None,
                False,
                station_race=race_id,
                station_guild=guild_id,
            )
        except Exception as exc:
            messagebox.showerror("Could not preview", str(exc))
            return
        _preview(window, session, plan, banner, reload_rows, refresh_status)

    button = ttk.Button(actions, text=MEET_LABEL, command=meet)
    button.pack(side="left")
    tip = add_tip(button, meet_does(None, None))
    race_var.trace_add("write", refresh_choice)
    guild_var.trace_add("write", refresh_choice)
    refresh_choice()
    if session is None:
        button.state(["disabled"])
    window._refresh_station = refresh
    refresh()


def _editor_bar(window, tree, session, by_iid: dict, banner, reload_rows, refresh_status, before=None) -> list:
    import tkinter as tk
    from tkinter import messagebox, ttk

    bar = ttk.Frame(window, padding=(8, 0, 8, 4))
    bar.pack(fill="x", before=before if before is not None else tree.master)
    give = tk.BooleanVar(value=True)
    rewards_box = ttk.Checkbutton(bar, text="Give items and money", variable=give)
    rewards_box.pack(side="left")
    add_tip(rewards_box, "When you finish a mission, also add its items, money, and recipes.")
    slot_var = tk.StringVar()
    if session.live:
        ttk.Label(bar, text="Slot").pack(side="left", padx=(12, 4))
        ttk.Entry(bar, textvariable=slot_var, width=4).pack(side="left")

    def selected():
        picked = tree.selection()
        if not picked:
            return None
        return by_iid.get(picked[0])

    def prepare(action: str, chain: bool) -> None:
        row = selected()
        if is_quest_line(row):
            if chain:
                messagebox.showinfo("Select a step", "Select a step in the quest line first.")
                return
            kind, title, body = quest_line_prompt(action, row, bool(give.get()))
            if kind == "info":
                messagebox.showinfo(title, body)
                return
            if kind == "confirm" and not messagebox.askokcancel(title, body):
                return
            _plan_line(action, row)
            return
        mission = None if row is None else row.get("mission_id") or None
        chain_id = None
        if action == "unlock":
            if not mission:
                messagebox.showinfo(
                    "Select a mission",
                    "Select a mission first." if row is None else "Select a mission, not the chain title.",
                )
                return
        elif chain:
            if row is None or not mission or not row.get("chain_id"):
                messagebox.showinfo("Select a step", "Select a mission in a chain first.")
                return
            chain_id = row.get("chain_id")
        elif not mission:
            messagebox.showinfo("Select a step", "Select a mission first.")
            return
        _plan(action, mission, chain_id, False)

    def _tables_ready_for_edit() -> bool:
        if session.live:
            try:
                session.confirm_slot = int(slot_var.get())
            except ValueError:
                session.confirm_slot = None
        if session.game_files and not session.tables_cached():
            if session._table_error is not None:
                messagebox.showerror("Mission tables", f"Mission tables failed to load.\n{session._table_error}")
                return False
            messagebox.showinfo("Mission tables", "Mission tables are still loading.")
            return False
        return True

    def _plan(action: str, mission: str | None, chain_id: str | None, whole_line: bool) -> None:
        session.rewards_on = bool(give.get())
        if not _tables_ready_for_edit():
            return
        if action == "unlock":
            try:
                blocked = session.unlock_reason(mission, load=False)
            except Exception as exc:
                messagebox.showerror("Could not preview", str(exc))
                return
            if blocked:
                messagebox.showinfo("Unlock only", blocked)
                return
        try:
            plan = session.plan_for(action, mission, chain_id, False, whole_line)
        except Exception as exc:
            messagebox.showerror("Could not preview", str(exc))
            return
        _preview(window, session, plan, banner, reload_rows, refresh_status)

    def _plan_line(action: str, row: dict) -> None:
        if action == "unlock":
            _plan("unlock", row.get("next_id") or None, None, False)
            return
        _plan(action, None, row.get("chain_id") or None, True)

    class _ButtonList(list):
        def __init__(self) -> None:
            super().__init__()
            self.refreshers: list = []

    buttons = _ButtonList()
    specs = (
        ("finish", FINISH_LABEL, "finish", False, (12, 0), "Mark the selected mission done and apply its unlock."),
        ("unlock", UNLOCK_LABEL, "unlock", False, (4, 0), UNLOCK_TIP),
        ("finish_to", FINISH_TO_LABEL, "finish", True, (4, 0), "Mark this step, and the steps before it in the chain, done."),
        ("reset", RESET_LABEL, "reset", False, (4, 0), "Set the selected mission back to not started."),
        ("reset_from", RESET_FROM_LABEL, "reset", True, (4, 0), "Set this step, and the steps after it, back to not started."),
    )
    unlock_button = None
    unlock_cover = None
    named: dict = {}
    tips: dict = {}
    unlock_tips: list[dict] = []
    for key, text, action, chain, pad, tip in specs:
        if action == "unlock":
            wrap = ttk.Frame(bar)
            wrap.pack(side="left", padx=pad)
            button = ttk.Button(wrap, text=text, command=lambda action=action, chain=chain: prepare(action, chain))
            button.pack()
            style = ttk.Style(wrap)
            disabled_fg = style.lookup("TButton", "foreground", ("disabled",)) or "#a3a3a3"
            disabled_bg = style.lookup("TButton", "background", ("disabled",)) or "#d9d9d9"
            disabled_font = style.lookup("TButton", "font") or "TkDefaultFont"
            cover = tk.Label(
                wrap,
                text=text,
                fg=disabled_fg,
                bg=disabled_bg,
                font=disabled_font,
                relief="raised",
                borderwidth=1,
                highlightthickness=0,
                padx=3,
                pady=3,
                cursor="arrow",
            )
            unlock_button = button
            unlock_cover = cover
            holder = add_tip(button, tip)
            cover_holder = add_tip(cover, tip)
            unlock_tips.append(holder)
            unlock_tips.append(cover_holder)
            tips[key] = holder
        else:
            button = ttk.Button(bar, text=text, command=lambda action=action, chain=chain: prepare(action, chain))
            button.pack(side="left", padx=pad)
            tips[key] = add_tip(button, tip)
        named[key] = button
        buttons.append(button)

    def refresh_unlock(*_args: object) -> None:
        if unlock_button is None or unlock_cover is None:
            return
        row = selected()
        line = is_quest_line(row)
        if session.game_files and not session.tables_cached():
            reason = "Mission tables are still loading."
        elif line:
            next_id = row.get("next_id") or ""
            if not next_id or not (row.get("unfinished_ids") or []):
                reason = FINISHED_LINE
            else:
                try:
                    reason = session.unlock_reason(next_id, load=False)
                except Exception as exc:
                    reason = str(exc)
        elif row is None:
            reason = "Select a mission first."
        elif not row.get("mission_id"):
            reason = "Select a mission, not the chain title."
        else:
            try:
                reason = session.unlock_reason(row.get("mission_id"), load=False)
            except Exception as exc:
                reason = str(exc)
        fallback = button_labels(line)["unlock_tip"]
        for holder in unlock_tips:
            set_tip(holder, reason or fallback)
        if reason:
            unlock_button.state(["disabled"])
            unlock_cover.place(relx=0, rely=0, relwidth=1, relheight=1)
            unlock_cover.lift()
        else:
            unlock_cover.place_forget()
            unlock_button.state(["!disabled"])

    def refresh_actions(*_args: object) -> None:
        with timed("selection"):
            _refresh_actions_now()

    def _refresh_actions_now() -> None:
        row = selected()
        line = is_quest_line(row)
        labels = button_labels(line)
        named["finish"].configure(text=labels["finish"])
        named["unlock"].configure(text=labels["unlock"])
        named["reset"].configure(text=labels["reset"])
        if unlock_cover is not None:
            unlock_cover.configure(text=labels["unlock"])
        line_finished = bool(line) and not (row.get("unfinished_ids") or [])
        missing_unlocks = False
        if line_finished and row.get("chain_id"):
            try:
                missing_unlocks = session.line_needs_unlocks(str(row["chain_id"]))
            except Exception:
                missing_unlocks = False
            row["missing_unlocks"] = missing_unlocks
        from nmsmissions.edit import READ_GAME_FILES_TIP, chain_ids_through, missions_need_game_files

        no_extract = session.game_files is None
        line_needs_files = False
        step_needs_files = False
        through_needs_files = False
        if no_extract and not (line_finished and missing_unlocks):
            if line:
                targets = list(row.get("main_unfinished") or [])
                line_needs_files = bool(targets) and missions_need_game_files(targets)
            elif row and row.get("mission_id"):
                mission_id = str(row["mission_id"])
                step_needs_files = missions_need_game_files([mission_id])
                through_needs_files = missions_need_game_files(
                    chain_ids_through(str(row.get("chain_id") or "") or None, mission_id)
                )
        if line_finished and missing_unlocks:
            finish_tip = "Apply the missing unlocks for this quest line. The steps stay as they are."
        elif line_needs_files or step_needs_files:
            finish_tip = READ_GAME_FILES_TIP
        elif line_finished:
            finish_tip = FINISHED_LINE
        else:
            finish_tip = labels["finish_tip"]
        set_tip(tips["finish"], finish_tip)
        if "finish_to" in tips:
            if through_needs_files or step_needs_files:
                set_tip(tips["finish_to"], READ_GAME_FILES_TIP)
            else:
                set_tip(tips["finish_to"], "Mark this step, and the steps before it in the chain, done.")
        set_tip(tips["reset"], labels["reset_tip"])
        if "unlock" in tips and not line_finished:
            set_tip(tips["unlock"], labels["unlock_tip"])
        loading = bool(session.game_files) and not session.tables_cached()
        has_row = bool(tree.selection())
        for key, button in named.items():
            if button is unlock_button:
                continue
            if line and key in {"finish_to", "reset_from"}:
                button.state(["disabled"])
            elif key == "finish" and line_finished and not missing_unlocks:
                button.state(["disabled"])
            elif key == "finish" and (line_needs_files or step_needs_files):
                button.state(["disabled"])
            elif key == "finish_to" and (through_needs_files or step_needs_files):
                button.state(["disabled"])
            elif loading or not has_row:
                button.state(["disabled"])
            else:
                button.state(["!disabled"])
        refresh_unlock()

    tree.bind("<<TreeviewSelect>>", refresh_actions, add="+")
    window._refresh_actions = refresh_actions
    refresh_actions()
    buttons.refreshers = [refresh_actions]
    return buttons


def _preview(window, session, plan, banner, reload_rows, refresh_status) -> None:
    import tkinter as tk
    from tkinter import ttk

    from nmsmissions.edit import format_plan

    top = tk.Toplevel(window)
    top.title("Preview")
    top.geometry("760x560")
    text = tk.Text(top, wrap="word")
    text.insert("1.0", format_plan(plan, dry_run=True, apply_hint="Press Write to copy. This does not put the save into the game."))
    text.configure(state="disabled")
    text.pack(fill="both", expand=True, padx=8, pady=8)
    actions = ttk.Frame(top, padding=8)
    actions.pack(fill="x")

    def write() -> None:
        def go(running: bool) -> None:
            if running:
                _keep_banner(banner, "No Man's Sky is running. Close it before writing a save.", None)
                return
            try:
                code, message = session.apply_last(running=running)
            except Exception as exc:
                _keep_banner(banner, str(exc), None)
                return
            if code == 0:
                _keep_banner(banner, message, "#0b6e4f")
                refresh_open_save(session, reload_rows, refresh_status)
                top.destroy()
                return
            _keep_banner(banner, message, None)

        _fresh_running(top, go, banner)

    button = ttk.Button(actions, text="Write to copy", command=write)
    add_tip(button, "Saves this change in the copy. It is not in the game yet.")
    if plan.failing() or not plan.changes:
        button.state(["disabled"])
    button.pack(side="left")
    ttk.Button(actions, text="Close", command=top.destroy).pack(side="left", padx=8)


def add_tip(widget, text: str) -> dict:
    """Short hover text. Plain words, one or two lines. The returned holder can change the words."""
    holder = {"text": text, "win": None}
    widget.nms_tip = holder

    def show(_event) -> None:
        if holder["win"] is not None:
            return
        try:
            import tkinter as tk
        except Exception:
            return
        x_pos = widget.winfo_rootx() + 12
        y_pos = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x_pos}+{y_pos}")
        tk.Label(
            win,
            text=holder["text"],
            background="#fff8dc",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            wraplength=320,
            justify="left",
        ).pack()
        holder["win"] = win

    def hide(_event) -> None:
        if holder["win"] is not None:
            holder["win"].destroy()
            holder["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    return holder


def set_tip(holder: dict, text: str) -> None:
    holder["text"] = text


def begin_game_file_read(window, session, banner, reload_rows, ask: bool = False) -> None:
    """Read mission and language tables on a background thread.

    The window stays responsive. Progress is applied from a queue.
    """
    from tkinter import messagebox

    if getattr(window, "_game_read_busy", False):
        return
    pcbanks = getattr(session, "pcbanks", None)
    if ask:
        if pcbanks is None:
            return
        agreed = messagebox.askyesno(
            "Read my game files",
            "Read mission names, rewards, and item tables from your game?\n"
            "This uses your PCBANKS folder. It does not change the game.\n"
            "The first read can take several minutes. The window stays open.",
            parent=window,
        )
        if not agreed:
            return
    if pcbanks is None:
        from nmsmissions.gameread import resolve_pcbanks
        from nmsmissions.locate import pick_directory

        chosen = pick_directory(window, "Choose the PCBANKS folder inside the game")
        if chosen is None:
            return
        pcbanks = resolve_pcbanks(chosen)
        if pcbanks is None:
            messagebox.showinfo(
                "Read my game files",
                "That folder does not look like PCBANKS.\nChoose the PCBANKS folder inside the game.",
                parent=window,
            )
            return
        session.pcbanks = pcbanks

    window._game_read_busy = True
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            from nmsmissions.gameread import read_game_files

            dest = read_game_files(pcbanks, progress=lambda message: events.put(("progress", message)))
            events.put(("done", dest))
        except Exception as exc:
            events.put(("error", exc))

    threading.Thread(target=work, name="nms-game-read", daemon=True).start()

    def finish_names() -> None:
        try:
            alive = window.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            return
        error = getattr(session, "_name_error", None)
        if error is not None:
            messagebox.showerror("Read my game files", f"Mission names could not be read.\n{error}", parent=window)
            return
        loader = getattr(session, "_name_loader", None)
        if getattr(session, "_names_report", None) is None and loader is not None and loader.is_alive():
            window.after(100, finish_names)
            return
        prepare = getattr(window, "_prepare_rows", None)
        tree = getattr(window, "_list_tree", None)
        holder = getattr(window, "_list_holder", None)
        urls = getattr(window, "_list_urls", None)
        by_iid = getattr(window, "_list_by_iid", None)
        if prepare is None or tree is None or holder is None or urls is None or by_iid is None:
            try:
                reload_rows()
            except Exception as exc:
                _keep_banner(banner, f"The mission list could not be updated. {exc}", None)
                return
            _keep_banner(banner, "Mission names are loaded from your game files.", "#0b6e4f")
            refresh = getattr(window, "_refresh_actions", None)
            if refresh is not None:
                refresh()
            return

        # Cache load already finished on the name thread. Building the name
        # map and the rows stays off this thread. Tk only applies the result.
        box: dict = {}

        def work() -> None:
            try:
                box["result"] = prepare()
            except Exception as exc:
                box["error"] = exc

        def apply() -> None:
            try:
                alive = window.winfo_exists()
            except tk.TclError:
                return
            if not alive:
                return
            if "error" in box:
                _keep_banner(banner, f"The mission list could not be updated. {box['error']}", None)
                return
            if "result" not in box:
                window.after(30, apply)
                return
            result = box["result"]
            if len(result) == 4:
                _roots, _other, subtitle, rows = result
            else:
                _roots, _other, subtitle = result
                rows = forest_rows(_roots, _other)
            try:
                apply_name_refresh(window, tree, holder, urls, by_iid, banner, subtitle, rows, session)
            except Exception as exc:
                _keep_banner(banner, f"The mission list could not be updated. {exc}", None)
                return
            _keep_banner(banner, "Mission names are loaded from your game files.", "#0b6e4f")
            refresh = getattr(window, "_refresh_actions", None)
            if refresh is not None:
                refresh()

        threading.Thread(target=work, name="nms-name-rows", daemon=True).start()
        window.after(30, apply)

    def poll() -> None:
        try:
            alive = window.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            return
        saw_done = None
        saw_error = None
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                _keep_banner(banner, str(payload), None)
            elif kind == "done":
                saw_done = payload
            else:
                saw_error = payload
        if saw_error is not None:
            window._game_read_busy = False
            messagebox.showerror("Read my game files", str(saw_error), parent=window)
            return
        if saw_done is not None:
            window._game_read_busy = False
            session.adopt_game_files(saw_done)
            session.start_name_load()
            session.start_table_load()
            _keep_banner(banner, "Game files read. Loading names…", "#0b6e4f")
            window.after(100, finish_names)
            return
        window.after(100, poll)

    window.after(100, poll)


def _save_menu(window, session, banner, refresh_status, reload_rows) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    menu = tk.Menu(window)
    save_menu = tk.Menu(menu, tearoff=0)

    def change() -> None:
        result = choose_working_copy(
            window,
            getattr(session, "mapping", {}) or {},
            _copy_root(session),
            session.backup_dir,
        )
        if result is None:
            return
        _adopt(session, result)
        refresh_open_save(session, reload_rows, refresh_status)
        _keep_banner(banner, result.message, "#0b6e4f")

    def backup() -> None:
        from nmsmissions.edit import EditError, backup_now

        try:
            path = backup_now(session.save, session.backup_dir)
        except EditError as exc:
            messagebox.showerror("Back up", str(exc))
            return
        messagebox.showinfo("Back up", f"Backup saved.\n{path}\nThe save was not changed.")
        _keep_banner(banner, f"Backup: {path}", "#0b6e4f")

    def restore() -> None:
        _restore_dialog(window, session, banner, refresh_status, reload_rows)

    def install() -> None:
        _install_dialog(window, session, banner, refresh_status)

    def undo() -> None:
        _undo_dialog(window, session, banner, refresh_status, reload_rows)

    def about() -> None:
        from nmsmissions import about_body

        messagebox.showinfo(
            "About / Tip me",
            about_body(str(getattr(session, "version_note", "") or "")),
            parent=window,
        )

    for label, command in (
        ("Change save", change),
        ("Back up this save now", backup),
        ("Restore a backup...", restore),
        ("Put edited save into my game", install),
        ("Undo last put-into-game", undo),
    ):
        save_menu.add_command(label=label, command=command)
    menu.add_cascade(label="Save", menu=save_menu)
    window.config(menu=menu)

    row = ttk.Frame(window, padding=(8, 0, 8, 4))
    row.pack(fill="x", before=banner)
    tips = {
        "Change save": "Pick another slot. The game folder is copied first.",
        "Back up this save now": "Make a zip of this copy. The game file is not changed.",
        "Restore a backup...": "Put a backup zip back into this copy. The game file stays as it is.",
        "Put edited save into my game": "Copy this save into the game slot. The game must be closed.",
        "Undo last put-into-game": "Put the game file back to how it was just before the last put-into-game.",
        "About / Tip me": "Version, credits, and a tip link.",
        "Read my game files": "Read mission names, rewards, and item tables from your game. The game files stay where they are.",
        "Clear cache": "Delete the copied game files from this PC. The game is not changed.",
    }

    def read_files() -> None:
        begin_game_file_read(window, session, banner, reload_rows)

    def clear_cache() -> None:
        if getattr(window, "_game_read_busy", False):
            return

        def work() -> None:
            error = None
            try:
                from nmsmissions.gameread import clear_game_cache

                clear_game_cache()
            except Exception as exc:
                error = exc

            def done() -> None:
                try:
                    alive = window.winfo_exists()
                except tk.TclError:
                    return
                if not alive:
                    return
                window._game_read_busy = False
                if hasattr(session, "drop_game_files"):
                    session.drop_game_files()
                banner.status_line = "Cache cleared."
                try:
                    reload_rows()
                except Exception as exc:
                    _keep_banner(banner, f"The mission list could not be updated. {exc}", None)
                    return
                if error is not None:
                    _keep_banner(banner, f"The cache could not be cleared. {error}", None)
                    return
                _keep_banner(banner, "Cache cleared.", "#0b6e4f")

            window.after(0, done)

        window._game_read_busy = True
        threading.Thread(target=work, name="nms-clear-cache", daemon=True).start()

    for label, command in (
        ("Change save", change),
        ("Back up this save now", backup),
        ("Restore a backup...", restore),
        ("Put edited save into my game", install),
        ("Undo last put-into-game", undo),
        ("Read my game files", read_files),
        ("Clear cache", clear_cache),
        ("About / Tip me", about),
    ):
        button = ttk.Button(row, text=label, command=command)
        button.pack(side="left", padx=(0, 6))
        add_tip(button, tips[label])


def _copy_root(session):
    from nmsmissions.slots import working_root

    root = getattr(session, "copy_root", None)
    return root if root is not None else working_root()


def _adopt(session, result) -> None:
    from nmsmissions.guard import is_live_nms_save

    session.save = result.copy_path
    session.origin = result.origin
    session.copy_dir = result.copy_dir
    session.slot = result.slot
    session.account = result.account
    session.live = is_live_nms_save(result.copy_path)
    hook = getattr(session, "on_save_path", None)
    if hook is not None:
        hook(result.copy_path)


def choose_working_copy(parent, mapping: dict, copy_root, backup_dir=None):
    """Show the slot list. Returns a PrepareResult, or None if cancelled."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    from nmsmissions.slots import (
        copy_dir_for,
        group_slots,
        nms_save_folders,
        prepare_working_copy,
        read_summary,
        remember_slot,
        remembered_slot,
        slot_row,
    )

    folders = nms_save_folders()
    if not folders:
        from nmsmissions.locate import offer_save_picker

        folders = offer_save_picker(parent)
        if not folders:
            return None
    rows = []
    for folder in folders:
        for group in group_slots(folder):
            mode, play = read_summary(group.newer, mapping)
            rows.append((group, slot_row(group, mode, play)))
    if not rows:
        messagebox.showerror("No saves", "That folder has no save files.", parent=parent)
        return None

    dialog = tk.Toplevel(parent)
    dialog.title("Choose a save")
    dialog.geometry("860x420")
    ttk.Label(
        dialog,
        text="Pick a slot. The newer file is copied next to this app. The game folder is not changed.",
        padding=8,
        wraplength=820,
    ).pack(fill="x")
    frame = ttk.Frame(dialog, padding=8)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=("detail",), show="headings", selectmode="browse")
    tree.heading("detail", text="Slot, mode, play time, last saved, newer file")
    tree.column("detail", width=820, stretch=True)
    tree.pack(fill="both", expand=True)
    for index, (_group, text) in enumerate(rows):
        tree.insert("", "end", iid=str(index), values=(text,))
    remembered = remembered_slot(copy_root)
    selected = "0"
    if remembered is not None:
        account, slot = remembered
        for index, (group, _text) in enumerate(rows):
            if group.slot == slot and group.account == account:
                selected = str(index)
                break
    tree.selection_set(selected)
    tree.focus(selected)
    chosen: dict = {"result": None}

    def open_selected() -> None:
        picked = tree.selection()
        if not picked:
            messagebox.showinfo("Choose a save", "Select a slot first.", parent=dialog)
            return
        group = rows[int(picked[0])][0]
        dest = copy_dir_for(copy_root, group.account, group.slot)
        result = prepare_working_copy(
            group.newer,
            dest,
            slot=group.slot,
            account=group.account,
            backup_dir=backup_dir,
        )
        if result.action == "ask":
            answer = messagebox.askyesnocancel(
                "Both saves changed",
                "The game save changed, and your copy has edits.\n"
                "This also covers the other file of this slot.\n\n"
                "Yes = use the game save. Your edits are backed up first.\n"
                "No = keep your edits.\n"
                "Cancel = go back.",
                parent=dialog,
            )
            if answer is None:
                return
            result = prepare_working_copy(
                group.newer,
                dest,
                slot=group.slot,
                account=group.account,
                choice="game" if answer else "edits",
                backup_dir=backup_dir,
            )
        remember_slot(copy_root, group.account, group.slot)
        chosen["result"] = result
        dialog.destroy()

    tree.bind("<Double-Button-1>", lambda _event: open_selected())
    buttons = ttk.Frame(dialog, padding=8)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Open this slot", command=open_selected).pack(side="left")
    ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=8)
    dialog.transient(parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return chosen["result"]


def _restore_dialog(window, session, banner, refresh_status, reload_rows) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    from nmsmissions.edit import EditError, EditRequest, default_backup_dir, run
    from nmsmissions.slots import filter_backups, list_backups, restore_slot_warning

    directory = session.backup_dir or default_backup_dir(session.slot)
    backups = list_backups(directory)
    if not backups:
        messagebox.showinfo("Restore a backup", f"No backup zips in {directory}.", parent=window)
        return
    dialog = tk.Toplevel(window)
    dialog.title("Restore a backup")
    dialog.geometry("720x420")
    ttk.Label(
        dialog,
        text="This replaces the working copy. It does not go into the game until you use Put edited save into my game.",
        padding=8,
        wraplength=680,
    ).pack(fill="x")
    show_all = tk.BooleanVar(value=False)
    show_box = ttk.Checkbutton(dialog, text="Show all slots", variable=show_all)
    show_box.pack(anchor="w", padx=8)
    add_tip(show_box, "The list starts with this slot only. Tick this to see other slots.")
    tree = ttk.Treeview(dialog, columns=("detail",), show="headings", selectmode="browse")
    tree.heading("detail", text="Date, slot, file")
    tree.column("detail", width=680, stretch=True)
    tree.pack(fill="both", expand=True, padx=8, pady=8)
    shown: list = []

    def refill(*_args) -> None:
        nonlocal shown
        shown = filter_backups(backups, session.slot, bool(show_all.get()))
        tree.delete(*tree.get_children())
        for index, item in enumerate(shown):
            tree.insert("", "end", iid=str(index), values=(item.label(),))
        if shown:
            tree.selection_set("0")

    show_all.trace_add("write", refill)
    refill()

    def go() -> None:
        picked = tree.selection()
        if not picked:
            messagebox.showinfo("Restore a backup", "Select a backup first.", parent=dialog)
            return
        item = shown[int(picked[0])]
        warning = restore_slot_warning(item.slot, session.slot, item.when_text, item.save_name)
        prompt = warning or f"Replace the working copy with this backup?\n{item.label()}\nThe game save is not changed."
        if not messagebox.askokcancel("Restore a backup", prompt, parent=dialog):
            return
        target_name = None
        if item.slot is not None and session.slot is not None and item.slot != session.slot:
            target_name = session.save.name
        from nmsmissions.guard import is_live_nms_save

        def finish(running: bool) -> None:
            if running:
                messagebox.showerror(
                    "Restore a backup",
                    "No Man's Sky is running. Close it before restoring into the game folder.",
                    parent=dialog,
                )
                return
            try:
                code = run(
                    EditRequest(
                        action="restore",
                        restore_zip=item.path,
                        restore_to=session.save.parent,
                        restore_name=target_name,
                        apply=True,
                        backup_dir=directory,
                        quiet=True,
                        running=running,
                    )
                )
            except EditError as exc:
                messagebox.showerror("Restore a backup", str(exc), parent=dialog)
                return
            dialog.destroy()
            if code != 0:
                messagebox.showerror("Restore a backup", f"Restore stopped (code {code}).", parent=window)
                return
            from nmsmissions.slots import restored_file_name

            written = restored_file_name(item.save_name, session.save.name, item.slot, session.slot)
            hook = getattr(session, "on_save_path", None)
            restored = session.save.parent / written
            if restored.is_file() and hook is not None and restored.resolve() != session.save.resolve():
                session.save = restored
                hook(restored)
            refresh_open_save(session, reload_rows, refresh_status)
            _keep_banner(banner, f"Restored {written}. Not in the game yet.", "#0b6e4f")

        if is_live_nms_save(session.save.parent):
            _fresh_running(dialog, finish, banner)
        else:
            finish(False)

    ttk.Button(dialog, text="Restore this backup", command=go).pack(side="left", padx=8, pady=8)
    ttk.Button(dialog, text="Cancel", command=dialog.destroy).pack(side="left")
    dialog.transient(window)
    dialog.grab_set()


def _install_dialog(window, session, banner, refresh_status) -> None:
    from tkinter import messagebox

    from nmsmissions.edit import STEAM_WARNING, EditError, install_one_save, partner_load_problem
    from nmsmissions.slots import already_in_game_note, load_state, note_installed

    if session.origin is None or session.slot is None:
        messagebox.showinfo(
            "Put save into the game",
            "Open a slot from the save list first. A file you opened from a path is not put back on its own.",
            parent=window,
        )
        return
    source_mtime = None
    if session.copy_dir is not None:
        state = load_state(session.copy_dir)
        if state is not None and state.source_mtime_ns:
            source_mtime = int(state.source_mtime_ns)
    partner_note = partner_load_problem(session.save, session.origin.parent, source_mtime)
    same = already_in_game_note(session.save, session.origin)
    if partner_note and same:
        _keep_banner(banner, partner_note, None)
        messagebox.showinfo(
            "Put save into the game",
            partner_note + "\nThis copy matches the same-named file, so Put will not change what the game loads.",
            parent=window,
        )
        return
    if same:
        _keep_banner(banner, same, "#0b6e4f")
        messagebox.showinfo("Put save into the game", same, parent=window)
        return
    slot = session.slot
    if partner_note:
        question = (
            f"{partner_note}\n"
            f"Put {session.save.name} into slot {slot} anyway?\n"
            f"Folder: {session.origin.parent}\n"
            "The game must stay closed. A backup of the game file is made first.\n"
            f"{STEAM_WARNING}"
        )
    else:
        question = (
            f"Put {session.save.name} into slot {slot}?\n"
            f"Folder: {session.origin.parent}\n"
            "The game must stay closed. A backup of the game file is made first.\n"
            f"{STEAM_WARNING}"
        )
    if not messagebox.askokcancel("Put save into the game", question, parent=window):
        return

    def go(running: bool) -> None:
        if running:
            messagebox.showerror(
                "Put save into the game",
                "No Man's Sky is running. Close it, then try again.",
                parent=window,
            )
            return
        try:
            _code, message, undo_zip = install_one_save(
                session.save,
                session.origin.parent,
                slot,
                session.backup_dir,
                apply=True,
                live=True,
                confirm_slot=slot,
                allow_partner=bool(partner_note),
                source_mtime_ns=source_mtime,
                running=running,
            )
        except EditError as exc:
            messagebox.showerror("Put save into the game", str(exc), parent=window)
            return
        if session.copy_dir is not None and session.origin is not None:
            written = session.origin.parent / session.save.name
            note_installed(session.copy_dir, written, session.save, undo_zip, session.backup_dir)
            session.origin = written
        refresh_status()
        _keep_banner(banner, message, "#0b6e4f")
        messagebox.showinfo("Put save into the game", message, parent=window)

    _fresh_running(window, go, banner)


def finish_reload(window, timeout: float = 8.0) -> None:
    """Wait until a background list reload has been applied. Tests use this."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        worker = getattr(window, "_reload_worker", None)
        if worker is not None and worker.is_alive():
            worker.join(0.05)
        apply = getattr(window, "_reload_apply", None)
        if apply is not None:
            apply()
        if not getattr(window, "_reload_busy", False):
            return
        try:
            window.update()
        except Exception:
            return
    raise TimeoutError("The mission list did not finish reloading.")


def refresh_open_save(session, reload_rows, refresh_status) -> None:
    """The open file was replaced. Drop the cached parse and rebuild the list."""
    invalidate = getattr(session, "invalidate_snapshot", None)
    if invalidate is not None:
        invalidate()
    if reload_rows is not None:
        reload_rows()
    if refresh_status is not None:
        refresh_status()


def _undo_dialog(window, session, banner, refresh_status, reload_rows=None) -> None:
    from tkinter import messagebox

    from pathlib import Path

    from nmsmissions.slots import load_state

    if session.copy_dir is None:
        messagebox.showinfo(
            "Undo last put-into-game",
            "Open a slot from the save list first.",
            parent=window,
        )
        return
    state = load_state(session.copy_dir)
    if state is None or not state.undo_zip:
        messagebox.showinfo(
            "Undo last put-into-game",
            "Nothing to undo. Put a save into the game first.",
            parent=window,
        )
        return
    _undo_after_confirm(window, session, banner, refresh_status, reload_rows, state)


def _undo_after_confirm(window, session, banner, refresh_status, reload_rows, state) -> None:
    from tkinter import messagebox

    from nmsmissions.edit import STEAM_WARNING, EditError, installed_live_path, newer_slot_files, undo_last_install
    from nmsmissions.slots import file_sha

    slot = state.slot or session.slot
    allow_changed = False
    allow_partner = False
    live_path = installed_live_path(state)
    kind, newer = newer_slot_files(state)
    if kind == "partner_lost":
        names = " and ".join(path.name for path in newer)
        messagebox.showerror(
            "Undo last put-into-game",
            f"The game saved {names} after the put. The game will load that file.\n"
            "There is no copy of it from before the put, so Undo cannot change what the game will load.\n"
            "Use Restore a backup.",
            parent=window,
        )
        return
    if kind == "partner":
        names = " and ".join(path.name for path in newer)
        if not messagebox.askokcancel(
            "Undo last put-into-game",
            f"The game saved {names} after the put. The game will load that file.\n"
            f"Restoring {live_path.name} alone does not undo the slot.\n"
            f"Put {names} back to how it was before the put?\n"
            "The current file is backed up first.\n"
            f"{STEAM_WARNING}",
            parent=window,
        ):
            return
        allow_partner = True
        allow_changed = True
    changed = False
    if not allow_partner and live_path.is_file() and state.installed_sha256:
        changed = file_sha(live_path) != state.installed_sha256
    if changed:
        if not messagebox.askokcancel(
            "Undo last put-into-game",
            f"The game saved again after you put slot {slot} in.\n"
            "Undo would replace that newer file.\n"
            "The current game file is backed up first.\n"
            f"{STEAM_WARNING}",
            parent=window,
        ):
            return
        allow_changed = True
    elif not allow_partner and not messagebox.askokcancel(
        "Undo last put-into-game",
        f"Put slot {slot} back to the file from before the last put-into-game?\n"
        "The game file you have now is backed up first.\n"
        f"{STEAM_WARNING}",
        parent=window,
    ):
        return

    def finish(running: bool) -> None:
        if running:
            messagebox.showerror(
                "Undo last put-into-game",
                "No Man's Sky is running. Close it, then try again.",
                parent=window,
            )
            return
        try:
            _code, message = undo_last_install(
                session.copy_dir,
                session.backup_dir,
                apply=True,
                live=True,
                confirm_slot=slot,
                allow_changed=allow_changed,
                allow_partner=allow_partner,
                running=running,
            )
        except EditError as exc:
            messagebox.showerror("Undo last put-into-game", str(exc), parent=window)
            return
        from nmsmissions.slots import point_session_at_state

        point_session_at_state(session)
        set_banner_mode(banner, str(session.save), live=bool(session.live), editing=True)
        refresh_open_save(session, reload_rows, refresh_status)
        _keep_banner(banner, message, "#0b6e4f")
        messagebox.showinfo("Undo last put-into-game", message, parent=window)

    _fresh_running(window, finish, banner)


def names_header(text: str) -> str:
    """The game-files names line, when the banner subtitle has one."""
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("Names from game files"):
            return stripped
    return ""


def banner_mode_line(subtitle: str, *, live: bool, editing: bool) -> str:
    """Top line plus the save path. Change save rebuilds this from the new path."""
    if not editing:
        prefix = "Read-only. This window does not change the save."
    elif live:
        prefix = "This is the game folder. Be careful."
    else:
        prefix = "This is a copy. Nothing is written until you press Write to copy."
    return prefix + "\n" + subtitle


def _status_line(banner) -> str:
    return str(getattr(banner, "status_line", "") or "")


def _with_status(banner, body: str) -> str:
    """Keep the latest status line when the path line is rebuilt."""
    status = _status_line(banner)
    body = body or ""
    if not status or status in body.splitlines():
        return body
    return f"{body}\n{status}" if body else status


def _banner_text(banner, body: str) -> None:
    banner.configure(text=_with_status(banner, body))


def set_banner_mode(banner, subtitle: str, *, live: bool, editing: bool) -> None:
    """Replace the path line. A names header and the status line stay."""
    kept = names_header(subtitle) or names_header(getattr(banner, "mode_line", ""))
    body = subtitle
    if kept and not names_header(subtitle):
        body = subtitle.rstrip("\n") + "\n" + kept
    banner.mode_line = banner_mode_line(body, live=live, editing=editing)
    banner.configure(text=_with_status(banner, banner.mode_line), fg="#9b1c1c" if live else "#1c2430")


def _keep_banner(banner, extra: str, colour: str | None) -> None:
    banner.status_line = extra
    mode = getattr(banner, "mode_line", "")
    text = f"{mode}\n{extra}" if mode else extra
    if colour:
        banner.configure(text=text, fg=colour)
    else:
        banner.configure(text=text)

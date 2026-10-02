"""Selection and preview timing on a large synthetic save.

The save is generated here. It is not a copy of a played game.
"""

from __future__ import annotations

def _isolate_test_dirs() -> None:
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from tests.harness import isolate_test_dirs
    except ImportError:
        from harness import isolate_test_dirs

    isolate_test_dirs()


_isolate_test_dirs()


def setUpModule() -> None:
    _isolate_test_dirs()


import io
import os
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nmsmissions.chains import ChainView, OtherView, StepView
from nmsmissions.edit import (
    EditorSession,
    EditRequest,
    _PARTNER_PLAY,
    _version,
    build_plan,
    collect_checks,
    format_plan,
    take_snapshot,
    unlock_block_reason,
)
from nmsmissions.gui import apply_name_refresh, flush_paint_chunks, launch, paint_rows
from tests.harness import TkCleanup
from tests.test_edit import _row, _save_body, _text, _write_pair


def _best(fn, repeats: int = 3) -> float:
    times = []
    for _ignored in range(repeats):
        started = time.perf_counter()
        fn()
        times.append((time.perf_counter() - started) * 1000.0)
    return min(times)


def _heavy(folder: Path, missions: int = 2000, extras: int = 150000) -> Path:
    rows = [_row(f"^M{i:04d}", -1) for i in range(missions)]
    body = _save_body(dwb=rows)
    body["disc"] = [{"id": f"D{i:06d}", "n": i, "s": f"system-name-{i}"} for i in range(extras)]
    text = _text(body)
    path = _write_pair(folder, "save7.hg", text)
    partner = _write_pair(folder, "save8.hg", text)
    os.utime(partner, ns=(1_000_000_000, 1_000_000_000))
    return path


class PerfTests(TkCleanup, unittest.TestCase):
    def test_warm_table_cache_is_under_three_seconds(self) -> None:
        import time

        from nmsmissions.gamedata import load_tables
        from nmsmissions.gamenames import load_game_names

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "gamefiles"
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            rows = []
            for index in range(4000):
                rows.append(
                    "<Property name='TkLocalisationEntry'>"
                    f"<Property name='Id' value='K{index}'/>"
                    f"<Property name='English' value='Mission title {index}'/>"
                    "</Property>"
                )
            (loc / "NMS_LOC1_ENGLISH.EXML").write_text("<Data>" + "".join(rows) + "</Data>", encoding="utf-8")
            for index in range(30):
                (missions / f"m{index}.EXML").write_text(
                    "<Data><Property name='GcGenericMissionSequence'>"
                    f"<Property name='MissionID' value='^M{index}'/>"
                    "<Property name='MissionTitles'>"
                    f"<Property name='Format' value='K{index}'/><Property name='Count' value='1'/>"
                    "</Property>"
                    "<Property name='FinalStageVersions'><Property>"
                    "<Property name='Version' value='39'/><Property name='Progress' value='2'/>"
                    "</Property></Property></Property></Data>",
                    encoding="utf-8",
                )
            cache = Path(tmp) / "tables.json"
            names = Path(tmp) / "names.json"
            seen = []

            def progress(message: str) -> None:
                seen.append(message)

            started = time.perf_counter()
            cold = load_tables(root, cache=cache, progress=progress)
            cold_ms = (time.perf_counter() - started) * 1000.0
            self.assertFalse(cold.from_cache)
            self.assertEqual(len(cold.missions), 30)
            self.assertTrue(any(item.startswith("Loading mission tables…") for item in seen))
            started = time.perf_counter()
            warm = load_tables(root, cache=cache)
            warm_ms = (time.perf_counter() - started) * 1000.0
            self.assertTrue(warm.from_cache)
            self.assertEqual(warm.missions["^M0"].finals, [(39, 2)])
            self.assertLess(warm_ms, 3000.0)
            self.assertLess(warm_ms, cold_ms)
            name_seen = []
            started = time.perf_counter()
            first = load_game_names(root, cache=names, progress=lambda message: name_seen.append(message))
            name_cold = (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            second = load_game_names(root, cache=names)
            name_warm = (time.perf_counter() - started) * 1000.0
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(second.names.get("^M0"), "Mission title 0")
            self.assertLess(name_warm, 3000.0)
            self.assertTrue(any("Loading mission names…" in item for item in name_seen))
            print(
                f"perf tables cold {cold_ms:.1f} ms warm {warm_ms:.1f} ms "
                f"names cold {name_cold:.1f} ms warm {name_warm:.1f} ms",
                flush=True,
            )

    def test_timing_flag_writes_a_log_in_the_app_folder(self) -> None:
        from nmsmissions.timing import log_path, timed

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "timing.log"
            with patch.dict(os.environ, {"NMSMISSIONS_TIMING": "1", "NMSMISSIONS_TIMING_LOG": str(path)}):
                with timed("unit-phase"):
                    pass
                self.assertEqual(log_path(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("timing unit-phase:", text)
            self.assertIn(" ms", text)
    def test_preview_does_not_change_the_cached_save(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path)
            first = session.plan_for("unlock", "^OPEN", None, False)
            second = session.plan_for("unlock", "^OPEN", None, False)
            self.assertTrue(first.changes)
            self.assertEqual([change.label for change in first.changes], [change.label for change in second.changes])

    def test_selection_reads_the_save_once(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            calls = {"n": 0}
            real = take_snapshot

            def counting(save):
                calls["n"] += 1
                return real(save)

            with patch("nmsmissions.edit.take_snapshot", counting):
                session = EditorSession(path)
                session.unlock_reason("^OPEN")
                session.unlock_reason("^TEMPLATE")
                session.plan_for("unlock", "^OPEN", None, False)
            self.assertEqual(calls["n"], 1)

    def test_timing_log_names_the_phase(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path)
            buffer = io.StringIO()
            with patch.dict(os.environ, {"NMSMISSIONS_TIMING": "1"}):
                with redirect_stderr(buffer):
                    session.unlock_reason("^OPEN")
                    session.plan_for("unlock", "^OPEN", None, False)
            log = buffer.getvalue()
            self.assertIn("timing snapshot-json:", log)
            self.assertIn("timing unlock-reason:", log)
            self.assertIn("timing plan-for:", log)

    def test_large_save_selection_and_dialog(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _heavy(folder)
            mission = "^M0100"

            def cold_select() -> None:
                snap = take_snapshot(path)
                unlock_block_reason(snap.player, None, mission, _version(snap.player))

            def cold_dialog() -> None:
                _PARTNER_PLAY.clear()
                snap = take_snapshot(path)
                request = EditRequest(action="unlock", save=path, mission=mission, flag_only=False, apply=False)
                plan = build_plan(request, snap, None)
                plan.checks = collect_checks(request, snap, None)

            cold_select_ms = _best(cold_select, 2)
            cold_dialog_ms = _best(cold_dialog, 2)

            session = EditorSession(path)
            session.warm_snapshot()
            warm_select_ms = _best(lambda: session.unlock_reason(mission))

            def warm_dialog() -> None:
                plan = session.plan_for("unlock", mission, None, False)
                format_plan(plan, dry_run=True)

            warm_dialog_ms = _best(warm_dialog)
            first = session.plan_for("unlock", mission, None, False)
            second = session.plan_for("unlock", mission, None, False)
            self.assertEqual(
                [change.label for change in first.changes],
                [change.label for change in second.changes],
            )

            one = StepView(mission, "Mission", None, 9, "not_started", "not started", False, False, False, None, None, None, None)
            two = StepView("^M0101", "Next", None, 9, "not_started", "not started", False, False, False, None, None, None, None)
            chain = ChainView("side", "A side quest", "side", 0, None, None, [], [], [], False, steps=[one, two])
            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            button_ms = 0.0
            opened_ms = 0.0
            try:
                launch(
                    "Missions",
                    str(path),
                    [chain],
                    OtherView([]),
                    window=window,
                    session=session,
                    reload_cb=lambda: ([chain], OtherView([]), str(path)),
                )
                tree = next(widget for widget in _walk(window) if isinstance(widget, ttk.Treeview))
                clicks = []
                for iid in ("chain:side/^M0100", "chain:side/^M0101"):
                    tree.selection_set(iid)
                    started = time.perf_counter()
                    tree.event_generate("<<TreeviewSelect>>")
                    clicks.append((time.perf_counter() - started) * 1000.0)
                button_ms = max(clicks)

                def open_dialog() -> None:
                    plan = session.plan_for("unlock", mission, None, False)
                    top = tk.Toplevel(window)
                    text = tk.Text(top)
                    text.insert("1.0", format_plan(plan, dry_run=True, apply_hint="Press Write to copy."))
                    window.update_idletasks()
                    top.destroy()

                opened_ms = _best(open_dialog, 2)
            finally:
                window.destroy()

            print(
                f"perf cold-select {cold_select_ms:.1f} ms warm-select {warm_select_ms:.1f} ms "
                f"button {button_ms:.1f} ms cold-dialog {cold_dialog_ms:.1f} ms "
                f"dialog {warm_dialog_ms:.1f} ms opened {opened_ms:.1f} ms",
                flush=True,
            )
            self.assertLess(warm_select_ms, 100.0)
            self.assertLess(button_ms, 100.0)
            self.assertLess(opened_ms, 300.0)
            self.assertLess(warm_select_ms, cold_select_ms)
            self.assertLess(opened_ms, cold_dialog_ms)

    def test_tree_updates_only_changed_rows(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        window = tk.Tk()
        window.withdraw()
        try:
            tree = ttk.Treeview(window, columns=("name", "status", "progress"))
            rows = [
                {
                    "iid": "chain:side",
                    "parent": "",
                    "text": "Side",
                    "name": "-",
                    "status": "in progress",
                    "progress": "0/2000",
                    "tag": "in_progress",
                }
            ]
            for index in range(2000):
                rows.append(
                    {
                        "iid": f"chain:side/^M{index:04d}",
                        "parent": "chain:side",
                        "text": f"^M{index:04d}",
                        "name": f"Mission {index}",
                        "status": "not started",
                        "progress": "-",
                        "tag": "not_started",
                    }
                )
            paint_rows(tree, rows)
            kept = "chain:side/^M0010"
            tree.selection_set(kept)
            rows[11]["status"] = "done"
            rows[11]["tag"] = "done"
            started = time.perf_counter()
            paint_rows(tree, rows)
            update_ms = (time.perf_counter() - started) * 1000.0
            print(f"perf tree-update {update_ms:.1f} ms", flush=True)
            self.assertEqual(tree.selection(), (kept,))
            self.assertEqual(str(tree.item(kept)["values"][1]), "done")
            self.assertEqual(str(tree.item("chain:side/^M0001")["values"][1]), "not started")
            self.assertLess(update_ms, 100.0)
            started = time.perf_counter()
            paint_rows(tree, rows, {"chain:side"})
            open_ms = (time.perf_counter() - started) * 1000.0
            paint_rows(tree, rows, set())
            close_ms = (time.perf_counter() - started) * 1000.0
            print(f"perf tree-open {open_ms:.1f} ms tree-close {close_ms:.1f} ms", flush=True)
            self.assertLess(open_ms, 100.0)
            self.assertLess(close_ms, 100.0)
            self.assertFalse(int(tree.item("chain:side", "open")))
            self.assertEqual(tree.selection(), (kept,))
        finally:
            window.destroy()

    def test_large_name_refresh_is_chunked(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        window = tk.Tk()
        window.withdraw()
        try:
            tree = ttk.Treeview(window, columns=("name", "status", "progress"))
            rows = []
            for index in range(80):
                rows.append(
                    {
                        "iid": f"chain:side/^M{index:04d}",
                        "parent": "",
                        "text": f"^M{index:04d}",
                        "name": f"Mission {index}",
                        "status": "not started",
                        "progress": "-",
                        "tag": "not_started",
                    }
                )
            paint_rows(tree, rows)
            for row in rows:
                row["name"] = "Named " + row["name"]
            paint_rows(tree, rows)
            self.assertTrue(str(tree.item(rows[0]["iid"])["values"][0]).startswith("Named "))
            self.assertFalse(str(tree.item(rows[-1]["iid"])["values"][0]).startswith("Named "))
            flush_paint_chunks(tree)
            self.assertTrue(str(tree.item(rows[-1]["iid"])["values"][0]).startswith("Named "))
        finally:
            window.destroy()

    def test_name_refresh_stays_under_200_ms_on_the_tk_thread(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        name_count = 3746
        row_count = 664
        names = {f"^M{index:04d}": f"Title {index}" for index in range(name_count)}
        window = tk.Tk()
        window.withdraw()
        try:
            tree = ttk.Treeview(window, columns=("name", "status", "progress"))
            banner = tk.Label(window, text="Loading")
            rows = []
            for index in range(row_count):
                rows.append(
                    {
                        "iid": f"chain:side/^M{index:04d}",
                        "parent": "",
                        "text": f"^M{index:04d}",
                        "name": f"Mission {index}",
                        "status": "not started",
                        "progress": "-",
                        "tag": "not_started",
                        "wiki": "",
                    }
                )
            paint_rows(tree, rows)
            holder = {"rows": [dict(row) for row in rows], "gen": 0}
            urls: dict[str, str] = {}
            by_iid: dict[str, dict] = {}
            prepared = []
            for row in rows:
                copied = dict(row)
                copied["name"] = names[row["text"]]
                prepared.append(copied)
            self.assertEqual(len(names), name_count)
            self.assertEqual(len(prepared), row_count)
            phases = apply_name_refresh(
                window,
                tree,
                holder,
                urls,
                by_iid,
                banner,
                "Names from game files: 1 title, 1 language rows, 664 mission links, 1 files read.",
                prepared,
                None,
            )
            blocked = max(phases.values())
            print(
                f"perf post-read banner {phases['post-read-banner']:.1f} ms "
                f"tree {phases['post-read-tree']:.1f} ms",
                flush=True,
            )
            self.assertLess(blocked, 200.0)
            self.assertEqual(str(tree.item(prepared[0]["iid"])["values"][0]), names["^M0000"])
            flush_paint_chunks(window)
            self.assertEqual(str(tree.item(prepared[-1]["iid"])["values"][0]), names[prepared[-1]["text"]])
        finally:
            window.destroy()

    def test_reload_after_write_does_not_block_the_window_thread(self) -> None:
        import tkinter as tk

        from nmsmissions.gui import finish_reload, launch

        window = tk.Tk()
        window.withdraw()
        window.mainloop = lambda: None
        gate = {"go": False}

        def slow():
            deadline = time.perf_counter() + 0.35
            while time.perf_counter() < deadline:
                time.sleep(0.01)
            gate["go"] = True
            return [], OtherView([]), "reloaded"

        try:
            launch("Missions", "copy", [], OtherView([]), window=window, reload_cb=slow)
            started = time.perf_counter()
            window._reload_rows()
            returned = (time.perf_counter() - started) * 1000.0
            self.assertLess(returned, 200.0)
            self.assertFalse(gate["go"])
            finish_reload(window)
            self.assertTrue(gate["go"])
            print(f"perf reload-return {returned:.1f} ms", flush=True)
        finally:
            window.destroy()

    def test_write_refresh_does_not_load_the_save_on_the_main_thread(self) -> None:
        import threading
        import tkinter as tk

        from nmsmissions.gui import finish_reload, launch
        from nmsmissions.saveio import load_save

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path)
            step = StepView("^OPEN", "Open", None, 9, "not_started", "not started", False, False, False, None, None, None, None)
            chain = ChainView("side", "A side quest", "side", 0, None, None, [], [], [], False, steps=[step])
            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            snap_threads: list[threading.Thread] = []
            load_threads: list[threading.Thread] = []
            real_snap = take_snapshot

            def spy_snap(save):
                snap_threads.append(threading.current_thread())
                return real_snap(save)

            def spy_load(*args, **kwargs):
                load_threads.append(threading.current_thread())
                return load_save(*args, **kwargs)

            def reload():
                from nmsmissions.saveio import load_save as read_save

                read_save(path, {}, "test")
                return [chain], OtherView([]), str(path)

            try:
                launch(
                    "Missions",
                    str(path),
                    [chain],
                    OtherView([]),
                    window=window,
                    session=session,
                    reload_cb=reload,
                )
                tree = window._list_tree
                self.assertTrue(tree.exists("chain:side/^OPEN"))
                tree.selection_set("chain:side/^OPEN")
                window.update()
                session.invalidate_snapshot()
                snap_threads.clear()
                load_threads.clear()
                with patch("nmsmissions.edit.take_snapshot", spy_snap), patch(
                    "nmsmissions.saveio.load_save", spy_load
                ):
                    window._reload_rows()
                    finish_reload(window)
                    window._refresh_actions()
                main = threading.main_thread()
                self.assertTrue(snap_threads, "the worker should store one snapshot")
                self.assertTrue(load_threads, "the list re-read should load the save")
                self.assertTrue(all(thread is not main for thread in snap_threads))
                self.assertTrue(all(thread is not main for thread in load_threads))
                self.assertEqual(len(snap_threads), 1)
                self.assertEqual(len(load_threads), 1)
                self.assertIsNotNone(session.peek_snapshot())
                self.assertEqual(tree.selection(), ("chain:side/^OPEN",))
            finally:
                window.destroy()

    def test_name_parse_keeps_gaps_under_200_ms(self) -> None:
        import io
        import threading

        from nmsmissions.gamenames import _parse_loc_xml

        rows = []
        for index in range(12000):
            rows.append(
                "<Property name='TkLocalisationEntry'>"
                f"<Property name='Id' value='K{index}'/>"
                f"<Property name='English' value='Mission title number {index}'/>"
                "</Property>"
            )
        payload = io.BytesIO(("<Data>" + "".join(rows) + "</Data>").encode("utf-8"))
        gaps: list[float] = []
        stop = {"flag": False}

        def heartbeat() -> None:
            last = time.perf_counter()
            while not stop["flag"]:
                time.sleep(0.02)
                now = time.perf_counter()
                gaps.append((now - last) * 1000.0)
                last = now

        beater = threading.Thread(target=heartbeat, name="nms-gap", daemon=True)
        beater.start()
        try:
            found = _parse_loc_xml(payload)
        finally:
            stop["flag"] = True
            beater.join(timeout=1)
        self.assertEqual(found["K0"], "Mission title number 0")
        self.assertEqual(found["K11999"], "Mission title number 11999")
        widest = max(gaps) if gaps else 0.0
        print(f"perf name-parse gap {widest:.1f} ms", flush=True)
        self.assertLess(widest, 200.0)

    def test_junk_xml_is_not_parsed_for_tables(self) -> None:
        import xml.etree.ElementTree as ET

        from nmsmissions.gamedata import load_tables

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            junk = "<Data>" + "".join(
                f"<Property name='Row{i}' value='{i}'><Property name='Blob' value='{'abcdefghij' * 8}'/></Property>"
                for i in range(40)
            ) + "</Data>"
            for index in range(40):
                (root / f"junk_{index:04d}.xml").write_text(junk, encoding="utf-8")
            (root / "conditions.xml").write_text(
                "<Data><Property name='MissionID' value='^NOPE'/></Data>",
                encoding="utf-8",
            )
            (root / "missions.mxml").write_text(
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^OPEN'/>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='9'/>"
                "</Property></Property></Property></Data>",
                encoding="utf-8",
            )
            (root / "notes.xml").write_text(
                "<Data><Property name='TechId' value='HYPERDRIVE'/></Data>",
                encoding="utf-8",
            )
            parsed: list[str] = []
            real = ET.parse

            def spy(source):
                parsed.append(Path(source).name)
                return real(source)

            cache = root / "tables.json"
            started = time.perf_counter()
            with patch("nmsmissions.gamedata.ET.parse", spy):
                tables = load_tables(root, cache=cache)
            cold_ms = (time.perf_counter() - started) * 1000.0
            print(f"perf filtered-tables {cold_ms:.1f} ms parsed {sorted(parsed)}", flush=True)
            import json

            names = [Path(row["path"]).name for row in json.loads(cache.read_text(encoding="utf-8"))["files"]]
            self.assertIn("junk_0000.xml", names)
            self.assertIn("missions.mxml", names)
            self.assertIn("^OPEN", tables.missions)
            self.assertIn("^HYPERDRIVE", tables.tech_ids)
            self.assertIn("missions.mxml", parsed)
            self.assertIn("notes.xml", parsed)
            self.assertNotIn("conditions.xml", parsed)
            self.assertFalse(any(name.startswith("junk_") for name in parsed))
            self.assertLess(len(parsed), 5)

    def test_parallel_table_parse_keeps_file_order(self) -> None:
        from nmsmissions.gamedata import _parse_chosen

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a_missions.mxml"
            second = root / "b_missions.mxml"
            body = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='{mid}'/>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='{progress}'/>"
                "</Property></Property></Property></Data>"
            )
            first.write_text(body.format(mid="^OPEN", progress="3"), encoding="utf-8")
            second.write_text(body.format(mid="^OPEN", progress="9"), encoding="utf-8")
            tables = _parse_chosen([first, second], None, pool_bytes=0)
            self.assertEqual(tables.missions["^OPEN"].finals, [(39, 9)])

    def test_table_cache_ignores_the_parser_hash(self) -> None:
        import json

        from nmsmissions.gamedata import cache_path_for_tables, load_tables

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            (root / "missions.mxml").write_text(
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^OPEN'/>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='4'/>"
                "</Property></Property></Property></Data>",
                encoding="utf-8",
            )
            cache = cache_path_for_tables(root)
            first = load_tables(root, cache=cache)
            self.assertFalse(first.from_cache)
            document = json.loads(cache.read_text(encoding="utf-8"))
            document["parser"] = "not-this-build"
            cache.write_text(json.dumps(document), encoding="utf-8")
            again = load_tables(root, cache=cache)
            self.assertTrue(again.from_cache)
            self.assertEqual(again.missions["^OPEN"].finals, [(39, 4)])

    def test_cache_and_timing_stay_out_of_the_home_and_app_folder(self) -> None:
        from nmsmissions import cache_root
        from nmsmissions.edit import EditRequest, _tables
        from nmsmissions.gamedata import cache_path_for_tables, load_tables
        from nmsmissions.gamenames import cache_path_for, load_game_names
        from nmsmissions.timing import log_path, record

        home = Path.home() / ".cache" / "nms_mission_editor"
        before = {path.name for path in home.glob("*")} if home.is_dir() else set()
        app_log = Path(__file__).resolve().parents[1] / "timing.log"
        before_size = app_log.stat().st_size if app_log.is_file() else None
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            loc = root / "LANGUAGE"
            loc.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.EXML").write_text(
                "<Data><Property name='TkLocalisationEntry'>"
                "<Property name='Id' value='K0'/><Property name='English' value='Awakenings'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            (root / "METADATA" / "SIMULATION" / "MISSIONS").mkdir(parents=True)
            (root / "METADATA" / "SIMULATION" / "MISSIONS" / "m0.EXML").write_text(
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^M0'/>"
                "<Property name='MissionTitles'><Property name='Format' value='K0'/>"
                "<Property name='Count' value='1'/></Property>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='2'/>"
                "</Property></Property></Property></Data>",
                encoding="utf-8",
            )
            _tables(EditRequest(action="finish", game_files=root))
            load_game_names(root)
            self.assertTrue(str(cache_path_for_tables(root)).startswith(str(cache_root())))
            self.assertTrue(str(cache_path_for(root)).startswith(str(cache_root())))
            self.assertTrue(cache_path_for_tables(root).is_file())
            self.assertTrue(cache_path_for(root).is_file())
            with patch.dict(os.environ, {"NMSMISSIONS_TIMING": "1"}):
                record("tree-reload", 1.5)
                load_game_names(root)
                load_tables(root, cache=cache_path_for_tables(root))
            text = log_path().read_text(encoding="utf-8")
            self.assertIn("timing tree-reload:", text)
            self.assertIn("timing name-load:", text)
            self.assertIn("timing table-load:", text)
            self.assertNotEqual(log_path().resolve(), app_log.resolve())
        after = {path.name for path in home.glob("*")} if home.is_dir() else set()
        self.assertEqual(after, before)
        after_size = app_log.stat().st_size if app_log.is_file() else None
        self.assertEqual(after_size, before_size)

    def test_windows_pool_hides_the_console(self) -> None:
        import subprocess

        from nmsmissions.gamedata import _hidden_children

        seen: dict[str, int] = {}

        def spy(*_args, **kwargs):
            seen["flags"] = int(kwargs.get("creationflags") or 0)
            raise RuntimeError("stop")

        with patch("nmsmissions.gamedata.os.name", "nt"):
            with patch("subprocess.Popen", spy):
                with self.assertRaises(RuntimeError):
                    with _hidden_children():
                        subprocess.Popen(["python", "-c", "pass"])
        self.assertTrue(seen["flags"] & 0x08000000)

    def test_warm_caches_do_not_rebuild_the_tree(self) -> None:
        import tkinter as tk

        from nmsmissions.edit import _source_stamp
        from nmsmissions.gamedata import GameTables
        from nmsmissions.gamenames import GameNameReport

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            root = folder / "game"
            root.mkdir()
            session = EditorSession(path, game_files=root)
            session._names_report = GameNameReport(names={"^OPEN": "Real name"}, from_cache=True)
            session._tables_cache = GameTables()
            session._tables_stamp = _source_stamp(root, None)
            self.assertTrue(session.tables_cached())
            step = StepView("^OPEN", "Real name", None, 9, "not_started", "not started", False, False, False, None, None, None, None)
            chain = ChainView("side", "A side quest", "side", 0, None, None, [], [], [], False, steps=[step])
            calls = {"n": 0}

            def reload_cb():
                calls["n"] += 1
                return [chain], OtherView([]), str(path)

            def asset_rows():
                raise AssertionError("a warm open must not rebuild rows")

            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            try:
                launch(
                    "Missions",
                    str(path),
                    [chain],
                    OtherView([]),
                    window=window,
                    session=session,
                    reload_cb=reload_cb,
                    asset_rows=asset_rows,
                )
                self.assertEqual(calls["n"], 0)
                tree = next(widget for widget in _walk(window) if widget.winfo_class() == "Treeview")
                self.assertEqual(str(tree.item("chain:side/^OPEN")["values"][0]), "Real name")
                self.assertNotEqual(window.geometry().split("+")[0], "1280x760")
            finally:
                window.destroy()

    def test_name_arrival_patches_cells_without_reopening_the_save(self) -> None:
        import threading
        import tkinter as tk

        from nmsmissions.edit import _source_stamp
        from nmsmissions.gamedata import GameTables
        from nmsmissions.gamenames import GameNameReport

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            root = folder / "game"
            root.mkdir()
            session = EditorSession(path, game_files=root)
            session._tables_cache = GameTables()
            session._tables_stamp = _source_stamp(root, None)
            report = GameNameReport(names={"^OPEN": "Real name"})
            step = StepView("^OPEN", "Placeholder", None, 9, "not_started", "not started", False, False, False, None, None, None, None)
            named = StepView("^OPEN", "Real name", None, 9, "done", "done", False, False, False, None, None, None, None)
            chain = ChainView("side", "A side quest", "side", 0, None, None, [], [], [], False, steps=[step])
            renamed = ChainView("side", "A side quest", "side", 0, None, None, [], [], [], False, steps=[named])
            calls = {"n": 0}

            def reload_cb():
                calls["n"] += 1
                return [chain], OtherView([]), str(path)

            def asset_rows():
                return [renamed], OtherView([]), str(path) + "\nNames from game files: 1 title"

            def start_name_load() -> None:
                threading.Timer(0.05, lambda: setattr(session, "_names_report", report)).start()

            session.start_name_load = start_name_load
            window = tk.Tk()
            window.withdraw()

            def pump() -> None:
                window.after(800, window.quit)
                tk.Tk.mainloop(window)

            window.mainloop = pump
            try:
                launch(
                    "Missions",
                    str(path),
                    [chain],
                    OtherView([]),
                    window=window,
                    session=session,
                    reload_cb=reload_cb,
                    asset_rows=asset_rows,
                )
                tree = next(widget for widget in _walk(window) if widget.winfo_class() == "Treeview")
                values = tree.item("chain:side/^OPEN")["values"]
                self.assertEqual(str(values[0]), "Real name")
                self.assertEqual(str(values[1]), "done")
                self.assertEqual(calls["n"], 0)
            finally:
                window.destroy()

    def test_cell_chunks_do_not_delete_the_tree(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.gui import schedule_cell_patch

        window = tk.Tk()
        window.withdraw()
        try:
            tree = ttk.Treeview(window, columns=("name", "status", "progress"))
            rows = []
            for index in range(80):
                rows.append(
                    {
                        "iid": f"chain:side/^M{index:04d}",
                        "parent": "",
                        "text": f"^M{index:04d}",
                        "name": f"Mission {index}",
                        "status": "not started",
                        "progress": "-",
                        "tag": "not_started",
                        "wiki": "",
                    }
                )
            paint_rows(tree, rows)
            before = tree.get_children("")
            deleted = {"n": 0}
            real_delete = tree.delete

            def spy(*args, **kwargs):
                deleted["n"] += 1
                return real_delete(*args, **kwargs)

            tree.delete = spy
            fresh = [dict(row) for row in rows]
            fresh[5]["name"] = "Renamed"
            fresh[5]["status"] = "done"
            holder = {"rows": rows, "gen": 0}
            urls: dict[str, str] = {}
            by_iid = {row["iid"]: row for row in rows}
            done = {"ok": False}
            schedule_cell_patch(
                window,
                tree,
                holder,
                urls,
                by_iid,
                fresh,
                on_done=lambda: done.__setitem__("ok", True),
            )
            for _ignored in range(40):
                window.update()
                if done["ok"]:
                    break
                time.sleep(0.01)
            self.assertTrue(done["ok"])
            self.assertEqual(deleted["n"], 0)
            self.assertEqual(tree.get_children(""), before)
            self.assertEqual(str(tree.item(fresh[5]["iid"])["values"][0]), "Renamed")
            self.assertEqual(str(tree.item(fresh[5]["iid"])["values"][1]), "done")
            self.assertEqual(holder["rows"][5]["name"], "Renamed")
        finally:
            window.destroy()

    def test_banner_wraps_with_the_window(self) -> None:
        import tkinter as tk

        window = tk.Tk()
        window.withdraw()
        window.mainloop = lambda: None
        try:
            launch("Missions", "A copy line\nLoading mission tables… 25%", [], OtherView([]), window=window)
            banner = next(
                widget
                for widget in _walk(window)
                if widget.winfo_class() == "Label" and "copy line" in str(widget.cget("text"))
            )
            window.geometry("900x600")
            window.update_idletasks()
            window.update()
            self.assertGreaterEqual(int(banner.cget("wraplength")), 800)
            self.assertNotEqual(window.geometry().split("+")[0], "1280x760")
        finally:
            window.destroy()

    def test_small_extract_does_not_start_a_process_pool(self) -> None:
        from nmsmissions.gamedata import _parse_chosen

        started = {"n": 0}

        class Boom:
            def __init__(self, *_args, **_kwargs):
                started["n"] += 1
                raise AssertionError("pool")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a_missions.mxml"
            second = root / "b_missions.mxml"
            body = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='{mid}'/>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='{progress}'/>"
                "</Property></Property></Property></Data>"
            )
            first.write_text(body.format(mid="^OPEN", progress="3"), encoding="utf-8")
            second.write_text(body.format(mid="^OPEN", progress="9"), encoding="utf-8")
            with patch("concurrent.futures.ProcessPoolExecutor", Boom):
                tables = _parse_chosen([first, second], None)
            self.assertEqual(started["n"], 0)
            self.assertEqual(tables.missions["^OPEN"].finals, [(39, 9)])

    def test_pool_submits_the_largest_file_first_and_keeps_path_order(self) -> None:
        import concurrent.futures

        from nmsmissions.gamedata import _parse_chosen

        submitted: list[str] = []

        class FakePool:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def submit(self, fn, arg):
                submitted.append(Path(arg).name)
                future = concurrent.futures.Future()
                future.set_result(fn(arg))
                return future

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = root / "a_missions.mxml"
            big = root / "b_missions.mxml"
            later = root / "c_missions.mxml"
            body = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='{mid}'/>"
                "<Property name='FinalStageVersions'><Property>"
                "<Property name='Version' value='39'/><Property name='Progress' value='{progress}'/>"
                "</Property></Property></Property></Data>"
            )
            small.write_text(body.format(mid="^OPEN", progress="3"), encoding="utf-8")
            later.write_text(body.format(mid="^OPEN", progress="9"), encoding="utf-8")
            big.write_text(body.format(mid="^OTHER", progress="1") + (" " * 8000), encoding="utf-8")
            with patch("concurrent.futures.ProcessPoolExecutor", FakePool):
                tables = _parse_chosen([small, big, later], None, pool_bytes=0)
            self.assertEqual(submitted[0], "b_missions.mxml")
            self.assertEqual(tables.missions["^OPEN"].finals, [(39, 9)])
            self.assertIn("^OTHER", tables.missions)

    def test_window_opens_tall_enough_and_can_be_maximized(self) -> None:
        import tkinter as tk

        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / "window.json"
            with patch.dict(os.environ, {"NMSMISSIONS_WINDOW_FILE": str(settings)}):
                window = tk.Tk()
                window.mainloop = lambda: None
                try:
                    launch("Missions", "A copy line", [], OtherView([]), window=window)
                    window.deiconify()
                    window.update_idletasks()
                    window.update()
                    tree = next(widget for widget in _walk(window) if widget.winfo_class() == "Treeview")
                    self.assertGreaterEqual(int(tree.cget("height")), 25)
                    wide, tall = window.resizable()
                    self.assertTrue(wide)
                    self.assertTrue(tall)
                    height = int(window.geometry().split("+")[0].split("x")[1])
                    screen_h = int(window.winfo_screenheight())
                    target = min(max(int(screen_h * 0.85), 25 * 24 + 220), max(screen_h - 40, 420))
                    self.assertGreaterEqual(height, target)
                finally:
                    window.destroy()

    def test_window_remembers_size_and_position(self) -> None:
        import json
        import tkinter as tk

        from nmsmissions.gui import apply_window_size, save_window_settings

        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / "window.json"
            with patch.dict(os.environ, {"NMSMISSIONS_WINDOW_FILE": str(settings)}):
                window = tk.Tk()
                try:
                    window.geometry("1100x800+30+40")
                    window.update_idletasks()
                    window.update()
                    if int(window.winfo_width()) < 200:
                        window.deiconify()
                        window.geometry("1100x800+30+40")
                        window.update()
                    self.assertGreaterEqual(int(window.winfo_width()), 200)
                    save_window_settings(window)
                finally:
                    window.destroy()
                saved = json.loads(settings.read_text(encoding="utf-8"))
                self.assertGreaterEqual(int(saved["width"]), 200)
                self.assertGreaterEqual(int(saved["height"]), 200)
                again = tk.Tk()
                try:
                    apply_window_size(again)
                    again.deiconify()
                    again.update_idletasks()
                    again.update()
                    screen_w = int(again.winfo_screenwidth())
                    screen_h = int(again.winfo_screenheight())
                    expect_w = max(200, min(int(saved["width"]), screen_w))
                    expect_h = max(200, min(int(saved["height"]), screen_h))
                    expect_x = max(0, min(int(saved["x"]), max(screen_w - 80, 0)))
                    expect_y = max(0, min(int(saved["y"]), max(screen_h - 80, 0)))
                    self.assertEqual(again.geometry(), f"{expect_w}x{expect_h}+{expect_x}+{expect_y}")
                finally:
                    again.destroy()

    def test_launch_checks_the_game_off_the_tk_thread(self) -> None:
        import threading
        import tkinter as tk

        from nmsmissions.edit import EditorSession

        calls: list[str] = []

        def spy() -> bool:
            calls.append(threading.current_thread().name)
            return False

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path)
            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            try:
                with patch("nmsmissions.edit.nms_is_running", spy):
                    launch(
                        "Missions",
                        str(path),
                        [],
                        OtherView([]),
                        window=window,
                        session=session,
                        reload_cb=lambda: ([], OtherView([]), str(path)),
                    )
                    deadline = time.perf_counter() + 2.0
                    while time.perf_counter() < deadline and not calls:
                        time.sleep(0.05)
                self.assertTrue(calls)
                self.assertNotIn("MainThread", calls)
            finally:
                window.destroy()

    def test_status_line_keeps_the_first_running_answer_across_a_rebuild(self) -> None:
        import threading
        import tkinter as tk

        from nmsmissions.edit import EditorSession

        gate = threading.Event()
        from nmsmissions import edit as edit_mod

        real_note = edit_mod.note_nms_running

        def note(value: bool) -> None:
            if gate.is_set():
                real_note(value)

        def spy() -> bool:
            gate.wait(2)
            return False

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path)
            window = tk.Tk()
            window.withdraw()

            def pump(seconds: float) -> None:
                deadline = time.perf_counter() + seconds
                while time.perf_counter() < deadline:
                    window.update()
                    time.sleep(0.02)

            window.mainloop = lambda: pump(0.3)
            try:
                with patch("nmsmissions.edit.nms_is_running", spy), patch(
                    "nmsmissions.edit.note_nms_running", note
                ):
                    with edit_mod._nms_lock:
                        edit_mod._nms_seen["value"] = None
                        edit_mod._nms_seen["at"] = 0.0
                    launch(
                        "Missions",
                        str(path),
                        [],
                        OtherView([]),
                        window=window,
                        session=session,
                        reload_cb=lambda: ([], OtherView([]), str(path)),
                    )
                    status = next(
                        widget
                        for widget in _walk(window)
                        if widget.winfo_class() == "Label" and "Checking whether" in str(widget.cget("text"))
                    )
                    window._reload_rows()
                    from nmsmissions.gui import finish_reload

                    finish_reload(window)
                    self.assertIn("Checking whether", str(status.cget("text")))
                    gate.set()
                    pump(1.5)
                    text = str(status.cget("text"))
                    self.assertIn("No Man's Sky is not running.", text)
                    self.assertNotIn("Checking whether", text)
                    window._reload_rows()
                    finish_reload(window)
                    again = str(status.cget("text"))
                    self.assertIn("No Man's Sky is not running.", again)
                    self.assertNotIn("Checking whether", again)
            finally:
                gate.set()
                window.destroy()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)

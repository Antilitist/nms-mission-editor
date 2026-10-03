"""Station standing against a packed UniverseAddress and a ^SYSTEM_STATS row."""

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


import json
import os
import threading
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from nmsmissions.chunks import unpack_save
from nmsmissions.edit import EditRequest, EditorSession, build_plan, run, take_snapshot
from nmsmissions.station import MISSING_SYSTEM, _pack_voxels, panel_text, read_station, universe_address_int
from tests.harness import TkCleanup
from tests.test_edit import _text, _write_pair

# Made-up voxels. The Address int is whatever the packer returns.
_VOXEL_X = 0x111
_VOXEL_Y = 0x22
_VOXEL_Z = 0x333
_SYSTEM = 0x044
_OTHER_SYSTEM = 0x055


def _voxels(planet: int, system: int = _SYSTEM) -> dict:
    return {
        "VoxelX": _VOXEL_X,
        "VoxelY": _VOXEL_Y,
        "VoxelZ": _VOXEL_Z,
        "SolarSystemIndex": system,
        "PlanetIndex": planet,
    }


def _universe(planet: int = 1, reality: int = 0, system: int = _SYSTEM) -> dict:
    """GalacticAddress is the voxels. RealityIndex sits beside it, not inside it."""
    return {"RealityIndex": reality, "GalacticAddress": _voxels(planet, system)}


def _packed(planet: int = 0, reality: int = 0, system: int = _SYSTEM) -> int:
    return int(_pack_voxels(_voxels(planet, system), reality), 16)


SYSTEM_ADDRESS = _packed()
OTHER_ADDRESS = _packed(system=_OTHER_SYSTEM)


def _stat(stat_id: str, value: dict) -> dict:
    return {"Id": stat_id, "Value": value}


def _real_stats() -> list[dict]:
    """The save's stat-group layout: Id plus Value.IntValue, and one empty Value."""
    return [
        _stat("^EXP_STANDING", {}),
        _stat("^TRA_STANDING", {"IntValue": 4, "FloatValue": 9.0}),
        _stat("^WAR_STANDING", {"IntValue": 8}),
        _stat("^WGUILD_STAND", {"IntValue": 3}),
        _stat("^EGUILD_STAND", {"IntValue": 2}),
        _stat("^TGUILD_STAND", {"IntValue": 1}),
        _stat("^SP_POI_MISSIONS", {"IntValue": 2}),
    ]


def _player(**extra) -> dict:
    body = {
        "floatCheck": "FLOAT",
        "note": "NOTE",
        "XTp": "Main",
        "b@r": 10,
        "vLc": {"6f=": {"yq:": 39, "dwb": [{"p0c": "^TEMPLATE", "tW6": -1}], "wGS": -1}},
    }
    player = body["vLc"]["6f="]
    player.update(extra)
    return body


def _plain_body(planet: int = 1, reality: int = 0) -> dict:
    return _player(
        Units=-1,
        UniverseAddress=_universe(planet, reality),
        Stats=[
            {
                "GroupId": "^SYSTEM_STATS",
                "Address": 0,
                "Stats": [_stat("^EXP_STANDING", {"IntValue": 1})],
            },
            {
                "GroupId": "^SYSTEM_STATS",
                "Address": OTHER_ADDRESS,
                "Stats": [_stat("^EXP_STANDING", {"IntValue": 1})],
            },
            {
                "GroupId": "^SYSTEM_STATS",
                "Address": SYSTEM_ADDRESS,
                "Stats": _real_stats(),
            },
        ],
    )


def _request(path: Path, folder: Path, *, apply: bool = False) -> EditRequest:
    return EditRequest(
        action="station",
        save=path,
        apply=apply,
        backup_dir=folder / "backups",
        quiet=True,
        station_race="^EXP_STANDING",
        station_guild="^WGUILD_STAND",
    )


class StationEditTests(unittest.TestCase):
    def test_packed_address_finds_the_system_and_raises_only_three_stats(self) -> None:
        player = _plain_body()["vLc"]["6f="]
        self.assertEqual(universe_address_int(player), _packed(planet=1))
        self.assertNotEqual(universe_address_int(player), SYSTEM_ADDRESS)
        self.assertEqual(universe_address_int({"UniverseAddress": _universe(0, 0)}), SYSTEM_ADDRESS)
        view = read_station(player)
        self.assertTrue(view.found)
        shown = {row.stat_id: row.current for row in view.rows}
        self.assertEqual(shown["^EXP_STANDING"], 0)
        self.assertEqual(shown["^SP_POI_MISSIONS"], 2)
        self.assertIn("Korvax standing in this system: 0 / 30", panel_text(player))

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_plain_body()))
            before = path.read_bytes()
            snap = take_snapshot(path)
            plan = build_plan(_request(path, folder), snap, None)
            labels = [change.label for change in plan.changes]
            self.assertEqual(
                labels,
                [
                    "Raise Korvax standing in this system from 0 to 30.",
                    "Raise Mercenaries Guild standing in this system from 3 to 15.",
                    "Raise Salvage contracts in this system from 2 to 5.",
                ],
            )
            self.assertIn("Korvax standing in this system", plan.summary)
            self.assertIn("Mercenaries Guild standing in this system", plan.summary)
            self.assertIn("Salvage contracts in this system", plan.summary)
            self.assertNotIn("Gek standing", plan.summary)
            self.assertNotIn("Explorers Guild", plan.summary)
            self.assertNotIn("Merchants Guild", plan.summary)
            self.assertTrue(any("about 4.29 billion" in line for line in plan.warnings))
            code = run(_request(path, folder, apply=True))
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            state = parsed["vLc"]["6f="]
            groups = state["Stats"]
            self.assertEqual(groups[0]["Stats"][0]["Value"]["IntValue"], 1)
            self.assertEqual(groups[1]["Stats"][0]["Value"]["IntValue"], 1)
            current = {row["Id"]: row["Value"] for row in groups[2]["Stats"]}
            self.assertEqual(current["^EXP_STANDING"], {"IntValue": 30})
            self.assertNotIn("FloatValue", current["^EXP_STANDING"])
            self.assertEqual(current["^TRA_STANDING"], {"IntValue": 4, "FloatValue": 9.0})
            self.assertEqual(current["^WAR_STANDING"], {"IntValue": 8})
            self.assertEqual(current["^WGUILD_STAND"], {"IntValue": 15})
            self.assertEqual(current["^EGUILD_STAND"], {"IntValue": 2})
            self.assertEqual(current["^TGUILD_STAND"], {"IntValue": 1})
            self.assertEqual(current["^SP_POI_MISSIONS"], {"IntValue": 5})
            self.assertEqual(state["wGS"], -1)
            self.assertEqual(parsed["note"], "NOTE")
            zips = list((folder / "backups").glob("*.zip"))
            self.assertEqual(len(zips), 1)
            with zipfile.ZipFile(zips[0]) as handle:
                self.assertEqual(handle.read(path.name), before)

    def test_obfuscated_save_matches_the_same_address(self) -> None:
        body = _player(
            **{
                "yhJ": {
                    "Iis": 0,
                    "oZw": {
                        "dZj": _VOXEL_X,
                        "IyE": _VOXEL_Y,
                        "uXE": _VOXEL_Z,
                        "vby": _SYSTEM,
                        "jsv": 0,
                    },
                },
                "gUR": [
                    {
                        ":rc": "^SYSTEM_STATS",
                        "2Ak": SYSTEM_ADDRESS,
                        "gUR": [
                            {"b2n": "^WAR_STANDING", ">MX": {"eoL": 9.0, ">vs": 4}},
                            {"b2n": "^EXP_STANDING", ">MX": {}},
                            {"b2n": "^WGUILD_STAND", ">MX": {">vs": 3}},
                            {"b2n": "^EGUILD_STAND", ">MX": {">vs": 2}},
                            {"b2n": "^SP_POI_MISSIONS", ">MX": {">vs": 2}},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(universe_address_int(body["vLc"]["6f="]), SYSTEM_ADDRESS)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(body))
            code = run(_request(path, folder, apply=True))
            self.assertEqual(code, 0)
            rows = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]["gUR"][0]["gUR"]
            by_id = {row["b2n"]: row[">MX"] for row in rows}
            self.assertEqual(by_id["^EXP_STANDING"], {">vs": 30})
            self.assertNotIn("eoL", by_id["^EXP_STANDING"])
            self.assertEqual(by_id["^WAR_STANDING"], {"eoL": 9.0, ">vs": 4})
            self.assertEqual(by_id["^WGUILD_STAND"], {">vs": 15})
            self.assertEqual(by_id["^EGUILD_STAND"], {">vs": 2})
            self.assertEqual(by_id["^SP_POI_MISSIONS"], {">vs": 5})

    def test_a_different_galaxy_is_not_this_system(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_plain_body(reality=1)))
            before = path.read_bytes()
            snap = take_snapshot(path)
            plan = build_plan(_request(path, folder), snap, None)
            self.assertEqual(plan.summary, MISSING_SYSTEM)
            self.assertEqual(plan.changes, [])
            code = run(_request(path, folder, apply=True))
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), before)

    def test_a_running_game_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_plain_body()))
            blob = path.read_bytes()
            os.environ["NMSMISSIONS_NMS_RUNNING"] = "1"
            try:
                code = run(_request(path, folder, apply=True))
            finally:
                os.environ.pop("NMSMISSIONS_NMS_RUNNING", None)
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), blob)

    def test_a_long_stats_list_still_matches_the_packed_address(self) -> None:
        groups = [
            {
                "GroupId": "^SYSTEM_STATS",
                "Address": OTHER_ADDRESS + index,
                "Stats": [_stat("^EXP_STANDING", {"IntValue": 1})],
            }
            for index in range(400)
        ]
        groups.append(
            {
                "GroupId": "^SYSTEM_STATS",
                "Address": SYSTEM_ADDRESS,
                "Stats": [_stat("^SP_POI_MISSIONS", {"IntValue": 2})],
            }
        )
        player = {"UniverseAddress": _universe(planet=0), "wGS": 50, "Stats": groups, "dwb": []}
        view = read_station(player)
        self.assertTrue(view.found)
        self.assertEqual(universe_address_int(player), SYSTEM_ADDRESS)
        self.assertEqual([(row.stat_id, row.current, row.target) for row in view.rows], [("^SP_POI_MISSIONS", 2, 5)])

    def test_meet_requirements_does_not_wait_for_mission_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_plain_body()))
            session = EditorSession(path, game_files=folder, backup_dir=folder / "backups")
            started = threading.Event()
            release = threading.Event()

            def hang() -> None:
                started.set()
                release.wait(2)

            session._loader = threading.Thread(target=hang, name="nms-mission-tables")
            session._loader.start()
            self.assertTrue(started.wait(1))
            try:
                began = time.perf_counter()
                plan = session.plan_for(
                    "station",
                    None,
                    None,
                    False,
                    station_race="^EXP_STANDING",
                    station_guild="^WGUILD_STAND",
                )
                elapsed = time.perf_counter() - began
            finally:
                release.set()
                session._loader.join(timeout=2)
            self.assertLess(elapsed, 0.3)
            self.assertEqual(len(plan.changes), 3)


class StationWindowTests(TkCleanup, unittest.TestCase):
    def test_station_tab_builds_with_the_other_button_rows(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        from unittest.mock import patch

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.gui import (
            FINISH_LABEL,
            FINISH_TO_LABEL,
            RESET_FROM_LABEL,
            RESET_LABEL,
            UNLOCK_LABEL,
            launch,
        )
        from nmsmissions.station import MEET_LABEL
        from tests.test_slots import _one, _walk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_plain_body()))
            session = EditorSession(path, backup_dir=folder / "backups")
            chain = ChainView("artemis", "Artemis Path", "story", 0, None, None, [], [], [], False)
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
                    reload_cb=lambda: ([chain], OtherView([]), str(path)),
                )
                widgets = list(_walk(window))
                buttons = [str(widget.cget("text")) for widget in widgets if isinstance(widget, ttk.Button)]
                for label in (
                    "Expand all",
                    "Collapse all",
                    "Open wiki page",
                    FINISH_LABEL,
                    UNLOCK_LABEL,
                    FINISH_TO_LABEL,
                    RESET_LABEL,
                    RESET_FROM_LABEL,
                    "Read my game files",
                    "Clear cache",
                    "About / Tip me",
                    MEET_LABEL,
                ):
                    self.assertIn(label, buttons)
                book = next(widget for widget in widgets if isinstance(widget, ttk.Notebook))
                names = [book.tab(tab, "text") for tab in book.tabs()]
                self.assertEqual(names, ["Missions", "Station and standing"])
                shown = "\n".join(str(widget.cget("text")) for widget in widgets if isinstance(widget, (tk.Label, ttk.Label)))
                self.assertIn("Local race standing: 30", shown)
                self.assertIn("Korvax standing in this system: 0 / 30", shown)
                self.assertIn("about 4.29 billion", shown)
                self.assertIn("not a debt", shown)
                self.assertIn("Only this one is raised to 30", shown)
                self.assertIn("Only this one is raised to 15", shown)
                with patch("nmsmissions.edit.take_snapshot", side_effect=AssertionError("parsed")):
                    window._refresh_station()
                _one(widgets, ttk.Radiobutton, "Raise Korvax standing in this system to 30").invoke()
                _one(widgets, ttk.Radiobutton, "Raise Mercenaries Guild standing in this system to 15").invoke()
                widgets = list(_walk(window))
                shown = "\n".join(str(widget.cget("text")) for widget in widgets if isinstance(widget, (tk.Label, ttk.Label)))
                self.assertIn("Raises Korvax standing in this system to 30", shown)
                self.assertIn("Mercenaries Guild standing in this system to 15", shown)
                self.assertIn("never lowers", shown)
                meet = _one(widgets, ttk.Button, MEET_LABEL)
                meet.invoke()
                preview = next(widget for widget in window.winfo_children() if isinstance(widget, tk.Toplevel))
                preview_widgets = list(_walk(preview))
                preview_buttons = [
                    str(widget.cget("text")) for widget in preview_widgets if isinstance(widget, ttk.Button)
                ]
                self.assertIn("Write to copy", preview_buttons)
                text = next(widget for widget in preview_widgets if isinstance(widget, tk.Text))
                preview_body = text.get("1.0", "end")
                self.assertIn("Raise Korvax standing in this system from 0 to 30.", preview_body)
                self.assertIn("Raise Mercenaries Guild standing in this system from 3 to 15.", preview_body)
                self.assertIn("Raise Salvage contracts in this system from 2 to 5.", preview_body)
                self.assertNotIn("Raise Gek standing", preview_body)
                self.assertNotIn("Raise Explorers Guild", preview_body)
                self.assertNotIn("Raise Merchants Guild", preview_body)
                self.assertNotIn("Raise Vy'keen standing", preview_body)
            finally:
                window.destroy()

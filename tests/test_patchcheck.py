"""Pause writes when a save is not the known 7.06 format."""

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


import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nmsmissions.edit import EditError, EditRequest, build_plan, collect_checks, commit, take_snapshot
from nmsmissions.patchcheck import KNOWN_SAVE_VERSIONS, PAUSE_MESSAGE, assess
from tests.harness import TkCleanup
from tests.test_edit import _text, _write_pair


def _player() -> dict:
    return {
        "yq:": 40,
        "Kg6": False,
        "cPt": False,
        "wGS": 0,
        "7QL": 0,
        "kN;": 0,
        ";R7": "^",
        "Mg<": "^",
        "4kj": [],
        "eZ<": [],
        "24<": [],
        ";l5": {":No": [], "hl?": []},
        "aBE": 0,
        "@Cs": [],
        "8ZP": {},
        "PMT": {":No": [], "hl?": []},
        "gUR": [
            {
                ":rc": "^SYSTEM_STATS",
                "2Ak": 1,
                "gUR": [{"b2n": "^EXP_STANDING", ">MX": {">vs": 0}}],
            }
        ],
        "yhJ": {
            "Iis": 0,
            "oZw": {"dZj": 0x111, "IyE": 0x22, "uXE": 0x333, "vby": 0x044, "jsv": 1},
        },
        "wem": {":No": [], "hl?": []},
        "9i?": {},
        "5hy": 0,
        "5?q": False,
        ";9U": False,
        "dwb": [
            {"p0c": "^OPEN", "tW6": 1, "8?J": 0, "Kex": 0, "@EL": 0, "eZ7": []},
            {"p0c": "^TEMPLATE", "tW6": -1, "8?J": 0, "Kex": 0, "@EL": "kept", "eZ7": []},
        ],
    }


def _game_body(**overrides) -> dict:
    body = {
        "F2P": 4739,
        "8>q": "Synthetic",
        "XTp": "Main",
        "<h0": {},
        "vLc": {"6f=": _player()},
        "2YS": {},
        "fDu": {},
    }
    body.update(overrides)
    return body


def _buttons(window) -> list:
    from tkinter import ttk

    found = []

    def walk(widget) -> None:
        if isinstance(widget, ttk.Button):
            found.append(widget)
        for child in widget.winfo_children():
            walk(child)

    walk(window)
    return found


def _named(window, label: str):
    matches = [button for button in _buttons(window) if str(button.cget("text")) == label]
    if len(matches) != 1:
        raise AssertionError(f"{label!r} matched {len(matches)} buttons")
    return matches[0]


class PatchCheckTests(unittest.TestCase):
    def test_known_version_passes(self) -> None:
        verdict = assess(_game_body())
        self.assertTrue(verdict.ok, verdict.details)
        self.assertIn(4739, KNOWN_SAVE_VERSIONS)

    def test_fixture_shape_still_passes(self) -> None:
        from tests.test_edit import _save_body

        verdict = assess(_save_body())
        self.assertTrue(verdict.ok, verdict.details)

    def test_changed_version_is_blocked(self) -> None:
        verdict = assess(_game_body(F2P=4740))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.message, PAUSE_MESSAGE)
        self.assertTrue(any("4740" in line for line in verdict.details))

    def test_missing_top_level_key_is_blocked(self) -> None:
        body = _game_body()
        del body["fDu"]
        verdict = assess(body)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("DiscoveryManagerData" in line for line in verdict.details))

    def test_unknown_top_level_key_is_blocked(self) -> None:
        verdict = assess(_game_body(NEWKEY=1))
        self.assertFalse(verdict.ok)
        self.assertTrue(any("NEWKEY" in line for line in verdict.details))

    def test_missing_editor_field_is_blocked(self) -> None:
        body = _game_body()
        del body["vLc"]["6f="]["dwb"]
        verdict = assess(body)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("MissionProgress" in line for line in verdict.details))

    def test_blocked_write_does_not_touch_the_file(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_game_body(F2P=4740)))
            before = path.read_bytes()
            snap = take_snapshot(path)
            request = EditRequest(
                action="purple",
                save=path,
                flag_only=True,
                apply=True,
                backup_dir=folder / "backups",
                running=False,
            )
            plan = build_plan(request, snap, None)
            plan.checks = collect_checks(request, snap, None, running=False)
            self.assertTrue(plan.failing())
            self.assertIn(PAUSE_MESSAGE, [check.message for check in plan.checks])
            self.assertTrue(plan.changes)
            plan.checks = []
            with self.assertRaises(EditError) as caught:
                commit(request, snap, plan, None, running=False)
            self.assertEqual(str(caught.exception), PAUSE_MESSAGE)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list((folder / "backups").glob("*.zip")), [])

    def test_known_write_still_makes_a_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_game_body()))
            before = path.read_bytes()
            snap = take_snapshot(path)
            request = EditRequest(
                action="purple",
                save=path,
                flag_only=True,
                apply=True,
                backup_dir=folder / "backups",
            )
            plan = build_plan(request, snap, None)
            plan.checks = collect_checks(request, snap, None, running=False)
            self.assertFalse(plan.failing())
            code, backup = commit(request, snap, plan, None, running=False)
            self.assertEqual(code, 0)
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.is_file())
            self.assertNotEqual(path.read_bytes(), before)

    def test_override_writes_and_still_makes_a_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", _text(_game_body(F2P=4740)))
            before = path.read_bytes()
            snap = take_snapshot(path)
            request = EditRequest(
                action="purple",
                save=path,
                flag_only=True,
                apply=True,
                backup_dir=folder / "backups",
                edit_anyway=True,
            )
            plan = build_plan(request, snap, None)
            plan.checks = collect_checks(request, snap, None, running=False)
            self.assertFalse(plan.failing())
            self.assertTrue(any(check.level == "warn" and "backup" in check.message for check in plan.checks))
            code, backup = commit(request, snap, plan, None, running=False)
            self.assertEqual(code, 0)
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.is_file())
            self.assertNotEqual(path.read_bytes(), before)


class PatchWindowTests(TkCleanup, unittest.TestCase):
    def _launch(self, body: dict):
        import tkinter as tk

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import launch

        folder = Path(self._tmp.name)
        path = _write_pair(folder, "save.hg", _text(body))
        session = EditorSession(path, backup_dir=folder / "backups")
        chain = ChainView("artemis", "Artemis Path", "story", 0, None, None, [], [], [], False)
        window = tk.Tk()
        window.withdraw()
        window.mainloop = lambda: None
        launch(
            "Missions",
            str(path),
            [chain],
            OtherView([]),
            window=window,
            session=session,
            reload_cb=lambda: ([chain], OtherView([]), str(path)),
        )
        return window

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def test_newer_save_shows_the_warning_and_still_lists_missions(self) -> None:
        window = self._launch(_game_body(F2P=4740))
        try:
            self.assertIn(PAUSE_MESSAGE, str(window._patch_warning.cget("text")))
            self.assertIn("look through this save", str(window._patch_warning.cget("text")))
            self.assertEqual(window._patch_frame.winfo_manager(), "pack")
            self.assertIn("disabled", _named(window, "Meet requirements").state())
            self.assertIn("disabled", _named(window, "Put edited save into my game").state())
            self.assertNotIn("disabled", _named(window, "I made a backup, edit anyway").state())
            self.assertTrue(window._list_tree.get_children(""))
            self.assertEqual(window._list_tree.item(window._list_tree.get_children("")[0], "text"), "Artemis Path")
            _named(window, "I made a backup, edit anyway").invoke()
            self.assertNotIn("disabled", _named(window, "Meet requirements").state())
            self.assertIn("backup is still made", str(window._patch_warning.cget("text")))
            self.assertIn("disabled", _named(window, "I made a backup, edit anyway").state())
        finally:
            window.destroy()

    def test_known_save_has_no_warning(self) -> None:
        window = self._launch(_game_body())
        try:
            self.assertEqual(str(window._patch_warning.cget("text")), "")
            self.assertEqual(window._patch_frame.winfo_manager(), "")
            self.assertNotIn("disabled", _named(window, "Meet requirements").state())
        finally:
            window.destroy()

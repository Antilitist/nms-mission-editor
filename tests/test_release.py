"""Release zip, first-run setup, and the startup warning. Synthetic files only."""

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


import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from nmsmissions import AI_CREDIT, CASH_APP, CREDIT, X_MONEY_HANDLE, __version__, about_body, tip_text
from nmsmissions.locate import TESTED_BUILD, discover, startup_message, version_line

try:
    from nmsmissions.release import (
        ZIP_DATE,
        banned_hashes,
        build_release_zip,
        export_public_tree,
        personal_list_note,
        scan_zip,
    )
except ImportError:
    build_release_zip = None
    export_public_tree = None
    scan_zip = None
    ZIP_DATE = None
    banned_hashes = None
    personal_list_note = None

from tests.harness import TkCleanup


class ReleaseTests(TkCleanup, unittest.TestCase):
    def _need_release(self) -> None:
        if build_release_zip is None:
            self.skipTest("release.py is not in this folder")

    def test_version_file_matches_the_package(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), __version__)
        self.assertEqual(__version__, "1.1.2")
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "1.1.2"', text)
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertNotIn("AI helpers", license_text)
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn(AI_CREDIT, readme)
        bat = (root / "Start.bat").read_text(encoding="utf-8")
        self.assertIn("REM NMS Mission Editor 1.1.2", bat)
        self.assertNotIn("1.0.6", bat)

    def test_zip_allowlist_has_no_personal_data(self) -> None:
        self._need_release()
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = build_release_zip(folder / "editor.zip")
            self.assertEqual(scan_zip(path), [])
            with zipfile.ZipFile(path) as handle:
                names = [info.filename.replace("\\", "/") for info in handle.infolist()]
            joined = "\n".join(names)
            self.assertTrue(any(name.endswith("/Start.bat") for name in names))
            self.assertFalse(any(name.lower().endswith(".bat") and not name.endswith("/Start.bat") for name in names))
            self.assertNotIn("window.json", joined)
            self.assertNotIn("working_copies", joined)
            self.assertTrue(any(name.endswith("/nmsmissions/release.py") for name in names))
            self.assertTrue(any(name.endswith("/tests/test_edit.py") for name in names))
            self.assertFalse(any(name.lower().endswith((".hg", ".mbin", ".exml", ".mxml", ".pak")) for name in names))
            self.assertTrue(any(name.endswith("/LICENSE") for name in names))
            self.assertTrue(any(name.endswith("/nmsmissions/data/names.yaml") for name in names))
            with zipfile.ZipFile(path) as handle:
                for info in handle.infolist():
                    self.assertEqual(info.date_time, ZIP_DATE)
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual((info.external_attr >> 16) & 0o777, 0o644)
            token = "zzbannedtokenzz"
            banned_file = folder / "banned.txt"
            banned_file.write_text(token + "\n", encoding="utf-8")
            previous = os.environ.get("NMSMISSIONS_BANNED_FILE")
            os.environ["NMSMISSIONS_BANNED_FILE"] = str(banned_file)
            try:
                with zipfile.ZipFile(path, "a") as handle:
                    handle.writestr(f"nms-mission-editor-{__version__}/note.txt", "see " + token + " here")
                self.assertTrue(scan_zip(path))
                self.assertTrue(any("banned token" in item for item in scan_zip(path)))
                self.assertFalse(any(token in item for item in scan_zip(path)))
            finally:
                if previous is None:
                    os.environ.pop("NMSMISSIONS_BANNED_FILE", None)
                else:
                    os.environ["NMSMISSIONS_BANNED_FILE"] = previous
            public = export_public_tree(folder / "public_repo")
            self.assertTrue((public / "tests" / "test_edit.py").is_file())
            self.assertTrue((public / "Start.bat").is_file())
            self.assertFalse((public / "working_copies").exists())

    def test_release_py_has_no_hex_constant(self) -> None:
        import re

        self._need_release()
        from nmsmissions import release as release_module

        text = Path(release_module.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b[0-9a-fA-F]{64}\b", text))

    def test_personal_list_is_skipped_when_the_file_is_absent(self) -> None:
        self._need_release()
        previous = os.environ.pop("NMSMISSIONS_BANNED_FILE", None)
        try:
            self.assertIn("Personal list was skipped.", personal_list_note())
            self.assertEqual(banned_hashes(), frozenset())
        finally:
            if previous is not None:
                os.environ["NMSMISSIONS_BANNED_FILE"] = previous

    def test_generic_leaks_are_reported_without_a_personal_list(self) -> None:
        self._need_release()
        previous = os.environ.pop("NMSMISSIONS_BANNED_FILE", None)
        try:
            with TemporaryDirectory() as tmp:
                folder = Path(tmp)
                path = build_release_zip(folder / "editor.zip")
                self.assertEqual(scan_zip(path), [])
                drive = "D:" + "\\" + "copies" + "\\" + "save2.hg"
                users = "/" + "Users" + "/someone"
                home = "/" + "home" + "/someone"
                steam = "7656119" + "0123456789"
                email = "person" + "@" + "example.com"
                body = "\n".join((drive, users, home, steam, email))
                with zipfile.ZipFile(path, "a") as handle:
                    handle.writestr(f"nms-mission-editor-{__version__}/note.txt", body)
                joined = "\n".join(scan_zip(path))
                self.assertIn("absolute drive path", joined)
                self.assertIn("home folder path", joined)
                self.assertIn("steam id", joined)
                self.assertIn("email address", joined)
                phone = "(555)" + " " + "010" + "-" + "0101"
                dotted = "555" + "." + "010" + "." + "0101"
                with zipfile.ZipFile(path, "a") as handle:
                    handle.writestr(
                        f"nms-mission-editor-{__version__}/phone.txt",
                        phone + "\n" + dotted,
                    )
                phones = "\n".join(scan_zip(path))
                self.assertIn("phone number", phones)
                self.assertNotIn(phone, phones)
                self.assertNotIn(drive, joined)
                self.assertNotIn(steam, joined)
                self.assertNotIn(email, joined)
                self.assertIn("Personal list was skipped.", personal_list_note())
        finally:
            if previous is not None:
                os.environ["NMSMISSIONS_BANNED_FILE"] = previous

    def test_setup_from_empty_folder(self) -> None:
        self._need_release()
        import subprocess

        from nmsmissions.bootstrap import setup_folder, venv_python

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            archive = build_release_zip(folder / "editor.zip")
            dest = folder / "fresh"
            dest.mkdir()
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(dest)
            unpacked = next(dest.iterdir())
            self.assertTrue((unpacked / "Start.bat").is_file())
            self.assertIn("venv", (unpacked / "Start.bat").read_text(encoding="utf-8"))
            self.assertFalse((unpacked / ".venv").exists())
            python = setup_folder(unpacked)
            self.assertEqual(python, venv_python(unpacked))
            self.assertTrue(python.is_file())
            probe = subprocess.run(
                [str(python), "-c", "import lz4, yaml"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_discover_install_and_warns_on_another_build(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam = root / "Steam"
            library = root / "Library"
            install = library / "steamapps" / "common" / "No Man's Sky"
            (install / "GAMEDATA" / "PCBANKS").mkdir(parents=True)
            (install / "Binaries").mkdir()
            (library / "steamapps" / "appmanifest_275850.acf").write_text(
                '"appid" "275850"\n"buildid" "111"\n',
                encoding="utf-8",
            )
            (steam / "steamapps").mkdir(parents=True)
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                '"path" "' + str(library).replace("\\", "\\\\") + '"\n',
                encoding="utf-8",
            )
            saves = root / "AppData" / "HelloGames" / "NMS" / "st_one"
            saves.mkdir(parents=True)
            found = discover({"NMS_STEAM_ROOT": str(steam), "APPDATA": str(root / "AppData")})
            self.assertEqual(found.install, install)
            self.assertEqual(found.pcbanks, install / "GAMEDATA" / "PCBANKS")
            self.assertEqual(found.saves, [saves])
            self.assertEqual(found.build_id, "111")
            self.assertTrue(found.version_differs)
            self.assertIn("111", found.version_line)
            self.assertIn(TESTED_BUILD, found.version_line)
            message = startup_message(found)
            self.assertIn("Back up your save", message)
            self.assertIn("own risk", message)
            self.assertIn("111", message)

            (library / "steamapps" / "appmanifest_275850.acf").write_text(
                f'"buildid" "{TESTED_BUILD}"\n',
                encoding="utf-8",
            )
            same = discover({"NMS_INSTALL": str(install), "APPDATA": str(root / "AppData")})
            self.assertFalse(same.version_differs)
            self.assertIn("7.05", same.version_line)
            missing = discover({"NMS_STEAM_ROOT": str(root / "missing"), "APPDATA": str(root / "missing")})
            self.assertIsNone(missing.install)
            self.assertEqual(missing.saves, [])
            self.assertTrue(missing.version_differs)
            line, differs = version_line("")
            self.assertTrue(differs)
            self.assertIn("not detected", line)

    def test_tip_line_names_x_money_and_cash_app(self) -> None:
        expected = f"X Money: {X_MONEY_HANDLE}  |  Cash App: {CASH_APP} (https://cash.app/{CASH_APP})"
        self.assertEqual(X_MONEY_HANDLE, "Antilitist")
        self.assertEqual(tip_text(), expected)
        self.assertIn(expected, about_body())
        self.assertIn(CREDIT, about_body())
        self.assertIn(AI_CREDIT, about_body())
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn(expected, readme)
        self.assertNotIn("{X_MONEY_HANDLE}", readme)

    def test_about_button_and_safety_line(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        from unittest.mock import patch

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import launch
        from tests.test_slots import _one, _walk, _write_pair

        body = '{"XTp":"Main","b@r":1,"vLc":{"6f=":{"dwb":[],"yq:":39}}}'
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", body)
            session = EditorSession(path, backup_dir=folder / "backups")
            session.version_note = "Detected build 111. This tool was tested on No Man's Sky 7.05 (build 25625620)."
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
                labels = [str(widget.cget("text")) for widget in widgets if isinstance(widget, tk.Label)]
                self.assertTrue(any("Use at your own risk" in text and "25625620" in text for text in labels))
                self.assertTrue(any("Detected build 111" in text for text in labels))
                about = _one(widgets, ttk.Button, "About / Tip me")
                with patch("tkinter.messagebox.showinfo") as info:
                    about.invoke()
                text = info.call_args.args[1]
                self.assertIn("1.1.2", text)
                self.assertIn("X Money: Antilitist", text)
                self.assertIn("Cash App", text)
                self.assertIn(CREDIT, text)
                buttons = [str(widget.cget("text")) for widget in widgets if isinstance(widget, ttk.Button)]
                self.assertIn("Read my game files", buttons)
                self.assertIn("Clear cache", buttons)
            finally:
                window.destroy()

    def test_startup_cancel_stops_before_the_save(self) -> None:
        from unittest.mock import Mock, patch

        from nmsmissions.locate import Discovery

        os.environ.pop("NMSMISSIONS_SKIP_SAFETY", None)
        try:
            window = Mock()
            with patch("nmsmissions.gui.begin_loading", return_value=window), patch(
                "nmsmissions.locate.discover", return_value=Discovery()
            ), patch("nmsmissions.locate.offer_install_picker", side_effect=lambda _parent, found: found), patch(
                "nmsmissions.locate.confirm_startup", return_value=False
            ), patch("nmsmissions.cli._open", side_effect=AssertionError("opened")):
                from nmsmissions.cli import main

                code = main(["gui", "missing.hg"])
            self.assertEqual(code, 0)
            window.destroy.assert_called()
        finally:
            os.environ["NMSMISSIONS_SKIP_SAFETY"] = "1"


class OfflineMappingTests(unittest.TestCase):
    def test_download_failure_uses_the_built_in_key_list(self) -> None:
        from unittest.mock import patch

        from nmsmissions.mapping import load_mapping

        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "missing.json"
            with patch("nmsmissions.mapping.urllib.request.urlopen", side_effect=OSError("offline")):
                table, source = load_mapping(cache=cache)
        self.assertEqual(source, "built-in key list")
        self.assertEqual(table["Kg6"], "HasDiscoveredPurpleSystems")
        self.assertEqual(table["6f="], "PlayerStateData")
        self.assertEqual(table[";R7"], "CurrentMissionID")
        self.assertNotIn("b@r", table)


class PrivacyTests(unittest.TestCase):
    def test_current_files_use_generic_examples(self) -> None:
        from nmsmissions.release import scan_shipped_tree

        editor = Path(__file__).resolve().parents[1]
        roots = [editor]
        parent = editor.parent
        if (parent / "nms_mission_editor").is_dir():
            readme = parent / "README.md"
            if readme.is_file():
                roots.append(readme)
            for name in ("Atlas_FleetTweaks", "Atlas_ShipSlots", "uploads"):
                extra = parent / name
                if extra.exists():
                    roots.append(extra)
        previous = os.environ.pop("NMSMISSIONS_BANNED_FILE", None)
        try:
            problems = scan_shipped_tree(roots)
        finally:
            if previous is not None:
                os.environ["NMSMISSIONS_BANNED_FILE"] = previous
        self.assertEqual(problems, [])

    def test_an_install_path_is_not_a_leak(self) -> None:
        from nmsmissions.release import _scan_hits, scan_shipped_tree

        install = "C:" + "\\" + "Games" + "\\" + "editor" + "\\" + "README.md"
        self.assertEqual(_scan_hits("plain words", install, frozenset()), [])
        leaked = "D:" + "\\" + "copies" + "\\" + "save2.hg"
        self.assertTrue(any("absolute drive path" in item for item in _scan_hits(leaked, "note.txt", frozenset())))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            note = folder / "note.txt"
            note.write_text("plain words\n", encoding="utf-8")
            self.assertEqual(scan_shipped_tree([folder]), [])
            note.write_text(leaked + "\n", encoding="utf-8")
            found = "\n".join(scan_shipped_tree([folder]))
            self.assertIn("absolute drive path", found)
            self.assertNotIn(leaked, found)


if __name__ == "__main__":
    unittest.main()

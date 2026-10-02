"""Slot list and working-copy rules. Synthetic files only."""

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
import struct
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from nmsmissions.chunks import pack_chunks
from nmsmissions.edit import EditError, Plan, backup_now, format_plan, install_one_save, partner_load_problem
from tests.harness import TkCleanup
from nmsmissions.manifest import FORMAT_2004, MAGIC, archive_number, encrypt_manifest, key_slot_for
from nmsmissions.slots import (
    already_in_game_note,
    copy_status,
    slot_status_line,
    group_slots,
    list_backups,
    load_state,
    nms_save_folders,
    play_text,
    prepare_working_copy,
    read_summary,
    remember_slot,
    remembered_slot,
    slot_of,
    slot_row,
    summarize_player,
)


def _write_pair(folder: Path, name: str, text: str) -> Path:
    raw = text.encode("utf-8") + b"\x00"
    packed = pack_chunks(raw)
    path = folder / name
    path.write_bytes(packed)
    words = [0] * 108
    words[0] = MAGIC
    words[1] = FORMAT_2004
    words[14] = len(raw)
    words[15] = len(packed)
    plain = struct.pack("<108I", *words)
    manifest = path.with_name("mf_" + path.name)
    manifest.write_bytes(encrypt_manifest(plain, key_slot_for(archive_number(path))))
    return path


def _touch(path: Path, when_ns: int) -> None:
    os.utime(path, ns=(when_ns, when_ns))


class SlotTests(unittest.TestCase):
    def test_pairs_and_which_file_is_newer(self):
        self.assertEqual(slot_of("save.hg"), 1)
        self.assertEqual(slot_of("save2.hg"), 1)
        self.assertEqual(slot_of("save1.hg"), 1)
        self.assertEqual(slot_of("save7.hg"), 4)
        self.assertEqual(slot_of("save8.hg"), 4)
        self.assertIsNone(slot_of("mf_save7.hg"))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("save.hg", "save1.hg", "save2.hg", "save7.hg", "save8.hg", "mf_save7.hg", "notes.txt"):
                (folder / name).write_bytes(b"x")
            base = 1_700_000_000_000_000_000
            _touch(folder / "save.hg", base)
            _touch(folder / "save1.hg", base)
            _touch(folder / "save2.hg", base)
            _touch(folder / "save7.hg", base + 5_000_000_000)
            _touch(folder / "save8.hg", base)
            groups = {item.slot: item for item in group_slots(folder)}
            self.assertEqual(set(groups), {1, 4})
            self.assertEqual(
                [path.name for path in groups[1].files],
                ["save.hg", "save1.hg", "save2.hg"],
            )
            self.assertEqual(groups[1].newer.name, "save2.hg")
            self.assertEqual(groups[4].newer.name, "save7.hg")
            self.assertIn("save7.hg is newer", groups[4].newer_note())
            _touch(folder / "save8.hg", base + 5_000_000_000)
            tied = {item.slot: item for item in group_slots(folder)}[4]
            self.assertEqual(tied.newer.name, "save8.hg")
            row = slot_row(tied, "Normal", 7200)
            self.assertIn("Slot 4", row)
            self.assertIn("Normal", row)
            self.assertIn("2.0 h", row)
            self.assertIn("save8.hg is newer", row)

    def test_mode_play_time_and_save_folders(self):
        mode, play = summarize_player({"PresetGameMode": "Normal", "TotalPlayTime": 7200})
        self.assertEqual((mode, play), ("Normal", 7200))
        self.assertEqual(play_text(7200), "2.0 h")
        self.assertEqual(play_text(20_000_000), "5.6 h")
        self.assertEqual(play_text(None), "—")
        mixed, _play = summarize_player(
            {"PresetGameMode": "GcGameMode.xml", "GameMode": "Survival", "DifficultyPreset": "Custom"}
        )
        self.assertEqual(mixed, "Survival / Custom")
        blank, missing = summarize_player({"note": "no mode"})
        self.assertEqual(blank, "—")
        self.assertIsNone(missing)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            raw = json.dumps({"abc": "Permadeath", "def": 3600}).encode("utf-8") + b"\x00"
            path = folder / "save7.hg"
            path.write_bytes(pack_chunks(raw))
            self.assertEqual(
                read_summary(path, {"abc": "PresetGameMode", "def": "TotalPlayTime"}),
                ("Permadeath", 3600),
            )
            junk = folder / "save8.hg"
            junk.write_bytes(b"not a save")
            self.assertEqual(read_summary(junk), ("—", None))
            (folder / "st_one").mkdir()
            (folder / "st_two").mkdir()
            (folder / "other").mkdir()
            self.assertEqual(
                nms_save_folders({"NMS_SAVE_DIR": str(folder)}),
                [folder / "st_one", folder / "st_two"],
            )
            self.assertEqual(
                nms_save_folders({"NMS_TEST_SAVEDIR": str(folder / "st_one")}),
                [folder / "st_one"],
            )
            app = folder / "app"
            account = app / "HelloGames" / "NMS" / "st_a"
            account.mkdir(parents=True)
            self.assertEqual(nms_save_folders({"APPDATA": str(app)}), [account])
            self.assertEqual(nms_save_folders({"APPDATA": str(folder / "missing")}), [])

    def test_working_copy_refresh_keep_and_ask(self):
        home_backups = Path.home() / ".cache" / "nms_mission_editor" / "backups"
        before = {path.name for path in home_backups.rglob("*") if path.is_file()} if home_backups.is_dir() else set()
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS" / "st_test"
            live_dir.mkdir(parents=True)
            live = live_dir / "save8.hg"
            live.write_bytes(b"live-v1")
            (live_dir / "mf_save8.hg").write_bytes(b"mf-v1")
            copy_dir = folder / "working" / "st_test-slot4"
            first = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(first.action, "copy")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"live-v1")
            self.assertEqual((copy_dir / "mf_save8.hg").read_bytes(), b"mf-v1")
            (copy_dir / "save8.hg").write_bytes(b"edited")
            kept = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(kept.action, "keep")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"edited")
            live.write_bytes(b"live-v2")
            _touch(live, time.time_ns() + 5_000_000_000)
            asked = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(asked.action, "ask")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"edited")
            stayed = prepare_working_copy(live, copy_dir, slot=4, account="st_test", choice="edits")
            self.assertEqual(stayed.action, "keep")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"edited")
            still = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(still.action, "keep")
            live.write_bytes(b"live-v3")
            _touch(live, time.time_ns() + 9_000_000_000)
            (live_dir / "mf_save8.hg").write_bytes(b"mf-v3")
            used_game = prepare_working_copy(
                live, copy_dir, slot=4, account="st_test", choice="game", backup_dir=folder / "backups"
            )
            self.assertEqual(used_game.action, "refresh")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"live-v3")
            kept_edit = list((folder / "backups").glob("*.zip"))
            self.assertEqual(len(kept_edit), 1)
            import zipfile

            with zipfile.ZipFile(kept_edit[0]) as handle:
                self.assertEqual(handle.read("save8.hg"), b"edited")
            self.assertEqual((copy_dir / "mf_save8.hg").read_bytes(), b"mf-v3")
            live.write_bytes(b"live-v4")
            _touch(live, time.time_ns() + 12_000_000_000)
            refreshed = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(refreshed.action, "refresh")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"live-v4")
            stored = load_state(copy_dir)
            self.assertIsNotNone(stored)
            live.write_bytes(b"same-time")
            _touch(live, stored.source_mtime_ns)
            quiet = prepare_working_copy(live, copy_dir, slot=4, account="st_test")
            self.assertEqual(quiet.action, "ask")
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"live-v4")
        after = {path.name for path in home_backups.rglob("*") if path.is_file()} if home_backups.is_dir() else set()
        self.assertEqual(after, before)

    def test_last_slot_status_and_backup_list(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(remembered_slot(root))
            remember_slot(root, "st_test", 4)
            self.assertEqual(remembered_slot(root), ("st_test", 4))
            origin = root / "save8.hg"
            copy = root / "copy.hg"
            origin.write_bytes(b"same")
            copy.write_bytes(b"same")
            self.assertEqual(copy_status(copy, None), "This file was opened from a path.")
            self.assertEqual(copy_status(copy, origin), "matches game")
            self.assertEqual(already_in_game_note(copy, origin), "copy.hg is already in the game.")
            copy.write_bytes(b"edited")
            self.assertEqual(copy_status(copy, origin), "Working copy: edited, not yet in game")
            self.assertIsNone(already_in_game_note(copy, origin))
            backups = root / "backups"
            backups.mkdir()
            stamped = backups / "save8.hg.20260301-153045.zip"
            with zipfile.ZipFile(stamped, "w") as handle:
                handle.writestr("saveinfo.json", json.dumps({"save": "save8.hg"}))
            plain = backups / "notes.zip"
            with zipfile.ZipFile(plain, "w") as handle:
                handle.writestr("readme.txt", "no save info")
            _touch(stamped, 2_000_000_000_000_000_000)
            _touch(plain, 1_000_000_000_000_000_000)
            found = list_backups(backups)
            self.assertEqual(found[0].path, stamped)
            self.assertEqual(found[0].slot, 4)
            self.assertEqual(found[0].when_text, "2026-03-01 15:30:45")
            self.assertIn("slot 4", found[0].label())
            self.assertIsNone(found[1].slot)
            counted = backups / "save8.hg.20260301-153045-2.zip"
            with zipfile.ZipFile(counted, "w") as handle:
                handle.writestr("saveinfo.json", json.dumps({"save": "save8.hg"}))
            _touch(counted, 2_000_000_000_000_000_001)
            labels = {item.path.name: item.when_text for item in list_backups(backups)}
            self.assertEqual(labels[counted.name], "2026-03-01 15:30:45 (2)")

    def test_preview_wording_and_install_one_file(self):
        preview = format_plan(
            Plan(summary="Reward preview.", granted=["R_X: 1 ^AMMO."]),
            dry_run=True,
            apply_hint="This preview does not write the save.",
        )
        self.assertNotIn("No changes.", preview)
        self.assertIn("Granted:", preview)
        self.assertIn("R_X: 1 ^AMMO.", preview)
        self.assertNotIn("Pass --apply", preview)
        empty = format_plan(Plan(summary="Finish."), dry_run=True)
        self.assertIn("No changes.", empty)
        self.assertIn("Pass --apply", empty)
        body = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            dst = folder / "dst"
            src.mkdir()
            dst.mkdir()
            path = _write_pair(src, "save8.hg", body)
            code, message, undo_zip = install_one_save(
                path, dst, 4, folder / "backups", apply=True, live=False, confirm_slot=None
            )
            self.assertIsNone(undo_zip)
            self.assertEqual(code, 0)
            self.assertEqual((dst / "save8.hg").read_bytes(), path.read_bytes())
            self.assertEqual((dst / "mf_save8.hg").read_bytes(), (src / "mf_save8.hg").read_bytes())
            self.assertIn("Slot 4", message)
            old = 1_700_000_000_000_000_000
            _touch(path, old)
            _touch(src / "mf_save8.hg", old)
            blocked = folder / "blocked"
            blocked.mkdir()
            newer = _write_pair(blocked, "save8.hg", body)
            before = newer.read_bytes()
            _touch(newer, old + 20_000_000_000)
            with self.assertRaises(EditError) as newer_error:
                install_one_save(path, blocked, 4, folder / "backups", apply=True, live=False, confirm_slot=None)
            self.assertIn("newer", str(newer_error.exception))
            self.assertEqual(newer.read_bytes(), before)
            live = folder / "HelloGames" / "NMS"
            live.mkdir(parents=True)
            with self.assertRaises(EditError) as live_error:
                install_one_save(path, live, 4, folder / "backups", apply=True, live=False, confirm_slot=None)
            self.assertIn("slot 4", str(live_error.exception))
            os.environ["NMSMISSIONS_NMS_RUNNING"] = "1"
            try:
                with self.assertRaises(EditError) as running:
                    install_one_save(path, dst, 4, folder / "backups", apply=True, live=False, confirm_slot=None)
                self.assertIn("running", str(running.exception))
                zipped = backup_now(path, folder / "backups")
                self.assertTrue(zipped.is_file())
                live_save = _write_pair(live, "save8.hg", body)
                with self.assertRaises(EditError):
                    backup_now(live_save, folder / "backups")
            finally:
                os.environ.pop("NMSMISSIONS_NMS_RUNNING", None)


class SlotFixTests(unittest.TestCase):
    def test_other_file_of_the_pair_asks_before_dropping_edits(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "live"
            live_dir.mkdir()
            live7 = live_dir / "save7.hg"
            live7.write_bytes(b"seven-v1")
            (live_dir / "mf_save7.hg").write_bytes(b"mf7")
            copy_dir = folder / "working"
            first = prepare_working_copy(live7, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups")
            self.assertEqual(first.action, "copy")
            (copy_dir / "save7.hg").write_bytes(b"seven-edited")
            live8 = live_dir / "save8.hg"
            live8.write_bytes(b"eight")
            (live_dir / "mf_save8.hg").write_bytes(b"mf8")
            _touch(live8, time.time_ns() + 5_000_000_000)
            asked = prepare_working_copy(live8, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups")
            self.assertEqual(asked.action, "ask")
            self.assertEqual((copy_dir / "save7.hg").read_bytes(), b"seven-edited")
            self.assertFalse((copy_dir / "save8.hg").exists())
            kept = prepare_working_copy(
                live8, copy_dir, slot=4, account="st_test", choice="edits", backup_dir=folder / "backups"
            )
            self.assertEqual(kept.action, "keep")
            self.assertEqual(kept.copy_path.name, "save7.hg")
            self.assertEqual(kept.copy_path.read_bytes(), b"seven-edited")
            again = prepare_working_copy(live8, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups")
            self.assertEqual(again.action, "keep")
            live8.write_bytes(b"eight-v2")
            _touch(live8, time.time_ns() + 9_000_000_000)
            asked_again = prepare_working_copy(live8, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups")
            self.assertEqual(asked_again.action, "ask")
            used = prepare_working_copy(
                live8, copy_dir, slot=4, account="st_test", choice="game", backup_dir=folder / "backups"
            )
            self.assertEqual((copy_dir / "save8.hg").read_bytes(), b"eight-v2")
            self.assertIn("backed up", used.message)
            with zipfile.ZipFile(next((folder / "backups").glob("save7.hg.*.zip"))) as handle:
                self.assertEqual(handle.read("save7.hg"), b"seven-edited")

    def test_save_hg_opens_and_undo_restores_the_game_file(self):
        from nmsmissions.diffing import choose_save_file, save_slot
        from nmsmissions.edit import undo_last_install
        from nmsmissions.manifest import archive_number, read_manifest
        from nmsmissions.slots import load_state, note_installed

        self.assertEqual(archive_number(Path("save.hg")), 0)
        self.assertEqual(archive_number(Path("save1.hg")), 0)
        self.assertEqual(save_slot(Path("save.hg")), 1)
        self.assertEqual(save_slot(Path("mf_save.hg")), 1)
        body_old = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        body_new = json.dumps({"XTp": "Main", "b@r": 11, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS"
            live_dir.mkdir(parents=True)
            plain = _write_pair(live_dir, "save.hg", body_old)
            paired = _write_pair(live_dir, "save2.hg", body_old)
            _touch(plain, 1_000_000_000)
            _touch(live_dir / "mf_save.hg", 1_000_000_000)
            _touch(paired, 5_000_000_000)
            chosen, note = choose_save_file(plain)
            self.assertEqual(chosen, paired)
            self.assertIn("save2.hg", note or "")
            manifest = read_manifest(live_dir / "mf_save.hg")
            self.assertEqual(manifest.archive, 0)
            original_save = plain.read_bytes()
            original_mf = (live_dir / "mf_save.hg").read_bytes()
            edited = _write_pair(folder, "save.hg", body_new)
            _touch(edited, 9_000_000_000)
            _touch(folder / "mf_save.hg", 9_000_000_000)
            copy_dir = folder / "working"
            copy_dir.mkdir()
            code, message, undo_zip = install_one_save(
                edited,
                live_dir,
                1,
                folder / "backups",
                apply=True,
                live=True,
                confirm_slot=1,
            )
            self.assertEqual(code, 0, message)
            self.assertIsNotNone(undo_zip)
            self.assertEqual((live_dir / "save.hg").read_bytes(), edited.read_bytes())
            from nmsmissions.slots import save_state

            note_installed(copy_dir, live_dir / "save.hg", edited, undo_zip, folder / "backups")
            state = load_state(copy_dir)
            self.assertIsNotNone(state)
            assert state is not None
            state.slot = 1
            save_state(copy_dir, state)
            self.assertTrue(state.undo_zip)
            before_undo = (live_dir / "save.hg").read_bytes()
            os.environ["NMSMISSIONS_NMS_RUNNING"] = "1"
            try:
                with self.assertRaises(EditError) as running:
                    undo_last_install(
                        copy_dir,
                        folder / "backups",
                        apply=True,
                        live=True,
                        confirm_slot=1,
                        allow_changed=False,
                    )
                self.assertIn("running", str(running.exception))
            finally:
                os.environ.pop("NMSMISSIONS_NMS_RUNNING", None)
            self.assertEqual((live_dir / "save.hg").read_bytes(), before_undo)
            body_changed = json.dumps({"XTp": "Main", "b@r": 40, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
            _write_pair(live_dir, "save.hg", body_changed)
            with self.assertRaises(EditError) as changed:
                undo_last_install(
                    copy_dir,
                    folder / "backups",
                    apply=True,
                    live=True,
                    confirm_slot=1,
                    allow_changed=False,
                )
            self.assertIn("changed", str(changed.exception))
            self.assertNotEqual((live_dir / "save.hg").read_bytes(), original_save)
            code, message = undo_last_install(
                copy_dir,
                folder / "backups",
                apply=True,
                live=True,
                confirm_slot=1,
                allow_changed=True,
            )
            self.assertEqual(code, 0, message)
            self.assertEqual((live_dir / "save.hg").read_bytes(), original_save)
            self.assertEqual((live_dir / "mf_save.hg").read_bytes(), original_mf)
            self.assertIn("Slot 1", message)
            restored_state = load_state(copy_dir)
            self.assertIsNotNone(restored_state)
            assert restored_state is not None
            self.assertEqual(restored_state.undo_zip, "")
            slot_copy = folder / "slot1"
            slot_copy.mkdir()
            _write_pair(slot_copy, "save.hg", body_new)
            _write_pair(slot_copy, "save2.hg", body_new)
            from nmsmissions.edit import EditRequest, run

            dest = folder / "installed"
            self.assertEqual(
                run(
                    EditRequest(
                        action="install",
                        install_folder=slot_copy,
                        install_slot=1,
                        install_to=dest,
                        apply=True,
                        backup_dir=folder / "backups",
                        quiet=True,
                    )
                ),
                0,
            )
            self.assertTrue((dest / "save.hg").is_file())
            self.assertTrue((dest / "mf_save.hg").is_file())
            self.assertTrue((dest / "save2.hg").is_file())

    def test_undo_sees_a_newer_partner_file(self):
        from nmsmissions.edit import undo_last_install
        from nmsmissions.slots import SyncState, load_state, note_installed, save_state

        old = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        edited = json.dumps({"XTp": "Main", "b@r": 11, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        played = json.dumps({"XTp": "Main", "b@r": 80, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS"
            live_dir.mkdir(parents=True)
            save7 = _write_pair(live_dir, "save7.hg", old)
            save8 = _write_pair(live_dir, "save8.hg", old)
            original7 = save7.read_bytes()
            original8 = save8.read_bytes()
            _touch(save7, 1_000_000_000)
            _touch(live_dir / "mf_save7.hg", 1_000_000_000)
            _touch(save8, 1_000_000_000)
            _touch(live_dir / "mf_save8.hg", 1_000_000_000)
            copy = _write_pair(folder, "save7.hg", edited)
            _touch(copy, 2_000_000_000)
            _touch(folder / "mf_save7.hg", 2_000_000_000)
            copy_dir = folder / "working"
            copy_dir.mkdir()
            save_state(
                copy_dir,
                SyncState("", "", 0, "", "", 4, "st_test"),
            )
            code, message, undo_zip = install_one_save(
                copy, live_dir, 4, folder / "backups", apply=True, live=True, confirm_slot=4
            )
            self.assertEqual(code, 0, message)
            note_installed(copy_dir, save7, copy, undo_zip, folder / "backups")
            _write_pair(live_dir, "save8.hg", played)
            _touch(live_dir / "save8.hg", time.time_ns() + 20_000_000_000)
            _touch(live_dir / "mf_save8.hg", time.time_ns() + 20_000_000_000)
            played8 = (live_dir / "save8.hg").read_bytes()
            with self.assertRaises(EditError) as blocked:
                undo_last_install(
                    copy_dir,
                    folder / "backups",
                    apply=True,
                    live=True,
                    confirm_slot=4,
                    allow_changed=False,
                )
            self.assertIn("save8.hg", str(blocked.exception))
            self.assertIn("game will load", str(blocked.exception))
            self.assertEqual((live_dir / "save7.hg").read_bytes(), copy.read_bytes())
            self.assertEqual((live_dir / "save8.hg").read_bytes(), played8)
            code, message = undo_last_install(
                copy_dir,
                folder / "backups",
                apply=True,
                live=True,
                confirm_slot=4,
                allow_changed=True,
                allow_partner=True,
            )
            self.assertEqual(code, 0, message)
            self.assertEqual((live_dir / "save7.hg").read_bytes(), original7)
            self.assertEqual((live_dir / "save8.hg").read_bytes(), original8)
            self.assertIn("save8.hg", message)
            state = load_state(copy_dir)
            self.assertIsNotNone(state)
            assert state is not None
            state.installed_at_ns = 1
            state.undo_zip = str(undo_zip)
            state.partner_zips = {}
            state.installed_sha256 = ""
            save_state(copy_dir, state)
            _touch(live_dir / "save8.hg", time.time_ns() + 30_000_000_000)
            before7 = (live_dir / "save7.hg").read_bytes()
            before8 = (live_dir / "save8.hg").read_bytes()
            with self.assertRaises(EditError) as lost:
                undo_last_install(
                    copy_dir,
                    folder / "backups",
                    apply=True,
                    live=True,
                    confirm_slot=4,
                    allow_changed=True,
                    allow_partner=True,
                )
            self.assertIn("Restore a backup", str(lost.exception))
            self.assertEqual((live_dir / "save7.hg").read_bytes(), before7)
            self.assertEqual((live_dir / "save8.hg").read_bytes(), before8)

    def test_restore_list_stays_on_the_open_slot(self):
        from nmsmissions.slots import BackupInfo, filter_backups, restore_slot_warning, restored_file_name

        slot4 = BackupInfo(Path("save7.hg.zip"), "2026-10-01 15:30:45", 4, "save7.hg")
        slot1 = BackupInfo(Path("save.hg.zip"), "2026-10-01 15:30:46", 1, "save.hg")
        unknown = BackupInfo(Path("notes.zip"), "2026-10-01 15:30:47", None, "notes.zip")
        rows = [slot4, slot1, unknown]
        self.assertEqual(filter_backups(rows, 4, False), [slot4])
        self.assertEqual(filter_backups(rows, 4, True), rows)
        self.assertEqual(filter_backups(rows, None, False), rows)
        self.assertEqual(restored_file_name("save7.hg", "save.hg", 4, 1), "save.hg")
        self.assertEqual(restored_file_name("save.hg", "save.hg", 1, 1), "save.hg")
        self.assertEqual(restored_file_name("save7.hg", "save7.hg", 4, 4), "save7.hg")
        self.assertIsNone(restore_slot_warning(4, 4))
        warning = restore_slot_warning(1, 4)
        self.assertIsNotNone(warning)
        assert warning is not None
        self.assertIn("slot 1", warning)
        self.assertIn("slot 4", warning)
        self.assertIn("other slot", warning)
        self.assertIn("unknown", restore_slot_warning(None, 4) or "")

    def test_banner_path_follows_the_open_save(self):
        from nmsmissions.gui import banner_mode_line, set_banner_mode

        line = banner_mode_line("working_copies/st-slot4/save8.hg", live=False, editing=True)
        self.assertIn("save8.hg", line)
        self.assertIn("This is a copy", line)

        class Banner:
            def __init__(self) -> None:
                self.mode_line = "This is a copy.\nworking_copies/st-slot1/save.hg"
                self.text = self.mode_line
                self.fg = ""

            def configure(self, **kwargs) -> None:
                if "text" in kwargs:
                    self.text = kwargs["text"]
                if "fg" in kwargs:
                    self.fg = kwargs["fg"]

        banner = Banner()
        set_banner_mode(banner, "working_copies/st-slot4/save8.hg", live=False, editing=True)
        self.assertIn("save8.hg", banner.mode_line)
        self.assertNotIn("save.hg", banner.mode_line)
        self.assertIn("save8.hg", banner.text)
        names = "Names from game files: 3 titles, 10 language rows, 2 mission links, 4 files read."
        banner.mode_line = banner.mode_line + "\n" + names
        set_banner_mode(banner, "working_copies/st-slot4/save7.hg", live=False, editing=True)
        self.assertIn("save7.hg", banner.mode_line)
        self.assertIn(names, banner.mode_line)
        self.assertIn(names, banner.text)

        from nmsmissions.gui import _keep_banner

        _keep_banner(banner, "Cache cleared.", "#0b6e4f")
        set_banner_mode(banner, "working_copies/st-slot4/save8.hg", live=False, editing=True)
        self.assertIn("Cache cleared.", banner.text)
        self.assertIn("save8.hg", banner.text)

    def test_clear_cache_rebuilds_the_name_line_from_the_list(self):
        from nmsmissions.gamenames import list_name_line
        from nmsmissions.gui import set_banner_mode

        rows = [
            {"mission_id": f"^M{index}", "name": f"They Who Returned - step {index}"}
            for index in range(664)
        ]
        line = list_name_line(rows, None)
        self.assertIn("664 mission links", line)
        self.assertIn("664 still have no name", line)

        class Banner:
            def __init__(self) -> None:
                self.mode_line = (
                    "This is a copy.\nworking_copies/slot/save.hg\n"
                    "Names from game files: 1584 titles, 9000 language rows, "
                    "664 mission links, 40 files read. 475 still have no name."
                )
                self.text = self.mode_line
                self.status_line = ""
                self.fg = ""

            def configure(self, **kwargs) -> None:
                if "text" in kwargs:
                    self.text = kwargs["text"]
                if "fg" in kwargs:
                    self.fg = kwargs["fg"]

        banner = Banner()
        set_banner_mode(banner, "working_copies/slot/save.hg\n" + line, live=False, editing=True)
        self.assertIn("664 still have no name", banner.mode_line)
        self.assertNotIn("475 still have no name", banner.mode_line)
        self.assertIn("664 mission links", banner.text)

    def test_middle_reset_uses_plain_words(self):
        from nmsmissions.edit import EditRequest, _mission_ids

        with self.assertRaises(EditError) as caught:
            _mission_ids(EditRequest(action="reset", mission="^PURPM2"), finish=False)
        text = str(caught.exception)
        self.assertIn("Reset from this step", text)
        self.assertNotIn("--chain", text)
        self.assertNotIn("--from", text)

    def test_undo_after_reopen_restores_the_newer_file(self):
        from nmsmissions.edit import installed_live_path, newer_slot_files, undo_last_install
        from nmsmissions.slots import SyncState, load_state, note_installed, save_state

        old = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        edited = json.dumps({"XTp": "Main", "b@r": 11, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        played = json.dumps({"XTp": "Main", "b@r": 80, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS"
            live_dir.mkdir(parents=True)
            save7 = _write_pair(live_dir, "save7.hg", old)
            save8 = _write_pair(live_dir, "save8.hg", old)
            original7 = save7.read_bytes()
            original8 = save8.read_bytes()
            _touch(save7, 1_000_000_000)
            _touch(live_dir / "mf_save7.hg", 1_000_000_000)
            _touch(save8, 1_000_000_000)
            _touch(live_dir / "mf_save8.hg", 1_000_000_000)
            copy_dir = folder / "working"
            copy_dir.mkdir()
            copy = _write_pair(copy_dir, "save7.hg", edited)
            _touch(copy, 2_000_000_000)
            _touch(copy_dir / "mf_save7.hg", 2_000_000_000)
            save_state(copy_dir, SyncState(str(save7), "", 0, "", "save7.hg", 4, "st_test"))
            code, message, undo_zip = install_one_save(
                copy, live_dir, 4, folder / "backups", apply=True, live=True, confirm_slot=4
            )
            self.assertEqual(code, 0, message)
            note_installed(copy_dir, save7, copy, undo_zip, folder / "backups")
            put = load_state(copy_dir)
            self.assertIsNotNone(put)
            assert put is not None
            self.assertEqual(Path(put.installed_source).name, "save7.hg")
            partner_zip = Path(put.partner_zips["save8.hg"])
            with zipfile.ZipFile(partner_zip) as handle:
                note = json.loads(handle.read("saveinfo.json"))["note"]
            self.assertNotIn("Working copy backed up", note)
            self.assertIn("put time", note)
            _write_pair(live_dir, "save8.hg", played)
            _touch(live_dir / "save8.hg", time.time_ns() + 20_000_000_000)
            _touch(live_dir / "mf_save8.hg", time.time_ns() + 20_000_000_000)
            reopened = prepare_working_copy(
                live_dir / "save8.hg", copy_dir, slot=4, account="st_test", backup_dir=folder / "backups"
            )
            self.assertEqual(reopened.action, "copy")
            self.assertEqual(reopened.copy_path.name, "save8.hg")
            state = load_state(copy_dir)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(Path(state.source).name, "save8.hg")
            self.assertEqual(installed_live_path(state).name, "save7.hg")
            kind, newer = newer_slot_files(state)
            self.assertEqual(kind, "partner")
            self.assertEqual([path.name for path in newer], ["save8.hg"])
            with self.assertRaises(EditError) as blocked:
                undo_last_install(
                    copy_dir,
                    folder / "backups",
                    apply=True,
                    live=True,
                    confirm_slot=4,
                    allow_changed=False,
                )
            self.assertIn("save8.hg", str(blocked.exception))
            self.assertNotEqual((live_dir / "save8.hg").read_bytes(), original8)
            code, message = undo_last_install(
                copy_dir,
                folder / "backups",
                apply=True,
                live=True,
                confirm_slot=4,
                allow_changed=True,
                allow_partner=True,
            )
            self.assertEqual(code, 0, message)
            self.assertEqual((live_dir / "save7.hg").read_bytes(), original7)
            self.assertEqual((live_dir / "save8.hg").read_bytes(), original8)
            self.assertIn("save8.hg", message)

    def test_opening_backs_up_an_untracked_edit_before_the_game_copy(self):
        from nmsmissions.slots import SyncState, file_sha, save_state

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "live"
            live_dir.mkdir()
            live7 = live_dir / "save7.hg"
            live7.write_bytes(b"game-seven")
            (live_dir / "mf_save7.hg").write_bytes(b"mf-game")
            live8 = live_dir / "save8.hg"
            live8.write_bytes(b"game-eight")
            (live_dir / "mf_save8.hg").write_bytes(b"mf-eight")
            _touch(live7, time.time_ns())
            _touch(live8, 1_000_000_000)
            copy_dir = folder / "working"
            copy_dir.mkdir()
            (copy_dir / "save7.hg").write_bytes(b"edited-seven-not-backed-up")
            (copy_dir / "mf_save7.hg").write_bytes(b"mf-edited")
            (copy_dir / "save8.hg").write_bytes(b"game-eight")
            (copy_dir / "mf_save8.hg").write_bytes(b"mf-eight")
            save_state(
                copy_dir,
                SyncState(
                    str(live8),
                    file_sha(live8),
                    live8.stat().st_mtime_ns,
                    file_sha(copy_dir / "save8.hg"),
                    "save8.hg",
                    4,
                    "st_test",
                ),
            )
            result = prepare_working_copy(
                live7, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups"
            )
            self.assertEqual(result.action, "copy")
            self.assertEqual((copy_dir / "save7.hg").read_bytes(), b"game-seven")
            self.assertIn("backed up", result.message)
            found = False
            for path in (folder / "backups").glob("save7.hg.*.zip"):
                with zipfile.ZipFile(path) as handle:
                    info = json.loads(handle.read("saveinfo.json"))
                    if handle.read("save7.hg") != b"edited-seven-not-backed-up":
                        continue
                    self.assertEqual(handle.read("mf_save7.hg"), b"mf-edited")
                    self.assertIn("before the game file replaced it", str(info.get("note") or ""))
                    found = True
            self.assertTrue(found)

    def test_reopen_after_undo_opens_the_live_file(self):
        from nmsmissions.diffing import choose_save_file
        from nmsmissions.edit import EditRequest, run, undo_last_install
        from nmsmissions.slots import (
            SyncState,
            copy_status,
            group_slots,
            load_state,
            note_installed,
            point_session_at_state,
            save_state,
        )

        old = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        edited = json.dumps({"XTp": "Main", "b@r": 11, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        played = json.dumps({"XTp": "Main", "b@r": 80, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS"
            live_dir.mkdir(parents=True)
            save7 = _write_pair(live_dir, "save7.hg", old)
            save8 = _write_pair(live_dir, "save8.hg", old)
            _touch(save7, 2_000_000_000)
            _touch(live_dir / "mf_save7.hg", 2_000_000_000)
            _touch(save8, 1_000_000_000)
            _touch(live_dir / "mf_save8.hg", 1_000_000_000)
            copy_dir = folder / "working_copies" / "st_test-slot4"
            copy_dir.mkdir(parents=True)
            copy = _write_pair(copy_dir, "save7.hg", edited)
            _touch(copy, 3_000_000_000)
            _touch(copy_dir / "mf_save7.hg", 3_000_000_000)
            save_state(copy_dir, SyncState(str(save7), "", 0, "", "save7.hg", 4, "st_test"))
            code, message, undo_zip = install_one_save(
                copy, live_dir, 4, folder / "backups", apply=True, live=True, confirm_slot=4
            )
            self.assertEqual(code, 0, message)
            note_installed(copy_dir, save7, copy, undo_zip, folder / "backups")
            played_path = _write_pair(live_dir, "save8.hg", played)
            played_bytes = played_path.read_bytes()
            _touch(played_path, time.time_ns() + 20_000_000_000)
            _touch(live_dir / "mf_save8.hg", time.time_ns() + 20_000_000_000)
            reopened = prepare_working_copy(
                live_dir / "save8.hg", copy_dir, slot=4, account="st_test", backup_dir=folder / "backups"
            )
            self.assertEqual(reopened.action, "copy")
            self.assertEqual(reopened.copy_path.name, "save8.hg")
            self.assertEqual([path.name for path in copy_dir.glob("save*.hg")], ["save8.hg"])
            code, message = undo_last_install(
                copy_dir,
                folder / "backups",
                apply=True,
                live=True,
                confirm_slot=4,
                allow_changed=True,
                allow_partner=True,
            )
            self.assertEqual(code, 0, message)
            found_played = False
            for path in (folder / "backups").glob("*.zip"):
                with zipfile.ZipFile(path) as handle:
                    info = json.loads(handle.read("saveinfo.json"))
                if "replaced with the game file" not in str(info.get("note") or ""):
                    continue
                with zipfile.ZipFile(path) as handle:
                    if handle.read(info["save"]) == played_bytes:
                        found_played = True
            self.assertTrue(found_played)
            live_choice = {item.slot: item for item in group_slots(live_dir)}[4].newer
            state = load_state(copy_dir)
            self.assertIsNotNone(state)
            assert state is not None
            tracked = copy_dir / state.save_name
            self.assertEqual([path.name for path in copy_dir.glob("save*.hg")], [live_choice.name])
            self.assertEqual(tracked.read_bytes(), live_choice.read_bytes())
            self.assertNotEqual(tracked.read_bytes(), played_bytes)
            self.assertEqual(copy_status(tracked, live_choice), "matches game")
            session = type("Session", (), {})()
            session.copy_dir = copy_dir
            session.save = reopened.copy_path
            session.origin = reopened.origin
            seen = {}

            def hook(path):
                seen["path"] = path

            session.on_save_path = hook
            point_session_at_state(session)
            self.assertEqual(session.save.read_bytes(), live_choice.read_bytes())
            self.assertEqual(copy_status(session.save, session.origin), "matches game")
            self.assertEqual(Path(seen["path"]).resolve(), session.save.resolve())
            stale_name = "save8.hg" if live_choice.name.lower() != "save8.hg" else "save7.hg"
            planted = _write_pair(copy_dir, stale_name, played)
            _touch(planted, time.time_ns() + 50_000_000_000)
            stuck, stuck_note = choose_save_file(tracked)
            self.assertEqual(stuck.resolve(), tracked.resolve())
            self.assertIsNone(stuck_note)
            again = prepare_working_copy(
                live_choice, copy_dir, slot=4, account="st_test", backup_dir=folder / "backups"
            )
            chosen, note = choose_save_file(again.copy_path)
            self.assertIsNone(note)
            self.assertEqual(chosen.resolve(), again.copy_path.resolve())
            self.assertEqual(chosen.name, live_choice.name)
            self.assertEqual(chosen.read_bytes(), live_choice.read_bytes())
            self.assertNotEqual(chosen.read_bytes(), played_bytes)
            self.assertEqual(copy_status(chosen, live_choice), "matches game")
            self.assertEqual([path.name for path in copy_dir.glob("save*.hg")], [live_choice.name])
            self.assertFalse(planted.exists())
            self.assertIn("matches game", copy_status(chosen, Path(state.source)))

    def test_restore_into_game_refreshes_the_working_copy(self):
        from unittest.mock import patch

        from nmsmissions.diffing import choose_save_file
        from nmsmissions.edit import EditRequest, run
        from nmsmissions.slots import SyncState, backup_edited_copy, copy_status, group_slots, save_state

        old = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        edited = json.dumps({"XTp": "Main", "b@r": 11, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live_dir = folder / "HelloGames" / "NMS"
            live_dir.mkdir(parents=True)
            save7 = _write_pair(live_dir, "save7.hg", old)
            _write_pair(live_dir, "save8.hg", old)
            _touch(save7, 5_000_000_000)
            _touch(live_dir / "save8.hg", 1_000_000_000)
            copies = folder / "working_copies"
            copy_dir = copies / "st_test-slot4"
            copy_dir.mkdir(parents=True)
            stale = _write_pair(copy_dir, "save8.hg", edited)
            edited_bytes = stale.read_bytes()
            _write_pair(copy_dir, "save7.hg", old)
            _touch(stale, time.time_ns())
            save_state(copy_dir, SyncState(str(save7), "", 0, "stale", "save8.hg", 4, "NMS"))
            zip_path = backup_edited_copy(save7, 4, folder / "backups")
            with patch("nmsmissions.slots.working_root", return_value=copies):
                code = run(
                    EditRequest(
                        action="restore",
                        restore_zip=zip_path,
                        restore_to=live_dir,
                        apply=True,
                        live=True,
                        confirm_slot=4,
                        backup_dir=folder / "backups",
                        quiet=True,
                    )
                )
            self.assertEqual(code, 0)
            live_choice = {item.slot: item for item in group_slots(live_dir)}[4].newer
            names = sorted(path.name for path in copy_dir.glob("save*.hg"))
            self.assertEqual(names, [live_choice.name])
            opened = copy_dir / live_choice.name
            self.assertEqual(opened.read_bytes(), live_choice.read_bytes())
            self.assertNotEqual(opened.read_bytes(), edited_bytes)
            self.assertEqual(copy_status(opened, live_choice), "matches game")
            chosen, note = choose_save_file(opened)
            self.assertEqual(chosen.resolve(), opened.resolve())
            self.assertIsNone(note)

    def test_cross_slot_restore_writes_the_open_file(self):
        from nmsmissions.edit import EditRequest, run
        from nmsmissions.manifest import read_manifest
        from nmsmissions.slots import backup_edited_copy, restore_slot_warning

        slot1_body = json.dumps({"XTp": "Main", "b@r": 1, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        slot4_body = json.dumps({"XTp": "Main", "b@r": 4, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            slot1 = folder / "slot1"
            slot1.mkdir()
            open_file = _write_pair(slot1, "save.hg", slot1_body)
            before = open_file.read_bytes()
            slot4 = folder / "slot4"
            slot4.mkdir()
            backed = _write_pair(slot4, "save7.hg", slot4_body)
            zip_path = backup_edited_copy(backed, 4, folder / "backups")
            warning = restore_slot_warning(4, 1, "2026-10-01 15:30:45", "save7.hg")
            self.assertIsNotNone(warning)
            assert warning is not None
            self.assertIn("2026-10-01 15:30:45", warning)
            self.assertIn("save7.hg", warning)
            self.assertIn("slot 4", warning)
            self.assertIn("slot 1", warning)
            code = run(
                EditRequest(
                    action="restore",
                    restore_zip=zip_path,
                    restore_to=slot1,
                    restore_name="save.hg",
                    apply=True,
                    backup_dir=folder / "backups",
                    quiet=True,
                )
            )
            self.assertEqual(code, 0)
            self.assertFalse((slot1 / "save7.hg").exists())
            self.assertNotEqual(open_file.read_bytes(), before)
            self.assertEqual(open_file.read_bytes(), backed.read_bytes())
            read_manifest(slot1 / "mf_save.hg")

    def test_editor_bar_builds_after_opening_a_slot(self):
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import ChainView, OtherView, StepView
        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import FINISH_LABEL, UNLOCK_LABEL, launch

        body = json.dumps(
            {
                "XTp": "Main",
                "b@r": 1,
                "vLc": {"6f=": {"dwb": [{"p0c": "^TEMPLATE", "tW6": -1, "@EL": 0}], "yq:": 39, ";R7": "^"}},
            }
        )
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live = folder / "HelloGames" / "NMS"
            live.mkdir(parents=True)
            save7 = _write_pair(live, "save7.hg", body)
            _write_pair(live, "save8.hg", body)
            _touch(save7, 5_000_000_000)
            _touch(live / "save8.hg", 1_000_000_000)
            prepared = prepare_working_copy(
                save7, folder / "copies" / "st_test-slot4", slot=4, account="st_test", backup_dir=folder / "backups"
            )
            self.assertEqual(prepared.copy_path.name, "save7.hg")
            session = EditorSession(prepared.copy_path, backup_dir=folder / "backups")
            session.origin = prepared.origin
            session.copy_dir = prepared.copy_dir
            session.slot = 4
            session.account = "st_test"
            step = StepView(
                "^ACT1_STEP1",
                "Awakenings",
                1,
                7,
                "in_progress",
                "in progress",
                False,
                False,
                True,
                None,
                None,
                None,
                None,
            )
            chain = ChainView(
                "side",
                "A side quest",
                "side",
                0,
                None,
                None,
                [],
                [],
                [],
                False,
                steps=[step],
            )
            roots = [chain]
            other = OtherView([])
            subtitle = str(prepared.copy_path)

            box = {"empty": False}

            def reload_cb():
                if box["empty"]:
                    return [], OtherView([]), subtitle
                return roots, other, subtitle

            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            try:
                launch("Missions", subtitle, roots, other, window=window, session=session, reload_cb=reload_cb)
                widgets = list(_walk(window))
                unlock = _one(widgets, ttk.Button, UNLOCK_LABEL)
                finish = _one(widgets, ttk.Button, FINISH_LABEL)
                self.assertEqual(str(unlock.cget("text")), UNLOCK_LABEL)
                self.assertIn("disabled", unlock.state())
                cover = _one(widgets, tk.Label, UNLOCK_LABEL)
                self.assertTrue(cover.place_info())
                self.assertIn("Unlock only", str(cover.cget("text")))
                style = ttk.Style(window)
                self.assertEqual(str(cover.cget("fg")), style.lookup("TButton", "foreground", ("disabled",)))
                self.assertEqual(str(cover.cget("bg")), style.lookup("TButton", "background", ("disabled",)))
                self.assertNotEqual(str(cover.cget("bg")), "#e4e4e4")
                tree = _one_type(widgets, ttk.Treeview)
                tree.selection_set("chain:side")
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                self.assertEqual(str(finish.cget("text")), "Finish whole quest line")
                self.assertEqual(str(unlock.cget("text")), "Unlock next step")
                reset = _one(widgets, ttk.Button, "Reset whole quest line")
                self.assertEqual(str(reset.cget("text")), "Reset whole quest line")
                up_to = _one(widgets, ttk.Button, "Finish up to this step")
                self.assertIn("disabled", up_to.state())
                mission = "chain:side/^ACT1_STEP1"
                tree.selection_set(mission)
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                self.assertEqual(tree.selection(), (mission,))
                self.assertEqual(str(finish.cget("text")), FINISH_LABEL)
                self.assertEqual(str(unlock.cget("text")), UNLOCK_LABEL)
                self.assertNotIn("disabled", finish.state())
                self.assertNotIn("disabled", up_to.state())
                window._reload_rows()
                from nmsmissions.gui import finish_reload

                finish_reload(window)
                self.assertEqual(tree.selection(), (mission,))
                self.assertNotIn("disabled", finish.state())
                box["empty"] = True
                window._reload_rows()
                finish_reload(window)
                self.assertEqual(tree.selection(), ())
                self.assertIn("disabled", finish.state())
                self.assertIn("disabled", unlock.state())
                self.assertTrue(cover.place_info())
                self.assertIn("Unlock only", str(cover.cget("text")))
            finally:
                window.destroy()


class FinishedLineTests(TkCleanup, unittest.TestCase):
    def test_complete_line_disables_finish_and_unlock(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import STATUS_DONE, STATUS_REACHED, ChainView, OtherView, StepView
        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import FINISHED_LINE, CHAIN_FINISH_LABEL, launch

        body = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", body)
            session = EditorSession(path, backup_dir=folder / "backups")
            intro = StepView(
                "^ROBOT_INTRO", "Intro", 6, 6, STATUS_DONE, "done", False, False, False, None, None, None, None
            )
            hope = StepView(
                "^ROBOM_HOPE",
                "Hope",
                0,
                0,
                STATUS_REACHED,
                "reached/unknown",
                False,
                True,
                False,
                None,
                None,
                None,
                None,
            )
            chain = ChainView(
                "twr",
                "They Who Returned",
                "story",
                0,
                None,
                None,
                [],
                [],
                [],
                True,
                done_count=14,
                required_count=14,
                state_label="complete, 14/14 done",
                steps=[intro, hope],
                next_step=None,
            )
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
                tree = _one_type(widgets, ttk.Treeview)
                tree.selection_set("chain:twr")
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                finish = _one(widgets, ttk.Button, CHAIN_FINISH_LABEL)
                self.assertIn("disabled", finish.state())
                self.assertEqual(finish.nms_tip["text"], FINISHED_LINE)
                unlock = _one(widgets, ttk.Button, "Unlock next step")
                self.assertIn("disabled", unlock.state())
                cover = _one(widgets, tk.Label, "Unlock next step")
                self.assertTrue(cover.place_info())
                self.assertEqual(cover.nms_tip["text"], FINISHED_LINE)
            finally:
                window.destroy()

    def test_complete_line_with_a_missing_unlock_keeps_finish_on(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        from unittest.mock import patch

        from nmsmissions.chains import STATUS_DONE, ChainView, OtherView, StepView, load_chains
        from nmsmissions.edit import EditRequest, EditorSession
        from nmsmissions.gui import CHAIN_FINISH_LABEL, launch
        from tests.test_edit import _mission_xml, _row, _save_body, _text, _write_xml

        ism = next(chain for chain in load_chains() if chain.chain_id == "ism")
        ids = [step.mission_id for step in ism.steps]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([(mission_id, [(39, 9)]) for mission_id in ids]))
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row("^TEMPLATE", -1)]
            for mission_id in ids:
                progress = -1 if mission_id == "^CORE_LORE" else 9
                player["dwb"].insert(0, _row(mission_id, progress))
            path = _write_pair(folder, "save.hg", _text(body))
            session = EditorSession(path, backup_dir=folder / "backups")
            session.game_files = tables
            session._cached_tables(EditRequest(action="finish", game_files=tables))
            session.cached_snapshot()
            steps = [
                StepView(
                    mission_id,
                    mission_id,
                    9,
                    9,
                    STATUS_DONE,
                    "done",
                    False,
                    mission_id == "^CORE_LORE",
                    False,
                    None,
                    None,
                    None,
                    None,
                )
                for mission_id in ids
            ]
            chain = ChainView(
                "ism",
                "In Stellar Multitudes",
                "story",
                0,
                None,
                None,
                [],
                [],
                [],
                True,
                done_count=5,
                required_count=5,
                state_label="complete, 5/5 done",
                steps=steps,
                next_step=None,
            )
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
                tree = _one_type(widgets, ttk.Treeview)
                tree.selection_set("chain:ism")
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                finish = _one(widgets, ttk.Button, CHAIN_FINISH_LABEL)
                self.assertNotIn("disabled", finish.state())
                self.assertIn("missing unlocks", finish.nms_tip["text"])
                with patch("tkinter.messagebox.askokcancel", return_value=True) as ask:
                    finish.invoke()
                ask.assert_called_once()
                self.assertIn("missing unlocks", ask.call_args.args[1])
                self.assertIn("steps stay as they are", ask.call_args.args[1])
                preview = next(child for child in window.winfo_children() if isinstance(child, tk.Toplevel))
                shown = preview.winfo_children()[0].get("1.0", "end")
                self.assertIn("Apply the missing unlocks", shown)
                self.assertIn("Set purple systems discovered.", shown)
                self.assertIn("^HDRIVEBOOST4", shown)
                self.assertNotIn("^CORE_LORE", shown)
                preview.destroy()
            finally:
                window.destroy()

    def test_unknown_step_without_tables_disables_finish(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import STATUS_NOT_STARTED, STATUS_PROGRESS, ChainView, OtherView, StepView
        from nmsmissions.edit import READ_GAME_FILES_TIP, EditorSession
        from nmsmissions.gui import FINISH_LABEL, launch

        body = json.dumps({"XTp": "Main", "b@r": 10, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", body)
            session = EditorSession(path, backup_dir=folder / "backups")
            session.cached_snapshot()
            unknown = StepView(
                "^ZZZNOTREAL",
                "^ZZZNOTREAL",
                None,
                0,
                STATUS_NOT_STARTED,
                "not started",
                True,
                False,
                False,
                None,
                None,
                None,
                None,
            )
            known = StepView(
                "^ACT1_STEP1",
                "Awakenings",
                1,
                7,
                STATUS_PROGRESS,
                "in progress",
                True,
                False,
                False,
                None,
                None,
                None,
                None,
            )
            missing = ChainView(
                "side",
                "Side",
                "side",
                0,
                None,
                None,
                [],
                [],
                [],
                False,
                done_count=0,
                required_count=1,
                state_label="not started, 0/1 done",
                steps=[unknown],
                next_step=unknown,
            )
            artemis = ChainView(
                "artemis",
                "Artemis Path",
                "story",
                0,
                None,
                None,
                [],
                [],
                [],
                False,
                done_count=0,
                required_count=1,
                state_label="in progress, 0/1 done",
                steps=[known],
                next_step=known,
            )
            window = tk.Tk()
            window.withdraw()
            window.mainloop = lambda: None
            try:
                launch(
                    "Missions",
                    str(path),
                    [missing, artemis],
                    OtherView([]),
                    window=window,
                    session=session,
                    reload_cb=lambda: ([missing, artemis], OtherView([]), str(path)),
                )
                widgets = list(_walk(window))
                tree = _one_type(widgets, ttk.Treeview)
                tree.selection_set("chain:side")
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                finish = _one(widgets, ttk.Button, "Finish whole quest line")
                self.assertIn("disabled", finish.state())
                self.assertEqual(finish.nms_tip["text"], READ_GAME_FILES_TIP)
                tree.selection_set("chain:artemis/^ACT1_STEP1")
                tree.event_generate("<<TreeviewSelect>>")
                window.update_idletasks()
                step_finish = _one(widgets, ttk.Button, FINISH_LABEL)
                self.assertNotIn("disabled", step_finish.state())
                self.assertNotIn("--game-files", step_finish.nms_tip["text"])
            finally:
                window.destroy()

    def test_undo_reloads_the_list_and_drops_the_cache(self) -> None:
        import tkinter as tk
        from unittest.mock import patch

        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import _undo_dialog
        from nmsmissions.slots import SyncState

        body = json.dumps({"XTp": "Main", "b@r": 1, "vLc": {"6f=": {"dwb": [{"p0c": "^OPEN", "tW6": 9}], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", body)
            session = EditorSession(path, backup_dir=folder / "backups")
            session.copy_dir = folder / "copy"
            session.copy_dir.mkdir()
            session.slot = 1
            session.cached_snapshot()
            self.assertIsNotNone(session._snap)
            seen = {"n": 0}

            def reload() -> None:
                seen["n"] += 1

            window = tk.Tk()
            window.withdraw()
            banner = tk.Label(window, text="copy")
            banner.mode_line = "copy"
            state = SyncState(
                source=str(path),
                source_sha256="abc",
                source_mtime_ns=1,
                synced_sha256="abc",
                save_name="save.hg",
                slot=1,
                account="st_test",
                undo_zip=str(folder / "undo.zip"),
                installed_source=str(folder / "missing.hg"),
            )
            try:
                with patch("nmsmissions.slots.load_state", return_value=state), patch(
                    "nmsmissions.edit.undo_last_install", return_value=(0, "Put the game file back.")
                ), patch("tkinter.messagebox.askokcancel", return_value=True), patch(
                    "tkinter.messagebox.showinfo"
                ), patch("tkinter.messagebox.showerror") as error:
                    _undo_dialog(window, session, banner, lambda: None, reload)
                    deadline = time.perf_counter() + 2
                    while time.perf_counter() < deadline and seen["n"] == 0:
                        window.update()
                        time.sleep(0.02)
                error.assert_not_called()
            finally:
                window.destroy()
            self.assertEqual(seen["n"], 1)
            self.assertIsNone(session._snap)

    def test_invalidate_snapshot_reads_bytes_restored_with_the_old_mtime(self) -> None:
        from nmsmissions.edit import EditorSession

        first = json.dumps({"XTp": "Main", "b@r": 1, "vLc": {"6f=": {"dwb": [{"p0c": "^OPEN", "tW6": 3}], "yq:": 39}}})
        second = json.dumps({"XTp": "Main", "b@r": 1, "vLc": {"6f=": {"dwb": [{"p0c": "^OPEN", "tW6": 4}], "yq:": 39}}})
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save.hg", first)
            session = EditorSession(path)
            snap = session.cached_snapshot()
            self.assertEqual(snap.player["dwb"][0]["tW6"], 3)
            stamp = path.stat().st_mtime_ns
            _write_pair(folder, "save.hg", second)
            os.utime(path, ns=(stamp, stamp))
            session._snap_key = (str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns)
            self.assertEqual(session.cached_snapshot().player["dwb"][0]["tW6"], 3)
            session.invalidate_snapshot()
            self.assertEqual(session.cached_snapshot().player["dwb"][0]["tW6"], 4)

    def test_write_drops_the_cached_parse(self) -> None:
        from nmsmissions.edit import EditorSession
        from tests.test_edit import _save_body, _text, _write_pair as write_save

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = write_save(folder, "save.hg", _text(_save_body()))
            session = EditorSession(path, backup_dir=folder / "backups")
            session.plan_for("unlock", "^OPEN", None, False)
            session.cached_snapshot()
            self.assertIsNotNone(session._snap)
            code, message = session.apply_last()
            self.assertEqual(code, 0, message)
            self.assertIsNone(session._snap)


class PartnerPutTests(TkCleanup, unittest.TestCase):
    def _pair(self, folder: Path, name: str, stamp: int, play: int = 10) -> Path:
        body = json.dumps({"XTp": "Main", "b@r": play, "vLc": {"6f=": {"dwb": [], "yq:": 39}}})
        path = _write_pair(folder, name, body)
        os.utime(path, ns=(stamp, stamp))
        os.utime(folder / f"mf_{name}", ns=(stamp, stamp))
        return path

    def test_put_refuses_when_the_other_file_is_newer(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            live = folder / "live"
            src.mkdir()
            live.mkdir()
            old = 1_700_000_000_000_000_000
            new = old + 80_000_000_000
            copy = self._pair(src, "save7.hg", old, play=11)
            self._pair(live, "save7.hg", old, play=10)
            self._pair(live, "save8.hg", new, play=10)
            before = (live / "save7.hg").read_bytes()
            note = partner_load_problem(copy, live, source_mtime_ns=old)
            self.assertIn("The game will load save8.hg", note or "")
            with self.assertRaises(EditError) as raised:
                install_one_save(
                    copy,
                    live,
                    4,
                    folder / "backups",
                    apply=True,
                    live=False,
                    confirm_slot=None,
                    source_mtime_ns=old,
                )
            self.assertIn("The game will load save8.hg", str(raised.exception))
            self.assertEqual((live / "save7.hg").read_bytes(), before)
            code, message, _undo = install_one_save(
                copy,
                live,
                4,
                folder / "backups",
                apply=True,
                live=False,
                confirm_slot=None,
                allow_partner=True,
                source_mtime_ns=old,
            )
            self.assertEqual(code, 0)
            self.assertIn("Slot 4 now has save7.hg", message)
            self.assertIn("The game will load save8.hg", message)

    def test_put_message_follows_the_file_just_written(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            live = folder / "live"
            src.mkdir()
            live.mkdir()
            old = 1_700_000_000_000_000_000
            mid = old + 40_000_000_000
            new = old + 80_000_000_000
            copy = self._pair(src, "save7.hg", new, play=12)
            self._pair(live, "save7.hg", old, play=10)
            self._pair(live, "save8.hg", mid, play=11)
            note = partner_load_problem(copy, live, source_mtime_ns=old)
            self.assertIn("The game will load save8.hg", note or "")
            with self.assertRaises(EditError):
                install_one_save(
                    copy,
                    live,
                    4,
                    folder / "backups",
                    apply=True,
                    live=False,
                    confirm_slot=None,
                    source_mtime_ns=old,
                )
            code, message, _undo = install_one_save(
                copy,
                live,
                4,
                folder / "backups",
                apply=True,
                live=False,
                confirm_slot=None,
                allow_partner=True,
                source_mtime_ns=old,
            )
            self.assertEqual(code, 0)
            self.assertEqual(message, "Slot 4 now has save7.hg.")
            self.assertGreater((live / "save7.hg").stat().st_mtime_ns, (live / "save8.hg").stat().st_mtime_ns)

    def test_partner_newer_than_the_copy_source_is_named(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            live = folder / "live"
            src.mkdir()
            live.mkdir()
            old = 1_700_000_000_000_000_000
            mid = old + 40_000_000_000
            new = old + 80_000_000_000
            copy = self._pair(src, "save7.hg", new, play=10)
            self._pair(live, "save8.hg", mid, play=10)
            note = partner_load_problem(copy, live, source_mtime_ns=old)
            self.assertIn("newer than this copy's source", note or "")
            self.assertIn("The game will load save8.hg", note or "")

    def test_status_stops_saying_matches_game_when_the_partner_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            origin = folder / "save7.hg"
            partner = folder / "save8.hg"
            copy_dir = folder / "copy"
            copy_dir.mkdir()
            copy = copy_dir / "save7.hg"
            origin.write_bytes(b"same-bytes")
            copy.write_bytes(b"same-bytes")
            partner.write_bytes(b"other")
            old = 1_700_000_000_000_000_000
            os.utime(partner, ns=(old, old))
            os.utime(origin, ns=(old + 5_000_000_000, old + 5_000_000_000))
            self.assertEqual(slot_status_line(copy, origin), "matches game")
            os.utime(partner, ns=(old + 20_000_000_000, old + 20_000_000_000))
            text = slot_status_line(copy, origin)
            self.assertEqual(text, "The game will load save8.hg.")
            self.assertNotIn("matches game", text)

    def test_put_dialog_names_the_file_the_game_will_load(self) -> None:
        import tkinter as tk
        from unittest.mock import patch

        from nmsmissions.edit import EditorSession
        from nmsmissions.gui import _install_dialog

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            live = folder / "live"
            src.mkdir()
            live.mkdir()
            old = 1_700_000_000_000_000_000
            new = old + 80_000_000_000
            copy = self._pair(src, "save7.hg", old, play=12)
            self._pair(live, "save7.hg", old, play=10)
            self._pair(live, "save8.hg", new, play=10)
            before = (live / "save7.hg").read_bytes()
            session = EditorSession(copy, backup_dir=folder / "backups")
            session.origin = live / "save7.hg"
            session.slot = 4
            window = tk.Tk()
            window.withdraw()
            banner = tk.Label(window, text="copy")
            asked = {}

            def ask(_title, message, parent=None):
                asked["message"] = message
                return False

            try:
                with patch("tkinter.messagebox.askokcancel", ask), patch(
                    "tkinter.messagebox.showinfo"
                ), patch("tkinter.messagebox.showerror"):
                    _install_dialog(window, session, banner, lambda: None)
            finally:
                window.destroy()
            self.assertIn("The game will load save8.hg", asked.get("message", ""))
            self.assertEqual((live / "save7.hg").read_bytes(), before)

    def test_stale_not_running_cache_refuses_the_put(self) -> None:
        import threading
        import tkinter as tk
        from unittest.mock import patch

        from nmsmissions.edit import EditorSession, note_nms_running, peek_nms_running
        from nmsmissions.gui import _install_dialog

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src = folder / "src"
            live = folder / "live"
            src.mkdir()
            live.mkdir()
            old = 1_700_000_000_000_000_000
            new = old + 80_000_000_000
            copy = self._pair(src, "save7.hg", old, play=12)
            self._pair(live, "save7.hg", old, play=10)
            self._pair(live, "save8.hg", new, play=10)
            before = (live / "save7.hg").read_bytes()
            session = EditorSession(copy, backup_dir=folder / "backups")
            session.origin = live / "save7.hg"
            session.slot = 4
            note_nms_running(False)
            self.assertIs(peek_nms_running(), False)
            window = tk.Tk()
            window.withdraw()
            banner = tk.Label(window, text="copy")
            seen = {"threads": []}

            def fresh() -> bool:
                seen["threads"].append(threading.current_thread().name)
                return True

            errors: list[str] = []

            def showerror(_title, message, parent=None) -> None:
                errors.append(message)

            try:
                with patch("nmsmissions.edit.nms_is_running", fresh), patch(
                    "tkinter.messagebox.askokcancel", return_value=True
                ), patch("tkinter.messagebox.showinfo"), patch("tkinter.messagebox.showerror", showerror):
                    _install_dialog(window, session, banner, lambda: None)
                    self.assertIn("Checking the game is closed", banner.cget("text"))
                    deadline = time.perf_counter() + 2
                    while time.perf_counter() < deadline and not errors:
                        window.update()
                        time.sleep(0.02)
            finally:
                window.destroy()
            self.assertTrue(seen["threads"])
            self.assertNotIn("MainThread", seen["threads"])
            self.assertIn("running", errors[0].lower())
            self.assertEqual((live / "save7.hg").read_bytes(), before)


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _one(widgets, kind, text: str):
    found = [widget for widget in widgets if isinstance(widget, kind) and str(widget.cget("text")) == text]
    if len(found) != 1:
        raise AssertionError(f"expected one {kind} {text!r}, found {len(found)}")
    return found[0]


def _one_type(widgets, kind):
    found = [widget for widget in widgets if isinstance(widget, kind)]
    if len(found) != 1:
        raise AssertionError(f"expected one {kind}, found {len(found)}")
    return found[0]

"""Synthetic saves only. No Hello Games profile is read or written."""

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
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nmsmissions.chunks import pack_chunks, unpack_save
from nmsmissions.cli import main
from nmsmissions.edit import (
    PURPLE_MISSIONS,
    EditError,
    EditRequest,
    EditorSession,
    build_plan,
    collect_checks,
    commit,
    Change,
    Plan,
    format_plan,
    prune_backups,
    run,
    take_snapshot,
    unlock_block_reason,
)
from nmsmissions.gui import FINISH_LABEL, UNLOCK_LABEL
from nmsmissions.guard import is_live_nms_save
from nmsmissions.gamedata import final_progress, load_tables
from nmsmissions.manifest import (
    FORMAT_2004,
    MAGIC,
    archive_number,
    encrypt_manifest,
    key_slot_for,
    read_manifest,
    size_bytes_differ_only_at_sizes,
)
from nmsmissions.rewards import stack_size
from nmsmissions.spanpatch import locate


def _row(mission: str, progress: int, seed: int = 0, data: int = 0, stat: int = 0) -> dict:
    return {
        "p0c": mission,
        "tW6": progress,
        "@EL": seed,
        "8?J": data,
        "Kex": stat,
        "eZ7": [{"kept": True}],
        "extra": "keep-me",
    }


def _slot(item: str, amount: int, maximum: int, x_pos: int, y_pos: int, kind: str = "Product") -> dict:
    return {
        "Vn8": {"elv": kind},
        "b2n": item,
        "1o9": amount,
        "F9q": maximum,
        "eVk": 0.0,
        "b76": True,
        "5tH": False,
        "3ZH": {">Qh": x_pos, "XJ>": y_pos},
    }


def _valid(*coords: tuple[int, int]) -> list[dict]:
    return [{"3ZH": {">Qh": x_pos, "XJ>": y_pos}} for x_pos, y_pos in coords]


def _inventory(slots: list[dict], coords: tuple[tuple[int, int], ...]) -> dict:
    return {":No": slots, "hl?": _valid(*coords)}


def _save_body(**overrides) -> dict:
    body = {
        "floatCheck": "FLOAT",
        "note": "NOTE",
        "XTp": "Main",
        "b@r": 10,
        "vLc": {
            "6f=": {
                "yq:": 39,
                "Kg6": False,
                "cPt": False,
                "wGS": 100,
                "7QL": 0,
                "kN;": 0,
                ";R7": "^OTHER",
                "Mg<": "^",
                "4kj": ["^OTHER"],
                "eZ<": [],
                "24<": [],
                "dwb": [_row("^OPEN", 3, data=2, stat=4), _row("^TEMPLATE", -1)],
                ";l5": _inventory([], ()),
                "aBE": 0,
                "@Cs": [{"NTx": "ship", "93M": "ship.mbin", ";l5": _inventory([], ())}],
                "8ZP": _inventory([], ()),
            }
        },
    }
    player = body["vLc"]["6f="]
    for key, value in overrides.items():
        if key in player:
            player[key] = value
        else:
            body[key] = value
    return body


def _text(body: dict, float_text: str | None = None, note: str | None = None) -> str:
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if float_text is not None:
        text = text.replace('"FLOAT"', float_text)
    else:
        text = text.replace('"FLOAT"', "0")
    if note is not None:
        text = text.replace("NOTE", note)
    return text


def _write_pair(
    folder: Path,
    name: str,
    text: str,
    *,
    decompressed: int | None = None,
    compressed: int | None = None,
    play_time: int = 0,
) -> Path:
    raw = text.encode("utf-8", "surrogateescape") + b"\x00"
    packed = pack_chunks(raw)
    path = folder / name
    path.write_bytes(packed)
    words = [0] * 108
    words[0] = MAGIC
    words[1] = FORMAT_2004
    words[14] = len(raw) if decompressed is None else decompressed
    words[15] = len(packed) if compressed is None else compressed
    words[19] = play_time
    plain = struct.pack("<108I", *words)
    manifest = path.with_name("mf_" + path.name)
    manifest.write_bytes(encrypt_manifest(plain, key_slot_for(archive_number(path))))
    return path


def _mission_xml(missions: list[tuple[str, list[tuple[int, int]]]], extra: str = "") -> str:
    blocks = []
    for mission_id, finals in missions:
        rows = []
        for version, progress in finals:
            rows.append(
                "<Property value='GcMissionVersionProgress.xml'>"
                f"<Property name='Version' value='{version}'/>"
                f"<Property name='Progress' value='{progress}'/>"
                "</Property>"
            )
        blocks.append(
            "<Property name='GcGenericMissionSequence'>"
            f"<Property name='MissionID' value='{mission_id}'/>"
            f"<Property name='FinalStageVersions'>{''.join(rows)}</Property>"
            f"{extra if mission_id == missions[0][0] else ''}"
            "</Property>"
        )
    return "<Data>" + "".join(blocks) + "</Data>"


def _write_xml(folder: Path, text: str) -> None:
    (folder / "missions.mxml").write_text(text, encoding="utf-8")


def _run(request: EditRequest, quiet: bool = True) -> int:
    request.quiet = quiet
    return run(request)


class EditTests(unittest.TestCase):
    def test_finals_follow_the_save_version(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_xml(
                root,
                _mission_xml([("^PURPM2", [(34, 24), (39, 23)]), ("^PURPM3", [(34, 64), (39, 72)])]),
            )
            tables = load_tables(root)
            progress, note = final_progress(tables.missions["^PURPM2"], 34)
            self.assertEqual(progress, 24)
            self.assertIsNone(note)
            progress, note = final_progress(tables.missions["^PURPM2"], 39)
            self.assertEqual(progress, 23)
            self.assertIsNone(note)
            progress, note = final_progress(tables.missions["^PURPM3"], 40)
            self.assertEqual(progress, 72)
            self.assertIn("newest row", note or "")
            stage = tables.missions["^PURPM2"]
            self.assertEqual(stage.finals, [(34, 24), (39, 23)])

    def test_stack_multiplier_zero_counts_as_one(self):
        limits = {
            "product": {"exosuit": 10, "ship": 10, "freighter": 20},
            "substance": {"exosuit": 9999, "ship": 9999, "freighter": 9999},
            "product_limit": 999999999,
            "substance_limit": 9999,
        }
        self.assertEqual(stack_size("Product", "exosuit", 0, limits), stack_size("Product", "exosuit", 1, limits))
        self.assertEqual(stack_size("Product", "exosuit", 0, limits), 10)
        self.assertEqual(stack_size("Product", "ship", 2, limits), 20)
        self.assertEqual(stack_size("Product", "freighter", 1, limits), 20)
        self.assertEqual(stack_size("Substance", "exosuit", 0, limits), 9999)

    def test_dry_run_does_not_write(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            path = _write_pair(folder, "save7.hg", _text(_save_body()))
            before = path.read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    game_files=tables,
                    backup_dir=folder / "backups",
                    rewards=False,
                )
            )
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((folder / "backups").exists())

    def test_finish_versions_units_and_restore(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'>"
                "<Property value='GcMissionSequenceShowDialog.xml'>"
                "<Property name='Progress' value='2'/>"
                "<Property name='Reward' value='R_TALK'/>"
                "</Property>"
                "<Property name='Stage'>"
                "<Property name='Progress' value='8'/>"
                "<Property name='Reward' value='R_MONEY'/>"
                "</Property>"
                "</Property>"
                "<Property name='Rewards'>"
                "<Property>"
                "<Property name='Id' value='R_TALK'/>"
                "<Property name='Reward' value='GcRewardMoney.xml'>"
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='50'/>"
                "</Property>"
                "</Property>"
                "<Property>"
                "<Property name='Id' value='R_MONEY'/>"
                "<Property name='RewardChoice' value='GiveAll'/>"
                "<Property name='Reward' value='GcRewardMoney.xml'>"
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='30000000'/>"
                "<Property name='AmountMax' value='30000000'/>"
                "</Property>"
                "</Property>"
                "</Property>"
                "<Property value='GcMissionConditionMissionCompleted.xml'>"
                "<Property name='MissionID' value='^NEED_ME'/>"
                "</Property>"
            )
            _write_xml(tables, _mission_xml([("^OPEN", [(34, 8), (39, 9)])], extra))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 1, data=2, stat=4), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = -27648696
            body["vLc"]["6f="]["yq:"] = 34
            text = _text(body, float_text="0.30000001192092898", note="\udcff")
            path = _write_pair(folder, "save7.hg", text, play_time=4321)
            before = path.read_bytes()
            before_mf = path.with_name("mf_save7.hg").read_bytes()
            before_mtime = path.stat().st_mtime_ns
            before_mf_mtime = path.with_name("mf_save7.hg").stat().st_mtime_ns
            old_plain = read_manifest(path.with_name("mf_save7.hg")).plain
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0, "finish should write")
            raw = unpack_save(path.read_bytes()).raw
            self.assertIn(b"0.30000001192092898", raw)
            self.assertIn("\udcff".encode("utf-8", "surrogateescape"), raw)
            parsed = json.loads(raw[:-1].decode("utf-8", "surrogateescape").encode("utf-8", "backslashreplace").decode("utf-8"))
            player = parsed["vLc"]["6f="]
            self.assertEqual(player["dwb"][0]["tW6"], 8)
            self.assertEqual(player["dwb"][0]["8?J"], 0)
            self.assertEqual(player["dwb"][0]["extra"], "keep-me")
            self.assertEqual(player["dwb"][0]["Kex"], 4)
            self.assertEqual(player["wGS"], -1)
            manifest = read_manifest(path.with_name("mf_save7.hg"))
            self.assertTrue(size_bytes_differ_only_at_sizes(old_plain, manifest.plain))
            self.assertEqual(manifest.total_play_time, 4321)
            self.assertEqual(manifest.decompressed_size, len(raw))
            backups = list((folder / "backups").glob("*.zip"))
            self.assertEqual(len(backups), 1)
            code = _run(
                EditRequest(
                    action="restore",
                    restore_zip=backups[0],
                    restore_to=folder,
                    apply=True,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.with_name("mf_save7.hg").read_bytes(), before_mf)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)
            self.assertEqual(path.with_name("mf_save7.hg").stat().st_mtime_ns, before_mf_mtime)

    def test_version_39_uses_the_lower_purple_final(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^PURPM2", [(34, 24), (39, 23)])]))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^PURPM2", 1), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^PURPM2",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            self.assertEqual(parsed["vLc"]["6f="]["dwb"][0]["tW6"], 23)

    def test_no_finals_seed_duplicate_and_retired_are_refused(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OTHER", [(39, 1)])]))
            body = _save_body()
            path = _write_pair(folder, "save7.hg", _text(body))
            before = path.read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), before)
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)]), ("^MISSING", [(39, 2)])]))
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 1, seed=5), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save2.hg", _text(body))
            code = _run(
                EditRequest(action="finish", save=path, mission="^OPEN", game_files=tables, backup_dir=folder / "backups")
            )
            self.assertEqual(code, 2)
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 1), _row("^OPEN", 2), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save3.hg", _text(body))
            code = _run(
                EditRequest(action="finish", save=path, mission="^OPEN", game_files=tables, backup_dir=folder / "backups")
            )
            self.assertEqual(code, 2)
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 2147483647), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save4.hg", _text(body))
            code = _run(
                EditRequest(action="finish", save=path, mission="^OPEN", game_files=tables, backup_dir=folder / "backups")
            )
            self.assertEqual(code, 2)
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 3)]
            path = _write_pair(folder, "save5.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^MISSING",
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 2)

    def test_placement_order_and_unknown_items(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='4'/>"
                "<Property name='Reward' value='R_ITEM'/>"
                "</Property></Property>"
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_ITEM'/>"
                "<Property name='Reward' value='GcRewardSpecificProduct.xml'>"
                "<Property name='ID' value='WIDGET'/>"
                "<Property name='AmountMin' value='50'/>"
                "</Property></Property>"
                "<Property><Property name='Id' value='R_BAD'/>"
                "<Property name='Reward' value='GcRewardSpecificProduct.xml'>"
                "<Property name='ID' value='NOTINTABLE'/>"
                "<Property name='AmountMin' value='1'/>"
                "</Property></Property>"
                "<Property><Property name='Id' value='R_PROC'/>"
                "<Property name='Reward' value='GcRewardSpecificProduct.xml'>"
                "<Property name='ID' value='SEED#1'/>"
                "<Property name='AmountMin' value='1'/>"
                "</Property></Property></Property>"
            )
            _write_xml(
                tables,
                "<Data>"
                + _mission_xml([("^OPEN", [(39, 9)])], extra)[6:-7]
                + "<Property name='GcProductData'><Property name='ID' value='WIDGET'/>"
                "<Property name='StackMultiplier' value='0'/></Property></Data>",
            )
            player = _save_body()["vLc"]["6f="]
            player[";l5"] = _inventory([_slot("WIDGET", 4, 10, 0, 0)], ((0, 0), (1, 0)))
            player["@Cs"] = [
                {"NTx": "", "93M": "", ";l5": _inventory([], ((0, 0),))},
                {"NTx": "ship", "93M": "ship.mbin", ";l5": _inventory([], ((0, 0),))},
            ]
            player["aBE"] = 1
            player["8ZP"] = _inventory([], ((0, 0),))
            body = {"floatCheck": "FLOAT", "note": "NOTE", "XTp": "Main", "b@r": 10, "vLc": {"6f=": player}}
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            player = parsed["vLc"]["6f="]
            suit = player[";l5"][":No"]
            self.assertEqual(suit[0]["1o9"], 10)
            self.assertEqual(suit[1]["b2n"], "^WIDGET")
            self.assertEqual(suit[1]["1o9"], 10)
            self.assertEqual(suit[1]["F9q"], 10)
            self.assertEqual(suit[1]["eVk"], 0.0)
            self.assertEqual(suit[1]["3ZH"], {">Qh": 1, "XJ>": 0})
            ship = player["@Cs"][1][";l5"][":No"]
            self.assertEqual(ship[0]["1o9"], 10)
            self.assertEqual(player["@Cs"][0][";l5"][":No"], [])
            freighter = player["8ZP"][":No"]
            self.assertEqual(freighter[0]["1o9"], 20)
            self.assertEqual(freighter[0]["F9q"], 20)
            raw = unpack_save(path.read_bytes()).json_text
            self.assertIn('"eVk":0.0', raw)
            self.assertNotIn("NOTINTABLE", raw)
            self.assertNotIn("SEED#1", raw)

    def test_reset_keeps_currency_and_refuses_a_middle_step(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='1'/>"
                "<Property name='Reward' value='R_MONEY'/>"
                "</Property></Property>"
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_MONEY'/>"
                "<Property name='Reward' value='GcRewardMoney.xml'>"
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='15'/>"
                "</Property></Property></Property>"
            )
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 3)])], extra))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 0, data=2, stat=4), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = 20
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            self.assertEqual(parsed["vLc"]["6f="]["wGS"], 35)
            code = _run(
                EditRequest(action="reset", save=path, mission="^OPEN", apply=True, backup_dir=folder / "backups")
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            row = parsed["vLc"]["6f="]["dwb"][0]
            self.assertEqual(row["tW6"], -1)
            self.assertEqual(row["8?J"], 0)
            self.assertEqual(row["Kex"], 0)
            self.assertEqual(parsed["vLc"]["6f="]["wGS"], 35)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^PURPM2", 4), _row("^TEMPLATE", -1)]
            middle = _write_pair(folder, "save3.hg", _text(body))
            before = middle.read_bytes()
            code = _run(EditRequest(action="reset", save=middle, mission="^PURPM2", backup_dir=folder / "backups"))
            self.assertEqual(code, 2)
            self.assertEqual(middle.read_bytes(), before)
            body["vLc"]["6f="]["dwb"] = [
                _row("^PURPM1", 3),
                _row("^PURPM2", 4),
                _row("^PURPM3", 5),
                _row("^TEMPLATE", -1),
            ]
            chain = _write_pair(folder, "save4.hg", _text(body))
            code = _run(
                EditRequest(
                    action="reset",
                    save=chain,
                    chain="ism",
                    target="^PURPM2",
                    apply=True,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(chain.read_bytes()).json_text)
            rows = {row["p0c"]: row for row in parsed["vLc"]["6f="]["dwb"]}
            self.assertEqual(rows["^PURPM1"]["tW6"], 3)
            self.assertEqual(rows["^PURPM2"]["tW6"], -1)
            self.assertEqual(rows["^PURPM3"]["tW6"], -1)

    def test_resetting_twr_warns_that_ism_still_requires_it(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^ROBOM_STAFF_C", 2), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(EditRequest(action="reset", save=path, mission="^ROBOM_STAFF_C"), quiet=False)
            self.assertEqual(code, 0)
            self.assertIn("In Stellar Multitudes", buf.getvalue())

    def test_purple_flag_only_leaves_mission_rows(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            text = _text(_save_body())
            path = _write_pair(folder, "save7.hg", text)
            code = _run(
                EditRequest(
                    action="purple",
                    save=path,
                    flag_only=True,
                    apply=True,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            after = unpack_save(path.read_bytes()).json_text
            stored = unpack_save(path.read_bytes()).raw[:-1].decode("utf-8", "surrogateescape")
            original = text.replace('"FLOAT"', "0")
            old = locate(original, ["vLc", "6f=", "dwb"])
            new = locate(stored, ["vLc", "6f=", "dwb"])
            self.assertEqual(original[old[0] : old[1]], stored[new[0] : new[1]])
            parsed = json.loads(after)
            self.assertIs(parsed["vLc"]["6f="]["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", parsed["vLc"]["6f="]["4kj"])
            self.assertNotIn("SwW", after)

    def test_purple_full_finishes_the_named_steps_only(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            missions = [(mission_id, [(39, 5)]) for mission_id in PURPLE_MISSIONS]
            _write_xml(tables, _mission_xml(missions))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [
                _row("^ROBOT_INTRO", 1),
                _row("^ROBOM_STAFF_A", 2),
                _row("^TEMPLATE", -1),
            ]
            body["vLc"]["6f="][";R7"] = "^ROBOMISS_0"
            body["vLc"]["6f="]["Mg<"] = "^"
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="purple",
                    save=path,
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            rows = {row["p0c"]: row for row in parsed["vLc"]["6f="]["dwb"]}
            self.assertEqual(rows["^ROBOT_INTRO"]["tW6"], 1)
            self.assertEqual(rows["^ROBOM_STAFF_A"]["tW6"], 2)
            for mission_id in PURPLE_MISSIONS:
                self.assertEqual(rows[mission_id]["tW6"], 5, mission_id)
                self.assertEqual(rows[mission_id]["eZ7"], [{"kept": True}])
            self.assertEqual(parsed["vLc"]["6f="][";R7"], "^")
            self.assertIs(parsed["vLc"]["6f="]["Kg6"], True)

    def test_safety_checks_block_the_write(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            text = _text(_save_body())
            live = folder / "HelloGames" / "NMS"
            live.mkdir(parents=True)
            path = _write_pair(live, "save7.hg", text)
            before = path.read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), before)
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    live=True,
                    confirm_slot=4,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            os.environ["NMSMISSIONS_NMS_RUNNING"] = "1"
            try:
                copy = _write_pair(folder, "save7.hg", text)
                blob = copy.read_bytes()
                code = _run(
                    EditRequest(
                        action="finish",
                        save=copy,
                        mission="^OPEN",
                        apply=True,
                        rewards=False,
                        game_files=tables,
                        backup_dir=folder / "backups",
                    )
                )
                self.assertEqual(code, 2)
                self.assertEqual(copy.read_bytes(), blob)
            finally:
                os.environ.pop("NMSMISSIONS_NMS_RUNNING", None)
            left = _write_pair(folder, "save5.hg", text)
            right = _write_pair(folder, "save6.hg", text)
            os.utime(right, ns=(left.stat().st_mtime_ns + 5_000_000, left.stat().st_mtime_ns + 5_000_000))
            blob = left.read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=left,
                    mission="^OPEN",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 2)
            self.assertEqual(left.read_bytes(), blob)
            stale_save = _write_pair(folder, "save8.hg", text)
            snap = take_snapshot(stale_save)
            plan = build_plan(
                EditRequest(action="finish", save=stale_save, mission="^OPEN", rewards=False, game_files=tables),
                snap,
                load_tables(tables),
            )
            plan.checks = collect_checks(
                EditRequest(action="finish", save=stale_save, mission="^OPEN", rewards=False, game_files=tables),
                snap,
                load_tables(tables),
            )
            os.utime(stale_save, ns=(snap.save_mtime_ns + 8_000_000, snap.save_mtime_ns + 8_000_000))
            with self.assertRaises(EditError) as caught:
                commit(
                    EditRequest(action="finish", save=stale_save, apply=True, backup_dir=folder / "backups"),
                    snap,
                    plan,
                    load_tables(tables),
                )
            self.assertIn("The game saved again", str(caught.exception))
            self.assertEqual(stale_save.read_bytes(), snap.save_bytes)
            with patch("nmsmissions.edit._free_bytes", return_value=0):
                tiny = _write_pair(folder, "save1.hg", text)
                blob = tiny.read_bytes()
                code = _run(
                    EditRequest(
                        action="finish",
                        save=tiny,
                        mission="^OPEN",
                        apply=True,
                        rewards=False,
                        game_files=tables,
                        backup_dir=folder / "backups",
                    )
                )
            self.assertEqual(code, 2)
            self.assertEqual(tiny.read_bytes(), blob)

    def test_stale_tables_skip_rewards_but_finish_progress(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='1'/>"
                "<Property name='Reward' value='R_MONEY'/>"
                "</Property></Property>"
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_MONEY'/>"
                "<Property name='Reward' value='GcRewardMoney.xml'>"
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='40'/>"
                "</Property></Property></Property>"
            )
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])], extra))
            paks = folder / "PCBANKS"
            paks.mkdir()
            pak = paks / "example.pak"
            pak.write_bytes(b"pak")
            xml = tables / "missions.mxml"
            os.utime(xml, (1_000_000_000, 1_000_000_000))
            os.utime(pak, (1_700_000_000, 1_700_000_000))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", -1), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = 5
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    pcbanks=paks,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            self.assertEqual(parsed["vLc"]["6f="]["dwb"][0]["tW6"], 9)
            self.assertEqual(parsed["vLc"]["6f="]["wGS"], 5)

    def test_failed_verify_restores_both_files(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            path = _write_pair(folder, "save7.hg", _text(_save_body()), play_time=7)
            before = path.read_bytes()
            before_mf = path.with_name("mf_save7.hg").read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                    break_after_replace=True,
                )
            )
            self.assertEqual(code, 4)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.with_name("mf_save7.hg").read_bytes(), before_mf)

    def test_verify_manifest_notes(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            mapping = folder / "mapping.json"
            mapping.write_text(json.dumps({"Mapping": [{"Key": "unused", "Value": "Unused"}]}))
            path = _write_pair(folder, "save7.hg", _text(_save_body()))
            path.with_name("mf_save7.hg").unlink()
            code = main(["verify", str(path), "--mapping", str(mapping), "--no-auto-save"])
            self.assertEqual(code, 0)
            bad = _write_pair(folder, "save8.hg", _text(_save_body()), decompressed=3)
            code = main(["verify", str(bad), "--mapping", str(mapping), "--no-auto-save"])
            self.assertEqual(code, 2)
            warned = _write_pair(folder, "save6.hg", _text(_save_body()), compressed=3)
            code = main(["verify", str(warned), "--mapping", str(mapping), "--no-auto-save"])
            self.assertEqual(code, 0)

    def test_backup_install_gamedata_and_prune(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save7.hg", _text(_save_body()))
            other = _write_pair(folder, "save8.hg", _text(_save_body()))
            before = path.read_bytes()
            code = _run(EditRequest(action="backup", save=path, backup_dir=folder / "backups"))
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list((folder / "backups").glob("*.zip"))), 1)
            dest = folder / "installed"
            code = _run(
                EditRequest(
                    action="install",
                    install_folder=folder,
                    install_slot=4,
                    install_to=dest,
                )
            )
            self.assertEqual(code, 0)
            self.assertFalse((dest / "save7.hg").exists())
            code = _run(
                EditRequest(
                    action="install",
                    install_folder=folder,
                    install_slot=4,
                    install_to=dest,
                    apply=True,
                )
            )
            self.assertEqual(code, 0)
            self.assertEqual((dest / "save7.hg").read_bytes(), path.read_bytes())
            self.assertEqual((dest / "mf_save8.hg").read_bytes(), other.with_name("mf_save8.hg").read_bytes())
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(EditRequest(action="gamedata-refresh", game_files=tables), quiet=False)
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn("hgpaktool", text)
            self.assertIn("MBINCompiler", text)
            self.assertIn("Missions with a final stage: 1", text)
            day = folder / "prune"
            day.mkdir()
            for index in range(32):
                (day / f"save7.hg.20260101-{index:06d}.zip").write_bytes(b"zip")
            removed = prune_backups(day, keep=30)
            self.assertTrue((day / "save7.hg.20260101-000000.zip").is_file())
            self.assertGreaterEqual(len(removed), 1)
            self.assertLessEqual(len(list(day.glob("*.zip"))), 30)

    def test_chain_finish_stops_at_the_named_step(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^PURPM1", [(39, 6)])]))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^PURPM1", 1), _row("^PURPM2", 1), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    target="^PURPM1",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            rows = {row["p0c"]: row for row in parsed["vLc"]["6f="]["dwb"]}
            self.assertEqual(rows["^PURPM1"]["tW6"], 6)
            self.assertEqual(rows["^PURPM2"]["tW6"], 1)

    def test_expedition_context_is_refused(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            body = _save_body()
            body["XTp"] = "Season"
            path = _write_pair(folder, "save7.hg", _text(body))
            before = path.read_bytes()
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), before)


def _nested_reward(reward_id: str, reward_value: str, fields: str, choice: str = "GiveAll") -> str:
    """Real reward rows are an entry, a list wrapper, an inner list, then the reward."""
    return (
        "<Property value='GcGenericRewardTableEntry'>"
        f"<Property name='Id' value='{reward_id}'/>"
        "<Property name='List' value='GcRewardTableItemList'>"
        f"<Property name='RewardChoice' value='{choice}'/>"
        "<Property name='List'>"
        "<Property value='GcRewardTableItem'>"
        "<Property name='PercentageChance' value='100'/>"
        f"<Property name='Reward' value='{reward_value}'>"
        f"{fields}"
        "</Property></Property></Property></Property></Property>"
    )


class RealStyleTests(unittest.TestCase):
    def test_caretless_ids_nested_rewards_and_stage_index(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            early = _nested_reward(
                "R_EARLY",
                "GcRewardMoney",
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='999'/>"
                "<Property name='AmountMax' value='999'/>",
            )
            late = _nested_reward(
                "R_LATE",
                "GcRewardMoney",
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='40'/>"
                "<Property name='AmountMax' value='40'/>",
            )
            mission = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='ROBOMISS_0'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcMissionVersionProgress'>"
                "<Property name='Version' value='39'/>"
                "<Property name='Progress' value='5'/>"
                "</Property></Property>"
                "<Property name='StartingConditions'>"
                "<Property value='GcMissionConditionMissionCompleted'>"
                "<Property name='MissionID' value='NEED_ME'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                "<Property value='GcGenericMissionStage'><Property name='Reward' value='R_EARLY'/></Property>"
                "<Property value='GcGenericMissionStage'><Property name='Reward' value='R_MID'/></Property>"
                "<Property value='GcGenericMissionStage'>"
                "<Property name='Reward' value='R_LATE'/>"
                "<Property name='Reward' value='R_BUILDERSKNOWN'/>"
                "</Property>"
                "<Property value='GcGenericMissionStage'>"
                "<Property value='GcMissionConditionMissionCompleted'>"
                "<Property name='MissionID' value='ROBOMISS_3_NADA'/>"
                "</Property></Property>"
                "</Property>"
                f"<Property name='Rewards'>{early}{late}</Property>"
                "</Property></Data>"
            )
            _write_xml(tables, mission)
            (tables / "rewardtable.MXML").write_text(
                "<Data>"
                + _nested_reward("R_BUILDERSKNOWN", "GcRewardBuildersKnown", "")
                + "</Data>",
                encoding="utf-8",
            )
            loaded = load_tables(tables)
            self.assertIn("^ROBOMISS_0", loaded.missions)
            self.assertNotIn("ROBOMISS_0", loaded.missions)
            self.assertEqual(loaded.missions["^ROBOMISS_0"].requires_missions, ["^NEED_ME"])
            self.assertEqual(loaded.reward_table["R_BUILDERSKNOWN"].kind, "GcRewardBuildersKnown")
            self.assertEqual(loaded.missions["^ROBOMISS_0"].rewards["R_LATE"].kind, "GcRewardMoney")
            stages = {stage.reward_id: stage.stage_progress for stage in loaded.missions["^ROBOMISS_0"].stages}
            self.assertEqual(stages["R_EARLY"], 0)
            self.assertEqual(stages["R_LATE"], 2)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^ROBOMISS_0", 1), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = 10
            body["vLc"]["6f="]["cPt"] = False
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            request = EditRequest(
                action="finish",
                save=path,
                mission="^ROBOMISS_0",
                game_files=tables,
                backup_dir=folder / "backups",
            )
            plan = build_plan(request, snap, loaded)
            text = "\n".join(plan.granted + plan.skipped + plan.warnings)
            self.assertNotIn("no save effect", text)
            self.assertIn("already passed", text)
            self.assertIn("^NEED_ME", "\n".join(plan.warnings))
            self.assertNotIn("ROBOMISS_3", "\n".join(plan.warnings))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^ROBOMISS_0",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            player = parsed["vLc"]["6f="]
            self.assertEqual(player["dwb"][0]["tW6"], 5)
            self.assertEqual(player["wGS"], 50)
            self.assertIs(player["cPt"], True)

    def test_item_caret_substance_table_and_nested_ship_filename(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            reward = _nested_reward(
                "R_ITEM",
                "GcRewardSpecificProduct",
                "<Property name='ID' value='ANTIMATTER'/><Property name='AmountMin' value='4'/>",
            )
            fuel = _nested_reward(
                "R_FUEL",
                "GcRewardSpecificProduct",
                "<Property name='ID' value='FUEL1'/><Property name='AmountMin' value='5'/>",
            )
            _write_xml(
                tables,
                "<Data>"
                + _mission_xml([("^OPEN", [(39, 9)])], "")[6:-7]
                + "<Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^FUELMISS'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='4'/>"
                "</Property></Property>"
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='2'/><Property name='Reward' value='R_FUEL'/>"
                "</Property></Property>"
                f"<Property name='Rewards'>{fuel}</Property></Property>"
                + _mission_xml([("^SHIP", [(39, 9)])], "")[6:-7]
                + "</Data>",
            )
            # The OPEN mission needs the antimatter stage. Rewrite OPEN by a dedicated file.
            (tables / "missions.mxml").write_text(
                "<Data>"
                "<Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^OPEN'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='9'/>"
                "</Property></Property>"
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='4'/><Property name='Reward' value='R_ITEM'/>"
                "</Property></Property>"
                f"<Property name='Rewards'>{reward}</Property>"
                "</Property>"
                "<Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^FUELMISS'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='4'/>"
                "</Property></Property>"
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='2'/><Property name='Reward' value='R_FUEL'/>"
                "</Property></Property>"
                f"<Property name='Rewards'>{fuel}</Property>"
                "</Property></Data>",
                encoding="utf-8",
            )
            (tables / "PRODUCTTABLE.MXML").write_text(
                "<Data><Property value='GcProductData'>"
                "<Property name='ID' value='ANTIMATTER'/>"
                "<Property name='StackMultiplier' value='1'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            (tables / "SUBSTANCETABLE.MXML").write_text(
                "<Data><Property value='GcRealitySubstanceData'>"
                "<Property name='ID' value='FUEL1'/>"
                "<Property name='StackMultiplier' value='1'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            loaded = load_tables(tables)
            self.assertIn("^ANTIMATTER", loaded.products)
            self.assertNotIn("^ANTIMATTER", loaded.substances)
            self.assertIn("^FUEL1", loaded.substances)
            self.assertNotIn("^FUEL1", loaded.products)
            self.assertNotIn("FUEL1", loaded.products)
            player = _save_body()["vLc"]["6f="]
            player["dwb"] = [_row("^OPEN", 1), _row("^TEMPLATE", -1)]
            player[";l5"] = _inventory([_slot("^ANTIMATTER", 4, 10, 0, 0)], ((0, 0),))
            body = {"floatCheck": "FLOAT", "note": "NOTE", "XTp": "Main", "b@r": 10, "vLc": {"6f=": player}}
            topped = _write_pair(folder, "save2.hg", _text(body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=topped,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            raw = unpack_save(topped.read_bytes()).json_text
            parsed = json.loads(raw)
            self.assertEqual(parsed["vLc"]["6f="][";l5"][":No"][0]["1o9"], 8)
            self.assertNotIn('"b2n":"ANTIMATTER"', raw)
            ship_player = _save_body()["vLc"]["6f="]
            ship_player["dwb"] = [_row("^OPEN", 1), _row("^TEMPLATE", -1)]
            ship_player[";l5"] = _inventory([_slot("^ANTIMATTER", 10, 10, 0, 0)], ((0, 0),))
            ship_player["@Cs"] = [
                {"NTx": {}, ";l5": _inventory([], ((0, 0),))},
                {"NTx": {"93M": "MODELS/ship.SCENE.MBIN"}, ";l5": _inventory([], ((0, 0),))},
            ]
            ship_player["aBE"] = 1
            ship_player["8ZP"] = _inventory([], ((0, 0),))
            ship_body = {"floatCheck": "FLOAT", "note": "NOTE", "XTp": "Main", "b@r": 10, "vLc": {"6f=": ship_player}}
            ship_path = _write_pair(folder, "save3.hg", _text(ship_body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=ship_path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(ship_path.read_bytes()).json_text)
            player = parsed["vLc"]["6f="]
            self.assertEqual(player[";l5"][":No"][0]["1o9"], 10)
            self.assertEqual(player["@Cs"][0][";l5"][":No"], [])
            self.assertEqual(player["@Cs"][1][";l5"][":No"][0]["b2n"], "^ANTIMATTER")
            self.assertEqual(player["@Cs"][1][";l5"][":No"][0]["1o9"], 4)
            self.assertEqual(player["8ZP"][":No"], [])
            fuel_player = _save_body()["vLc"]["6f="]
            fuel_player["dwb"] = [_row("^FUELMISS", 0), _row("^TEMPLATE", -1)]
            fuel_player[";l5"] = _inventory([], ((0, 0),))
            fuel_player["@Cs"] = [{"NTx": "", "93M": "", ";l5": _inventory([], ())}]
            fuel_player["8ZP"] = _inventory([], ())
            fuel_body = {"floatCheck": "FLOAT", "note": "NOTE", "XTp": "Main", "b@r": 10, "vLc": {"6f=": fuel_player}}
            fuel_path = _write_pair(folder, "save4.hg", _text(fuel_body))
            code = _run(
                EditRequest(
                    action="finish",
                    save=fuel_path,
                    mission="^FUELMISS",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            slot = json.loads(unpack_save(fuel_path.read_bytes()).json_text)["vLc"]["6f="][";l5"][":No"][0]
            self.assertEqual(slot["b2n"], "^FUEL1")
            self.assertEqual(slot["Vn8"]["elv"], "Substance")
            self.assertEqual(slot["1o9"], 5)
            self.assertEqual(slot["F9q"], 9999)

    def test_drive_in_another_ships_tech_inventory_is_installed(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            ships = []
            for _index in range(4):
                ships.append({"NTx": "ship", "93M": "ship.mbin", ";l5": _inventory([], ()), "PMT": _inventory([], ())})
            tech_slots = [_slot("^OTHER", 1, 1, 0, index) for index in range(21)]
            tech_slots.append(_slot("^HDRIVEBOOST4", 1, 1, 0, 21, "Technology"))
            ships[3]["PMT"] = _inventory(tech_slots, tuple((0, index) for index in range(22)))
            body = _save_body()
            body["vLc"]["6f="]["@Cs"] = ships
            body["vLc"]["6f="]["aBE"] = 0
            path = _write_pair(folder, "save7.hg", _text(body))
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(EditRequest(action="purple", save=path, flag_only=True), quiet=False)
            self.assertEqual(code, 0)
            self.assertNotIn("not installed", buf.getvalue())
            bare = _save_body()
            missing = _write_pair(folder, "save8.hg", _text(bare))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(EditRequest(action="purple", save=missing, flag_only=True), quiet=False)
            self.assertEqual(code, 0)
            self.assertIn("not installed", buf.getvalue())

    def test_install_refuses_a_newer_destination_and_restores_a_bad_copy(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = _write_pair(folder, "save7.hg", _text(_save_body()))
            _write_pair(folder, "save8.hg", _text(_save_body()))
            dest = folder / "live"
            dest.mkdir()
            newer_body = _save_body()
            newer_body["b@r"] = 99
            newer = _write_pair(dest, "save8.hg", _text(newer_body))
            _write_pair(dest, "save7.hg", _text(_save_body()))
            for name in ("save7.hg", "mf_save7.hg", "save8.hg", "mf_save8.hg"):
                stamp = (folder / name).stat().st_mtime_ns
                os.utime(dest / name, ns=(stamp, stamp))
            os.utime(newer, ns=(source.stat().st_mtime_ns + 80_000_000, source.stat().st_mtime_ns + 80_000_000))
            before = newer.read_bytes()
            import io
            from contextlib import redirect_stderr

            buf = io.StringIO()
            with redirect_stderr(buf):
                code = _run(
                    EditRequest(
                        action="install",
                        install_folder=folder,
                        install_slot=4,
                        install_to=dest,
                        apply=True,
                        backup_dir=folder / "backups",
                    ),
                    quiet=False,
                )
            self.assertEqual(code, 2)
            self.assertIn("save8.hg", buf.getvalue())
            self.assertIn("newer", buf.getvalue())
            self.assertEqual(newer.read_bytes(), before)
            stamp_dir = folder / "stamped"
            stamp_dir.mkdir()
            copy_body = _save_body()
            copy_body["b@r"] = 10
            copy_save = _write_pair(stamp_dir, "save7.hg", _text(copy_body))
            _write_pair(stamp_dir, "save8.hg", _text(copy_body))
            live_body = _save_body()
            live_body["b@r"] = 80
            _write_pair(dest, "save7.hg", _text(live_body))
            _write_pair(dest, "save8.hg", _text(live_body))
            older = copy_save.stat().st_mtime_ns - 50_000_000
            for name in ("save7.hg", "save8.hg", "mf_save7.hg", "mf_save8.hg"):
                os.utime(dest / name, ns=(older, older))
            before_stamp = (dest / "save8.hg").read_bytes()
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = _run(
                    EditRequest(
                        action="install",
                        install_folder=stamp_dir,
                        install_slot=4,
                        install_to=dest,
                        apply=True,
                        backup_dir=folder / "backups",
                    ),
                    quiet=False,
                )
            self.assertEqual(code, 2)
            self.assertIn("timestamp", buf.getvalue())
            self.assertEqual((dest / "save8.hg").read_bytes(), before_stamp)
            fresh = folder / "installed"
            code = _run(
                EditRequest(
                    action="install",
                    install_folder=folder,
                    install_slot=4,
                    install_to=fresh,
                    apply=True,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            original = (fresh / "save7.hg").read_bytes()
            original_mf = (fresh / "mf_save7.hg").read_bytes()

            def bad_copy(src, dst, *args, **kwargs):
                Path(dst).write_bytes(b"not-a-save")

            with patch("nmsmissions.edit.shutil.copy2", side_effect=bad_copy):
                code = _run(
                    EditRequest(
                        action="install",
                        install_folder=folder,
                        install_slot=4,
                        install_to=fresh,
                        apply=True,
                        backup_dir=folder / "backups",
                    )
                )
            self.assertEqual(code, 4)
            self.assertEqual((fresh / "save7.hg").read_bytes(), original)
            self.assertEqual((fresh / "mf_save7.hg").read_bytes(), original_mf)
            self.assertTrue(list((folder / "backups").glob("*.zip")))

    def test_manifest_write_failure_restores_with_code_4(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            path = _write_pair(folder, "save7.hg", _text(_save_body()))
            before = path.read_bytes()
            before_mf = path.with_name("mf_save7.hg").read_bytes()
            real_replace = os.replace

            def fail_mf_write(src, dst, *args, **kwargs):
                if str(src).endswith(".nmsmissions-tmp") and Path(str(dst)).name.startswith("mf_"):
                    raise OSError("mf write failed")
                return real_replace(src, dst, *args, **kwargs)

            with patch("nmsmissions.edit.os.replace", side_effect=fail_mf_write):
                code = _run(
                    EditRequest(
                        action="finish",
                        save=path,
                        mission="^OPEN",
                        apply=True,
                        rewards=False,
                        game_files=tables,
                        backup_dir=folder / "backups",
                    )
                )
            self.assertEqual(code, 4)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.with_name("mf_save7.hg").read_bytes(), before_mf)
            self.assertEqual(list(folder.glob("*.nmsmissions-tmp")), [])
            self.assertEqual(list(folder.glob("*.nmsmissions-restore")), [])

            def fail_every_mf(src, dst, *args, **kwargs):
                if Path(str(dst)).name.startswith("mf_"):
                    raise OSError("mf blocked")
                return real_replace(src, dst, *args, **kwargs)

            with patch("nmsmissions.edit.os.replace", side_effect=fail_every_mf):
                code = _run(
                    EditRequest(
                        action="finish",
                        save=path,
                        mission="^OPEN",
                        apply=True,
                        rewards=False,
                        game_files=tables,
                        backup_dir=folder / "backups",
                    )
                )
            self.assertEqual(code, 4)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.with_name("mf_save7.hg").read_bytes(), before_mf)

    def test_units_clamp_is_labelled_and_older_files_warn_once(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='8'/>"
                "<Property name='Reward' value='R_MONEY'/>"
                "</Property></Property>"
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_MONEY'/>"
                "<Property name='Reward' value='GcRewardMoney'>"
                "<Property name='Currency' value='Units'/>"
                "<Property name='AmountMin' value='30000000'/>"
                "</Property></Property></Property>"
            )
            names = [("^OPEN", [(1, 8)])] + [(mission_id, [(1, 3)]) for mission_id in PURPLE_MISSIONS]
            _write_xml(tables, _mission_xml(names, extra))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 1), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = -27648696
            body["vLc"]["6f="]["yq:"] = 50
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            plan = build_plan(
                EditRequest(action="finish", save=path, mission="^OPEN", game_files=tables),
                snap,
                loaded,
            )
            self.assertTrue(any("Clamped" in change.label for change in plan.changes))
            purple = build_plan(
                EditRequest(action="purple", save=path, rewards=False, game_files=tables),
                take_snapshot(path),
                loaded,
            )
            purple.checks = collect_checks(
                EditRequest(action="purple", save=path, rewards=False, game_files=tables),
                snap,
                loaded,
            )
            blob = "\n".join(purple.warnings + [check.message for check in purple.checks])
            self.assertEqual(blob.count("older than this save"), 1)

    def test_relative_live_path_is_resolved_before_the_guard(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            live = folder / "HelloGames" / "NMS"
            live.mkdir(parents=True)
            (live / "save7.hg").write_bytes(b"x")
            copies = folder / "copies"
            copies.mkdir()
            (copies / "save7.hg").write_bytes(b"x")
            previous = os.getcwd()
            try:
                os.chdir(folder)
                self.assertTrue(is_live_nms_save(Path("HelloGames") / "NMS" / "save7.hg"))
                self.assertFalse(is_live_nms_save(Path("copies") / "save7.hg"))
                os.chdir(copies)
                self.assertTrue(is_live_nms_save(Path("..") / "HelloGames" / "NMS" / "save7.hg"))
            finally:
                os.chdir(previous)

    def test_preview_names_the_backup_and_caches_game_files(self):
        hint = format_plan(
            Plan(summary="Finish ^OPEN.", changes=[Change("Set progress.", ["tW6"], "set", 9)], backup_path="save7.hg.20260101-000000.zip"),
            dry_run=True,
            apply_hint="Press Write to write the save.",
        )
        self.assertIn("Press Write", hint)
        self.assertIn("save7.hg.20260101-000000.zip", hint)
        self.assertNotIn("Pass --apply", hint)
        cli = format_plan(Plan(summary="Finish.", backup_path="save7.hg.zip"), dry_run=True)
        self.assertIn("Pass --apply", cli)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            path = _write_pair(folder, "save7.hg", _text(_save_body()))
            session = EditorSession(path, game_files=tables, backup_dir=folder / "backups")
            self.assertTrue(session.flag_only)
            with patch("nmsmissions.edit.load_tables", wraps=load_tables) as loaded:
                session.plan_for("finish", "^OPEN", None, False)
                session.plan_for("reset", "^OPEN", None, False)
            self.assertEqual(loaded.call_count, 1)
            self.assertTrue(session.tables_cached())
            self.assertIn("save7.hg.", session._last[2].backup_path)
            session.rewards_on = False
            planned = session.plan_for("finish", "^OPEN", None, False)
            backup_name = planned.backup_path
            code, message = session.apply_last()
            self.assertEqual(code, 0)
            self.assertTrue(Path(backup_name).is_file())
            self.assertEqual(list((folder / "backups").glob("*.zip")), [Path(backup_name)])
            self.assertIn(Path(backup_name).name, message)


class RealLayoutTests(unittest.TestCase):
    def test_wrapped_amounts_currency_versions_and_repeat_rewards(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            ammo = _nested_reward(
                "R_AMMO",
                "GcRewardSpecificProduct",
                "<Property name='GcRewardSpecificProduct'>"
                "<Property name='ID' value='AMMO'/>"
                "<Property name='AmountMin' value='100'/>"
                "<Property name='AmountMax' value='600'/>"
                "</Property>",
            )
            money = _nested_reward(
                "R_MONEY",
                "GcRewardMoney",
                "<Property name='GcRewardMoney'>"
                "<Property name='Currency' value='GcCurrency'>"
                "<Property name='Currency' value='Units'/>"
                "</Property>"
                "<Property name='AmountMin' value='250'/>"
                "<Property name='AmountMax' value='250'/>"
                "</Property>",
            )
            recipes = _nested_reward(
                "R_FRIG_FUEL",
                "GcRewardMultiSpecificProductRecipes",
                "<Property name='GcRewardMultiSpecificProductRecipes'>"
                "<Property name='ProductId' value='FRIG_FUEL'/>"
                "</Property>",
            )
            builders = _nested_reward("R_BUILDERSKNOWN", "GcRewardBuildersKnown", "")

            def stage(progress_rows: list[tuple[int, int]], reward_id: str) -> str:
                rows = "".join(
                    "<Property value='GcGenericMissionVersionProgress'>"
                    f"<Property name='Version' value='{version}'/>"
                    f"<Property name='Progress' value='{progress}'/>"
                    "</Property>"
                    for version, progress in progress_rows
                )
                return (
                    "<Property name='Stages' value='GcGenericMissionStage'>"
                    f"<Property name='Versions'>{rows}</Property>"
                    "<Property name='Stage'>"
                    f"<Property name='Reward' value='{reward_id}'/>"
                    "</Property></Property>"
                )

            mission = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^LAYOUT'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/>"
                "<Property name='Progress' value='62'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                + stage([(1, 8), (39, 12)], "R_BUILDERSKNOWN")
                + stage([(1, 4), (39, 43)], "R_AMMO")
                + stage([(39, 50)], "R_AMMO")
                + stage([(39, 55)], "R_MONEY")
                + stage([(39, 60)], "R_FRIG_FUEL")
                + "</Property>"
                f"<Property name='Rewards'>{ammo}{money}{recipes}</Property>"
                "</Property></Data>"
            )
            _write_xml(tables, mission)
            (tables / "rewardtable.MXML").write_text("<Data>" + builders + "</Data>", encoding="utf-8")
            (tables / "PRODUCTTABLE.MXML").write_text(
                "<Data><Property value='GcProductData'>"
                "<Property name='ID' value='AMMO'/>"
                "<Property name='StackMultiplier' value='1'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            from nmsmissions.gamedata import stage_progress_for

            loaded = load_tables(tables)
            info = loaded.missions["^LAYOUT"]
            by_reward: dict[str, list] = {}
            for item in info.stages:
                by_reward.setdefault(item.reward_id, []).append(item)
            self.assertIsNone(by_reward["R_BUILDERSKNOWN"][0].stage_progress)
            self.assertEqual(stage_progress_for(by_reward["R_BUILDERSKNOWN"][0], 39), 12)
            self.assertEqual(stage_progress_for(by_reward["R_AMMO"][0], 39), 43)
            self.assertEqual(stage_progress_for(by_reward["R_AMMO"][1], 39), 50)
            self.assertEqual(loaded.missions["^LAYOUT"].rewards["R_AMMO"].kind, "GcRewardSpecificProduct")
            self.assertEqual(loaded.reward_table["R_BUILDERSKNOWN"].kind, "GcRewardBuildersKnown")
            body = _save_body()
            player = body["vLc"]["6f="]
            player["yq:"] = 39
            player["dwb"] = [_row("^LAYOUT", 33), _row("^TEMPLATE", -1)]
            player["wGS"] = 10
            player["cPt"] = False
            player["eZ<"] = ["^FRIG_FUEL"]
            player[";l5"] = _inventory([], ((0, 0),))
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            request = EditRequest(action="finish", save=path, mission="^LAYOUT", game_files=tables)
            plan = build_plan(request, snap, loaded)
            granted = "\n".join(plan.granted)
            skipped = "\n".join(plan.skipped)
            self.assertEqual(granted.count("100 ^AMMO"), 2)
            self.assertNotIn(": 1 ^AMMO", granted)
            self.assertNotIn("granted 100", granted)
            self.assertIn("250 Units", granted)
            self.assertNotIn("GcCurrency", skipped)
            self.assertIn("already passed", skipped)
            self.assertIn("already known", skipped)
            self.assertNotIn("known product", granted)
            self.assertNotIn("no save effect", skipped)
            self.assertTrue(any("builders known" in change.label for change in plan.changes))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^LAYOUT",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            self.assertEqual(parsed["vLc"]["6f="]["wGS"], 260)
            self.assertIs(parsed["vLc"]["6f="]["cPt"], True)
            import io
            from contextlib import redirect_stdout

            fresh = _write_pair(folder, "save3.hg", _text(body))
            before = fresh.read_bytes()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(
                    EditRequest(action="rewards", save=fresh, mission="^LAYOUT", game_files=tables),
                    quiet=False,
                )
            self.assertEqual(code, 0)
            preview = buf.getvalue()
            self.assertNotIn("Pass --apply", preview)
            self.assertIn("does not write", preview)
            self.assertNotIn("No changes.", preview)
            self.assertIn("Granted:", preview)
            self.assertEqual(fresh.read_bytes(), before)

    def test_missing_yaml_final_shows_done_after_lookup(self):
        from nmsmissions.catalog import load_completion_catalog
        from nmsmissions.chains import build_views, completion_for, load_chains
        from nmsmissions.gamedata import finals_for_catalog

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_xml(folder, _mission_xml([("^NOYAML_STAGE", [(39, 37)])]))
            tables = load_tables(folder)
            catalog = load_completion_catalog()
            self.assertNotIn("^NOYAML_STAGE", catalog)
            for key, value in finals_for_catalog(tables, 39).items():
                catalog.setdefault(key, value)
            self.assertEqual(completion_for(catalog, "NOYAML_STAGE"), 37)
            _roots, other = build_views(
                load_chains(),
                {"NOYAML_STAGE": {"Mission": "NOYAML_STAGE", "Progress": 37}},
                catalog,
                None,
            )
            step = next(item for item in other.steps if item.mission_id == "NOYAML_STAGE")
            self.assertEqual(step.status_label, "done")
            self.assertNotIn("unknown/raw", step.status_label)


class LeftoverTests(unittest.TestCase):
    def test_multi_lists_stage_position_and_current_stage(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            recipes = _nested_reward(
                "R_FRIG_FUEL",
                "GcRewardMultiSpecificProductRecipes",
                "<Property name='GcRewardMultiSpecificProductRecipes'>"
                "<Property name='ProductIds'>"
                "<Property name='ProductIds' value='FRIGATE_FUEL_1'/>"
                "<Property name='ProductIds' value='FRIGATE_FUEL_2'/>"
                "<Property name='ProductIds' value='FRIGATE_FUEL_3'/>"
                "</Property></Property>",
            )
            products = _nested_reward(
                "R_MULTI",
                "GcRewardMultiSpecificProducts",
                "<Property name='GcRewardMultiSpecificProducts'>"
                "<Property name='Items'>"
                "<Property name='Items'><Property name='Id' value='SKIP_PART'/><Property name='Amount' value='4'/></Property>"
                "<Property name='Items'><Property name='Id' value='TUT_PART'/><Property name='Amount' value='7'/></Property>"
                "</Property></Property>",
            )
            items = _nested_reward(
                "R_ITEMS",
                "GcRewardMultiSpecificItems",
                "<Property name='GcRewardMultiSpecificItems'>"
                "<Property name='Items'>"
                "<Property name='Items'><Property name='Id' value='ABAND_PART'/><Property name='Amount' value='3'/></Property>"
                "</Property></Property>",
            )
            quicksilver = _nested_reward(
                "R_QS",
                "GcRewardMoney",
                "<Property name='GcRewardMoney'>"
                "<Property name='Currency' value='GcCurrency'><Property name='Currency' value='Specials'/></Property>"
                "<Property name='AmountMin' value='500'/>"
                "</Property>",
            )

            def bare_stage(reward_id: str) -> str:
                return (
                    "<Property name='Stages' value='GcGenericMissionStage'>"
                    "<Property name='Stage'>"
                    f"<Property name='Reward' value='{reward_id}'/>"
                    "</Property></Property>"
                )

            def versioned(progress: int, reward_id: str) -> str:
                return (
                    "<Property name='Stages' value='GcGenericMissionStage'>"
                    "<Property name='Versions'>"
                    "<Property value='GcGenericMissionVersionProgress'>"
                    f"<Property name='Version' value='39'/><Property name='Progress' value='{progress}'/>"
                    "</Property></Property>"
                    "<Property name='Stage'>"
                    f"<Property name='Reward' value='{reward_id}'/>"
                    "</Property></Property>"
                )

            mission = (
                "<Data>"
                "<Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^PIRATE_PORTAL'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='5'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                + bare_stage("R_FIRST")
                + bare_stage("R_SECOND")
                + "</Property></Property>"
                "<Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^FLEET_TUT'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='30'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                + versioned(12, "R_FRIG_FUEL")
                + versioned(12, "R_FRIG_FUEL")
                + versioned(20, "R_MULTI")
                + versioned(25, "R_ITEMS")
                + versioned(28, "R_QS")
                + "</Property>"
                f"<Property name='Rewards'>{recipes}{products}{items}{quicksilver}</Property>"
                "</Property></Data>"
            )
            _write_xml(tables, mission)
            (tables / "rewardtable.MXML").write_text(
                "<Data>"
                + _nested_reward("R_FIRST", "GcRewardBuildersKnown", "")
                + _nested_reward("R_SECOND", "GcRewardBuildersKnown", "")
                + "</Data>",
                encoding="utf-8",
            )
            loaded = load_tables(tables)
            portal = loaded.missions["^PIRATE_PORTAL"]
            self.assertEqual([stage.stage_progress for stage in portal.stages], [0, 1])
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row("^PIRATE_PORTAL", 0), _row("^FLEET_TUT", 11), _row("^TEMPLATE", -1)]
            player["eZ<"] = []
            player["kN;"] = 0
            player["7QL"] = 0
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            portal_plan = build_plan(
                EditRequest(action="finish", save=path, mission="^PIRATE_PORTAL", game_files=tables),
                snap,
                loaded,
            )
            portal_skipped = "\n".join(portal_plan.skipped)
            self.assertEqual(portal_skipped.count("R_FIRST: already passed"), 1)
            self.assertIn("R_SECOND: builders known.", "\n".join(portal_plan.granted))
            fleet = build_plan(
                EditRequest(action="finish", save=path, mission="^FLEET_TUT", game_files=tables),
                take_snapshot(path),
                loaded,
            )
            granted = "\n".join(fleet.granted)
            skipped = "\n".join(fleet.skipped)
            self.assertEqual(granted.count("new recipe ^FRIGATE_FUEL_1"), 1)
            self.assertNotIn("R_FRIG_FUEL", skipped)
            self.assertIn("new recipe ^FRIGATE_FUEL_1", granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_2", granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_3", granted)
            self.assertNotIn("4 ^SKIP_PART", granted)
            self.assertNotIn("7 ^TUT_PART", granted)
            self.assertNotIn("3 ^ABAND_PART", granted)
            self.assertIn("^SKIP_PART", skipped)
            self.assertIn("^TUT_PART", skipped)
            self.assertIn("^ABAND_PART", skipped)
            self.assertIn("will be skipped", skipped)
            self.assertNotIn("item reward has no id", skipped)
            self.assertNotIn("no id to record", skipped)
            self.assertIn("500 Quicksilver", granted)
            self.assertNotIn("Specials", granted)
            player["dwb"] = [_row("^PIRATE_PORTAL", 0), _row("^FLEET_TUT", 12), _row("^TEMPLATE", -1)]
            passed_path = _write_pair(folder, "save2.hg", _text(body))
            at_stage = build_plan(
                EditRequest(action="finish", save=passed_path, mission="^FLEET_TUT", game_files=tables),
                take_snapshot(passed_path),
                loaded,
            )
            self.assertEqual("\n".join(at_stage.skipped).count("R_FRIG_FUEL: already passed"), 1)
            player["eZ<"] = ["^FRIGATE_FUEL_1"]
            player["dwb"] = [_row("^PIRATE_PORTAL", 0), _row("^FLEET_TUT", 11), _row("^TEMPLATE", -1)]
            partial_path = _write_pair(folder, "save8.hg", _text(body))
            partial = build_plan(
                EditRequest(action="finish", save=partial_path, mission="^FLEET_TUT", game_files=tables),
                take_snapshot(partial_path),
                loaded,
            )
            partial_granted = "\n".join(partial.granted)
            partial_skipped = "\n".join(partial.skipped)
            self.assertIn("^FRIGATE_FUEL_1: already known.", partial_skipped)
            self.assertNotIn("new recipe ^FRIGATE_FUEL_1", partial_granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_2", partial_granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_3", partial_granted)
            self.assertNotIn("known product", partial_granted)
            player["eZ<"] = ["^FRIGATE_FUEL"]
            short_path = _write_pair(folder, "save4.hg", _text(body))
            short = build_plan(
                EditRequest(action="finish", save=short_path, mission="^FLEET_TUT", game_files=tables),
                take_snapshot(short_path),
                loaded,
            )
            short_granted = "\n".join(short.granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_1", short_granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_2", short_granted)
            self.assertIn("new recipe ^FRIGATE_FUEL_3", short_granted)
            self.assertNotIn("already known", "\n".join(short.skipped))

    def test_same_item_fills_one_stack_and_wording(self):
        from nmsmissions.chains import progress_cell
        from nmsmissions.edit import Change, Plan, format_plan

        self.assertEqual(progress_cell(-1, 37), "not started")
        self.assertEqual(progress_cell(4, 37), "4/37")
        self.assertEqual(progress_cell(3, 2), "2/2")
        details = format_plan(
            Plan(summary="Flag.", changes=[Change("Set purple systems discovered.", ["vLc", "6f=", "Kg6"], "set", True)]),
            dry_run=True,
        )
        self.assertIn("BaseContext/PlayerStateData/HasDiscoveredPurpleSystems", details)
        self.assertNotIn("Kg6", details)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            fuel = _nested_reward(
                "R_FUEL",
                "GcRewardSpecificProduct",
                "<Property name='GcRewardSpecificProduct'>"
                "<Property name='ID' value='FRIGATE_FUEL_3'/>"
                "<Property name='AmountMin' value='1'/>"
                "</Property>",
            )
            nanites = _nested_reward(
                "R_NANITES",
                "GcRewardMoney",
                "<Property name='GcRewardMoney'>"
                "<Property name='Currency' value='GcCurrency'><Property name='Currency' value='Nanites'/></Property>"
                "<Property name='AmountMin' value='5000000000'/>"
                "</Property>",
            )
            mission = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^STACK'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='9'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                "<Property name='Stages' value='GcGenericMissionStage'>"
                "<Property name='Versions'><Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='2'/>"
                "</Property></Property>"
                "<Property name='Stage'><Property name='Reward' value='R_FUEL'/></Property>"
                "</Property>"
                "<Property name='Stages' value='GcGenericMissionStage'>"
                "<Property name='Versions'><Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='4'/>"
                "</Property></Property>"
                "<Property name='Stage'><Property name='Reward' value='R_FUEL'/></Property>"
                "</Property>"
                "<Property name='Stages' value='GcGenericMissionStage'>"
                "<Property name='Versions'><Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='6'/>"
                "</Property></Property>"
                "<Property name='Stage'><Property name='Reward' value='R_NANITES'/></Property>"
                "</Property>"
                "</Property>"
                f"<Property name='Rewards'>{fuel}{nanites}</Property>"
                "</Property></Data>"
            )
            _write_xml(tables, mission)
            (tables / "PRODUCTTABLE.MXML").write_text(
                "<Data><Property value='GcProductData'>"
                "<Property name='ID' value='FRIGATE_FUEL_3'/>"
                "<Property name='StackMultiplier' value='1'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            loaded = load_tables(tables)
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row("^STACK", -1), _row("^TEMPLATE", -1)]
            player["4kj"] = ["^HDRIVEBOOST4"]
            player["Kg6"] = False
            player[";l5"] = _inventory([], ((0, 0), (1, 0)))
            player["7QL"] = 10
            path = _write_pair(folder, "save7.hg", _text(body))
            plan = build_plan(
                EditRequest(action="finish", save=path, mission="^STACK", game_files=tables),
                take_snapshot(path),
                loaded,
            )
            adds = [change for change in plan.changes if change.label.startswith("Add ") and "FRIGATE_FUEL_3" in change.label]
            self.assertEqual(len(adds), 1)
            self.assertEqual(adds[0].value["1o9"], 2)
            self.assertTrue(any("Nanites" in change.label and "Clamped to 4294967295" in change.label for change in plan.changes))
            purple = build_plan(
                EditRequest(action="purple", save=path, flag_only=True, rewards=False, game_files=tables),
                take_snapshot(path),
                loaded,
            )
            self.assertNotIn("drive technology", purple.summary)
            self.assertFalse(any("HDRIVEBOOST4" in change.label for change in purple.changes))


class QuestLineTests(unittest.TestCase):
    def test_windows_checks_hide_the_console(self):
        import subprocess
        from unittest.mock import patch

        from nmsmissions.edit import _git_head, hidden_window_kwargs, nms_is_running

        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        with patch("nmsmissions.edit.os.name", "nt"):
            flags = hidden_window_kwargs()["creationflags"]
        self.assertEqual(flags & expected, expected)
        with patch("nmsmissions.edit.os.name", "nt"), patch.dict(os.environ, {"NMSMISSIONS_NMS_RUNNING": ""}), patch(
            "nmsmissions.edit.subprocess.check_output", return_value="INFO: No tasks are running"
        ) as check:
            self.assertFalse(nms_is_running())
        self.assertEqual(check.call_args.kwargs["creationflags"] & expected, expected)
        with patch("nmsmissions.edit.subprocess.check_output", return_value="abc\n") as git_check, patch(
            "nmsmissions.edit.hidden_window_kwargs", return_value={"creationflags": expected}
        ) as hidden:
            _git_head()
        hidden.assert_called_once()
        self.assertEqual(git_check.call_args.kwargs["creationflags"], expected)
        with patch("nmsmissions.edit.os.name", "posix"):
            self.assertEqual(hidden_window_kwargs(), {})

    def test_finish_and_reset_a_whole_quest_line(self):
        from nmsmissions.chains import load_chains
        from nmsmissions.edit import _mission_ids

        dreams = next(chain for chain in load_chains() if chain.chain_id == "dreams")
        ids = [step.mission_id for step in dreams.steps]
        self.assertEqual(ids[0], "^WATERSTORY1")
        self.assertEqual(
            _mission_ids(EditRequest(action="finish", chain="dreams", whole_line=True), finish=True),
            ids,
        )
        with self.assertRaises(EditError) as missing:
            _mission_ids(EditRequest(action="finish", chain="dreams"), finish=True)
        self.assertIn("Name the step", str(missing.exception))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            fuel = _nested_reward(
                "R_PAY",
                "GcRewardMoney",
                "<Property name='GcRewardMoney'>"
                "<Property name='Currency' value='GcCurrency'><Property name='Currency' value='Units'/></Property>"
                "<Property name='AmountMin' value='15'/>"
                "</Property>",
            )
            blocks = []
            for mission_id in ids:
                reward = ""
                stages = ""
                if mission_id == "^WATERSTORY2":
                    stages = (
                        "<Property name='Stages'>"
                        "<Property name='Stages' value='GcGenericMissionStage'>"
                        "<Property name='Versions'><Property value='GcGenericMissionVersionProgress'>"
                        "<Property name='Version' value='39'/><Property name='Progress' value='2'/>"
                        "</Property></Property>"
                        "<Property name='Stage'><Property name='Reward' value='R_PAY'/></Property>"
                        "</Property></Property>"
                    )
                    reward = f"<Property name='Rewards'>{fuel}</Property>"
                blocks.append(
                    "<Property name='GcGenericMissionSequence'>"
                    f"<Property name='MissionID' value='{mission_id}'/>"
                    "<Property name='FinalStageVersions'>"
                    "<Property value='GcGenericMissionVersionProgress'>"
                    "<Property name='Version' value='39'/><Property name='Progress' value='9'/>"
                    "</Property></Property>"
                    f"{stages}{reward}</Property>"
                )
            _write_xml(tables, "<Data>" + "".join(blocks) + "</Data>")
            loaded = load_tables(tables)
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row("^WATERSTORY1", 9), _row("^WATERSTORY2", 1), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            quiet = build_plan(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="dreams",
                    whole_line=True,
                    rewards=False,
                    game_files=tables,
                ),
                snap,
                loaded,
            )
            self.assertIn("Rewards are off", " ".join(quiet.warnings))
            self.assertNotIn("R_PAY", "\n".join(quiet.granted))
            mentioned = []
            for change in quiet.changes:
                for mission_id in ids:
                    if mission_id in change.label and mission_id not in mentioned:
                        mentioned.append(mission_id)
            self.assertNotIn("^WATERSTORY1", mentioned)
            self.assertEqual(mentioned, [mission_id for mission_id in ids if mission_id != "^WATERSTORY1"])
            paid = build_plan(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="dreams",
                    whole_line=True,
                    rewards=True,
                    game_files=tables,
                ),
                take_snapshot(path),
                loaded,
            )
            self.assertNotIn("Rewards are off", " ".join(paid.warnings))
            self.assertIn("R_PAY", "\n".join(paid.granted))
            self.assertIn("15 Units", "\n".join(paid.granted))
            reset = build_plan(
                EditRequest(action="reset", save=path, chain="dreams", whole_line=True),
                take_snapshot(path),
                None,
            )
            labels = [change.label for change in reset.changes]
            first = next(index for index, label in enumerate(labels) if "^WATERSTORY1" in label)
            second = next(index for index, label in enumerate(labels) if "^WATERSTORY2" in label)
            self.assertLess(first, second)
            self.assertIn("not started", labels[first])
            self.assertTrue(any("^WATERSTORY3" in item and "not in the save" in item for item in reset.skipped))


class UnlockTests(unittest.TestCase):
    def test_window_has_no_purple_preset(self):
        source = Path(__file__).resolve().parents[1].joinpath("nmsmissions", "gui.py").read_text(encoding="utf-8")
        self.assertEqual(FINISH_LABEL, "Finish this mission")
        self.assertEqual(UNLOCK_LABEL, "Unlock only (you can still play it)")
        self.assertIn(UNLOCK_LABEL, source)
        self.assertIn("Give items and money", source)
        self.assertNotIn("Unlock purple stars", source)
        self.assertNotIn("Just unlock purple stars", source)
        self.assertNotIn("don't finish story missions", source)

    def test_unlock_story_matches_flag_only(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [
                _row("^PURPM1", 1),
                _row("^OPEN", 3),
                _row("^TEMPLATE", -1),
            ]
            text = _text(body)
            flag_dir = folder / "flag"
            unlock_dir = folder / "unlock"
            flag_dir.mkdir()
            unlock_dir.mkdir()
            flag_path = _write_pair(flag_dir, "save7.hg", text)
            unlock_path = _write_pair(unlock_dir, "save7.hg", text)
            flag_code = _run(
                EditRequest(action="purple", save=flag_path, flag_only=True, apply=True, backup_dir=flag_dir / "backups")
            )
            unlock_code = _run(
                EditRequest(
                    action="unlock",
                    save=unlock_path,
                    mission="^PURPM1",
                    apply=True,
                    rewards=True,
                    backup_dir=unlock_dir / "backups",
                )
            )
            self.assertEqual(flag_code, 0)
            self.assertEqual(unlock_code, 0)
            flag_player = json.loads(unpack_save(flag_path.read_bytes()).json_text)["vLc"]["6f="]
            unlock_player = json.loads(unpack_save(unlock_path.read_bytes()).json_text)["vLc"]["6f="]
            self.assertIs(unlock_player["Kg6"], True)
            self.assertEqual(unlock_player["Kg6"], flag_player["Kg6"])
            self.assertEqual(unlock_player["4kj"], flag_player["4kj"])
            self.assertIn("^HDRIVEBOOST4", unlock_player["4kj"])
            self.assertEqual(unlock_player["dwb"], flag_player["dwb"])
            self.assertEqual(unlock_player[";R7"], flag_player[";R7"])
            self.assertEqual(unlock_player["wGS"], flag_player["wGS"])
            self.assertEqual({row["p0c"]: row["tW6"] for row in unlock_player["dwb"]}["^PURPM1"], 1)
            self.assertNotIn("SwW", unpack_save(unlock_path.read_bytes()).json_text)
            import io
            from contextlib import redirect_stdout

            warn_dir = folder / "warn"
            warn_dir.mkdir()
            bare = _write_pair(warn_dir, "save4.hg", text)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = _run(
                    EditRequest(action="unlock", save=bare, mission="^PURPM1", rewards=True),
                    quiet=False,
                )
            self.assertEqual(code, 0)
            self.assertIn("not installed", buf.getvalue())

    def test_reward_purple_uses_the_same_unlock(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_PURPLESYSTEMS'/>"
                "<Property name='Reward' value='GcRewardPurpleSystems'/>"
                "</Property></Property>"
            )
            _write_xml(tables, _mission_xml([("^SIDE", [(39, 8)])], extra))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^SIDE", 2), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="unlock",
                    save=path,
                    mission="^SIDE",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            player = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in player["dwb"]}
            self.assertIs(player["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", player["4kj"])
            self.assertEqual(rows["^SIDE"]["tW6"], 2)
            self.assertEqual(player[";R7"], "^OTHER")
            self.assertEqual(player["wGS"], 100)

    def test_flag_prerequisite_does_not_start_the_mission(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='StartingConditions'>"
                "<Property value='GcMissionConditionFlag'>"
                "<Property name='Flag' value='BuildersKnown'/>"
                "</Property></Property>"
            )
            _write_xml(tables, _mission_xml([("^GATE", [(39, 6)])], extra))
            loaded = load_tables(tables)
            self.assertEqual(loaded.missions["^GATE"].requires_flags, ["cPt"])
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^GATE", -1), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["cPt"] = False
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="unlock",
                    save=path,
                    mission="^GATE",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            player = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in player["dwb"]}
            self.assertIs(player["cPt"], True)
            self.assertIs(player["Kg6"], False)
            self.assertEqual(rows["^GATE"]["tW6"], -1)
            self.assertEqual(player[";R7"], "^OTHER")

    def test_no_unlock_starts_the_mission(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^FRESH", [(39, 9)])]))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 3), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="unlock",
                    save=path,
                    mission="^FRESH",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            player = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in player["dwb"]}
            self.assertEqual(rows["^FRESH"]["tW6"], 0)
            self.assertLess(rows["^FRESH"]["tW6"], 9)
            self.assertEqual(rows["^OPEN"]["tW6"], 3)
            self.assertEqual(player[";R7"], "^FRESH")
            self.assertIs(player["Kg6"], False)
            self.assertNotIn("^HDRIVEBOOST4", player["4kj"])
            self.assertEqual(player["wGS"], 100)

    def test_already_started_mission_explains_why(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 3), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="][";R7"] = "^OPEN"
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            reason = unlock_block_reason(snap.player, loaded, "^OPEN", 39)
            self.assertEqual(reason, "This mission is already started. There is no separate unlock.")
            plan = build_plan(
                EditRequest(action="unlock", save=path, mission="^OPEN", rewards=True, game_files=tables),
                snap,
                loaded,
            )
            self.assertEqual(plan.changes, [])
            self.assertIn(reason, plan.summary)
            code = _run(
                EditRequest(
                    action="unlock",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 3)
            done = _save_body()
            done["vLc"]["6f="]["dwb"] = [_row("^OPEN", 9), _row("^TEMPLATE", -1)]
            done_path = _write_pair(folder, "save8.hg", _text(done))
            done_reason = unlock_block_reason(take_snapshot(done_path).player, loaded, "^OPEN", 39)
            self.assertEqual(done_reason, "This mission is already done.")

    def test_in_progress_mission_becomes_the_tracked_one(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([("^OPEN", [(39, 9)])]))
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", 3), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            code = _run(
                EditRequest(
                    action="unlock",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            player = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in player["dwb"]}
            self.assertEqual(rows["^OPEN"]["tW6"], 3)
            self.assertEqual(player[";R7"], "^OPEN")

    def test_finish_chain_sets_the_unlock_flag(self):
        from nmsmissions.chains import load_chains
        from nmsmissions.edit import build_plan, format_plan

        ism = next(chain for chain in load_chains() if chain.chain_id == "ism")
        ids = [step.mission_id for step in ism.steps]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            _write_xml(tables, _mission_xml([(mission_id, [(39, 9)]) for mission_id in ids]))
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row(mission_id, 1) for mission_id in ids] + [_row("^TEMPLATE", -1)]
            player["5?q"] = False
            player[";9U"] = False
            player["5hy"] = 0
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            preview = build_plan(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    whole_line=True,
                    rewards=False,
                    game_files=tables,
                ),
                snap,
                loaded,
            )
            shown = format_plan(preview, dry_run=True)
            self.assertIn("Set purple systems discovered.", shown)
            self.assertIn("R_PURPLESYSTEMS: purple systems discovered.", shown)
            self.assertIn("^HDRIVEBOOST4", shown)
            self.assertNotIn("It stays unfinished", preview.summary)
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    whole_line=True,
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            done = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in done["dwb"]}
            for mission_id in ids:
                self.assertEqual(rows[mission_id]["tW6"], 9)
            self.assertIs(done["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", done["4kj"])
            self.assertIs(done["5?q"], False)
            self.assertIs(done[";9U"], False)
            self.assertEqual(done["5hy"], 0)
            self.assertNotIn("SwW", unpack_save(path.read_bytes()).json_text)

            stuck = _save_body()
            stuck["vLc"]["6f="]["dwb"] = [_row("^PURPM1", 9), _row("^TEMPLATE", -1)]
            stuck["vLc"]["6f="]["5?q"] = False
            stuck["vLc"]["6f="][";9U"] = False
            stuck_path = _write_pair(folder, "save8.hg", _text(stuck))
            step_code = _run(
                EditRequest(
                    action="finish",
                    save=stuck_path,
                    mission="^PURPM1",
                    apply=True,
                    rewards=False,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(step_code, 0)
            repaired = json.loads(unpack_save(stuck_path.read_bytes()).json_text)["vLc"]["6f="]
            repaired_rows = {row["p0c"]: row for row in repaired["dwb"]}
            self.assertEqual(repaired_rows["^PURPM1"]["tW6"], 9)
            self.assertIs(repaired["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", repaired["4kj"])
            self.assertIs(repaired["5?q"], False)
            self.assertIs(repaired[";9U"], False)

            side_tables = folder / "side"
            side_tables.mkdir()
            extra = (
                "<Property name='Rewards'><Property>"
                "<Property name='Id' value='R_PURPLESYSTEMS'/>"
                "<Property name='Reward' value='GcRewardPurpleSystems'/>"
                "</Property></Property>"
            )
            _write_xml(side_tables, _mission_xml([("^SIDE", [(39, 8)]), ("^PLAIN", [(39, 4)])], extra))
            side = _save_body()
            side["vLc"]["6f="]["dwb"] = [_row("^SIDE", 1), _row("^PLAIN", 1), _row("^TEMPLATE", -1)]
            side_path = _write_pair(folder, "save4.hg", _text(side))
            side_code = _run(
                EditRequest(
                    action="finish",
                    save=side_path,
                    mission="^SIDE",
                    apply=True,
                    rewards=False,
                    game_files=side_tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(side_code, 0)
            side_player = json.loads(unpack_save(side_path.read_bytes()).json_text)["vLc"]["6f="]
            self.assertIs(side_player["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", side_player["4kj"])
            plain_path = _write_pair(folder, "save5.hg", _text(side))
            plain_code = _run(
                EditRequest(
                    action="finish",
                    save=plain_path,
                    mission="^PLAIN",
                    apply=True,
                    rewards=False,
                    game_files=side_tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(plain_code, 0)
            plain_player = json.loads(unpack_save(plain_path.read_bytes()).json_text)["vLc"]["6f="]
            self.assertIs(plain_player["Kg6"], False)
            self.assertNotIn("^HDRIVEBOOST4", plain_player["4kj"])

    def test_finished_line_applies_only_missing_unlocks(self):
        from nmsmissions.chains import load_chains
        from nmsmissions.edit import build_plan, format_plan

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
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            preview = build_plan(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    whole_line=True,
                    rewards=True,
                    game_files=tables,
                ),
                snap,
                loaded,
            )
            shown = format_plan(preview, dry_run=True)
            self.assertIn("missing unlocks", preview.summary)
            self.assertIn("Set purple systems discovered.", shown)
            self.assertIn("^HDRIVEBOOST4", shown)
            self.assertNotIn("^CORE_LORE", shown)
            self.assertFalse(any("^CORE_LORE" in change.label for change in preview.changes))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    whole_line=True,
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            done = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in done["dwb"]}
            self.assertEqual(rows["^CORE_LORE"]["tW6"], -1)
            self.assertEqual(rows["^PURPM1"]["tW6"], 9)
            self.assertIs(done["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", done["4kj"])


class SaveOnlyFinishTests(unittest.TestCase):
    def test_complete_ism_applies_unlocks_without_game_files(self):
        from nmsmissions.catalog import load_completion_catalog
        from nmsmissions.chains import load_chains
        from nmsmissions.edit import build_plan

        ism = next(chain for chain in load_chains() if chain.chain_id == "ism")
        catalog = load_completion_catalog()
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            player = body["vLc"]["6f="]
            player["Kg6"] = False
            player["4kj"] = []
            player["dwb"] = [_row("^TEMPLATE", -1)]
            for step in ism.steps:
                progress = -1 if step.mission_id == "^CORE_LORE" else catalog[step.mission_id]
                player["dwb"].insert(0, _row(step.mission_id, progress))
            path = _write_pair(folder, "save7.hg", _text(body))
            preview = build_plan(
                EditRequest(action="finish", save=path, chain="ism", whole_line=True, rewards=True),
                take_snapshot(path),
                None,
            )
            shown = "\n".join(change.label for change in preview.changes)
            self.assertIn("missing unlocks", preview.summary)
            self.assertIn("purple systems discovered", shown.lower())
            self.assertIn("^HDRIVEBOOST4", shown)
            self.assertNotIn("^CORE_LORE", shown)
            self.assertFalse(any("progress" in change.label and "^PURPM1" in change.label for change in preview.changes))
            self.assertNotIn("--game-files", shown)
            self.assertNotIn("--game-files", " ".join(preview.warnings))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    chain="ism",
                    whole_line=True,
                    apply=True,
                    rewards=True,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            done = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]
            rows = {row["p0c"]: row for row in done["dwb"]}
            self.assertEqual(rows["^CORE_LORE"]["tW6"], -1)
            self.assertEqual(rows["^PURPM1"]["tW6"], catalog["^PURPM1"])
            self.assertIs(done["Kg6"], True)
            self.assertIn("^HDRIVEBOOST4", done["4kj"])

    def test_unknown_mission_without_tables_asks_for_game_files(self):
        from nmsmissions.edit import READ_GAME_FILES_TIP, build_plan

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^NOTINCATALOG", 1), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            preview = build_plan(
                EditRequest(action="finish", save=path, mission="^NOTINCATALOG", rewards=True),
                take_snapshot(path),
                None,
            )
            self.assertEqual(preview.summary, READ_GAME_FILES_TIP)
            self.assertEqual(preview.changes, [])
            self.assertTrue(any(READ_GAME_FILES_TIP in item for item in preview.warnings))

    def test_catalog_final_finishes_a_known_step_without_tables(self):
        from nmsmissions.edit import build_plan

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^ACT1_STEP1", 1), _row("^TEMPLATE", -1)]
            path = _write_pair(folder, "save7.hg", _text(body))
            preview = build_plan(
                EditRequest(action="finish", save=path, mission="^ACT1_STEP1", rewards=True),
                take_snapshot(path),
                None,
            )
            self.assertTrue(any("^ACT1_STEP1" in change.label and "7" in change.label for change in preview.changes))
            self.assertTrue(any("Read my game files" in item for item in preview.warnings))

    def test_reset_summary_counts_only_steps_that_change(self):
        from nmsmissions.chains import is_lore_step, load_chains
        from nmsmissions.edit import build_plan
        from nmsmissions.gui import quest_line_prompt

        twr = next(chain for chain in load_chains() if chain.chain_id == "twr")
        started = [step.mission_id for step in twr.steps if not step.optional and not is_lore_step(step.mission_id)]
        self.assertEqual(len(twr.steps), 19)
        self.assertEqual(len(started), 17)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row(mission_id, 1) for mission_id in started]
            body["vLc"]["6f="]["dwb"].append(_row("^TEMPLATE", -1))
            path = _write_pair(folder, "save7.hg", _text(body))
            preview = build_plan(
                EditRequest(action="reset", save=path, chain="twr", whole_line=True),
                take_snapshot(path),
                None,
            )
            self.assertTrue(preview.summary.startswith("Reset 17 steps:"))
            listed = preview.summary.split(":", 1)[1].split(".")[0]
            ids = [part.strip() for part in listed.split(",") if part.strip()]
            self.assertEqual(ids, started)
            self.assertNotIn("^SENT_MISS_LORE", preview.summary)
            self.assertNotIn("^ROBOMISS_LORE_", preview.summary)
            kind, _title, body_text = quest_line_prompt(
                "reset",
                {"text": "They Who Returned", "open_ids": started, "chain_id": "twr"},
                False,
            )
            self.assertEqual(kind, "confirm")
            self.assertIn("Reset 17 steps", body_text)


class SeparateRewardTableTests(unittest.TestCase):
    def test_finish_grants_items_and_money_from_the_reward_table(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            (tables / "metadata" / "simulation" / "missions").mkdir(parents=True)
            (tables / "metadata" / "reality" / "tables").mkdir(parents=True)

            def stage(progress: int, reward_id: str) -> str:
                return (
                    "<Property name='Stages' value='GcGenericMissionStage'>"
                    "<Property name='Versions'><Property value='GcGenericMissionVersionProgress'>"
                    f"<Property name='Version' value='39'/><Property name='Progress' value='{progress}'/>"
                    "</Property></Property>"
                    "<Property name='Stage'>"
                    f"<Property name='Reward' value='{reward_id}'/>"
                    "</Property></Property>"
                )

            mission = (
                "<Data><Property name='GcGenericMissionSequence'>"
                "<Property name='MissionID' value='^OPEN'/>"
                "<Property name='FinalStageVersions'>"
                "<Property value='GcGenericMissionVersionProgress'>"
                "<Property name='Version' value='39'/><Property name='Progress' value='9'/>"
                "</Property></Property>"
                "<Property name='Stages'>"
                + stage(1, "R_PAY")
                + stage(2, "R_ITEM")
                + stage(3, "R_GHOST")
                + stage(4, "R_WEIRD")
                + "</Property></Property></Data>"
            )
            (tables / "metadata" / "simulation" / "missions" / "story.exml").write_text(mission, encoding="utf-8")
            money = _nested_reward(
                "R_PAY",
                "GcRewardMoney",
                "<Property name='GcRewardMoney'>"
                "<Property name='Currency' value='GcCurrency'>"
                "<Property name='Currency' value='Units'/>"
                "</Property>"
                "<Property name='AmountMin' value='15'/>"
                "<Property name='AmountMax' value='15'/>"
                "</Property>",
            )
            item = _nested_reward(
                "R_ITEM",
                "GcRewardSpecificProduct",
                "<Property name='GcRewardSpecificProduct'>"
                "<Property name='ID' value='LAUNCHFUEL'/>"
                "<Property name='AmountMin' value='2'/>"
                "<Property name='AmountMax' value='2'/>"
                "</Property>",
            )
            ghost = _nested_reward(
                "R_GHOST",
                "GcRewardSpecificProduct",
                "<Property name='GcRewardSpecificProduct'>"
                "<Property name='ID' value='GHOST'/>"
                "<Property name='AmountMin' value='1'/>"
                "</Property>",
            )
            weird = _nested_reward(
                "R_WEIRD",
                "GcRewardNotARealKind",
                "<Property name='GcRewardNotARealKind'><Property name='Dummy' value='1'/></Property>",
            )
            (tables / "metadata" / "reality" / "tables" / "rewardtable.exml").write_text(
                "<Data>" + money + item + ghost + weird + "</Data>",
                encoding="utf-8",
            )
            (tables / "metadata" / "reality" / "tables" / "nms_reality_gcproducttable.exml").write_text(
                "<Data><Property value='GcProductData'>"
                "<Property name='ID' value='LAUNCHFUEL'/>"
                "<Property name='StackMultiplier' value='1'/>"
                "</Property></Data>",
                encoding="utf-8",
            )
            loaded = load_tables(tables)
            self.assertNotIn("R_PAY", loaded.missions["^OPEN"].rewards)
            self.assertEqual(loaded.reward_table["R_PAY"].kind, "GcRewardMoney")
            self.assertIn("^LAUNCHFUEL", loaded.products)
            body = _save_body()
            player = body["vLc"]["6f="]
            player["dwb"] = [_row("^OPEN", -1), _row("^TEMPLATE", -1)]
            player["wGS"] = 100
            player[";l5"] = _inventory([], ((0, 0),))
            path = _write_pair(folder, "save7.hg", _text(body))
            preview = build_plan(
                EditRequest(action="finish", save=path, mission="^OPEN", rewards=True, game_files=tables),
                take_snapshot(path),
                loaded,
            )
            granted = "\n".join(preview.granted)
            skipped = "\n".join(preview.skipped)
            self.assertIn("15 Units", granted)
            self.assertIn("^LAUNCHFUEL", granted)
            self.assertIn("R_GHOST", skipped)
            self.assertIn("R_WEIRD", skipped)
            self.assertIn("will be skipped", skipped)
            self.assertNotIn("GHOST", granted)
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            state = parsed["vLc"]["6f="]
            self.assertEqual(state["wGS"], 115)
            slot = state[";l5"][":No"][0]
            self.assertEqual(slot["b2n"], "^LAUNCHFUEL")
            self.assertEqual(slot["1o9"], 2)
            raw = unpack_save(path.read_bytes()).json_text
            self.assertNotIn("GHOST", raw)
            print("fixture items and money granted")

    def test_matching_pak_record_allows_rewards_and_skips_the_older_files_warning(self):
        from nmsmissions.gameread import write_source_record

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = folder / "tables"
            tables.mkdir()
            extra = (
                "<Property name='Stages'><Property name='Stage'>"
                "<Property name='Progress' value='1'/>"
                "<Property name='Reward' value='R_MONEY'/>"
                "</Property></Property>"
            )
            _write_xml(tables, _mission_xml([("^OPEN", [(1, 8)])], extra))
            (tables / "rewardtable.exml").write_text(
                "<Data>"
                + _nested_reward(
                    "R_MONEY",
                    "GcRewardMoney",
                    "<Property name='GcRewardMoney'>"
                    "<Property name='Currency' value='Units'/>"
                    "<Property name='AmountMin' value='15'/>"
                    "</Property>",
                )
                + "</Data>",
                encoding="utf-8",
            )
            paks = folder / "PCBANKS"
            paks.mkdir()
            pak = paks / "example.pak"
            pak.write_bytes(b"pak")
            for path in tables.rglob("*"):
                if path.is_file():
                    os.utime(path, (1_000_000_000, 1_000_000_000))
            os.utime(pak, (1_700_000_000, 1_700_000_000))
            write_source_record(tables, [pak])
            loaded = load_tables(tables, paks)
            self.assertFalse(loaded.stale)
            self.assertTrue(loaded.files_match)
            body = _save_body()
            body["vLc"]["6f="]["dwb"] = [_row("^OPEN", -1), _row("^TEMPLATE", -1)]
            body["vLc"]["6f="]["wGS"] = 100
            body["vLc"]["6f="]["yq:"] = 50
            path = _write_pair(folder, "save7.hg", _text(body))
            snap = take_snapshot(path)
            request = EditRequest(
                action="finish",
                save=path,
                mission="^OPEN",
                rewards=True,
                game_files=tables,
                pcbanks=paks,
            )
            plan = build_plan(request, snap, loaded)
            plan.checks = collect_checks(request, snap, loaded)
            blob = "\n".join(plan.warnings + [check.message for check in plan.checks])
            self.assertNotIn("older than this save", blob)
            self.assertIn("15 Units", "\n".join(plan.granted))
            code = _run(
                EditRequest(
                    action="finish",
                    save=path,
                    mission="^OPEN",
                    apply=True,
                    rewards=True,
                    game_files=tables,
                    pcbanks=paks,
                    backup_dir=folder / "backups",
                )
            )
            self.assertEqual(code, 0)
            parsed = json.loads(unpack_save(path.read_bytes()).json_text)
            self.assertEqual(parsed["vLc"]["6f="]["wGS"], 115)


if __name__ == "__main__":
    unittest.main()

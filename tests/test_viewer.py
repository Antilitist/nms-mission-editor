"""Synthetic saves only. No real No Man's Sky profile is read or written."""

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
import json
import unittest
from collections import OrderedDict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from nmsmissions.catalog import load_completion_catalog
from nmsmissions.chains import (
    ChainDef,
    StepDef,
    build_views,
    flatten_chains,
    format_forest,
    load_chains,
    mission_status,
    progress_cell,
    tidy_mission_id,
)
from nmsmissions.chunks import pack_chunks, unpack_save
from nmsmissions.cli import main
from nmsmissions.guard import is_live_nms_save
from nmsmissions.diffing import choose_save_file
from nmsmissions.gui import forest_rows, matching_rows
from nmsmissions.saveio import deobfuscate, load_save, verify_save

MAPPING = {
    "vLc": "BaseContext",
    "2YS": "ExpeditionContext",
    "XTp": "ActiveContext",
    "6f=": "PlayerStateData",
    "dwb": "MissionProgress",
    "p0c": "Mission",
    "tW6": "Progress",
    ";R7": "CurrentMissionID",
    "Mg<": "PreviousMissionID",
    "Kg6": "HasDiscoveredPurpleSystems",
    "SwW": "PurpleSystemsUnlocked",
    "5hy": "FirstPurpleSystemUA",
    "5?q": "HasGalacticMapRequestFirstPurple",
    ";9U": "HasGalacticMapRequestAllPurples",
}
REVERSE = {value: key for key, value in MAPPING.items()}


def obfuscate(node):
    if isinstance(node, dict):
        out = OrderedDict()
        for key, value in node.items():
            out[REVERSE.get(key, key)] = obfuscate(value)
        return out
    if isinstance(node, list):
        return [obfuscate(item) for item in node]
    return node


def document(
    missions,
    purple=False,
    unlocked=False,
    current="^ACT1_STEP2",
    total_play_time=None,
    time_alive=None,
):
    entries = []
    for mission_id, progress in missions:
        entries.append(
            OrderedDict(
                [
                    ("Mission", mission_id),
                    ("Progress", progress),
                    ("kku", "keep-me"),
                ]
            )
        )
    state = OrderedDict(
        [
            ("MissionProgress", entries),
            ("CurrentMissionID", current),
            ("PreviousMissionID", "^ACT1_STEP1"),
            ("HasDiscoveredPurpleSystems", purple),
            ("PurpleSystemsUnlocked", unlocked),
            ("FirstPurpleSystemUA", "0"),
            ("HasGalacticMapRequestFirstPurple", False),
            ("HasGalacticMapRequestAllPurples", False),
        ]
    )
    if time_alive is not None:
        state["TimeAlive"] = time_alive
    root = OrderedDict(
        [
            ("ActiveContext", "BaseContext"),
            ("BaseContext", OrderedDict([("PlayerStateData", state)])),
            (
                "ExpeditionContext",
                OrderedDict(
                    [
                        (
                            "PlayerStateData",
                            OrderedDict(
                                [
                                    ("MissionProgress", []),
                                    ("CurrentMissionID", ""),
                                ]
                            ),
                        )
                    ]
                ),
            ),
        ]
    )
    if total_play_time is not None:
        root["CommonStateData"] = OrderedDict([("TotalPlayTime", total_play_time)])
    return root


def write_chunked(path: Path, missions, **kwargs) -> bytes:
    raw = json.dumps(obfuscate(document(missions, **kwargs))).encode("utf-8") + b"\x00"
    blob = pack_chunks(raw, chunk_size=64)
    path.write_bytes(blob)
    return blob


def run_cli(argv):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ChunkTests(unittest.TestCase):
    def test_round_trip_keeps_nul_and_uses_several_chunks(self):
        raw = b'{"ok":true}' + b"\x00" + (b" " * 200)
        blob = pack_chunks(raw, chunk_size=64)
        self.assertGreater(blob.count(bytes.fromhex("E5A1EDFE")), 1)
        payload = unpack_save(blob)
        self.assertTrue(payload.chunked)
        self.assertEqual(payload.raw, raw)
        self.assertFalse(payload.json_text.endswith("\x00"))

    def test_plain_json_is_legacy(self):
        raw = b'{"plain":1}\n'
        payload = unpack_save(raw)
        self.assertFalse(payload.chunked)
        self.assertEqual(payload.raw, raw)


class SaveTests(unittest.TestCase):
    def test_unknown_key_and_order_survive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            mapping_path = Path(tmp) / "mapping.json"
            mapping_path.write_text(json.dumps({"Mapping": [{"Key": k, "Value": v} for k, v in MAPPING.items()]}))
            write_chunked(path, [("^ACT1_STEP1", 7), ("^ACT1_STEP2", 3), ("^SIDE_ONLY", 1)])
            save = load_save(path, MAPPING, "fixture")
            entry = save.contexts["BaseContext"].missions["^ACT1_STEP1"]
            self.assertEqual(list(entry.keys()), ["Mission", "Progress", "kku"])
            self.assertEqual(entry["kku"], "keep-me")
            self.assertEqual(save.active_context, "BaseContext")
            self.assertEqual(save.contexts["BaseContext"].current_mission, "^ACT1_STEP2")
            code, out, err = run_cli(["list", str(path), "--mapping", str(mapping_path)])
            self.assertEqual(code, 0, err)
            self.assertIn(">>> NEXT", out)
            self.assertIn("Artemis Path", out)
            self.assertIn("They Who Returned", out)
            self.assertIn("In Stellar Multitudes", out)
            self.assertIn("^SIDE_ONLY", out)
            self.assertIn("Other", out)
            self.assertIn("not in a known chain", out)
            self.assertIn("Mission ID", out)
            self.assertIn("Friendly Name", out)
            self.assertIn("Status", out)
            self.assertIn("Progress", out)
            side = next(line for line in out.splitlines() if "^SIDE_ONLY" in line)
            self.assertIn("Side Only (id)", side)

    def test_deobfuscate_leaves_unknown_keys(self):
        node = OrderedDict([("p0c", "^X"), ("kku", 1), ("tW6", 2)])
        plain = deobfuscate(node, MAPPING)
        self.assertEqual(list(plain.keys()), ["Mission", "kku", "Progress"])


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.chains = load_chains()
        self.catalog = load_completion_catalog()

    def test_every_chain_step_is_in_the_catalog(self):
        missing = [
            step.mission_id
            for chain in self.chains
            for step in chain.steps
            if step.mission_id not in self.catalog
        ]
        self.assertEqual(missing, [])

    def test_wiki_prerequisite_graph(self):
        by_id = {chain.chain_id: chain for chain in self.chains}
        self.assertEqual(by_id["twr"].requires, ("artemis",))
        self.assertEqual(by_id["ism"].requires, ("twr", "atlas"))
        self.assertEqual(by_id["artemis"].requires, ())
        self.assertEqual(by_id["atlas"].requires, ())

    def test_minus_one_is_not_started_before_completion_check(self):
        code, label = mission_status(-1, -1)
        self.assertEqual(code, "not_started")
        self.assertEqual(label, "not started")
        code, _label = mission_status(-1, 6)
        self.assertEqual(code, "not_started")

    def test_next_step_skips_done_and_optional(self):
        done = []
        for chain in self.chains:
            if chain.chain_id != "atlas":
                continue
            for step in chain.steps:
                if step.mission_id == "^ATLAS_LOOP_DENY":
                    done.append((step.mission_id, -1))
                else:
                    done.append((step.mission_id, self.catalog[step.mission_id]))
        roots, _other = build_views(self.chains, _entries(done), self.catalog, None, {})
        atlas = {node.chain_id: node for node in flatten_chains(roots)}["atlas"]
        self.assertIsNone(atlas.next_step)
        self.assertTrue(atlas.complete)

    def test_twr_and_ism_gates(self):
        roots, _other = build_views(self.chains, {}, self.catalog, None, {})
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        self.assertEqual(flat["artemis"].next_step.mission_id, "^ACT1_STEP1")
        self.assertIn("Artemis Path", flat["twr"].blocked_by)
        self.assertEqual(flat["twr"].next_step.mission_id, "^ROBOT_INTRO")
        self.assertIn("They Who Returned", flat["ism"].blocked_by)
        self.assertIn("Atlas Path", flat["ism"].blocked_by)
        text = format_forest(roots, _other)
        self.assertIn(">>> NEXT", text)
        self.assertIn("Artemis Path -> They Who Returned", text)
        self.assertIn("They Who Returned + Atlas Path -> In Stellar Multitudes", text)
        self.assertIn("(start of chain)", text)

        finished = []
        for chain in self.chains:
            if chain.chain_id not in ("artemis", "atlas"):
                continue
            for step in chain.steps:
                if step.optional or self.catalog[step.mission_id] == -1:
                    finished.append((step.mission_id, -1))
                else:
                    finished.append((step.mission_id, self.catalog[step.mission_id]))
        roots, _other = build_views(self.chains, _entries(finished), self.catalog, None, {})
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        self.assertEqual(flat["twr"].blocked_by, [])
        self.assertEqual(flat["ism"].blocked_by, ["They Who Returned"])
        self.assertTrue(flat["artemis"].complete)
        self.assertTrue(flat["atlas"].complete)

    def test_rows_highlight_the_next_step(self):
        roots, other = build_views(self.chains, {}, self.catalog, "^ACT1_STEP1", {})
        rows = forest_rows(roots, other)
        nxt = next(row for row in rows if row["text"] == "^ACT1_STEP1")
        self.assertEqual(nxt["tag"], "next")
        self.assertIn("NEXT", nxt["status"])
        self.assertEqual(nxt["name"], "Artemis Path - step 1")
        self.assertEqual(nxt["progress"], "absent")
        self.assertIn("optional", next(row["status"] for row in rows if row["text"] == "^ATLAS_LORE"))
        twr = next(row for row in rows if row["text"] == "They Who Returned")
        self.assertEqual(twr["parent"], "")
        self.assertEqual(twr["name"], "Needs: Artemis Path")
        ism = next(row for row in rows if row["text"] == "In Stellar Multitudes")
        self.assertEqual(ism["parent"], "")
        self.assertEqual(ism["name"], "Needs: They Who Returned, Atlas Path")
        self.assertIn("blocked until: Artemis Path", twr["status"])
        self.assertEqual(twr["mission_id"], "")
        self.assertEqual(twr["next_id"], "^ROBOT_INTRO")
        self.assertEqual(twr["unfinished_ids"][0], "^ROBOT_INTRO")
        self.assertIn("^ROBOT_INTRO", twr["step_ids"])
        from nmsmissions.gui import button_labels, quest_line_prompt

        kind, _title, body = quest_line_prompt("finish", twr, True)
        self.assertEqual(kind, "confirm")
        self.assertEqual(len(twr["main_unfinished"]), 14)
        self.assertEqual(len(twr["optional_unfinished"]), 5)
        self.assertIn("14 main steps (+5 optional)", body)
        self.assertIn("They Who Returned", body)
        self.assertIn("in order", body)
        self.assertIn("grant their rewards", body)
        kind, _title, body = quest_line_prompt("finish", twr, False)
        self.assertIn("rewards are not added", body)
        kind, title, body = quest_line_prompt("unlock", twr, True)
        self.assertEqual(kind, "go")
        self.assertEqual(twr["next_id"], "^ROBOT_INTRO")
        kind, _title, body = quest_line_prompt("reset", twr, True)
        self.assertEqual(kind, "info")
        started = dict(twr)
        started["open_ids"] = ["^ROBOT_INTRO"]
        kind, _title, body = quest_line_prompt("reset", started, True)
        self.assertEqual(kind, "confirm")
        self.assertIn("1 step of They Who Returned", body)
        self.assertIn("already granted", body)
        self.assertEqual(button_labels(True)["finish"], "Finish whole quest line")
        self.assertEqual(button_labels(False)["finish"], "Finish this mission")

    def test_finished_line_ignores_steps_that_cannot_change(self):
        from nmsmissions.gui import FINISHED_LINE, quest_line_prompt

        chain = next(item for item in self.chains if item.chain_id == "twr")
        done = []
        for step in chain.steps:
            final = self.catalog[step.mission_id]
            if final == 0:
                done.append((step.mission_id, 0))
            elif not step.optional and final not in (-1, 0):
                done.append((step.mission_id, final))
        roots, other = build_views(self.chains, _entries(done), self.catalog, None, {})
        node = next(item for item in flatten_chains(roots) if item.chain_id == "twr")
        self.assertTrue(node.complete)
        row = next(item for item in forest_rows(roots, other) if item["text"] == "They Who Returned")
        self.assertEqual(row["progress"], "14/14")
        self.assertEqual(row["unfinished_ids"], [])
        self.assertEqual(row["next_id"], "")
        self.assertNotIn("^ROBOM_HOPE", row["unfinished_ids"])
        self.assertNotIn("^ROBOM_ATLAS3_H", row["unfinished_ids"])
        self.assertNotIn("^ROBOM_NADAR2", row["unfinished_ids"])
        kind, _title, body = quest_line_prompt("finish", row, True)
        self.assertEqual(kind, "info")
        self.assertIn("already finished", body)
        self.assertEqual(FINISHED_LINE, "This quest line is already finished.")

        partial = [("^ROBOT_INTRO", self.catalog["^ROBOT_INTRO"]), ("^ROBOM_HOPE", 0)]
        roots, other = build_views(self.chains, _entries(partial), self.catalog, None, {})
        row = next(item for item in forest_rows(roots, other) if item["text"] == "They Who Returned")
        self.assertEqual(row["progress"], "1/14")
        self.assertEqual(len(row["main_unfinished"]), 13)
        self.assertNotIn("^ROBOM_HOPE", row["optional_unfinished"])
        self.assertEqual(len(row["optional_unfinished"]), 4)
        kind, _title, body = quest_line_prompt("finish", row, True)
        self.assertEqual(kind, "confirm")
        self.assertIn("Finish 13 main steps (+4 optional)", body)
        self.assertNotEqual(row["next_id"], "^ROBOM_HOPE")


class GuardAndVerifyTests(unittest.TestCase):
    def test_live_folder_shapes(self):
        live = "C:" + "\\" + "Users" + "\\" + "x" + "\\" + "AppData" + "\\" + "HelloGames" + "\\" + "NMS" + "\\" + "save2.hg"
        self.assertTrue(is_live_nms_save(live))
        self.assertTrue(is_live_nms_save("/tmp/HelloGames/NMS/save.hg"))
        self.assertFalse(is_live_nms_save("/tmp/copies/save.hg"))

    def test_cli_refuses_live_folder_without_override(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "HelloGames" / "NMS"
            live.mkdir(parents=True)
            path = live / "save.hg"
            blob = write_chunked(path, [("^ACT1_STEP1", 1)])
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            code, _out, err = run_cli(["list", str(path), "--mapping", str(mapping)])
            self.assertEqual(code, 1)
            self.assertIn("Refusing", err)
            self.assertEqual(path.read_bytes(), blob)
            code, out, err = run_cli(["list", str(path), "--mapping", str(mapping), "--i-know", "--brief"])
            self.assertEqual(code, 0, err)
            self.assertIn(">>> NEXT", out)
            self.assertEqual(path.read_bytes(), blob)

    def test_verify_does_not_touch_the_source(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            blob = write_chunked(path, [("^ACT1_STEP1", 7), ("^NOT_A_CHAIN", 4)])
            before_mtime = path.stat().st_mtime_ns
            ok, message = verify_save(path)
            self.assertTrue(ok, message)
            self.assertIn("did not modify", message)
            self.assertIn("json loads/dumps round trip ok", message)
            self.assertIn("deobfuscate/re-obfuscate skipped (no mapping)", message)
            self.assertEqual(path.read_bytes(), blob)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_diff_purple_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "save1.hg"
            right = Path(tmp) / "save2.hg"
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(left, [("^ACT1_STEP1", 7)], purple=True, unlocked=True)
            write_chunked(right, [("^ACT1_STEP1", 1)], purple=False, unlocked=False, current="^ACT1_STEP1")
            code, out, err = run_cli(
                ["diff", str(left), str(right), "--mapping", str(mapping), "--missions-only"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("PurpleSystemsUnlocked", out)
            self.assertIn("HasDiscoveredPurpleSystems", out)
            self.assertIn("True -> False", out)
            self.assertIn("Story flags are included", out)
            self.assertNotIn("warning: these look like different games", out)
            id_lines = [line for line in out.splitlines() if "CurrentMissionID" in line]
            self.assertEqual(len(id_lines), 1)
            self.assertIn("CurrentMissionID [", id_lines[0])
            self.assertNotIn("CurrentMissionID:", out)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.chains = load_chains()
        self.catalog = load_completion_catalog()

    def test_comms_missions_are_optional_on_atlas_not_twr(self):
        by_id = {chain.chain_id: chain for chain in self.chains}
        atlas_ids = [step.mission_id for step in by_id["atlas"].steps]
        twr_ids = [step.mission_id for step in by_id["twr"].steps]
        self.assertEqual(atlas_ids[-2:], ["^POI_ATLAS_COMMS", "^SENTMISS_ATLAS"])
        self.assertNotIn("^POI_ATLAS_COMMS", twr_ids)
        self.assertNotIn("^SENTMISS_ATLAS", twr_ids)
        self.assertIn("^POI_MEMORYBOAT", twr_ids)
        for chain in self.chains:
            for step in chain.steps:
                if "LORE" in step.mission_id:
                    self.assertTrue(step.optional, step.mission_id)

    def test_lore_and_zero_completion_do_not_block_atlas(self):
        code, label = mission_status(0, 0)
        self.assertEqual((code, label), ("reached", "reached/unknown"))
        code, label = mission_status(-1, 0)
        self.assertEqual(code, "not_started")
        atlas = next(chain for chain in self.chains if chain.chain_id == "atlas")
        done = []
        for step in atlas.steps:
            if step.optional or self.catalog[step.mission_id] in (-1, 0):
                done.append((step.mission_id, -1 if step.optional else 0))
            else:
                done.append((step.mission_id, self.catalog[step.mission_id]))
        roots, _other = build_views(self.chains, _entries(done), self.catalog, None, {})
        view = {node.chain_id: node for node in flatten_chains(roots)}["atlas"]
        self.assertIsNone(view.next_step)
        self.assertTrue(view.complete)
        self.assertTrue(view.state_label.startswith("complete"))
        lore = next(step for step in view.steps if step.mission_id == "^ATLAS_LORE")
        self.assertEqual(lore.status, "not_started")
        seed = next(step for step in view.steps if step.mission_id == "^HAS_STARSEED")
        self.assertEqual(seed.status_label, "reached/unknown")
        self.assertNotEqual(seed.status, "done")

    def test_partial_chain_counts_required_steps(self):
        artemis = next(chain for chain in self.chains if chain.chain_id == "artemis")
        first = artemis.steps[0].mission_id
        roots, other = build_views(
            self.chains,
            _entries([(first, self.catalog[first]), ("^SIDE_ONLY", 1)]),
            self.catalog,
            None,
            {"^ACT1_STEP1": "Awakenings"},
        )
        view = {node.chain_id: node for node in flatten_chains(roots)}["artemis"]
        self.assertIn("in progress", view.state_label)
        self.assertIn("/", view.state_label)
        text = format_forest(roots, other)
        self.assertIn("Awakenings", text)
        step2 = next(line for line in text.splitlines() if line.strip().startswith("^ACT1_STEP2"))
        self.assertIn("Artemis Path - step 2", step2)
        self.assertNotRegex(step2, r"\^ACT1_STEP2\s+-\s+")
        side = next(line for line in text.splitlines() if "^SIDE_ONLY" in line)
        self.assertIn("Side Only (id)", side)
        brief = format_forest(roots, other, brief=True)
        self.assertNotIn("^SIDE_ONLY", brief)
        self.assertIn("1 mission. Use list without --brief", brief)
        self.assertIn(">>> NEXT", brief)
        self.assertIn("Artemis Path - step 2", brief)
        named = next(row for row in forest_rows(roots, other) if row["text"] == "^ACT1_STEP1")
        self.assertEqual(named["name"], "Awakenings")
        blank = next(row for row in forest_rows(roots, other) if row["text"] == "^SIDE_ONLY")
        self.assertEqual(blank["name"], "Side Only (id)")

    def test_finished_step_keeps_its_friendly_name(self):
        catalog = self.catalog
        done = [
            ("^ROBOT_INTRO", catalog["^ROBOT_INTRO"]),
            ("^ROBOMISS_0", catalog["^ROBOMISS_0"]),
            ("^POI_MEMORYBOAT", 3),
        ]
        roots, _other = build_views(
            self.chains,
            _entries(done),
            catalog,
            None,
            {"^ROBOMISS_0": "Audience with the Autophage"},
        )
        view = {node.chain_id: node for node in flatten_chains(roots)}["twr"]
        named = next(step for step in view.steps if step.mission_id == "^ROBOMISS_0")
        self.assertEqual(named.status, "done")
        self.assertEqual(named.title, "Audience with the Autophage")
        self.assertNotEqual(named.title, "Robomiss 0 (id)")
        roots, _other = build_views(self.chains, _entries(done), catalog, None, {})
        view = {node.chain_id: node for node in flatten_chains(roots)}["twr"]
        blank = next(step for step in view.steps if step.mission_id == "^ROBOMISS_0")
        self.assertEqual(blank.title, "They Who Returned - step 2")
        self.assertNotIn("(id)", blank.title or "")
        boat = next(step for step in view.steps if step.mission_id == "^POI_MEMORYBOAT")
        self.assertEqual(boat.progress, 3)
        self.assertEqual(boat.complete, 2)
        self.assertEqual(progress_cell(boat.progress, boat.complete), "2/2")
        row = next(item for item in forest_rows(roots, _other) if item["text"] == "^POI_MEMORYBOAT")
        self.assertEqual(row["progress"], "2/2")

    def test_high_bytes_in_strings_load_and_verify(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            mapping_path = Path(tmp) / "mapping.json"
            mapping_path.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            body = json.dumps(obfuscate(document([("^ACT1_STEP1", 1)]))).encode("utf-8")
            raw = body[:-1] + b',"b2n":"product\x80\xff"}' + b"\x00"
            blob = pack_chunks(raw, chunk_size=64)
            path.write_bytes(blob)
            payload = unpack_save(blob)
            with self.assertRaises(UnicodeDecodeError):
                payload.raw.rstrip(b"\x00").decode("utf-8")
            save = load_save(path, MAPPING, "fixture")
            self.assertEqual(
                save.data["b2n"].encode("utf-8", errors="surrogateescape"),
                b"product\x80\xff",
            )
            ok, message = verify_save(path, mapping=MAPPING)
            self.assertTrue(ok, message)
            self.assertIn("json loads/dumps round trip ok", message)
            self.assertIn("deobfuscate/re-obfuscate round trip ok", message)
            self.assertEqual(path.read_bytes(), blob)
            code, out, err = run_cli(["list", str(path), "--mapping", str(mapping_path), "--brief"])
            self.assertEqual(code, 0, err)
            self.assertIn("Friendly Name", out)
            self.assertNotIn("^SIDE_ONLY", out)

    def test_flags_are_grouped_by_context(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            mapping_path = Path(tmp) / "mapping.json"
            mapping_path.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(path, [("^ACT1_STEP1", 1)], purple=True)
            code, out, err = run_cli(["flags", str(path), "--mapping", str(mapping_path)])
            self.assertEqual(code, 0, err)
            self.assertIn("  BaseContext", out)
            self.assertIn("HasDiscoveredPurpleSystems", out)

    def test_diff_warns_when_save_slots_differ(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "save6.hg"
            right = Path(tmp) / "save7.hg"
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(left, [("^ACT1_STEP1", 7)], purple=True, unlocked=True)
            write_chunked(right, [("^ACT1_STEP1", 1)], purple=False, unlocked=False, current="^ACT1_STEP1")
            code, out, err = run_cli(["diff", str(left), str(right), "--mapping", str(mapping)])
            self.assertEqual(code, 0, err)
            self.assertIn("warning: these look like different games", out)
            self.assertIn("save slot 3 vs 4", out)
            self.assertIn("save1/2 = slot 1, save3/4 = slot 2", out)
            id_lines = [line for line in out.splitlines() if "CurrentMissionID" in line]
            self.assertEqual(len(id_lines), 1)
            self.assertIn("CurrentMissionID [", id_lines[0])
            self.assertNotIn("CurrentMissionID:", out)
            self.assertIn("Artemis Path - step 1", out)

    def test_repeated_titles_get_a_step_number(self):
        names = {f"^ATLAS{index}": "The Atlas Path" for index in range(1, 12)}
        names["^PURPM1"] = "In Stellar Multitudes"
        names["^PURPM2"] = "In Stellar Multitudes"
        names["^PURPM3"] = "In Stellar Multitudes"
        roots, _other = build_views(self.chains, {}, self.catalog, None, names)
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        atlas = {step.mission_id: step.title for step in flat["atlas"].steps}
        self.assertEqual(atlas["^ATLAS3"], "The Atlas Path (3)")
        self.assertEqual(atlas["^ATLAS1"], "The Atlas Path (1)")
        self.assertIn("Atlas Path - step ", atlas["^ATLAS_LORE"])
        self.assertNotIn("(id)", atlas["^ATLAS_LORE"])
        ism = {step.mission_id: step.title for step in flat["ism"].steps}
        self.assertEqual(ism["^PURPM3"], "In Stellar Multitudes (3)")
        named = {
            "^ATLAS1": "The Atlas Path (The Dust of Creation)",
            "^ATLAS2": "The Atlas Path (The Second Interface)",
            "^ATLAS3": "The Atlas Path",
            "^ATLAS4": "The Atlas Path",
        }
        roots, _other = build_views(self.chains, {}, self.catalog, None, named)
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        titled = {step.mission_id: step.title for step in flat["atlas"].steps}
        self.assertEqual(titled["^ATLAS1"], "The Atlas Path (The Dust of Creation)")
        self.assertEqual(titled["^ATLAS3"], "The Atlas Path (1)")
        self.assertEqual(titled["^ATLAS4"], "The Atlas Path (2)")
        self.assertEqual(tidy_mission_id("^DROPPOD_GUIDE"), "Droppod Guide (id)")
        self.assertEqual(tidy_mission_id("^WEAPGUY_LORE"), "Weapguy Lore (id)")

    def test_subtitles_are_dropped_when_they_do_not_uniquify(self):
        base = {f"^ATLAS{index}": "The Atlas Path" for index in range(1, 5)}
        distinct = {
            "^ATLAS1": "The Dust of Creation",
            "^ATLAS2": "The Second Interface",
            "^ATLAS3": "The Third Interface",
            "^ATLAS4": "The Fourth Interface",
        }
        roots, _other = build_views(self.chains, {}, self.catalog, None, base, distinct)
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        titled = {step.mission_id: step.title for step in flat["atlas"].steps}
        self.assertEqual(titled["^ATLAS1"], "The Atlas Path (The Dust of Creation)")
        self.assertNotIn("(1)", titled["^ATLAS1"])
        self.assertEqual(titled["^ATLAS3"], "The Atlas Path (The Third Interface)")
        shared = {f"^ATLAS{index}": "The Dust of Creation" for index in range(1, 5)}
        roots, _other = build_views(self.chains, {}, self.catalog, None, base, shared)
        flat = {node.chain_id: node for node in flatten_chains(roots)}
        numbered = {step.mission_id: step.title for step in flat["atlas"].steps}
        self.assertEqual(numbered["^ATLAS1"], "The Atlas Path (1)")
        self.assertEqual(numbered["^ATLAS3"], "The Atlas Path (3)")
        self.assertNotIn("Dust", numbered["^ATLAS1"])

    def test_unique_names_are_not_numbered_by_chain_position(self):
        steps = tuple(StepDef(f"^S{index}") for index in range(1, 31))
        solo = [
            ChainDef(
                chain_id="long",
                title="Long Chain",
                group="story",
                requires=(),
                steps=steps,
            )
        ]
        names = {f"^S{index}": f"Chapter {index}" for index in range(1, 31)}
        names["^S1"] = "Awakenings"
        names["^S29"] = "The Purge"
        names["^S30"] = "The Purge"
        roots, _other = build_views(solo, {}, {}, None, names)
        titled = {step.mission_id: step.title for step in roots[0].steps}
        self.assertEqual(titled["^S1"], "Awakenings")
        self.assertEqual(titled["^S29"], "The Purge (1)")
        self.assertEqual(titled["^S30"], "The Purge (2)")

    def test_repeated_other_names_include_the_tidied_id(self):
        roots, other = build_views(
            self.chains,
            _entries([("^DROP_A", 1), ("^DROP_B", 1), ("^ONLY_ONE", 1)]),
            {},
            None,
            {
                "^DROP_A": "The Space Anomaly",
                "^DROP_B": "The Space Anomaly",
                "^ONLY_ONE": "A One-Off Visit",
            },
        )
        titled = {step.mission_id: step.title for step in other.steps}
        self.assertEqual(titled["^DROP_A"], "The Space Anomaly — Drop A (id)")
        self.assertEqual(titled["^DROP_B"], "The Space Anomaly — Drop B (id)")
        self.assertEqual(titled["^ONLY_ONE"], "A One-Off Visit")
        self.assertTrue(roots)

    def test_one_blank_title_uses_a_tidied_id(self):
        solo = [
            ChainDef(
                chain_id="solo",
                title="Solo Chain",
                group="story",
                requires=(),
                steps=(StepDef("^ONLY"), StepDef("^NAMED")),
            )
        ]
        roots, _other = build_views(solo, {}, {}, None, {"^NAMED": "A Real Log Title"})
        titles = [step.title for step in roots[0].steps]
        self.assertEqual(titles, ["Solo Chain - step 1", "A Real Log Title"])

    def test_slash_token_other_cap_and_retired_progress(self):
        from nmsmissions.chains import OTHER_NAME_LIMIT, polish_name

        self.assertEqual(polish_name("Open the SLASH menu %SLASH% %PLANET%"), "Open the / menu / …")
        code, label = mission_status(2147483647, 6)
        self.assertEqual((code, label), ("done", "done (retired)"))
        long_name = "The Space Anomaly " + ("region " * 12)
        _roots, other = build_views(
            self.chains,
            _entries([("^DROP_A", 1), ("^DROP_B", 1)]),
            {},
            None,
            {"^DROP_A": long_name, "^DROP_B": long_name},
        )
        for step in other.steps:
            self.assertLessEqual(len(step.title or ""), OTHER_NAME_LIMIT)
            self.assertIn("(id)", step.title or "")

    def test_filter_keeps_dependent_chains(self):
        roots, other = build_views(self.chains, {}, self.catalog, None, {})
        rows = forest_rows(roots, other)
        shown = {row["text"] for row in matching_rows(rows, "They Who Returned")}
        self.assertIn("In Stellar Multitudes", shown)
        self.assertIn("^PURPM1", shown)
        self.assertNotIn("Dreams of the Deep", shown)
        via_atlas = {row["text"] for row in matching_rows(rows, "Atlas Path")}
        self.assertIn("^PURPM1", via_atlas)

    def test_every_builtin_line_is_top_level(self):
        roots, other = build_views(self.chains, {}, self.catalog, None, {})
        rows = forest_rows(roots, other)
        by_id = {
            row["chain_id"]: row
            for row in rows
            if row.get("chain_id") and not row.get("mission_id")
        }
        for chain in self.chains:
            row = by_id[chain.chain_id]
            self.assertEqual(row["parent"], "", chain.title)
        self.assertEqual(by_id["twr"]["text"], "They Who Returned")
        self.assertEqual(by_id["ism"]["text"], "In Stellar Multitudes")
        self.assertEqual(by_id["twr"]["name"], "Needs: Artemis Path")
        self.assertIn("Atlas Path", by_id["ism"]["name"])

    def test_groups_start_collapsed_and_find_opens_matches(self):
        import time
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.gui import launch

        roots, other = build_views(self.chains, {}, self.catalog, None, {})

        def reload_cb():
            return roots, other, "copy"

        def walk(widget):
            found = [widget]
            for child in widget.winfo_children():
                found.extend(walk(child))
            return found

        window = tk.Tk()
        window.withdraw()
        window.mainloop = lambda: None
        try:
            launch("Missions", "copy", roots, other, window=window, reload_cb=reload_cb)
            widgets = walk(window)
            tree = next(widget for widget in widgets if widget.winfo_class() == "Treeview")
            buttons = {
                widget.cget("text"): widget
                for widget in widgets
                if widget.winfo_class() == "TButton"
            }
            entry = next(widget for widget in widgets if widget.winfo_class() == "TEntry")
            self.assertIn("Expand all", buttons)
            self.assertIn("Collapse all", buttons)
            for chain_id in ("artemis", "atlas", "twr", "ism"):
                self.assertFalse(int(tree.item(f"chain:{chain_id}", "open")))
            tree.focus("chain:twr")
            tree.item("chain:twr", open=True)
            tree.event_generate("<<TreeviewOpen>>")
            window.update_idletasks()
            window._reload_rows()
            from nmsmissions.gui import finish_reload

            finish_reload(window)
            self.assertTrue(int(tree.item("chain:twr", "open")))
            self.assertFalse(int(tree.item("chain:artemis", "open")))
            self.assertFalse(int(tree.item("chain:ism", "open")))
            entry.insert(0, "^PURPM1")
            window.update_idletasks()
            self.assertEqual(tree.selection(), ("chain:ism/^PURPM1",))
            self.assertTrue(int(tree.item("chain:ism", "open")))
            self.assertFalse(tree.exists("chain:artemis"))
            entry.delete(0, "end")
            window.update_idletasks()
            self.assertTrue(int(tree.item("chain:twr", "open")))
            self.assertFalse(int(tree.item("chain:ism", "open")))
            started = time.perf_counter()
            buttons["Expand all"].invoke()
            expand_ms = (time.perf_counter() - started) * 1000.0
            self.assertTrue(int(tree.item("chain:artemis", "open")))
            self.assertTrue(int(tree.item("chain:ism", "open")))
            buttons["Collapse all"].invoke()
            collapse_ms = (time.perf_counter() - started) * 1000.0
            self.assertLess(expand_ms, 100.0)
            self.assertLess(collapse_ms, 100.0)
            self.assertFalse(int(tree.item("chain:artemis", "open")))
            self.assertFalse(int(tree.item("chain:twr", "open")))
            window._reload_rows()
            finish_reload(window)
            self.assertFalse(int(tree.item("chain:twr", "open")))
        finally:
            window.destroy()

    def test_story_lines_are_top_level_and_helpers_group(self):
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.gui import is_quest_line, paint_rows, quest_line_prompt

        roots, other = build_views(
            self.chains,
            _entries(
                [
                    ("^PURPM3_NADA", 1),
                    ("^PURPM_LORE", 0),
                    ("^ROBOM2_VISIT", 2),
                    ("^SENTMISS_D_LOOT", 1),
                    ("^SENTMISS_W_LOOT", 1),
                    ("^NOT_A_HELPER", 1),
                    ("^POI_UNRELATED", 1),
                ]
            ),
            self.catalog,
            None,
            {},
        )
        rows = forest_rows(roots, other)
        by_text = {row["text"]: row for row in rows}
        self.assertEqual(by_text["They Who Returned"]["parent"], "")
        self.assertEqual(by_text["In Stellar Multitudes"]["parent"], "")
        self.assertEqual(by_text["Artemis Path"]["parent"], "")
        self.assertEqual(by_text["Artemis Path"]["name"], "-")
        artemis_children = [row["text"] for row in rows if row["parent"] == "chain:artemis"]
        self.assertNotIn("They Who Returned", artemis_children)
        self.assertIn("^ACT1_STEP1", artemis_children)
        nada = by_text["^PURPM3_NADA"]
        lore = by_text["^PURPM_LORE"]
        robot = by_text["^ROBOM2_VISIT"]
        self.assertEqual(nada["parent"], "chain:ism/extra")
        self.assertEqual(lore["parent"], "chain:ism/extra")
        self.assertEqual(robot["parent"], "chain:twr/extra")
        extra_rows = [row for row in rows if row["text"] == "Extra steps (optional)"]
        self.assertEqual({row["iid"] for row in extra_rows}, {"chain:ism/extra", "chain:twr/extra"})
        self.assertIn("optional", nada["status"])
        self.assertEqual(nada["chain_id"], "")
        self.assertEqual(by_text["^NOT_A_HELPER"]["parent"], "chain:other")
        self.assertEqual(by_text["^POI_UNRELATED"]["parent"], "chain:other")
        self.assertEqual(by_text["^SENTMISS_D_LOOT"]["parent"], "chain:other")
        self.assertEqual(by_text["^SENTMISS_W_LOOT"]["parent"], "chain:other")
        twr = by_text["They Who Returned"]
        self.assertNotIn("^ROBOM2_VISIT", twr["step_ids"])
        self.assertTrue(is_quest_line(twr))
        self.assertFalse(any(is_quest_line(row) for row in extra_rows))
        kind, _title, body = quest_line_prompt("finish", twr, True)
        self.assertEqual(kind, "confirm")
        self.assertIn("They Who Returned", body)
        shared = [
            ChainDef("one", "One Line", "story", (), (StepDef("^FAMILY1"), StepDef("^FAMILY2"))),
            ChainDef("two", "Two Line", "story", (), (StepDef("^SENTMISS_ATLAS"),)),
        ]
        _roots, shared_other = build_views(
            shared,
            _entries(
                [
                    ("^FAMILY_EXTRA", 1),
                    ("^SENTMISS_D_LOOT", 1),
                    ("^SHARED9", 1),
                ]
            ),
            {},
            None,
            {},
        )
        shared_rows = {row["text"]: row for row in forest_rows(_roots, shared_other)}
        self.assertEqual(shared_rows["^FAMILY_EXTRA"]["parent"], "chain:one/extra")
        self.assertEqual(shared_rows["^SENTMISS_D_LOOT"]["parent"], "chain:other")
        self.assertEqual(shared_rows["^SHARED9"]["parent"], "chain:other")
        window = tk.Tk()
        window.withdraw()
        try:
            tree = ttk.Treeview(window, columns=("name", "status", "progress"))
            paint_rows(tree, rows)
            top = list(tree.get_children(""))
            self.assertIn("chain:twr", top)
            self.assertIn("chain:ism", top)
            self.assertNotIn("chain:twr", tree.get_children("chain:artemis"))
            self.assertIn("chain:ism/extra", tree.get_children("chain:ism"))
            self.assertEqual(tree.item("chain:twr")["values"][0], "Needs: Artemis Path")
        finally:
            window.destroy()

    def test_json_includes_chain_counts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(path, [("^ACT1_STEP1", 7)])
            code, out, err = run_cli(["list", str(path), "--mapping", str(mapping), "--json", "--chain", "artemis"])
            self.assertEqual(code, 0, err)
            document = json.loads(out)
            chain = document["chains"][0]
            self.assertEqual(chain["id"], "artemis")
            self.assertIn("state_label", chain)
            self.assertIn("in progress", chain["state_label"])
            self.assertEqual(chain["done"], 1)
            self.assertGreater(chain["required"], 1)

    def test_play_time_uses_total_play_time_not_time_alive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "save1.hg"
            right = Path(tmp) / "save2.hg"
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(left, [("^ACT1_STEP1", 1)], total_play_time=1000, time_alive=424242)
            write_chunked(right, [("^ACT1_STEP1", 1)], total_play_time=20000, time_alive=424242)
            code, out, err = run_cli(["diff", str(left), str(right), "--mapping", str(mapping)])
            self.assertEqual(code, 0, err)
            self.assertIn("1000 s", out)
            self.assertIn("20000 s", out)
            self.assertNotIn("424242", out)
            self.assertIn("play time differs by more than an hour", out)

    def test_auto_pick_uses_the_newer_paired_save(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            older = folder / "save7.hg"
            newer = folder / "save8.hg"
            mapping = folder / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(older, [("^ACT1_STEP1", 1)])
            write_chunked(newer, [("^ACT1_STEP1", 1)])
            os.utime(older, (1, 1_000))
            os.utime(newer, (1, 2_000))
            chosen, note = choose_save_file(older)
            self.assertEqual(chosen, newer)
            self.assertIn("save8.hg", note or "")
            code, out, err = run_cli(["list", str(older), "--mapping", str(mapping), "--brief"])
            self.assertEqual(code, 0, err)
            self.assertIn("save8.hg", out)
            self.assertIn("newer than save7.hg", out)
            code, out, err = run_cli(
                ["list", str(older), "--mapping", str(mapping), "--brief", "--no-auto-save"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn(str(older), out)
            self.assertNotIn("newer than", out)
            code, out, err = run_cli(
                ["list", str(folder), "--mapping", str(mapping), "--brief", "--save-slot", "4"]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("save8.hg", out)
            self.assertIn("save slot 4", out)
            code, _out, err = run_cli(["list", str(folder), "--mapping", str(mapping), "--save-slot", "0"])
            self.assertEqual(code, 1)
            self.assertIn("Save slot must be 1 or greater", err)


class GameNameTests(unittest.TestCase):
    def test_exml_links_a_title_and_skips_binary(self):
        import tempfile

        from nmsmissions.gamenames import load_game_names

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.EXML").write_text(
                """<Data>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="ROBOMISS_TITLE_1" />
                    <Property name="English" value="Audience with the Autophage" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="RAW_ID" />
                    <Property name="English" value="STILL_A_KEY" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="TAGGED" />
                    <Property name="English" value="Hello &lt;IMG&gt;there" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (missions / "story.EXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ROBOMISS_0" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="ROBOMISS_TITLE_%d" />
                      <Property name="Count" value="3" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^NO_TITLE" />
                    <Property name="MissionPageLocID" value="RAW_ID" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (loc / "NMS_LOC4_ENGLISH.MBIN").write_bytes(b"MBIN\x00\x80not text")
            cache = root / "names-cache.json"
            report = load_game_names(root, cache=cache)
            self.assertEqual(report.names.get("^ROBOMISS_0"), "Audience with the Autophage")
            self.assertEqual(report.names.get("ROBOMISS_0"), "Audience with the Autophage")
            self.assertEqual(report.unique_titles(), 1)
            self.assertIn("1 title,", report.summary())
            self.assertNotIn("^NO_TITLE", report.names)
            self.assertEqual(report.binary_skipped, 1)
            self.assertIn("Skipped 1 binary MBIN", report.summary())
            self.assertFalse(report.from_cache)
            again = load_game_names(root, cache=cache)
            self.assertTrue(again.from_cache)
            self.assertEqual(again.names.get("^ROBOMISS_0"), "Audience with the Autophage")
            self.assertIn("Loaded from cache", again.summary())
            self.assertNotIn("--game-files", report.summary())

    def test_nested_english_and_descriptions_count_unnamed(self):
        import tempfile

        from nmsmissions.gamenames import load_game_names

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.MXML").write_text(
                """<Data>
                  <Property>
                    <Property name="Id" value="VariableSizeString.xml">
                      <Property name="Value" value="AWAKE" />
                    </Property>
                    <Property name="English" value="VariableSizeString.xml">
                      <Property name="Value" value="Awakenings" />
                    </Property>
                  </Property>
                  <Property>
                    <Property name="Id"><Property name="Value" value="DESC_ONE" /></Property>
                    <Property name="English"><Property name="Value" value="Walk to the ship. Then look up." /></Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (loc / "NMS_LOC2_ENGLISH.MXML").write_text(
                """<Data>
                  <Property>
                    <Property name="Id" value="SECOND" />
                    <Property name="English" value="The Second Step" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (loc / "NMS_LOC1_USENGLISH.MXML").write_text(
                """<Data>
                  <Property>
                    <Property name="Id" value="AWAKE" />
                    <Property name="English" value="Wrong Title" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (missions / "story.MXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ACT1_STEP1" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="AWAKE" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ACT1_STEP2" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="" />
                    </Property>
                    <Property name="MissionDescriptions">
                      <Property name="Format" value="DESC_ONE" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ACT1_STEP3" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="SECOND" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^NO_NAME" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="MISSING_KEY" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            report = load_game_names(root)
            self.assertEqual(report.names["^ACT1_STEP1"], "Awakenings")
            self.assertEqual(report.names["^ACT1_STEP2"], "Walk to the ship")
            self.assertEqual(report.names["^ACT1_STEP3"], "The Second Step")
            self.assertNotIn("^NO_NAME", report.names)
            self.assertNotIn("Wrong Title", report.names.values())
            self.assertEqual(report.missions_linked, 4)
            self.assertEqual(report.unnamed, 1)
            self.assertIn("1 still have no name", report.summary())
            self.assertNotIn("--game-files", report.summary())
            print(f"fixture names {report.unique_titles()} unnamed {report.unnamed}")

    def test_chain_steps_keep_step_labels_and_the_banner_counts_the_list(self):
        import tempfile

        from nmsmissions.chains import build_views, flatten_chains, is_generated_title, load_chains, tidy_mission_id
        from nmsmissions.gamenames import display_names, load_game_names
        from nmsmissions.gui import forest_rows

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.MXML").write_text(
                """<Data>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="UI_ROBOMISS_0_COMMS_TITLE" />
                    <Property name="English" value="STARSHIP ALERT" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="AWAKE" />
                    <Property name="English" value="Awakenings" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (missions / "story.MXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ROBOMISS_0" />
                    <Property name="MissionTitles" value="GcNumberedTextList">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                    <Property name="Stages">
                      <Property value="GcGenericMissionStage.xml">
                        <Property name="Stage" value="GcMissionSequenceShowMessage.xml">
                          <Property name="Message" value="MSG_ROBOM_COMMS" />
                          <Property name="OSDMessage" value="" />
                        </Property>
                      </Property>
                    </Property>
                    <Property name="Dialog">
                      <Property value="GcAlienPuzzleEntry.xml">
                        <Property name="Title" value="" />
                      </Property>
                      <Property value="GcAlienPuzzleEntry.xml">
                        <Property name="Title" value="UI_ROBOMISS_0_MISSING" />
                      </Property>
                      <Property value="GcAlienPuzzleEntry.xml">
                        <Property name="Title" value="UI_ROBOMISS_0_COMMS_TITLE" />
                      </Property>
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^PURPM_BOAT" />
                    <Property name="MissionTitles" value="GcNumberedTextList">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^NOT_IN_CHAIN" />
                    <Property name="MissionTitles" value="TkLocalisationEntry">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ACT1_STEP1" />
                    <Property name="MissionTitles" value="GcNumberedTextList">
                      <Property name="Format" value="AWAKE" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            report = load_game_names(root)
            self.assertEqual(report.names["^ACT1_STEP1"], "Awakenings")
            self.assertNotIn("GcNumberedTextList", " ".join(report.names.values()))
            self.assertNotIn("Robomiss 0 (id)", " ".join(report.names.values()))
            titles, subtitles, alerts = display_names(report, {})
            roots, other = build_views(
                load_chains(),
                {"^NOT_IN_CHAIN": {"Progress": 0}},
                {},
                None,
                titles,
                subtitles,
                alerts,
            )
            flat = {node.chain_id: node for node in flatten_chains(roots)}
            ship = next(step for step in flat["twr"].steps if step.mission_id == "^ROBOMISS_0")
            self.assertTrue(ship.title.startswith("They Who Returned - step "))
            self.assertIn("STARSHIP ALERT", ship.title or "")
            self.assertNotIn("(id)", ship.title or "")
            self.assertNotIn("MSG_ROBOM_COMMS", ship.title or "")
            boat = next(step for step in flat["ism"].steps if step.mission_id == "^PURPM_BOAT")
            self.assertTrue(boat.title.startswith("In Stellar Multitudes - step "))
            self.assertNotIn("—", boat.title or "")
            self.assertNotIn("(id)", boat.title or "")
            self.assertNotIn("PURPM_BOAT", boat.title or "")
            self.assertNotIn("^PURPM_BOAT", report.alerts)
            self.assertEqual(report.alerts.get("^ROBOMISS_0"), "STARSHIP ALERT")
            loose = next(step for step in other.steps if step.mission_id == "^NOT_IN_CHAIN")
            self.assertEqual(loose.title, tidy_mission_id("^NOT_IN_CHAIN"))
            rows = forest_rows(roots, other)
            mission_rows = [row for row in rows if row.get("mission_id")]
            line = report.summary_for_list(rows)
            self.assertIn(f"{len(mission_rows)} mission links", line)
            self.assertNotEqual(len(mission_rows), report.missions_linked)
            unnamed = sum(1 for row in mission_rows if is_generated_title(str(row.get("name") or "")))
            self.assertIn(f"{unnamed} still have no name", line)
            self.assertLess(report.missions_linked, len(mission_rows))

    def test_subtitle_objective_tokens_and_duplicate_subtitles(self):
        import os
        import tempfile

        from nmsmissions.gamenames import load_game_names

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_UPDATE3_ENGLISH.EXML").write_text(
                """<Data>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="SUB_MEET" />
                    <Property name="English" value="Meet the Autophage" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="OBJ_SCAN" />
                    <Property name="English" value="Scan the %PLANET% near %SETTLEMENT%" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="ATLAS_TITLE" />
                    <Property name="English" value="The Atlas Path" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="ATLAS_SUB_3" />
                    <Property name="English" value="The Third Interface" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="ATLAS_SUB_4" />
                    <Property name="English" value="The Fourth Interface" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (missions / "story.EXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ROBOMISS_0" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                    <Property name="MissionSubtitles">
                      <Property name="Format" value="SUB_MEET" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^POI_BOAT" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                    <Property name="Stages">
                      <Property value="GcGenericMissionStage.xml">
                        <Property name="Stage" value="GcMissionSequenceGroup.xml">
                          <Property name="DebugText" value="not a title" />
                          <Property name="ObjectiveID" value="" />
                          <Property name="Stages">
                            <Property value="GcMissionSequenceShowMessage.xml">
                              <Property name="Message" value="OBJ_SCAN" />
                            </Property>
                          </Property>
                        </Property>
                      </Property>
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ATLAS3" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="ATLAS_TITLE" />
                      <Property name="Count" value="1" />
                    </Property>
                    <Property name="MissionSubtitles">
                      <Property name="Format" value="ATLAS_SUB_3" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ATLAS4" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="ATLAS_TITLE" />
                      <Property name="Count" value="1" />
                    </Property>
                    <Property name="MissionSubtitles">
                      <Property name="Format" value="ATLAS_SUB_4" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            cache = root / "cache.json"
            report = load_game_names(root, cache=cache)
            self.assertEqual(report.names["^ROBOMISS_0"], "Meet the Autophage")
            self.assertEqual(report.names["^POI_BOAT"], "Scan the … near …")
            self.assertEqual(report.names["^ATLAS3"], "The Atlas Path")
            self.assertEqual(report.names["^ATLAS4"], "The Atlas Path")
            self.assertEqual(report.subtitles["^ATLAS3"], "The Third Interface")
            self.assertEqual(report.subtitles["^ATLAS4"], "The Fourth Interface")
            self.assertEqual(report.unique_titles(), 4)
            (loc / "NMS_UPDATE3_ENGLISH.EXML").write_text(
                (loc / "NMS_UPDATE3_ENGLISH.EXML").read_text(encoding="utf-8").replace(
                    "Meet the Autophage", "Meet them again"
                ),
                encoding="utf-8",
            )
            os.utime(loc / "NMS_UPDATE3_ENGLISH.EXML", (1, 5_000_000_000))
            fresh = load_game_names(root, cache=cache)
            self.assertFalse(fresh.from_cache)
            self.assertEqual(fresh.names["^ROBOMISS_0"], "Meet them again")

    def test_later_mission_reference_does_not_wipe_the_name(self):
        import tempfile

        from nmsmissions.gamenames import CACHE_VERSION, cache_path_for, load_game_names

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.EXML").write_text(
                """<Data>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="ACT_TITLE" />
                    <Property name="English" value="Awakenings" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="BOAT_OBJ" />
                    <Property name="English" value="Scan the wreck" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="INV_FULL" />
                    <Property name="English" value="Inventory Full Clear space in your suit now please" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="MENU_MSG" />
                    <Property name="English" value="Open the QUICK_MENU panel" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="LONG_OBJ" />
                    <Property name="English" value="%s" />
                  </Property>
                </Data>"""
                % ("Walk to the marker and keep going " * 12),
                encoding="utf-8",
            )
            (missions / "story.EXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^ACT1_STEP1" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="ACT_TITLE" />
                      <Property name="Count" value="1" />
                    </Property>
                  </Property>
                  <Property name="GcMissionConditionMissionCompleted">
                    <Property name="MissionID" value="^ACT1_STEP1" />
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^POI_PURPM_BOAT" />
                    <Property name="MissionTitles">
                      <Property name="Format" value="" />
                      <Property name="Count" value="0" />
                    </Property>
                    <Property name="Stages">
                      <Property name="Stage" value="GcMissionSequenceGroup.xml">
                        <Property name="ObjectiveID" value="BOAT_OBJ" />
                        <Property name="Message" value="INV_FULL" />
                      </Property>
                    </Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^SHARED_A" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="Message" value="INV_FULL" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^SHARED_B" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="Message" value="INV_FULL" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^SHARED_C" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="Message" value="INV_FULL" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^MENU_ONE" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="Message" value="MENU_MSG" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^LONG_ONE" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="ObjectiveID" value="LONG_OBJ" /></Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            cache = root / "cache.json"
            report = load_game_names(root, cache=cache)
            self.assertEqual(report.names["^ACT1_STEP1"], "Awakenings")
            self.assertEqual(report.missions_linked, 7)
            self.assertIn("7 mission links", report.summary())
            self.assertEqual(report.names["^POI_PURPM_BOAT"], "Scan the wreck")
            self.assertNotIn("^SHARED_A", report.names)
            self.assertNotIn("Inventory Full", report.names.get("^POI_PURPM_BOAT", ""))
            self.assertEqual(report.names["^MENU_ONE"], "Open the panel")
            self.assertNotIn("QUICK_MENU", report.names["^MENU_ONE"])
            self.assertLessEqual(len(report.names["^LONG_ONE"]), 60)
            self.assertTrue(report.names["^LONG_ONE"].endswith("…"))
            from nmsmissions import __version__

            cached = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(__version__, "1.1.2")
            self.assertEqual(cached["tool"], "1.1.2")
            self.assertEqual(cached["version"], CACHE_VERSION)
            cached["source"] = "0" * 16
            cached["tool"] = "0.0.0"
            cache.write_text(json.dumps(cached), encoding="utf-8")
            reused = load_game_names(root, cache=cache)
            self.assertTrue(reused.from_cache)
            self.assertEqual(reused.names["^ACT1_STEP1"], "Awakenings")
            stale = {
                "version": CACHE_VERSION - 1,
                "tool": "0.0.0",
                "root": str(root.resolve()),
                "files": [],
                "names": {"^ACT1_STEP1": "WRONG"},
                "files_read": 0,
                "loc_entries": 0,
                "missions_linked": 99,
                "binary_skipped": 0,
            }
            cache.write_text(__import__("json").dumps(stale), encoding="utf-8")
            rebuilt = load_game_names(root, cache=cache)
            self.assertFalse(rebuilt.from_cache)
            self.assertEqual(rebuilt.names["^ACT1_STEP1"], "Awakenings")
            other = Path(tmp) / "other-game"
            other.mkdir()
            self.assertNotEqual(cache_path_for(root), cache_path_for(other))

    def test_fallback_keeps_the_first_line_and_does_not_space_capitals(self):
        import tempfile

        from nmsmissions.gamenames import load_game_names

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc = root / "LANGUAGE"
            missions = root / "METADATA" / "SIMULATION" / "MISSIONS"
            loc.mkdir(parents=True)
            missions.mkdir(parents=True)
            (loc / "NMS_LOC1_ENGLISH.EXML").write_text(
                """<Data>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="RELAY" />
                    <Property name="English" value="Communication relay locked: A T L A S&#10;Answer the Communicator and keep walking" />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="REACH" />
                    <Property name="English" value="Reach the relay. Then answer the communicator and walk much further." />
                  </Property>
                  <Property name="TkLocalisationEntry">
                    <Property name="Id" value="SPEAK" />
                    <Property name="English" value="Speak to ATLAS now" />
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            (missions / "story.EXML").write_text(
                """<Data>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^RELAY" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="ObjectiveID" value="RELAY" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^REACH" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="ObjectiveID" value="REACH" /></Property>
                  </Property>
                  <Property name="GcGenericMissionSequence">
                    <Property name="MissionID" value="^SPEAK" />
                    <Property name="MissionTitles"><Property name="Format" value="" /><Property name="Count" value="0" /></Property>
                    <Property name="Stages"><Property name="ObjectiveID" value="SPEAK" /></Property>
                  </Property>
                </Data>""",
                encoding="utf-8",
            )
            report = load_game_names(root, cache=root / "cache.json")
            self.assertEqual(report.names["^RELAY"], "Communication relay locked: ATLAS")
            self.assertNotIn("A T L A S", report.names["^RELAY"])
            self.assertNotIn("Answer the Communicator", report.names["^RELAY"])
            self.assertEqual(report.names["^REACH"], "Reach the relay")
            self.assertEqual(report.names["^SPEAK"], "Speak to ATLAS now")

    def test_run_gui_builds_gui_arguments(self):
        import importlib.machinery
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "run_gui.pyw"
        loader = importlib.machinery.SourceFileLoader("run_gui", str(path))
        spec = importlib.util.spec_from_loader("run_gui", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(module.command_args([], root), ["gui"])
            save = "D:" + "\\" + "copies" + "\\" + "save2.hg"
            banks = "D:" + "\\" + "nms"
            self.assertEqual(module.command_args([save], root), ["gui", save])
            self.assertEqual(
                module.command_args(["gui", save, "--game-files", banks], root),
                ["gui", save, "--game-files", banks],
            )
            (root / "gamefiles").mkdir()
            (root / "mapping.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                module.command_args([], root),
                ["gui", "--game-files", str(root / "gamefiles"), "--mapping", str(root / "mapping.json")],
            )
        raw = "D:" + "\\\\" + "copies\\\\save2.hg"
        expected = "D:" + "\\" + "copies" + "\\" + "save2.hg"
        self.assertEqual(module.plain_error(raw), expected)

    def test_gui_prints_wait_before_reading_the_save(self):
        import tempfile
        from unittest.mock import patch

        from nmsmissions.gui import PROGRESS_COLUMN

        self.assertGreaterEqual(PROGRESS_COLUMN["minwidth"], 100)
        self.assertTrue(PROGRESS_COLUMN["stretch"])
        calls = []

        def stop(*_args, **_kwargs):
            calls.append("open")
            raise RuntimeError("stop after open")

        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            with patch("nmsmissions.gui.begin_loading", return_value=object()), patch(
                "nmsmissions.gui.set_status"
            ), patch("nmsmissions.cli._open", side_effect=stop):
                code, out, err = run_cli(["gui", str(Path(tmp) / "save.hg"), "--mapping", str(mapping)])
        self.assertEqual(code, 1, err)
        self.assertEqual(calls, ["open"])
        self.assertLess(out.index("Loading missions... please wait"), out.index("Reading save..."))


class WikiTests(unittest.TestCase):
    def test_confirmed_and_guessed_chain_pages(self):
        chains = {chain.chain_id: chain for chain in load_chains()}
        self.assertEqual(
            chains["artemis"].wiki,
            "https://nomanssky.fandom.com/wiki/Artemis_Path",
        )
        self.assertTrue(chains["artemis"].wiki_confirmed)
        self.assertEqual(
            chains["atlas"].wiki,
            "https://nomanssky.fandom.com/wiki/The_Atlas_Path",
        )
        self.assertTrue(chains["twr"].wiki_confirmed)
        self.assertTrue(chains["ism"].wiki_confirmed)
        self.assertFalse(chains["weapons"].wiki_confirmed)
        self.assertEqual(
            chains["weapons"].wiki,
            "https://nomanssky.fandom.com/wiki/Weapons_Specialist",
        )
        roots, other = build_views(load_chains(), {}, load_completion_catalog(), None, {})
        rows = forest_rows(roots, other)
        artemis = next(row for row in rows if row["text"] == "Artemis Path")
        self.assertEqual(artemis["wiki"], chains["artemis"].wiki)
        step = next(row for row in rows if row["text"] == "^ACT1_STEP1")
        self.assertIn("Special:Search?query=Artemis%20Path", step["wiki"])
        named = build_views(
            load_chains(),
            {},
            load_completion_catalog(),
            None,
            {"^ACT1_STEP1": "Awakenings"},
        )[0]
        awake = next(
            step
            for chain in flatten_chains(named)
            for step in chain.steps
            if step.mission_id == "^ACT1_STEP1"
        )
        self.assertIn("query=Awakenings", awake.wiki)

    def test_explicit_mission_page_and_cli(self):
        from unittest.mock import patch

        from nmsmissions.chains import lookup_wiki

        solo = [
            ChainDef(
                chain_id="solo",
                title="Solo",
                group="story",
                requires=(),
                steps=(
                    StepDef(
                        "^AWAKEN",
                        title="Awakenings",
                        wiki="https://nomanssky.fandom.com/wiki/Awakenings",
                        wiki_confirmed=True,
                    ),
                    StepDef("^PLAIN"),
                ),
                wiki="https://nomanssky.fandom.com/wiki/Artemis_Path",
                wiki_confirmed=True,
            )
        ]
        url, note = lookup_wiki("^AWAKEN", solo)
        self.assertEqual(url, "https://nomanssky.fandom.com/wiki/Awakenings")
        self.assertEqual(note, "confirmed page")
        url, note = lookup_wiki("^PLAIN", solo)
        self.assertEqual(note, "search")
        self.assertIn("query=Solo", url)
        code, out, err = run_cli(["wiki", "artemis"])
        self.assertEqual(code, 0, err)
        self.assertIn("https://nomanssky.fandom.com/wiki/Artemis_Path", out)
        self.assertIn("confirmed page", out)
        code, out, err = run_cli(["wiki", "farmer"])
        self.assertIn("unconfirmed page", out)
        with patch("nmsmissions.cli.open_wiki") as opened:
            code, out, err = run_cli(["wiki", "^ACT1_STEP1", "--open"])
            self.assertEqual(code, 0, err)
            opened.assert_called_once()
            self.assertIn("Special:Search", opened.call_args.args[0])

    def test_list_json_includes_wiki(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "save.hg"
            mapping = Path(tmp) / "mapping.json"
            mapping.write_text(
                json.dumps({"Mapping": [{"Key": key, "Value": value} for key, value in MAPPING.items()]})
            )
            write_chunked(path, [("^ACT1_STEP1", 1)])
            code, out, err = run_cli(["list", str(path), "--mapping", str(mapping), "--json", "--chain", "artemis"])
            self.assertEqual(code, 0, err)
            document = json.loads(out)
            chain = document["chains"][0]
            self.assertEqual(chain["wiki"], "https://nomanssky.fandom.com/wiki/Artemis_Path")
            self.assertTrue(chain["wiki_confirmed"])
            step = next(row for row in chain["steps"] if row["id"] == "^ACT1_STEP1")
            self.assertIn("wiki", step)
            self.assertTrue(step["wiki"].startswith("https://nomanssky.fandom.com/wiki/"))


def _entries(pairs):
    return {
        mission_id: {"Mission": mission_id, "Progress": progress} for mission_id, progress in pairs
    }


if __name__ == "__main__":
    unittest.main()

"""Corvette Workshop Cache fill. Synthetic tables and saves only."""

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


import fnmatch
import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nmsmissions.chunks import unpack_save
from nmsmissions.corvette import FILL_LABEL, REREAD, plan_fill, select_parts
from nmsmissions.edit import EditError, EditRequest, EditorSession, build_plan, commit, run, take_snapshot
from nmsmissions.gamedata import TABLES_CACHE_VERSION, load_tables
from nmsmissions.gameread import FILTERS, _keep_extracted
from tests.harness import TkCleanup
from tests.test_edit import _save_body, _slot, _text, _write_pair


def _stack_row(chest: int, capsule: int) -> str:
    values = ["10"] * 13
    values[8] = str(chest)
    values[9] = str(capsule)
    inner = "".join(f"<Property value='{value}'/>" for value in values)
    return (
        "<Property value='GcDifficultyInventoryStackSizeOptionData'>"
        "<Property name='ProductStackLimit' value='9999'/>"
        f"<Property name='MaxProductStackSizes'>{inner}</Property>"
        "</Property>"
    )


def _category(name: str) -> str:
    return (
        "<Property name='CorvettePartCategory' value='GcCorvettePartCategory'>"
        f"<Property name='CorvettePartCategory' value='{name}'/>"
        "</Property>"
    )


def _product(item_id: str, name: str, category: str, group: str = "", multiplier: int = 5) -> str:
    return (
        "<Property value='GcProductData'>"
        f"<Property name='ID' value='{item_id}'/>"
        f"<Property name='Name' value='{name}'/>"
        f"<Property name='NameLower' value='{name.lower()}'/>"
        f"<Property name='GroupID' value='{group}'/>"
        f"<Property name='StackMultiplier' value='{multiplier}'/>"
        f"{_category(category)}"
        "</Property>"
    )


def _entry(
    item_id: str,
    *,
    show: str = "True",
    groups: tuple[str, ...] = (),
    style: str = "None",
    decoration: str = "False",
    structural: str = "False",
    decorative: str = "False",
) -> str:
    grouped = ""
    if groups:
        rows = "".join(
            "<Property value='NMSString0x10'>"
            f"<Property name='Value' value='{group}'/>"
            "</Property>"
            for group in groups
        )
        grouped = f"<Property name='Groups'>{rows}</Property>"
    return (
        "<Property value='GcBaseBuildingEntry'>"
        f"<Property name='ID' value='{item_id}'/>"
        f"<Property name='ShowInBuildMenu' value='{show}'/>"
        f"<Property name='Style' value='{style}'/>"
        f"<Property name='IsDecoration' value='{decoration}'/>"
        f"<Property name='BuildableInShipStructural' value='{structural}'/>"
        f"<Property name='BuildableInShipDecorative' value='{decorative}'/>"
        f"{grouped}"
        "</Property>"
    )


def _english(pairs: list[tuple[str, str]]) -> str:
    rows = []
    for ident, english in pairs:
        rows.append(
            "<Property value='TkLocalisationEntry'>"
            f"<Property name='Id' value='{ident}'/>"
            f"<Property name='English' value='{english}'/>"
            "</Property>"
        )
    return "<Data>" + "".join(rows) + "</Data>"


def _write_tables(folder: Path, *, difficulty: bool = True) -> Path:
    root = folder / "tables"
    (root / "metadata" / "reality" / "tables").mkdir(parents=True)
    (root / "language").mkdir()
    products = [
        _product("B_MAG_1X1", "B_MAG_1X1", "TractorBeam"),
        _product("B_HULL_1", "B_HULL_1", "Hull"),
        _product("B_HULL_MIRROR", "B_HULL_MIRROR", "Hull"),
        _product("B_HULL_AUTO", "B_HULL_AUTO", "Hull"),
        _product("PLANT_BOX", "PLANT_BOX", "Decor", "BIGGS_LOW_PERF"),
        _product("BASE_LAMP", "BASE_LAMP", "Decor"),
        _product("NO_NAME", "NO_NAME", "Hull"),
        _product("B_BUNK", "B_BUNK", "Interior"),
        _product("B_DOOR", "B_DOOR", "Access"),
        _product("B_ALK_D", "B_ALK_D", "None"),
        _product("WIDGET", "WIDGET", "None"),
        _product("CHAIR", "CHAIR", "None", multiplier=1),
        _product("B_STAIR", "B_STAIR", "Interior"),
    ]
    entries = [
        _entry("B_MAG_1X1", decorative="True", groups=("BIGGS_DECOR",)),
        _entry("B_HULL_1", structural="True", groups=("BIGGS_STRUCT",)),
        _entry("B_HULL_MIRROR", show="False", structural="True"),
        _entry("B_HULL_AUTO", show="False", structural="True"),
        _entry("PLANT_BOX", decorative="True", groups=("BIGGS_LOW_PERF",)),
        _entry("BASE_LAMP", decoration="True", structural="False", decorative="False", groups=("BASE_DECOR",)),
        _entry("NO_NAME", structural="True"),
        _entry("B_BUNK", decorative="True", groups=("BIGGS_BUNK",)),
        _entry("B_DOOR", show="False", structural="True"),
        _entry("B_DOOR", show="True", structural="True", groups=("BIGGS_ACCESS",)),
        _entry("B_ALK_D", structural="True", groups=("BIGGS_ACCESS",)),
        _entry("WIDGET", decoration="True", structural="False", decorative="False", groups=("BASE_DECOR",)),
        _entry("CHAIR", decorative="True", structural="False"),
        _entry("B_STAIR", decorative="True"),
        _entry("ONLY_GENERAL", structural="True"),
    ]
    (root / "metadata" / "reality" / "tables" / "nms_basepartproducts.exml").write_text(
        "<Data>" + "".join(products) + "</Data>",
        encoding="utf-8",
    )
    (root / "metadata" / "reality" / "tables" / "nms_reality_gcproducttable.exml").write_text(
        "<Data>" + _product("ONLY_GENERAL", "ONLY_GENERAL", "Hull") + "</Data>",
        encoding="utf-8",
    )
    (root / "metadata" / "reality" / "tables" / "basebuildingobjectstable.exml").write_text(
        "<Data>" + "".join(entries) + "</Data>",
        encoding="utf-8",
    )
    if difficulty:
        (root / "metadata" / "reality" / "tables" / "gcdifficultyconfig.exml").write_text(
            "<Data><Property name='InventoryStackLimitsOptionData'>"
            + _stack_row(50, 999)
            + _stack_row(20, 100)
            + _stack_row(5, 10)
            + "</Property></Data>",
            encoding="utf-8",
        )
    (root / "language" / "nms_loc1_english.exml").write_text(
        _english(
            [
                ("b_mag_1x1", "Tractor Beam"),
                ("B_HULL_1", "Hull"),
                ("b_hull_mirror", "Mirror Hull"),
                ("B_HULL_AUTO", "Auto Hull"),
                ("PLANT_BOX", "Planter"),
                ("BASE_LAMP", "Lamp"),
                ("B_BUNK", "Bunk"),
                ("b_door", "Door"),
                ("B_ALK_D", "Internal Landing Bay"),
                ("WIDGET", "Widget"),
                ("CHAIR", "Wall Light"),
                ("B_STAIR", "Stairs"),
                ("ONLY_GENERAL", "Not A Corvette Part"),
            ]
        ),
        encoding="utf-8",
    )
    (root / "language" / "nms_loc1_usenglish.exml").write_text(
        _english([("NO_NAME", "Hidden Name"), ("b_mag_1x1", "Wrong Beam")]),
        encoding="utf-8",
    )
    return root


def _with_cache(cache: dict, key: str = "wem") -> dict:
    body = _save_body()
    body["vLc"]["6f="][key] = cache
    return body


def _valid(*coords: tuple[int, int]) -> list[dict]:
    return [{"3ZH": {">Qh": x_pos, "XJ>": y_pos}} for x_pos, y_pos in coords]


def _leaves(node: object) -> int:
    if isinstance(node, dict):
        return sum(_leaves(value) for value in node.values())
    if isinstance(node, list):
        return sum(_leaves(value) for value in node)
    return 1


def _request(path: Path, tables: Path, folder: Path, **extra) -> EditRequest:
    return EditRequest(
        action="corvette",
        save=path,
        game_files=tables,
        backup_dir=folder / "backups",
        stack_size=extra.pop("stack_size", 500),
        quiet=True,
        **extra,
    )


# This fixture stands in for the 7.05 list. That install's known-good catalog is 160.
FIXTURE_CATALOG_COUNT = 6


class CatalogTests(unittest.TestCase):
    def test_buildable_parts_come_from_the_base_part_table(self) -> None:
        names = [
            "metadata/reality/tables/nms_basepartproducts.mbin",
            "metadata/reality/tables/basebuildingobjectstable.mbin",
            "metadata\\reality\\tables\\basebuildingobjectstable.mbin",
            "metadata/reality/tables/basebuildingtable.mbin",
            "metadata\\reality\\tables\\basebuildingtable.mbin",
            "metadata/reality/tables/legacybasebuildingtable.mbin",
            "metadata/reality/tables/basebuildingcoststable.mbin",
            "models/planets/biomes/common/buildings/parts/basebuilding/decor/large.mbin",
        ]

        def matched(path: str) -> bool:
            return any(fnmatch.fnmatch(path.lower(), pattern.lower()) for pattern in FILTERS)

        self.assertTrue(matched(names[0]))
        self.assertTrue(matched(names[1]))
        self.assertTrue(matched(names[2]))
        self.assertFalse(matched(names[3]))
        self.assertFalse(matched(names[4]))
        self.assertFalse(matched(names[5]))
        self.assertFalse(matched(names[6]))
        self.assertFalse(matched(names[7]))
        self.assertNotIn("*basebuilding*", FILTERS)
        self.assertFalse(_keep_extracted(Path("models/basebuilding/large_decor.exml")))
        self.assertFalse(_keep_extracted(Path("metadata/reality/tables/legacybasebuildingtable.exml")))
        self.assertFalse(_keep_extracted(Path("metadata/reality/tables/basebuildingtable.mbin.exml")))
        self.assertTrue(_keep_extracted(Path("metadata/reality/tables/nms_basepartproducts.exml")))
        self.assertTrue(_keep_extracted(Path("metadata/reality/tables/basebuildingobjectstable.exml")))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = _write_tables(folder)
            tables = load_tables(root, cache=folder / "cache.json")
            self.assertEqual(TABLES_CACHE_VERSION, 4)
            self.assertFalse(tables.corvette_needs_reread)
            self.assertEqual(tables.corvette_stack_cap, 500)
            found = [(part["id"], part["name"], part["limit"]) for part in tables.corvette_parts]
            self.assertEqual(len(found), FIXTURE_CATALOG_COUNT)
            self.assertEqual(
                found,
                [
                    ("^B_MAG_1X1", "Tractor Beam", 500),
                    ("^B_HULL_1", "Hull", 500),
                    ("^B_BUNK", "Bunk", 500),
                    ("^B_DOOR", "Door", 500),
                    ("^B_ALK_D", "Internal Landing Bay", 500),
                    ("^B_STAIR", "Stairs", 500),
                ],
            )
            self.assertEqual(tables.corvette_parts[0]["category"], "TractorBeam")
            self.assertNotIn("ONLY_GENERAL", {part["id"] for part in tables.corvette_parts})
            self.assertNotIn("Widget", {part["name"] for part in tables.corvette_parts})
            self.assertNotIn("Wall Light", {part["name"] for part in tables.corvette_parts})
            part_cap = tables.corvette_stack_cap
            self.assertTrue(part_cap)
            self.assertTrue(all(part["limit"] == part_cap for part in tables.corvette_parts))
            again = load_tables(root, cache=folder / "cache.json")
            self.assertTrue(again.from_cache)
            self.assertEqual(again.corvette_stack_cap, 500)
            self.assertEqual(again.corvette_parts[4]["name"], "Internal Landing Bay")

    def test_an_old_extract_asks_for_another_read(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = _write_tables(folder)
            part_table = root / "metadata" / "reality" / "tables" / "nms_basepartproducts.exml"
            part_table.unlink()
            tables = load_tables(root)
            self.assertTrue(tables.corvette_needs_reread)
            self.assertEqual(tables.corvette_parts, [])
            self.assertEqual(tables.corvette_stack_cap, 0)

    def test_a_missing_stack_table_is_not_replaced_with_500(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = _write_tables(folder, difficulty=False)
            tables = load_tables(root)
            self.assertEqual(tables.corvette_stack_cap, 0)
            self.assertTrue(tables.corvette_parts)

    def test_an_uncategorized_decoration_is_not_a_corvette_part(self) -> None:
        parts = select_parts(
            [
                {
                    "id": "B_ALK_D",
                    "name_key": "B_ALK_D",
                    "name_lower": "",
                    "category": "None",
                    "group": "",
                    "multiplier": 5,
                },
                {
                    "id": "CHAIR",
                    "name_key": "CHAIR",
                    "name_lower": "",
                    "category": "None",
                    "group": "",
                    "multiplier": 1,
                },
            ],
            [
                {
                    "id": "B_ALK_D",
                    "show": True,
                    "groups": [],
                    "style": "",
                    "decoration": False,
                    "ship_structural": True,
                    "ship_decorative": False,
                },
                {
                    "id": "CHAIR",
                    "show": True,
                    "groups": [],
                    "style": "",
                    "decoration": False,
                    "ship_structural": False,
                    "ship_decorative": True,
                },
            ],
            {"b_alk_d": "Internal Landing Bay", "chair": "Wall Light"},
        )
        self.assertEqual(
            [(part.item_id, part.name, part.multiplier) for part in parts],
            [("^B_ALK_D", "Internal Landing Bay", 5)],
        )

    def test_select_parts_keeps_a_flags_category(self) -> None:
        parts = select_parts(
            [{"id": "B_WING", "name_key": "B_WING", "name_lower": "", "category": "Wing, Hull", "group": ""}],
            [{"id": "B_WING", "show": True, "groups": [], "style": "", "decoration": False, "ship_structural": True, "ship_decorative": False}],
            {"b_wing": "Wing"},
        )
        self.assertEqual([(part.item_id, part.name) for part in parts], [("^B_WING", "Wing")])


class FillTests(unittest.TestCase):
    def _tables(self, folder: Path) -> Path:
        return _write_tables(folder)

    def _occupied_save(self, folder: Path) -> Path:
        held = _slot("^b_hull_1", 7, 10, 0, 0)
        held["eVk"] = 0.25
        held["kept"] = "yes"
        cache = {":No": [held], "hl?": _valid((0, 0), (1, 0), (2, 0))}
        return _write_pair(folder, "save2.hg", _text(_with_cache(cache)))

    def test_fill_skips_what_is_there_and_copies_its_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            before = path.read_bytes()
            snap = take_snapshot(path)
            request = _request(path, tables, folder)
            plan = build_plan(request, snap, load_tables(tables))
            self.assertEqual(len(snap.player["wem"][":No"]), 1)
            self.assertEqual(snap.player["wem"][":No"][0]["1o9"], 7)
            self.assertEqual(plan.expected_add, 2)
            self.assertEqual(plan.expected_occupied, 1)
            self.assertIn("Add 2 parts", plan.summary)
            self.assertIn("2 free slots", plan.summary)
            self.assertIn("Stack size 500", plan.summary)
            self.assertIn("3 parts do not fit", plan.summary)
            labels = [change.label for change in plan.changes]
            self.assertEqual(
                labels,
                [
                    "Add a stack of 500 Tractor Beam (^B_MAG_1X1).",
                    "Add a stack of 500 Bunk (^B_BUNK).",
                ],
            )
            self.assertIn("Hull is already in the cache.", plan.skipped)
            self.assertTrue(any("Stairs" in line and "does not fit" for line in plan.skipped))
            first, second = (change.value for change in plan.changes)
            self.assertEqual(first["eVk"], 0.25)
            self.assertEqual(first["kept"], "yes")
            self.assertEqual(first["b76"], True)
            self.assertEqual(first["5tH"], False)
            self.assertEqual(first["Vn8"], {"elv": "Product"})
            self.assertEqual(first["1o9"], 500)
            self.assertEqual(first["F9q"], 500)
            self.assertEqual(first["3ZH"], {">Qh": 1, "XJ>": 0})
            self.assertEqual(second["3ZH"], {">Qh": 2, "XJ>": 0})
            self.assertIsNot(first["3ZH"], second["3ZH"])
            request.apply = True
            code = run(request)
            self.assertEqual(code, 0)
            self.assertNotEqual(path.read_bytes(), before)
            slots = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]["wem"][":No"]
            self.assertEqual(len(slots), 3)
            self.assertEqual(slots[0]["b2n"], "^b_hull_1")
            self.assertEqual(slots[0]["1o9"], 7)
            self.assertEqual(slots[0]["eVk"], 0.25)
            self.assertEqual([slot["b2n"] for slot in slots[1:]], ["^B_MAG_1X1", "^B_BUNK"])
            backup = request.backup_dir
            assert backup is not None
            zips = list(backup.glob("*.zip"))
            self.assertEqual(len(zips), 1)

    def test_a_lower_multiplier_stops_at_its_own_stack(self) -> None:
        from nmsmissions.corvette import CorvettePart

        parts = [
            CorvettePart("^B_MAG_1X1", "Tractor Beam", "TractorBeam", multiplier=5, limit=500),
            CorvettePart("^B_SMALL", "Small Trim", "Decor", multiplier=1, limit=100),
        ]
        cache = {
            "Slots": [],
            "ValidSlotIndices": [
                {"Index": {"X": 0, "Y": 0}},
                {"Index": {"X": 1, "Y": 0}},
            ],
        }
        filled = plan_fill({"CorvetteStorageInventory": cache}, ["PlayerStateData"], parts, 500, 500)
        beam, small = (change[2] for change in filled["changes"])
        self.assertEqual(beam["Amount"], 500)
        self.assertEqual(beam["MaxAmount"], 500)
        self.assertEqual(small["Amount"], 100)
        self.assertEqual(small["MaxAmount"], 100)

    def test_a_smaller_stack_still_uses_the_table_cap_as_the_max(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            code = run(_request(path, tables, folder, stack_size=12, apply=True))
            self.assertEqual(code, 0)
            slots = json.loads(unpack_save(path.read_bytes()).json_text)["vLc"]["6f="]["wem"][":No"]
            self.assertEqual(slots[1]["1o9"], 12)
            self.assertEqual(slots[1]["F9q"], 500)
            self.assertEqual(slots[0]["1o9"], 7)

    def test_a_stack_above_the_table_cap_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            blob = path.read_bytes()
            code = run(_request(path, tables, folder, stack_size=501, apply=True))
            self.assertEqual(code, 3)
            self.assertEqual(path.read_bytes(), blob)

    def test_an_empty_cache_uses_nine_obfuscated_values(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            cache = {":No": [], "hl?": _valid((0, 0))}
            path = _write_pair(folder, "save2.hg", _text(_with_cache(cache)))
            snap = take_snapshot(path)
            plan = build_plan(_request(path, tables, folder, stack_size=1), snap, load_tables(tables))
            slot = plan.changes[0].value
            self.assertEqual(_leaves(slot), 9)
            self.assertEqual(set(slot), {"Vn8", "b2n", "1o9", "F9q", "eVk", "b76", "5tH", "3ZH"})
            self.assertTrue(slot["b2n"].startswith("^"))
            self.assertEqual(slot["b2n"], "^B_MAG_1X1")

    def test_an_empty_plain_cache_stays_in_plain_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            cache = {
                "Slots": [],
                "ValidSlotIndices": [{"Index": {"X": 0, "Y": 0}}],
            }
            path = _write_pair(folder, "save2.hg", _text(_with_cache(cache, "CorvetteStorageInventory")))
            snap = take_snapshot(path)
            plan = build_plan(_request(path, tables, folder, stack_size=4), snap, load_tables(tables))
            slot = plan.changes[0].value
            self.assertEqual(slot["Id"], "^B_MAG_1X1")
            self.assertEqual(slot["Amount"], 4)
            self.assertEqual(slot["MaxAmount"], 500)
            self.assertNotIn("b2n", slot)
            self.assertEqual(_leaves(slot), 9)
            self.assertEqual(plan.changes[0].path[-2:], ["CorvetteStorageInventory", "Slots"])

    def test_existing_plain_ids_keep_their_spelling(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            held = {
                "Type": {"InventoryType": "Product"},
                "Id": "B_HULL_1",
                "Amount": 2,
                "MaxAmount": 10,
                "DamageFactor": 0.0,
                "FullyInstalled": True,
                "AddedAutomatically": False,
                "Index": {"X": 0, "Y": 0},
            }
            cache = {
                "Slots": [held],
                "ValidSlotIndices": [
                    {"Index": {"X": 0, "Y": 0}},
                    {"Index": {"X": 1, "Y": 0}},
                ],
            }
            player = {"CorvetteStorageInventory": cache}
            filled = plan_fill(player, ["PlayerStateData"], [], 500, 1)
            self.assertEqual(filled["add_count"], 0)
            parts = load_tables(tables).corvette_parts
            from nmsmissions.corvette import CorvettePart

            chosen = [CorvettePart(row["id"], row["name"], row["category"]) for row in parts]
            filled = plan_fill(player, ["PlayerStateData"], chosen, 500, 1)
            self.assertEqual(player["CorvetteStorageInventory"]["Slots"][0]["Amount"], 2)
            self.assertTrue(filled["changes"])
            self.assertFalse(filled["changes"][0][2]["Id"].startswith("^"))
            self.assertEqual(filled["changes"][0][2]["Id"], "B_MAG_1X1")

    def test_width_and_height_make_a_grid_and_a_bare_cache_does_not(self) -> None:
        parts = [
            type("Part", (), {"item_id": "^B_MAG_1X1", "name": "Tractor Beam", "category": "TractorBeam"})()
        ]
        wide = {"Slots": [], "Width": 2, "Height": 2}
        filled = plan_fill({"CorvetteStorageInventory": wide}, ["PlayerStateData"], parts, 500, 1)
        self.assertEqual(filled["free"], 4)
        self.assertEqual(filled["add_count"], 1)
        bare = {"Slots": []}
        filled = plan_fill({"CorvetteStorageInventory": bare}, ["PlayerStateData"], parts, 500, 1)
        self.assertEqual(filled["free"], 0)
        self.assertEqual(filled["add_count"], 0)
        self.assertIn("no free slot", filled["summary"])

    def test_a_running_game_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            blob = path.read_bytes()
            os.environ["NMSMISSIONS_NMS_RUNNING"] = "1"
            try:
                code = run(_request(path, tables, folder, apply=True))
            finally:
                os.environ.pop("NMSMISSIONS_NMS_RUNNING", None)
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), blob)

    def test_other_backups_are_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            backup = folder / "backups"
            backup.mkdir()
            kept = []
            for index in range(40):
                name = backup / f"save2.hg.20200101-{index:06d}.zip"
                name.write_bytes(b"PK")
                kept.append(name)
            code = run(_request(path, tables, folder, apply=True))
            self.assertEqual(code, 0)
            for name in kept:
                self.assertTrue(name.is_file(), name.name)
            self.assertGreaterEqual(len(list(backup.glob("*.zip"))), 41)

    def test_a_changed_cache_is_not_written(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            blob = path.read_bytes()
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            request = _request(path, tables, folder, apply=True)
            plan = build_plan(request, snap, loaded)
            self.assertEqual(plan.expected_add, 2)
            snap.player["wem"][":No"].append(_slot("^EXTRA", 1, 1, 3, 0))
            with self.assertRaises(EditError) as caught:
                commit(request, snap, plan, loaded, running=False)
            self.assertIn("changed after the preview", str(caught.exception))
            self.assertEqual(path.read_bytes(), blob)
            self.assertEqual(list((folder / "backups").glob("*.zip")), [])

    def test_a_patched_count_that_misses_the_preview_is_refused(self) -> None:
        from nmsmissions.edit import _corvette_guard

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = self._tables(folder)
            path = self._occupied_save(folder)
            snap = take_snapshot(path)
            loaded = load_tables(tables)
            request = _request(path, tables, folder)
            plan = build_plan(request, snap, loaded)
            with self.assertRaises(EditError) as caught:
                _corvette_guard(snap, plan, snap.text)
            self.assertIn("does not match the preview", str(caught.exception))


class CorvetteWindowTests(TkCleanup, unittest.TestCase):
    def test_the_page_previews_before_it_writes(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.gui import launch
        from tests.test_slots import _one, _walk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = _write_tables(folder)
            held = _slot("^b_hull_1", 7, 10, 0, 0)
            cache = {":No": [held], "hl?": _valid((0, 0), (1, 0))}
            path = _write_pair(folder, "save2.hg", _text(_with_cache(cache)))
            session = EditorSession(path, game_files=tables, backup_dir=folder / "backups")
            session.wait_for_tables()
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
                self.assertIn(FILL_LABEL, buttons)
                book = next(widget for widget in widgets if isinstance(widget, ttk.Notebook))
                names = [book.tab(tab, "text") for tab in book.tabs()]
                self.assertEqual(names, ["Missions", "Station and standing", "Corvette parts"])
                shown = "\n".join(
                    str(widget.cget("text")) for widget in widgets if isinstance(widget, (tk.Label, ttk.Label))
                )
                self.assertIn("1 part already in the cache", shown)
                self.assertIn("1 free slot", shown)
                self.assertIn("stack of 500", shown)
                self.assertIn("5 buildable parts are not in the cache yet", shown)
                spin = next(widget for widget in widgets if isinstance(widget, ttk.Spinbox))
                self.assertEqual(float(spin.cget("to")), 500.0)
                self.assertEqual(spin.get(), "500")
                _one(widgets, ttk.Button, FILL_LABEL).invoke()
                preview = next(widget for widget in window.winfo_children() if isinstance(widget, tk.Toplevel))
                body = next(widget for widget in _walk(preview) if isinstance(widget, tk.Text)).get("1.0", "end")
                self.assertIn("Add 1 part to the Corvette Workshop Cache", body)
                self.assertIn("1 free slot", body)
                self.assertIn("Stack size 500", body)
                self.assertIn("Add a stack of 500 Tractor Beam (^B_MAG_1X1).", body)
                self.assertIn("Hull is already in the cache.", body)
                self.assertIn("Stairs (^B_STAIR) does not fit.", body)
                self.assertIn("Write to copy", body)
            finally:
                window.destroy()

    def test_fill_does_not_wait_for_the_table_loader(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.gui import launch
        from tests.test_slots import _one, _walk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_pair(folder, "save2.hg", _text(_save_body()))
            session = EditorSession(path, game_files=folder, backup_dir=folder / "backups")
            started = threading.Event()
            release = threading.Event()

            def hang() -> None:
                started.set()
                release.wait(2)

            session._loader = threading.Thread(target=hang, name="nms-mission-tables")
            session._loader.start()
            self.assertTrue(started.wait(1))
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
                button = _one(widgets, ttk.Button, FILL_LABEL)
                began = time.perf_counter()
                with patch("tkinter.messagebox.showinfo") as info:
                    button.invoke()
                elapsed = time.perf_counter() - began
                self.assertLess(elapsed, 0.3)
                self.assertIn("still loading", info.call_args.args[1])
            finally:
                release.set()
                session._loader.join(timeout=2)
                window.destroy()

    def test_a_stack_above_the_cap_stays_on_the_page(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.gui import launch
        from tests.test_slots import _one, _walk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tables = _write_tables(folder)
            cache = {":No": [], "hl?": _valid((0, 0))}
            path = _write_pair(folder, "save2.hg", _text(_with_cache(cache)))
            session = EditorSession(path, game_files=tables, backup_dir=folder / "backups")
            session.wait_for_tables()
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
                spin = next(widget for widget in widgets if isinstance(widget, ttk.Spinbox))
                spin.delete(0, "end")
                spin.insert(0, "900")
                with patch("tkinter.messagebox.showinfo") as info:
                    _one(widgets, ttk.Button, FILL_LABEL).invoke()
                self.assertIn("1 to 500", info.call_args.args[1])
                self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in window.winfo_children()))
            finally:
                window.destroy()

    def test_an_old_game_file_copy_says_to_read_again(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        from nmsmissions.chains import ChainView, OtherView
        from nmsmissions.gui import launch
        from tests.test_slots import _one, _walk

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = _write_tables(folder)
            (root / "metadata" / "reality" / "tables" / "nms_basepartproducts.exml").unlink()
            cache = {":No": [], "hl?": _valid((0, 0))}
            path = _write_pair(folder, "save2.hg", _text(_with_cache(cache)))
            session = EditorSession(path, game_files=root, backup_dir=folder / "backups")
            session.wait_for_tables()
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
                shown = "\n".join(
                    str(widget.cget("text")) for widget in widgets if isinstance(widget, (tk.Label, ttk.Label))
                )
                self.assertIn(REREAD, shown)
                with patch("tkinter.messagebox.showinfo") as info:
                    _one(widgets, ttk.Button, FILL_LABEL).invoke()
                self.assertEqual(info.call_args.args[1], REREAD)
                self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in window.winfo_children()))
            finally:
                window.destroy()

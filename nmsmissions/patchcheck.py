"""Stop a write when the save is not the format this editor knows.

The known game format is the 7.06 save this editor was checked with.
Its top-level Version field is the obfuscated key F2P, and the value is 4739.
Inventory objects use F2P for their own version. Only the top-level field is
the save version.

A small save built by the tests has no game container. That shape is still
known, so the suite can write it. A real game save is recognized by the
container keys (Version, Platform, CommonStateData, ExpeditionContext,
DiscoveryManagerData). Unknown or missing pieces of that container pause
editing. The window says so. Viewing does not use this check.
"""

from __future__ import annotations

from dataclasses import dataclass

PAUSE_MESSAGE = (
    "This save is from a newer game version. Editing is paused to keep your save safe."
)
OVERRIDE_LABEL = "I made a backup, edit anyway"
KNOWN_LINE = "This save matches the format this editor knows."
OVERRIDE_LINE = "You chose to edit this save anyway. A backup is still made before the write."

# Top-level Version on the checked 7.06 save. Not the mission-progress number.
VERSION_KEY = "F2P"
KNOWN_SAVE_VERSIONS = frozenset({4739})

# Obfuscated key, then the plain name used in messages.
_TOP = (
    ("F2P", "Version"),
    ("8>q", "Platform"),
    ("XTp", "ActiveContext"),
    ("<h0", "CommonStateData"),
    ("vLc", "BaseContext"),
    ("2YS", "ExpeditionContext"),
    ("fDu", "DiscoveryManagerData"),
)
_TOP_NAMES = dict(_TOP)
_TOP_KEYS = frozenset(_TOP_NAMES)
# Present on a game save and absent from the editor's small test saves.
_GAME_MARKERS = frozenset({"F2P", "8>q", "<h0", "2YS", "fDu"})

_PLAYER = "6f="
_MISSIONS = "dwb"

# Fields the editor writes on the main player, and the shape they have on 7.06.
_PLAYER_FIELDS = (
    ("dwb", "list", "MissionProgress"),
    ("yq:", "int", "MissionVersion"),
    ("Kg6", "bool", "HasDiscoveredPurpleSystems"),
    ("cPt", "bool", "BuildersKnown"),
    ("wGS", "int", "Units"),
    ("7QL", "int", "Nanites"),
    ("kN;", "int", "Quicksilver"),
    (";R7", "str", "CurrentMissionID"),
    ("Mg<", "str", "PreviousMissionID"),
    ("4kj", "list", "KnownTech"),
    ("eZ<", "list", "KnownProducts"),
    ("24<", "list", "KnownSpecials"),
    (";l5", "dict", "Inventory"),
    ("aBE", "int", "PrimaryShip"),
    ("@Cs", "list", "Ships"),
    ("8ZP", "dict", "FreighterInventory"),
    ("gUR", "list", "Stats"),
    ("yhJ", "dict", "UniverseAddress"),
    ("wem", "dict", "CorvetteStorageInventory"),
    ("9i?", "dict", "CorvetteStorageLayout"),
    ("5hy", "int", "FirstPurpleSystemUA"),
    ("5?q", "bool", "HasGalacticMapRequestFirstPurple"),
    (";9U", "bool", "HasGalacticMapRequestAllPurples"),
    ("PMT", "dict", "TechInventory"),
)

_ROW_FIELDS = (
    ("p0c", "str", "Mission"),
    ("tW6", "int", "Progress"),
    ("8?J", "int", "Data"),
    ("Kex", "int", "Stat"),
    ("eZ7", "list", "Participants"),
)
_SEED = "@EL"
_LIMIT = 8


@dataclass(frozen=True)
class PatchVerdict:
    """ok is false when a write must pause. details are for tests and logs."""

    ok: bool
    details: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return "" if self.ok else PAUSE_MESSAGE


def assess(parsed: object) -> PatchVerdict:
    """Return whether this parsed save is a format the editor may write."""
    if not isinstance(parsed, dict):
        return PatchVerdict(False, ("The save JSON is not an object.",))
    if _is_game_save(parsed):
        return _assess_game(parsed)
    return _assess_fixture(parsed)


def _is_game_save(parsed: dict) -> bool:
    return bool(set(parsed) & _GAME_MARKERS)


def _assess_fixture(parsed: dict) -> PatchVerdict:
    """The small saves the tests build. They are not a game container."""
    problems: list[str] = []
    base = parsed.get("vLc")
    player = base.get(_PLAYER) if isinstance(base, dict) else None
    if not isinstance(player, dict):
        problems.append("The file has no player state.")
    elif not isinstance(player.get(_MISSIONS), list):
        problems.append("The file has no mission list.")
    else:
        version = player.get("yq:")
        if isinstance(version, bool) or not isinstance(version, int):
            problems.append("The file has no mission version.")
    if problems:
        return PatchVerdict(False, tuple(problems))
    return PatchVerdict(True, ())


def _assess_game(parsed: dict) -> PatchVerdict:
    problems: list[str] = []
    _version(parsed, problems)
    _top_level(parsed, problems)
    if len(problems) >= _LIMIT:
        return PatchVerdict(False, tuple(problems[:_LIMIT]))
    _containers(parsed, problems)
    player = _player(parsed)
    if player is None:
        _add(problems, "PlayerStateData is missing.")
        return PatchVerdict(False, tuple(problems[:_LIMIT]))
    _player_fields(player, problems)
    _mission_rows(player.get(_MISSIONS), problems)
    _stats(player.get("gUR"), problems)
    _address(player.get("yhJ"), problems)
    _inventory(player.get(";l5"), "Inventory", problems)
    _inventory(player.get("wem"), "Corvette Workshop Cache", problems)
    if problems:
        return PatchVerdict(False, tuple(problems[:_LIMIT]))
    return PatchVerdict(True, ())


def _version(parsed: dict, problems: list[str]) -> None:
    if VERSION_KEY not in parsed:
        _add(problems, "The save version is missing.")
        return
    value = parsed[VERSION_KEY]
    if isinstance(value, bool) or not isinstance(value, int):
        _add(problems, "The save version is not a whole number.")
        return
    if value not in KNOWN_SAVE_VERSIONS:
        _add(problems, f"Save version {value} is not one this editor knows.")


def _top_level(parsed: dict, problems: list[str]) -> None:
    found = set(parsed)
    for key, name in _TOP:
        if key not in found:
            if _add(problems, f"The top-level key {name} is missing."):
                return
    for key in parsed:
        if key in _TOP_KEYS:
            continue
        if _add(problems, f"The top-level key {key} is not one this editor knows."):
            return
    _typed(parsed, "F2P", "int", "Version", problems)
    _typed(parsed, "8>q", "str", "Platform", problems)
    _typed(parsed, "XTp", "str", "ActiveContext", problems)
    _typed(parsed, "<h0", "dict", "CommonStateData", problems)
    _typed(parsed, "vLc", "dict", "BaseContext", problems)
    _typed(parsed, "2YS", "dict", "ExpeditionContext", problems)
    _typed(parsed, "fDu", "dict", "DiscoveryManagerData", problems)
    platform = parsed.get("8>q")
    if isinstance(platform, str) and not platform:
        _add(problems, "Platform is empty.")
    active = parsed.get("XTp")
    if isinstance(active, str) and not active:
        _add(problems, "ActiveContext is empty.")


def _containers(parsed: dict, problems: list[str]) -> None:
    base = parsed.get("vLc")
    if isinstance(base, dict) and _PLAYER not in base:
        _add(problems, "PlayerStateData is missing.")


def _player(parsed: dict) -> dict | None:
    base = parsed.get("vLc")
    if not isinstance(base, dict):
        return None
    player = base.get(_PLAYER)
    if not isinstance(player, dict):
        return None
    return player


def _player_fields(player: dict, problems: list[str]) -> None:
    for key, kind, name in _PLAYER_FIELDS:
        if key not in player:
            if _add(problems, f"{name} is missing."):
                return
        elif _shape(player[key]) != kind:
            if _add(problems, f"{name} is not the shape this editor knows."):
                return


def _mission_rows(rows: object, problems: list[str]) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if len(problems) >= _LIMIT:
            return
        if not isinstance(row, dict):
            _add(problems, "A mission row is not an object.")
            continue
        for key, kind, name in _ROW_FIELDS:
            if key not in row:
                if _add(problems, f"A mission row is missing {name}."):
                    return
            elif _shape(row[key]) != kind:
                if _add(problems, f"A mission row {name} is not the shape this editor knows."):
                    return
        if _SEED not in row:
            if _add(problems, "A mission row is missing Seed."):
                return
        elif _shape(row[_SEED]) not in {"int", "str"}:
            if _add(problems, "A mission row Seed is not the shape this editor knows."):
                return
        mission = row.get("p0c")
        if isinstance(mission, str) and not mission:
            if _add(problems, "A mission row has an empty Mission id."):
                return


def _stats(rows: object, problems: list[str]) -> None:
    if not isinstance(rows, list):
        return
    for group in rows:
        if len(problems) >= _LIMIT:
            return
        if not isinstance(group, dict):
            _add(problems, "A standing group is not an object.")
            continue
        for key, name in ((":rc", "GroupId"), ("2Ak", "Address")):
            if key not in group:
                if _add(problems, f"A standing group is missing {name}."):
                    return
        inner = group.get("gUR")
        if not isinstance(inner, list):
            if _add(problems, "A standing group is missing its stat list."):
                return
        for stat in inner:
            if len(problems) >= _LIMIT:
                return
            if not isinstance(stat, dict) or "b2n" not in stat or ">MX" not in stat:
                _add(problems, "A standing stat is missing its id or value.")
                return


def _address(node: object, problems: list[str]) -> None:
    if not isinstance(node, dict):
        return
    reality = node.get("Iis")
    if isinstance(reality, bool) or not isinstance(reality, int):
        _add(problems, "UniverseAddress is missing RealityIndex.")
    galactic = node.get("oZw")
    if not isinstance(galactic, dict):
        _add(problems, "UniverseAddress is missing GalacticAddress.")
        return
    for key, name in (
        ("dZj", "VoxelX"),
        ("IyE", "VoxelY"),
        ("uXE", "VoxelZ"),
        ("vby", "SolarSystemIndex"),
        ("jsv", "PlanetIndex"),
    ):
        value = galactic.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            if _add(problems, f"GalacticAddress is missing {name}."):
                return


def _inventory(node: object, label: str, problems: list[str]) -> None:
    if not isinstance(node, dict):
        return
    slots = node.get(":No")
    valid = node.get("hl?")
    if not isinstance(slots, list) or not isinstance(valid, list):
        _add(problems, f"{label} is missing its slot list.")


def _typed(node: dict, key: str, kind: str, name: str, problems: list[str]) -> None:
    if key not in node:
        return
    if _shape(node[key]) != kind:
        _add(problems, f"{name} is not the shape this editor knows.")


def _shape(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    if isinstance(value, float):
        return "float"
    return type(value).__name__


def _add(problems: list[str], text: str) -> bool:
    if text not in problems:
        problems.append(text)
    return len(problems) >= _LIMIT

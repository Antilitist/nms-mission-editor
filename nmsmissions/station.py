"""Current-system station standing. Nothing here writes a save.

Race standing, guild standing, and salvage contracts for one star system
live on that system's ^SYSTEM_STATS row in PlayerStateData.Stats. The row
is matched to the player's current address. Address 0 is the player-wide
row and is not a system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nmsmissions.rewards import CURRENCY_CAP, stored_to_true

GROUP_NAME = "^SYSTEM_STATS"
RACE_TARGET = 30
GUILD_TARGET = 15
SALVAGE_TARGET = 5
UNITS_ASKED = 1_000_000_000
# The field is a uint32. Warn once the true amount is in the top of that range.
NEAR_CAP = 4_000_000_000

MEET_LABEL = "Meet requirements"
CHOOSE_STANDING = (
    "Choose the race standing and the guild standing this station uses. "
    "Only those two, plus salvage contracts, are raised."
)
MISSING_SYSTEM = "This system is not in the save, so standing was not changed."
LOADING = "The save is still loading."
NO_SAVE = "Open a save to see standing in the current system."

REQUIREMENTS = (
    "To claim the station in the current system, these numbers in that system are what count. "
    "Standing in another system does not count.\n"
    "Local race standing: 30. Korvax uses explorer standing. Gek uses trade standing. "
    "Vy'keen uses warrior standing.\n"
    "Local guild standing: 15. That is the Mercenaries Guild, the Explorers Guild, "
    "or the Merchants Guild in this system.\n"
    "Salvage contracts in this system: 5.\n"
    f"The station also asks for {UNITS_ASKED:,} units."
)

# Obfuscated key first, then the plain name, so either save spelling works.
_STATS = ("gUR", "Stats")
_GROUP = (":rc", "GroupId")
_ADDRESS = ("2Ak", "Address")
_ID = ("b2n", "Id")
_VALUE = (">MX", "Value")
_INT = (">vs", "IntValue")
_UA = ("yhJ", "UniverseAddress")
_GAL = ("oZw", "GalacticAddress")
_VX = ("dZj", "VoxelX")
_VY = ("IyE", "VoxelY")
_VZ = ("uXE", "VoxelZ")
_SYS = ("vby", "SolarSystemIndex")
_PLANET = ("jsv", "PlanetIndex")
_REALITY = ("Iis", "RealityIndex")
_UNITS = ("wGS", "Units")

# (stat id, plain label, target, choice text). The choice text is the radio the
# player uses. Salvage has no choice: it is always one of the three that rise.
RACE_SPECS = (
    ("^EXP_STANDING", "Korvax standing in this system", RACE_TARGET, "Raise Korvax standing in this system to 30"),
    ("^TRA_STANDING", "Gek standing in this system", RACE_TARGET, "Raise Gek standing in this system to 30"),
    ("^WAR_STANDING", "Vy'keen standing in this system", RACE_TARGET, "Raise Vy'keen standing in this system to 30"),
)
GUILD_SPECS = (
    (
        "^WGUILD_STAND",
        "Mercenaries Guild standing in this system",
        GUILD_TARGET,
        "Raise Mercenaries Guild standing in this system to 15",
    ),
    (
        "^EGUILD_STAND",
        "Explorers Guild standing in this system",
        GUILD_TARGET,
        "Raise Explorers Guild standing in this system to 15",
    ),
    (
        "^TGUILD_STAND",
        "Merchants Guild standing in this system",
        GUILD_TARGET,
        "Raise Merchants Guild standing in this system to 15",
    ),
)
SALVAGE_SPEC = (
    "^SP_POI_MISSIONS",
    "Salvage contracts in this system",
    SALVAGE_TARGET,
    "Raise salvage contracts in this system to 5",
)
STAT_SPECS = RACE_SPECS + GUILD_SPECS + (SALVAGE_SPEC,)
RACE_IDS = {spec[0] for spec in RACE_SPECS}
GUILD_IDS = {spec[0] for spec in GUILD_SPECS}
_LABELS = {spec[0]: spec[1] for spec in STAT_SPECS}


@dataclass
class StandingRow:
    stat_id: str
    label: str
    current: int
    target: int
    group_index: int
    stat_index: int
    value_key: str
    int_key: str | None
    replace_value: dict | None = None
    whole_stat: dict | None = None


@dataclass
class StationView:
    found: bool
    rows: list[StandingRow] = field(default_factory=list)
    units: int | None = None
    stats_key: str = "gUR"
    inner_key: str = "gUR"


def _first(node: object, keys: tuple[str, ...]) -> tuple[str | None, object]:
    if not isinstance(node, dict):
        return None, None
    for key in keys:
        if key in node:
            return key, node[key]
    return None, None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hex_body(value: object) -> str:
    """Uppercase hex digits for a stored address int. A voxel struct is not an address."""
    if isinstance(value, bool) or isinstance(value, dict):
        return ""
    if isinstance(value, float):
        if not value.is_integer():
            return ""
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("0x"):
            body = text[2:]
            if body and all(char in "0123456789abcdefABCDEF" for char in body):
                return body.upper()
            return ""
        if text.lstrip("-").isdigit():
            value = int(text)
        else:
            return ""
    if isinstance(value, int):
        number = value + (1 << 64) if value < 0 else value
        return format(number, "X")
    return ""


def _nibble(value: object, digits: int) -> int | None:
    number = _as_int(value)
    if number is None:
        return None
    span = 16**digits
    number = number % span
    if number < 0:
        number += span
    return number


def _pack_voxels(node: dict, reality: int) -> str:
    """14 nibbles: planet, system, reality, y, z, x.

    RealityIndex lives on UniverseAddress, beside GalacticAddress. The format
    widths are zero-padded. A width without the zero flag pads with spaces.
    """
    _, planet = _first(node, _PLANET)
    _, system = _first(node, _SYS)
    _, y_voxel = _first(node, _VY)
    _, z_voxel = _first(node, _VZ)
    _, x_voxel = _first(node, _VX)
    parts = (
        _nibble(planet, 1),
        _nibble(system, 3),
        _nibble(reality, 2),
        _nibble(y_voxel, 2),
        _nibble(z_voxel, 3),
        _nibble(x_voxel, 3),
    )
    if any(part is None for part in parts):
        return ""
    return f"{parts[0]:01X}{parts[1]:03X}{parts[2]:02X}{parts[3]:02X}{parts[4]:03X}{parts[5]:03X}"


def _padded(value: object) -> str:
    body = _hex_body(value)
    if not body or len(body) > 14:
        return ""
    return body.zfill(14)


def _zero_address(value: object) -> bool:
    body = _hex_body(value)
    return not body or set(body) <= {"0"}


def universe_address_hex(player: dict) -> str:
    """14-nibble address for the player's current UniverseAddress. Empty if it cannot be packed."""
    _, universe = _first(player, _UA)
    if not isinstance(universe, dict):
        return ""
    _, reality_value = _first(universe, _REALITY)
    reality = _as_int(reality_value)
    if reality is None:
        return ""
    _, galactic = _first(universe, _GAL)
    if not isinstance(galactic, dict):
        return ""
    return _pack_voxels(galactic, reality)


def universe_address_int(player: dict) -> int | None:
    packed = universe_address_hex(player)
    if not packed:
        return None
    return int(packed, 16)


def label_for(stat_id: str) -> str:
    return _LABELS.get(stat_id, stat_id)


def chosen_stat_ids(race_id: str | None, guild_id: str | None) -> tuple[str, str, str] | None:
    """The race the player picked, the guild they picked, and salvage contracts."""
    if race_id not in RACE_IDS or guild_id not in GUILD_IDS:
        return None
    return (race_id, guild_id, SALVAGE_SPEC[0])


def meet_does(race_id: str | None, guild_id: str | None) -> str:
    """Tip and preview words. Names the three values that will change."""
    chosen = chosen_stat_ids(race_id, guild_id)
    if chosen is None:
        return (
            "Choose the race this station uses and the guild this station uses. "
            "Meet requirements then raises that race standing to 30, that guild standing to 15, "
            "and salvage contracts to 5 when a number is lower. "
            "Other standings stay as they are. It never lowers a number. Units stay as they are."
        )
    race, guild, salvage = (_LABELS[stat_id] for stat_id in chosen)
    return (
        f"Raises {race} to 30, {guild} to 15, and {salvage} to 5 when a number is lower. "
        "Other standings stay as they are. It never lowers a number. Units stay as they are."
    )


def _int_key(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in _INT:
        if key in value:
            return key
    return None


def _read_int(stat: dict) -> int:
    _, value = _first(stat, _VALUE)
    key = _int_key(value)
    if key is None or not isinstance(value, dict):
        return 0
    number = _as_int(value.get(key))
    return 0 if number is None else number


def _style_keys(stat: dict) -> tuple[str, str]:
    id_key, _ignored = _first(stat, _ID)
    if id_key == "Id":
        return "Value", "IntValue"
    return ">MX", ">vs"


def _int_key_name(inner: list) -> str | None:
    """The IntValue key spelling used in this group. The neighbour's number is not used."""
    for stat in inner:
        if not isinstance(stat, dict):
            continue
        _, value = _first(stat, _VALUE)
        key = _int_key(value)
        if key:
            return key
    return None


def _default_value(int_key: str, target: int) -> dict:
    """An empty Value {} becomes this. Only IntValue, set to the target."""
    return {int_key: target}


def _matching_group(player: dict) -> tuple[str, int, dict] | None:
    stats_key, stats = _first(player, _STATS)
    if not isinstance(stats, list) or stats_key is None:
        return None
    current_full = universe_address_hex(player)
    if len(current_full) != 14 or set(current_full) <= {"0"}:
        return None
    want = current_full[1:]
    matches: list[tuple[int, dict, str]] = []
    for index, group in enumerate(stats):
        if not isinstance(group, dict):
            continue
        _, group_id = _first(group, _GROUP)
        if group_id != GROUP_NAME:
            continue
        _, address = _first(group, _ADDRESS)
        if address is None or _zero_address(address):
            continue
        padded = _padded(address)
        if not padded or padded[1:] != want:
            continue
        matches.append((index, group, padded))
    if not matches:
        return None
    system_rows = [item for item in matches if item[2][0] == "0"]
    exact = [item for item in matches if item[2] == current_full]
    if system_rows:
        index, group, _padded_addr = system_rows[0]
    elif exact:
        index, group, _padded_addr = exact[0]
    else:
        index, group, _padded_addr = matches[0]
    return stats_key, index, group


def read_station(player: object) -> StationView:
    """Standing rows for the current system. Missing system leaves found false."""
    if not isinstance(player, dict):
        return StationView(False)
    _, units_value = _first(player, _UNITS)
    units = _as_int(units_value)
    located = _matching_group(player)
    if located is None:
        return StationView(False, units=units)
    stats_key, group_index, group = located
    inner_key, inner = _first(group, _STATS)
    if not isinstance(inner, list) or inner_key is None:
        return StationView(False, units=units, stats_key=stats_key)
    by_id: dict[str, tuple[int, dict]] = {}
    for index, stat in enumerate(inner):
        if not isinstance(stat, dict):
            continue
        _, stat_id = _first(stat, _ID)
        if isinstance(stat_id, str) and stat_id not in by_id:
            by_id[stat_id] = (index, stat)
    rows: list[StandingRow] = []
    for stat_id, label, target, _choice in STAT_SPECS:
        found = by_id.get(stat_id)
        if found is None:
            continue
        stat_index, stat = found
        value_key, value = _first(stat, _VALUE)
        value_missing = value_key is None
        if value_missing:
            value_key, int_key = _style_keys(stat)
            value = None
        else:
            int_key = _int_key(value)
        current = _read_int(stat)
        replace = None
        whole = None
        number_key = int_key
        if current < target and (not isinstance(value, dict) or int_key is None):
            style_value, style_int = _style_keys(stat)
            if value_key is None:
                value_key = style_value
            number_key = _int_key_name(inner) or style_int
            replace = _default_value(number_key, target)
            if value_missing:
                whole = dict(stat)
                whole[value_key] = replace
                replace = None
            number_key = None
        rows.append(
            StandingRow(
                stat_id=stat_id,
                label=label,
                current=current,
                target=target,
                group_index=group_index,
                stat_index=stat_index,
                value_key=value_key or ">MX",
                int_key=number_key,
                replace_value=replace,
                whole_stat=whole,
            )
        )
    return StationView(True, rows=rows, units=units, stats_key=stats_key, inner_key=inner_key)


def raise_changes(
    view: StationView,
    player_path: list,
    stat_ids: tuple[str, ...] | None = None,
) -> list[tuple[str, list, object]]:
    """(label, path, value) for the chosen stats that are below their target.

    Higher counts are skipped. Stats that were not chosen are skipped.
    """
    if not view.found:
        return []
    allowed = None if stat_ids is None else set(stat_ids)
    changes = []
    for row in view.rows:
        if allowed is not None and row.stat_id not in allowed:
            continue
        if row.current >= row.target:
            continue
        base = [view.stats_key, row.group_index, view.inner_key, row.stat_index]
        if row.whole_stat is not None:
            path = player_path + base
            value: object = row.whole_stat
        elif row.replace_value is not None:
            path = player_path + base + [row.value_key]
            value = row.replace_value
        elif row.int_key:
            path = player_path + base + [row.value_key, row.int_key]
            value = row.target
        else:
            continue
        label = f"Raise {row.label} from {row.current} to {row.target}."
        changes.append((label, path, value))
    return changes


def units_text(stored: int | None) -> str:
    """True units, the station price, and a warning when the amount is near the cap."""
    if stored is None:
        return "Units are not in this save."
    true = stored_to_true(stored)
    lines = [
        f"Units: {true:,}.",
        f"The station asks for {UNITS_ASKED:,} units. Meet requirements does not change units.",
    ]
    if true >= NEAR_CAP:
        lines.append(
            f"Warning: units are near the {CURRENCY_CAP:,} cap, about 4.29 billion."
        )
    if stored < 0:
        lines.append(
            "Units are stored as a signed number, so this amount is written as a negative. "
            "That is not a debt. A negative stored value means the amount plus 4,294,967,296."
        )
    return " ".join(lines)


def units_warning_lines(stored: int | None) -> list[str]:
    if stored is None:
        return []
    true = stored_to_true(stored)
    lines = []
    if true >= NEAR_CAP:
        lines.append(f"Warning: units are near the {CURRENCY_CAP:,} cap, about 4.29 billion.")
    if stored < 0:
        lines.append(
            "Units are stored as a signed number, so this amount is written as a negative. "
            "That is not a debt. A negative stored value means the amount plus 4,294,967,296."
        )
    return lines


def panel_text(player: object, *, loading: bool = False, opened: bool = True) -> str:
    """Words for the Station and standing tab. Reads an in-memory player only."""
    lines = [REQUIREMENTS, ""]
    if not opened:
        lines.append(NO_SAVE)
        return "\n".join(lines)
    if loading or player is None:
        lines.append(LOADING)
        return "\n".join(lines)
    view = read_station(player)
    if not view.found:
        lines.append(MISSING_SYSTEM)
    elif not view.rows:
        lines.append("This system has no standing or salvage counts to show.")
    else:
        for row in view.rows:
            lines.append(f"{row.label}: {row.current} / {row.target}")
    lines.append("")
    lines.append(units_text(view.units if view else None))
    return "\n".join(lines)

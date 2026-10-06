"""Corvette Workshop Cache fill. Nothing here writes a save.

The part list is read from the player's own extracted tables. Hello Games
files are not shipped with this tool. The cache is PlayerStateData's
CorvetteStorageInventory. Parts come from NMS_BASEPARTPRODUCTS, not the
general product table. On the 7.05 tables the Normal BaseCapsule product
stack is 100, and these parts use a stack multiplier of 5, so a full stack
is 500. That inventory has 160 slots.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FILL_LABEL = "Fill Corvette Workshop Cache with all parts"
NEED_FILES = "Click Read my game files first. The part list comes from your own install."
REREAD = (
    "This copy of your game files does not include the corvette part table. "
    "Click Read my game files again."
)
LOADING = "The part list is still loading."
NO_SAVE = "Open a save to see the Corvette Workshop Cache."
NO_CACHE = "This save has no Corvette Workshop Cache, so nothing was added."
# GcInventoryStackSizeGroup.BaseCapsule on the Normal difficulty row.
# Chest is the index before it and is not the workshop cap.
BASE_CAPSULE_INDEX = 9
NORMAL_INDEX = 1
_PART_STEMS = {"nms_basepartproducts"}
# 7.05 keeps every build-menu entry in the objects table.
_ENTRY_STEMS = {"basebuildingobjectstable"}
PLANT_GROUP = "BIGGS_LOW_PERF"
# 10 columns, the same width the game uses for a storage grid. 16 rows is 160.
GRID_WIDTH = 10
GRID_HEIGHT = 16

_CACHE_KEYS = ("wem", "CorvetteStorageInventory")
_LAYOUT_KEYS = ("9i?", "CorvetteStorageLayout")
_SLOT_KEYS = (":No", "Slots")
_VALID_KEYS = ("hl?", "ValidSlotIndices", "ValidSlots")
_WIDTH_KEYS = ("=Tb", "Width")
_HEIGHT_KEYS = ("N9>", "Height")
_ID_KEYS = ("b2n", "Id")
_AMOUNT_KEYS = ("1o9", "Amount")
_MAX_KEYS = ("F9q", "MaxAmount")
_TYPE_KEYS = ("Vn8", "Type")
_KIND_KEYS = ("elv", "InventoryType")
_DAMAGE_KEYS = ("eVk", "DamageFactor")
_INSTALLED_KEYS = ("b76", "FullyInstalled")
_AUTO_KEYS = ("5tH", "AddedAutomatically")
_INDEX_KEYS = ("3ZH", "Index")
_X_KEYS = (">Qh", "X")
_Y_KEYS = ("XJ>", "Y")

_CLASS_VALUES = {
    "gccorvettepartcategory",
    "gccorvettepartcategory.xml",
    "nmsstring0x10",
    "nmsstring0x10.xml",
}


@dataclass
class CorvettePart:
    item_id: str
    name: str
    category: str
    multiplier: int = 1
    limit: int = 0


def attach_catalog(tables, root: Path) -> None:
    """Fill tables.corvette_parts and tables.corvette_stack_cap from extracted files."""
    base = root if root.is_dir() else root.parent
    tables.corvette_needs_reread = not _has_part_table(base)
    products = _read_products(base)
    entries = _read_entries(base)
    names = _read_names(base)
    base_cap = _read_stack_base(base)
    parts = select_parts(products, entries, names)
    for part in parts:
        part.limit = _part_limit(base_cap, part.multiplier)
    tables.corvette_parts = [
        {"id": part.item_id, "name": part.name, "category": part.category, "limit": part.limit}
        for part in parts
    ]
    limits = [part.limit for part in parts if part.limit > 0]
    tables.corvette_stack_cap = max(limits) if limits else 0


def parts_from_tables(tables) -> list[CorvettePart]:
    found: list[CorvettePart] = []
    raw = getattr(tables, "corvette_parts", None) or []
    for row in raw:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        category = str(row.get("category") or "").strip()
        try:
            limit = int(row.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        if item_id and name:
            found.append(CorvettePart(item_id, name, category, limit=limit if limit > 0 else 0))
    return found


def stack_cap_from_tables(tables) -> int:
    try:
        value = int(getattr(tables, "corvette_stack_cap", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def select_parts(products: list[dict], entries: list[dict], names: dict[str, str]) -> list[CorvettePart]:
    """Buildable corvette parts, in the order the product table lists them."""
    by_id: dict[str, list[dict]] = {}
    for entry in entries:
        by_id.setdefault(_bare(entry.get("id") or ""), []).append(entry)
    chosen: list[CorvettePart] = []
    seen: set[str] = set()
    for product in products:
        bare = _bare(product.get("id") or "")
        if not bare or bare in seen:
            continue
        if _is_plant(product, by_id.get(bare) or []):
            continue
        visible = [entry for entry in by_id.get(bare) or [] if entry.get("show")]
        if not visible:
            continue
        entry = visible[0]
        if _is_plant(product, [entry]):
            continue
        if _general_base_decor(entry):
            continue
        # A real category is the usual corvette part. A few ship parts, such as
        # the internal landing bay, are in the build menu with no category.
        # The decoration flag also marks ordinary chairs, lights, and ruins.
        if not _real_category(product.get("category") or "") and not _ship_structure(entry):
            continue
        name = _english_name(names, product)
        if not name:
            continue
        seen.add(bare)
        # Keep the product table's own spelling. Matching is case-insensitive.
        chosen.append(
            CorvettePart(
                "^" + _stem(str(product.get("id") or "")),
                name,
                str(product.get("category") or ""),
                multiplier=_multiplier(product),
            )
        )
    return chosen


def panel_text(
    player: dict | None,
    parts: list[CorvettePart] | None,
    cap: int,
    *,
    loading: bool = False,
    reread: bool = False,
) -> str:
    if loading:
        return LOADING
    if player is None:
        return NO_SAVE
    if reread:
        return REREAD
    if parts is None:
        return NEED_FILES
    inventory = _inventory(player)
    if inventory is None:
        return NO_CACHE
    present, free = _occupancy(inventory, player)
    have = _present_ids(present)
    missing = [part for part in parts if _bare(part.item_id) not in have]
    limit = f"{cap}" if cap else "the game tables"
    lines = [
        "Corvette Workshop Cache.",
        f"{_plural(len(present), 'part')} already in the cache. {_plural(len(free), 'free slot')}.",
        f"The game tables allow a stack of {limit} in this cache.",
        f"{_plural(len(missing), 'buildable part')} are not in the cache yet."
        if len(missing) != 1
        else "1 buildable part is not in the cache yet.",
        "Fill adds one stack of each missing part. Parts already here stay as they are.",
    ]
    return "\n".join(lines)


def parse_stack_size(text: str, cap: int) -> int | None:
    """A whole number from 1 to the table cap. None when the text is not that."""
    if cap < 1:
        return None
    raw = text.strip()
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    if value < 1 or value > cap:
        return None
    return value


def plan_fill(
    player: dict,
    player_path: list,
    parts: list[CorvettePart],
    cap: int,
    stack: int,
) -> dict:
    """Changes for one new stack per missing part. Existing stacks are not edited."""
    inventory, inv_key = _inventory_keyed(player)
    if inventory is None or inv_key is None:
        return {
            "summary": NO_CACHE,
            "changes": [],
            "skipped": [],
            "warnings": [],
            "add_count": 0,
            "occupied": 0,
            "free": 0,
        }
    if cap < 1:
        return {
            "summary": "The stack limit is not in the game tables, so nothing was added.",
            "changes": [],
            "skipped": [],
            "warnings": [],
            "add_count": 0,
            "occupied": 0,
            "free": 0,
        }
    amount = stack if 1 <= stack <= cap else 0
    if amount < 1:
        return {
            "summary": f"Enter a stack size from 1 to {cap}.",
            "changes": [],
            "skipped": [],
            "warnings": [],
            "add_count": 0,
            "occupied": 0,
            "free": 0,
        }
    present, free = _occupancy(inventory, player)
    have = _present_ids(present)
    slots_key = _which(inventory, _SLOT_KEYS) or (":No" if _uses_obfuscated(inventory) else "Slots")
    template = _template(present, cap, slots_key)
    id_spelling_caret = _wants_caret(present)
    missing = [part for part in parts if _bare(part.item_id) not in have]
    room = list(free)
    changes = []
    leftovers: list[CorvettePart] = []
    for part in missing:
        if not room:
            leftovers.append(part)
            continue
        x_pos, y_pos = room.pop(0)
        item_id = part.item_id if id_spelling_caret else _stem(part.item_id)
        own = int(getattr(part, "limit", 0) or 0)
        if own < 1:
            own = cap
        put = amount if amount <= own else own
        slot = _new_slot(template, item_id, put, own, x_pos, y_pos)
        label = f"Add a stack of {put} {part.name} ({item_id})."
        changes.append((label, player_path + [inv_key, slots_key], slot))
    already = [part.name for part in parts if _bare(part.item_id) in have]
    skipped = [f"{name} is already in the cache." for name in already]
    skipped.extend(f"{part.name} ({part.item_id}) does not fit." for part in leftovers)
    summary = (
        f"Add {_plural(len(changes), 'part')} to the Corvette Workshop Cache. "
        f"{_plural(len(free), 'free slot')}. Stack size {amount}."
    )
    if leftovers:
        summary += f" {_plural(len(leftovers), 'part')} do not fit." if len(leftovers) != 1 else " 1 part does not fit."
    if not changes and not missing:
        summary = "Every buildable part is already in the Corvette Workshop Cache. Nothing was added."
    elif not changes and missing:
        summary = "The Corvette Workshop Cache has no free slot, so nothing was added."
    return {
        "summary": summary,
        "changes": changes,
        "skipped": skipped,
        "warnings": [],
        "add_count": len(changes),
        "occupied": len(present),
        "free": len(free),
    }


def cache_counts(player: dict) -> tuple[int, int] | None:
    """Occupied slots and free slots. None when this save has no workshop cache."""
    inventory = _inventory(player)
    if inventory is None:
        return None
    present, free = _occupancy(inventory, player)
    return len(present), len(free)


def _ship_structure(entry: dict) -> bool:
    """Ship structure only. Ship decoration is also set on ordinary base items."""
    return entry.get("ship_structural") is True


def _multiplier(product: dict) -> int:
    try:
        value = int(product.get("multiplier") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _part_limit(base: int, multiplier: int) -> int:
    """BaseCapsule stack times this part's multiplier. A missing multiplier counts as 1."""
    if base < 1:
        return 0
    factor = multiplier if multiplier > 0 else 1
    return base * factor


def _real_category(category: str) -> bool:
    text = category.strip()
    if not text or text.casefold() in {"none", "0"}:
        return False
    if text.casefold() in _CLASS_VALUES or text.casefold().startswith("gc"):
        return False
    return True


def _is_plant(product: dict, entries: list[dict]) -> bool:
    groups = [str(product.get("group") or "")]
    for entry in entries:
        groups.extend(entry.get("groups") or [])
        groups.append(str(entry.get("style") or ""))
    return any(group.casefold() == PLANT_GROUP.casefold() for group in groups if group)


def _general_base_decor(entry: dict) -> bool:
    """Planetary decoration, not a part the corvette workshop menu can place."""
    structural = entry.get("ship_structural")
    decorative = entry.get("ship_decorative")
    if structural is True or decorative is True:
        return False
    if entry.get("decoration") is True and structural is False and decorative is False:
        return True
    groups = [str(group) for group in entry.get("groups") or [] if str(group)]
    if groups and structural is False and decorative is False:
        return True
    return False


def _english_name(names: dict[str, str], product: dict) -> str:
    keys = [product.get("name_key") or "", product.get("name_lower") or "", product.get("id") or ""]
    bare = _bare(str(product.get("id") or ""))
    keys.extend([bare, bare + "_NAME", bare + "_NAME_L"])
    for key in keys:
        text = str(key or "").strip()
        if not text:
            continue
        found = names.get(text.casefold())
        if found:
            return found
    return ""


def _read_products(root: Path) -> list[dict]:
    found: list[dict] = []
    for path in _stem_files(root, _PART_STEMS):
        _walk(path, lambda elem: _take_product(elem, found))
    return found


def _read_entries(root: Path) -> list[dict]:
    found: list[dict] = []
    for path in _stem_files(root, _ENTRY_STEMS):
        _walk(path, lambda elem: _take_entry(elem, found))
    return found


def _has_part_table(root: Path) -> bool:
    return bool(_stem_files(root, _PART_STEMS))


def _read_names(root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for path in _language_files(root):
        _walk(path, lambda elem: _take_name(elem, names))
    return names


def _read_stack_base(root: Path) -> int:
    """Normal BaseCapsule product stack. Chest is a different column and is not used."""
    rows: list[list[int]] = []
    for path in _named_files(root, "difficultyconfig"):
        _walk(path, lambda elem: _take_stack_row(elem, rows))
    if not rows:
        return 0
    chosen = rows[NORMAL_INDEX] if len(rows) > NORMAL_INDEX else rows[0]
    if len(chosen) <= BASE_CAPSULE_INDEX:
        return 0
    value = chosen[BASE_CAPSULE_INDEX]
    return value if value > 0 else 0


def _take_product(elem: ET.Element, found: list[dict]) -> bool:
    ident = _child_value(elem, "ID")
    if not ident:
        return False
    # A reward row also has an ID. A product row carries the category or a name.
    if not _has_child(elem, "CorvettePartCategory") and not _has_child(elem, "Name"):
        return False
    found.append(
        {
            "id": ident,
            "name_key": _child_value(elem, "Name") or "",
            "name_lower": _child_value(elem, "NameLower") or "",
            "category": _category_value(elem),
            "group": _child_value(elem, "GroupID") or "",
            "multiplier": _number(_child_value(elem, "StackMultiplier")),
        }
    )
    return True


def _take_entry(elem: ET.Element, found: list[dict]) -> bool:
    if not _has_child(elem, "ShowInBuildMenu"):
        return False
    ident = _child_value(elem, "ID")
    if not ident:
        return False
    found.append(
        {
            "id": ident,
            "show": _is_true(_child_value(elem, "ShowInBuildMenu")),
            "groups": _group_values(elem),
            "style": _child_value(elem, "Style") or "",
            "decoration": _optional_bool(elem, "IsDecoration"),
            "ship_structural": _optional_bool(elem, "BuildableInShipStructural"),
            "ship_decorative": _optional_bool(elem, "BuildableInShipDecorative"),
        }
    )
    return True


def _take_name(elem: ET.Element, names: dict[str, str]) -> bool:
    ident = _child_value(elem, "Id")
    english = _child_value(elem, "English")
    if not ident or not english:
        return False
    names.setdefault(ident.casefold(), english.strip())
    return True


def _take_stack_row(elem: ET.Element, rows: list[list[int]]) -> bool:
    values = _stack_values(elem)
    if len(values) > BASE_CAPSULE_INDEX:
        rows.append(values)
        return True
    return False


def _stack_values(elem: ET.Element) -> list[int]:
    values: list[int] = []
    for child in list(elem):
        if child.attrib.get("name") != "MaxProductStackSizes":
            continue
        raw = child.attrib.get("value")
        if raw is not None and _is_int(raw):
            values.append(int(raw))
            continue
        for grand in list(child):
            item = grand.attrib.get("value")
            if item is not None and _is_int(item):
                values.append(int(item))
    return values


def _category_value(elem: ET.Element) -> str:
    for child in list(elem):
        if child.attrib.get("name") != "CorvettePartCategory":
            continue
        direct = (child.attrib.get("value") or "").strip()
        if direct and direct.casefold() not in _CLASS_VALUES and not direct.casefold().startswith("gc"):
            return direct
        for grand in list(child):
            value = (grand.attrib.get("value") or "").strip()
            if value and value.casefold() not in _CLASS_VALUES and not value.casefold().startswith("gc"):
                return value
    return ""


def _group_values(elem: ET.Element) -> list[str]:
    groups: list[str] = []
    for child in list(elem):
        if child.attrib.get("name") != "Groups":
            continue
        for node in child.iter():
            if node.attrib.get("name") == "Value" and node.attrib.get("value"):
                groups.append(node.attrib["value"])
    return groups


def _inventory(player: dict) -> dict | None:
    found, _key = _inventory_keyed(player)
    return found


def _inventory_keyed(player: dict) -> tuple[dict | None, str | None]:
    if not isinstance(player, dict):
        return None, None
    for key in _CACHE_KEYS:
        node = player.get(key)
        if isinstance(node, dict):
            return node, key
    return None, None


def _occupancy(inventory: dict, player: dict) -> tuple[list[dict], list[tuple[int, int]]]:
    slots = _slots(inventory)
    occupied = []
    taken: set[tuple[int, int]] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        occupied.append(slot)
        coord = _coord(slot)
        if coord is not None:
            taken.add(coord)
    free = [coord for coord in _valid_coords(inventory, player) if coord not in taken]
    return occupied, free


def _slots(inventory: dict) -> list:
    for key in _SLOT_KEYS:
        value = inventory.get(key)
        if isinstance(value, list):
            return value
    return []


def _valid_coords(inventory: dict, player: dict) -> list[tuple[int, int]]:
    for node in (inventory, _layout(player) or {}):
        if not isinstance(node, dict):
            continue
        for key in _VALID_KEYS:
            raw = node.get(key)
            if not isinstance(raw, list):
                continue
            coords = []
            for entry in raw:
                coord = _coord(entry)
                if coord is not None:
                    coords.append(coord)
            if coords:
                return sorted(set(coords), key=lambda item: (item[1], item[0]))
    width = _axis(inventory, player, _WIDTH_KEYS)
    height = _axis(inventory, player, _HEIGHT_KEYS)
    if width and height:
        return [(x_pos, y_pos) for y_pos in range(height) for x_pos in range(width)]
    return []


def _layout(player: dict) -> dict | None:
    for key in _LAYOUT_KEYS:
        node = player.get(key)
        if isinstance(node, dict):
            return node
    return None


def _axis(inventory: dict, player: dict, keys: tuple[str, ...]) -> int | None:
    for node in (inventory, _layout(player) or {}):
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


def _coord(entry: object) -> tuple[int, int] | None:
    if not isinstance(entry, dict):
        return None
    point = entry
    for key in _INDEX_KEYS:
        if isinstance(entry.get(key), dict):
            point = entry[key]
            break
    x_pos = _int_at(point, _X_KEYS)
    y_pos = _int_at(point, _Y_KEYS)
    if x_pos is None or y_pos is None:
        return None
    return x_pos, y_pos


def _template(present: list[dict], cap: int, slots_key: str) -> dict:
    for slot in present:
        if isinstance(slot, dict) and _slot_id(slot):
            return _clone_slot(slot)
    # Nine values, the same shape as a reward slot. A plain-key cache stays plain.
    if slots_key == "Slots":
        return {
            "Type": {"InventoryType": "Product"},
            "Id": "",
            "Amount": 0,
            "MaxAmount": cap,
            "DamageFactor": 0.0,
            "FullyInstalled": True,
            "AddedAutomatically": False,
            "Index": {"X": 0, "Y": 0},
        }
    return {
        "Vn8": {"elv": "Product"},
        "b2n": "",
        "1o9": 0,
        "F9q": cap,
        "eVk": 0.0,
        "b76": True,
        "5tH": False,
        "3ZH": {">Qh": 0, "XJ>": 0},
    }


def _new_slot(template: dict, item_id: str, amount: int, cap: int, x_pos: int, y_pos: int) -> dict:
    slot = _clone_slot(template)
    _put(slot, _ID_KEYS, item_id)
    _put(slot, _AMOUNT_KEYS, amount)
    _put(slot, _MAX_KEYS, cap)
    index_key = _which(slot, _INDEX_KEYS)
    if index_key is None:
        index_key = "3ZH" if "b2n" in slot or "Vn8" in slot else "Index"
        slot[index_key] = {}
    point = slot.get(index_key)
    if not isinstance(point, dict):
        point = {}
        slot[index_key] = point
    x_key = _which(point, _X_KEYS) or (">Qh" if index_key == "3ZH" else "X")
    y_key = _which(point, _Y_KEYS) or ("XJ>" if index_key == "3ZH" else "Y")
    point[x_key] = x_pos
    point[y_key] = y_pos
    return slot


def _clone_slot(slot: dict) -> dict:
    cloned: dict = {}
    for key, value in slot.items():
        if isinstance(value, dict):
            cloned[key] = dict(value)
        elif isinstance(value, list):
            cloned[key] = list(value)
        else:
            cloned[key] = value
    return cloned


def _put(slot: dict, keys: tuple[str, ...], value: object) -> None:
    key = _which(slot, keys)
    if key is None:
        key = keys[-1] if "Id" in keys or "Amount" in keys or "MaxAmount" in keys else keys[0]
        if keys is _ID_KEYS and "b2n" in slot:
            key = "b2n"
        elif keys is _ID_KEYS and any(name in slot for name in ("Vn8", "3ZH", "1o9")):
            key = "b2n"
        elif keys is _AMOUNT_KEYS and ("1o9" in slot or "F9q" in slot or "b2n" in slot):
            key = "1o9"
        elif keys is _MAX_KEYS and ("F9q" in slot or "1o9" in slot or "b2n" in slot):
            key = "F9q"
    slot[key] = value


def _wants_caret(present: list[dict]) -> bool:
    for slot in present:
        ident = _slot_id(slot)
        if ident:
            return ident.startswith("^")
    return True


def _present_ids(present: list[dict]) -> set[str]:
    found: set[str] = set()
    for slot in present:
        ident = _slot_id(slot)
        if ident:
            found.add(_bare(ident))
    return found


def _slot_id(slot: dict) -> str:
    for key in _ID_KEYS:
        value = slot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _uses_obfuscated(inventory: dict) -> bool:
    return ":No" in inventory or "hl?" in inventory


def _which(node: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in node:
            return key
    return None


def _int_at(node: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None


def _stem(item_id: str) -> str:
    return item_id.strip().lstrip("^")


def _bare(item_id: str) -> str:
    return _stem(item_id).casefold()


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"1 {word}"
    return f"{count} {word}s"


def _stem_name(path: Path) -> str:
    name = path.name.lower()
    for suffix in (".exml", ".mxml", ".xml"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for suffix in (".mbin.pc", ".mbin"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _stem_files(root: Path, stems: set[str]) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".exml", ".mxml", ".xml"}:
            continue
        if _stem_name(path) in stems:
            found.append(path)
    return found


def _number(value: str | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        return 0
    if number <= 0:
        return 0
    return int(number)


def _named_files(root: Path, token: str) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".exml", ".mxml", ".xml"}:
            continue
        if token in path.name.lower():
            found.append(path)
    return found


def _language_files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if path.suffix.lower() not in {".exml", ".mxml", ".xml"}:
            continue
        if "usenglish" in name:
            continue
        if name.startswith("nms_") and "english" in name:
            found.append(path)
    return found


def _walk(path: Path, capture) -> None:
    stack: list[ET.Element] = []
    try:
        events = ET.iterparse(path, events=("start", "end"))
    except (ET.ParseError, OSError):
        return
    for event, elem in events:
        if event == "start":
            stack.append(elem)
            continue
        if stack:
            stack.pop()
        # Leave a miss in place so the parent can still read its children.
        if not capture(elem):
            continue
        elem.clear()
        if stack:
            try:
                stack[-1].remove(elem)
            except ValueError:
                pass


def _has_child(elem: ET.Element, name: str) -> bool:
    return any(child.attrib.get("name") == name for child in list(elem))


def _child_value(elem: ET.Element, name: str) -> str | None:
    for child in list(elem):
        if child.attrib.get("name") == name and "value" in child.attrib:
            return child.attrib["value"]
    return None


def _optional_bool(elem: ET.Element, name: str) -> bool | None:
    raw = _child_value(elem, name)
    if raw is None:
        return None
    return _is_true(raw)


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().casefold() == "true"


def _is_int(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text[0] in "+-":
        text = text[1:]
    return text.isdigit()

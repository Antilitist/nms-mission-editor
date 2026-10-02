"""Turn extracted reward rows into inventory, currency, and flag edits.

Dialog rewards and word or standing rewards stay off unless the caller asks.
The same reward id is granted once per stage. Nothing here writes a save.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nmsmissions.gamedata import GameTables, MissionInfo, stage_progress_for

ITEM_KINDS = {
    "GcRewardSpecificProduct",
    "GcRewardSpecificSubstance",
    "GcRewardMultiSpecificItems",
    "GcRewardMultiSpecificProducts",
}
TECH_KINDS = {"GcRewardSpecificTech", "GcRewardTechRecipe"}
PRODUCT_LIST_KINDS = {
    "GcRewardProductRecipe",
    "GcRewardSpecificProductRecipe",
    "GcRewardMultiSpecificProductRecipes",
}
SPECIAL_LIST_KINDS = {"GcRewardSpecials", "GcRewardSpecificSpecial"}
WORD_KINDS = {"GcRewardTeachWord", "GcRewardWord", "GcRewardLearnWord"}
STAT_KINDS = {"GcRewardStat", "GcRewardStanding", "GcRewardFactionStanding"}
SCRIPTED_KINDS = {
    "GcRewardInstallTech",
    "GcRewardMission",
    "GcRewardScanEvent",
    "GcRewardPortal",
    "GcRewardOpenPortal",
    "GcRewardTrigger",
    "GcRewardSpecificShip",
    "GcRewardStartMission",
    "GcRewardSetMission",
    "GcRewardMultiSpecificTechRecipes",
}
UI_KINDS = {"GcRewardMissionMessage", "GcRewardShowMessage", "GcRewardOSDMessage", "GcRewardNotification"}

CURRENCY_KEYS = {"Units": "wGS", "Nanites": "7QL", "Specials": "kN;"}
CURRENCY_NAMES = {"Units": "Units", "Nanites": "Nanites", "Specials": "Quicksilver"}
# Units, Nanites, and Quicksilver are 32-bit player fields. Nanites have been
# seen at this ceiling. Quicksilver has no lower published cap.
CURRENCY_CAP = 4294967295
LIST_KEYS = {"tech": "4kj", "product": "eZ<", "special": "24<"}

DEFAULT_LIMITS = {
    "product": {"exosuit": 10, "ship": 10, "freighter": 20},
    "substance": {"exosuit": 9999, "ship": 9999, "freighter": 9999},
    "product_limit": 999999999,
    "substance_limit": 9999,
}


@dataclass
class ItemNeed:
    item_id: str
    kind: str
    amount: int
    reward_id: str


@dataclass
class OtherNeed:
    """Currency, a known-list append, or a boolean flag."""

    sort: str
    reward_id: str
    label: str
    key: str
    amount: int = 0
    item_id: str = ""
    flag: bool = False


@dataclass
class RewardPlan:
    items: list[ItemNeed] = field(default_factory=list)
    others: list[OtherNeed] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)
    choices: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def stack_size(kind: str, group: str, multiplier: int, limits: dict) -> int:
    """Product stack is min(group size * max(1, multiplier), product limit). 0 counts as 1."""
    table = limits["substance"] if kind == "Substance" else limits["product"]
    cap = limits["substance_limit"] if kind == "Substance" else limits["product_limit"]
    base = int(table.get(group, 1))
    return max(1, min(base * max(1, int(multiplier)), int(cap)))


def limits_from_save(root: object) -> dict:
    limits = {
        "product": dict(DEFAULT_LIMITS["product"]),
        "substance": dict(DEFAULT_LIMITS["substance"]),
        "product_limit": DEFAULT_LIMITS["product_limit"],
        "substance_limit": DEFAULT_LIMITS["substance_limit"],
    }
    if not isinstance(root, dict):
        return limits
    raw = root.get("limits")
    if not isinstance(raw, dict):
        return limits
    for group in ("product", "substance"):
        found = raw.get(group)
        if isinstance(found, dict):
            for name, value in found.items():
                try:
                    limits[group][str(name)] = int(value)
                except (TypeError, ValueError):
                    continue
    for key, dest in (("productLimit", "product_limit"), ("substanceLimit", "substance_limit")):
        if key in raw:
            try:
                limits[dest] = int(raw[key])
            except (TypeError, ValueError):
                continue
    return limits


def stored_to_true(stored: int) -> int:
    """Units are a signed int32. A stored negative value is that value plus 2**32."""
    if stored < 0:
        return stored + 2**32
    return stored


def true_to_stored(value: int) -> int:
    clamped = max(0, min(int(value), CURRENCY_CAP))
    if clamped >= 2**31:
        return clamped - 2**32
    return clamped


def caret_id(item_id: str) -> str:
    text = item_id.strip()
    if text.startswith("^"):
        return text
    return "^" + text


def rewards_for_finish(
    info: MissionInfo,
    current: int,
    final: int,
    tables: GameTables,
    *,
    amount_mode: str = "min",
    choices: dict[str, int] | None = None,
    include_dialog: bool = False,
    seen: set[str] | None = None,
    save_version: int = 0,
) -> RewardPlan:
    """Rewards for stages the player has not already passed, up to the new progress.

    The same reward id at two stages is granted once per stage.
    """
    plan = RewardPlan()
    granted_ids = seen if seen is not None else set()
    picked = choices or {}
    reported: set[str] = set()
    for stage in info.stages:
        progress = stage_progress_for(stage, save_version)
        token = f"{progress}:{stage.reward_id}"
        if token in reported:
            continue
        reported.add(token)
        # The save stores the last completed stage. The game grants that
        # stage's reward when it writes the number, so equality is already
        # passed. A later stage still has a higher number.
        if progress is not None and current >= 0 and progress <= current:
            plan.skipped.append(f"{stage.reward_id}: already passed this stage.")
            continue
        if progress is not None and progress > final:
            plan.skipped.append(f"{stage.reward_id}: past the final progress, left alone.")
            continue
        if token in granted_ids:
            plan.skipped.append(f"{stage.reward_id}: already granted.")
            continue
        reward = info.rewards.get(stage.reward_id) or tables.reward_table.get(stage.reward_id)
        if reward is None:
            plan.skipped.append(f"{stage.reward_id}: not in the reward table. It will be skipped.")
            continue
        if stage.dialog and not include_dialog and stage.reward_id not in picked:
            plan.choices.append(f"{stage.reward_id}: dialog reward, off unless you pick it.")
            continue
        _apply_reward(plan, reward.reward_id, reward.choice, reward.body, tables, amount_mode, picked)
        granted_ids.add(token)
    return plan


def _apply_reward(
    plan: RewardPlan,
    reward_id: str,
    choice: str,
    body: dict,
    tables: GameTables,
    amount_mode: str,
    picked: dict[str, int],
) -> None:
    parts = list(body.get("parts") or [])
    if not parts:
        plan.skipped.append(f"{reward_id}: no reward body.")
        return
    selected = _select_parts(reward_id, choice, parts, body.get("chance"), picked, plan)
    for part in selected:
        _one_part(plan, reward_id, part, tables, amount_mode)


def _select_parts(
    reward_id: str,
    choice: str,
    parts: list[dict],
    row_chance: int | None,
    picked: dict[str, int],
    plan: RewardPlan,
) -> list[dict]:
    if reward_id in picked:
        index = picked[reward_id]
        if index < 0 or index >= len(parts):
            plan.skipped.append(f"{reward_id}: choice {index} is outside the reward.")
            return []
        return [parts[index]]
    if choice == "SelectAlways":
        best = max(range(len(parts)), key=lambda index: (parts[index].get("chance") or 0, -index))
        return [parts[best]]
    chosen: list[dict] = []
    for index, part in enumerate(parts):
        chance = part.get("chance")
        if chance is None:
            chance = row_chance
        if chance is None or chance >= 100:
            chosen.append(part)
        else:
            plan.choices.append(f"{reward_id}: part {index} is {chance} percent, left unticked.")
    return chosen


def _one_part(plan: RewardPlan, reward_id: str, part: dict, tables: GameTables, amount_mode: str) -> None:
    kind = part.get("kind") or ""
    if kind in ITEM_KINDS:
        _items(plan, reward_id, part, tables, amount_mode)
        return
    if kind == "GcRewardMoney":
        _money(plan, reward_id, part, amount_mode)
        return
    if kind in TECH_KINDS:
        _known(plan, reward_id, part, "tech")
        return
    if kind in PRODUCT_LIST_KINDS:
        _known(plan, reward_id, part, "product")
        return
    if kind in SPECIAL_LIST_KINDS:
        _known(plan, reward_id, part, "special")
        return
    if kind == "GcRewardPurpleSystems":
        plan.others.append(OtherNeed("flag", reward_id, f"{reward_id}: purple systems discovered.", "Kg6", flag=True))
        plan.granted.append(f"{reward_id}: purple systems discovered.")
        return
    if kind == "GcRewardBuildersKnown":
        plan.others.append(OtherNeed("flag", reward_id, f"{reward_id}: builders known.", "cPt", flag=True))
        plan.granted.append(f"{reward_id}: builders known.")
        return
    if kind in WORD_KINDS or kind in STAT_KINDS:
        plan.skipped.append(f"{reward_id}: words and standings stay off.")
        return
    if kind in SCRIPTED_KINDS or "Install" in kind or "Scan" in kind or "Portal" in kind or "Mission" in kind:
        plan.skipped.append(f"{reward_id}: not granted, scripted by the game.")
        return
    if kind in UI_KINDS or not kind:
        plan.skipped.append(f"{reward_id}: no save effect.")
        return
    plan.skipped.append(f"{reward_id}: {kind} will be skipped.")


def _amount(part: dict, amount_mode: str) -> int:
    low = part.get("amount_min")
    high = part.get("amount_max")
    if amount_mode == "max" and high is not None:
        return int(high)
    if low is not None:
        return int(low)
    if high is not None:
        return int(high)
    return 1


def _items(plan: RewardPlan, reward_id: str, part: dict, tables: GameTables, amount_mode: str) -> None:
    kind_name = part.get("kind") or ""
    default_kind = "Substance" if "Substance" in kind_name else "Product"
    entries = [entry for entry in (part.get("entries") or []) if entry.get("id")]
    if not entries:
        fallback = _amount(part, amount_mode)
        entries = [{"id": raw, "amount": fallback} for raw in (part.get("ids") or [])]
    if not entries:
        plan.skipped.append(f"{reward_id}: item reward has no id. It will be skipped.")
        return
    for entry in entries:
        item_id = caret_id(str(entry.get("id") or ""))
        amount = entry.get("amount")
        if amount is None:
            amount = _amount(part, amount_mode)
        bare = item_id[1:] if item_id.startswith("^") else item_id
        in_sub = item_id in tables.substances or bare in tables.substances
        in_prod = item_id in tables.products or bare in tables.products
        if not in_sub and not in_prod:
            plan.skipped.append(
                f"{reward_id}: {item_id} is not in the product or substance tables. It will be skipped."
            )
            continue
        if in_sub and not in_prod:
            item_kind = "Substance"
        elif in_prod and not in_sub:
            item_kind = "Product"
        else:
            item_kind = default_kind
        plan.items.append(ItemNeed(item_id, item_kind, int(amount), reward_id))
        plan.granted.append(f"{reward_id}: {amount} {item_id}.")


def _money(plan: RewardPlan, reward_id: str, part: dict, amount_mode: str) -> None:
    currency = part.get("currency") or "Units"
    key = CURRENCY_KEYS.get(currency)
    if key is None:
        plan.skipped.append(f"{reward_id}: currency {currency} is not units, nanites, or quicksilver.")
        return
    amount = _amount(part, amount_mode)
    name = CURRENCY_NAMES.get(currency, currency)
    plan.others.append(OtherNeed("currency", reward_id, f"Add {amount} {name}.", key, amount=amount))
    plan.granted.append(f"{reward_id}: {amount} {name}.")


def _known(plan: RewardPlan, reward_id: str, part: dict, which: str) -> None:
    ids = part.get("ids") or []
    if not ids:
        plan.skipped.append(f"{reward_id}: no id to record as known.")
        return
    key = LIST_KEYS[which]
    for item_id in ids:
        marked = caret_id(item_id)
        if which == "product":
            label = f"Add recipe {marked}."
            granted = f"{reward_id}: new recipe {marked}."
        else:
            label = f"Add {marked} to known {which}."
            granted = f"{reward_id}: known {which} {marked}."
        plan.others.append(OtherNeed("list", reward_id, label, key, item_id=marked))
        plan.granted.append(granted)

"""Mission commands: list, show, flags, diff, verify, and the version 2 edits."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nmsmissions import __version__, CREDIT
from nmsmissions.catalog import load_completion_catalog
from nmsmissions.chains import (
    ChainView,
    OtherView,
    StepView,
    build_views,
    flatten_chains,
    format_forest,
    friendly_cell,
    load_chains,
    load_names,
    lookup_wiki,
    mission_status,
    open_wiki,
    progress_cell,
)
from nmsmissions.chunks import SaveFormatError, unpack_save
from nmsmissions.diffing import choose_save_file, diff_saves
from nmsmissions.edit import EditError, EditRequest, EditorSession, manifest_verify_note, run as run_edit
from nmsmissions.gamenames import GameNameReport, load_game_names
from nmsmissions.guard import LiveSaveRefused
from nmsmissions.mapping import MappingError, load_mapping
from nmsmissions.saveio import LoadedSave, context_key, load_save, require_context, verify_save

STATUS_ALIASES = {
    "done": "done",
    "progress": "in_progress",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "notstarted": "not_started",
    "not_started": "not_started",
    "not-started": "not_started",
    "unknown": "unknown",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (LiveSaveRefused, SaveFormatError, MappingError, ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mapping", help="Local mapping.json. Default: download and cache it.")
    common.add_argument(
        "--i-know",
        action="store_true",
        help="Allow a path inside the live HelloGames/NMS save folder. Still read-only.",
    )
    common.add_argument(
        "--game-files",
        help="Folder of extracted LANGUAGE and MISSIONS EXML/MXML. Not bundled.",
    )
    common.add_argument(
        "--context",
        choices=("base", "expedition"),
        default="base",
        help="Which player state to read. Default: base.",
    )
    common.add_argument(
        "--save-slot",
        type=int,
        default=None,
        help="Open the newer of the two files for this slot (save1/2 = slot 1).",
    )
    common.add_argument(
        "--no-auto-save",
        action="store_true",
        help="Use the save path as given. By default a newer paired saveN.hg is chosen.",
    )

    parser = argparse.ArgumentParser(
        prog="nmsmissions",
        description=(
            "No Man's Sky mission chains. List and compare are read-only. "
            "finish, reset, and preset purple preview by default and write only with --apply."
        ),
        epilog=CREDIT,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    list_cmd = commands.add_parser("list", parents=[common], help="Show mission chains in one save.")
    list_cmd.add_argument("save")
    list_cmd.add_argument("--chain", help="One chain id (artemis, atlas, twr, ism, ...), all, or other.")
    list_cmd.add_argument("--status", help="done, progress, notstarted, or unknown.")
    list_cmd.add_argument("--brief", action="store_true", help="Chain headers and the next step only.")
    list_cmd.add_argument("--json", action="store_true", help="Print the chain tree as JSON.")
    list_cmd.set_defaults(func=cmd_list)

    show_cmd = commands.add_parser("show", parents=[common], help="Show one mission and its neighbors.")
    show_cmd.add_argument("save")
    show_cmd.add_argument("mission")
    show_cmd.set_defaults(func=cmd_show)

    flags_cmd = commands.add_parser("flags", parents=[common], help="Show purple-system and story flags.")
    flags_cmd.add_argument("save")
    flags_cmd.set_defaults(func=cmd_flags)

    diff_cmd = commands.add_parser(
        "diff",
        parents=[common],
        help="Compare mission progress, chain position, and story flags.",
    )
    diff_cmd.add_argument("left")
    diff_cmd.add_argument("right")
    diff_cmd.add_argument(
        "--missions-only",
        action="store_true",
        help="Accepted for clarity. Story flags are still included.",
    )
    diff_cmd.set_defaults(func=cmd_diff)

    verify_cmd = commands.add_parser(
        "verify",
        parents=[common],
        help="Repack to a temp file and confirm the JSON bytes match.",
    )
    verify_cmd.add_argument("save")
    verify_cmd.set_defaults(func=cmd_verify)

    gui_cmd = commands.add_parser("gui", parents=[common], help="Open the chain tree. Finish and reset ask first.")
    gui_cmd.add_argument("save", nargs="?", help="Save file. Leave this out to pick a slot.")
    gui_cmd.add_argument("--backup-dir", help="Where to write backup zips. Tests and copies can set this.")
    gui_cmd.add_argument("--pcbanks", help="Folder of PCBANKS paks, used to see if extracted tables are stale.")
    gui_cmd.set_defaults(func=cmd_gui)

    edit = argparse.ArgumentParser(add_help=False)
    edit.add_argument("--apply", action="store_true", help="Write the save. Without this, the command only prints a plan.")
    edit.add_argument("--write", action="store_true", help="Same as --apply.")
    edit.add_argument("--live", action="store_true", help="Required, with --confirm-slot, before a live HelloGames save is written.")
    edit.add_argument("--confirm-slot", type=int, help="Slot number. save7/save8 are slot 4.")
    edit.add_argument("--backup-dir", help="Backup folder. Default is the cache folder.")
    edit.add_argument("--game-files", help="Extracted mission EXML or MXML. Final progress comes from these files.")
    edit.add_argument("--pcbanks", help="PCBANKS folder. If a pak is newer than the tables, rewards are skipped.")
    edit.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    edit.add_argument("--patch-out", help="Write offset, old text, and new text for each patch.")
    edit.add_argument("--no-rewards", action="store_true", help="Change progress only. Do not grant items or currency.")
    edit.add_argument("--choice", action="append", default=[], help="R_ID=index, to take one reward part. Repeat for more.")
    edit.add_argument("--amount", choices=("min", "max"), default="min", help="AmountMin or AmountMax. Default: min.")

    finish_cmd = commands.add_parser("finish", parents=[edit], help="Finish one mission, or a chain up to a step.")
    finish_cmd.add_argument("save")
    finish_cmd.add_argument("mission", nargs="?", help="Mission id, such as ^PURPM2.")
    finish_cmd.add_argument("--chain", help="Chain id from chains.yaml, such as ism.")
    finish_cmd.add_argument("--to", help="Finish the chain from the start through this step.")
    finish_cmd.set_defaults(func=cmd_finish)

    reset_cmd = commands.add_parser("reset", parents=[edit], help="Reset one mission, or a chain from a step.")
    reset_cmd.add_argument("save")
    reset_cmd.add_argument("mission", nargs="?", help="Mission id. A middle chain step needs --chain --from.")
    reset_cmd.add_argument("--chain", help="Chain id. Use with --from.")
    reset_cmd.add_argument("--from", dest="from_step", help="Reset this step and every later step in the chain.")
    reset_cmd.set_defaults(func=cmd_reset)

    rewards_cmd = commands.add_parser("rewards", parents=[edit], help="Preview rewards for one mission. Never writes.")
    rewards_cmd.add_argument("save")
    rewards_cmd.add_argument("mission")
    rewards_cmd.set_defaults(func=cmd_rewards)

    preset_cmd = commands.add_parser("preset", help="Named edits. purple unlocks purple stars.")
    preset_sub = preset_cmd.add_subparsers(dest="preset_name", required=True)
    purple_cmd = preset_sub.add_parser("purple", parents=[edit], help="Purple-star flag, or the flag plus the story steps.")
    purple_cmd.add_argument("save")
    purple_cmd.add_argument("--flag-only", action="store_true", help="Set the flag and known drive tech. Do not finish missions.")
    purple_cmd.set_defaults(func=cmd_purple)

    backup_cmd = commands.add_parser("backup", help="Zip the save and mf_ file. Does not modify them.")
    backup_cmd.add_argument("save")
    backup_cmd.add_argument("--backup-dir")
    backup_cmd.add_argument("--game-files")
    backup_cmd.add_argument("--pcbanks")
    backup_cmd.set_defaults(func=cmd_backup)

    restore_cmd = commands.add_parser("restore", parents=[edit], help="Put a backup zip back. Needs --apply.")
    restore_cmd.add_argument("zip")
    restore_cmd.add_argument("--to", help="Folder to write into. Default: the zip's folder.")
    restore_cmd.set_defaults(func=cmd_restore)

    install_cmd = commands.add_parser(
        "install",
        parents=[edit],
        help="Copy one slot's save pair into a folder. Needs --apply.",
    )
    install_cmd.add_argument("copy_folder")
    install_cmd.add_argument("--slot", type=int, required=True)
    install_cmd.add_argument("--to", required=True)
    install_cmd.set_defaults(func=cmd_install)

    gamedata_cmd = commands.add_parser("gamedata", help="Check extracted tables, or print the extract commands.")
    gamedata_sub = gamedata_cmd.add_subparsers(dest="gamedata_action", required=True)
    for name, help_text in (
        ("check", "Read extracted tables and say if they are older than the paks."),
        ("refresh", "Print the hgpaktool and MBINCompiler commands."),
    ):
        sub = gamedata_sub.add_parser(name, help=help_text)
        sub.add_argument("--game-files")
        sub.add_argument("--pcbanks")
        sub.set_defaults(func=cmd_gamedata, gamedata_action=name)

    wiki_cmd = commands.add_parser(
        "wiki",
        help="Print the No Man's Sky Wiki page or search for a mission or chain.",
    )
    wiki_cmd.add_argument("mission", help="Mission id, such as ^ACT1_STEP1, or a chain id such as artemis.")
    wiki_cmd.add_argument("--open", action="store_true", help="Open the URL in the browser.")
    wiki_cmd.set_defaults(func=cmd_wiki)
    return parser


def _mapping(args: argparse.Namespace):
    path = Path(args.mapping) if args.mapping else None
    return load_mapping(path)


def _open(args: argparse.Namespace, save: str) -> tuple[LoadedSave, str | None]:
    path, note = choose_save_file(
        Path(save),
        save_slot_number=args.save_slot,
        auto=not args.no_auto_save,
    )
    table, source = _mapping(args)
    return load_save(path, table, source, allow=args.i_know), note


def _names(
    args: argparse.Namespace,
) -> tuple[dict[str, str], dict[str, str], GameNameReport | None]:
    fallback = load_names()
    if not args.game_files:
        return fallback, {}, None
    report = load_game_names(Path(args.game_files))
    merged = dict(fallback)
    merged.update(report.names)
    return merged, dict(report.subtitles), report


def _views(
    save: LoadedSave,
    context: str,
    names: dict[str, str],
    subtitles: dict[str, str],
    completions: dict[str, int] | None = None,
    alerts: dict[str, str] | None = None,
):
    player = require_context(save, context)
    catalog = load_completion_catalog()
    if completions:
        for key, value in completions.items():
            catalog.setdefault(key, value)
    roots, other = build_views(
        load_chains(),
        player.missions,
        catalog,
        player.current_mission,
        names,
        subtitles,
        alerts,
    )
    return player, roots, other


def _game_finals(session, player) -> dict[str, int]:
    """Finals from extracted tables for missions the yaml catalog does not list."""
    from nmsmissions.gamedata import finals_for_catalog

    tables = session.cached_tables() if session is not None else None
    if tables is None:
        return {}
    return finals_for_catalog(tables, getattr(player, "mission_version", None))


def _status_code(text: str | None) -> str | None:
    if not text:
        return None
    code = STATUS_ALIASES.get(text.strip().lower())
    if code is None:
        known = ", ".join(sorted(set(STATUS_ALIASES)))
        raise ValueError(f"Unknown status {text!r}. Use one of: {known}.")
    return code


def _filter(roots: list[ChainView], other: OtherView, chain: str | None, status: str | None):
    import copy

    roots = copy.deepcopy(roots)
    other = copy.deepcopy(other)
    if chain and chain != "all":
        if chain == "other":
            roots = []
        else:
            found = [node for node in flatten_chains(roots) if node.chain_id == chain]
            if not found:
                known = ", ".join(node.chain_id for node in flatten_chains(roots))
                raise ValueError(f"Unknown chain {chain!r}. Known: {known}, other.")
            roots = found[:1]
            other = OtherView(steps=[])
    if status:
        def trim(node: ChainView) -> None:
            node.steps = [step for step in node.steps if step.status == status or step.is_next]
            for child in node.children:
                trim(child)

        for root in roots:
            trim(root)
        other.steps = [step for step in other.steps if step.status == status]
    return roots, other


def _header(save: LoadedSave, player, context: str, note: str | None = None) -> str:
    active = save.active_context
    active_text = "(not in this save)" if active is None else repr(active)
    showing = context_key(context)
    lines = [
        "Read-only. This command does not change the save.",
        f"Save: {save.path}",
        f"sha256: {save.sha256}",
        f"Mapping: {save.mapping_source}",
        f"Active context value: {active_text}",
        f"Showing: {showing}  ({player.path})",
        f"Current mission: {player.current_mission or '(none)'}",
        f"Previous mission: {player.previous_mission or '(none)'}",
    ]
    if note:
        lines.append(note)
    return "\n".join(lines)


def _flag_block(save: LoadedSave) -> str:
    lines = ["Flags"]
    names = (
        "HasDiscoveredPurpleSystems",
        "PurpleSystemsUnlocked",
        "FirstPurpleSystemUA",
        "HasGalacticMapRequestFirstPurple",
        "HasGalacticMapRequestAllPurples",
    )
    hits = [hit for hit in save.flags if hit.name in names]
    if not hits:
        lines.append("  (none of the known purple-system flags are in this save)")
        return "\n".join(lines)
    groups: dict[str, list] = {}
    for hit in hits:
        group = hit.path.split("/", 1)[0]
        if group not in ("BaseContext", "ExpeditionContext", "CommonStateData"):
            group = "Other"
        groups.setdefault(group, []).append(hit)
    for group in ("BaseContext", "ExpeditionContext", "CommonStateData", "Other"):
        rows = groups.get(group)
        if not rows:
            continue
        lines.append(f"  {group}")
        for hit in rows:
            lines.append(f"    {hit.name} = {hit.value!r}    {hit.path}")
    return "\n".join(lines)


def cmd_wiki(args: argparse.Namespace) -> int:
    url, note = lookup_wiki(args.mission, load_chains(), load_names())
    print(url)
    print(note)
    if args.open:
        open_wiki(url)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    save, note = _open(args, args.save)
    names, subtitles, game_names = _names(args)
    player, roots, other = _views(save, args.context, names, subtitles)
    roots, other = _filter(roots, other, args.chain, _status_code(args.status))
    if args.json:
        document = {
            "readonly": True,
            "save": str(save.path),
            "sha256": save.sha256,
            "active_context": save.active_context,
            "context": context_key(args.context),
            "current_mission": player.current_mission,
            "previous_mission": player.previous_mission,
            "flags": [
                {"name": hit.name, "path": hit.path, "value": hit.value} for hit in save.flags
            ],
            "chains": [_chain_json(node) for node in roots],
            "other": [_step_json(step) for step in other.steps],
        }
        print(json.dumps(document, indent=2))
        return 0
    parts = [_header(save, player, args.context, note)]
    if game_names is not None:
        parts.append(game_names.summary())
    parts.extend(["", _flag_block(save), "", format_forest(roots, other, brief=args.brief).rstrip(), ""])
    print("\n".join(parts))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    save, note = _open(args, args.save)
    names, subtitles, game_names = _names(args)
    player, roots, other = _views(save, args.context, names, subtitles)
    mission_id = _resolve_mission(args.mission, roots, other, player.missions)
    step, chain = _find_step(mission_id, roots, other)
    print(_header(save, player, args.context, note))
    if game_names is not None:
        print(game_names.summary())
    print()
    if step is None:
        raw = player.missions.get(mission_id)
        progress = None if raw is None else raw.get("Progress")
        complete = load_completion_catalog().get(mission_id)
        status, label = mission_status(progress if isinstance(progress, int) else None, complete)
        print(mission_id)
        print("  chain: (not in this save or in a known chain)")
        print(f"  status: {label}")
        print(f"  code: {status}")
        return 0
    where = chain.title if chain is not None else "Other"
    print(step.mission_id)
    print(f"  friendly name: {friendly_cell(step.title)}")
    print(f"  chain: {where}")
    print(f"  status: {step.status_label}")
    print(f"  progress: {_progress_text(step)}")
    print(f"  next in this chain: {'yes' if step.is_next else 'no'}")
    print(f"  tracked: {'yes' if step.tracked else 'no'}")
    print(f"  before: {_neighbor(step.before_id, step.before_status, chain is None, '(start of chain)')}")
    print(f"  after: {_neighbor(step.after_id, step.after_status, chain is None, '(end of chain)')}")
    if chain is not None and chain.blocked_by and step.is_next:
        print(f"  blocked until: {', '.join(chain.blocked_by)}")
    return 0


def cmd_flags(args: argparse.Namespace) -> int:
    save, note = _open(args, args.save)
    player = require_context(save, args.context)
    print(_header(save, player, args.context, note))
    print()
    print(_flag_block(save))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    if args.save_slot is not None:
        raise ValueError("diff compares the two files you name. It does not take --save-slot.")
    table, source = _mapping(args)
    left = load_save(args.left, table, source, allow=args.i_know)
    right = load_save(args.right, table, source, allow=args.i_know)
    names, subtitles, _game_names = _names(args)
    text = diff_saves(
        left,
        right,
        args.context,
        load_chains(),
        load_completion_catalog(),
        names,
        subtitles,
    )
    print("Read-only. This command does not change either save.")
    if args.missions_only:
        print("Story flags are included. They are the point of comparing these saves.")
    print(text, end="")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    path, note = choose_save_file(
        Path(args.save),
        save_slot_number=args.save_slot,
        auto=not args.no_auto_save,
    )
    table, _source = _mapping(args)
    ok, message = verify_save(path, allow=args.i_know, mapping=table)
    payload = unpack_save(path.read_bytes())
    manifest_ok, manifest_line = manifest_verify_note(path, len(payload.raw))
    if note:
        print(note)
    print(message)
    print(manifest_line)
    return 0 if ok and manifest_ok else 2


def cmd_gui(args: argparse.Namespace) -> int:
    from nmsmissions.gui import begin_loading, choose_working_copy, launch, set_status
    from nmsmissions.slots import working_root

    print("Loading missions... please wait", flush=True)
    window = begin_loading()
    picked = None
    mapping: dict = {}
    found = None
    if os.environ.get("NMSMISSIONS_SKIP_SAFETY") != "1":
        from nmsmissions.locate import confirm_startup, discover, offer_install_picker

        found = offer_install_picker(window, discover())
        if not confirm_startup(window, found):
            try:
                window.destroy()
            except Exception:
                pass
            return 0
        if not getattr(args, "pcbanks", None) and found.pcbanks is not None:
            args.pcbanks = str(found.pcbanks)
    try:
        mapping, _source = _mapping(args)
    except (MappingError, OSError):
        mapping = {}
    if not getattr(args, "game_files", None):
        from nmsmissions.gameread import cached_game_read

        cached = cached_game_read()
        if cached is not None:
            args.game_files = str(cached)
    if not getattr(args, "save", None):
        print("Looking for saves...", flush=True)
        set_status(window, "Looking for saves...")
        backup_dir = Path(args.backup_dir) if getattr(args, "backup_dir", None) else None
        picked = choose_working_copy(window, mapping, working_root(), backup_dir)
        if picked is None:
            try:
                window.destroy()
            except Exception:
                pass
            return 0
        args.save = str(picked.copy_path)
    print("Reading save...", flush=True)
    set_status(window, "Reading save...")
    save, note = _open(args, args.save)
    print("Building mission list...", flush=True)
    set_status(window, "Building mission list...")
    session = EditorSession(
        save.path,
        Path(args.game_files) if args.game_files else None,
        Path(args.pcbanks) if getattr(args, "pcbanks", None) else None,
        Path(args.backup_dir) if getattr(args, "backup_dir", None) else None,
    )
    session.mapping = mapping
    session.copy_root = working_root()
    if found is not None:
        session.version_note = found.version_line
    if picked is not None:
        session.origin = picked.origin
        session.copy_dir = picked.copy_dir
        session.slot = picked.slot
        session.account = picked.account

    def _point(path) -> None:
        args.save = str(path)

    session.on_save_path = _point
    session.adopt_cached_assets()
    fallback_names = load_names()
    held = {"save": save, "note": note}

    def _open_names():
        from nmsmissions.gamenames import display_names

        report = getattr(session, "_names_report", None)
        titles, subtitles, alerts = display_names(report, fallback_names)
        return titles, subtitles, report, alerts

    def _bundle(current, current_note):
        import time

        from nmsmissions.timing import record

        started = time.perf_counter()
        names, subtitles, game_names, alerts = _open_names()
        player, roots, other = _views(
            current,
            args.context,
            names,
            subtitles,
            _game_finals(session, require_context(current, args.context)),
            alerts,
        )
        record("post-read-names", (time.perf_counter() - started) * 1000.0)
        subtitle = (
            f"{current.path}    showing {context_key(args.context)}    "
            f"active context {current.active_context!r}    current {player.current_mission or '(none)'}"
        )
        if current_note:
            subtitle += "\n" + current_note
        return roots, other, subtitle, game_names

    def _with_rows(roots, other, subtitle, game_names):
        import time

        from nmsmissions.gui import forest_rows
        from nmsmissions.timing import record

        started = time.perf_counter()
        rows = forest_rows(roots, other)
        record("post-read-rows", (time.perf_counter() - started) * 1000.0)
        from nmsmissions.gamenames import list_name_line

        kept = [
            line
            for line in subtitle.splitlines()
            if not line.strip().startswith("Names from game files")
        ]
        subtitle = "\n".join(kept).rstrip("\n")
        subtitle += "\n" + list_name_line(rows, game_names)
        return roots, other, subtitle, rows

    roots, other, subtitle, name_report = _bundle(held["save"], held["note"])
    roots, other, subtitle, _prepared_rows = _with_rows(roots, other, subtitle, name_report)

    def reload_tree():
        again, again_note = _open(args, args.save)
        held["save"] = again
        held["note"] = again_note
        packed = _bundle(again, again_note)
        return _with_rows(*packed)

    def asset_rows():
        return _with_rows(*_bundle(held["save"], held["note"]))

    launch(
        "No Man's Sky mission chains",
        subtitle,
        roots,
        other,
        window=window,
        session=session,
        reload_cb=reload_tree,
        asset_rows=asset_rows,
    )
    return 0


def _edit_request(args: argparse.Namespace, action: str) -> EditRequest:
    choices: dict[str, int] = {}
    for item in getattr(args, "choice", None) or []:
        if "=" not in item:
            raise EditError(f"Choice {item} should look like R_ID=0.", code=2)
        key, raw = item.split("=", 1)
        choices[key.strip()] = int(raw)
    target = getattr(args, "to", None) or getattr(args, "from_step", None)
    return EditRequest(
        action=action,
        save=Path(args.save) if getattr(args, "save", None) else None,
        mission=getattr(args, "mission", None),
        chain=getattr(args, "chain", None),
        target=target,
        flag_only=getattr(args, "flag_only", False),
        apply=bool(getattr(args, "apply", False) or getattr(args, "write", False)),
        live=bool(getattr(args, "live", False)),
        confirm_slot=getattr(args, "confirm_slot", None),
        backup_dir=Path(args.backup_dir) if getattr(args, "backup_dir", None) else None,
        game_files=Path(args.game_files) if getattr(args, "game_files", None) else None,
        pcbanks=Path(args.pcbanks) if getattr(args, "pcbanks", None) else None,
        rewards=not bool(getattr(args, "no_rewards", False)),
        choices=choices,
        amount=getattr(args, "amount", "min") or "min",
        patch_out=Path(args.patch_out) if getattr(args, "patch_out", None) else None,
        as_json=bool(getattr(args, "json", False)),
        restore_zip=Path(args.zip) if getattr(args, "zip", None) else None,
        restore_to=Path(args.to) if action == "restore" and getattr(args, "to", None) else None,
        install_folder=Path(args.copy_folder) if getattr(args, "copy_folder", None) else None,
        install_slot=getattr(args, "slot", None),
        install_to=Path(args.to) if action == "install" and getattr(args, "to", None) else None,
    )


def cmd_finish(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "finish"))


def cmd_reset(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "reset"))


def cmd_rewards(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "rewards"))


def cmd_purple(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "purple"))


def cmd_backup(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "backup"))


def cmd_restore(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "restore"))


def cmd_install(args: argparse.Namespace) -> int:
    return run_edit(_edit_request(args, "install"))


def cmd_gamedata(args: argparse.Namespace) -> int:
    action = "gamedata-refresh" if args.gamedata_action == "refresh" else "gamedata-check"
    return run_edit(_edit_request(args, action))


def _resolve_mission(text: str, roots, other, missions: dict) -> str:
    candidates = [text]
    if not text.startswith("^"):
        candidates.append("^" + text)
    known = set(missions)
    for chain in flatten_chains(roots):
        for step in chain.steps:
            known.add(step.mission_id)
    for step in other.steps:
        known.add(step.mission_id)
    for candidate in candidates:
        if candidate in known:
            return candidate
    return text


def _find_step(mission_id: str, roots, other) -> tuple[StepView | None, ChainView | None]:
    for chain in flatten_chains(roots):
        for step in chain.steps:
            if step.mission_id == mission_id:
                return step, chain
    for step in other.steps:
        if step.mission_id == mission_id:
            return step, None
    return None, None


def _neighbor(mission_id: str | None, status: str | None, unchained: bool, empty: str) -> str:
    if unchained:
        return "(not in a known chain)"
    if mission_id is None:
        return empty
    labels = {
        "done": "done",
        "in_progress": "in progress",
        "not_started": "not started",
        "unknown": "unknown",
    }
    if not status:
        return mission_id
    return f"{mission_id} ({labels.get(status, status)})"


def _progress_text(step: StepView) -> str:
    return progress_cell(step.progress, step.complete)


def _step_json(step: StepView) -> dict:
    return {
        "id": step.mission_id,
        "title": step.title,
        "status": step.status,
        "status_label": step.status_label,
        "progress": step.progress,
        "complete": step.complete,
        "tracked": step.tracked,
        "optional": step.optional,
        "next": step.is_next,
        "before": step.before_id,
        "after": step.after_id,
        "before_status": step.before_status,
        "after_status": step.after_status,
        "wiki": step.wiki,
    }


def _chain_json(node: ChainView) -> dict:
    return {
        "id": node.chain_id,
        "title": node.title,
        "group": node.group,
        "complete": node.complete,
        "state_label": node.state_label,
        "done": node.done_count,
        "required": node.required_count,
        "blocked_by": node.blocked_by,
        "requires": [
            {"id": chain_id, "title": title, "done": done} for chain_id, title, done in node.requires
        ],
        "unlocks": node.unlocks,
        "next": None if node.next_step is None else node.next_step.mission_id,
        "wiki": node.wiki,
        "wiki_confirmed": node.wiki_confirmed,
        "steps": [_step_json(step) for step in node.steps],
        "children": [_chain_json(child) for child in node.children],
    }

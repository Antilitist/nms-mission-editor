"""Find the No Man's Sky install and save folders on this PC.

Nothing here reads or copies game files into the tool. A missing install
asks the user to pick the folder.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from nmsmissions.slots import nms_save_folders

TESTED_PATCH = "7.05"
TESTED_BUILD = "25625620"
NMS_APP_ID = "275850"
SAFETY_LINE = (
    "Back up the save first. Close No Man's Sky. "
    f"Tested on No Man's Sky {TESTED_PATCH} (build {TESTED_BUILD}). "
    "Use at your own risk."
)
_PATH_KEY = re.compile(r'"path"\s+"([^"]+)"')
_BUILD_KEY = re.compile(r'"buildid"\s+"(\d+)"')


@dataclass
class Discovery:
    install: Path | None = None
    pcbanks: Path | None = None
    saves: list[Path] = field(default_factory=list)
    build_id: str = ""
    version_line: str = ""
    version_differs: bool = True


def tested_label() -> str:
    return f"No Man's Sky {TESTED_PATCH} (build {TESTED_BUILD})"


def version_line(build_id: str) -> tuple[str, bool]:
    """Words for the detected build, and whether it is not the tested build."""
    tested = tested_label()
    if build_id == TESTED_BUILD:
        return f"Detected {tested}.", False
    if build_id:
        return (
            f"Detected build {build_id}. This tool was tested on {tested}. "
            "A different build can change mission data.",
            True,
        )
    return f"Game version was not detected. This tool was tested on {tested}.", True


def startup_message(found: Discovery) -> str:
    return "\n".join(
        [
            "Back up your save before you write.",
            "Close No Man's Sky. If Steam Cloud is on, it can put an old file back.",
            f"Tested on {tested_label()}.",
            "Use at your own risk.",
            "",
            found.version_line,
        ]
    )


def _steam_roots(env: dict[str, str]) -> list[Path]:
    roots: list[Path] = []
    override = env.get("NMS_STEAM_ROOT", "").strip()
    if override:
        roots.append(Path(override))
    for key in ("ProgramFiles(x86)", "PROGRAMFILES(X86)", "ProgramFiles", "PROGRAMFILES"):
        value = env.get(key, "").strip()
        if value:
            roots.append(Path(value) / "Steam")
    home = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
    if home:
        base = Path(home)
        roots.extend(
            [
                base / "Steam",
                base / ".steam" / "steam",
                base / ".local" / "share" / "Steam",
            ]
        )
    return roots


def _library_paths(steam: Path) -> list[Path]:
    paths = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return paths
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return paths
    for match in _PATH_KEY.finditer(text):
        raw = match.group(1).replace("\\\\", "\\")
        paths.append(Path(raw))
    return paths


def _looks_like_install(folder: Path) -> bool:
    return (folder / "GAMEDATA").is_dir() or (folder / "Binaries").is_dir()


def _install_in(library: Path) -> Path | None:
    folder = library / "steamapps" / "common" / "No Man's Sky"
    if _looks_like_install(folder):
        return folder
    return None


def read_build_id(install: Path) -> str:
    """Steam build id from appmanifest next to the install. Empty when missing."""
    manifest = install.parent.parent / f"appmanifest_{NMS_APP_ID}.acf"
    if not manifest.is_file():
        return ""
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _BUILD_KEY.search(text)
    return match.group(1) if match else ""


def find_install(env: dict[str, str] | None = None) -> Path | None:
    """The game folder, from NMS_INSTALL or a Steam library. None when not found."""
    values = os.environ if env is None else env
    override = values.get("NMS_INSTALL", "").strip()
    if override:
        folder = Path(override)
        if _looks_like_install(folder):
            return folder
        return None
    seen: set[Path] = set()
    for steam in _steam_roots(values):
        for library in _library_paths(steam):
            try:
                key = library.resolve()
            except OSError:
                key = library
            if key in seen:
                continue
            seen.add(key)
            found = _install_in(library)
            if found is not None:
                return found
    return None


def discover(env: dict[str, str] | None = None) -> Discovery:
    """Install, PCBANKS, save folders, and the version line. Does not open a window."""
    values = os.environ if env is None else env
    install = find_install(values)
    pcbanks = None
    build_id = ""
    if install is not None:
        banks = install / "GAMEDATA" / "PCBANKS"
        if banks.is_dir():
            pcbanks = banks
        build_id = read_build_id(install)
    line, differs = version_line(build_id)
    return Discovery(
        install=install,
        pcbanks=pcbanks,
        saves=nms_save_folders(values),
        build_id=build_id,
        version_line=line,
        version_differs=differs,
    )


def pick_directory(parent, title: str) -> Path | None:
    """Folder picker. Cancel returns None."""
    from tkinter import filedialog

    chosen = filedialog.askdirectory(parent=parent, title=title, mustexist=True)
    if not chosen:
        return None
    return Path(chosen)


def offer_install_picker(parent, found: Discovery) -> Discovery:
    """Ask for the install when Steam did not have it. Cancel leaves the discovery as it is."""
    if found.install is not None:
        return found
    from tkinter import messagebox

    chosen = pick_directory(parent, "Choose the No Man's Sky install folder")
    if chosen is None:
        return found
    if not _looks_like_install(chosen):
        messagebox.showinfo(
            "No Man's Sky",
            "That folder does not look like the game.\nChoose the folder that contains GAMEDATA.",
            parent=parent,
        )
        return found
    os.environ["NMS_INSTALL"] = str(chosen)
    return discover()


def offer_save_picker(parent) -> list[Path]:
    """Ask for a save folder when none was found. Cancel returns an empty list."""
    from tkinter import messagebox

    chosen = pick_directory(parent, "Choose the No Man's Sky save folder")
    if chosen is None:
        return []
    os.environ["NMS_SAVE_DIR"] = str(chosen)
    folders = nms_save_folders()
    if not folders:
        os.environ.pop("NMS_SAVE_DIR", None)
        messagebox.showerror(
            "No saves",
            "No save slots were in that folder.\n"
            "Choose the HelloGames\\NMS folder, or the st_ folder inside it.",
            parent=parent,
        )
        return []
    return folders


def confirm_startup(parent, found: Discovery) -> bool:
    """Startup warning. False when the user cancels."""
    from tkinter import messagebox

    return bool(messagebox.askokcancel("Before you edit a save", startup_message(found), parent=parent))

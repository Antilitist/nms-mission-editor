"""Refuse the live Steam save folder unless the operator opts in."""

from __future__ import annotations

from pathlib import Path


class LiveSaveRefused(RuntimeError):
    """The path is inside the game's HelloGames\\NMS save directory."""


def is_live_nms_save(path: Path | str) -> bool:
    """True when a path points at %APPDATA%\\HelloGames\\NMS, or the same shape elsewhere."""
    try:
        text = str(Path(path).resolve()).replace("\\", "/")
    except OSError:
        text = str(path).replace("\\", "/")
    parts = [chunk.lower() for chunk in text.split("/") if chunk]
    for index, name in enumerate(parts[:-1]):
        if name == "hellogames" and parts[index + 1] == "nms":
            return True
    return False


def refuse_live_save(path: Path | str, allow: bool) -> None:
    if allow or not is_live_nms_save(path):
        return
    raise LiveSaveRefused(
        f"Refusing {path}. That looks like the live No Man's Sky save folder. "
        "Copy the save somewhere else and point this tool at the copy. "
        "Pass --i-know only if you mean to read the live folder. "
        "Version 1 never writes a save, but the live folder is still off limits by default."
    )

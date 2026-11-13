"""No Man's Sky mission chain viewer and copy editor."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "1.3.0"

CREDIT = "Sam (Coder), Avea (Art) & Antilitist (Tester, Coder, Modder)"
AI_CREDIT = "Grok Bot crew (AI helpers)"
# Handle only. Shown in the About box and the README tip line.
X_MONEY_HANDLE = "Antilitist"
CASH_APP = "$Antilitist"
CASH_APP_URL = "https://cash.app/$Antilitist"


def tip_text() -> str:
    """Tip line for the About box and the README."""
    handle = X_MONEY_HANDLE.strip()
    return f"X Money: {handle}  |  Cash App: {CASH_APP} ({CASH_APP_URL})"


def about_body(version_note: str = "") -> str:
    """Plain About text. Handles only."""
    from nmsmissions.locate import support_phrase

    lines = [
        f"NMS Mission Editor {__version__}",
        "",
        CREDIT,
        AI_CREDIT,
        "",
        f"This tool was {support_phrase()}.",
        "Use at your own risk.",
    ]
    note = version_note.strip()
    if note:
        lines.append(note)
    lines.extend(["", tip_text()])
    return "\n".join(lines)


def cache_root() -> Path:
    """Name and table caches. NMSMISSIONS_CACHE_DIR overrides the home folder."""
    override = os.environ.get("NMSMISSIONS_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".cache" / "nms_mission_editor"

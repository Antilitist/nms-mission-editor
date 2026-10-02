"""Completion values vendored from okranger1777/nms-mission-progress (MIT)."""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
MISSIONS_DIR = DATA_DIR / "missions"


def load_completion_catalog(directory: Path | None = None) -> dict[str, int]:
    """Map mission id -> progress value seen on a finished save.

    Duplicate ids are kept when the values agree. A disagreement is an error
    so a bad table cannot silently change a status.
    """
    root = directory if directory is not None else MISSIONS_DIR
    catalog: dict[str, int] = {}
    files = sorted(root.rglob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"No mission tables in {root}")
    for path in files:
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            if ":" not in text:
                raise ValueError(f"{path}:{line_no}: expected '^ID: number'")
            key, value = text.split(":", 1)
            mission_id = key.strip().strip("\"'")
            try:
                number = int(value.strip())
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: completion value is not an integer") from exc
            previous = catalog.get(mission_id)
            if previous is not None and previous != number:
                raise ValueError(
                    f"{mission_id} is {previous} in one table and {number} in {path}"
                )
            catalog[mission_id] = number
    return catalog

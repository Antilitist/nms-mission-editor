"""Optional phase timings. Set NMSMISSIONS_TIMING=1 to print them on stderr.

pythonw hides stderr, so the same lines are also appended to timing.log in the
app folder (next to run_gui.pyw). NMSMISSIONS_TIMING_LOG overrides that path.
Phases include name-load, table-load, and tree-reload.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def enabled() -> bool:
    flag = os.environ.get("NMSMISSIONS_TIMING", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def log_path() -> Path:
    override = os.environ.get("NMSMISSIONS_TIMING_LOG", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "timing.log"


def _emit(line: str) -> None:
    print(line, file=sys.stderr)
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def breathe(every: int = 400) -> None:
    """Give the window thread a turn during a long in-process parse."""
    global _breaths
    _breaths += 1
    if _breaths < every:
        return
    _breaths = 0
    time.sleep(0)


_breaths = 0


def record(phase: str, elapsed_ms: float) -> None:
    """Write one phase line. Used when the work is split across after() chunks."""
    if enabled():
        _emit(f"timing {phase}: {elapsed_ms:.1f} ms")


@contextmanager
def timed(phase: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        record(phase, (time.perf_counter() - start) * 1000.0)

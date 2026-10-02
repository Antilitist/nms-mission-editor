"""Point every test run at a temp cache, timing log, and window file.

Call isolate_test_dirs() from each test module. unittest does not always
import tests/__init__.py, so a single test file cannot rely on that.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_root: str | None = None


def isolate_test_dirs() -> str:
    """Force cache, timing log, and window settings into one temp folder."""
    global _root
    app = str(Path(__file__).resolve().parents[1])
    if app not in sys.path:
        sys.path.insert(0, app)
    if _root is None:
        _root = tempfile.mkdtemp(prefix="nmsmissions-test-")
        atexit.register(lambda: shutil.rmtree(_root or "", ignore_errors=True))
    os.environ["NMSMISSIONS_CACHE_DIR"] = _root
    os.environ["NMSMISSIONS_TIMING_LOG"] = os.path.join(_root, "timing.log")
    os.environ["NMSMISSIONS_WINDOW_FILE"] = os.path.join(_root, "window.json")
    os.environ["NMSMISSIONS_SKIP_SAFETY"] = "1"
    return _root


def close_tk_roots() -> None:
    """Destroy leftover Tk roots, then collect them on this thread.

    On Python 3.10 a root collected on a worker thread aborts the run with
    Tcl_AsyncDelete: async handler deleted by the wrong thread.
    """
    import gc

    try:
        import tkinter as tk
    except Exception:
        gc.collect()
        return
    roots: list = []
    default = getattr(tk, "_default_root", None)
    if default is not None:
        roots.append(default)
    for obj in gc.get_objects():
        try:
            if isinstance(obj, tk.Tk):
                roots.append(obj)
        except Exception:
            continue
    seen: set[int] = set()
    for root in roots:
        ident = id(root)
        if ident in seen:
            continue
        seen.add(ident)
        try:
            root.destroy()
        except Exception:
            pass
    try:
        tk._default_root = None
    except Exception:
        pass
    gc.collect()


class TkCleanup:
    """tearDown for tests that open a Tk window."""

    def tearDown(self) -> None:
        close_tk_roots()
        super().tearDown()

"""Open the mission viewer without a console window.

Double-click this file, or run it with pythonw:

    pythonw run_gui.pyw
    pythonw run_gui.pyw path\\to\\save2.hg
    pythonw run_gui.pyw --game-files path\\to\\extracted --mapping path\\to\\mapping.json

With no save path, the window lists save slots and copies the one you pick.
If gamefiles or mapping.json sit next to this file, they are used.
If startup fails, the error is shown in a message box.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def command_args(argv: list[str], root: Path | None = None) -> list[str]:
    """Build the gui command. No save path means the slot picker runs."""
    base = ROOT if root is None else root
    if not argv:
        args = ["gui"]
    elif argv[0] == "gui":
        args = list(argv)
    else:
        args = ["gui", *argv]
    if "--game-files" not in args and (base / "gamefiles").is_dir():
        args.extend(["--game-files", str(base / "gamefiles")])
    if "--mapping" not in args and (base / "mapping.json").is_file():
        args.extend(["--mapping", str(base / "mapping.json")])
    return args


def plain_error(message: str) -> str:
    """Show a Windows path with single backslashes in the error box."""
    return message.replace("\\\\", "\\")


def _fail(message: str) -> None:
    message = plain_error(message)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("No Man's Sky mission chains", message)
        root.destroy()
    except Exception:
        sys.stderr.write(message + "\n")


def main(argv: list[str] | None = None) -> int:
    import io
    from contextlib import redirect_stderr

    args = command_args(list(sys.argv[1:] if argv is None else argv))
    from nmsmissions.cli import main as cli_main

    buffer = io.StringIO()
    try:
        with redirect_stderr(buffer):
            code = cli_main(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        _fail(f"The mission viewer did not start.\n{exc}")
        return 1
    if code:
        detail = buffer.getvalue().strip()
        _fail(detail or f"The mission viewer stopped (exit {code}).")
    return code if isinstance(code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())

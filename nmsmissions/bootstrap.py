"""Create a local venv and install requirements. Standard library only.

Start.bat does this on Windows. Tests call setup_folder on an empty unzip.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def python_version_ok(executable: str) -> bool:
    """True when that interpreter is Python 3.10 or newer."""
    try:
        result = subprocess.run(
            [executable, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def setup_folder(root: Path, python: str | None = None) -> Path:
    """Create root/.venv and pip-install requirements.txt. Returns the venv interpreter."""
    folder = Path(root)
    requirements = folder / "requirements.txt"
    if not requirements.is_file():
        raise FileNotFoundError(f"requirements.txt is missing in {folder}")
    executable = python or sys.executable
    if not python_version_ok(executable):
        raise RuntimeError("Python 3.10 or newer is required. Install it from https://www.python.org/downloads/")
    target = venv_python(folder)
    if not target.is_file():
        subprocess.run([executable, "-m", "venv", str(folder / ".venv")], check=True)
    if not target.is_file():
        raise RuntimeError(f"The venv did not create {target}")
    subprocess.run([str(target), "-m", "pip", "install", "-r", str(requirements)], check=True)
    return target

"""Build the release zip from an allowlist.

The zip is the app a fresh PC unzips. It does not include saves, backups,
window settings, or extracted game files. The same allowlist can be copied
to dist/public_repo for a fresh repository. History is not copied.

Generic checks always run: no saves, backups, window settings, absolute
drive paths, home-folder paths, Steam64 ids, or email addresses. A personal
word list is not stored here. NMSMISSIONS_BANNED_FILE can point at a local
uncommitted file. When that file is absent, the personal list is skipped.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

from nmsmissions import __version__

APP_ROOT = Path(__file__).resolve().parents[1]
ZIP_NAME = f"nms-mission-editor-{__version__}.zip"
# Fixed so two builds of the same tree have the same zip bytes.
ZIP_DATE = (2026, 10, 2, 0, 0, 0)

INCLUDE_FILES = (
    "Start.bat",
    "run_gui.pyw",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "VERSION",
    "pyproject.toml",
    "screenshots/README.txt",
)

SKIP_PARTS = {
    "__pycache__",
    ".venv",
    "working_copies",
    "backups",
    "gamefiles",
    "dist",
}

SKIP_SUFFIXES = {".pyc", ".pyo", ".hg", ".mbin", ".exml", ".mxml", ".pak"}
_WORD = re.compile(r"[A-Za-z0-9]+")
# Built in pieces so this file does not contain the text it rejects.
_DRIVE_PATTERNS = (
    re.compile(r"(?<![A-Za-z])[A-Za-z]:" + "\\\\"),
    re.compile(r"(?<![A-Za-z])[A-Za-z]:" + "/"),
)
_HOME_MARKERS = ("/" + "users" + "/", "/" + "home" + "/")
_STEAM_ID = re.compile("7656119" + r"\d{10}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 3-3-4 groups, with dashes, dots, spaces, or brackets. Dates and plain integers do not match.
_PHONE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-])?(?:\(\d{3}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()


def banned_file_path() -> Path | None:
    """The local uncommitted word list, when NMSMISSIONS_BANNED_FILE names a file."""
    raw_path = os.environ.get("NMSMISSIONS_BANNED_FILE", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_file():
        return None
    return path


def personal_list_note() -> str:
    """Said when the personal list is not available. The generic checks still run."""
    if banned_file_path() is None:
        return "Personal list was skipped."
    return ""


def banned_hashes() -> frozenset[str]:
    """SHA-256 of whole tokens in the local banned file. Empty when that file is absent."""
    path = banned_file_path()
    if path is None:
        return frozenset()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    found: set[str] = set()
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        found.add(_digest(token))
        found.add(_digest(token.replace("\\", "/")))
    return frozenset(found)


def generic_problems(text: str) -> list[str]:
    """Structural leaks. The matched text is not repeated."""
    found: list[str] = []
    normalized = text.replace("\\", "/")
    folded = normalized.casefold()
    if any(pattern.search(text) or pattern.search(normalized) for pattern in _DRIVE_PATTERNS):
        found.append("absolute drive path")
    if any(marker in folded for marker in _HOME_MARKERS):
        found.append("home folder path")
    if _STEAM_ID.search(text):
        found.append("steam id")
    if _EMAIL.search(text):
        found.append("email address")
    if _formatted_phone(text):
        found.append("phone number")
    return found


def _formatted_phone(text: str) -> bool:
    """True for a phone written with dashes, dots, brackets, or spaces."""
    return _PHONE.search(text) is not None


def iter_scan_tokens(text: str):
    """Words, plus 2- and 3-segment path windows. Slashes are folded first."""
    normalized = text.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    for word in _WORD.findall(normalized):
        yield word
    for chunk in normalized.split():
        parts: list[str] = []
        for raw in chunk.split("/"):
            if not raw:
                continue
            if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
                parts.append(raw[:2])
                rest = raw[2:].strip("\"'`.,;:()[]{}<>")
                if rest:
                    parts.append(rest)
                continue
            piece = raw.strip("\"'`.,;:()[]{}<>")
            if piece:
                parts.append(piece)
        for size in (2, 3):
            for index in range(0, len(parts) - size + 1):
                yield "/".join(parts[index : index + size])


def scan_text(text: str, hashes: frozenset[str] | None = None) -> list[str]:
    """Generic problems, plus hashes of banned tokens. The token itself is not returned."""
    found = list(generic_problems(text))
    banned = hashes if hashes is not None else banned_hashes()
    if not banned:
        return found
    seen: set[str] = set()
    for token in iter_scan_tokens(text):
        digest = _digest(token)
        if digest in banned and digest not in seen:
            seen.add(digest)
            found.append(digest)
    return found


def release_files(root: Path | None = None) -> list[Path]:
    """Files the zip is allowed to contain."""
    base = APP_ROOT if root is None else root
    found: list[Path] = []
    for name in INCLUDE_FILES:
        path = base / name
        if not path.is_file():
            raise FileNotFoundError(f"Release file is missing: {name}")
        found.append(path)
    package = base / "nmsmissions"
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.relative_to(base).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        found.append(path)
    tests = base / "tests"
    if tests.is_dir():
        for path in sorted(tests.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_PARTS for part in path.relative_to(base).parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            found.append(path)
    return found


def zip_entry_name(path: Path, root: Path) -> str:
    folder = f"nms-mission-editor-{__version__}"
    return f"{folder}/{path.relative_to(root).as_posix()}"


def build_release_zip(dest: Path | None = None, root: Path | None = None) -> Path:
    """Write the allowlisted zip. Returns the zip path."""
    base = APP_ROOT if root is None else root
    target = dest if dest is not None else base / "dist" / ZIP_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    files = release_files(base)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in files:
            info = zipfile.ZipInfo(zip_entry_name(path, base), date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            handle.writestr(info, path.read_bytes())
    return target


def export_public_tree(dest: Path | None = None, root: Path | None = None) -> Path:
    """Copy the allowlisted files into a folder that can be a new repository."""
    base = APP_ROOT if root is None else root
    target = dest if dest is not None else base / "dist" / "public_repo"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in release_files(base):
        relative = path.relative_to(base)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
    return target


def scan_zip(path: Path) -> list[str]:
    """Problems in a built zip. Empty means the zip is safe to hand to someone."""
    problems: list[str] = []
    banned = banned_hashes()
    with zipfile.ZipFile(path) as handle:
        for info in handle.infolist():
            name = info.filename.replace("\\", "/")
            lower = name.lower()
            if lower.endswith("/"):
                continue
            if "/working_copies/" in f"/{lower}" or lower.endswith("/window.json") or lower.endswith("window.json"):
                problems.append(f"personal file: {name}")
            if "/backups/" in f"/{lower}":
                problems.append(f"backup file: {name}")
            suffix = Path(lower).suffix
            if suffix in SKIP_SUFFIXES or suffix == ".zip":
                problems.append(f"game or save file: {name}")
            problems.extend(_scan_hits(name, name, banned))
            raw = handle.read(info)
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                text = raw.decode("utf-8", errors="replace")
            problems.extend(_scan_hits(text, name, banned))
    return problems


def _is_digest(item: str) -> bool:
    return len(item) == 64 and all(char in "0123456789abcdef" for char in item)


def _scan_hits(text: str, label: str, banned: frozenset[str]) -> list[str]:
    hits: list[str] = []
    for item in scan_text(text, banned):
        if _is_digest(item):
            hits.append(f"banned token {item[:12]} in {label}")
        else:
            hits.append(f"{item} in {label}")
    return hits


def main() -> int:
    export_public_tree()
    path = build_release_zip()
    note = personal_list_note()
    if note:
        print(note)
    problems = scan_zip(path)
    print(path)
    print(f"bytes {path.stat().st_size}")
    if problems:
        for item in problems:
            print(item)
        return 1
    with zipfile.ZipFile(path) as handle:
        for info in handle.infolist():
            print(info.filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

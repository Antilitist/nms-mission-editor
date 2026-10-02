"""Read mission, language, and reward tables from the player's own PCBANKS.

The read keeps mission text, every NMS_*_ENGLISH language file, and the
reward, product, substance, and technology tables. Raw MBIN files and the
duplicate US-English files are deleted after conversion. A new read replaces
the previous one.

HGPAKtool (monkeyman192, https://github.com/monkeyman192/HGPAKtool) is MIT.
Start.bat installs it with the other packages on first run. This folder does
not copy the tool's source.

MBINCompiler (monkeyman192) is LGPL-3.0. The program is downloaded into the
cache the first time a read finds binary MBIN files, and it is not part of
the zip. The download is pinned to one version and a SHA-256 for each file.
A mismatch is deleted and the read stops.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from nmsmissions import cache_root

MISSION_FILTER = "*metadata/simulation/missions*"
LANGUAGE_FILTER = "*language/*english*"
REWARD_FILTER = "*rewardtable*"
PRODUCT_FILTER = "*producttable*"
SUBSTANCE_FILTER = "*substancetable*"
TECH_FILTER = "*technologytable*"
FILTERS = (
    MISSION_FILTER,
    LANGUAGE_FILTER,
    REWARD_FILTER,
    PRODUCT_FILTER,
    SUBSTANCE_FILTER,
    TECH_FILTER,
)

COMPILER_VERSION = "v7.04.1-pre3"
COMPILER_PAGE = (
    "https://github.com/monkeyman192/MBINCompiler/releases/download/" + COMPILER_VERSION
)
# SHA-256 of the v7.04.1-pre3 release assets. A different file is refused.
COMPILER_SHA256 = {
    "MBINCompiler.exe": "4179dddb665f7cddbe9dddddf6e529172abdd98b0097f65fdd224467d5bb3ea4",
    "libMBIN.dll": "cc7c35d1bf111a7e01ec933b13b6f879eb95e621c0af2f229dbf492743652184",
    "MBINCompiler-linux": "f65d5bbd36cc841ec82ee50cec13c99e0f0f6ac433b9135f92e2ad4582d0288d",
    "libMBIN-linux.so": "02156b615bf2ef68d1d2b28948381875a6ddb4368c6d9aa747729ff545e507fa",
}
_KEEP_TABLES = ("rewardtable", "producttable", "substancetable", "technologytable")
_CHILD_SCRIPT = r"""
import os
import sys

os.environ["NMSMISSIONS_READ_CHILD"] = "1"
from pathlib import Path
from nmsmissions.gameread import read_game_files

def _progress(message: str) -> None:
    sys.stdout.write("PROGRESS\t" + message + "\n")
    sys.stdout.flush()

banks = Path(sys.argv[1])
raw_dest = sys.argv[2]
dest = Path(raw_dest) if raw_dest else None
try:
    found = read_game_files(banks, dest=dest, progress=_progress)
except Exception as exc:
    sys.stdout.write("ERROR\t" + str(exc) + "\n")
    sys.stdout.flush()
    raise SystemExit(1)
sys.stdout.write("DONE\t" + str(found) + "\n")
sys.stdout.flush()
"""


class GameReadError(RuntimeError):
    """The player's pak files could not be read."""


def compiler_assets() -> tuple[str, str]:
    if os.name == "nt":
        return ("MBINCompiler.exe", "libMBIN.dll")
    return ("MBINCompiler-linux", "libMBIN-linux.so")


def game_read_root() -> Path:
    return cache_root() / "game-read"


def cached_game_read() -> Path | None:
    """The last successful read, if those text files are still on disk."""
    pointer = game_read_root() / "current.txt"
    if not pointer.is_file():
        return None
    try:
        path = Path(pointer.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    if not path.is_dir():
        return None
    if not any(path.rglob("*")):
        return None
    return path


def resolve_pcbanks(path: Path) -> Path | None:
    """Accept PCBANKS, GAMEDATA, or the game install folder."""
    if not path.is_dir():
        return None
    if path.name.lower() == "pcbanks":
        return path
    direct = path / "PCBANKS"
    if direct.is_dir():
        return direct
    nested = path / "GAMEDATA" / "PCBANKS"
    if nested.is_dir():
        return nested
    return None


def read_game_files(
    pcbanks: Path,
    dest: Path | None = None,
    progress=None,
    unpacker=None,
    compiler_runner=None,
) -> Path:
    """Unpack the tables this editor needs into a local cache folder.

    progress is called with a short sentence. It must not touch the window.
    unpacker and compiler_runner are for tests. The real read uses HGPAKtool
    and, when the files are still MBIN, MBINCompiler. That work runs in
    another process so the window thread stays free.
    """
    banks = Path(pcbanks)
    _require_paks(banks)
    in_child = os.environ.get("NMSMISSIONS_READ_CHILD") == "1"
    if in_child or unpacker is not None or compiler_runner is not None:
        return _read_game_files_body(banks, dest, progress, unpacker, compiler_runner)
    return _read_in_child(banks, dest, progress)


def clear_game_cache() -> None:
    """Delete the copied game files and the name and table caches.

    The MBINCompiler download stays, so the next read does not fetch it again.
    """
    root = cache_root()
    read_root = game_read_root()
    if read_root.exists():
        shutil.rmtree(read_root)
    for pattern in ("game-names*.json", "game-tables*.json"):
        for path in root.glob(pattern):
            path.unlink(missing_ok=True)


def pak_record(paks: list[Path]) -> list[dict]:
    """Name, size, and mtime for each pak. This is what a later read compares."""
    rows = []
    for path in sorted(paks, key=lambda item: item.name.lower()):
        stat = path.stat()
        rows.append({"name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return rows


def write_source_record(dest: Path, paks: list[Path]) -> None:
    payload = {"paks": pak_record(paks)}
    (Path(dest) / "source.json").write_text(json.dumps(payload), encoding="utf-8")


def source_matches(root: Path, paks: list[Path]) -> bool | None:
    """True when source.json still matches these paks. None when it is absent."""
    path = Path(root) / "source.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    recorded = document.get("paks")
    if not isinstance(recorded, list):
        return None
    return recorded == pak_record(list(paks))


def _read_game_files_body(
    banks: Path,
    dest: Path | None,
    progress,
    unpacker,
    compiler_runner,
) -> Path:
    paks = _require_paks(banks)
    own_dest = dest is None
    if dest is None:
        staging = game_read_root() / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        dest = staging
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    extract = unpacker if unpacker is not None else _unpack_pak

    total = len(paks)
    for index, pak in enumerate(paks, 1):
        _say(progress, f"Reading pak {index} of {total}: {pak.name}")
        try:
            extract(pak, dest, FILTERS)
        except GameReadError:
            raise
        except Exception as exc:
            raise GameReadError(f"Could not read {pak.name}. ({exc})") from exc

    binaries = _binary_tables(dest)
    if binaries:
        _say(progress, "Converting mission files to text. This can take a few minutes.")
        if compiler_runner is not None:
            compiler_runner(dest)
        else:
            _compile_folder(dest, progress)
        if _binary_tables(dest) and not _text_tables(dest):
            raise GameReadError("The mission files stayed binary, so names could not be read.")

    prune_extracted(dest)
    write_source_record(dest, paks)
    if own_dest:
        dest = _publish_read(dest)
    _say(progress, "Game files are ready.")
    return dest


def _read_in_child(banks: Path, dest: Path | None, progress) -> Path:
    """Run the read in a separate process. The window thread does not hold the work."""
    from nmsmissions.edit import hidden_window_kwargs

    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("NMSMISSIONS_READ_CHILD", None)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(banks), "" if dest is None else str(dest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=root,
        env=env,
        **hidden_window_kwargs(),
    )
    stderr_parts: list[str] = []

    def drain() -> None:
        if proc.stderr is not None:
            stderr_parts.append(proc.stderr.read())

    drainer = threading.Thread(target=drain, name="nms-game-read-err", daemon=True)
    drainer.start()
    result: Path | None = None
    error = ""
    if proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith("PROGRESS\t"):
                _say(progress, line.split("\t", 1)[1].rstrip("\n"))
            elif line.startswith("DONE\t"):
                result = Path(line.split("\t", 1)[1].strip())
            elif line.startswith("ERROR\t"):
                error = line.split("\t", 1)[1].strip()
    code = proc.wait()
    drainer.join(timeout=5)
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    if result is not None and code == 0:
        return result
    detail = error or "".join(stderr_parts).strip()
    if not detail:
        detail = "The game file read stopped before it finished."
    raise GameReadError(detail)


def _require_paks(banks: Path) -> list[Path]:
    if not banks.is_dir():
        raise GameReadError("That folder was not found. Choose the PCBANKS folder inside the game.")
    paks = sorted(path for path in banks.iterdir() if path.is_file() and path.suffix.lower() == ".pak")
    if not paks:
        raise GameReadError("That folder has no pak files. Choose the PCBANKS folder inside the game.")
    return paks


def _publish_read(staging: Path) -> Path:
    """Replace the previous read. Older stamp folders are removed."""
    root = game_read_root()
    root.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    if current.exists():
        shutil.rmtree(current)
    shutil.move(str(staging), str(current))
    (root / "current.txt").write_text(str(current.resolve()), encoding="utf-8")
    for child in list(root.iterdir()):
        if child.name in {"current", "current.txt"}:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    return current


def prune_extracted(dest: Path) -> None:
    """Keep only the converted files the editor reads."""
    for path in list(dest.rglob("*")):
        if not path.is_file() or path.name == "source.json":
            continue
        if not _keep_extracted(path):
            path.unlink(missing_ok=True)
    folders = [path for path in dest.rglob("*") if path.is_dir()]
    for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            continue


def _keep_extracted(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".mbin") or name.endswith(".mbin.pc") or "usenglish" in name:
        return False
    if path.suffix.lower() not in {".exml", ".mxml", ".xml"}:
        return False
    folded = "/" + "/".join(part.lower() for part in path.parts) + "/"
    if "/missions/" in folded or "missiontable" in name:
        return True
    if "/language/" in folded and name.startswith("nms_") and "english" in name:
        return True
    stem = path.stem.lower()
    return any(token in stem for token in _KEEP_TABLES)


def _say(progress, message: str) -> None:
    if progress is not None:
        progress(message)


def _binary_tables(dest: Path) -> list[Path]:
    found: list[Path] = []
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(".mbin") or name.endswith(".mbin.pc"):
            found.append(path)
    return found


def _text_tables(dest: Path) -> bool:
    for path in dest.rglob("*"):
        if path.suffix.lower() in {".exml", ".mxml", ".xml"}:
            return True
    return False


def _unpack_pak(pak: Path, dest: Path, filters: tuple[str, ...]) -> int:
    try:
        from hgpaktool.api import HGPAKFile
    except ImportError as exc:
        raise GameReadError(
            "The pak reader is not installed yet. Run Start.bat once so it can install packages, then try again."
        ) from exc
    with HGPAKFile(pak) as handle:
        return int(handle.unpack(dest, filters=list(filters)))


def compiler_dir() -> Path:
    return cache_root() / "mbincompiler" / COMPILER_VERSION


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_compiler(progress=None, downloader=None, folder: Path | None = None, hashes: dict | None = None) -> Path:
    """Download MBINCompiler into the cache. Returns the program path.

    A file whose SHA-256 is not the pinned one is deleted. Nothing is kept.
    """
    expected = dict(COMPILER_SHA256 if hashes is None else hashes)
    folder = compiler_dir() if folder is None else Path(folder)
    exe_name, dll_name = compiler_assets()
    exe = folder / exe_name
    dll = folder / dll_name
    for path in (exe, dll):
        if path.is_file() and not _hash_ok(path, expected):
            path.unlink()
    if exe.is_file() and dll.is_file() and _hash_ok(exe, expected) and _hash_ok(dll, expected):
        return exe
    folder.mkdir(parents=True, exist_ok=True)
    fetch = downloader if downloader is not None else _download
    for name in (exe_name, dll_name):
        target = folder / name
        if target.is_file() and _hash_ok(target, expected):
            continue
        _say(progress, f"Downloading {name}…")
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            fetch(f"{COMPILER_PAGE}/{name}", temporary)
        except GameReadError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise GameReadError(
                "Could not download the program that turns game files into text. "
                "Check the network, then click Read my game files again."
            ) from exc
        if not _hash_ok(temporary, expected, name):
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise GameReadError(
                "The downloaded program did not match. Nothing was saved. Click Read my game files again."
            )
        temporary.replace(target)
    if os.name != "nt":
        exe.chmod(0o755)
    return exe


def _hash_ok(path: Path, expected: dict, name: str | None = None) -> bool:
    key = name if name is not None else path.name
    want = str(expected.get(key) or "").lower()
    if not want or not path.is_file():
        return False
    return file_sha256(path) == want


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response:
        target.write_bytes(response.read())


def _compile_folder(dest: Path, progress=None) -> None:
    exe = ensure_compiler(progress)
    _say(progress, "Converting mission files to text. This can take a few minutes.")
    from nmsmissions.edit import hidden_window_kwargs

    completed = subprocess.run(
        [str(exe), str(dest)],
        cwd=str(exe.parent),
        check=False,
        capture_output=True,
        **hidden_window_kwargs(),
    )
    if completed.returncode != 0 and not _text_tables(dest):
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        extra = f" ({detail[:180]})" if detail else ""
        raise GameReadError(f"The mission files could not be converted to text.{extra}")


def forget_compiler_download(folder: Path | None = None) -> None:
    """Test helper. Removes a compiler download from a cache folder."""
    target = folder if folder is not None else compiler_dir()
    if target.is_dir():
        shutil.rmtree(target)

"""Download and cache MBINCompiler mapping.json. That file is not bundled.

mapping.json maps the 3-character save keys to field names. Its license on
the MBINCompiler repo is not a normal open-source grant (NOASSERTION), so
this tool fetches a named release at runtime instead of shipping a copy.

If that download fails, a short key list written for this tool is used.
That list is not MBINCompiler's mapping.json. It is enough to open a save
and show mission progress while offline.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from nmsmissions import cache_root

MAPPING_VERSION = "v7.04.1-pre3"
MAPPING_URL = (
    "https://github.com/monkeyman192/MBINCompiler/releases/download/"
    f"{MAPPING_VERSION}/mapping.json"
)


class MappingError(RuntimeError):
    """mapping.json could not be loaded."""


def default_cache_path() -> Path:
    return cache_root() / f"mapping-{MAPPING_VERSION}.json"


def parse_mapping(document: dict) -> dict[str, str]:
    """Return obfuscated key -> field name. Unknown keys are simply absent."""
    rows = document.get("Mapping")
    if not isinstance(rows, list):
        raise MappingError("mapping.json has no Mapping list.")
    table: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("Key")
        value = row.get("Value")
        if isinstance(key, str) and isinstance(value, str) and key not in table:
            table[key] = value
    if not table:
        raise MappingError("mapping.json Mapping list was empty.")
    return table


def bundled_mapping_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "mapping-fallback.json"


def load_bundled_mapping() -> tuple[dict[str, str], str]:
    """The offline key list shipped with this tool."""
    path = bundled_mapping_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingError(f"The built-in key list could not be read: {exc}") from exc
    return parse_mapping(document), "built-in key list"


def load_mapping(path: Path | None = None, url: str = MAPPING_URL, cache: Path | None = None) -> tuple[dict[str, str], str]:
    """Load a mapping. Returns (table, source description).

    A path is used as-is. Otherwise a cached download is reused, then a
    download is tried. If that fails, the built-in key list is used so the
    first run still opens a save.
    """
    if path is not None:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MappingError(f"Could not read mapping file {path}: {exc}") from exc
        return parse_mapping(document), str(path)

    cache_path = cache if cache is not None else default_cache_path()
    if cache_path.is_file():
        try:
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            return parse_mapping(document), str(cache_path)
        except (OSError, json.JSONDecodeError, MappingError):
            pass

    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
        document = json.loads(payload.decode("utf-8"))
        table = parse_mapping(document)
    except Exception:
        return load_bundled_mapping()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return table, url

"""Game-file read. Synthetic paks only. No Hello Games install is used."""

from __future__ import annotations


def _isolate_test_dirs() -> None:
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from tests.harness import isolate_test_dirs
    except ImportError:
        from harness import isolate_test_dirs

    isolate_test_dirs()


_isolate_test_dirs()


import hashlib
import io
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nmsmissions import cache_root
from nmsmissions.gameread import (
    FILTERS,
    GameReadError,
    clear_game_cache,
    compiler_assets,
    compiler_dir,
    ensure_compiler,
    game_read_root,
    read_game_files,
    resolve_pcbanks,
)


class GameReadTests(unittest.TestCase):
    def test_reader_keeps_reward_tables_and_drops_the_rest(self) -> None:
        seen = {}

        def unpack(pak: Path, dest: Path, filters) -> int:
            seen["filters"] = tuple(filters)
            (dest / "metadata" / "simulation" / "missions").mkdir(parents=True)
            (dest / "language").mkdir()
            (dest / "metadata" / "reality" / "tables").mkdir(parents=True)
            (dest / "metadata" / "simulation" / "missions" / "demo.mbin").write_bytes(b"MBIN" + b"x" * (2 * 1024 * 1024))
            (dest / "language" / "nms_loc1_english.mbin").write_bytes(b"MBIN")
            (dest / "language" / "nms_loc1_usenglish.mbin").write_bytes(b"MBIN" + b"u" * (2 * 1024 * 1024))
            (dest / "metadata" / "reality" / "tables" / "rewardtable.mbin").write_bytes(b"MBIN")
            (dest / "metadata" / "reality" / "tables" / "nms_reality_gcproducttable.mbin").write_bytes(b"MBIN")
            (dest / "textures").mkdir()
            (dest / "textures" / "junk.bin").write_bytes(b"nope" * 1000)
            return 4

        def compile_folder(dest: Path) -> None:
            for path in list(dest.rglob("*.mbin")):
                path.with_suffix(".exml").write_text("<Data/>", encoding="utf-8")
                path.unlink()

        messages: list[str] = []
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            banks = folder / "PCBANKS"
            banks.mkdir()
            (banks / "NMSARC.pak").write_bytes(b"pak")
            dest = read_game_files(
                banks,
                dest=folder / "out",
                progress=messages.append,
                unpacker=unpack,
                compiler_runner=compile_folder,
            )
            self.assertEqual(seen["filters"], FILTERS)
            self.assertTrue((dest / "metadata" / "simulation" / "missions" / "demo.exml").is_file())
            self.assertTrue((dest / "language" / "nms_loc1_english.exml").is_file())
            self.assertTrue((dest / "metadata" / "reality" / "tables" / "rewardtable.exml").is_file())
            self.assertTrue((dest / "metadata" / "reality" / "tables" / "nms_reality_gcproducttable.exml").is_file())
            self.assertFalse(list(dest.rglob("*.mbin")))
            self.assertFalse(any("usenglish" in path.name.lower() for path in dest.rglob("*")))
            self.assertFalse((dest / "textures" / "junk.bin").exists())
            self.assertTrue((dest / "source.json").is_file())
            size = sum(path.stat().st_size for path in dest.rglob("*") if path.is_file())
            print(f"fixture cache bytes {size}")
            self.assertLess(size, 1024 * 1024)
            self.assertTrue(any("Reading pak 1 of 1" in item for item in messages))
            self.assertTrue(any("Converting" in item for item in messages))
            self.assertIsNone(resolve_pcbanks(folder / "missing"))
            install = folder / "NoMansSky"
            (install / "GAMEDATA" / "PCBANKS").mkdir(parents=True)
            self.assertEqual(resolve_pcbanks(install), install / "GAMEDATA" / "PCBANKS")

    def test_missing_paks_explain_the_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(GameReadError) as raised:
                read_game_files(Path(tmp), dest=Path(tmp) / "out", unpacker=lambda *_args: 0)
        self.assertIn("PCBANKS", str(raised.exception))

    def test_tiny_pak_unpacks_without_the_compiler(self) -> None:
        try:
            from hgpaktool.api import HGPAKFile
        except ImportError:
            self.skipTest("hgpaktool is not installed")
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "src"
            (source / "metadata" / "simulation" / "missions").mkdir(parents=True)
            (source / "metadata" / "reality" / "tables").mkdir(parents=True)
            (source / "language").mkdir()
            (source / "textures").mkdir()
            mission = (
                "<Data><Property name='MissionID' value='^ACT1_STEP1'/>"
                "<Property name='MissionTitles'><Property value='TkLocalisationString'>"
                "<Property name='Id' value='AWAKE'/></Property></Property></Data>"
            )
            (source / "metadata" / "simulation" / "missions" / "demo.exml").write_text(mission, encoding="utf-8")
            (source / "language" / "nms_loc1_english.exml").write_text(
                "<Data><Property><Property name='Id' value='AWAKE'/>"
                "<Property name='English' value='Awakenings'/></Property></Data>",
                encoding="utf-8",
            )
            (source / "language" / "nms_loc1_usenglish.exml").write_text(
                "<Data><Property><Property name='Id' value='AWAKE'/>"
                "<Property name='English' value='Wrong'/></Property></Data>",
                encoding="utf-8",
            )
            (source / "metadata" / "reality" / "tables" / "rewardtable.exml").write_text(
                "<Data><Property name='Id' value='R_PAY'/></Data>",
                encoding="utf-8",
            )
            (source / "textures" / "junk.bin").write_bytes(b"nope")
            manifest = source / "sample.txt"
            manifest.write_bytes(
                b"metadata/simulation/missions/demo.exml\r\n"
                b"language/nms_loc1_english.exml\r\n"
                b"language/nms_loc1_usenglish.exml\r\n"
                b"metadata/reality/tables/rewardtable.exml\r\n"
                b"textures/junk.bin\r\n"
            )
            banks = folder / "PCBANKS"
            banks.mkdir()
            HGPAKFile.repack(manifest, banks / "NMSARC.pak", compress=False)
            started = time.perf_counter()
            dest = read_game_files(banks, dest=folder / "out")
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 30)
            self.assertTrue((dest / "metadata" / "simulation" / "missions" / "demo.exml").is_file())
            self.assertTrue((dest / "language" / "nms_loc1_english.exml").is_file())
            self.assertFalse((dest / "textures" / "junk.bin").exists())
            kept = [path.name.lower() for path in dest.rglob("*") if path.is_file()]
            self.assertIn("demo.exml", kept)
            self.assertIn("nms_loc1_english.exml", kept)
            self.assertIn("rewardtable.exml", kept)
            self.assertNotIn("nms_loc1_usenglish.exml", kept)
            print(f"synthetic pak read seconds {elapsed:.3f}")

    def test_second_read_replaces_the_previous_cache(self) -> None:
        calls = {"n": 0}

        def unpack(pak: Path, dest: Path, filters) -> int:
            calls["n"] += 1
            (dest / "language").mkdir(parents=True, exist_ok=True)
            (dest / "language" / "nms_loc1_english.exml").write_text(f"<Data>{calls['n']}</Data>", encoding="utf-8")
            (dest / "language" / "nms_loc1_usenglish.exml").write_text("<Data>us</Data>", encoding="utf-8")
            (dest / "old.mbin").write_bytes(b"mbin")
            return 1

        with TemporaryDirectory() as tmp:
            banks = Path(tmp) / "PCBANKS"
            banks.mkdir()
            (banks / "NMSARC.pak").write_bytes(b"pak")
            first = read_game_files(banks, unpacker=unpack, compiler_runner=lambda dest: None)
            second = read_game_files(banks, unpacker=unpack, compiler_runner=lambda dest: None)
            self.assertEqual(first, second)
            self.assertEqual((second / "language" / "nms_loc1_english.exml").read_text(encoding="utf-8"), "<Data>2</Data>")
            self.assertFalse((second / "old.mbin").exists())
            self.assertFalse(any("usenglish" in path.name.lower() for path in second.rglob("*")))
            folders = [path.name for path in game_read_root().iterdir() if path.is_dir()]
            self.assertEqual(folders, ["current"])

    def test_clear_cache_keeps_the_compiler_download(self) -> None:
        read = game_read_root() / "current" / "language"
        read.mkdir(parents=True)
        (read / "nms_loc1_english.exml").write_text("<Data/>", encoding="utf-8")
        compiler = compiler_dir()
        compiler.mkdir(parents=True)
        marker = compiler / compiler_assets()[0]
        marker.write_bytes(b"compiler")
        names = cache_root() / "game-names-abc.json"
        names.write_text("{}", encoding="utf-8")
        clear_game_cache()
        self.assertFalse(game_read_root().exists())
        self.assertFalse(names.exists())
        self.assertTrue(marker.is_file())

    def test_compiler_download_must_match_the_pinned_hash(self) -> None:
        exe_name, dll_name = compiler_assets()
        good = b"compiler-bytes"
        digest = hashlib.sha256(good).hexdigest()
        hashes = {exe_name: digest, dll_name: digest}

        def download_bad(url: str, target: Path) -> None:
            name = url.rsplit("/", 1)[-1]
            target.write_bytes(b"nope" if name == exe_name else good)

        def download_ok(url: str, target: Path) -> None:
            target.write_bytes(good)

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(GameReadError) as raised:
                ensure_compiler(folder=folder, downloader=download_bad, hashes=hashes)
            self.assertIn("did not match", str(raised.exception))
            self.assertIn("Nothing was saved", str(raised.exception))
            self.assertEqual(list(folder.iterdir()), [])
            exe = ensure_compiler(folder=folder, downloader=download_ok, hashes=hashes)
            self.assertTrue(exe.is_file())
            self.assertEqual(exe.read_bytes(), good)

    def test_read_without_a_test_unpacker_starts_a_process(self) -> None:
        class Fake:
            def __init__(self) -> None:
                self.stdout = io.StringIO("PROGRESS\tReading pak 1 of 1: a.pak\nDONE\t/tmp/nms-read-out\n")
                self.stderr = io.StringIO("")

            def wait(self) -> int:
                return 0

        with TemporaryDirectory() as tmp:
            banks = Path(tmp) / "PCBANKS"
            banks.mkdir()
            (banks / "a.pak").write_bytes(b"pak")
            messages: list[str] = []
            with patch("nmsmissions.gameread.subprocess.Popen", return_value=Fake()) as popen:
                dest = read_game_files(banks, progress=messages.append)
            self.assertTrue(popen.called)
            self.assertEqual(dest, Path("/tmp/nms-read-out"))
            self.assertTrue(any("Reading pak" in item for item in messages))
            command = popen.call_args.args[0]
            self.assertEqual(command[0], __import__("sys").executable)
            self.assertNotIn("MainThread-blocked", command)


if __name__ == "__main__":
    unittest.main()

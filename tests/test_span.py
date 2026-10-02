"""Span patches and the mf_ manifest. Synthetic bytes only."""

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


def setUpModule() -> None:
    _isolate_test_dirs()


import struct
import unittest
from pathlib import Path

from nmsmissions.manifest import (
    FORMAT_2004,
    MAGIC,
    Manifest,
    decrypt_manifest,
    encrypt_manifest,
    key_slot_for,
    size_bytes_differ_only_at_sizes,
    with_sizes,
)
from nmsmissions.spanpatch import append_patch, apply, dumps, locate, replace_patch, unchanged_outside


class SpanTests(unittest.TestCase):
    def test_this_file_keeps_cache_and_log_out_of_the_real_folders(self):
        from nmsmissions import cache_root
        from nmsmissions.gui import window_settings_path
        from nmsmissions.timing import log_path

        home = Path.home() / ".cache" / "nms_mission_editor"
        app = Path(__file__).resolve().parents[1]
        self.assertNotEqual(cache_root().resolve(), home.resolve())
        self.assertNotEqual(log_path().resolve(), (app / "timing.log").resolve())
        self.assertNotEqual(window_settings_path().resolve(), (app / "window.json").resolve())
        self.assertIn("nmsmissions-test-", str(cache_root()))

    def test_replace_append_and_untouched_float(self):
        text = (
            '{"keep":0.30000001192092898,"flag":false,"rows":[{"id":"a"}],'
            '"odd":"\\udc80"}'
        )
        flag = replace_patch(text, ["flag"], "true", "flag")
        row = append_patch(text, ["rows"], dumps({"id": "b"}), "row")
        patched = apply(text, [flag, row])
        self.assertIn("0.30000001192092898", patched)
        self.assertIn('"odd":"\\udc80"', patched)
        self.assertTrue(unchanged_outside(text, patched, [flag, row]))
        self.assertEqual(locate(patched, ["flag"]), (patched.index("true"), patched.index("true") + 4))
        self.assertEqual(apply(text, []), text)

    def test_manifest_round_trip_changes_only_the_size_fields(self):
        words = [0] * (432 // 4)
        words[0] = MAGIC
        words[1] = FORMAT_2004
        words[14] = 1000
        words[15] = 400
        words[19] = 50
        plain = struct.pack("<" + "I" * len(words), *words)
        slot = key_slot_for(6)
        blob = encrypt_manifest(plain, slot)
        back, used = decrypt_manifest(blob, 6)
        self.assertEqual(used, slot)
        self.assertEqual(back, plain)
        manifest = Manifest(
            path=Path("mf_save7.hg"),
            archive=6,
            key_slot=slot,
            rounds=6,
            plain=plain,
            magic=MAGIC,
            format_version=FORMAT_2004,
            decompressed_size=1000,
            compressed_size=400,
            total_play_time=50,
        )
        plain2, _slot = decrypt_manifest(with_sizes(manifest, 1004, 408), 6)
        self.assertTrue(size_bytes_differ_only_at_sizes(plain, plain2))
        self.assertNotEqual(plain, plain2)


if __name__ == "__main__":
    unittest.main()

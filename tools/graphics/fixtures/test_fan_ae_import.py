"""Check standalone importer regeneration when a cast starts with invisible draws."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from make_fan_ae_import import prepare


class FanAeImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "other-pass").touch()
        self.sources, rows = [], []
        for i in range(4):
            source = self.directory / f"game-{i}.png"
            Image.new("RGB", (2, 2), (i, 0, 0)).save(source)
            self.sources.append(source)
            rows.append({
                "number": i + 1, "sourceIndex": i + 10, "present": i + 100, "qpc": i + 1000,
                "startSeconds": i / 50, "observedSupportPixels": int(i == 3), "exportedSupportPixels": int(i == 3),
                "stages": [{}] if i in (1, 3) else [],
                "files": {"game": source.name, "black": "other-pass", "multiply": "other-pass", "add": "other-pass"},
                "outputSha256": {"game": hashlib.sha256(source.read_bytes()).hexdigest()},
            })
        recorded = rows[0]
        self.manifest = {
            "crop": {"width": 2, "height": 2}, "timing": {"durationSeconds": 4 / 50}, "frames": rows,
            "autoSelection": {"cleanPlate": {k: recorded[k] for k in ("sourceIndex", "present", "qpc")}},
            "aeCleanPlate": {"present": recorded["present"], "number": recorded["number"],
                             "sha256": recorded["outputSha256"]["game"]},
        }

    def write_manifest(self):
        (self.directory / "sequence.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_regenerate_with_same_selected_plate_before_invisible_draws(self):
        self.write_manifest()
        script = prepare(self.directory, self.sources[0])
        content = script.read_bytes()
        self.assertEqual(prepare(self.directory), script)
        self.assertEqual(script.read_bytes(), content)
        self.assertEqual((self.directory / "clean-plate.png").read_bytes(), self.sources[0].read_bytes())

    def test_manual_manifest_keeps_last_draw_free_frame_before_visible_activity(self):
        del self.manifest["autoSelection"]
        del self.manifest["aeCleanPlate"]
        self.write_manifest()
        prepare(self.directory)
        self.assertEqual((self.directory / "clean-plate.png").read_bytes(), self.sources[2].read_bytes())

    def test_selected_identity_and_hash_are_verified(self):
        point = self.manifest["autoSelection"]["cleanPlate"]
        for key, wrong, error in (("sourceIndex", 99, "exported source index"), ("present", 101, "present and QPC"),
                                  ("qpc", 999, "present and QPC")):
            with self.subTest(key=key):
                value, point[key] = point[key], wrong
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, error):
                    prepare(self.directory)
                point[key] = value
        self.manifest["aeCleanPlate"]["sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "saved clean plate provenance"):
            prepare(self.directory)
        del self.manifest["aeCleanPlate"]
        self.write_manifest()
        self.sources[0].write_bytes(self.sources[2].read_bytes())
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            prepare(self.directory)


if __name__ == "__main__":
    unittest.main()

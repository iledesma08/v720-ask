"""Tests for facewatch/clips/settings logic (#25).

No camera or network needed.
Run on the Pi:
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ap_gateway as ag


class PickBestTest(unittest.TestCase):
    def test_most_faces_wins(self):
        self.assertEqual(ag._pick_best([0, 2, 1], [9.0, 1.0, 1.0]), 1)

    def test_motion_breaks_ties(self):
        self.assertEqual(ag._pick_best([1, 1], [5.0, 9.0]), 1)

    def test_single_candidate(self):
        self.assertEqual(ag._pick_best([0], [0.0]), 0)


class CaptureSettingsValidationTest(unittest.TestCase):
    def test_valid_modes(self):
        for mode in ("shots", "clips"):
            cleaned, err = ag._validate_settings({"capture_mode": mode})
            self.assertIsNone(err)
            self.assertEqual(cleaned["capture_mode"], mode)

    def test_bad_mode(self):
        _, err = ag._validate_settings({"capture_mode": "video"})
        self.assertIsNotNone(err)

    def test_valid_triggers(self):
        for trig in ("motion", "faces", "both"):
            cleaned, err = ag._validate_settings({"capture_trigger": trig})
            self.assertIsNone(err)
            self.assertEqual(cleaned["capture_trigger"], trig)

    def test_bad_trigger(self):
        _, err = ag._validate_settings({"capture_trigger": "sound"})
        self.assertIsNotNone(err)

    def test_clip_ranges(self):
        cleaned, err = ag._validate_settings(
            {"clip_sec": 10, "clip_cooldown_sec": 30})
        self.assertIsNone(err)
        _, err = ag._validate_settings({"clip_sec": 2})
        self.assertIsNotNone(err)
        _, err = ag._validate_settings({"clip_cooldown_sec": 301})
        self.assertIsNotNone(err)


class LoadClampTest(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as fh:
                json.dump(data, fh)
            with patch.object(ag, "SETTINGS_PATH", path):
                return ag._load_settings()

    def test_unknown_mode_falls_back(self):
        self.assertEqual(self._load({"capture_mode": "video"})[
            "capture_mode"], "shots")

    def test_unknown_trigger_falls_back(self):
        self.assertEqual(self._load({"capture_trigger": "sound"})[
            "capture_trigger"], "both")

    def test_clip_sec_clamped(self):
        self.assertEqual(self._load({"clip_sec": 999})["clip_sec"], 60.0)

    def test_retention_zero_kept(self):
        self.assertEqual(
            self._load({"snap_retention_days": 0})["snap_retention_days"], 0)


class ListShotsTest(unittest.TestCase):
    def _list(self, names):
        with tempfile.TemporaryDirectory() as d:
            for n in names:
                open(os.path.join(d, n), "wb").write(b"x")
            with patch.object(ag, "SNAP_DIR", d):
                return ag._list_shots()

    def test_combined_infixes(self):
        out = self._list(["ap-camera-20260911-120000-auto-face.mp4"])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["face"])
        self.assertEqual(out[0]["src"], "auto")
        self.assertEqual(out[0]["kind"], "video")

    def test_manual_shot(self):
        out = self._list(["ap-camera-20260911-120000-manual.jpg"])
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["face"])
        self.assertEqual(out[0]["src"], "manual")
        self.assertEqual(out[0]["kind"], "shot")

    def test_junk_ignored(self):
        self.assertEqual(self._list(["notes.txt", ".part-x.mp4"]), [])


if __name__ == "__main__":
    unittest.main()

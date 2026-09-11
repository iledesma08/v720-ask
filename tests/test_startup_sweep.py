"""Tests for boot sweep of orphan mux temp files (#25, quick-wins).

Run on the Pi:
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ap_gateway as ag


class SweepTest(unittest.TestCase):
    def test_removes_part_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            part = os.path.join(d, ".part-ap-camera-20260911-120000.mp4")
            keep = os.path.join(d, "ap-camera-20260911-120000-auto.mp4")
            open(part, "wb").write(b"x")
            open(keep, "wb").write(b"y")
            self.assertEqual(ag._sweep_part_files(d), 1)
            self.assertFalse(os.path.exists(part))
            self.assertTrue(os.path.exists(keep))

    def test_missing_dir_is_zero(self):
        self.assertEqual(
            ag._sweep_part_files("/nonexistent-dir-xyz"), 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for retention settings (#25).

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


class RetentionSettingsTest(unittest.TestCase):
    def test_valid(self):
        cleaned, err = ag._validate_settings({"snap_retention_days": 7})
        self.assertIsNone(err)
        self.assertEqual(cleaned["snap_retention_days"], 7)

    def test_zero_disables(self):
        cleaned, err = ag._validate_settings({"snap_retention_days": 0})
        self.assertIsNone(err)
        self.assertEqual(cleaned["snap_retention_days"], 0)

    def test_negative_rejected(self):
        _, err = ag._validate_settings({"snap_retention_days": -1})
        self.assertIsNotNone(err)

    def test_huge_rejected(self):
        _, err = ag._validate_settings({"snap_retention_days": 400})
        self.assertIsNotNone(err)

    def test_non_number_rejected(self):
        _, err = ag._validate_settings({"snap_retention_days": "week"})
        self.assertIsNotNone(err)

    def test_zero_kept_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as fh:
                json.dump({"snap_retention_days": 0}, fh)
            with patch.object(ag, "SETTINGS_PATH", path):
                self.assertEqual(
                    ag._load_settings()["snap_retention_days"], 0)


if __name__ == "__main__":
    unittest.main()

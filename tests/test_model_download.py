"""Tests for YuNet model download robustness (#25, quick-wins).

Run on the Pi (needs opencv for the download path):
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

try:
    import cv2  # noqa: F401

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

import ap_gateway as ag


@unittest.skipUnless(HAS_CV2, "needs opencv")
class DownloadTimeoutTest(unittest.TestCase):
    def test_download_has_timeout_and_fails_soft(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            raise OSError("net down")

        with tempfile.TemporaryDirectory() as d:
            model = os.path.join(d, "yunet.onnx")
            with patch.dict(os.environ, {"FACE_MODEL": model}):
                with patch("urllib.request.urlopen",
                           side_effect=fake_urlopen):
                    self.assertIsNone(ag._detect_faces(b"notajpeg"))
        self.assertEqual(seen.get("timeout"), 30)


if __name__ == "__main__":
    unittest.main()

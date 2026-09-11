"""Tests for the live-session audio buffer cap (#25).

Run on the Pi:
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ap_gateway as ag


class AudioCapTest(unittest.TestCase):
    def setUp(self):
        with ag._audio_lock:
            del ag._audio_chunks[:]

    def tearDown(self):
        with ag._audio_lock:
            del ag._audio_chunks[:]

    def test_trims_to_cap_keeping_newest(self):
        chunk = b"a" * 1024
        for _ in range(300):
            ag._push_audio(chunk)
        with ag._audio_lock:
            total = sum(len(c) for c in ag._audio_chunks)
        self.assertLessEqual(total, ag._audio_cap)
        self.assertGreater(total, 0)
        with ag._audio_lock:
            self.assertEqual(ag._audio_chunks[-1], chunk)

    def test_small_amount_untouched(self):
        ag._push_audio(b"abc")
        with ag._audio_lock:
            self.assertEqual(list(ag._audio_chunks), [b"abc"])


if __name__ == "__main__":
    unittest.main()

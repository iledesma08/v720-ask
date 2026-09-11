"""Pure-function tests for Telegram motion alerts (#30).

No camera or network needed (the HTTP send is mocked).
Run on the Pi:
    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ap_gateway as ag


class WindowTest(unittest.TestCase):
    def test_inside_day_window(self):
        self.assertTrue(ag._hour_in_window(2, 0, 5))

    def test_outside_day_window(self):
        self.assertFalse(ag._hour_in_window(6, 0, 5))
        self.assertFalse(ag._hour_in_window(23, 0, 5))

    def test_overnight_wrap(self):
        self.assertTrue(ag._hour_in_window(23, 22, 5))
        self.assertTrue(ag._hour_in_window(3, 22, 5))
        self.assertFalse(ag._hour_in_window(12, 22, 5))
        self.assertFalse(ag._hour_in_window(6, 22, 5))

    def test_boundaries(self):
        self.assertTrue(ag._hour_in_window(0, 0, 5))
        self.assertFalse(ag._hour_in_window(5, 0, 5))

    def test_equal_means_always(self):
        for h in (0, 5, 12, 23):
            self.assertTrue(ag._hour_in_window(h, 0, 0))


class CooldownTest(unittest.TestCase):
    def test_first_event_passes(self):
        self.assertTrue(ag._cooldown_ok(100.0, None, 300.0))

    def test_elapsed_passes(self):
        self.assertTrue(ag._cooldown_ok(400.0, 100.0, 300.0))

    def test_inside_cooldown_blocks(self):
        self.assertFalse(ag._cooldown_ok(200.0, 100.0, 300.0))


class DecisionTest(unittest.TestCase):
    def test_send(self):
        send, reason = ag._alert_decision(True, 2, 0, 5, 1000.0, None, 300.0)
        self.assertTrue(send)

    def test_disabled(self):
        send, reason = ag._alert_decision(False, 2, 0, 5, 1000.0, None, 300.0)
        self.assertFalse(send)
        self.assertEqual(reason, "disabled")

    def test_out_of_window(self):
        send, reason = ag._alert_decision(True, 12, 0, 5, 1000.0, None, 300.0)
        self.assertFalse(send)
        self.assertEqual(reason, "out-of-window")

    def test_cooldown(self):
        send, reason = ag._alert_decision(True, 2, 0, 5, 1100.0, 1000.0, 300.0)
        self.assertFalse(send)
        self.assertEqual(reason, "cooldown")


class SettingsValidationTest(unittest.TestCase):
    def test_valid_keys(self):
        cleaned, err = ag._validate_settings({
            "telegram_enabled": True, "alert_start_hour": 22,
            "alert_end_hour": 5, "alert_cooldown_sec": 300})
        self.assertIsNone(err)
        self.assertTrue(cleaned["telegram_enabled"])
        self.assertEqual(cleaned["alert_start_hour"], 22)
        self.assertEqual(cleaned["alert_end_hour"], 5)

    def test_bad_hour(self):
        _, err = ag._validate_settings({"alert_start_hour": 24})
        self.assertIsNotNone(err)

    def test_bad_cooldown(self):
        _, err = ag._validate_settings({"alert_cooldown_sec": 5})
        self.assertIsNotNone(err)

    def test_bad_enabled(self):
        _, err = ag._validate_settings({"telegram_enabled": "yes"})
        self.assertIsNotNone(err)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


class SendTest(unittest.TestCase):
    def test_ok(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResp({"ok": True})) as urlopen:
            ok, err = ag._telegram_send("tok", "123", b"JPEG", "cap")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        req = urlopen.call_args[0][0]
        self.assertIn("sendPhoto", req.full_url)
        self.assertIn(b"photo", req.data)

    def test_api_error(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResp(
                       {"ok": False, "description": "bad chat id"})):
            ok, err = ag._telegram_send("tok", "123", b"JPEG", "cap")
        self.assertFalse(ok)
        self.assertIn("bad chat id", err)

    def test_network_error(self):
        with patch("urllib.request.urlopen",
                   side_effect=OSError("net down")):
            ok, err = ag._telegram_send("tok", "123", b"JPEG", "cap")
        self.assertFalse(ok)
        self.assertTrue(err)


class DeliverTest(unittest.TestCase):
    def test_retry_then_success(self):
        calls = []

        def sender(token, chat, jpeg, caption):
            calls.append((token, chat))
            if len(calls) < 3:
                return False, "boom"
            return True, ""

        ok = ag._deliver_alert("tok", "123", b"JPEG", "cap",
                               sender=sender, delays=(0, 0))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)

    def test_drop_after_retries(self):
        calls = []

        def sender(token, chat, jpeg, caption):
            calls.append((token, chat))
            return False, "boom"

        ok = ag._deliver_alert("tok", "123", b"JPEG", "cap",
                               sender=sender, delays=(0, 0))
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()

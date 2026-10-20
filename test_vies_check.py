"""Offline tests (no network): python -m unittest -v"""
import unittest
from unittest import mock

import vies_check as v


class FormatTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(v.normalize(" de 811.569-869 "), ("DE", "811569869"))
        self.assertEqual(v.normalize("GR094014298"), ("EL", "094014298"))

    def test_formats(self):
        good = ["IE6388047V", "DE811569869", "ATU12345678", "NL123456789B01", "FRXX123456789",
                "ESA12345678", "BE0403170701", "HU12345678", "XIGD123", "CY12345678X"]
        bad = ["DE12345", "ATX12345678", "NL123456789", "FRIO123456789", "US123456789"]
        for s in good:
            self.assertTrue(v.format_ok(*v.normalize(s)), s)
        for s in bad:
            self.assertFalse(v.format_ok(*v.normalize(s)), s)

    def test_bad_format_skips_network(self):
        with mock.patch.object(v, "_post_json") as post:
            r = v.check_vies("DE123")
        post.assert_not_called()
        self.assertEqual(r["status"], "bad_format")


class ViesTests(unittest.TestCase):
    def test_valid(self):
        resp = {"valid": True, "name": "GOOGLE IRELAND LIMITED", "address": "DUBLIN 4",
                "requestDate": "2026-10-20T08:54:45Z", "requestIdentifier": "WAPIAAAA"}
        with mock.patch.object(v, "_post_json", return_value=resp):
            r = v.check_vies("IE6388047V")
        self.assertEqual((r["status"], r["valid"], r["consultation_number"]), ("valid", True, "WAPIAAAA"))

    def test_unavailable_is_not_invalid(self):
        down = {"actionSucceed": False, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}
        with mock.patch.object(v, "_post_json", return_value=down) as post, mock.patch.object(v.time, "sleep"):
            r = v.check_vies("DE811569869", retries=2)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(r["status"], "unavailable")
        self.assertIsNone(r["valid"])

    def test_retry_then_success(self):
        down = {"actionSucceed": False, "errorWrappers": [{"error": "MS_MAX_CONCURRENT_REQ"}]}
        ok = {"valid": False, "name": "---", "address": "---", "requestIdentifier": ""}
        with mock.patch.object(v, "_post_json", side_effect=[down, ok]), mock.patch.object(v.time, "sleep"):
            r = v.check_vies("DE811569869")
        self.assertEqual(r["status"], "invalid")
        self.assertIsNone(r["name"])

    def test_non_retryable_error(self):
        err = {"actionSucceed": False, "errorWrappers": [{"error": "INVALID_INPUT"}]}
        with mock.patch.object(v, "_post_json", return_value=err) as post:
            r = v.check_vies("DE811569869")
        self.assertEqual(post.call_count, 1)
        self.assertEqual((r["status"], r["error"]), ("error", "INVALID_INPUT"))

    def test_fallback_only_when_unavailable(self):
        down = {"actionSucceed": False, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}
        fb = {"status": "valid", "valid": True, "fallback": True, "source": "CZ ARES"}
        with mock.patch.object(v, "_post_json", return_value=down), mock.patch.object(v.time, "sleep"), \
                mock.patch.object(v, "check_wsystem", return_value=fb) as ws:
            r = v.check("CZ12345678", fallback="wsystem", retries=1)
        ws.assert_called_once_with("CZ12345678")
        self.assertEqual(r["status"], "unavailable")
        self.assertTrue(r["fallback_result"]["fallback"])


if __name__ == "__main__":
    unittest.main()

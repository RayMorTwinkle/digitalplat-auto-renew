import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import os

from scripts.digitalplat_auto_renew import (
    parse_datetime,
    normalize_domain,
    parse_domain_variable,
    load_state,
    save_state,
    should_renew,
    remove_stale_domains,
    update_state_for_record,
    DomainRecord,
    unwrap_response,
)


class TestDigitalPlatAutoRenew(unittest.TestCase):
    def test_parse_datetime_iso(self):
        dt = parse_datetime("2026-12-31T23:59:59Z")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 31)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_datetime_date_only(self):
        dt = parse_datetime("2026-10-19")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 19)

    def test_parse_datetime_slash_format(self):
        dt = parse_datetime("2026/10/19")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 19)

    def test_parse_datetime_yyyymmdd(self):
        dt = parse_datetime("20261019")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 19)

    def test_parse_datetime_unix_timestamp(self):
        # 1760860800 = 2025-10-19 08:00:00 UTC (or similar valid date)
        dt = parse_datetime(1760860800)
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.tzinfo, timezone.utc)

        # String 10-digit timestamp
        dt_str = parse_datetime("1760860800")
        self.assertEqual(dt, dt_str)

        # Millisecond timestamp
        dt_ms = parse_datetime(1760860800000)
        self.assertEqual(dt, dt_ms)

    def test_parse_datetime_empty(self):
        with self.assertRaises(ValueError):
            parse_datetime("")
        with self.assertRaises(ValueError):
            parse_datetime(None)

    def test_normalize_domain_various_fields(self):
        raw = {
            "domain_name": "MyDomain.dpdns.org",
            "expires_at": "2026-11-01",
            "status": "active",
            "is_renewable": "yes",
        }
        rec = normalize_domain(raw)
        self.assertEqual(rec.name, "mydomain.dpdns.org")
        self.assertEqual(rec.status, "active")
        self.assertTrue(rec.can_renew)

    def test_normalize_domain_missing_fields(self):
        with self.assertRaises(RuntimeError):
            normalize_domain({"status": "active"})
        with self.assertRaises(RuntimeError):
            normalize_domain({"domain": "example.com"})

    def test_parse_domain_variable_deduplication(self):
        old_env = os.environ.get("DIGITALPLAT_DOMAINS")
        try:
            os.environ["DIGITALPLAT_DOMAINS"] = "a.dpdns.org, b.dpdns.org; a.dpdns.org\n c.dpdns.org "
            domains = parse_domain_variable()
            self.assertEqual(domains, ["a.dpdns.org", "b.dpdns.org", "c.dpdns.org"])
        finally:
            if old_env is not None:
                os.environ["DIGITALPLAT_DOMAINS"] = old_env
            else:
                os.environ.pop("DIGITALPLAT_DOMAINS", None)

    def test_parse_domain_variable_empty(self):
        old_env = os.environ.get("DIGITALPLAT_DOMAINS")
        try:
            os.environ.pop("DIGITALPLAT_DOMAINS", None)
            domains = parse_domain_variable()
            self.assertEqual(domains, [])
        finally:
            if old_env is not None:
                os.environ["DIGITALPLAT_DOMAINS"] = old_env

    def test_load_state_resilience(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "state.json"
            self.assertEqual(load_state(p), {"domains": {}})

            p.write_text("", encoding="utf-8")
            self.assertEqual(load_state(p), {"domains": {}})

            p.write_text("invalid json", encoding="utf-8")
            self.assertEqual(load_state(p), {"domains": {}})

            p.write_text('{"domains": {"a.dpdns.org": {"status": "active"}}}', encoding="utf-8")
            self.assertEqual(load_state(p)["domains"]["a.dpdns.org"]["status"], "active")

    def test_unwrap_response(self):
        # Normal data unwrap
        self.assertEqual(unwrap_response({"success": True, "data": {"domains": []}}), {"domains": []})
        # Error response
        with self.assertRaises(RuntimeError):
            unwrap_response({"success": False, "error": "Token expired"})
        with self.assertRaises(RuntimeError):
            unwrap_response({"code": 401, "message": "Unauthorized"})
        # List response
        self.assertEqual(unwrap_response([{"name": "test"}]), [{"name": "test"}])

    def test_renew_domain_idempotency_key(self):
        from unittest.mock import patch
        from scripts.digitalplat_auto_renew import DigitalPlatClient

        client = DigitalPlatClient("token123", "https://api.example.com")
        with patch.object(client, "_request", return_value={"success": True}) as mock_req:
            res = client.renew_domain("test.dpdns.org", "free", 1)
            mock_req.assert_called_once()
            _, kwargs = mock_req.call_args
            self.assertIn("extra_headers", kwargs)
            self.assertIn("Idempotency-Key", kwargs["extra_headers"])
            self.assertTrue(len(kwargs["extra_headers"]["Idempotency-Key"]) > 10)


if __name__ == "__main__":
    unittest.main()

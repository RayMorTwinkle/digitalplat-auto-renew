import os
import tempfile
import unittest
from pathlib import Path

from scripts.digitalplat_auto_renew import (
    DomainRecord,
    parse_datetime,
    parse_domain_variable,
    should_renew,
    write_step_summary,
    RunResult,
    normalize_domain,
    redact_domain,
    update_state_for_record,
)


class TestMergedImprovements(unittest.TestCase):
    def _record(self, days_ahead: int, lifecycle: str = "yearly", status: str = "ok") -> DomainRecord:
        raw = {
            "domain": "example.dpdns.org",
            "expires_at": f"2999-01-01",
            "lifecycle_type": lifecycle,
            "status": status,
        }
        record = normalize_domain(raw)
        record.expiry_date = record.expiry_date.replace(
            year=2999, month=1, day=1
        )
        return record

    def test_permanent_domain_never_renews(self):
        record = normalize_domain(
            {"domain": "a.dpdns.org", "expires_at": "20271120", "lifecycle_type": "permanent"}
        )
        self.assertTrue(record.is_permanent)
        self.assertFalse(should_renew(record, 100000))

    def test_expired_status_is_not_renewed(self):
        record = normalize_domain(
            {"domain": "a.dpdns.org", "expires_at": "20271120", "status": "deleted"}
        )
        self.assertFalse(should_renew(record, 100000))

    def test_yearly_domain_inside_window_renews(self):
        record = normalize_domain({"domain": "a.dpdns.org", "expires_at": "20271120"})
        self.assertFalse(record.is_permanent)
        self.assertTrue(should_renew(record, 100000))

    def test_domain_variable_mixed_separators(self):
        os.environ["DIGITALPLAT_DOMAINS"] = "a.dpdns.org, b.dpdns.org;c.dpdns.org\nA.dpdns.org"
        try:
            self.assertEqual(
                parse_domain_variable(),
                ["a.dpdns.org", "b.dpdns.org", "c.dpdns.org"],
            )
        finally:
            del os.environ["DIGITALPLAT_DOMAINS"]

    def test_step_summary_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            os.environ["GITHUB_STEP_SUMMARY"] = str(path)
            try:
                result = RunResult(renewed=1)
                result.rows.append(
                    {
                        "domain": "a.dpdns.org",
                        "expiry_date": "2027-11-20",
                        "days_remaining": 389,
                        "lifecycle_type": "permanent",
                        "action": "permanent / 无需续期",
                    }
                )
                write_step_summary(result, 120, False)
            finally:
                del os.environ["GITHUB_STEP_SUMMARY"]
            content = path.read_text(encoding="utf-8")
            self.assertIn("DigitalPlat 续期检查", content)
            self.assertIn("a***.dpdns.org", content)
            self.assertNotIn("| `a.dpdns.org` |", content)
            self.assertIn("本次续期数量：**1**", content)

    def test_redact_domain_hides_full_name(self):
        self.assertEqual(redact_domain("mydomain.dpdns.org"), "m******n.dpdns.org")
        self.assertEqual(redact_domain("ab.dpdns.org"), "a***.dpdns.org")
        self.assertEqual(redact_domain("a.dpdns.org"), "a***.dpdns.org")
        self.assertEqual(redact_domain(""), "***")
        self.assertNotIn("mydomain", redact_domain("mydomain.dpdns.org"))

    def test_state_key_is_redacted(self):
        import json
        import tempfile
        from pathlib import Path as P

        from scripts.digitalplat_auto_renew import save_state, load_state

        record = normalize_domain({"domain": "mydomain.dpdns.org", "expires_at": "20271120"})
        state = {"domains": {}}
        self.assertTrue(update_state_for_record(state, record, "skipped", 120))
        self.assertIn("m******n.dpdns.org", state["domains"])
        self.assertNotIn("mydomain", json.dumps(state))
        with tempfile.TemporaryDirectory() as tmp:
            path = P(tmp) / "state.json"
            save_state(path, state)
            self.assertNotIn("mydomain", path.read_text(encoding="utf-8"))
            self.assertIn("m******n.dpdns.org", json.dumps(load_state(path)))

    def test_parse_datetime_yyyymmdd_from_dpdns(self):
        self.assertEqual(parse_datetime("20271120").strftime("%Y-%m-%d"), "2027-11-20")


if __name__ == "__main__":
    unittest.main()

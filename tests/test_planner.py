import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, time
from pathlib import Path

import planner as pl

ROOT = Path(__file__).resolve().parents[1]
CFG = pl.load_config(ROOT / "examples" / "rules.json")
DATA = json.loads((ROOT / "examples" / "sample_data.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 11)


def plan(purchases=None, customers=None, consents=None, existing=None, today=TODAY, cfg=CFG):
    return pl.plan_reminders(cfg, purchases if purchases is not None else DATA["purchases"],
                             customers if customers is not None else DATA["customers"],
                             consents if consents is not None else DATA["consents"],
                             existing if existing is not None else DATA["existing"], today)


def by_pid(actions):
    return {a.purchase_id: a for a in actions}


class SampleTest(unittest.TestCase):
    def test_sample_plan(self):
        got = {k: (a.action, a.rule_key, a.channel, a.send_at) for k, a in by_pid(plan()).items()}
        self.assertEqual(got, {
            "P1": ("create", "insoles", "email", "2026-12-04T10:00:00+01:00"),
            "P2": ("create", "insoles_sport", "email", "2026-11-19T10:00:00+01:00"),
            "P3": ("create", "compression", "sms", "2026-11-30T09:30:00+01:00"),   # Sat 28.11 -> Mon
            "P4": ("skip", "insoles", None, None),                                 # consent expired
            "P5": ("skip", "service_check", None, None),                           # whatsapp not consented
            "P6": ("skip", "insoles", "letter", None),                             # stale
            "P7": ("skip", None, None, None),                                      # no rule
            "P8": ("keep", "insoles", "email", "2026-10-16T10:00:00+02:00"),       # already sent
            "P9": ("update", "insoles", "sms", "2026-11-16T09:30:00+01:00"),
        })


class IdempotencyTest(unittest.TestCase):
    def test_second_run_on_its_own_output_changes_nothing(self):
        first = plan()
        stored = [{"purchase_id": a.purchase_id, "rule_key": a.rule_key, "status": "pending",
                   "channel": a.channel, "send_at": a.send_at} for a in first if a.action in ("create", "update")]
        stored += [e for e in DATA["existing"] if e["status"] == "sent"]
        second = by_pid(plan(existing=stored))
        for pid in ("P1", "P2", "P3", "P9"):
            self.assertEqual(second[pid].action, "keep", pid)

    def test_sent_cancelled_failed_are_never_replanned(self):
        for status in ("sent", "delivered", "cancelled", "failed"):
            with self.subTest(status=status):
                existing = [{"purchase_id": "P1", "rule_key": "insoles", "status": status,
                             "channel": "email", "send_at": "2026-12-04T10:00:00+01:00"}]
                self.assertEqual(by_pid(plan(existing=existing))["P1"].action, "keep")


class DueDateTest(unittest.TestCase):
    def test_counted_from_purchase_date_not_import_date(self):
        a = plan(purchases=[{"purchase_id": "X", "customer_id": "C1", "product": "Einlagen",
                             "purchase_date": "2026-11-01", "imported_at": "2026-11-10"}])[0]
        self.assertTrue(a.send_at.startswith("2026-12-16"))

    def test_slightly_overdue_is_sent_today_or_next_allowed_day(self):
        # due 2026-11-06 (Fri), 5 days overdue; today is Wed 2026-11-11
        a = plan(purchases=[{"purchase_id": "X", "customer_id": "C1", "product": "Einlagen", "purchase_date": "2026-09-22"}])[0]
        self.assertEqual(a.send_at, "2026-11-11T10:00:00+01:00")

    def test_weekend_and_dst(self):
        # due Sat 2027-03-27 -> Mon 2027-03-29, after the switch to summer time (+02:00)
        a = plan(purchases=[{"purchase_id": "X", "customer_id": "C1", "product": "Einlagen", "purchase_date": "2027-02-10"}])[0]
        self.assertEqual(a.send_at, "2027-03-29T10:00:00+02:00")

    def test_stale_existing_reminder_is_cancelled(self):
        existing = [{"purchase_id": "P6", "rule_key": "insoles", "status": "pending", "channel": "letter", "send_at": "x"}]
        self.assertEqual(by_pid(plan(existing=existing))["P6"].action, "cancel")


class ConsentTest(unittest.TestCase):
    def test_withdrawn_or_missing_consent(self):
        consents = [{"customer_id": "C1", "scope": "reminders", "status": "withdrawn"},
                    {"customer_id": "C1", "scope": "newsletter", "status": "granted"}]
        a = by_pid(plan(consents=consents))["P1"]
        self.assertEqual((a.action, a.reason), ("skip", "no consent for reminders"))

    def test_withdrawal_cancels_a_pending_reminder(self):
        existing = [{"purchase_id": "P1", "rule_key": "insoles", "status": "pending", "channel": "email", "send_at": "x"}]
        a = by_pid(plan(consents=[], existing=existing))["P1"]
        self.assertEqual(a.action, "cancel")

    def test_explicit_empty_channel_list_covers_no_channel(self):
        consents = [{"customer_id": "C1", "scope": "reminders", "status": "granted", "channels": []}]
        a = by_pid(plan(consents=consents))["P1"]
        self.assertEqual(a.action, "skip")
        self.assertIn("preferred channel", a.reason)

    def test_consent_must_still_be_valid_on_the_send_date(self):
        # P1 is due 2026-12-04; a consent ending 2026-11-12 is valid today but not then
        short = [{"customer_id": "C1", "scope": "reminders", "status": "granted", "expires_on": "2026-11-12"}]
        a = by_pid(plan(consents=short))["P1"]
        self.assertEqual(a.action, "skip")
        self.assertIn("before the send date 2026-12-04", a.reason)
        long = [{"customer_id": "C1", "scope": "reminders", "status": "granted", "expires_on": "2026-12-04"}]
        self.assertEqual(by_pid(plan(consents=long))["P1"].action, "create")
        existing = [{"purchase_id": "P1", "rule_key": "insoles", "status": "pending", "channel": "email", "send_at": "x"}]
        self.assertEqual(by_pid(plan(consents=short, existing=existing))["P1"].action, "cancel")

    def test_consent_without_channel_list_covers_all_channels(self):
        a = by_pid(plan(consents=[{"customer_id": "C4", "scope": "reminders", "status": "granted"}]))["P5"]
        self.assertEqual((a.action, a.channel), ("create", "whatsapp"))


class RuleTest(unittest.TestCase):
    def test_longest_keyword_wins_and_case_is_ignored(self):
        self.assertEqual(pl.match_rule("SPORTEINLAGEN Laufen", CFG).key, "insoles_sport")
        self.assertEqual(pl.match_rule("Einlagen", CFG).key, "insoles")
        self.assertIsNone(pl.match_rule("Gutschein", CFG))

    def test_quiet_hours(self):
        self.assertTrue(pl.in_quiet_hours(time(21, 0), CFG))
        self.assertTrue(pl.in_quiet_hours(time(7, 59), CFG))
        self.assertFalse(pl.in_quiet_hours(time(8, 0), CFG))

    def test_invalid_configs(self):
        base = json.loads((ROOT / "examples" / "rules.json").read_text(encoding="utf-8"))
        bad = [dict(send_times={"email": "21:00"}), dict(send_times={"fax": "10:00"}),
               dict(no_send_weekdays=[0, 1, 2, 3, 4, 5, 6]), dict(timezone="Mars/Olympus"),
               dict(rules=base["rules"] + [{"key": "insoles", "keywords": ["x"], "days": 5}]),
               dict(rules=[{"key": "a", "keywords": ["x"], "days": 0}])]
        with tempfile.TemporaryDirectory() as d:
            for change in bad:
                with self.subTest(change=change):
                    p = Path(d) / "c.json"
                    p.write_text(json.dumps({**base, **change}), encoding="utf-8")
                    with self.assertRaises(Exception):
                        pl.load_config(p)


class CliTest(unittest.TestCase):
    def test_cli_runs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pl.main(["--config", str(ROOT / "examples" / "rules.json"),
                                      "--data", str(ROOT / "examples" / "sample_data.json"),
                                      "--today", "2026-11-11", "--json"]), 0)
        self.assertEqual(len(json.loads(out.getvalue())), 9)


if __name__ == "__main__":
    unittest.main()

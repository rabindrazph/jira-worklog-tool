#!/usr/bin/env python3
"""Unit tests for the pure logic in jira_worklog.py (no network/subprocess calls)."""
import unittest
from datetime import datetime, timedelta, timezone

import jira_worklog as m


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TicketFromBranch(unittest.TestCase):
    def test_extracts_ticket(self):
        self.assertEqual(m.ticket_from_branch("feature/ZC-123-do-thing"), "ZC-123")

    def test_no_branch(self):
        self.assertIsNone(m.ticket_from_branch(None))

    def test_no_match(self):
        self.assertIsNone(m.ticket_from_branch("main"))

    def test_known_keys_accepts_valid_project(self):
        self.assertEqual(m.ticket_from_branch("ZC-123-fix", known_keys={"ZC"}), "ZC-123")

    def test_known_keys_rejects_unknown_project(self):
        self.assertIsNone(m.ticket_from_branch("release-FOO-123-hotfix", known_keys={"ZC"}))


class LocalDate(unittest.TestCase):
    def test_matches_utc_when_no_offset_needed(self):
        # Just checks it round-trips through astimezone without error and
        # returns an ISO date string.
        d = m.local_date(ts("2026-11-24T12:00:00"))
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")


class ProcessSession(unittest.TestCase):
    def mkevent(self, t, branch, text=None):
        return {"ts": ts(t), "branch": branch, "role": "user", "text": text}

    def test_first_session_uses_min_floor(self):
        events = [self.mkevent("2026-11-24T09:00:00", "ZC-1-fix")]
        intervals, new_logged_until, stats = m.process_session(
            events, None, timedelta(minutes=10), timedelta(minutes=5),
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["ticket"], "ZC-1")
        self.assertEqual(intervals[0]["minutes"], 5.0)
        self.assertEqual(new_logged_until, ts("2026-11-24T09:00:00"))

    def test_gap_capped(self):
        events = [
            self.mkevent("2026-11-24T09:00:00", "ZC-1-fix"),
            self.mkevent("2026-11-24T09:30:00", "ZC-1-fix"),
        ]
        intervals, _, stats = m.process_session(
            events, None, timedelta(minutes=10), timedelta(minutes=5),
        )
        # first interval is the min-floor seed, second is the capped 30m gap -> 10m
        self.assertEqual([round(i["minutes"], 1) for i in intervals], [5.0, 10.0])

    def test_no_ticket_goes_to_unmatched(self):
        events = [
            self.mkevent("2026-11-24T09:00:00", "main"),
            self.mkevent("2026-11-24T09:05:00", "main"),
        ]
        intervals, _, stats = m.process_session(
            events, None, timedelta(minutes=10), timedelta(minutes=5),
        )
        self.assertEqual(intervals, [])
        self.assertGreater(stats["unmatched_minutes"], 0)

    def test_resume_does_not_rebill_prior_events(self):
        events = [
            self.mkevent("2026-11-24T09:00:00", "ZC-1-fix"),
            self.mkevent("2026-11-24T09:05:00", "ZC-1-fix"),
            self.mkevent("2026-11-24T09:10:00", "ZC-1-fix"),
        ]
        logged_until = ts("2026-11-24T09:05:00")
        intervals, new_logged_until, _ = m.process_session(
            events, logged_until, timedelta(minutes=10), timedelta(minutes=5),
        )
        # only the gap from 09:05 -> 09:10 should bill, not the earlier ones
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["minutes"], 5.0)
        self.assertEqual(new_logged_until, ts("2026-11-24T09:10:00"))

    def test_unknown_project_key_treated_as_unmatched(self):
        events = [self.mkevent("2026-11-24T09:00:00", "FOO-1-fix")]
        intervals, _, stats = m.process_session(
            events, None, timedelta(minutes=10), timedelta(minutes=5), known_keys={"ZC"},
        )
        self.assertEqual(intervals, [])
        self.assertGreater(stats["unmatched_minutes"], 0)


class Aggregate(unittest.TestCase):
    def test_sums_minutes_across_sessions_same_day(self):
        intervals = [
            {"ticket": "ZC-1", "day": "2026-11-24", "minutes": 10.0, "prompts": [], "ts": ts("2026-11-24T09:00:00"), "session_key": "s1"},
            {"ticket": "ZC-1", "day": "2026-11-24", "minutes": 20.0, "prompts": [], "ts": ts("2026-11-24T10:00:00"), "session_key": "s2"},
        ]
        agg = m.aggregate(intervals)
        entry = agg[("ZC-1", "2026-11-24")]
        self.assertEqual(entry["minutes"], 30.0)
        self.assertEqual(entry["sessions"], {"s1", "s2"})

    def test_separate_days_stay_separate(self):
        intervals = [
            {"ticket": "ZC-1", "day": "2026-11-24", "minutes": 10.0, "prompts": [], "ts": ts("2026-11-24T09:00:00")},
            {"ticket": "ZC-1", "day": "2026-11-25", "minutes": 20.0, "prompts": [], "ts": ts("2026-11-25T09:00:00")},
        ]
        agg = m.aggregate(intervals)
        self.assertEqual(len(agg), 2)


class ApplyDailyCap(unittest.TestCase):
    def mkentry(self, day, minutes):
        return {"day": day, "minutes": minutes, "ticket": "ZC-1", "sessions": set(), "prompts": [], "ts": None}

    def test_no_scaling_under_cap(self):
        entries = [self.mkentry("2026-11-24", 60.0)]
        m.apply_daily_cap(entries, cap_hours=7)
        self.assertEqual(entries[0]["minutes"], 60.0)

    def test_scales_down_over_cap(self):
        entries = [self.mkentry("2026-11-24", 500.0)]
        m.apply_daily_cap(entries, cap_hours=7)
        self.assertEqual(entries[0]["minutes"], 420.0)

    def test_accounts_for_already_submitted(self):
        entries = [self.mkentry("2026-11-24", 120.0)]
        m.apply_daily_cap(entries, cap_hours=7, already_submitted={"2026-11-24": 400.0})
        self.assertAlmostEqual(entries[0]["minutes"], 20.0)

    def test_drops_to_zero_when_already_over_cap(self):
        entries = [self.mkentry("2026-11-24", 120.0)]
        m.apply_daily_cap(entries, cap_hours=7, already_submitted={"2026-11-24": 500.0})
        self.assertEqual(entries[0]["minutes"], 0.0)


class RoundMinutes(unittest.TestCase):
    def test_floors_to_minimum(self):
        self.assertEqual(m.round_minutes(0.2), 5)

    def test_rounds_to_nearest_five(self):
        self.assertEqual(m.round_minutes(12.0), 10)
        self.assertEqual(m.round_minutes(13.0), 15)


class RedactSensitive(unittest.TestCase):
    def test_redacts_token_assignment(self):
        self.assertIn("[REDACTED]", m.redact_sensitive("api_key: sk-abc123xyz"))

    def test_redacts_email(self):
        self.assertIn("[REDACTED_EMAIL]", m.redact_sensitive("reach me at foo@bar.com"))

    def test_redacts_aws_key(self):
        self.assertIn("[REDACTED_AWS_KEY]", m.redact_sensitive("key AKIAABCDEFGHIJKLMNOP"))

    def test_leaves_normal_text_alone(self):
        text = "Refactored the login handler to use app_user table."
        self.assertEqual(m.redact_sensitive(text), text)


if __name__ == "__main__":
    unittest.main()

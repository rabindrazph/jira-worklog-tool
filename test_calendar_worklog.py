#!/usr/bin/env python3
"""Unit tests for the pure logic in jira_calendar_worklog.py (no network/subprocess calls)."""
import re
import unittest
from datetime import datetime, timezone

import jira_worklog as jw
import jira_calendar_worklog as m


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class ExtractSprintNumber(unittest.TestCase):
    def test_matches_hash_form(self):
        self.assertEqual(m.extract_sprint_number("Sprint #50", m.DEFAULT_SPRINT_RE), "50")

    def test_matches_no_hash(self):
        self.assertEqual(m.extract_sprint_number("Sprint 50", m.DEFAULT_SPRINT_RE), "50")

    def test_matches_within_longer_title(self):
        self.assertEqual(
            m.extract_sprint_number("Meeting/Discussion Sprint #50", m.DEFAULT_SPRINT_RE), "50",
        )

    def test_no_digit_no_match(self):
        self.assertIsNone(m.extract_sprint_number("Sprint Planning", m.DEFAULT_SPRINT_RE))

    def test_unrelated_text_no_match(self):
        self.assertIsNone(m.extract_sprint_number("1:1 with manager", m.DEFAULT_SPRINT_RE))

    def test_none_title(self):
        self.assertIsNone(m.extract_sprint_number(None, m.DEFAULT_SPRINT_RE))


class EventDurationMinutes(unittest.TestCase):
    def test_timed_event(self):
        event = {
            "start": {"dateTime": "2026-11-24T09:00:00+00:00"},
            "end": {"dateTime": "2026-11-24T09:30:00+00:00"},
        }
        self.assertEqual(m.event_duration_minutes(event), 30.0)

    def test_all_day_event_is_none(self):
        event = {"start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}
        self.assertIsNone(m.event_duration_minutes(event))

    def test_malformed_event_is_none(self):
        self.assertIsNone(m.event_duration_minutes({}))
        self.assertIsNone(m.event_duration_minutes({"start": {}, "end": {}}))

    def test_zero_or_negative_duration_is_none(self):
        event = {
            "start": {"dateTime": "2026-11-24T09:00:00+00:00"},
            "end": {"dateTime": "2026-11-24T09:00:00+00:00"},
        }
        self.assertIsNone(m.event_duration_minutes(event))


class ResolveTicketForEventTier1(unittest.TestCase):
    def mkevent(self, summary=None, description=None, location=None):
        return {"summary": summary, "description": description, "location": location}

    def test_key_in_summary(self):
        event = self.mkevent(summary="Quick sync on ZC-3721")
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, {"jira_project_keys": ["ZC"]}, {}, lambda jql: [], m.DEFAULT_SPRINT_RE,
        )
        self.assertEqual((ticket, reason), ("ZC-3721", "direct"))

    def test_link_in_description(self):
        event = self.mkevent(
            summary="Meeting/Discussion Sprint #50",
            description="https://zakipointhealth.atlassian.net/browse/ZC-3721",
        )
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, {"jira_project_keys": ["ZC"]}, {}, lambda jql: [], m.DEFAULT_SPRINT_RE,
        )
        self.assertEqual((ticket, reason), ("ZC-3721", "direct"))

    def test_key_in_location(self):
        event = self.mkevent(location="Jira ZC-99 war room")
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, {"jira_project_keys": ["ZC"]}, {}, lambda jql: [], m.DEFAULT_SPRINT_RE,
        )
        self.assertEqual((ticket, reason), ("ZC-99", "direct"))

    def test_unknown_project_prefix_is_unmatched(self):
        event = self.mkevent(summary="Sync on FOO-1")
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, {"jira_project_keys": ["ZC"]}, {}, lambda jql: [], m.DEFAULT_SPRINT_RE,
        )
        self.assertEqual((ticket, reason), (None, "unmatched"))


class ResolveTicketForEventTier2(unittest.TestCase):
    def mkcfg(self):
        return {"jira_project_keys": ["ZC"], "sprint_reporter_account_id": "acc123"}

    def test_exact_one_hit_resolves(self):
        event = {"summary": "Meeting/Discussion Sprint #50"}
        search_fn = lambda jql: [{"key": "ZC-3800"}]
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE,
        )
        self.assertEqual((ticket, reason), ("ZC-3800", "sprint:50"))

    def test_zero_hits_is_ambiguous_unmatched(self):
        event = {"summary": "Meeting/Discussion Sprint #50"}
        search_fn = lambda jql: []
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE,
        )
        self.assertIsNone(ticket)
        self.assertEqual(reason, "sprint:50:ambiguous")

    def test_multiple_hits_is_ambiguous_unmatched(self):
        event = {"summary": "Meeting/Discussion Sprint #50"}
        search_fn = lambda jql: [{"key": "ZC-3800"}, {"key": "CM-10"}]
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE,
        )
        self.assertIsNone(ticket)
        self.assertEqual(reason, "sprint:50:ambiguous")

    def test_no_reporter_configured_skips_tier2(self):
        event = {"summary": "Meeting/Discussion Sprint #50"}
        cfg = {"jira_project_keys": ["ZC"]}  # no sprint_reporter_account_id
        search_fn = lambda jql: (_ for _ in ()).throw(AssertionError("search should not be called"))
        ticket, reason = m.resolve_ticket_for_event(event, {"ZC"}, cfg, {}, search_fn, m.DEFAULT_SPRINT_RE)
        self.assertEqual((ticket, reason), (None, "unmatched"))


class SprintTicketCache(unittest.TestCase):
    def test_second_lookup_uses_cache_not_search(self):
        calls = []

        def search_fn(jql):
            calls.append(jql)
            return [{"key": "ZC-3800"}]

        cache = {}
        first = m.resolve_sprint_ticket("50", ["ZC"], "acc123", cache, search_fn)
        second = m.resolve_sprint_ticket("50", ["ZC"], "acc123", cache, search_fn)
        self.assertEqual(first, "ZC-3800")
        self.assertEqual(second, "ZC-3800")
        self.assertEqual(len(calls), 1)

    def test_ambiguous_result_is_not_cached(self):
        calls = []

        def search_fn(jql):
            calls.append(jql)
            return []

        cache = {}
        m.resolve_sprint_ticket("50", ["ZC"], "acc123", cache, search_fn)
        m.resolve_sprint_ticket("50", ["ZC"], "acc123", cache, search_fn)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("50", cache)


class EventToEntryAndDedup(unittest.TestCase):
    def test_entry_shape_feeds_cap_without_merging(self):
        event = {"id": "evt1", "summary": "Standup", "description": None}
        start_dt = ts("2026-11-24T09:00:00")
        entry = m.event_to_entry(event, "ZC-1", 30.0, start_dt)
        entries = [entry]
        jw.apply_daily_cap(entries, cap_hours=7)
        self.assertEqual(entries[0]["ticket"], "ZC-1")
        self.assertEqual(entries[0]["minutes"], 30.0)
        self.assertEqual(entries[0]["sessions"], {"evt1"})
        self.assertEqual(entries[0]["started"], start_dt)

    def test_two_same_day_same_ticket_events_stay_separate_entries(self):
        start_a = ts("2026-11-24T09:00:00")
        start_b = ts("2026-11-24T14:00:00")
        entry_a = m.event_to_entry({"id": "evt1", "summary": "DSM"}, "ZC-1", 15.0, start_a)
        entry_b = m.event_to_entry({"id": "evt2", "summary": "Sprint #50"}, "ZC-1", 60.0, start_b)
        entries = [entry_a, entry_b]
        jw.apply_daily_cap(entries, cap_hours=7)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["prompts"], ["DSM"])
        self.assertEqual(entries[1]["prompts"], ["Sprint #50"])

    def test_is_new_event(self):
        state = {"logged_events": {"evt1": {}}}
        self.assertFalse(m.is_new_event("evt1", state))
        self.assertTrue(m.is_new_event("evt2", state))

    def test_is_new_event_empty_state(self):
        self.assertTrue(m.is_new_event("evt1", {}))


class SelfResponseStatus(unittest.TestCase):
    def test_finds_self_attendee(self):
        event = {"attendees": [
            {"email": "other@x.com", "responseStatus": "accepted"},
            {"email": "me@x.com", "self": True, "responseStatus": "declined"},
        ]}
        self.assertEqual(m.self_response_status(event), "declined")

    def test_no_attendees_is_none(self):
        self.assertIsNone(m.self_response_status({}))
        self.assertIsNone(m.self_response_status({"attendees": []}))

    def test_no_self_attendee_is_none(self):
        event = {"attendees": [{"email": "other@x.com", "responseStatus": "accepted"}]}
        self.assertIsNone(m.self_response_status(event))


class IsDeclinedBySelf(unittest.TestCase):
    def test_declined_is_skipped(self):
        event = {"attendees": [{"self": True, "responseStatus": "declined"}]}
        self.assertTrue(m.is_declined_by_self(event))

    def test_accepted_bills(self):
        event = {"attendees": [{"self": True, "responseStatus": "accepted"}]}
        self.assertFalse(m.is_declined_by_self(event))

    def test_tentative_bills(self):
        event = {"attendees": [{"self": True, "responseStatus": "tentative"}]}
        self.assertFalse(m.is_declined_by_self(event))

    def test_needs_action_bills(self):
        event = {"attendees": [{"self": True, "responseStatus": "needsAction"}]}
        self.assertFalse(m.is_declined_by_self(event))

    def test_no_rsvp_data_bills(self):
        self.assertFalse(m.is_declined_by_self({}))


class CalendarWindow(unittest.TestCase):
    def test_window_spans_days_back(self):
        time_min, time_max = m.calendar_window(3)
        self.assertLess(time_min, time_max)
        self.assertAlmostEqual((time_max - time_min).total_seconds(), 3 * 86400, delta=5)


class ResolveActiveSprintNumber(unittest.TestCase):
    def test_exact_one_active_sprint_extracts_number(self):
        fetch_fn = lambda: [{"name": "Sprint 50"}]
        self.assertEqual(m.resolve_active_sprint_number(127, m.DEFAULT_SPRINT_RE, fetch_fn), "50")

    def test_zero_active_sprints_is_none(self):
        fetch_fn = lambda: []
        self.assertIsNone(m.resolve_active_sprint_number(127, m.DEFAULT_SPRINT_RE, fetch_fn))

    def test_multiple_active_sprints_is_ambiguous_none(self):
        fetch_fn = lambda: [{"name": "Sprint 50"}, {"name": "Sprint 51"}]
        self.assertIsNone(m.resolve_active_sprint_number(127, m.DEFAULT_SPRINT_RE, fetch_fn))

    def test_fetch_failure_is_none(self):
        fetch_fn = lambda: None
        self.assertIsNone(m.resolve_active_sprint_number(127, m.DEFAULT_SPRINT_RE, fetch_fn))


class ResolveTicketForEventTier3(unittest.TestCase):
    def mkcfg(self):
        return {"jira_project_keys": ["ZC"], "sprint_reporter_account_id": "acc123"}

    def test_no_title_sprint_number_falls_back_to_active_sprint(self):
        event = {"summary": "Online : zConnect DSM"}
        search_fn = lambda jql: [{"key": "ZC-3800"}]
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE, active_sprint_number="50",
        )
        self.assertEqual((ticket, reason), ("ZC-3800", "current_sprint:50"))

    def test_title_sprint_number_takes_precedence_over_active_sprint(self):
        event = {"summary": "Meeting/Discussion Sprint #49"}
        search_fn = lambda jql: [{"key": "ZC-OLD"}]
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE, active_sprint_number="50",
        )
        self.assertEqual((ticket, reason), ("ZC-OLD", "sprint:49"))

    def test_no_active_sprint_and_no_title_number_stays_unmatched(self):
        event = {"summary": "Customer Segments, ICP & Leveraging AI Across ZPH"}
        search_fn = lambda jql: (_ for _ in ()).throw(AssertionError("search should not be called"))
        ticket, reason = m.resolve_ticket_for_event(
            event, {"ZC"}, self.mkcfg(), {}, search_fn, m.DEFAULT_SPRINT_RE, active_sprint_number=None,
        )
        self.assertEqual((ticket, reason), (None, "unmatched"))


def mkevent(event_id, response_status="accepted"):
    return {"id": event_id, "attendees": [{"self": True, "responseStatus": response_status}]}


class ResolveOverlaps(unittest.TestCase):
    def test_no_overlap_bills_full_duration_both(self):
        e1, e2 = mkevent("a", "accepted"), mkevent("b", "accepted")
        result = m.resolve_overlaps([
            (e1, ts("2026-11-24T09:00:00"), ts("2026-11-24T10:00:00")),
            (e2, ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00")),
        ])
        by_id = {e["id"]: (start, minutes) for e, start, minutes in result}
        self.assertEqual(by_id["a"], (ts("2026-11-24T09:00:00"), 60.0))
        self.assertEqual(by_id["b"], (ts("2026-11-24T10:00:00"), 60.0))

    def test_same_tier_partial_overlap_earlier_start_wins(self):
        earlier, later = mkevent("earlier", "tentative"), mkevent("later", "tentative")
        result = m.resolve_overlaps([
            (later, ts("2026-11-24T10:30:00"), ts("2026-11-24T11:30:00")),
            (earlier, ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00")),
        ])
        by_id = {e["id"]: (start, minutes) for e, start, minutes in result}
        self.assertEqual(by_id["earlier"], (ts("2026-11-24T10:00:00"), 60.0))
        self.assertEqual(by_id["later"], (ts("2026-11-24T11:00:00"), 30.0))

    def test_accepted_overrides_tentative_started_earlier(self):
        tentative = mkevent("tentative_ev", "tentative")
        accepted = mkevent("accepted_ev", "accepted")
        result = m.resolve_overlaps([
            (tentative, ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00")),
            (accepted, ts("2026-11-24T10:30:00"), ts("2026-11-24T11:30:00")),
        ])
        by_id = {e["id"]: (start, minutes) for e, start, minutes in result}
        self.assertEqual(by_id["accepted_ev"], (ts("2026-11-24T10:30:00"), 60.0))
        self.assertEqual(by_id["tentative_ev"], (ts("2026-11-24T10:00:00"), 30.0))

    def test_lower_priority_fully_covered_bills_zero(self):
        higher = mkevent("higher", "accepted")
        lower = mkevent("lower", "tentative")
        result = m.resolve_overlaps([
            (lower, ts("2026-11-24T10:30:00"), ts("2026-11-24T11:00:00")),
            (higher, ts("2026-11-24T10:00:00"), ts("2026-11-24T12:00:00")),
        ])
        by_id = {e["id"]: minutes for e, _, minutes in result}
        self.assertEqual(by_id["higher"], 120.0)
        self.assertEqual(by_id["lower"], 0.0)

    def test_higher_priority_nested_inside_keeps_larger_remainder(self):
        """A short accepted meeting nested in the middle of a long tentative
        block splits the tentative block into a before/after remainder --
        only the larger piece should bill, never both as one entry."""
        tentative = mkevent("tentative_block", "tentative")
        accepted = mkevent("accepted_nested", "accepted")
        result = m.resolve_overlaps([
            (tentative, ts("2026-11-24T09:00:00"), ts("2026-11-24T12:00:00")),
            (accepted, ts("2026-11-24T10:00:00"), ts("2026-11-24T10:10:00")),
        ])
        by_id = {e["id"]: (start, minutes) for e, start, minutes in result}
        self.assertEqual(by_id["accepted_nested"], (ts("2026-11-24T10:00:00"), 10.0))
        self.assertEqual(by_id["tentative_block"], (ts("2026-11-24T10:10:00"), 110.0))

    def test_declined_never_passed_in_does_not_affect_others(self):
        """Caller is expected to filter out declined events before calling
        resolve_overlaps -- this just confirms a single surviving event is
        unaffected when nothing else overlaps it."""
        solo = mkevent("solo", "tentative")
        result = m.resolve_overlaps([
            (solo, ts("2026-11-24T09:00:00"), ts("2026-11-24T09:30:00")),
        ])
        self.assertEqual(result, [(solo, ts("2026-11-24T09:00:00"), 30.0)])



class ResolveOverlapsAlreadyClaimed(unittest.TestCase):
    """Time billed by events logged in earlier runs blocks new events."""

    def test_new_event_clipped_by_previously_billed_time(self):
        new = mkevent("new", "accepted")
        result = m.resolve_overlaps(
            [(new, ts("2026-11-24T10:30:00"), ts("2026-11-24T11:30:00"))],
            already_claimed=[(ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00"))],
        )
        self.assertEqual(result, [(new, ts("2026-11-24T11:00:00"), 30.0)])

    def test_new_event_fully_covered_by_previously_billed_time_bills_zero(self):
        new = mkevent("new", "accepted")
        result = m.resolve_overlaps(
            [(new, ts("2026-11-24T10:15:00"), ts("2026-11-24T10:45:00"))],
            already_claimed=[(ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00"))],
        )
        self.assertEqual(result, [(new, ts("2026-11-24T10:15:00"), 0.0)])

    def test_already_claimed_never_returned(self):
        result = m.resolve_overlaps(
            [], already_claimed=[(ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00"))],
        )
        self.assertEqual(result, [])

    def test_no_already_claimed_matches_default(self):
        e = mkevent("e", "tentative")
        items = [(e, ts("2026-11-24T09:00:00"), ts("2026-11-24T09:30:00"))]
        self.assertEqual(m.resolve_overlaps(items, already_claimed=[]), m.resolve_overlaps(items))


class LoggedBilledInterval(unittest.TestCase):
    def event(self):
        return {"id": "x", "start": {"dateTime": "2026-11-24T10:00:00Z"},
                "end": {"dateTime": "2026-11-24T11:00:00Z"}}

    def test_uses_recorded_billed_slice(self):
        record = {"ticket": "ZC-1", "day": "2026-11-24",
                  "billed_start": "2026-11-24T10:30:00+00:00", "billed_minutes": 30}
        self.assertEqual(m.logged_billed_interval(self.event(), record),
                         (ts("2026-11-24T10:30:00"), ts("2026-11-24T11:00:00")))

    def test_zero_billed_blocks_nothing(self):
        record = {"ticket": "ZC-1", "day": "2026-11-24",
                  "billed_start": "2026-11-24T10:00:00+00:00", "billed_minutes": 0}
        self.assertIsNone(m.logged_billed_interval(self.event(), record))

    def test_legacy_record_falls_back_to_scheduled_interval(self):
        record = {"ticket": "ZC-1", "day": "2026-11-24"}
        self.assertEqual(m.logged_billed_interval(self.event(), record),
                         (ts("2026-11-24T10:00:00"), ts("2026-11-24T11:00:00")))

    def test_legacy_record_for_all_day_event_blocks_nothing(self):
        event = {"id": "x", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}
        self.assertIsNone(m.logged_billed_interval(event, {"ticket": "ZC-1", "day": "2026-11-24"}))


if __name__ == "__main__":
    unittest.main()

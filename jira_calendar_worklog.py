#!/usr/bin/env python3
"""Reads the user's Google Calendar, resolves each meeting to a Jira ticket
(a direct ticket key/link in the title/description/location, or — for
recurring "Sprint #N" meetings — a JQL lookup narrowed by project and
reporter), and auto-submits Jira worklogs for meeting time. Sibling script
to jira_worklog.py: same Jira credentials/submission machinery (reused via
import), its own Google OAuth credentials/config/state. Designed to run
unattended from cron. Stdlib only.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import jira_worklog as jw

GOOGLE_CREDENTIALS_FILE = jw.CONFIG_DIR / "google_credentials.json"
CALENDAR_CONFIG_FILE = jw.CONFIG_DIR / "calendar_config.json"
CALENDAR_STATE_FILE = jw.CONFIG_DIR / "calendar_state.json"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_REDIRECT_URI = "http://localhost"

DEFAULT_SPRINT_RE = re.compile(r"Sprint\s*#?\s*(\d+)", re.IGNORECASE)
DEFAULT_DAYS_BACK = 1


def validate_google_credentials(creds: dict) -> None:
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not creds.get(k)]
    if missing:
        jw.log(f"ERROR: google_credentials.json missing required key(s): {', '.join(missing)}. "
               f"Run --setup-google-auth first.")
        sys.exit(1)


def validate_calendar_config(cfg: dict) -> None:
    keys = cfg.get("jira_project_keys")
    if not isinstance(keys, list) or not keys:
        jw.log("ERROR: calendar_config.json 'jira_project_keys' must be a non-empty list")
        sys.exit(1)


def save_calendar_state(cal_state: dict) -> None:
    """Same atomic-write pattern as jw.save_state, targeting the separate
    calendar dedup/cache file instead of jw.STATE_FILE."""
    tmp_path = CALENDAR_STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cal_state, indent=2))
    os.replace(tmp_path, CALENDAR_STATE_FILE)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute worklogs and print what would be submitted, without calling Jira or saving state",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print extra per-event detail",
    )
    parser.add_argument(
        "--days-back", type=int, default=None, metavar="N",
        help="How many days back from now to scan the calendar (default: calendar_config.json's days_back, or 1)",
    )
    parser.add_argument(
        "--setup-google-auth", action="store_true",
        help="One-time interactive OAuth flow to obtain and store a Google refresh token, then exit",
    )
    parser.add_argument(
        "--void", nargs=2, metavar=("TICKET", "WORKLOG_ID"),
        help="Delete a previously-submitted Jira worklog (id from logs/<TICKET>/activity.log), then exit",
    )
    parser.add_argument(
        "--find-account", metavar="QUERY",
        help="Look up a Jira user's accountId by name/email (for sprint_reporter_account_id), then exit",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str | None:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        jw.log(f"ERROR: Google access-token refresh rejected: {e.code} {e.read().decode(errors='replace')}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        jw.log(f"ERROR: could not refresh Google access token: {e}")
        return None
    token = payload.get("access_token")
    if not token:
        jw.log(f"ERROR: Google token refresh response had no access_token: {payload}")
        return None
    return token


def exchange_auth_code(client_id: str, client_secret: str, auth_code: str, redirect_uri: str) -> dict | None:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        jw.log(f"ERROR: Google auth-code exchange rejected: {e.code} {e.read().decode(errors='replace')}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        jw.log(f"ERROR: Google auth-code exchange failed: {e}")
        return None


def extract_auth_code(pasted: str) -> str | None:
    """Accepts either the bare code or the full redirected URL it was in."""
    pasted = pasted.strip()
    if "code=" in pasted:
        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs and qs["code"]:
            return qs["code"][0]
    return pasted or None


def run_setup_google_auth() -> int:
    existing = jw.load_json(GOOGLE_CREDENTIALS_FILE, required=False, default={})
    client_id = existing.get("client_id") or input("Google OAuth client_id: ").strip()
    client_secret = existing.get("client_secret") or input("Google OAuth client_secret: ").strip()
    if not client_id or not client_secret:
        jw.log("ERROR: client_id and client_secret are required")
        return 1

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    print(
        "\nVisit this URL, sign in, and authorize access. Google will redirect to a "
        "localhost URL that won't load — that's fine, just copy the full address bar "
        "contents (or just the 'code=' value) back here:\n"
        f"{GOOGLE_AUTH_URL}?{params}\n"
    )
    pasted = input("Redirected URL or code: ")
    auth_code = extract_auth_code(pasted)
    if not auth_code:
        jw.log("ERROR: could not find an authorization code in that input")
        return 1

    token_payload = exchange_auth_code(client_id, client_secret, auth_code, GOOGLE_REDIRECT_URI)
    if not token_payload or "refresh_token" not in token_payload:
        jw.log(
            "ERROR: Google did not return a refresh_token. If you've authorized this app "
            "before, revoke access at https://myaccount.google.com/permissions and try again "
            "(Google only issues a refresh_token on first consent / forced re-consent)."
        )
        return 1

    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token_payload["refresh_token"],
    }
    jw.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    GOOGLE_CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2))
    os.chmod(GOOGLE_CREDENTIALS_FILE, 0o600)
    jw.log(f"Saved Google credentials to {GOOGLE_CREDENTIALS_FILE}")
    return 0


# ---------------------------------------------------------------------------
# Calendar fetch
# ---------------------------------------------------------------------------

def calendar_window(days_back: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now


def fetch_calendar_events(access_token: str, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict] | None:
    events: list[dict] = []
    page_token = None
    base = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    while True:
        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{base}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {access_token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            jw.log(f"ERROR: Google Calendar API rejected events request: {e.code} {e.read().decode(errors='replace')}")
            return None
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            jw.log(f"ERROR: could not fetch Google Calendar events: {e}")
            return None
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


# ---------------------------------------------------------------------------
# Jira JQL search (new; same auth/urllib pattern as jw.fetch_project_keys)
# ---------------------------------------------------------------------------

def jql_search(base_url: str, auth_header: str, jql: str) -> list[dict] | None:
    url = f"{base_url}/rest/api/3/search/jql?{urllib.parse.urlencode({'jql': jql, 'fields': 'key', 'maxResults': 50})}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        jw.log(f"WARN: Jira JQL search rejected ({e.code} {e.read().decode(errors='replace')}): {jql}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        jw.log(f"WARN: Jira JQL search failed ({e}): {jql}")
        return None
    return data.get("issues", [])


def search_jira_users(base_url: str, auth_header: str, query: str) -> list[dict] | None:
    """GET /rest/api/3/user/search — the lookup README.md used to tell you
    to run by hand with curl, to find the accountId sprint_reporter_account_id
    needs. Needs a Jira admin-granted 'browse users' permission, same as the
    curl version did; an unauthorized caller gets back an empty list, not an
    error, so there's nothing special to detect there."""
    url = f"{base_url}/rest/api/3/user/search?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        jw.log(f"ERROR: Jira user search rejected: {e.code} {e.read().decode(errors='replace')}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        jw.log(f"ERROR: Jira user search failed: {e}")
        return None


def run_find_account(base_url: str, auth_header: str, query: str) -> int:
    users = search_jira_users(base_url, auth_header, query)
    if users is None:
        return 1
    if not users:
        print(
            f"No Jira users found matching {query!r}. If this happens for every query "
            f"(even a single common letter), your Jira instance likely restricts "
            f"/rest/api/3/user/search to admins — ask a Jira admin for the accountId instead, "
            f"or have them grant you the 'Browse users' global permission."
        )
        return 1
    for u in users:
        print(f"{u.get('accountId')}  {u.get('displayName')}  <{u.get('emailAddress', '?')}>"
              f"{'' if u.get('active', True) else '  [inactive]'}")
    if len(users) > 1:
        print("\nMultiple matches — copy the accountId of the right person into "
              "sprint_reporter_account_id in calendar_config.json.")
    return 0


def fetch_board_sprints(base_url: str, auth_header: str, board_id: int) -> list[dict] | None:
    """Every active + closed sprint on the board, with its dates, so each
    meeting can be matched to the sprint that was running *when it
    happened* (see sprint_number_for_time), not whichever sprint happens
    to be active when the script runs. Paginated; None on any failure."""
    sprints: list[dict] = []
    start_at = 0
    while True:
        query = urllib.parse.urlencode({"state": "active,closed", "startAt": start_at, "maxResults": 50})
        url = f"{base_url}/rest/agile/1.0/board/{board_id}/sprint?{query}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", auth_header)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            jw.log(f"WARN: sprint lookup rejected ({e.code} {e.read().decode(errors='replace')}) for board {board_id}")
            return None
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            jw.log(f"WARN: sprint lookup failed ({e}) for board {board_id}")
            return None
        values = data.get("values", [])
        sprints.extend(values)
        if data.get("isLast", True) or not values:
            return sprints
        start_at += len(values)


# ---------------------------------------------------------------------------
# Ticket resolution
# ---------------------------------------------------------------------------

def extract_sprint_number(title: str | None, pattern: re.Pattern) -> str | None:
    if not title:
        return None
    m = pattern.search(title)
    return m.group(1) if m else None


def resolve_sprint_ticket(sprint_num: str, jira_project_keys: list[str], reporter_account_id: str,
                           cache: dict, search_fn) -> str | None:
    if sprint_num in cache:
        return cache[sprint_num]
    projects_clause = ", ".join(jira_project_keys)
    jql = f'project in ({projects_clause}) AND summary ~ "Sprint #{sprint_num}" AND reporter = "{reporter_account_id}"'
    issues = search_fn(jql)
    if issues is None:
        jw.log(f"WARN: sprint ticket lookup for Sprint #{sprint_num} failed (search error); skipping this meeting")
        return None
    if len(issues) == 1:
        ticket = issues[0]["key"]
        cache[sprint_num] = ticket
        jw.log(f"Resolved Sprint #{sprint_num} -> {ticket}")
        return ticket
    if not issues:
        jw.log(f"WARN: Sprint #{sprint_num}: found 0 matching ticket(s); skipping this meeting")
    else:
        keys = ", ".join(i["key"] for i in issues)
        jw.log(f"WARN: Sprint #{sprint_num}: found {len(issues)} matching ticket(s) ({keys}), ambiguous; skipping this meeting")
    return None


def sprint_interval(sprint: dict) -> tuple[datetime, datetime | None] | None:
    """[start, end) a sprint actually covered. A closed sprint ends at its
    completeDate (when it was really closed), falling back to its planned
    endDate. An active sprint is open-ended (None) -- it's still running
    even if it overran its planned endDate. A sprint with no startDate
    (never started) has no interval."""
    try:
        start = jw.parse_ts(sprint["startDate"])
    except (KeyError, TypeError, ValueError):
        return None
    if sprint.get("state") == "active":
        return start, None
    raw_end = sprint.get("completeDate") or sprint.get("endDate")
    try:
        return start, jw.parse_ts(raw_end)
    except (TypeError, ValueError):
        return None


def sprint_number_for_time(sprints: list[dict] | None, when: datetime, sprint_re: re.Pattern) -> str | None:
    """The number of the board sprint that was running at `when` (e.g. a
    meeting's start), extracted from the sprint's name ('Sprint 50' ->
    '50'). Exactly one sprint must cover `when`; several overlapping ->
    None, never guess -- same caution as resolve_sprint_ticket's
    ambiguous-match handling.

    Same-day gap rule: sprints here usually get closed one day and the
    next one "started" in Jira only later on its first day, so a morning
    meeting on a sprint's first day falls in no sprint at all. If nothing
    covers `when`, a sprint that starts later on the same *local* day
    claims it (exactly one such sprint, else None). Any longer gap, or
    before the board's history -> None."""
    if not sprints:
        return None
    matches = []
    starts_later_same_day = []
    when_day = jw.local_date(when)
    for sprint in sprints:
        interval = sprint_interval(sprint)
        if interval is None:
            continue
        start, end = interval
        if start <= when and (end is None or when < end):
            matches.append(sprint)
        elif start > when and jw.local_date(start) == when_day:
            starts_later_same_day.append(sprint)
    if not matches and len(starts_later_same_day) == 1:
        jw.vlog(f"{when.isoformat()} is before {starts_later_same_day[0].get('name')!r} was started "
                f"in Jira, but on its first day; using it")
        matches = starts_later_same_day
    if len(matches) != 1:
        names = ", ".join(str(s.get("name")) for s in matches) or "none"
        jw.vlog(f"No single board sprint covers {when.isoformat()} (matches: {names})")
        return None
    return extract_sprint_number(matches[0].get("name"), sprint_re)


def resolve_ticket_for_event(event: dict, known_keys: set[str] | None, cal_cfg: dict,
                              sprint_cache: dict, search_fn, sprint_re: re.Pattern,
                              board_sprint_number: str | None = None) -> tuple[str | None, str]:
    """Tier 1: direct ticket key/link in summary+description+location.
    Tier 2: recurring 'Sprint #N' meetings resolved via JQL, narrowed by
    cal_cfg's jira_project_keys + sprint_reporter_account_id.
    Tier 3: any other meeting falls back to board_sprint_number (the
    board sprint that was running when this meeting started, from
    sprint_number_for_time) -- same JQL/cache/ambiguous-skip
    machinery as Tier 2, just sourced from the board instead of the title.
    Returns (ticket_or_None, reason) — reason is logged, never guessed from."""
    text = " ".join(filter(None, [event.get("summary"), event.get("description"), event.get("location")]))
    ticket = jw.ticket_from_branch(text, known_keys)
    if ticket:
        return ticket, "direct"

    reporter_account_id = cal_cfg.get("sprint_reporter_account_id")
    if reporter_account_id:
        title_sprint_num = extract_sprint_number(event.get("summary"), sprint_re)
        sprint_num = title_sprint_num or board_sprint_number
        if sprint_num:
            ticket = resolve_sprint_ticket(
                sprint_num, cal_cfg["jira_project_keys"], reporter_account_id, sprint_cache, search_fn,
            )
            reason_prefix = "sprint" if title_sprint_num else "board_sprint"
            if ticket:
                return ticket, f"{reason_prefix}:{sprint_num}"
            return None, f"{reason_prefix}:{sprint_num}:ambiguous"

    return None, "unmatched"


# ---------------------------------------------------------------------------
# Event -> billable entry
# ---------------------------------------------------------------------------

def event_start_end(event: dict) -> tuple[datetime, datetime] | None:
    start = event.get("start") or {}
    end = event.get("end") or {}
    if "date" in start or "date" in end:
        return None  # all-day event: no meaningful duration to bill
    try:
        return jw.parse_ts(start["dateTime"]), jw.parse_ts(end["dateTime"])
    except (KeyError, ValueError):
        return None


def event_duration_minutes(event: dict) -> float | None:
    se = event_start_end(event)
    if se is None:
        return None
    start_dt, end_dt = se
    minutes = (end_dt - start_dt).total_seconds() / 60
    return minutes if minutes > 0 else None


def meeting_comment(event: dict) -> str:
    """Jira worklog comment for a meeting: just the title — no AI
    summarization, the title is already the summary."""
    return event.get("summary") or "Meeting"


def event_to_entry(event: dict, ticket: str, minutes: float, start_dt: datetime, shortened: bool = False) -> dict:
    """One entry per meeting -- unlike the main tool, these are never merged
    by (ticket, day): each meeting gets its own Jira worklog, at its own
    actual (possibly overlap-clipped) start time, with its own title as
    the comment. Shaped to match what jw.apply_daily_cap() expects (it
    only groups by "day", so capping across several same-day meetings
    still works unchanged). `shortened` appends a note to the comment
    when resolve_overlaps clipped this meeting's duration because a
    higher-priority meeting overlapped it."""
    prompts = [meeting_comment(event)]
    if shortened:
        prompts.append("shortened: overlapped with a higher-priority meeting")
    return {
        "ticket": ticket,
        "day": jw.local_date(start_dt),
        "minutes": minutes,
        "prompts": prompts,
        "started": start_dt,
        "sessions": {event["id"]},
    }


def is_new_event(event_id: str, cal_state: dict) -> bool:
    return event_id not in cal_state.get("logged_events", {})


def self_response_status(event: dict) -> str | None:
    """Your own RSVP on this event ('accepted'/'declined'/'tentative'/
    'needsAction'), or None if there's no attendee data at all (e.g. a solo
    block you created yourself) — Google Calendar has no 'did you actually
    join' signal, only this RSVP, so that's the best available proxy for
    an optional meeting you chose to skip."""
    for attendee in event.get("attendees") or []:
        if attendee.get("self"):
            return attendee.get("responseStatus")
    return None


def is_declined_by_self(event: dict) -> bool:
    """Only an explicit decline excludes a meeting — tentative, no-response,
    and events with no RSVP data at all still bill (confirmed policy)."""
    return self_response_status(event) == "declined"


# ---------------------------------------------------------------------------
# Overlap resolution
# ---------------------------------------------------------------------------

def event_priority_tier(event: dict) -> int:
    """0 (highest) for an accepted RSVP, 1 for everything else that
    survives is_declined_by_self (tentative/no-response/no attendee data
    at all — these already bill the same as each other, so they share a
    tier). Used only by resolve_overlaps to decide who keeps the
    overlapping slice of time; a declined meeting never reaches this,
    it's filtered out earlier."""
    return 0 if self_response_status(event) == "accepted" else 1


def _merge_claimed(claimed: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> None:
    """Inserts [start, end) into the sorted, merged list of claimed
    intervals in place."""
    claimed.append((start, end))
    claimed.sort()
    merged = [claimed[0]]
    for s, e in claimed[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    claimed[:] = merged


def _largest_remainder(start: datetime, end: datetime,
                        claimed: list[tuple[datetime, datetime]]) -> tuple[datetime, datetime] | None:
    """[start, end) minus everything already in `claimed`, keeping only
    the single largest contiguous leftover piece. An event is never
    fragmented into two separate billable pieces (e.g. a higher-priority
    meeting nested in the middle would split it into a before/after
    remainder; only the bigger half bills and the smaller one is dropped
    silently) -- same "don't guess/split" spirit as the rest of this
    tool's ticket resolution. None if nothing is left at all."""
    pieces = [(start, end)]
    for c_start, c_end in claimed:
        next_pieces = []
        for p_start, p_end in pieces:
            if c_end <= p_start or c_start >= p_end:
                next_pieces.append((p_start, p_end))
                continue
            if c_start > p_start:
                next_pieces.append((p_start, c_start))
            if c_end < p_end:
                next_pieces.append((c_end, p_end))
        pieces = next_pieces
    pieces = [p for p in pieces if p[1] > p[0]]
    if not pieces:
        return None
    return max(pieces, key=lambda p: p[1] - p[0])


def logged_billed_interval(event: dict, record: dict) -> tuple[datetime, datetime] | None:
    """The wall-clock interval an already-logged event actually billed, for
    seeding resolve_overlaps' claimed time. Uses the billed_start/
    billed_minutes recorded in calendar_state.json["logged_events"] when
    present (an overlap- or cap-clipped event only blocks what it really
    billed; one dropped to 0 blocks nothing). Records written before those
    fields existed fall back to the event's full scheduled interval, which
    can only over-block -- i.e. err toward billing less, never double."""
    billed_start = record.get("billed_start")
    billed_minutes = record.get("billed_minutes")
    if billed_start is not None and billed_minutes is not None:
        if billed_minutes <= 0:
            return None
        try:
            start_dt = jw.parse_ts(billed_start)
        except (TypeError, ValueError):
            return event_start_end(event)
        return start_dt, start_dt + timedelta(minutes=billed_minutes)
    return event_start_end(event)


def resolve_overlaps(events_with_times: list[tuple[dict, datetime, datetime]],
                     already_claimed: list[tuple[datetime, datetime]] | None = None,
                     ) -> list[tuple[dict, datetime, float]]:
    """Clips each event's billable interval against higher-or-equal-
    priority events that overlap it, so two overlapping meetings never
    double-bill the same wall-clock time. Priority: an accepted RSVP
    outranks everything else; tied priority breaks by earlier start --
    the meeting you were already in keeps the floor over one that starts
    overlapping it later (an accepted meeting can still claim time away
    from an already-started lower-priority one, though, since tier beats
    start time). An event fully covered by a higher-priority one bills 0
    minutes; see _largest_remainder for the never-split-into-two-pieces
    rule. Returns (event, billable_start, billable_minutes) once per
    input event; order is not meaningful to callers.

    `already_claimed` seeds the claimed time with intervals already billed
    to Jira in earlier runs (see logged_billed_interval). Those always win
    regardless of RSVP tier: their worklogs already exist and this tool
    never edits a submitted worklog, so clipping the new event is the only
    way to avoid billing the same wall-clock slice twice. They are never
    returned, so nothing already logged is ever resubmitted."""
    ordered = sorted(events_with_times, key=lambda item: (event_priority_tier(item[0]), item[1]))
    claimed: list[tuple[datetime, datetime]] = []
    for c_start, c_end in already_claimed or []:
        if c_end > c_start:
            _merge_claimed(claimed, c_start, c_end)
    results = []
    for event, start_dt, end_dt in ordered:
        remainder = _largest_remainder(start_dt, end_dt, claimed)
        if remainder is None:
            results.append((event, start_dt, 0.0))
        else:
            r_start, r_end = remainder
            results.append((event, r_start, (r_end - r_start).total_seconds() / 60))
        _merge_claimed(claimed, start_dt, end_dt)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(args: argparse.Namespace) -> int:
    jw.VERBOSE = args.verbose

    creds = jw.load_json(jw.CREDENTIALS_FILE)
    jw.validate_credentials(creds)
    base_url = creds["base_url"].rstrip("/")
    token = base64.b64encode(f"{creds['email']}:{creds['api_token']}".encode()).decode()
    auth_header = f"Basic {token}"

    if args.void:
        ticket, worklog_id = args.void
        ok = jw.void_worklog(base_url, auth_header, ticket, worklog_id)
        if ok:
            jw.log(f"Voided worklog {worklog_id} on {ticket}")
            jw.log_ticket(ticket, f"status=voided worklog_id={worklog_id} source=calendar")
        return 0 if ok else 1

    if args.find_account:
        return run_find_account(base_url, auth_header, args.find_account)

    google_creds = jw.load_json(GOOGLE_CREDENTIALS_FILE, required=False, default={})
    validate_google_credentials(google_creds)

    cal_cfg = jw.load_json(CALENDAR_CONFIG_FILE)
    validate_calendar_config(cal_cfg)

    main_cfg = jw.load_json(jw.CONFIG_FILE)
    daily_cap_hours = main_cfg.get("daily_cap_hours", 7)

    access_token = refresh_access_token(
        google_creds["client_id"], google_creds["client_secret"], google_creds["refresh_token"],
    )
    if not access_token:
        jw.log("ERROR: could not obtain a Google access token; aborting")
        return 1

    state = jw.load_json(jw.STATE_FILE, required=False, default={"sessions": {}, "bootstrapped_projects": []})
    state.setdefault("daily_submitted_minutes", {})
    known_keys = jw.get_known_project_keys(base_url, auth_header, state)

    cal_state = jw.load_json(CALENDAR_STATE_FILE, required=False, default={})
    cal_state.setdefault("logged_events", {})
    cal_state.setdefault("sprint_ticket_cache", {})

    days_back = args.days_back if args.days_back is not None else cal_cfg.get("days_back", DEFAULT_DAYS_BACK)
    time_min, time_max = calendar_window(days_back)
    calendar_id = cal_cfg.get("calendar_id", "primary")

    if args.dry_run:
        jw.log("DRY RUN: no worklogs will be submitted to Jira and no state will be saved")

    events = fetch_calendar_events(access_token, calendar_id, time_min, time_max)
    if events is None:
        jw.log("ERROR: could not fetch calendar events; aborting")
        return 1

    sprint_re = re.compile(cal_cfg.get("sprint_title_regex", DEFAULT_SPRINT_RE.pattern), re.IGNORECASE)
    search_fn = lambda jql: jql_search(base_url, auth_header, jql)

    # Fetched once per run; each meeting is then matched to the sprint
    # that was running when it started (sprint_number_for_time below).
    board_sprints = None
    board_id = cal_cfg.get("board_id")
    if board_id and cal_cfg.get("sprint_reporter_account_id"):
        board_sprints = fetch_board_sprints(base_url, auth_header, board_id)

    all_entries = []
    unmatched = 0
    skipped_dupe = 0
    skipped_all_day = 0
    skipped_declined = 0
    overlap_dropped = 0
    overlap_shortened = 0

    # Pass 1: dedup/declined/all-day filtering only -- deliberately before
    # ticket resolution, since resolve_overlaps needs every surviving
    # event's real interval up front to clip against each other, and
    # doesn't care yet which ticket (if any) an event will resolve to.
    candidates = []  # (event, start_dt, end_dt, full_minutes)
    # Time already billed by events logged in earlier runs: still blocks
    # new overlapping events (regardless of the old event's current RSVP --
    # its worklog exists in Jira either way), but is never resubmitted.
    already_claimed: list[tuple[datetime, datetime]] = []
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        if not is_new_event(event_id, cal_state):
            skipped_dupe += 1
            interval = logged_billed_interval(event, cal_state["logged_events"][event_id])
            if interval is not None:
                already_claimed.append(interval)
            continue
        if is_declined_by_self(event):
            skipped_declined += 1
            jw.vlog(f"Skipped declined event {event_id!r} ({event.get('summary')!r})")
            continue
        full_minutes = event_duration_minutes(event)
        if full_minutes is None:
            skipped_all_day += 1
            continue
        start_dt, end_dt = event_start_end(event)
        candidates.append((event, start_dt, end_dt, full_minutes))

    full_minutes_by_id = {e["id"]: fm for e, _, _, fm in candidates}
    resolved = resolve_overlaps([(e, s, en) for e, s, en, _ in candidates], already_claimed)

    # Pass 2: ticket resolution + entry building, using each event's
    # overlap-clipped (billable_start, billable_minutes) instead of its
    # raw scheduled interval.
    for event, billable_start, billable_minutes in resolved:
        event_id = event["id"]
        ticket, reason = resolve_ticket_for_event(
            event, known_keys, cal_cfg, cal_state["sprint_ticket_cache"], search_fn, sprint_re,
            sprint_number_for_time(board_sprints, event_start_end(event)[0], sprint_re),
        )
        if not ticket:
            unmatched += 1
            jw.vlog(f"Unmatched event {event_id!r} ({event.get('summary')!r}): {reason}")
            continue
        full_minutes = full_minutes_by_id[event_id]
        shortened = 0 < billable_minutes < full_minutes - 0.5
        if shortened:
            overlap_shortened += 1
        entry = event_to_entry(event, ticket, billable_minutes, billable_start, shortened=shortened)
        if billable_minutes <= 0:
            overlap_dropped += 1
            entry["dropped_reason"] = "overlap"
            jw.vlog(f"Overlap-dropped event {event_id!r} ({event.get('summary')!r}): "
                    f"fully covered by a higher-priority meeting")
        all_entries.append(entry)

    if not all_entries:
        jw.log(
            f"No new billable meeting activity. "
            f"(unmatched={unmatched} dupes_skipped={skipped_dupe} all_day_skipped={skipped_all_day} "
            f"declined_skipped={skipped_declined} overlap_dropped={overlap_dropped})"
        )
        return 0

    # Deliberately not jw.aggregate()'d — each meeting stays its own entry
    # and becomes its own Jira worklog, rather than merging same-day/same-
    # ticket meetings into one. jw.apply_daily_cap() only groups by "day",
    # so the cap still applies correctly across all of a day's meetings.
    entries = all_entries
    jw.apply_daily_cap(entries, daily_cap_hours, state["daily_submitted_minutes"])

    remaining_per_event: dict[str, int] = {}
    for e in entries:
        for eid in e["sessions"]:
            remaining_per_event[eid] = remaining_per_event.get(eid, 0) + 1

    submitted = 0
    failed = 0
    dropped = 0
    blocked_events: set[str] = set()

    for e in entries:
        if e["minutes"] <= 0:
            dropped += 1
            # "overlap" was set in pass 2 above, before the cap even ran;
            # anything else hitting zero here was zeroed by the cap itself.
            drop_reason = e.get("dropped_reason", "cap")
            drop_desc = ("fully overlapped by a higher-priority meeting" if drop_reason == "overlap"
                         else "no room left under daily cap")
            if args.dry_run:
                jw.log(f"[DRY RUN] Would drop {e['ticket']} ({e['day']}): {drop_desc}")
            else:
                jw.log_ticket(e["ticket"], f"day={e['day']} status=dropped_{drop_reason} source=calendar")
        else:
            seconds = jw.round_minutes(e["minutes"]) * 60
            comment = "; ".join(dict.fromkeys(e["prompts"]))
            if args.dry_run:
                jw.log(f"[DRY RUN] Would log {seconds // 60}m meeting time on {e['ticket']} ({e['day']}): {comment}")
                submitted += 1
            else:
                worklog_id = jw.submit_worklog(base_url, auth_header, e["ticket"], e["started"], seconds, comment)
                if worklog_id:
                    submitted += 1
                    jw.log(f"Logged {seconds // 60}m meeting time on {e['ticket']} ({e['day']}) [worklog {worklog_id}]")
                    jw.log_ticket(
                        e["ticket"],
                        f"day={e['day']} minutes={seconds // 60} status=submitted worklog_id={worklog_id} "
                        f"source=calendar comment={comment!r}",
                    )
                    state["daily_submitted_minutes"][e["day"]] = (
                        state["daily_submitted_minutes"].get(e["day"], 0.0) + seconds / 60
                    )
                else:
                    failed += 1
                    blocked_events.update(e["sessions"])
                    jw.log_ticket(
                        e["ticket"], f"day={e['day']} minutes={seconds // 60} status=failed source=calendar comment={comment!r}",
                    )

        if args.dry_run:
            continue
        for eid in e["sessions"]:
            remaining_per_event[eid] -= 1
            if remaining_per_event[eid] == 0 and eid not in blocked_events:
                cal_state["logged_events"][eid] = {
                    "ticket": e["ticket"], "day": e["day"],
                    # What was actually billed (post overlap-clip and cap
                    # scaling), so later runs block only this slice -- see
                    # logged_billed_interval.
                    "billed_start": e["started"].isoformat(),
                    "billed_minutes": round(e["minutes"], 2),
                }
        jw.save_state(state)
        save_calendar_state(cal_state)

    jw.log(
        f"Done. submitted={submitted} failed={failed} dropped={dropped} "
        f"unmatched={unmatched} dupes_skipped={skipped_dupe} all_day_skipped={skipped_all_day} "
        f"declined_skipped={skipped_declined} overlap_dropped={overlap_dropped} overlap_shortened={overlap_shortened}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    args = parse_args()
    jw.LOG_DIR.mkdir(parents=True, exist_ok=True)
    jw.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if args.setup_google_auth:
        sys.exit(run_setup_google_auth())

    lock_fh = jw.acquire_lock()
    if lock_fh is None:
        jw.log("Another run is already in progress; exiting.")
        sys.exit(0)
    try:
        exit_code = main(args)
    finally:
        lock_fh.close()
    sys.exit(exit_code)

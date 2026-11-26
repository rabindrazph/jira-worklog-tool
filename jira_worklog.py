#!/usr/bin/env python3
"""Scans local Claude Code session transcripts (prompts + responses) for
enabled local projects, estimates active time from message-gap timing,
maps sessions to Jira tickets via the recorded git branch, and auto-submits
Jira worklogs. Designed to run unattended from cron. Stdlib only.
"""
import argparse
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUMMARY_MODEL = "claude-sonnet-5"
SUMMARY_TIMEOUT_SECONDS = 45

HOME = Path.home()
# TOOL_DIR is derived from this file's own location, not hardcoded, so the
# repo works no matter where/what it's cloned as. BASE_DIR (its parent) is
# the folder that holds all your Jira-ticket project checkouts alongside it.
TOOL_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOL_DIR.parent
CONFIG_DIR = HOME / ".config" / "jira-worklog-tool"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOCK_FILE = CONFIG_DIR / "run.lock"
LOG_DIR = TOOL_DIR / "logs"
TRANSCRIPTS_ROOT = HOME / ".claude" / "projects"

# cron runs with a minimal PATH that usually doesn't include ~/.local/bin
# (where the claude CLI is commonly installed), so PATH lookup alone isn't
# reliable here — fall back to the common install locations.
CLAUDE_BIN_CANDIDATES = [HOME / ".local" / "bin" / "claude", Path("/usr/local/bin/claude")]

TICKET_RE = re.compile(r'([A-Z][A-Z0-9]{1,9}-\d+)')
ROUND_TO_MINUTES = 5
MAX_PROMPT_CHARS = 140
PROJECT_KEYS_CACHE_TTL = timedelta(hours=24)
DAILY_TOTALS_RETENTION_DAYS = 14

REDACTION_PATTERNS = [
    (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._-]{10,}'), "Bearer [REDACTED]"),
    (re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'), "[REDACTED_JWT]"),
    (re.compile(r'(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*\S+'),
     lambda m: f"{m.group(1)}=[REDACTED]"),
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'), "[REDACTED_EMAIL]"),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), "[REDACTED_AWS_KEY]"),
]

VERBOSE = False


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        log_file = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
        with log_file.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def vlog(msg: str) -> None:
    if VERBOSE:
        log(f"[verbose] {msg}")


def log_ticket(ticket: str, msg: str) -> None:
    """Append-only per-ticket activity record, so what was actually
    submitted for a given ticket can be reviewed without grepping the
    combined run logs."""
    ts = datetime.now().isoformat(timespec="seconds")
    ticket_dir = LOG_DIR / ticket
    try:
        ticket_dir.mkdir(parents=True, exist_ok=True)
        with (ticket_dir / "activity.log").open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError as e:
        log(f"WARN: could not write ticket log for {ticket}: {e}")


def load_json(path: Path, required: bool = True, default=None):
    if not path.exists():
        if required:
            log(f"ERROR: required file not found: {path}")
            sys.exit(1)
        return default
    return json.loads(path.read_text())


def save_state(state: dict) -> None:
    """Atomic write (temp file + rename) so a crash mid-write can never
    leave state.json truncated or corrupted."""
    tmp_path = STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, STATE_FILE)


def acquire_lock():
    """Non-blocking advisory lock so an overlapping cron run can't double-submit
    worklogs. Returns an open file handle to hold for the run's duration, or
    None if another run already holds it. The OS releases the lock
    automatically if the holding process dies, so there's no stale-lock case
    to clean up."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def validate_credentials(creds: dict) -> None:
    missing = [k for k in ("base_url", "email", "api_token") if not creds.get(k)]
    if missing:
        log(f"ERROR: credentials.json missing required key(s): {', '.join(missing)}")
        sys.exit(1)


def validate_config(cfg: dict) -> None:
    projects = cfg.get("enabled_projects")
    if not isinstance(projects, list) or not projects:
        log("ERROR: config.json 'enabled_projects' must be a non-empty list")
        sys.exit(1)
    for key in ("gap_cap_minutes", "min_floor_minutes", "daily_cap_hours"):
        if key in cfg and not isinstance(cfg[key], (int, float)):
            log(f"ERROR: config.json '{key}' must be a number")
            sys.exit(1)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute worklogs and print what would be submitted, without calling Jira or saving state",
    )
    parser.add_argument(
        "--project", dest="projects", action="append", metavar="NAME",
        help="Only process this project (repeatable); default is every project in config.json's enabled_projects",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print extra per-session/interval detail",
    )
    parser.add_argument(
        "--void", nargs=2, metavar=("TICKET", "WORKLOG_ID"),
        help="Delete a previously-submitted Jira worklog (id from logs/<TICKET>/activity.log), then exit",
    )
    parser.add_argument(
        "--skip", action="store_true",
        help="Mark all pending activity as seen WITHOUT billing it (e.g. a session that wasn't real ticket work)",
    )
    return parser.parse_args(argv)


def transcript_dir_for(project_name: str) -> Path:
    project_path = BASE_DIR / project_name
    dir_name = str(project_path).replace("/", "-")
    return TRANSCRIPTS_ROOT / dir_name


def parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def extract_prompt_text(entry: dict) -> str | None:
    """Best-effort human-readable text for a user or assistant turn."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    content = msg.get("content")
    if role == "user" and isinstance(content, str):
        return content.strip()
    if role == "assistant" and isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        joined = " ".join(t for t in texts if t).strip()
        return joined or None
    return None


def load_session_events(path: Path) -> list[dict]:
    """Every line with a timestamp, carrying gitBranch + optional text."""
    events = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = entry.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = parse_ts(ts_raw)
        except ValueError:
            continue
        events.append({
            "ts": ts,
            "branch": entry.get("gitBranch"),
            "role": entry.get("type"),
            "text": extract_prompt_text(entry),
        })
    events.sort(key=lambda e: e["ts"])
    return events


def ticket_from_branch(branch: str | None, known_keys: set[str] | None = None) -> str | None:
    if not branch:
        return None
    m = TICKET_RE.search(branch)
    if not m:
        return None
    ticket = m.group(1)
    if known_keys is not None and ticket.rsplit("-", 1)[0] not in known_keys:
        return None
    return ticket


def fetch_project_keys(base_url: str, auth_header: str) -> set[str] | None:
    url = f"{base_url}/rest/api/3/project"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log(f"WARN: could not fetch Jira project list ({e}); skipping ticket-key validation this run")
        return None
    keys = {p["key"] for p in data if isinstance(p, dict) and "key" in p}
    if not keys:
        log("WARN: Jira project list came back empty; skipping ticket-key validation this run")
        return None
    return keys


def get_known_project_keys(base_url: str, auth_header: str, state: dict) -> set[str] | None:
    """Cached (24h) set of real Jira project keys, used to reject branch
    substrings that merely look like a ticket key. Falls back to no
    validation (returns None) if Jira can't be reached, rather than treating
    every session as unmatched."""
    cache = state.get("project_keys_cache")
    now = datetime.now(timezone.utc)
    if cache:
        try:
            fresh = now - parse_ts(cache["fetched_at"]) < PROJECT_KEYS_CACHE_TTL
        except (KeyError, ValueError):
            fresh = False
        if fresh:
            return set(cache["keys"])
    keys = fetch_project_keys(base_url, auth_header)
    if keys is None:
        return set(cache["keys"]) if cache else None
    state["project_keys_cache"] = {"fetched_at": now.isoformat(), "keys": sorted(keys)}
    return keys


def redact_sensitive(text: str) -> str:
    """Strip common credential/secret shapes before text leaves the machine
    via the summarizer subprocess call. Not exhaustive — covers accidental
    pastes of tokens/keys/passwords/emails, not general PII."""
    for pattern, repl in REDACTION_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def void_worklog(base_url: str, auth_header: str, ticket: str, worklog_id: str) -> bool:
    url = f"{base_url}/rest/api/3/issue/{ticket}/worklog/{worklog_id}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        return True
    except urllib.error.HTTPError as e:
        log(f"ERROR: Jira rejected void of worklog {worklog_id} on {ticket}: {e.code} {e.read().decode(errors='replace')}")
        return False
    except urllib.error.URLError as e:
        log(f"ERROR: could not reach Jira to void worklog {worklog_id} on {ticket}: {e}")
        return False


def local_date(ts: datetime) -> str:
    """Calendar date in the local timezone, matching the day worklogs are
    actually posted to Jira under (submit_worklog uses local time too)."""
    return ts.astimezone().date().isoformat()


def process_session(events: list[dict], logged_until: datetime | None,
                     gap_cap: timedelta, min_floor: timedelta,
                     known_keys: set[str] | None = None) -> tuple[list[dict], datetime | None, dict]:
    """
    Returns (intervals, new_logged_until, stats) where intervals is a list of
    {ticket, day, minutes, prompt_texts} and stats has unmatched_minutes count.
    """
    if not events:
        return [], logged_until, {"unmatched_minutes": 0.0}

    new_events = [e for e in events if logged_until is None or e["ts"] > logged_until]
    if not new_events:
        return [], logged_until, {"unmatched_minutes": 0.0}

    anchor = None
    if logged_until is not None:
        prior = [e for e in events if e["ts"] <= logged_until]
        if prior:
            anchor = prior[-1]

    sequence = ([anchor] if anchor else []) + new_events
    intervals = []
    unmatched_minutes = 0.0

    if anchor is None:
        first = sequence[0]
        ticket = ticket_from_branch(first["branch"], known_keys)
        minutes = min_floor.total_seconds() / 60
        if ticket:
            intervals.append({
                "ticket": ticket, "day": local_date(first["ts"]),
                "minutes": minutes, "prompts": [first["text"]] if first["text"] else [],
                "ts": first["ts"],
            })
        else:
            unmatched_minutes += minutes

    for i in range(1, len(sequence)):
        prev, cur = sequence[i - 1], sequence[i]
        gap = cur["ts"] - prev["ts"]
        active = min(gap, gap_cap)
        minutes = max(active.total_seconds() / 60, 0.0)
        if minutes <= 0:
            continue
        ticket = ticket_from_branch(cur["branch"], known_keys) or ticket_from_branch(prev["branch"], known_keys)
        day = local_date(cur["ts"])
        prompts = [t for t in (cur["text"],) if t]
        if ticket:
            intervals.append({"ticket": ticket, "day": day, "minutes": minutes, "prompts": prompts, "ts": prev["ts"]})
        else:
            unmatched_minutes += minutes

    new_logged_until = new_events[-1]["ts"]
    return intervals, new_logged_until, {"unmatched_minutes": unmatched_minutes}


def aggregate(intervals: list[dict]) -> dict[tuple[str, str], dict]:
    agg: dict[tuple[str, str], dict] = {}
    for iv in intervals:
        key = (iv["ticket"], iv["day"])
        if key not in agg:
            agg[key] = {
                "ticket": iv["ticket"], "day": iv["day"], "minutes": 0.0,
                "prompts": [], "sessions": set(), "started": iv["ts"],
            }
        entry = agg[key]
        entry["minutes"] += iv["minutes"]
        entry["prompts"].extend(iv["prompts"])
        entry["started"] = min(entry["started"], iv["ts"])
        if "session_key" in iv:
            entry["sessions"].add(iv["session_key"])
    return agg


def apply_daily_cap(entries: list[dict], cap_hours: float, already_submitted: dict[str, float] | None = None) -> None:
    """Scales down minutes so each day's total (this run's new minutes PLUS
    whatever was already successfully submitted for that day in earlier runs,
    per already_submitted) stays within cap_hours."""
    already_submitted = already_submitted or {}
    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(e["day"], []).append(e)
    cap_minutes = cap_hours * 60
    for day, day_entries in by_day.items():
        total_new = sum(e["minutes"] for e in day_entries)
        prior = already_submitted.get(day, 0.0)
        remaining_room = max(cap_minutes - prior, 0.0)
        if total_new <= remaining_room:
            continue
        if remaining_room <= 0:
            log(f"WARN: {day} already at/over {cap_hours}h cap from earlier run(s) ({prior:.0f}m); "
                f"dropping {total_new:.0f}m of new activity")
            scale = 0.0
        else:
            scale = remaining_room / total_new
            log(f"WARN: {day} new {total_new:.0f}m + already-logged {prior:.0f}m exceeds {cap_hours}h cap, "
                f"scaling new activity by {scale:.2f}")
        for e in day_entries:
            e["minutes"] *= scale


def round_minutes(minutes: float) -> int:
    rounded = round(minutes / ROUND_TO_MINUTES) * ROUND_TO_MINUTES
    return max(rounded, ROUND_TO_MINUTES)


def raw_activity_text(prompts: list[str]) -> str:
    seen = []
    for p in prompts:
        p = redact_sensitive(p.strip().replace("\n", " "))
        if not p or p in seen:
            continue
        seen.append(p)
        if len(seen) >= 15:
            break
    return "\n".join(seen)


def resolve_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_BIN_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return "claude"


def summarize_activity(ticket: str, raw_text: str, api_key: str | None, base_url: str | None) -> str | None:
    if not raw_text.strip() or not api_key:
        return None
    instruction = (
        "Rewrite the following raw log of prompts/notes from an engineer's AI-assisted "
        f"coding session into a VERY SHORT professional engineering status update for a "
        "manager reading a Jira worklog comment: strictly 1 sentence, under 25 words. "
        "Plain text only, no markdown, no quotes of the raw text, past tense.\n"
        "This comment is posted directly on the ticket it concerns, so do NOT mention the "
        "ticket number/key, and do NOT reference internal phase/batch numbering (e.g. "
        "'Phase 5') — just describe the actual technical change in plain terms a manager "
        "unfamiliar with the internal plan would understand.\n"
        "State what the engineer actually implemented/changed in this session, from the "
        "engineer's own stated intent (their prompts), not from the assistant's cautionary "
        "or explanatory commentary. Caution/risk text earlier in the notes often describes "
        "PAST history (e.g. a prior incident), not what happened in this session — weight the "
        "most recent actions/outcomes in the notes most heavily, and do not say something was "
        "'rejected' or 'not done' if the notes show work on it continued. Do not add vague "
        "process filler about HOW the work is being approached (e.g. 'continuing file by "
        "file', 'working through it incrementally') — state only the concrete WHAT: which "
        "specific things were changed or checked.\n\n"
        f"Raw session notes:\n{raw_text}"
    )
    cmd = [resolve_claude_bin(), "-p", "--restricted", "--bare", "--model", SUMMARY_MODEL, instruction]
    env = {**os.environ, "ANTHROPIC_AUTH_TOKEN": api_key}
    env.pop("ANTHROPIC_API_KEY", None)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    else:
        env.pop("ANTHROPIC_BASE_URL", None)
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=SUMMARY_TIMEOUT_SECONDS, env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f"WARN: summarizer call failed on attempt {attempt} ({e})")
            continue
        if result.returncode != 0:
            log(f"WARN: summarizer exited {result.returncode} on attempt {attempt}: "
                f"stderr={result.stderr.strip()[:300]!r} stdout={result.stdout.strip()[:300]!r}")
            continue
        summary = result.stdout.strip()
        if summary:
            return summary
        log(f"WARN: summarizer returned empty output on attempt {attempt}")
    log("WARN: summarizer failed after retry; falling back to raw notes")
    return None


def build_comment(ticket: str, prompts: list[str], api_key: str | None, anthropic_base_url: str | None) -> str:
    raw_text = raw_activity_text(prompts)
    summary = summarize_activity(ticket, raw_text, api_key, anthropic_base_url)
    if summary:
        return summary
    if not raw_text:
        return "Auto-logged from Claude Code session activity."
    truncated = [p[:MAX_PROMPT_CHARS] for p in raw_text.split("\n")[:8]]
    return "Auto-logged from Claude Code session activity (summary unavailable):\n" + "\n".join(
        f"- {t}" for t in truncated
    )


def submit_worklog(base_url: str, auth_header: str, ticket: str, started: datetime, seconds: int, comment: str) -> str | None:
    """POST a worklog to Jira. Returns the Jira worklog id on success, None on failure."""
    started_local = started.astimezone()
    started_str = started_local.strftime("%Y-%m-%dT%H:%M:%S.000%z")
    body = json.dumps({
        "started": started_str,
        "timeSpentSeconds": seconds,
        "comment": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
        },
    }).encode()
    url = f"{base_url}/rest/api/3/issue/{ticket}/worklog"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", auth_header)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        try:
            return json.loads(raw).get("id", "unknown")
        except json.JSONDecodeError:
            return "unknown"
    except urllib.error.HTTPError as e:
        log(f"ERROR: Jira rejected worklog for {ticket}: {e.code} {e.read().decode(errors='replace')}")
        return None
    except urllib.error.URLError as e:
        log(f"ERROR: could not reach Jira for {ticket}: {e}")
        return None


def main(args: argparse.Namespace) -> int:
    global VERBOSE
    VERBOSE = args.verbose

    creds = load_json(CREDENTIALS_FILE)
    validate_credentials(creds)
    base_url = creds["base_url"].rstrip("/")
    token = base64.b64encode(f"{creds['email']}:{creds['api_token']}".encode()).decode()
    auth_header = f"Basic {token}"
    anthropic_api_key = creds.get("anthropic_api_key")
    anthropic_base_url = creds.get("anthropic_base_url")
    if not anthropic_api_key:
        log("WARN: credentials.json has no 'anthropic_api_key'; comments will fall back to raw session notes")

    if args.void:
        ticket, worklog_id = args.void
        ok = void_worklog(base_url, auth_header, ticket, worklog_id)
        if ok:
            log(f"Voided worklog {worklog_id} on {ticket}")
            log_ticket(ticket, f"status=voided worklog_id={worklog_id}")
        return 0 if ok else 1

    cfg = load_json(CONFIG_FILE)
    validate_config(cfg)
    state = load_json(STATE_FILE, required=False, default={"sessions": {}, "bootstrapped_projects": []})
    state.setdefault("bootstrapped_projects", [])
    state.setdefault("daily_submitted_minutes", {})
    cutoff_day = local_date(datetime.now(timezone.utc) - timedelta(days=DAILY_TOTALS_RETENTION_DAYS))
    for day in [d for d in state["daily_submitted_minutes"] if d < cutoff_day]:
        del state["daily_submitted_minutes"][day]

    known_keys = get_known_project_keys(base_url, auth_header, state)

    gap_cap = timedelta(minutes=cfg.get("gap_cap_minutes", 10))
    min_floor = timedelta(minutes=cfg.get("min_floor_minutes", 5))
    daily_cap_hours = cfg.get("daily_cap_hours", 7)

    enabled_projects = cfg["enabled_projects"]
    if args.projects:
        unknown = [p for p in args.projects if p not in enabled_projects]
        if unknown:
            log(f"WARN: ignoring --project value(s) not in config.json: {', '.join(unknown)}")
        enabled_projects = [p for p in enabled_projects if p in args.projects]
        if not enabled_projects:
            log("ERROR: no matching project to process after applying --project filter")
            return 1

    if args.dry_run:
        log("DRY RUN: no worklogs will be submitted to Jira and no state will be saved")

    all_intervals = []
    total_unmatched_minutes = 0.0
    session_states_touched: dict[str, datetime] = {}

    for project in enabled_projects:
        tdir = transcript_dir_for(project)
        if not tdir.is_dir():
            log(f"WARN: no transcript dir for project '{project}' ({tdir}); skipping")
            continue
        project_live = project in state["bootstrapped_projects"]

        for session_file in sorted(tdir.glob("*.jsonl")):
            session_id = session_file.stem
            state_key = f"{project}:{session_id}"
            prev_logged_raw = state["sessions"].get(state_key)
            prev_logged = parse_ts(prev_logged_raw) if prev_logged_raw else None

            events = load_session_events(session_file)
            if not events:
                continue
            vlog(f"{state_key}: {len(events)} events loaded")

            if args.skip:
                new_events = [e for e in events if prev_logged is None or e["ts"] > prev_logged]
                if new_events:
                    session_states_touched[state_key] = new_events[-1]["ts"]
                    log(f"Skipped {state_key} (marked seen, not billed, --skip)")
                continue

            if prev_logged is None and not project_live:
                # Pre-existing session from before this project went live:
                # mark everything up to its current tail as already-seen.
                # Nothing logged yet. Once a project is marked live (below),
                # any session with no state entry is genuinely new and gets
                # logged from its true start instead.
                session_states_touched[state_key] = events[-1]["ts"]
                log(f"Bootstrapped {state_key} (no retroactive logging)")
                continue

            intervals, new_logged_until, stats = process_session(events, prev_logged, gap_cap, min_floor, known_keys)
            vlog(f"{state_key}: {len(intervals)} interval(s), unmatched={stats['unmatched_minutes']:.1f}m")
            total_unmatched_minutes += stats["unmatched_minutes"]
            for iv in intervals:
                iv["session_key"] = state_key
            all_intervals.extend(intervals)
            if new_logged_until:
                session_states_touched[state_key] = new_logged_until

        if not project_live:
            state["bootstrapped_projects"].append(project)
            log(f"Project '{project}' is now live: future new sessions will be logged from their start.")

    if not all_intervals:
        log(f"No new billable activity. (unmatched idle/no-ticket minutes: {total_unmatched_minutes:.0f})")
        if not args.dry_run:
            for key, ts in session_states_touched.items():
                state["sessions"][key] = ts.isoformat()
            save_state(state)
        return 0

    agg = aggregate(all_intervals)
    entries = list(agg.values())
    apply_daily_cap(entries, daily_cap_hours, state["daily_submitted_minutes"])

    # Sessions with no entries left pending can have their state committed as
    # soon as the entries they *do* appear in are resolved, instead of
    # waiting for the whole run to finish — this shrinks the window in which
    # a crash mid-run could cause a resubmission on the next run.
    remaining_per_session: dict[str, int] = {}
    for e in entries:
        for sk in e["sessions"]:
            remaining_per_session[sk] = remaining_per_session.get(sk, 0) + 1
    zero_entry_sessions = {k for k in session_states_touched if k not in remaining_per_session}
    if not args.dry_run:
        for key in zero_entry_sessions:
            state["sessions"][key] = session_states_touched[key].isoformat()
        save_state(state)

    submitted = 0
    failed = 0
    dropped = 0
    blocked_sessions: set[str] = set()
    for e in entries:
        if e["minutes"] <= 0:
            dropped += 1
            if args.dry_run:
                log(f"[DRY RUN] Would drop {e['ticket']} ({e['day']}): no room left under daily cap")
            else:
                log_ticket(e["ticket"], f"day={e['day']} status=dropped_cap (no room left under daily cap)")
            for sk in e["sessions"]:
                remaining_per_session[sk] -= 1
        else:
            seconds = round_minutes(e["minutes"]) * 60
            comment = build_comment(e["ticket"], e["prompts"], anthropic_api_key, anthropic_base_url)
            if args.dry_run:
                log(f"[DRY RUN] Would log {seconds // 60}m on {e['ticket']} ({e['day']}): {comment}")
                submitted += 1
                continue
            worklog_id = submit_worklog(base_url, auth_header, e["ticket"], e["started"], seconds, comment)
            if worklog_id:
                submitted += 1
                log(f"Logged {seconds // 60}m on {e['ticket']} ({e['day']}) [worklog {worklog_id}]")
                log_ticket(
                    e["ticket"],
                    f"day={e['day']} minutes={seconds // 60} status=submitted worklog_id={worklog_id} comment={comment!r}",
                )
                state["daily_submitted_minutes"][e["day"]] = state["daily_submitted_minutes"].get(e["day"], 0.0) + seconds / 60
            else:
                failed += 1
                blocked_sessions.update(e["sessions"])
                log_ticket(e["ticket"], f"day={e['day']} minutes={seconds // 60} status=failed comment={comment!r}")
            for sk in e["sessions"]:
                remaining_per_session[sk] -= 1

        if args.dry_run:
            continue
        for sk in e["sessions"]:
            if remaining_per_session[sk] != 0:
                continue
            if sk in blocked_sessions:
                log(f"WARN: not advancing state for {sk} due to failure; will retry.")
            else:
                state["sessions"][sk] = session_states_touched[sk].isoformat()
        save_state(state)

    log(f"Done. submitted={submitted} failed={failed} dropped={dropped} unmatched_minutes={total_unmatched_minutes:.0f}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = acquire_lock()
    if lock_fh is None:
        log("Another run is already in progress; exiting.")
        sys.exit(0)
    try:
        exit_code = main(args)
    finally:
        lock_fh.close()
    sys.exit(exit_code)

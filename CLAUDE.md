# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, stdlib-only Python script (`jira_worklog.py`) that scans local Claude Code session transcripts, infers active engineering time per Jira ticket, and auto-submits Jira worklogs. Intended to run unattended from cron. See `README.md` for install/setup instructions and `FUTURE_ENHANCEMENTS.md` for known gaps.

A sibling script, `jira_calendar_worklog.py`, does the same for meeting time by reading Google Calendar instead of Claude Code transcripts. It `import jira_worklog as jw`s and reuses its Jira credential/submission/state helpers rather than duplicating them — see the "Calendar add-on" subsections below for what's specific to it.

## Commands

Run directly (no install/build step, no dependencies beyond the Python stdlib):

```bash
python3 jira_worklog.py                        # normal run
python3 jira_worklog.py --dry-run --verbose     # preview without touching Jira or state
python3 jira_worklog.py --project claims-manager  # restrict to one project (repeatable flag)
python3 jira_worklog.py --void TICKET WORKLOG_ID  # delete a wrongly-submitted worklog
python3 -m unittest test_jira_worklog -v        # run the test suite

python3 jira_calendar_worklog.py                              # normal run (calendar add-on)
python3 jira_calendar_worklog.py --dry-run --verbose --days-back 7
python3 jira_calendar_worklog.py --setup-google-auth          # one-time OAuth setup
python3 jira_calendar_worklog.py --void TICKET WORKLOG_ID
python3 -m unittest test_calendar_worklog -v    # calendar add-on's test suite
```

There is no linter or build process in this repo. `fcntl` makes the script POSIX-only (Linux/macOS), which is fine since it only ever runs from local cron.

## Runtime configuration (outside the repo)

The script reads/writes state under `CONFIG_DIR` (a constant near the top of `jira_worklog.py`), not in this repo:

- `credentials.json` — `{base_url, email, api_token}` for Jira Basic Auth (required; validated by `validate_credentials()`), plus optional `anthropic_api_key` + `anthropic_base_url` used to authenticate the `claude -p` comment-summarizer subprocess headlessly. These are exported as `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` (bearer-token auth through this org's internal AI gateway, not the public Anthropic API's `ANTHROPIC_API_KEY`/x-api-key), combined with `--bare` so the subprocess never falls back to OAuth/keychain login. Without `anthropic_api_key`, comments fall back to raw session notes.
- `config.json` — `{enabled_projects: [...], gap_cap_minutes, min_floor_minutes, daily_cap_hours}`. Required; validated by `validate_config()`.
- `state.json` — `{sessions: {...}, bootstrapped_projects: [...], daily_submitted_minutes: {...}, project_keys_cache: {...}}`. Created automatically, written atomically (temp file + `os.replace`).
- `run.lock` — advisory lock (`fcntl.flock`) held for the duration of a run; a concurrent invocation exits immediately instead of racing.

Logs are written to `logs/` under this repo's directory (`TOOL_DIR`): a dated `logs/<YYYY-MM-DD>.log` for the whole run, plus `logs/<TICKET>/activity.log` per ticket (day, minutes, status, Jira worklog id, comment — one line per submission/failure/drop/void).

When changing code that reads these files, check actual file contents in `CONFIG_DIR` rather than assuming shape, since nothing in the repo enforces a schema beyond the startup validation.

**Calendar add-on's own files, also in `CONFIG_DIR`:**
- `google_credentials.json` — `{client_id, client_secret, refresh_token}`, written by `--setup-google-auth` (chmod 600). `refresh_token` doesn't expire unless revoked; the access token is refreshed unconditionally once per run, no expiry tracking needed.
- `calendar_config.json` — `{calendar_id, jira_project_keys, sprint_reporter_account_id, board_id, days_back, sprint_title_regex}`. Required; validated by `validate_calendar_config()` (only `jira_project_keys` is required). `jira_project_keys` are real Jira project keys, deliberately distinct from `config.json`'s `enabled_projects` (which are local folder names, not Jira keys, and irrelevant to the calendar tool). `board_id` is a Jira Agile/Scrum board id (`GET /rest/agile/1.0/board?projectKeyOrId=<key>` to find it) — enables Tier 3 ticket resolution (see Architecture stage 3 below) when set alongside `sprint_reporter_account_id`.
- `calendar_state.json` — `{logged_events: {event_id: {ticket, day, billed_start, billed_minutes}}, sprint_ticket_cache: {sprint_number: ticket}}`. Created automatically, written atomically via `save_calendar_state()` (same temp-file + `os.replace` pattern as `jw.save_state()`, but a separate file/function since `jw.save_state()` is hardcoded to `jw.STATE_FILE`).
- The calendar tool also reads (not writes) `jw.CREDENTIALS_FILE` (same Jira Basic Auth) and `jw.CONFIG_FILE`'s `daily_cap_hours`, and reads/writes `jw.STATE_FILE`'s `daily_submitted_minutes` and `project_keys_cache` — see Architecture stage 6 below.

## Architecture

The pipeline, driven by `main()`, runs in distinct stages — most bugs will involve getting the hand-off between these stages wrong:

1. **Transcript discovery**: For each `enabled_projects` entry, `transcript_dir_for()` derives the Claude Code project transcript directory from `~/.claude/projects/<slugified-path>` and globs its `*.jsonl` session files.

2. **Bootstrapping**: A project seen for the first time has all its *existing* sessions marked as already-logged up to their current tail (no retroactive billing), then flagged live in `state["bootstrapped_projects"]`. Only sessions appearing *after* a project goes live are logged from their true start. This bootstrap-vs-live distinction is the main source of subtlety in `main()` — don't "simplify" it without preserving the no-retroactive-logging guarantee.

3. **Event extraction** (`load_session_events`): Parses each JSONL transcript line into `{ts, branch (gitBranch field), role, text}`. Text is pulled from user turns (plain string content) or assistant turns (joined text blocks).

4. **Ticket inference** (`ticket_from_branch`): Jira ticket key (`TICKET_RE`) is extracted from the session's recorded git branch name — this is the *only* signal tying a session to a ticket. The extracted project prefix is cross-checked against `known_keys` (real Jira project keys, fetched by `get_known_project_keys()` and cached 24h in `state["project_keys_cache"]`) when available. Sessions with no ticket key, or a key whose project prefix isn't real, contribute to `unmatched_minutes` and are never billed.

5. **Interval processing** (`process_session`): Converts consecutive event timestamps into billable minutes. Each gap is capped at `gap_cap_minutes` (caller idle time isn't billed beyond that) and a session's first interval gets a `min_floor_minutes` floor. Resumable: picks up from `state["sessions"][key]` (`logged_until`) so repeat runs don't double-bill, using the last-seen event as an anchor for the next gap calculation. Day bucketing uses `local_date()` (local timezone), matching the local-time conversion `submit_worklog` does when posting to Jira.

6. **Aggregation & capping**: `aggregate()` sums minutes per `(ticket, day)` across all sessions/projects. `apply_daily_cap()` scales down (or fully zeroes) a day's entries so that new-run minutes plus `state["daily_submitted_minutes"][day]` (minutes already successfully submitted in *earlier* runs that same day) stay within `daily_cap_hours`.

7. **Comment generation** (`build_comment` → `summarize_activity`): Prompt text is redacted (`redact_sensitive()` — strips Bearer tokens, JWTs, key/secret/password assignments, emails, AWS keys) before being used. Shells out to `claude -p --restricted --bare --model claude-sonnet-5`, authenticated via `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` (from `credentials.json`'s `anthropic_api_key`/`anthropic_base_url`, pointing at this org's internal AI gateway) so it works unattended from cron without any interactive `/login`, to turn it into a one-sentence status update suitable for a Jira comment. Retries once, then falls back to a truncated raw-prompt list if the CLI call fails, returns empty output, or times out (`SUMMARY_TIMEOUT_SECONDS`).

8. **Submission** (`submit_worklog`): POSTs to `{base_url}/rest/api/3/issue/{ticket}/worklog` via stdlib `urllib`, Basic Auth from `credentials.json`. Returns the Jira worklog id on success (parsed from the response body) or `None` on failure.

9. **State commit**: State is committed incrementally, not just once at the end. Each `(ticket, day)` entry's sessions have a countdown (`remaining_per_session`) of how many entries they still appear in; once it hits zero, that session's `state["sessions"][key]` watermark is written and `save_state()` is called immediately — a session only advances if none of its entries failed (`blocked_sessions`). This shrinks the crash-recovery window from "the whole run" to "whatever's still unresolved" at the moment of a crash. `state["daily_submitted_minutes"]` is updated the moment each submission succeeds, for the next run's cap check.

## Key invariants to preserve

- State advancement is per-`session_key` (`"<project>:<session_id>"`), not global — a failure on one ticket/session must not block state progress on unrelated sessions.
- Minutes are only ever billed when a git branch resolves to a *real* ticket key; there's no fallback ticket.
- `save_state()` must stay atomic (temp file + rename) — don't reintroduce a direct `write_text()` on `STATE_FILE`.
- `--dry-run` must never call `submit_worklog`/`void_worklog`/`save_state` — it should be safe to run repeatedly with zero side effects (the real project-key fetch is the one exception, since it's a read-only GET).
- The script must remain dependency-free (stdlib only) since it runs from cron without a virtualenv.

## Calendar add-on architecture (`jira_calendar_worklog.py`)

1. **Auth**: `refresh_access_token()` exchanges `google_credentials.json`'s `refresh_token` for a fresh access token, unconditionally, once per run (stdlib `urllib` POST to `https://oauth2.googleapis.com/token` — no pip OAuth library).
2. **Fetch**: `fetch_calendar_events()` pulls events from `calendar_config.json`'s `calendar_id` over a *retrospective* window (`calendar_window(days_back)` → `[now - days_back, now]` — this bills past meetings like a worklog, not a calendar reminder), with `singleEvents=true` so recurring meetings expand into per-instance events with unique ids.
3. **Ticket resolution** (`resolve_ticket_for_event`), three-tier, in order:
   - **Tier 1**: `jw.ticket_from_branch()` (reused as-is — it's just regex + prefix-validate on a string) against the concatenated `summary`+`description`+`location`, validated against `known_keys` from `jw.get_known_project_keys()` (the full real Jira project-key set, same 24h cache in `state["project_keys_cache"]` as `jira_worklog.py` — shared cache, same Jira instance).
   - **Tier 2** (only if Tier 1 found nothing, and `sprint_reporter_account_id` is configured): `extract_sprint_number()` pulls a sprint number from the title, `resolve_sprint_ticket()` JQL-searches (`jql_search()` helper, hitting `/rest/api/3/search/jql` — Atlassian removed the old `/rest/api/3/search` GET endpoint) `project in (<calendar_config's jira_project_keys>) AND summary ~ "Sprint #<N>" AND reporter = "<accountId>"`. Exactly one hit → cached in `calendar_state.json["sprint_ticket_cache"]` and used. Zero or multiple hits → **never cached**, logged as ambiguous, skipped — so a later run can still resolve it once a real/unique ticket exists.
   - **Tier 3** (only if Tier 1 and Tier 2 both found nothing, and `board_id` is also configured alongside `sprint_reporter_account_id`): for a meeting whose title never mentions a sprint number at all (e.g. a recurring standup), the sprint is taken from the *board*, matched by the meeting's own start time — not by whichever sprint is active when the script runs (that older behaviour billed the tail of a just-ended sprint's meetings to the new sprint's ticket). `fetch_board_sprints()` pulls every active + closed sprint once per run (`GET /rest/agile/1.0/board/<board_id>/sprint?state=active,closed`, paginated); `sprint_number_for_time()` picks the one sprint whose interval (`sprint_interval()`: closed → `startDate` to `completeDate`, falling back to `endDate`; active → `startDate` onward, open-ended even past its planned end) contains the meeting's scheduled start, and extracts its number from the sprint name (`"Sprint 50"` → `"50"`) with the same `extract_sprint_number()`/regex as Tier 2. **Same-day gap rule:** this team closes a sprint one day and clicks "Start sprint" on the next only later on its first day, so a morning meeting on a sprint's first day falls in no sprint; when nothing covers the meeting, a sprint that starts later on the *same local day* claims it (exactly one such sprint). Any longer gap, overlapping sprints (>1 match), or a time before the board's history → no number, skipped. That number then feeds the *same* `resolve_sprint_ticket()` call as Tier 2 — identical JQL, cache, and ambiguous-skip handling. This is a deliberate, broad catch-all: once enabled, essentially any meeting that isn't declined/all-day and doesn't otherwise resolve bills to its sprint's ticket — a conscious trade-off the user chose over leaving those meetings unbilled.
   - No match at any tier → skipped, logged as `"unmatched"` (same "no fallback ticket" principle as the main tool).
4. **Dedup & RSVP filtering**: events are keyed by their Calendar API per-instance `id` in `calendar_state.json["logged_events"]`; already-logged ids are skipped first. `is_declined_by_self()` then skips any event where your own attendee `responseStatus` is `"declined"` — the only RSVP state that excludes billing (tentative/no-response/no-attendee-data all still bill). This is a proxy for "chose not to attend" an optional recurring meeting; there's no real join/leave signal available without Workspace-admin Meet attendance reports.
5. **Overlap resolution** (`resolve_overlaps`): runs on every surviving (deduped, non-declined, non-all-day) event *before* ticket resolution, so two overlapping meetings never double-bill the same wall-clock time. Priority is RSVP-based via `event_priority_tier()` — an `"accepted"` event always outranks everything else that survived the decline filter (tentative/no-response/no-attendee-data, which share a tier); tied priority breaks by earlier start. Events are processed in `(tier, start)` order against a merged `claimed` interval list: `_largest_remainder()` clips each event to whatever's left of its own interval after subtracting already-claimed time, keeping **only the single largest contiguous leftover piece** — an event is never fragmented into two separate billable pieces (e.g. a higher-priority meeting nested in the middle of a longer lower-priority block splits it into a before/after remainder; only the bigger half bills, the smaller is dropped silently), same "don't guess/split" spirit as the Tier-2/3 ambiguous-match handling below. Events already logged in earlier runs (skipped by dedup) still seed `claimed` via `resolve_overlaps(..., already_claimed)` — using the `billed_start`/`billed_minutes` recorded in `calendar_state.json["logged_events"]` (`logged_billed_interval()`; older records without those fields fall back to the full scheduled interval) — and always win regardless of RSVP tier, since their Jira worklogs already exist and are never edited; they're never re-emitted. A lower-priority event fully covered by a higher one bills **0** minutes but is still carried through to ticket resolution and entry-building, so it still gets logged/dedup-tracked (mirrors how a cap-dropped entry is handled) rather than silently vanishing.
6. **Event → entry**: `event_to_entry()` builds one entry per meeting using each event's overlap-clipped `(billable_start, billable_minutes)` from step 5, not its raw scheduled interval — unlike the main tool, **these are never merged by `(ticket, day)`**; `jw.aggregate()` is deliberately not called here, so each meeting becomes its own Jira worklog at its own actual (possibly clipped) start time with its own title as the comment. A partially-clipped (but not fully zeroed) event gets a `"shortened: overlapped with a higher-priority meeting"` note appended to its comment. Each entry carries `"sessions": {event_id}` directly (a one-element set) so the per-event dedup bookkeeping below has what it needs without an aggregation step.
7. **Shared daily cap**: reads `daily_cap_hours` from `jw.CONFIG_FILE` (not duplicated into `calendar_config.json` — there's one real daily ceiling). Runs under the *same* `jw.LOCK_FILE` as `jira_worklog.py` (acquired in `__main__`, same as the main script) before touching `jw.STATE_FILE`, so the two scripts never race on `state["daily_submitted_minutes"]`. `jw.apply_daily_cap()` is reused unchanged — it only groups entries by `"day"`, not by ticket, so capping still works correctly across several same-day meetings even though they're now separate entries instead of one merged one. An entry already zeroed by overlap resolution stays zero regardless of cap scaling, so the two mechanisms never conflict.
8. **Submit & commit**: Comments are built directly from the event — `meeting_comment()` is just the event's title, no attendee list, no AI summarization (unlike the main tool's `jw.build_comment()`, which this script does *not* use). `jw.submit_worklog()` is reused verbatim. A zero-minute entry is logged with `status=dropped_overlap` or `status=dropped_cap` depending on `entry["dropped_reason"]` (set in step 5 for the overlap case, absent/defaulted to `"cap"` otherwise), distinguishing the two causes in `activity.log`. On each resolved `(ticket, day)` entry, once every event id backing it has been accounted for, successful/dropped entries mark their event ids into `calendar_state.json["logged_events"]`; failed entries don't, so they retry next run (mirrors `blocked_sessions` in the main tool). `jw.save_state(state)` + `save_calendar_state(cal_state)` are called together, incrementally, same spirit as the main tool's per-session incremental saves.

## Key invariants to preserve (calendar add-on)

- Same "no fallback ticket" invariant as the main tool — an ambiguous Tier-2/Tier-3 lookup (0 or >1 JQL hits, or 0 or >1 board sprints covering the meeting's start after the same-day gap rule) must skip and log, never guess/split/auto-pick.
- Tier 3 (board-sprint fallback) is opt-in — only engages when `board_id` is set in `calendar_config.json` (alongside `sprint_reporter_account_id`); a config without `board_id` sees no behavior change.
- Tier 3 matches a meeting to the board sprint running *at the meeting's start time*, never to "whatever is active now" — a run that spans a sprint boundary (or retries an old meeting) must still bill each meeting to its own sprint's ticket.
- Overlap resolution (`resolve_overlaps`) never bills the same wall-clock slice twice and never fragments one event into two billable pieces — same "don't guess/split" principle as Tier-2/3. Priority is RSVP-based (`accepted` > everything else that survives the decline filter), not start-time-based; only ties break by start time.
- Dedup is by Calendar event **instance** id, never by a time range — don't reintroduce time-window-based dedup, which would double- or mis-bill recurring events.
- `--dry-run` must never call `jw.submit_worklog`/`jw.void_worklog`/`jw.save_state`/`save_calendar_state` — the unconditional access-token refresh and calendar/Jira-search GETs are the accepted exceptions (read-only, same category as the main tool's project-key fetch under `--dry-run`).
- Must acquire `jw.LOCK_FILE` (same lock as `jira_worklog.py`) before touching `jw.STATE_FILE`, since the two scripts share `daily_submitted_minutes`.
- Stdlib only — no `google-api-python-client`/`google-auth` or other pip packages, matching the main tool's cron-without-a-virtualenv constraint.

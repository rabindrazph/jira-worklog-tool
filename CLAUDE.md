# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, stdlib-only Python script (`jira_worklog.py`) that scans local Claude Code session transcripts, infers active engineering time per Jira ticket, and auto-submits Jira worklogs. Intended to run unattended from cron. See `README.md` for install/setup instructions and `FUTURE_ENHANCEMENTS.md` for known gaps.

## Commands

Run directly (no install/build step, no dependencies beyond the Python stdlib):

```bash
python3 jira_worklog.py                        # normal run
python3 jira_worklog.py --dry-run --verbose     # preview without touching Jira or state
python3 jira_worklog.py --project claims-manager  # restrict to one project (repeatable flag)
python3 jira_worklog.py --void TICKET WORKLOG_ID  # delete a wrongly-submitted worklog
python3 -m unittest test_jira_worklog -v        # run the test suite
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

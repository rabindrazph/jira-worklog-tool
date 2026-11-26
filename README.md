# jira-worklog-tool

Scans your local Claude Code session transcripts, estimates how much active engineering time you spent per Jira ticket (inferred from the git branch you were on), and auto-submits Jira worklogs. Runs unattended from cron. Pure Python stdlib — no `pip install` needed.

See `FUTURE_ENHANCEMENTS.md` for known limitations, and `CLAUDE.md` if you're working on the script itself.

## Requirements

- **Linux or macOS.** The locking mechanism uses `fcntl`, which isn't available on Windows.
- **Python 3.10+** (the script uses `X | None` type hints).
- **The `claude` CLI, installed and logged in.** It's shelled out to (`claude -p --restricted`) to turn your raw prompts into a one-sentence worklog comment.
- **A Jira Cloud instance** you can create an API token for.

## Install on a new machine

### 1. Put the repo in the right place

Nothing to edit in the script itself — clone it wherever you like, no path constants to touch:

- `BASE_DIR` is derived automatically from wherever `jira_worklog.py` lives: its parent folder. That's the folder that holds all your Jira-ticket project checkouts (the tool derives its Claude Code transcript lookups from `BASE_DIR/<project-name>/`), so the tool itself must live directly under it, in its own subfolder.
- `CONFIG_DIR` (where credentials/config/state live — see step 3) defaults to `~/.config/jira-worklog-tool`, separate from the repo. No need to change it.

The rest of this guide uses `<BASE_DIR>` as a placeholder for whichever parent folder you pick.

```bash
mkdir -p <BASE_DIR>
cd <BASE_DIR>
git clone <this-repo-url> jira-worklog-tool
# or: copy the jira-worklog-tool folder here directly
```

The `enabled_projects` names in your config (step 3) must match directory names directly under `BASE_DIR`.

### 2. Get a Jira API token

Create one at https://id.atlassian.com/manage-profile/security/api-tokens. You'll also need the email address tied to your Atlassian account and your Jira Cloud base URL (e.g. `https://yourcompany.atlassian.net`).

### 3. Create your config files

These live outside the repo, at `<CONFIG_DIR>`, and are **never committed to git**.

```bash
mkdir -p <CONFIG_DIR>
```

`<CONFIG_DIR>/credentials.json`:
```json
{
  "base_url": "https://yourcompany.atlassian.net",
  "email": "you@yourcompany.com",
  "api_token": "paste-your-jira-api-token-here"
}
```

`<CONFIG_DIR>/config.json`:
```json
{
  "enabled_projects": ["your-project-folder-name"],
  "gap_cap_minutes": 10,
  "min_floor_minutes": 5,
  "daily_cap_hours": 7
}
```

- `enabled_projects` — folder names under `BASE_DIR` that you want scanned. Each must have a matching Claude Code transcript directory under `~/.claude/projects/` (created automatically once you've run Claude Code inside that project folder at least once).
- `gap_cap_minutes` — longest idle gap between prompts that still counts as active work.
- `min_floor_minutes` — minimum minutes credited for a session's first prompt.
- `daily_cap_hours` — safety ceiling; total logged time per day gets scaled down if it would exceed this (across all runs that day, not just one).

Lock these files down since `credentials.json` holds your API token:
```bash
chmod 700 <CONFIG_DIR>
chmod 600 <CONFIG_DIR>/credentials.json
```

### 4. Test it before trusting it

```bash
cd <BASE_DIR>/jira-worklog-tool
python3 jira_worklog.py --dry-run --verbose
```

This prints exactly what it *would* submit — ticket, minutes, generated comment — without calling Jira or touching any state file. Run it a few times while you tune `gap_cap_minutes`/`daily_cap_hours` to taste; dry-run never has side effects.

The first real (non-dry-run) run on a project **bootstraps** it: any sessions that already existed get marked as already-seen with no retroactive billing, and only sessions created after that point get logged. So don't worry about it suddenly billing weeks of old history on first run.

### 5. Run the test suite (optional, but recommended before changing the script)

```bash
python3 -m unittest test_jira_worklog -v
```

### 6. Schedule it with cron

```bash
crontab -e
```

Add a line like:
```
30 17 * * * /usr/bin/python3 <BASE_DIR>/jira-worklog-tool/jira_worklog.py
```

Use absolute paths (no `~`) and adjust the time and `python3` path (`which python3`) for your machine. The script creates its own dated log files under `logs/<YYYY-MM-DD>.log` and per-ticket logs under `logs/<TICKET>/activity.log`, so you don't need to redirect cron's output anywhere yourself (though doing so as a backstop doesn't hurt).

It's safe to schedule this more than once a day — a lockfile prevents overlapping runs, and the daily cap is now enforced cumulatively across runs, not per-invocation.

## Usage reference

```bash
python3 jira_worklog.py                            # normal run
python3 jira_worklog.py --dry-run                   # preview, no side effects
python3 jira_worklog.py --dry-run --verbose         # preview + per-session detail
python3 jira_worklog.py --project your-project      # only process one project (repeatable)
python3 jira_worklog.py --void TICKET WORKLOG_ID    # delete a wrongly-submitted worklog
```

The worklog id for `--void` comes from `logs/<TICKET>/activity.log`, which records one every time a worklog is actually submitted.

## Troubleshooting

- **"ERROR: credentials.json missing required key(s): ..."** — check `<CONFIG_DIR>/credentials.json` has `base_url`, `email`, and `api_token`.
- **"WARN: no transcript dir for project '<name>'"** — you haven't run Claude Code inside `<BASE_DIR>/<name>/` yet, so there's no transcript history to scan.
- **"Another run is already in progress; exiting."** — a previous invocation (likely from cron) is still running; this is expected and safe, not an error.
- **Nothing gets billed** — check the branch name you were working on actually contains a real Jira ticket key (e.g. `feature/ZC-123-thing`), and that `ZC` is a real project key in your Jira instance (unknown-looking project prefixes are deliberately ignored).

## Google Calendar worklog tool (optional add-on)

`jira_calendar_worklog.py` is a sibling script: it bills Jira worklogs for your meeting time by reading Google Calendar, instead of Claude Code session transcripts. It reuses this tool's Jira credentials/submission code and shares the same `daily_cap_hours` so the two tools together don't exceed your intended daily ceiling. Same requirements as above (Linux/macOS, Python 3.10+, Jira API token), plus a Google account and a few minutes of one-time setup. Still pure stdlib — no `pip install`.

### 1. Create a Google OAuth client (one-time, in Google Cloud Console)

1. Create or pick a GCP project at https://console.cloud.google.com/.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** → configure it (External is fine; Testing mode is fine for personal use and avoids Google's verification process).
4. Under the consent screen's "Test users", add your own Google account — required while the app is in Testing mode.
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID** → Application type **Desktop app**. Note the `client_id` and `client_secret`.

### 2. Run the one-time auth setup

```bash
cd <BASE_DIR>/jira-worklog-tool
python3 jira_calendar_worklog.py --setup-google-auth
```

It'll ask for the `client_id`/`client_secret` from step 1, print a consent URL to open in a browser, and ask you to paste back the redirected URL (or just the `code=` value) after you approve access. On success it writes `<CONFIG_DIR>/google_credentials.json` (chmod 600) — a refresh token that doesn't expire unless you revoke access, so this is a true one-time step.

If it ever says Google returned no refresh token, you've likely already consented once before — revoke access at https://myaccount.google.com/permissions for this app and re-run.

### 3. Find your sprint-reporter accountId (optional, only needed for Tier 2)

Meetings with a direct Jira ticket key or `/browse/TICKET-123` link anywhere in the title/description/location resolve automatically (Tier 1) — most meetings should hit this. For recurring sprint ceremonies named like `Sprint #50` with no direct link, the tool falls back to a Jira search (Tier 2) for the matching sprint ticket, filtered to a specific reporter. Jira Cloud's search needs an **accountId**, not a display name:

```bash
python3 jira_calendar_worklog.py --find-account "Jasmeen"
```

This looks up `/rest/api/3/user/search` using the Jira credentials already in `<CONFIG_DIR>/credentials.json` and prints each match's `accountId`, name, and email. If it prints "No Jira users found" for every query (even a single common letter), your Jira instance restricts that endpoint to admins — ask a Jira admin for the accountId instead, or have them grant you the "Browse users" global permission.

Copy the `accountId` of the right person into `sprint_reporter_account_id` below.

### 4. Create `calendar_config.json`

`<CONFIG_DIR>/calendar_config.json`:
```json
{
  "calendar_id": "primary",
  "jira_project_keys": ["ZC"],
  "sprint_reporter_account_id": "paste-the-accountId-from-step-3",
  "days_back": 1
}
```

- `calendar_id` — which Google Calendar to read; `"primary"` is your main calendar.
- `jira_project_keys` — **real Jira project keys** (not folder names like `enabled_projects` in `config.json`) — used both to validate Tier-1 matches and to narrow the Tier-2 JQL search.
- `sprint_reporter_account_id` — omit entirely to disable Tier 2 (only direct ticket links/keys will be billed).
- `days_back` — default lookback window for a run; override per-invocation with `--days-back`.

This file doesn't need `chmod 600` (no secrets in it), but `google_credentials.json` does — it's created with that permission automatically.

### 5. Test it before trusting it

```bash
python3 jira_calendar_worklog.py --dry-run --verbose --days-back 7
python3 -m unittest test_calendar_worklog -v
```

### 6. Schedule it with cron

```
0 20 * * * /usr/bin/python3 <BASE_DIR>/jira-worklog-tool/jira_calendar_worklog.py
```

Run it in the evening, after the day's meetings are over — it bills *past* meeting time, like a worklog, not a calendar reminder. It shares `run.lock` with `jira_worklog.py`, so the two never race on the same state file even if a run overlaps.

### Usage reference

```bash
python3 jira_calendar_worklog.py                               # normal run
python3 jira_calendar_worklog.py --dry-run --verbose            # preview, no side effects
python3 jira_calendar_worklog.py --days-back 3                  # scan further back than the default
python3 jira_calendar_worklog.py --void TICKET WORKLOG_ID       # delete a wrongly-submitted worklog
python3 jira_calendar_worklog.py --setup-google-auth           # (re-)run the one-time OAuth setup
```

### Troubleshooting

- **"ERROR: google_credentials.json missing required key(s)..."** — run `--setup-google-auth` first.
- **No refresh_token returned during setup** — you already consented once; revoke access at https://myaccount.google.com/permissions and re-run.
- **Sprint lookup always "ambiguous" or "found 0"** — check `jira_project_keys` actually covers the project the sprint ticket lives in, that `sprint_reporter_account_id` is an accountId (not a name — see step 3), and that the sprint ticket's summary really contains `Sprint #<N>` matching the meeting title's number.
- **A meeting you expected to bill shows up as "unmatched" in the log** — it has no ticket key/link anywhere in its title/description/location, and either didn't match the sprint-number pattern or `sprint_reporter_account_id` isn't configured. This is intentional: there's no fallback ticket, so ambiguous or unresolvable meetings are always skipped rather than guessed at.
- **An optional meeting you skipped still gets billed** — decline the invite in Google Calendar for that occurrence; only an explicit decline excludes a meeting (`declined_skipped` in the run summary). Just not joining one you accepted (or never responded to) still bills — there's no actual attendance signal available, only your RSVP.
- **Two overlapping meetings don't both bill their full duration** — this is intentional: an `"accepted"` meeting always wins the overlapping time over a tentative/no-response one; same-priority ties go to whichever started earlier. The loser either gets clipped (comment gets a "shortened: overlapped..." note) or, if fully covered, drops to 0 minutes and shows up as `overlap_dropped` in the run summary / `status=dropped_overlap` in `activity.log`.
- **Known v1 limitations** — see `FUTURE_ENHANCEMENTS.md` (declined/tentative meetings still bill if matched even when you didn't actually attend, no automated accountId discovery on Jira instances that restrict user search).

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

Two path constants near the top of `jira_worklog.py` control where everything lives:

- `BASE_DIR` — the parent folder that holds all your Jira-ticket project checkouts (the tool derives its Claude Code transcript lookups from `BASE_DIR/<project-name>/`). The tool itself must also live directly under `BASE_DIR`, in a subfolder.
- `CONFIG_DIR` — where credentials/config/state live (see step 3), separate from the repo.

Check what those two constants are currently set to before you start, and either use those exact paths, or edit the constants to point wherever you'd like instead. The rest of this guide uses `<BASE_DIR>` and `<CONFIG_DIR>` as placeholders for whatever you land on.

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

# Future Enhancements

Gaps and ideas found while reading `jira_worklog.py`. Grouped by whether they're correctness/reliability risks or just missing conveniences.

## Correctness & reliability gaps

1. ~~**No lockfile against concurrent runs.**~~ **Fixed** — `acquire_lock()` takes a non-blocking `fcntl.flock()` on `CONFIG_DIR/run.lock` for the duration of the run. If another run already holds it, the new invocation logs a message and exits 0 immediately. The OS releases the lock automatically if the holding process dies, so there's no stale-lock cleanup needed.

2. ~~**State is only saved once, at the very end of `main()`.**~~ **Fixed** — state is now committed incrementally: as soon as every entry a given session contributes to has been resolved (submitted, failed, or dropped by the cap), that session's watermark is written and `save_state()` is called immediately, rather than waiting for the whole run to finish. `save_state()` itself now writes atomically (temp file + `os.replace`) so a crash mid-write can't corrupt `state.json`. This shrinks the at-risk window from "the whole run" to "whatever's still unresolved."

3. ~~**Day-bucketing uses UTC date, but submission uses local time.**~~ **Fixed** — added `local_date()` which does `ts.astimezone().date().isoformat()`, used everywhere `day` is derived in `process_session`. Now matches the local-time conversion already done in `submit_worklog`.

4. ~~**`apply_daily_cap` only sees the current run's minutes.**~~ **Fixed** — `state["daily_submitted_minutes"]` now tracks cumulative minutes actually submitted per day across runs, updated immediately after each successful submission. `apply_daily_cap()` takes this as `already_submitted` and scales (or fully drops) new entries based on remaining room under `daily_cap_hours`, so multiple cron runs in a day can no longer exceed the cap in aggregate. Entries older than `DAILY_TOTALS_RETENTION_DAYS` (14) are pruned each run so this doesn't grow unbounded.

5. ~~**No validation of `config.json`/`credentials.json` contents.**~~ **Fixed** — `validate_credentials()`/`validate_config()` now check required keys and basic types up front and exit with a clear `ERROR:` message (e.g. "missing required key(s): email, api_token") instead of a raw `KeyError`/`TypeError` traceback.

6. ~~**Ticket regex has no cross-check against real Jira project keys.**~~ **Fixed** — `get_known_project_keys()` fetches the real project key list from Jira (`GET /rest/api/3/project`), caches it in `state["project_keys_cache"]` for 24h, and `ticket_from_branch()` now rejects any regex match whose project prefix isn't in that set (treated the same as "no ticket" — goes to unmatched minutes). Falls back to no validation if Jira can't be reached or returns an empty list, so a transient API issue doesn't wrongly blank out every session.

## Missing features / usability

7. ~~**No `--dry-run` flag.**~~ **Fixed** — `--dry-run` computes and prints what would be submitted (ticket, minutes, generated comment) without calling Jira or saving state, for safely tuning `gap_cap_minutes`/`daily_cap_hours` or debugging a new project's branch-naming conventions.

8. ~~**No other CLI flags at all.**~~ **Fixed** — added `--project NAME` (repeatable, filters `enabled_projects`), `--verbose` (per-session event/interval counts), and `--void TICKET WORKLOG_ID` (see #13). `--since <date>` is still not implemented (there's not yet a clear need, since `state.json` already tracks per-session resume points).

9. ~~**`LOG_DIR` is created but never written to.**~~ **Fixed** — `log()` now appends every line to `logs/<YYYY-MM-DD>.log` in addition to stdout. No rotation/cleanup of old daily log files yet, but at least it's no longer a single unbounded file.

10. ~~**No local ledger of what was actually submitted.**~~ **Fixed** — `log_ticket()` appends `logs/<TICKET>/activity.log` lines (day, minutes, status, Jira worklog id, comment) on every real submission attempt. `submit_worklog()` now parses the `id` out of Jira's POST response and returns it (or `None` on failure), so each ledger line can be cross-referenced directly against the specific worklog entry in Jira.

11. ~~**No redaction before sending prompt text to the summarizer.**~~ **Fixed** — `redact_sensitive()` strips common credential/secret shapes (Bearer tokens, JWTs, `key: value`-style secrets/passwords, emails, AWS access keys) from prompt text before it's used to build the summarizer instruction or the raw-notes fallback. Scoped intentionally to credential-like patterns, not general PII — see the function's docstring for what it doesn't cover.

12. ~~**No automated tests.**~~ **Fixed** — `test_jira_worklog.py` (stdlib `unittest`, 23 tests) covers `ticket_from_branch` (incl. project-key validation), `local_date`, `process_session` (min-floor seeding, gap capping, resume/anchor semantics, unmatched tickets), `aggregate`, `apply_daily_cap` (incl. cross-run carryover), `round_minutes`, and `redact_sensitive`. Run with `python3 -m unittest test_jira_worklog -v`. Network/subprocess-calling functions (`submit_worklog`, `summarize_activity`, Jira project-key fetch) are intentionally left untested here.

13. ~~**No way to correct or void an already-submitted worklog.**~~ **Fixed** — `python3 jira_worklog.py --void TICKET WORKLOG_ID` calls Jira's `DELETE /rest/api/3/issue/{ticket}/worklog/{id}` and records the void in that ticket's `activity.log`. The worklog id needed comes straight from the ledger added in #10.

14. **Credentials stored in plaintext JSON** (`CONFIG_DIR/credentials.json`) rather than an OS keychain/secret store. Not fixed — doing this properly means either a non-stdlib dependency (e.g. `keyring`) or OS-specific tooling (e.g. shelling out to `secret-tool`/`gnome-keyring`), which conflicts with this script's deliberate "stdlib only, cron-friendly" constraint. Fine for a personal/local tool; worth revisiting only if this ever needs to run on a shared machine.

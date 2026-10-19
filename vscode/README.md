# Lean Brain for VS Code

A read-mostly dashboard over the lean-agent brain database. It reads the SQLite file and never writes it. The only changes it makes go through `bin/brain` (close, reopen, and closing a question as answered).

## What it shows

- Status bar: `brain: N real, M flaky` for open items of the current workspace project (matched to the folder name or the git main repo name, case-insensitive), or all projects when none match. While a test job runs it adds the suite name and elapsed time. Click opens the dashboard.
- Activity bar, "Open items": grouped by project, then kind. Click an item for all its fields. Right click to close as fixed, close as not a bug, reopen, or copy the ref.
- Activity bar, "Queue": queued and running jobs with elapsed time, and the last 10 finished.
- Dashboard (`Lean Brain: Open dashboard`): waiting on you (question, blocker, pending style kinds), headline numbers, cycles, slowest suites with sparklines, flakiest tests, machine load, and hygiene (open items not seen for 14 days, with a button that copies a `brain list` command).
- `Lean Brain: Show resume digest` runs `brain resume` into an output channel.
- Claude Code: the dashboard opens Claude Code in an editor tab. Pending, risk and question items have Send to Claude, which starts a new session on them.
- Answers: an open question's page has an answer box. Brain stores the session that filed each item (the session-env hook sets it). Sending an answer closes the question as answered, then: if this extension's own terminal runs that session, the answer is typed in; if the session has ended, `claude --resume` starts it again in its folder with the answer; if it may still be open in another window, you pick between its next prompt (the session-env hook hands it over) and a fork (`--fork-session`). It never resumes a session that is still open, so one conversation never runs twice.

It refreshes 2 seconds after brain.db (or brain.db-wal) changes, and on `Lean Brain: Refresh`. Each refresh reads the file fresh into sql.js. If the read fails it retries once, then keeps the last good data and says so. sql.js reads only the main file, so it expects brain's default rollback journal, not WAL mode.

## Build and install

```
cd vscode
npm ci
npm run package
code --install-extension lean-brain-0.5.0.vsix
```

The fonts in `media/fonts` (Plus Jakarta Sans, IBM Plex Mono) are licensed under the SIL Open Font License 1.1; each ships with its license text beside it.

`npm run build` bundles to `out/`. `npm test` runs the vitest tests. No telemetry and no network calls.

## Settings

- `leanBrain.dbPath`: path to brain.db. Empty uses `LEAN_BRAIN_DB`, then `~/.local/share/lean/brain.db`.
- `leanBrain.brainPath`: path to `bin/brain`. Empty uses `brain` on PATH, then the newest lean-agent plugin in `~/.claude/plugins/cache/lean-agent/lean-agent/*/bin/brain`.
- `leanBrain.claudeCommand`: path to the claude executable. Empty uses the absolute PATH entries, then `~/.local/bin`, `~/.claude/local`, `/opt/homebrew/bin` and `/usr/local/bin`, since an editor started from the Dock does not get the shell's PATH.

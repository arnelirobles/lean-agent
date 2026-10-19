# Lean Brain for VS Code

A read-mostly dashboard over the lean-agent brain database. It reads the SQLite file and never writes it. The only changes it makes go through `bin/brain` (close and reopen), after a confirmation.

## What it shows

- Status bar: `brain: N real, M flaky` for open items of the current workspace project (matched to the folder name or the git main repo name, case-insensitive), or all projects when none match. While a test job runs it adds the suite name and elapsed time. Click opens the dashboard.
- Activity bar, "Open items": grouped by project, then kind. Click an item for all its fields. Right click to close as fixed, close as not a bug, reopen, or copy the ref.
- Activity bar, "Queue": queued and running jobs with elapsed time, and the last 10 finished.
- Dashboard (`Lean Brain: Open dashboard`): waiting on you (question, blocker, pending style kinds), headline numbers, cycles, slowest suites with sparklines, flakiest tests, machine load, and hygiene (open items not seen for 14 days, with a button that copies a `brain list` command).
- `Lean Brain: Show resume digest` runs `brain resume` into an output channel.

It refreshes 2 seconds after brain.db (or brain.db-wal) changes, and on `Lean Brain: Refresh`. Each refresh reads the file fresh into sql.js. If the read fails it retries once, then keeps the last good data and says so. sql.js reads only the main file, so it expects brain's default rollback journal, not WAL mode.

## Build and install

```
cd vscode
npm ci
npm run package
code --install-extension lean-brain-0.1.0.vsix
```

`npm run build` bundles to `out/`. `npm test` runs the vitest tests. No telemetry and no network calls.

## Settings

- `leanBrain.dbPath`: path to brain.db. Empty uses `LEAN_BRAIN_DB`, then `~/.local/share/lean/brain.db`.
- `leanBrain.brainPath`: path to `bin/brain`. Empty uses `brain` on PATH, then the newest lean-agent plugin in `~/.claude/plugins/cache/lean-agent/lean-agent/*/bin/brain`.

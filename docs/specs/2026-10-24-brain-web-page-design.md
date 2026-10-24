# Brain web page

## Why

The lean brain VS Code extension makes VS Code use a lot of memory. The person running agents no longer reads code. They need one place that says what happened and what needs them: the test queue, decisions waiting, things to approve, usage, and what agents did, decided and created. A local web page does that without VS Code.

## Goals

One page, served from this machine, that shows:

1. **Needs you.** Open questions, blockers and pending items, newest first. Decisions to approve are the open questions: an agent asks and recommends, the user approves the recommendation or writes a different answer (see Approving a decision). Pending, risk and question items also have a button that starts a Claude session on them.
2. **Test queue.** Queued and running jobs, lanes, and the last result per job.
3. **What was created.** Pull requests, issues and releases that agents made, with where they live (GitHub or Azure DevOps), the repo, the current state (open, merged, closed) and the session that made them.
4. **Pending tickets.** Open GitHub issues and Azure DevOps work items assigned to the user or waiting on them.
5. **Worktrees.** Every git worktree on the machine across repos, with its branch and whether it has unpushed commits, uncommitted changes, or a merged pull request (safe to remove).
6. **Tested or not.** Per branch or pull request, whether a passing test-phase run exists and which one. A pull request merged with no passing run is flagged.
7. **Notices.** Heads-ups that today scroll past in a session (hook notes, side-agent warnings, "merged with checks pending"), kept as a feed the user dismisses.
8. **Usage.** What `lean-stats` already reports: tokens and cost per batch and per model.
9. **Decided.** Rulings and closed questions with their answers.

## Non-goals

- Reading or reviewing code on the page.
- Jira for now. It comes later through the `twg` CLI, so leave the ticket source pluggable.
- A team or hosted view. That is the team brain server (docs/specs/2026-10-20-team-brain-design.md). This page is one user on one machine.

## Anti-goals (a failure even if it works)

- **Storing credentials.** The page never stores a token, key or password. GitHub goes through `gh`, Azure DevOps through `az`, Jira later through `twg`, each with the login it already has. If a CLI is missing or logged out, that section says so and the rest of the page works.
- **A page any website can drive.** A local server that starts sessions can be triggered by any site open in the same browser. See Security.
- **A new memory hog.** If the server holds more memory than the extension did, it failed its purpose.
- **407 flaky items burying everything.** Flaky items are grouped into one count with a link, not listed one per row.

## How it runs

The user picks at setup, and can change it later:

- **Always on** from login (launchd on macOS, a systemd user unit on Linux).
- **With a session.** Started by the SessionStart hook if not already running, stopped when no session has been open for a while.
- **By hand.** `brain serve` starts it, Ctrl-C stops it.

The choice is a plugin `userConfig` option, so Claude Code asks for it at install.

The page is at `http://leanagent.localhost:<port>`. Chrome, Edge and Firefox send any `*.localhost` name to this machine with no setup (Safari is unverified, so the builder checks it); `http://127.0.0.1:<port>` works too and is printed when the server starts. The plugin never edits `/etc/hosts` or runs `sudo`: a hook cannot prompt for a password, and a plugin that changes system files would likely be held at the directory's review.

## Approving a decision

An open question shows the agent's recommendation with two controls: approve it, or write a different answer. Either closes the item with resolution `answered` and the answer as the note, the same as `brain close ID --resolution answered --note TEXT`. The session that asked gets the answer through `brain answers`, as it does today. The answer is stored in brain as text and never reaches a command line.

## Acting on an item

Each item that needs work has one action: start a Claude session on it. On macOS the session opens in a new Terminal window in the item's folder (`session_cwd`, else the repository, else the home folder with a note), with a prompt built from the item and passed as an argument, never spliced into a command string. On Windows it opens a new Windows Terminal tab (`wt.exe -d <folder> claude <prompt>`, as an argument list, never through `cmd /c`). On Linux, and whenever no terminal can be opened, the button copies the `claude` command instead. Questions get a recommendation and stay open for the user; other kinds get fixed and closed. Port the prompt rules from `vscode/src/claude.ts` (`askPrompt`, `workPrompt`, `canSend`, `clean`, `validSession`) so both surfaces say the same thing.

No other command runs from the page. There is no free text field that reaches a shell.

## Security

- Listen on 127.0.0.1 only, never 0.0.0.0.
- Reject any request whose `Host` header is not `leanagent.localhost`, `localhost`, `127.0.0.1` or `[::1]` (each with or without `:port`) (blocks DNS rebinding).
- Every request that changes something (starting a session, answering, dismissing) is a POST carrying a random key made at server start and embedded in the page, and the server checks `Origin`. No CORS headers, so another site cannot read the page or its key. The key is not stored on disk; after a restart an open tab gets "reload the page".
- The action takes an item id only. The server reads the item from brain and builds the prompt itself; nothing from the request reaches a command line.
- Brain text comes from agents and test output, so it is HTML-escaped on render and stripped of control characters before it goes into a prompt.

## Where the data comes from

- Brain database (`~/.local/share/lean/brain.db`, or `LEAN_BRAIN_DB`): items, queue, runs, rulings. Opened through `lib/brain/store.py`, so it migrates forward like the CLI. Writes are only answering a question and dismissing a notice.
- `lean-stats` database for usage.
- `git worktree list --porcelain` per known repository for worktrees. Known repositories are the ones brain has seen plus the user's configured roots.
- `gh` and `az` for current state of pull requests, issues and work items, cached for a short time so the page does not call them on every load.
- **What was created** needs a new record, kept from the day this ships (no backfill): when an agent creates a pull request, issue or release, it is written to brain at that moment (a hook on `gh pr create`, `gh issue create`, `gh release create` and the `az repos pr create` equivalents is one way). The page then asks `gh`/`az` for the current state.
- **Notices** need a new record too: hooks that print a note today also write it to brain as a notice.

## The VS Code extension

It stays. It shows a notice that it is deprecated, will be retired, and that the web page replaces it, with a button that opens the page. No new features go into it.

The team brain spec says the developer's dashboard is the VS Code dashboard; once this ships, that line points here instead.

## Constraints

- Python 3 standard library only, like the rest of `bin/` and `lib/`. No npm, no framework.
- Every new script has a `--self-test`, run in CI.
- macOS with stock bash 3.2, Linux and Windows. The server and page use only Python and `git`, `gh`, `az`, so they run on Windows even though the rest of `bin/` (bash, `flock`) does not yet. On Windows the interpreter may be `python` or `py`, not `python3`; `brain serve` and its self-test must not assume the name.
- Launch at login on Windows is a Startup-folder shortcut or a per-user scheduled task, written by `brain serve --install`, no admin rights.

## Calls made after the builder check

- Records of created items carry session, repo, branch and URL. Test jobs record the branch they ran on, so "tested or not" joins on repo and branch. Agents that create through MCP tools are not caught in v1; the Bash hook covers `gh` and `az`.
- "With a session" mode stops the server after 30 minutes with no open session in brain's `sessions` table.
- `brain serve --install` writes the launchd plist, systemd user unit or Windows startup entry and `brain serve --uninstall` removes it; the plugin does not write either.
- Worktrees come from repositories with a known path (`jobs.repo_dir`, `session_cwd`) plus roots listed in a `userConfig` option. The merged-PR check uses the cached `gh`/`az` state, refreshed in the background.
- Flaky items show as one count per project that expands in place, so the page stays one page.
- Usage imports `lib/leanstats.py` rather than shelling out to `lean-stats`.

## Left to the builder

- Port number.
- Page layout and refresh (plain HTML; polling or server-sent events).
- Cache times for `gh`/`az` calls.
- The exact brain schema for created items and notices.

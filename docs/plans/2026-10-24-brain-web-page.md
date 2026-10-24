# Brain Web Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web page, served by `brain serve`, that shows what needs the user, the test queue, what agents created, pending tickets, worktrees, test status, notices, usage and decisions, and can answer a question or start a Claude session on an item.

**Architecture:** A new package `lib/brain/web/` holds the data reads (one module per section), the HTML render and an `http.server` server bound to 127.0.0.1. Two new records feed it: created items (written by a PostToolUse hook on `gh`/`az` create commands) and notices (written by `hookkit.note`). `bin/brain serve` runs the server; `--install` sets up launch at login per OS. The VS Code extension gets a deprecation notice and a button that opens the page.

**Tech Stack:** Python 3 standard library only (`http.server`, `sqlite3`, `subprocess`, `secrets`, `html`), `git`, `gh`, `az`, bash 3.2 compatible shell, TypeScript for the extension notice.

**Spec:** `docs/specs/2026-10-24-brain-web-page-design.md`

## Global Constraints

- Python standard library only. No npm or framework in the page; the HTML is rendered server side.
- Store no credentials. `gh`, `az` (and `twg` later) are called as subprocesses with an argument list; a missing or logged-out CLI makes that section say so, never fail the page.
- Listen on 127.0.0.1 only. Default port 8770, `LEAN_SERVE_PORT` overrides. The page URL is `http://leanagent.localhost:<port>`; `http://127.0.0.1:<port>` is printed at start.
- Accept only `Host` values `leanagent.localhost`, `leanagent.local`, `127.0.0.1`, `localhost`, each with or without `:<port>`. Anything else gets 421.
- Every state change is a POST with the per-start key and an `Origin` whose host passes the same check. No CORS headers.
- Nothing from a request reaches a command line except an integer item id; prompts are built server side and passed as one argv element.
- Schema changes are additive (one new migration, `migrate_10`): the VS Code extension reads `brain.db` directly and must keep working.
- Every new script or `lib/` module with logic has a `--self-test`, found by `tests/run-all.sh`. `bash tests/run-all.sh` ends with `0 failed`.
- macOS (bash 3.2), Linux and Windows for the server. Never assume the interpreter is named `python3`; use `sys.executable`.
- `.claude-plugin/plugin.json` moves to `1.15.0` in the last task.
- Commit messages short, imperative, plain English, no trailers. No em dashes or arrow characters in any text.

## Review Focus

1. **`brain.db` locked by a running drainer.** Expect the page to render with what it could read and a one-line "brain busy, retrying" note, never a 500. Pinned in Task 6.
2. **Item text holding HTML or a `<script>`.** Expect it shown as text. Pinned in Task 6.
3. **A cross-site POST or a rebinding `Host`.** Expect 403 or 421 and nothing run. Pinned in Task 7.
4. **An item whose folder no longer exists.** Expect the session to open in the home folder with a line saying the folder is gone. Pinned in Task 4.
5. **The port already in use by another `brain serve`.** Expect the second start to print the running URL and exit 0, not a traceback. Pinned in Task 8.

---

### Task 1: Schema for created items, notices and job branches

**Files:**
- Modify: `lib/brain/store.py` (add `migrate_10`, append to `MIGRATIONS`)
- Modify: `lib/brain/queue.py` (record branch at `queue add`)
- Create: `lib/brain/records.py`
- Test: `lib/brain/selftest.py`

**Interfaces:**
- Produces tables:
  - `created (id INTEGER PK, kind TEXT CHECK kind IN ('pr','issue','release','workitem'), forge TEXT ('github'|'azure'), repo TEXT, branch TEXT, url TEXT UNIQUE, number INTEGER, title TEXT, session_id TEXT, cwd TEXT, created_at TEXT, state TEXT DEFAULT '', state_checked_at TEXT DEFAULT '')`
  - `notices (id INTEGER PK, hook TEXT, text TEXT, session_id TEXT, cwd TEXT, created_at TEXT, dismissed_at TEXT DEFAULT '')`
  - `jobs.branch TEXT NOT NULL DEFAULT ''`
- Produces in `lib/brain/records.py`:
  - `add_created(con, kind, forge, repo, branch, url, number, title, session_id, cwd) -> int` (insert or ignore on `url`, returns the row id)
  - `set_state(con, url, state) -> None`
  - `add_notice(con, hook, text, session_id, cwd) -> int` (same hook and text inside 10 minutes is not added twice)
  - `dismiss_notice(con, notice_id) -> bool`
- `brain queue add` stores `git -C <repo_dir> rev-parse --abbrev-ref HEAD` in `jobs.branch` ('' when git fails).

- [ ] **Step 1: Write failing checks** in `selftest.py`: a fresh DB reaches schema version 10; `add_created` twice with one URL returns the same id and one row; `add_notice` twice with the same hook and text inside 10 minutes leaves one row; `dismiss_notice` on a missing id returns False; `queue add` in a git repo on branch `feat/x` stores `feat/x`; a DB at version 9 with rows migrates and keeps them.
- [ ] **Step 2: Run** `python3 bin/brain --self-test`. Expected: FAIL on the version 10 check.
- [ ] **Step 3: Implement** `migrate_10`, `records.py` and the branch lookup (subprocess with an argument list, 3 s timeout).
- [ ] **Step 4: Run** `python3 bin/brain --self-test`. Expected: `brain self-test: ok`.
- [ ] **Step 5: Commit** `record created items, notices and the branch a test job ran on`

### Task 2: Record what agents create

**Files:**
- Create: `hooks/created-log.py`
- Modify: `hooks/hooks.json` (PostToolUse, matcher `Bash`, `"if": "Bash(gh *)"` and a second entry `"if": "Bash(az *)"`)
- Modify: `lib/brain/cli.py` (`brain created list [--open] [--limit N]`)

**Interfaces:**
- Consumes: `records.add_created`, `hookkit.segments`, `hookkit.gh_call`, `hookkit.tool_input`.
- Produces: `parse_created(words: list[str], output: str) -> dict | None` in `hooks/created-log.py`, returning `{kind, forge, repo, number, url}`.

Matches `gh pr create`, `gh issue create`, `gh release create`, `az repos pr create`, `az boards work-item create`. The URL is taken from the tool output (`gh` prints it on the last line; `az` prints JSON with `url`/`_links.web.href`). No URL in the output means nothing is recorded. Branch is `git rev-parse --abbrev-ref HEAD` in the segment's cwd. Session id and cwd come from the hook event. `LEAN_CREATED_LOG=off` turns it off.

- [ ] **Step 1: Write failing self-test cases**: `gh pr create --fill` with output ending `https://github.com/o/r/pull/12` gives `{kind: 'pr', forge: 'github', repo: 'o/r', number: 12}`; `gh issue create` with `.../issues/5` gives kind `issue`; `gh release create v1` with `.../releases/tag/v1` gives kind `release`, number None; `az repos pr create` with JSON `{"pullRequestId": 7, "repository": {"webUrl": "https://dev.azure.com/org/p/_git/r"}}` gives forge `azure`, number 7; `gh pr list` gives None; a failed create (no URL) gives None; `echo gh pr create` gives None.
- [ ] **Step 2: Run** `python3 hooks/created-log.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement** `parse_created` and `main` (never blocks, exits 0 on any error through `hookkit.run`).
- [ ] **Step 4: Run** the self-test and `bash tests/run-all.sh`. Expected: pass, and the duplicate-key check on `hooks.json` passes.
- [ ] **Step 5: Commit** `log pull requests, issues and releases agents create`

### Task 3: Keep hook notes as notices

**Files:**
- Modify: `lib/hookkit.py` (`note`)
- Modify: `lib/brain/cli.py` (`brain notice list [--all]`, `brain notice dismiss ID`)

**Interfaces:**
- Consumes: `records.add_notice`, `records.dismiss_notice`.
- `hookkit.note(event_name, text)` keeps printing exactly as today, then records the notice with a 1 s SQLite busy timeout. Any failure is dropped. `LEAN_NOTICES=off` turns recording off. The hook name is `os.path.basename(sys.argv[0])`.

- [ ] **Step 1: Write failing checks** in `hookkit.self_test`: with `LEAN_BRAIN_DB` in a temp dir, `note("PreToolUse", "x")` prints the same JSON as before and leaves one notice row; with a locked DB it still prints and returns within 2 s; with `LEAN_NOTICES=off` no row.
- [ ] **Step 2: Run** `python3 lib/hookkit.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.** Import `brain.records` lazily inside `note` so hooks that never note do not open the DB.
- [ ] **Step 4: Run** `bash tests/run-all.sh`. Expected: `0 failed` (every hook self-test still sees the same printed output).
- [ ] **Step 5: Commit** `keep hook notes as notices in brain`

### Task 4: Prompts and opening a session

**Files:**
- Create: `lib/brain/web/__init__.py` (empty)
- Create: `lib/brain/web/session.py`

**Interfaces:**
- Produces:
  - `clean(s) -> str`, `ask_prompt(item: dict) -> str`, `work_prompt(item: dict) -> str`, `can_send(kind: str) -> bool`: line-for-line ports of `clean`, `askPrompt`, `workPrompt`, `canSend` in `vscode/src/claude.ts`, same output strings.
  - `session_folder(item: dict) -> tuple[str, str]`: `(folder, note)`. `session_cwd` if it is a directory, else `jobs.repo_dir`/`items.repo` if it is a directory, else home with note `"folder <x> is gone, opened in home"`.
  - `launch_argv(platform: str, folder: str, prompt: str) -> list[str] | None`: macOS returns `["osascript", "-e", <script>, folder, prompt]` where the script reads `item 1 of argv`/`item 2 of argv` and runs `do script "cd " & quoted form of f & " && claude " & quoted form of p` in Terminal; Windows returns `["wt.exe", "-d", folder, "claude", prompt]`; anything else returns None.
  - `copy_command(folder, prompt) -> str`: `cd <shlex-quoted folder> && claude <shlex-quoted prompt>`.
  - `start_session(item: dict, run=subprocess.run) -> dict`: `{"opened": bool, "command": str, "note": str}`; uses `launch_argv(sys.platform)` and falls back to `copy_command` when it is None or the launch fails.

- [ ] **Step 1: Write failing self-test cases**: `work_prompt` and `ask_prompt` equal the strings the TypeScript tests in `vscode/test` expect for the same items; a title with `\n`, `\x1b` and `$(rm -rf ~)` comes out on one line with the `$(...)` intact as text and never outside a quoted argv element; `launch_argv("darwin", ...)` has the prompt as its own element; `launch_argv("linux", ...)` is None; `session_folder` with a deleted `session_cwd` returns home and the "is gone" note; `start_session` with a `run` that raises returns `opened: False` and a copy command.
- [ ] **Step 2: Run** `python3 lib/brain/web/session.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the self-test. Expected: pass.
- [ ] **Step 5: Commit** `open a Claude session on a brain item from Python`

### Task 5: Forge state, tickets and worktrees

**Files:**
- Create: `lib/brain/web/forges.py`
- Create: `lib/brain/web/worktrees.py`

**Interfaces:**
- Produces in `forges.py`:
  - `available(cli: str, which=shutil.which, run=subprocess.run) -> str`: `'ok'`, `'missing'` or `'logged out'` (`gh auth status`, `az account show`).
  - `pr_state(forge, repo, number, run=...) -> str`: `'open'|'merged'|'closed'|''` via `gh pr view N -R repo --json state` or `az repos pr show --id N`.
  - `pending_tickets(run=...) -> list[dict]`: GitHub `gh search issues --assignee @me --state open --json repository,number,title,url,updatedAt --limit 50`; Azure `az boards query --wiql` for open items assigned to `@Me` when `az devops configure -l` names an organization. Each dict has `forge, repo, number, title, url, updated`.
  - `class Cache(ttl_s=300)`: `get(key, fn)` returns a cached value or calls `fn`; a failing `fn` caches `''` for 60 s.
- Produces in `worktrees.py`:
  - `known_repos(con, roots: list[str]) -> list[str]`: distinct existing directories from `jobs.repo_dir`, `items.session_cwd`, `sessions.cwd` that are git repos (`git rev-parse --show-toplevel`), plus git repos one level under each root; deduplicated by `--git-common-dir`.
  - `list_worktrees(repo: str, run=...) -> list[dict]`: from `git worktree list --porcelain`, each `{path, branch, unpushed: int, dirty: bool, missing: bool}`; `unpushed` from `git rev-list --count @{u}..HEAD` (no upstream counts every commit not on any remote: `git rev-list --count HEAD --not --remotes`).

- [ ] **Step 1: Write failing self-test cases** with fake `run` and `which`: `gh` missing gives `'missing'` and `pending_tickets` returns `[]` without raising; bad JSON from `gh` gives `[]`; `pr_state` for `MERGED` gives `'merged'`; `Cache` calls `fn` once inside the TTL; in a real temp repo with two worktrees, one with an uncommitted file and one commit not pushed, `list_worktrees` reports `dirty` and `unpushed: 1`; a worktree whose folder was deleted reports `missing: True`.
- [ ] **Step 2: Run** both self-tests. Expected: FAIL.
- [ ] **Step 3: Implement.** All subprocess calls take an argument list and a 10 s timeout.
- [ ] **Step 4: Run** both self-tests. Expected: pass.
- [ ] **Step 5: Commit** `read pull request state, tickets and worktrees for the brain page`

### Task 6: Page data and HTML

**Files:**
- Create: `lib/brain/web/sections.py`
- Create: `lib/brain/web/page.py`

**Interfaces:**
- Consumes: Task 1 tables, Task 5 functions, `session.can_send`, `measures(conn, since)` from `bin/lean-stats` loaded with `importlib.machinery.SourceFileLoader` against `leanstats.connect()`.
- Produces in `sections.py`: `collect(con, cache, roots) -> dict` with keys `needs_you, queue, created, tickets, worktrees, tested, notices, usage, decided, errors`. Each section is a list of dicts; a section that failed puts one line in `errors[section]` and an empty list, so one failure never hides the rest.
  - `needs_you`: open items of kind `question`, `blocker`, `pending`, `risk`, newest first, limit 200; each with `can_send`.
  - `flaky`: open `flaky` counts per project (shown as one expandable line per project).
  - `queue`: jobs `queued`/`running` plus the last 20 ended, with run pass/fail counts.
  - `created`: last 100 `created` rows with cached `pr_state`.
  - `tested`: per `created` PR, the newest run whose job has the same `repo_dir` repo and `branch`, with `passed`/`failed`; a merged PR with no passing run gets `flag: 'merged untested'`.
  - `decided`: closed items with resolution `answered` and open or closed `ruling` items, last 50.
- Produces in `page.py`: `render(data: dict, key: str, port: int) -> str`. Every value goes through `html.escape`. The key is embedded in each form as a hidden field. The page refreshes itself every 30 s with `<meta http-equiv="refresh">`.

- [ ] **Step 1: Write failing self-test cases**: an item titled `<script>alert(1)</script>` renders as escaped text; with 407 flaky items the page has one flaky line per project, not 407 rows; a merged PR with no run shows `merged untested`; a DB locked with `BEGIN EXCLUSIVE` from another connection makes `collect` return within 3 s with `errors` filled and the page still renders; `gh` missing shows "gh is not installed" in the tickets section and the other sections render.
- [ ] **Step 2: Run** `python3 lib/brain/web/page.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.** Open the DB through `store.connect()` with a 2 s busy timeout.
- [ ] **Step 4: Run** the self-test. Expected: pass.
- [ ] **Step 5: Commit** `render the brain page`

### Task 7: The server and its actions

**Files:**
- Create: `lib/brain/web/server.py`

**Interfaces:**
- Consumes: `sections.collect`, `page.render`, `session.start_session`, `records.dismiss_notice`, `items` close logic used by `brain close`.
- Produces: `make_server(port: int, key: str, db_path: str) -> http.server.ThreadingHTTPServer` and `host_ok(host: str, port: int) -> bool`, `origin_ok(origin: str, port: int) -> bool`.
- Routes: `GET /` page; `GET /healthz` returns `lean-brain <version>`; `POST /session` (`id`, `key`); `POST /answer` (`id`, `key`, `answer`, `approve`); `POST /dismiss` (`id`, `key`). A POST redirects back to `/` with a one-line result. `answer` closes the item with resolution `answered` and the text as the note (approve uses the item's recommendation line as the text); text is stored only, max 4000 characters.

- [ ] **Step 1: Write failing self-test cases** against a server on an ephemeral port: `Host: evil.example` gives 421; POST `/session` with a wrong key gives 403; POST with `Origin: https://evil.example` gives 403; POST `/session` with `id=1;rm` gives 400; a valid `/answer` closes the question with resolution `answered` and `brain answers --session <its session>` returns it; the response has no `Access-Control-Allow-Origin` header; the server is bound to `127.0.0.1`.
- [ ] **Step 2: Run** `python3 lib/brain/web/server.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.** `key = secrets.token_urlsafe(32)`, compared with `hmac.compare_digest`.
- [ ] **Step 4: Run** the self-test. Expected: pass.
- [ ] **Step 5: Commit** `serve the brain page on localhost`

### Task 8: `brain serve`, run modes and launch at login

**Files:**
- Modify: `lib/brain/cli.py` (`serve` subcommand)
- Create: `lib/brain/web/install.py`
- Create: `hooks/serve-start.py`
- Modify: `hooks/hooks.json` (SessionStart entry running `serve-start.py "${user_config.serve_mode}"`)
- Modify: `.claude-plugin/plugin.json` (`userConfig`: `serve_mode` with values `always`, `session`, `manual`, default `manual`; `worktree_roots`, a comma-separated list of folders, default empty). Check both entries with `claude plugin validate .` before committing.

**Interfaces:**
- `brain serve [--port N] [--idle-exit MIN] [--install MODE | --uninstall]`.
  - Start: if `GET http://127.0.0.1:<port>/healthz` answers `lean-brain`, print the URL and exit 0. Else serve, print both URLs.
  - `--idle-exit 30`: exit when no row in `sessions` has an empty `ended_at` for 30 minutes.
- `install.py`: `unit_for(platform, python, brain_path, port) -> tuple[path, text]` (macOS `~/Library/LaunchAgents/dev.baryo.lean-brain.plist`, Linux `~/.config/systemd/user/lean-brain.service`, Windows a `lean-brain.cmd` in the Startup folder under `%APPDATA%`); `install(...)`, `uninstall(...) -> bool`. No admin rights anywhere.
- `serve-start.py MODE`: for `session`, start `brain serve --idle-exit 30` detached (`start_new_session=True`, or `DETACHED_PROCESS` on Windows) when `/healthz` does not answer; for `always`, start it if `--install always` has not been run, and note once that it can be installed; for `manual`, do nothing. Never blocks the session.

- [ ] **Step 1: Write failing self-test cases**: a second `serve` on a port where a fake `/healthz` answers prints the URL and exits 0; a port held by something else exits 1 with "port N is in use"; `unit_for('darwin', ...)` writes a plist whose `ProgramArguments` are the python path, the brain path, `serve`, `--port`, `N`; `unit_for('win32', ...)` path is under `Microsoft\Windows\Start Menu\Programs\Startup`; `uninstall` of a missing unit returns False; `serve-start.py manual` starts nothing.
- [ ] **Step 2: Run** `python3 bin/brain --self-test` and `python3 hooks/serve-start.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `bash tests/run-all.sh`. Expected: `0 failed`.
- [ ] **Step 5: Commit** `add brain serve with run modes and launch at login`

### Task 9: Deprecate the VS Code panel, document, bump

**Files:**
- Modify: `vscode/src/extension.ts` (on activation, once per version, an information message: "The lean brain panel is deprecated and will be retired. Open the brain page instead." with a button that opens `http://leanagent.localhost:8770`; a command `leanBrain.openPage` does the same)
- Modify: `vscode/package.json` (register the command), `vscode/README.md` (deprecation line at the top)
- Modify: `README.md` (a "Brain page" section: `brain serve`, the three modes, the URL, the optional hosts line `127.0.0.1 leanagent.local` the user adds themselves, the Windows and Linux behaviour of the session button), and the Data section (the page calls `gh` and `az` with the user's login)
- Modify: `docs/specs/2026-10-20-team-brain-design.md` line 187: the developer's page is the brain page, not the VS Code dashboard
- Modify: `.claude-plugin/plugin.json` version `1.15.0`

- [ ] **Step 1: Write the failing vitest** in `vscode/test`: the activation shows the deprecation message once per version and `leanBrain.openPage` opens the page URL.
- [ ] **Step 2: Run** `cd vscode && npx vitest run`. Expected: FAIL.
- [ ] **Step 3: Implement** the extension change and the docs.
- [ ] **Step 4: Run** `cd vscode && npm run typecheck && npx vitest run` and `bash tests/run-all.sh`. Expected: both pass.
- [ ] **Step 5: Commit** `point the VS Code panel at the brain page and document it`

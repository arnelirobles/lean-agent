# Groundwork Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the repo root, split `bin/brain` into a `lib/brain/` package, and put every `gh` read behind a forge adapter, with no change in behaviour.

**Architecture:** `bin/brain` becomes a thin entry point over `lib/brain/` (store, items, queue, report, cli, selftest). The schema does not change, because the VS Code extension reads the database directly. `lib/forge.py` names the code host from a remote URL and wraps the GitHub reads that `lean-stats` and `retro-signals.sh` make today. GitLab and Azure DevOps raise `Unsupported` until step 4 adds them.

**Tech Stack:** Python 3 standard library only, bash 3.2 compatible shell, `gh` and `jq` as today.

**Spec:** `docs/specs/2026-10-20-team-brain-design.md`, delivery step 1.

## Global Constraints

- No behaviour change. Every existing self-test passes unchanged: `bash tests/run-all.sh` ends with `0 failed`.
- Python standard library only. No new dependency.
- Shell stays bash 3.2 compatible (no `mapfile`, no heredoc inside `$( )`).
- The brain database schema, file path (`${LEAN_BRAIN_DB:-~/.local/share/lean/brain.db}`) and migrations 1 to 7 are untouched.
- Every `brain` command's output, exit code and `--help` text stay byte for byte the same.
- A script in `bin/` or `hooks/` keeps a `--self-test`. New Python under `lib/` that holds logic carries a `--self-test`, which `tests/run-all.sh` finds through its `lib/*.py` glob.
- `.claude-plugin/plugin.json` moves from `1.11.0` to `1.12.0`, because `bin` and `lib` change.
- Commit messages are short imperative plain English with no trailers. No em dashes or arrow characters in any text.
- Out of scope: hooks that parse `gh` commands an agent types (`public-text`, `verify-claim`, `pr-merge-blocked`, `test-before-push`, `hookkit.gh_call`). They move to `lean check` in step 2. `bin/test-phase` is not split: it already reaches brain only through the CLI, and step 8 revisits it.

## Review Focus

1. **Running `bin/brain` from a symlink or another folder.** `test-phase` runs it as `python3 bin/brain`, the plugin puts `bin/` on PATH, and users symlink it. Expect the package to load from `lib/` next to the real file, found through `os.path.realpath(__file__)`. Pinned in Task 2.
2. **A `brain.db` written by 1.11.0.** Expect it to open with no migration and give the same `list`, `show` and `stats` output. Pinned in Task 2 by the golden comparison against the 1.11.0 script.
3. **A repo string that is a URL, not `owner/name`.** `lean-stats collect --repo` and `retro-signals.sh` take `owner/repo`. Expect `owner/name` to stay GitHub, a GitHub URL to work, and a GitLab or Azure URL to fail with one clear line and exit 2, not a `gh` error. Pinned in Task 3.
4. **`gh` missing, failing or returning bad JSON.** Expect today's results: `fetch_changes` returns `None`, `retro-signals.sh` prints 0 for the count. Pinned in Task 3.
5. **`pr-blockers.sh` run in a GitLab or Azure clone with no `-R`.** Expect one line naming the forge and exit 2, not a misleading GitHub error. Pinned in Task 4.

---

### Task 1: Remove the stale root scripts

**Reverted.** The root files were links into `bin/`, kept so the scripts work without the plugin (README, "Everything in the method still works without the plugin"). The final review caught it, and commit "put back the root links into bin" restores them.

The repo root holds 20 copies of `bin/` scripts from before the plugin layout (last touched in 73dd61c and 2dbaa05). Every one differs from its `bin/` copy, and nothing runs them.

**Files:**
- Delete: `agent-hygiene.sh assert-ran.sh asset-provenance.py bump-version.sh check-conflict-markers.sh check-deployed-sha.sh check-pinned-values.sh check-runtime.sh check-top-level-md.sh check-workflow-keys.py heavy.sh holdout.sh interaction-surface.sh lean-stats needs-review.sh pr-blockers.sh retro-signals.sh sync-default.sh why-red.sh workflow-cost.py` (repo root only)

**Interfaces:** none.

- [ ] **Step 1: Prove nothing refers to the root copies**

Run: `git grep -nE '(^|[ "(])\./(agent-hygiene|assert-ran|asset-provenance|bump-version|check-[a-z-]+|heavy|holdout|interaction-surface|lean-stats|needs-review|pr-blockers|retro-signals|sync-default|why-red|workflow-cost)(\.sh|\.py)?\b' -- ':!bin' ':!docs/plans'`
Expected: no output. Any hit means a reference to fix before deleting.

- [ ] **Step 2: Delete the 20 files with `git rm`**

- [ ] **Step 3: Run the suite**

Run: `bash tests/run-all.sh`
Expected: last line `run-all: N self-tests, 0 failed, ...`, where N is the count before the change.

- [ ] **Step 4: Commit**

```bash
git commit -m "remove the old script copies at the repo root"
```

### Task 2: Split brain into a package

**Files:**
- Create: `lib/brain/__init__.py` (empty), `lib/brain/store.py`, `lib/brain/items.py`, `lib/brain/queue.py`, `lib/brain/report.py`, `lib/brain/cli.py`, `lib/brain/selftest.py`
- Modify: `bin/brain` (becomes the entry point only)
- Modify: `.claude-plugin/plugin.json` (`"version": "1.12.0"`)

The code moves as it is. Only imports and the self-test's path change.

| Module | Takes from `bin/brain` (lines on 43446c1) |
| --- | --- |
| `store.py` | the vocabularies `STATUS` to `RUNNERS`, `ITEM_COLS`, `db_path`, `migrate_1` to `migrate_7`, `MIGRATIONS`, `connect`, `now`, `epoch` (82 to 216) |
| `items.py` | `tags_of`, `line`, `add_tags`, `get`, `body_of`, `session_ok`, `cmd_add`, `cmd_list`, `cmd_show`, `cmd_close`, `cmd_session`, `cmd_answers`, `cmd_reopen`, `cmd_run_add` (218 to 395) |
| `queue.py` | `JOB_STATUS`, `JOB_TIERS`, `project_of`, `cancel_dependents`, `job_line`, the Apple regexes, `_apple_text`, `detect_platform`, `cmd_queue` (396 to 650) |
| `report.py` | `RESUME_ORDER`, `default_project`, `cmd_resume`, `stats`, `grid`, `cmd_stats`, `cmd_export` (651 to 774) |
| `cli.py` | the module docstring (the usage text), `parser()`, and a new `main(argv) -> int` (775 to 903) |
| `selftest.py` | `self_test`, renamed `run(brain_cli: str) -> int` (905 to the end of the function) |

Import direction, no cycles: `store` imports nothing from the package; `items` and `queue` import `store`; `report` imports `store`, `items` and `queue`; `cli` imports all four. Use relative imports (`from . import store`), so `lib/brain/queue.py` never shadows the standard `queue` module.

**Interfaces:**
- Produces: `brain.store.connect() -> sqlite3.Connection`, `brain.store.db_path() -> str`, `brain.store.now() -> str`. Step 3 adds the event log in `store`.
- Produces: `brain.cli.main(argv: list[str]) -> int`, which parses `argv` with `parser()` and returns `args.fn(args)`.
- Produces: `brain.selftest.run(brain_cli: str) -> int`, the existing self-test, spawning `brain_cli` instead of `os.path.realpath(__file__)`.

`bin/brain` after the change, whole:

```python
#!/usr/bin/env python3
"""A local list of what went wrong and is not fixed yet, shared by every session on the machine.

The commands and their options are in lib/brain/cli.py; run brain --help.
"""
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

from brain import cli, selftest  # noqa: E402

if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(selftest.run(os.path.realpath(__file__)))
    sys.exit(cli.main(sys.argv[1:]))
```

- [ ] **Step 1: Save the 1.11.0 script and record golden output**

Run: `git show 43446c1:bin/brain > /tmp/brain-1.11.0 && chmod +x /tmp/brain-1.11.0`

Write `/tmp/brain-golden.sh <brain-cli> <db>`, which runs this sequence with `LEAN_BRAIN_DB=<db>` and prints every command's stdout, stderr and exit code:
`--help`; `add --project p --kind question --ref q1 --title "a question"`; `add --project p --kind test-fail --ref T.A --severity important --tag x`; `list`; `list --open --kind test-fail`; `show 1`; `close 1 --resolution answered --note yes`; `reopen 1`; `run-add --cycle c --project p --test-project T --tier fast --passed 3`; `queue add /tmp T.csproj --tier fast --platform none --project p`; `queue list`; `queue next --peek`; `queue cancel 1`; `resume --project p`; `stats`; `stats --json`; `export --format json`; `show 99`.

Run: `bash /tmp/brain-golden.sh /tmp/brain-1.11.0 /tmp/g-old.db > /tmp/golden-old.txt`
Expected: the file exists and ends with the `show 99` error and its exit code.

- [ ] **Step 2: Move the code into `lib/brain/` as in the table, and cut `bin/brain` down to the entry point above**

- [ ] **Step 3: Compare the output**

Run: `bash /tmp/brain-golden.sh bin/brain /tmp/g-new.db > /tmp/golden-new.txt && diff /tmp/golden-old.txt /tmp/golden-new.txt`
Expected: no diff, except timestamps if `show` or `export` print them. Mask those with `sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+Z-]+/TS/g'` on both files before diffing.

- [ ] **Step 4: Open a 1.11.0 database with the new code**

Run: `LEAN_BRAIN_DB=/tmp/g-old.db bin/brain list && LEAN_BRAIN_DB=/tmp/g-old.db /tmp/brain-1.11.0 list`
Expected: the same lines from both.

- [ ] **Step 5: Run brain through a symlink from another folder**

Run: `ln -sf "$PWD/bin/brain" /tmp/brain-link && (cd / && LEAN_BRAIN_DB=/tmp/g-new.db /tmp/brain-link list)`
Expected: the same lines as Step 4, not `ModuleNotFoundError`.

- [ ] **Step 6: Run the suite**

Run: `bash tests/run-all.sh`
Expected: `0 failed`. `bin/brain`, `bin/test-phase` and `bin/brain-mcp` all pass, since the last two call the CLI.

- [ ] **Step 7: Bump the version and commit**

Set `"version": "1.12.0"` in `.claude-plugin/plugin.json`.

```bash
git add bin/brain lib/brain .claude-plugin/plugin.json
git commit -m "split brain into a package under lib"
```

### Task 3: Forge adapter for the GitHub reads

**Files:**
- Create: `lib/forge.py`
- Modify: `bin/lean-stats` (`fetch_changes`, around line 288)
- Modify: `bin/retro-signals.sh` (the loop at lines 36 to 56)

**Interfaces:**
- Produces, in `lib/forge.py`:
  - `class Unsupported(Exception)`
  - `kind_of(url: str) -> str` returns `"github"` for `github.com`, `"gitlab"` for `gitlab.com` or any host whose name contains `gitlab`, `"azure"` for `dev.azure.com` and `*.visualstudio.com`, and `"unknown"` otherwise. Takes https, `ssh://` and `git@host:path` forms.
  - `remote_url(cwd: str) -> str` is the `origin` URL from `git remote get-url origin`, or `""`.
  - `run_command(cmd: list[str], timeout: int = 600) -> str | None`, the same contract as `lean-stats`' function of that name.
  - `for_repo(repo: str, run=run_command) -> GitHub` takes `owner/name` (always GitHub, as today) or a URL. Raises `Unsupported(f"{kind} is not supported yet; only GitHub is")` for gitlab, azure or unknown.
  - `class GitHub(run=run_command)` with:
    - `merged_prs(repo: str, since: str, limit: int = 200) -> list[dict] | None`. Each dict has `number: int`, `head_ref: str`, `head_sha: str`, `created_at: str`, `merged_at: str`. It is `None` when `gh` fails or prints bad JSON. It runs exactly `["gh", "pr", "list", "-R", repo, "--state", "merged", "--search", f"merged:>={since}", "--limit", str(limit), "--json", "number,headRefName,headRefOid,createdAt,mergedAt"]`.
    - `commits_after(repo: str, number: int, after: str) -> int` counts non-merge commits on the PR with committer date after `after`, and returns 0 on failure. The `gh api` call and `-q` filter are the ones `retro-signals.sh` uses now.
    - `failed_checks(repo: str, sha: str) -> int` uses retro-signals' `check-runs` call, and returns 0 on failure.
    - `escaped_refs(repo: str, number: int, merged_at: str) -> int` uses retro-signals' `gh search issues` call, and returns 0 on failure.
  - The CLI is `python3 lib/forge.py kind [DIR]` (prints the kind of DIR's origin, default `.`), `merged-prs REPO SINCE` (TSV: number, created_at, merged_at, head_sha), `commits-after REPO N ISO`, `failed-checks REPO SHA`, `escaped-refs REPO N ISO`, and `--self-test`. On `Unsupported` it prints `forge: <message>` to stderr and exits 2.

- [ ] **Step 1: Write the self-test in `lib/forge.py`**

It uses a fake `run` that records argv and returns canned output. The checks:
- `kind_of`: `https://github.com/o/r.git` gives github, `git@github.com:o/r.git` gives github, `https://gitlab.com/g/r` gives gitlab, `https://gitlab.example.org/g/r` gives gitlab, `https://dev.azure.com/org/p/_git/r` gives azure, `https://org.visualstudio.com/p/_git/r` gives azure, `https://bitbucket.org/o/r` gives unknown, `""` gives unknown.
- `for_repo("o/r")` is a `GitHub`. `for_repo("https://gitlab.com/g/r")` raises `Unsupported` with `"gitlab is not supported yet; only GitHub is"`.
- `merged_prs` sends the argv above exactly and maps `[{"number": 7, "headRefName": "b", "headRefOid": "abc", "createdAt": "2026-10-01T00:00:00Z", "mergedAt": "2026-10-02T00:00:00Z"}]` to `[{"number": 7, "head_ref": "b", "head_sha": "abc", "created_at": "2026-10-01T00:00:00Z", "merged_at": "2026-10-02T00:00:00Z"}]`.
- `merged_prs` is `None` when `run` returns `None`, and when it returns `"not json"`.
- `failed_checks`, `commits_after` and `escaped_refs` return 0 when `run` returns `None`, and the parsed count when it returns `"3\n"`. `commits_after` sums several lines, as the `awk` did: `"2\n1\n"` gives 3.
- The `merged-prs` CLI on a URL of an Azure repo exits 2 with `forge: azure is not supported yet; only GitHub is` on stderr.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 lib/forge.py --self-test`
Expected: FAIL, the functions are not defined yet.

- [ ] **Step 3: Implement `lib/forge.py` to the interfaces above**

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 lib/forge.py --self-test`
Expected: `self-test passed`.

- [ ] **Step 5: Move `fetch_changes` in `bin/lean-stats` onto `forge.for_repo(repo, run).merged_prs(repo, since)`**

Keep its signature and its `None` on failure. Build its rows from `head_ref`, `created_at` and `merged_at`. Catch `forge.Unsupported` in `collect_changes`, print `changes: skipped <repo>: <message>`, and go on to the next repo. The existing self-test's argv check (`calls[0][:8]`) still holds, because the first 8 arguments are the same.

- [ ] **Step 6: Move `retro-signals.sh`' `gh` calls onto the forge CLI**

Replace the `gh pr list` plus `jq` lookups with one `python3 "$lib/forge.py" merged-prs "$repo" "$since"` read in a `while IFS=$'\t' read -r n opened merged head` loop. Replace the three `gh api` and `gh search` calls with `commits-after`, `failed-checks` and `escaped-refs`. Set `lib` from the script's real path, the way `bin/lean-stats` does. The script no longer needs `jq`, so drop it from the header comment.

- [ ] **Step 7: Add a check to the forge self-test for URL repos given to `retro-signals.sh`**

Running `bash bin/retro-signals.sh --since 2026-10-01 https://dev.azure.com/o/p/_git/r` exits 2 and prints `forge: azure is not supported yet; only GitHub is` before printing any row past the header.

- [ ] **Step 8: Run the suite**

Run: `bash tests/run-all.sh`
Expected: `0 failed`. `bin/lean-stats`, `bin/retro-signals.sh` and `lib/forge.py` are listed `ok`.

- [ ] **Step 9: Commit**

```bash
git add lib/forge.py bin/lean-stats bin/retro-signals.sh
git commit -m "read merged pull requests through a forge adapter"
```

### Task 4: Make pr-blockers say when the forge is not GitHub

`pr-blockers.sh` reads GitHub-only state (rulesets, merge queue, branch protection), so it stays GitHub-only. In a GitLab or Azure clone it must say that, not fail on a `gh` error.

**Files:**
- Modify: `bin/pr-blockers.sh` (before the `gh repo view` at line 345)

**Interfaces:**
- Consumes: `python3 lib/forge.py kind [DIR]` from Task 3.

- [ ] **Step 1: Add a self-test case**

In `pr-blockers.sh`' self-test, make a temp git repo with `origin` set to `https://gitlab.com/g/r.git`. Run `pr-blockers.sh 3` inside it with no `-R`. It must exit 2 and print `pr-blockers: this repository is on gitlab; pr-blockers reads GitHub pull requests only` on stderr. A second case with origin `https://dev.azure.com/o/p/_git/r` prints the same line with `azure`.

- [ ] **Step 2: Run it and watch it fail**

Run: `bash bin/pr-blockers.sh --self-test`
Expected: FAIL on the new case.

- [ ] **Step 3: When no `-R` is given, read the kind with the forge CLI. On anything but `github` or `unknown`, print the line and exit 2**

`unknown` (no origin, or another host) keeps today's path, so `gh` gives its own error as it does now.

- [ ] **Step 4: Run it and watch it pass, then run the suite**

Run: `bash bin/pr-blockers.sh --self-test && bash tests/run-all.sh`
Expected: `self-test passed`, then `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add bin/pr-blockers.sh
git commit -m "say so when pr-blockers runs outside GitHub"
```

### Task 5: Document the layout

**Files:**
- Modify: `README.md` (the part that lists the scripts on PATH, near line 101)

- [ ] **Step 1: Add two sentences.** brain's code lives in `lib/brain/` and `bin/brain` only starts it. Reads of merged pull requests go through `lib/forge.py`, which supports GitHub, and GitLab and Azure DevOps come with tracker support.

- [ ] **Step 2: Check the text**

Run: `bin/style-scan --base origin/main`
Expected: no findings.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "say where brain's code and the forge adapter live"
```

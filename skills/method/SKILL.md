---
name: method
description: Lean agent's working rules for coding agents on a backlog. Load whenever planning, filing or triaging issues, starting or reviewing a change, running agents in parallel, writing a script, or finishing a batch.
user-invocable: false
---

# Lean agent, condensed

Full text: ${CLAUDE_PLUGIN_ROOT}/skills/method/full-text.md

## Mode
Two settings change how the rules below apply: `LEAN_AGENTS` (`many` or `one`) and `LEAN_MODELS` (`mixed` or `one`), in the `env` block of Claude Code settings. At session start the plugin notes any setting that is not the default and what it changes. No note means many agents and mixed models, as written here.

With mixed models, three more settings name the models: `LEAN_MODEL_CHEAP` (default `sonnet`), `LEAN_MODEL_STRONG` (default `opus`) and `LEAN_MODEL_FAST` (default `haiku`), each a Claude Code model alias. Start the drafter, the spec review and the critic's hunter and refuter with the cheap model. The strong model takes the branch and the critic's list only when a finding is not fixed in one round or a tier 2 answer is unsure. The fast model runs agents that read and report: `search` (locate the code a draft or review needs), `gate` (run the scripted gates and paste their output) and `retro` (the retro's fact collection). It never judges code, so the hunter and the refuter do not run on it. No note means `sonnet`, `opus` and `haiku`.

Label every agent you start, so lean-stats can count it by role. The Agent description is `lean:<role> <owner/repo>#<pr>`, or `lean:<role> <owner/repo>@<branch>` before the pull request exists. The roles are `draft`, `spec`, `hunt`, `refute`, `escalate` (the strong model taking what the cheap one did not close), `search`, `gate` and `retro` (the last three run on the fast model). In a workflow script the same label is the first line of the agent's prompt, because a workflow agent has no description. An agent with no label is counted as `other` and its cost cannot be tied to a change.

## Before work starts
- Refine and revise while the plan moves; start a ticket only when it is one agent's worth.
- Before filing an issue, search the open issues and open pull requests for the same area or problem (a fix can already sit in an open pull request) and add to the one that fits (its Covers checklist, with its own check). File new only when it is truly separate.
- One agent pass is one ticket: whatever one agent can finish in one focused pass (one pull request or a short stack, one repository, same area or fix pattern). Security fixes stay separate unless they are the same fix.
- Write tickets in the agent-ready shape with the `shape-ticket` skill: Goal, Where, Covers, Done when, Risks, Constraints, Out of scope.
- Bigger tickets get a cheap spec review (read the ticket and the code, list what could go wrong) before anyone builds.
- Plan the steps that need a person up front: merges without a standing authorisation, production, writes to outside services, destructive git. Record any standing authorisation with its scope (which changes, until when, what it excludes). End a run with one block of exact commands for the person.

## While working
- Scripts, not instructions: run the repository's full gate script, not the one gate that seems relevant.
- The second time something is reasoned through, it becomes a script with a test. Fix the script, not the run. Scripts stop at the first surprise and say so.
- A rule in the agent instructions that can be stated about types, namespaces or references becomes an architecture test, and the instructions name the test.
- A script that works on any repository goes to the method as a pull request; one tied to a repository goes to its `scripts/`. Always a pull request, merged by a person.
- Every change the diff gates gets the `adversarial-review` skill; findings go back to the agent that wrote the change.
- At most four changes in flight. Heavy commands under a shared lock (`${CLAUDE_PLUGIN_ROOT}/tools/heavy.sh`), every wait loop has a deadline, every agent writes under its own scratchpad subdirectory.
- Subagents run commands in the foreground; they are never told when background work finishes and stall. Size every gate piece to finish under the 600 second tool timeout, including any time spent waiting for a lock.
- A worktree-isolated agent runs git as plain single commands from the worktree root: no `cd x &&`, no pipes. The harness refuses what it cannot verify.
- Parallel changes never append to one shared file such as a changelog: one fragment file per change.
- Verify content and identity, not status codes or ratios: a page baked empty still answers 200, a small pixel ratio can hide wrong dates, and the deployed commit sha proves a deploy where a version string does not.
- Before opening a pull request, run `${CLAUDE_PLUGIN_ROOT}/tools/style-scan` on the branch and on the body file, and `${CLAUDE_PLUGIN_ROOT}/tools/holdout.sh` for the mutation check. Do not write your own dash grep or revert loop.
- A task is not done until the pull request URL exists.

## Scripts
The scripts live in `${CLAUDE_PLUGIN_ROOT}/tools/`. They are on PATH when the session-env hook ran, so run one by name; otherwise run it as `${CLAUDE_PLUGIN_ROOT}/tools/<name>`. The names below stand for either. A session start note also prints the absolute folder. Use them instead of writing your own.
- `style-scan`: checks the lines you add (and untracked files, so a body file counts) for dashes, arrows, slop and attribution. Run it before every commit, and on a body file before `gh ... --body-file`.
- `holdout.sh --spec <file>`: holds a change's production hunk out and requires its named test to fail. Use it for the mutation check, not a hand revert.
- `replace`: exact text edit that fails unless the old text is there exactly once.
- `wait-loop` hook (`hooks/wait-loop.py`): denies `sleep` and `pgrep` wait loops. Enqueue and end the turn instead, or start `brain queue wait ID...` or `pr-wait` in the background.
- `review-log-check` hook (`hooks/review-log-check.py`): denies `gh pr ready`, `gh pr merge` and `az repos pr update --draft false` when `lean.db` has no review row for that pull request. Log it with `log-review.sh <repo> <pr> ...` first, or set `LEAN_SKIP_REVIEW_LOG_CHECK=1` for a change that was not reviewed.
- `pr-wait <pr>`: waits for a pull request's checks (or `--provider azure` for a pipeline run) and prints only the failed checks and the tail of their logs.
- `orient [--all]`: repo, branch, dirty count, ahead and behind, open PR with its checks, and with `--all` one line per worktree. Use it instead of a status loop over worktrees.
- `pkg-version <name> [--nuget|--npm]`: the latest stable version and what each project in the repo pins. Use it instead of a curl to a registry.
- `script-note` hook (`hooks/script-note.py`): when a command matches a family in `lib/script_families.json` that a script covers, or calls a script by its plugin cache path, it adds a one line note naming the script. Once per family per session. `LEAN_SCRIPT_NOTE=off` turns it off. The same hook blocks (exit 2) a direct `dotnet test` (a .sln or .csproj in reach) or `npm test`, `npx vitest`, `vitest run` (a pnpm-lock.yaml in reach) and names the `brain queue add` line to use; `LEAN_DIRECT_TEST=allow` turns that off.
- `bounded [--minutes N] -- cmd`: runs a command in its own process group and stops the whole group after `LEAN_BG_MINUTES` (default 60), when the task is stopped, or when its shell dies. A SIGKILL of `bounded` itself cannot be caught and leaves the group running; `agent-hygiene.sh` reports such orphans. The `bg-bound` hook (`hooks/bg-bound.py`) denies a background command that does not run under it, `test-phase` or `heavy.sh`, and prints the wrapped form to run instead.
- `pr-blockers.sh <pr>`: every reason a pull request cannot merge, in one read-only call.
- `why-red.sh <log>`: names environmental causes (no Docker, a busy port, a full disk) in a failed log before you debug code.
- `assert-ran.sh <report>`: fails when a test report shows zero tests ran.
- `check-deployed-sha.sh <url> <sha>`: proves a deploy by the commit sha it reports, not a 200.
- `brain` and `test-phase`: the issue list and the test queue (below).
- `heavy.sh <lane> <command>`: runs a heavy command under the shared lane lock; `heavy.sh status <lane>` says whether it is free.
- `sync-default.sh`: merges the default branch and runs a locked restore. Refuses a dirty tree.
- `needs-review.sh`: prints the review rules a diff fires.
- `agent-hygiene.sh`: reports what parallel agents left behind.
- `check-conflict-markers.sh`, `check-top-level-md.sh`, `check-workflow-keys.py`, `check-runtime.sh`: cheap repository checks, run them in the preflight.
- `script-repeats [--days N]`: counts the commands agents wrote by hand in recent transcripts, per family, with the script that covers each. The retro runs it.
- `retro-signals.sh`, `lean-stats`, `workflow-cost.py`: the retro's facts and what a run cost.

### Drafter brief (`lean:draft`)
Coding agents call these instead of writing the command again:
- `replace FILE < blocks`: exact text edit that fails unless the old text is there once. Use it instead of inline python that calls `.replace`.
- `brain queue add ...`: queue the tests, end the turn, and the orchestrator runs `test-phase`. Never run `dotnet test`, `npx vitest`, `vitest run` or `npm test` yourself; the script-note hook blocks it where test-phase can take the job.
- `check-deployed-sha.sh <url> <sha>`: proves a deploy by the commit sha it reports. Use it instead of a curl smoke check.
- `style-scan`: checks added lines for dashes, arrows, slop and attribution. Use it instead of a grep, before every commit.
- `pkg-version <name> [--nuget|--npm]`: the latest stable version and what the repo pins. Use it instead of a curl to a registry.

## Tests and brain
- Code first, build only. Run the tests once, as one phase: add each test project with `${CLAUDE_PLUGIN_ROOT}/tools/brain queue add <repo-dir> <test project> [--after ID]`, then the orchestrator starts one `${CLAUDE_PLUGIN_ROOT}/tools/test-phase` as a background task. Queue one job per worktree: put every class you need in one `--filter 'ATests|BTests'` rather than one job per class, run `${CLAUDE_PLUGIN_ROOT}/tools/brain queue list` first, and when an earlier job of yours for the same worktree is still queued, cancel it and add the merged one. Adding a job that is already queued returns its id, so wait on that. Failures land in `${CLAUDE_PLUGIN_ROOT}/tools/brain`. The fix round reads `${CLAUDE_PLUGIN_ROOT}/tools/brain list --open`, closes entries with a resolution and the fix commit, and the phase runs again.
- Test only what is needed while working. A fix or review round queues only the classes the change touches and the classes that failed. The full suite runs once, as the final check before pushing: the queue refuses a second full job for the same project, test project and commit, and a full job from a tree with uncommitted changes, unless you pass `--again "reason"`. Its failures are rerun one class at a time. When several branches need that final run, merge them into one candidate and run it once. A red run, the test proving it fails before the fix, stays on its own branch.
- To wait, start the wait command in the background and end the turn: `${CLAUDE_PLUGIN_ROOT}/tools/brain queue wait ID...` for test jobs, `${CLAUDE_PLUGIN_ROOT}/tools/pr-wait` for a pull request's checks or a pipeline run. Never call `${CLAUDE_PLUGIN_ROOT}/tools/brain queue list`, `gh pr checks` or `az pipelines runs show` twice in a row for the same thing.
- Never write a `sleep` or `pgrep` wait loop. Enqueue the job, end the turn, and the orchestrator's one drainer runs it. The orchestrator gets one notice when the drainer exits, then reads `${CLAUDE_PLUGIN_ROOT}/tools/brain list --cycle C` or `${CLAUDE_PLUGIN_ROOT}/tools/brain stats`. `${CLAUDE_PLUGIN_ROOT}/tools/brain resume` shows queued and running jobs after a compaction, and the startup hook prints `${CLAUDE_PLUGIN_ROOT}/tools/brain digest` for the project.
- Ask whether a lane is free with `${CLAUDE_PLUGIN_ROOT}/tools/heavy.sh status <lane>`. Never write a `pgrep` loop for it: it matches its own command line and never ends. Set `HEAVY_STALL` on test commands so a stalled run stops as load, not as a code failure.
- Check the runner mode before `dotnet test`. A repository whose `global.json` sets `test.runner` to `Microsoft.Testing.Platform` takes `dotnet test --project X`; any other takes `dotnet test X`. The wrong form loses the run.
- Brain is where state that changes lives, so it survives a compaction. Open an item when you dispatch an agent (kind `agent`: id, task, worktree), when you defer work (`pending` for the next step, `minor` for a deferred fix) or when an action is denied or waits on the person (`blocker`). Close it when the agent reports back, the work is done or the answer comes, with a resolution (or `--wontfix`). Kinds are a fixed list and an unknown one is refused. A denied action is not worked around.
- What goes where: brain holds anything with a status that changes (test failures, flaky classes, deferred minors, questions for the user, tickets, security findings). Markdown ledgers keep rulings with the cost if wrong, task complete lines and user decisions, and point to open items with `open items: brain list --project X --status open`. Memory files keep standing preferences. Run logs are scratch.
- A question for the person: file it with `${CLAUDE_PLUGIN_ROOT}/tools/brain add --kind question` (title is the question, body has the options and your recommendation), then end your turn. Do not wait in the session. Brain stores the session id, and the answer comes back to this session: the extension resumes it, or the next prompt carries it.
- Never store secrets, tokens or passwords in brain.

## After a batch
- Run the `lean-retro` skill: facts from lean-stats, fixed triggers, check whether the last method change helped, propose changes with evidence. Nothing changes without the owner's approval.
- Log every review with `${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/log-review.sh`.
- No agent attribution and no slop in commits, pull requests, issues or releases. The plugin hook blocks both; rewrite the text, do not turn the hook off.
- Sweep with `${CLAUDE_PLUGIN_ROOT}/tools/agent-hygiene.sh` for what parallel agents left behind.
- After each milestone, a lean pass over what shipped: agent-written code and comments over-explain, so cut commentary that restates the code and verbose code a newer language feature says more plainly. No behaviour change, and its own pull request.

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

Label every agent you start, so `lean-stats` can count it by role. The Agent description is `lean:<role> <owner/repo>#<pr>`, or `lean:<role> <owner/repo>@<branch>` before the pull request exists. The roles are `draft`, `spec`, `hunt`, `refute`, `escalate` (the strong model taking what the cheap one did not close), `search`, `gate` and `retro` (the last three run on the fast model). In a workflow script the same label is the first line of the agent's prompt, because a workflow agent has no description. An agent with no label is counted as `other` and its cost cannot be tied to a change.

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
- At most four changes in flight. Heavy commands under a shared lock (`${CLAUDE_PLUGIN_ROOT}/bin/heavy.sh`), every wait loop has a deadline, every agent writes under its own scratchpad subdirectory.
- Subagents run commands in the foreground; they are never told when background work finishes and stall. Size every gate piece to finish under the 600 second tool timeout, including any time spent waiting for a lock.
- A worktree-isolated agent runs git as plain single commands from the worktree root: no `cd x &&`, no pipes. The harness refuses what it cannot verify.
- Parallel changes never append to one shared file such as a changelog: one fragment file per change.
- Verify content and identity, not status codes or ratios: a page baked empty still answers 200, a small pixel ratio can hide wrong dates, and the deployed commit sha proves a deploy where a version string does not.
- A task is not done until the pull request URL exists.

## After a batch
- Run the `lean-retro` skill: facts from `${CLAUDE_PLUGIN_ROOT}/bin/lean-stats`, fixed triggers, check whether the last method change helped, propose changes with evidence. Nothing changes without the owner's approval.
- Log every review with `${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/log-review.sh`.
- No agent attribution and no slop in commits, pull requests, issues or releases. The plugin hook blocks both; rewrite the text, do not turn the hook off.
- Sweep with `${CLAUDE_PLUGIN_ROOT}/bin/agent-hygiene.sh` for what parallel agents left behind.
- After each milestone, a lean pass over what shipped: agent-written code and comments over-explain, so cut commentary that restates the code and verbose code a newer language feature says more plainly. No behaviour change, and its own pull request.

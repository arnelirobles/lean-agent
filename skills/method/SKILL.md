---
name: method
description: Lean agent's working rules for coding agents on a backlog. Load whenever planning, filing or triaging issues, starting or reviewing a change, running agents in parallel, writing a script, or finishing a batch.
user-invocable: false
---

# Lean agent, condensed

Full text: ${CLAUDE_PLUGIN_ROOT}/README.md

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
- A script that works on any repository goes to the method as a pull request; one tied to a repository goes to its `scripts/`. Always a pull request, merged by a person.
- Every change the diff gates gets the `adversarial-review` skill; findings go back to the agent that wrote the change.
- At most four changes in flight. Heavy commands under a shared lock (`${CLAUDE_PLUGIN_ROOT}/bin/heavy.sh`), every wait loop has a deadline, every agent writes under its own scratchpad subdirectory.
- Subagents run commands in the foreground; they are never told when background work finishes and stall. Size every gate piece to finish under the 600 second tool timeout.
- A worktree-isolated agent runs git as plain single commands from the worktree root: no `cd x &&`, no pipes. The harness refuses what it cannot verify.
- Parallel changes never append to one shared file such as a changelog: one fragment file per change.
- Verify content and identity, not status codes or ratios: a page baked empty still answers 200, a small pixel ratio can hide wrong dates, and the deployed commit sha proves a deploy where a version string does not.
- A task is not done until the pull request URL exists.

## After a batch
- Run the `lean-retro` skill: facts from `${CLAUDE_PLUGIN_ROOT}/bin/retro-signals.sh`, fixed triggers, check whether the last method change helped, propose changes with evidence. Nothing changes without the owner's approval.
- Log every review with `${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/log-review.sh`.
- No agent attribution and no slop in commits, pull requests, issues or releases. The plugin hook blocks both; rewrite the text, do not turn the hook off.
- Sweep with `${CLAUDE_PLUGIN_ROOT}/bin/agent-hygiene.sh` for what parallel agents left behind.
- After each milestone, a lean pass over what shipped: agent-written code and comments over-explain, so cut commentary that restates the code and verbose code a newer language feature says more plainly.

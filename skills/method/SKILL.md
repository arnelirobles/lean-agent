---
name: method
description: The lean agent method's working rules for coding agents on a backlog. Load whenever planning, filing or triaging issues, starting or reviewing a change, running agents in parallel, writing a script, or finishing a batch.
user-invocable: false
---

# The lean agent method, condensed

Full text: ${CLAUDE_PLUGIN_ROOT}/README.md

## Before work starts
- Refine and revise while the plan moves; start a ticket only when it is one agent's worth.
- Before filing an issue, search the open issues for the same area or problem and add to the one that fits (its Covers checklist, with its own check). File new only when it is truly separate.
- One agent pass is one ticket: whatever one agent can finish in one focused pass (one pull request or a short stack, one repository, same area or fix pattern). Security fixes stay separate unless they are the same fix.
- Write tickets in the agent-ready shape with the `shape-ticket` skill: Goal, Where, Covers, Done when, Risks, Constraints, Out of scope.
- Bigger tickets get a cheap spec review (read the ticket and the code, list what could go wrong) before anyone builds.

## While working
- Scripts, not instructions: run the repository's full gate script, not the one gate that seems relevant.
- The second time something is reasoned through, it becomes a script with a test. Fix the script, not the run. Scripts stop at the first surprise and say so.
- A script that works on any repository goes to the method as a pull request; one tied to a repository goes to its `scripts/`. Always a pull request, merged by a person.
- Every change the diff gates gets the `adversarial-review` skill; findings go back to the agent that wrote the change.
- At most four changes in flight. Heavy commands under a shared lock (`${CLAUDE_PLUGIN_ROOT}/bin/heavy.sh`), every wait loop has a deadline, every agent writes under its own scratchpad subdirectory.
- A task is not done until the pull request URL exists.

## After a batch
- Run the `lean-retro` skill: facts from `${CLAUDE_PLUGIN_ROOT}/bin/retro-signals.sh`, fixed triggers, check whether the last method change helped, propose changes with evidence. Nothing changes without the owner's approval.
- Log every review with `${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/log-review.sh`.
- No agent attribution and no slop in commits, pull requests, issues or releases. The plugin hook blocks both; rewrite the text, do not turn the hook off.
- Sweep with `${CLAUDE_PLUGIN_ROOT}/bin/agent-hygiene.sh` for what parallel agents left behind.

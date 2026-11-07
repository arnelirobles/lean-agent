---
name: method
description: Lean agent's working rules for coding agents on a backlog. Load whenever planning, filing or triaging issues, starting or reviewing a change, running agents in parallel, writing a script, or finishing a batch.
user-invocable: false
---

# Lean agent, condensed

This is the plain Markdown edition. It needs only git, gh, the repository's own test command and files in the repo. With lean-agent-tools installed, hooks and scripts enforce several of these rules for you.

## Agents and models
- Use a cheaper model for the drafter, the spec review and the review's hunter and refuter. Use the strongest model only when a finding is not fixed in one round, or when a tier 2 answer is unsure.
- Use a small, fast model for agents that only read and report: finding code, running the gates and pasting their output, collecting the retro's facts. It never judges code, so the hunter and the refuter do not run on it.
- Label every agent you start with its role and the change, for example `draft owner/repo#12` or `hunt owner/repo@branch`. Roles: draft, spec, hunt, refute, escalate, search, gate, retro. It lets you count what each role cost.
- With one agent only, run the review steps as two separate passes in the same session and say in the report that the finder and the judge shared a context.

## Before work starts
- Refine and revise while the plan moves; start a ticket only when it is one agent's worth.
- Before filing an issue, search the open issues and open pull requests for the same area or problem (a fix can already sit in an open pull request) and add to the one that fits (its Covers checklist, with its own check). File new only when it is truly separate.
- One agent pass is one ticket: whatever one agent can finish in one focused pass (one pull request or a short stack, one repository, same area or fix pattern). Security fixes stay separate unless they are the same fix.
- Write tickets in the agent-ready shape with the `shape-ticket` skill: Goal, Where, Covers, Done when, Risks, Constraints, Out of scope.
- Bigger tickets get a cheap spec review (read the ticket and the code, list what could go wrong) before anyone builds.
- Plan the steps that need a person up front: merges without a standing authorisation, production, writes to outside services, destructive git. Record any standing authorisation with its scope (which changes, until when, what it excludes). End a run with one block of exact commands for the person.
- Do not stack pull requests on Azure DevOps: `az` cannot change a target branch, so each retarget is a person in the web UI. Open each pull request against the default branch. Either open the next one after the one before merges, or open it against the default branch now and name in its body which commits belong to the earlier pull request.

## While working
- Scripts, not instructions: run the repository's full gate script (its preflight), not the one gate that seems relevant.
- The second time something is reasoned through, it becomes a script with a test. Fix the script, not the run. Scripts stop at the first surprise and say so.
- A rule in the agent instructions that can be stated about types, namespaces or references becomes an architecture test, and the instructions name the test.
- A script that works on any repository goes to the method as a pull request; one tied to a repository goes to its `scripts/`. Always a pull request, merged by a person.
- Every change the diff gates gets the `adversarial-review` skill; findings go back to the agent that wrote the change.
- At most four changes in flight. Every wait has a deadline, and every agent writes under its own scratch subdirectory.
- Subagents run commands in the foreground; they are never told when background work finishes and stall. Size every gate piece to finish under the tool timeout.
- A worktree-isolated agent runs git as plain single commands from the worktree root: no `cd x &&`, no pipes. The harness refuses what it cannot verify.
- Parallel changes never append to one shared file such as a changelog: one fragment file per change.
- Verify content and identity, not status codes or ratios: a page baked empty still answers 200, a small pixel ratio can hide wrong dates, and the deployed commit sha proves a deploy where a version string does not.
- Before opening a pull request, scan the branch and the body text for dashes, arrows and slop words, and do the mutation check: take the change's production hunk out and confirm its named test fails. Do the mutation check in the draft, before the adversarial review starts, not after it. With lean-agent-tools installed, `style-scan` and `holdout.sh` do this for you.
- A task is not done until the pull request URL exists.

## Tests
- Code first, build only. Run the tests once, as one phase, after the code is written.
- Test only what is needed while working. A fix or review round runs only the classes the change touches and the classes that failed. The full suite runs once, as the final check before pushing, unless CI runs it on push; then let CI be the full run and rerun its failures locally one class at a time.
- When several branches need that final run, merge them into one candidate and run it once. A red run, the test proving it fails before the fix, stays on its own branch.
- Never write a `sleep` or `pgrep` wait loop. Start the long command in the background, end the turn, and read the result when it finishes. Check a pull request's checks once with `gh pr checks`, not in a loop.
- Check the runner mode before `dotnet test`. A repository whose `global.json` sets `test.runner` to `Microsoft.Testing.Platform` takes `dotnet test --project X`; any other takes `dotnet test X`. The wrong form loses the run.
- Check a failed log for environmental causes (no Docker, a busy port, a full disk) before you debug code, and fail a run that reports zero tests.

## State
- State that changes lives in files in the repo, so it survives a compaction. Keep one Markdown file, `docs/agent-state.md`, with sections for open agents (id, task, worktree), pending steps, deferred minors, blockers and questions for the person. Add an entry when you dispatch an agent, defer work, or an action is denied or waits on the person. Close it when the agent reports back, the work is done or the answer comes, with a resolution. A denied action is not worked around.
- What goes where: the state file holds anything with a status that changes (test failures, flaky classes, deferred minors, questions for the person, security findings). A rulings file keeps decisions with the cost if wrong and the person's decisions. Memory files keep standing preferences. Run logs are scratch.
- A question for the person: write it in the state file (the title is the question, the body has the options and your recommendation), then end your turn. Do not wait in the session.
- Never store secrets, tokens or passwords in the state file or anywhere in the repo.
- With lean-agent-tools installed, brain and the test queue keep this state and run the tests for you.

## After a batch
- Run the `lean-retro` skill: facts from git and gh, fixed triggers, check whether the last method change helped, propose changes with evidence. Nothing changes without the owner's approval.
- Log every review in `docs/review-log.md` (see the `adversarial-review` skill).
- No agent attribution and no slop in commits, pull requests, issues or releases. Rewrite the text if it slips in.
- Sweep for what parallel agents left behind: extra worktrees (`git worktree list`), stale branches, scratch files.
- After each milestone, a lean pass over what shipped: agent-written code and comments over-explain, so cut commentary that restates the code and verbose code a newer language feature says more plainly. No behaviour change, and its own pull request.

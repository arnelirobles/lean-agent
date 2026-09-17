---
name: adversarial-review
description: >
  The lean agent method's critic as a skill. Reviews one change (a pull request, a branch, or an
  agent's working tree) as an adversary: gates on what the diff touches, has one fresh agent hunt
  for defects against fixed questions and the repository's own rules, has a second fresh agent try
  to disprove every finding, and reports only what survives, with proof. Routes findings back to
  the agent that wrote the change, or posts them as review threads. Use it on every agent-made
  change before it merges, and whenever asked for an adversarial, critic or pre-merge review.
---

# Adversarial review

A review bot confirms. This decides. It exists because two things keep happening with agent-made
changes: the checks pass having checked nothing, and a general reviewer cannot know the rules a
repository has already decided. It catches what CI and a generic bot cannot see, and it proves
every claim before anyone spends time on it.

Three rules shape everything below.

1. **The finder is never the judge.** One agent looks for defects. A different agent, with no
   stake in the finding, tries to prove each one wrong. Only survivors are reported.
2. **No proof, no finding.** Every finding names the input, the code path at a line in the file as
   it is now, and the wrong result. If a runnable check would settle it in a minute, it is run.
3. **The repository's rules beat general taste.** Read the rules file before looking at code.

## Inputs

- A target: a PR number or URL, a branch, or `--wip` for an agent's uncommitted work.
- The repository's rules file, if it has one: `docs/review-rules.md`, then `.claude/review-rules.md`.
  `review-rules.example.md` next to this file shows the shape.
- Optional: a gate script such as `scripts/needs-review.sh` that prints the rules a diff fires.
- Optional: a general reviewer skill (for example a `pr-review` skill) for the broad passes in
  step 3. Without one, use the condensed passes listed there.

## 1. Check out the change cleanly

Review in a clean worktree of the branch, never in a checkout holding other work. A gate script
that walks the working tree will otherwise review someone else's files too.

```bash
git fetch origin
git worktree add "$(mktemp -d)/review" "origin/<branch>"
```

## 2. Gate on the diff, not the ticket

Run the gate script if the repository has one. Any rule it prints means a full review. Nothing
printed means a light review: steps 3 and 4 on the six questions only.

When there is no gate script, a full review is required if the diff touches authentication or
permissions, anything reachable without signing in, raw SQL, schema or migrations, secrets or
logging, concurrency or background work, deletion or retention, build and dependency files, or a
test that loses assertions.

## 3. Hunt (agent one, fresh context)

Give the hunter the diff, the PR description and linked issue, the rules file, and a checkout it
can build and test. It reads whole files around each hunk and every caller of a changed symbol,
then answers, each with the line that proves it:

**The six questions (always):**
1. Does every new test fail when the production line it depends on is reverted? Revert it and run it.
2. Is every new route in every inventory it belongs to, with the permission gate the issue asked for?
3. Does every session, query and cache key in the change name its tenant?
4. Does every new document, event, column or index have a migration, and does existing data survive it?
5. Does anything from an exception, a request or a user string reach a log or a response unexamined?
6. Does every read-then-write on a shared row name its lock, and every outbound call name its timeout and bound?

**The contract question (always):** does the change alter what a consumer sees (a route, a field,
a status code, stricter validation, a public type or function)? If so, is the version the rules
file names moved, and does every consumer's accepted range, listed in the rules file, still cover
the new version? A change that is correct in isolation and locks out a released consumer is a
blocker.

**The rules file (always):** every rule in it, answered yes or no with a line.

**Broad passes (full review):** use the general reviewer skill if one is installed. Otherwise run
these as separate short passes: correctness on edge and error paths; security from source to sink
(authorization, injection, redirects, secrets in anything publicly readable, constant-time
comparison, rate limits); stability (timeouts, bounded lists, cache staleness under failure,
shutdown); data integrity (ordering of side effects against failures, replay and duplicates);
missing behaviour (the handler, default, rollback or check that should be in the diff and is not);
tests that pass for the wrong reason (a disabled retry, an empty collection, a mock asserting on itself).

The hunter writes candidates as `category | severity | file:line | input | path | wrong result |
proposed fix`, and states what it did not check.

## 4. Refute (agent two, fresh context)

Give the refuter each candidate alone, with the checkout and the rules file, and no view of the
hunter's reasoning. Its job is to make the finding go away:

- Look for the guard elsewhere: a caller that validates, a framework that escapes, a type that
  excludes the case, a test that already covers it.
- Confirm the defect is caused by this change. A break in an untouched caller caused by a changed
  signature counts, anchored to the changed line.
- Run the smallest check that settles it: a scratch test, the named test with the line reverted, a
  query plan, a request against a local build.

Each candidate ends as **confirmed** (with the evidence), **refuted** (with the reason, dropped) or
**unsure**. Keep an unsure finding only if it is a blocker, marked as unverified with what would
settle it.

## 5. Report

Survivors only, blockers first, at most ten. Severity: **blocker** (security hole, data loss,
contract break with a consumer, wrong results on a normal path), **concern** (a real defect on an
edge path), **nit** (optional, labelled, never presented as required). Effort: **quick** or
**heavy**. Each one carries its evidence and a proposed fix. End with what was not checked.

If nothing survives, say so, and list what was checked. A clean review is a result.

## 6. Route

- **An agent is still working on the change:** send the findings back to that agent, not to a new
  one. It holds the context, and resuming it costs a small fraction of a fresh read. It fixes each
  finding or rebuts it with evidence, and the refuter checks the rebuttal once.
- **A pull request is open:** post one review with inline comments at the head commit, as a
  comment, never an approval or a request for changes. That vote belongs to a person. The author
  replies on each thread with what changed or why not, and resolves it.
- **No finding is left as a follow-up by default.** Fix it in the change or drop it. File an issue
  only for something that needs a different repository or a decision nobody in the change can make.

## 7. Record

Append one line per review with `log-review.sh`, so each batch can compare what this caught against
CI and any review bot:

```bash
skills/adversarial-review/log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> "<categories>" "<caught elsewhere first>"
```

When a category keeps surviving refutation, turn it into a scripted check. When a tier has
produced nothing for three batches, stop running the full review on it.

## Voice

Plain and short. One defect per thread. Quote the code. No filler, no attribution footer, and the
repository's own style rules apply to every word posted.

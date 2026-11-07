---
name: lean-retro
description: Run lean agent's retro after a batch of changes merges. Collects facts from git and gh, checks them against fixed triggers, checks whether the last method change helped, and proposes changes to the method with evidence for a person to approve. Use it at the end of every batch, weekly at the latest, and whenever asked for a retro, a method review or "what should we change about how we work".
---

# Lean retro

The method improves only through this routine, and only with a person's approval. You propose. You never change the method, a script, a CLAUDE.md or memory on your own.

## 1. Collect facts, do not reason yet

For the batch's window and every repository it touched, collect one row per merged pull request:

```bash
gh pr list --repo owner/repo --state merged --search "merged:>=YYYY-MM-DD" \
  --json number,title,mergedAt,additions,deletions,commits,reviews,comments,labels
git log --since=YYYY-MM-DD --oneline --no-merges
```

For each pull request also count the fix rounds (commits pushed after the first review or the first failed check) and note which checks failed first (`gh pr checks <n>`). The collection can be given to a small, fast model that runs the commands and returns the rows. The reasoning in sections 2 to 4 stays with the session.

A fact you could not collect is a gap, not a zero: say so in the retro and do not reason from it. With lean-agent-tools installed, lean-stats and retro-signals.sh collect cost per change by model and role, the escalation rate, critic misses and the avoidable share of tokens for you.

Also gather, from the session or the coordinator's notes:
- the adversarial review findings of the batch, by category (the review reports, or `docs/review-log.md`);
- every step that needed a person (an approval, a production apply, a permission change);
- anything an agent worked out by reasoning that an earlier agent had also worked out;
- the commands agents wrote by hand more than once in the batch, grouped by what they did;
- `.lean/lessons.tsv` in each repository, if it exists: one line per lesson (date, commit sha, subject). Take the lines in the batch's window as facts.

## 2. Check the last change first

Read the previous retro's accepted changes (the method's history, or `docs/retro-log.md`). For each, find the number it said should move. Did it? If not, or it moved the wrong way, propose reverting it, with the numbers.

## 3. Apply the fixed triggers

| Signal | Proposal |
| --- | --- |
| A finding category in two or more changes | A scripted check, a Risks line in the ticket shape or a critic question |
| A check or critic question with no findings for three batches | Drop it for that tier |
| Reasoning repeated across agents | A script, with a test |
| A hand-written command family used in 3 or more projects in a week | A ticket for a script that covers it, with the count as evidence |
| A family whose covering script exists but is still hand-rolled | The method does not name the script: add it to the agent brief |
| A step that needed a person in two or more changes | Automate it or script it with confirmations |
| Fix rounds or cost per change rising | Name the step that grew and a change to it |
| Wasted effort over 15% of the batch (context carried too long, lookups done by the coordinator, errored tool calls, wait loops) | A method change aimed at that signal: end batches earlier, send lookups to a search agent, a script for the errored calls, or a notify instead of a wait loop. Name the share that should fall and by when |
| An escaped defect | A check that would have caught it, proven against that defect |
| Reviews not logged | Make logging part of the review routine |

Propose nothing a trigger does not support. If no trigger fires, the retro says so; that is a valid result.

## 4. Write the proposal

One short file, in the owner's style (plain, short, no em or en dashes, no arrow glyphs, no marketing words), with:
- the facts table from step 1, trimmed to what the proposals use;
- the result of step 2;
- each proposal as: what changes, where (section, script or file), the signal that triggered it, and the number that should move if it works, by how much, by when (which future batch).

## 5. After approval

For each approved proposal, open one pull request to the method. A new script goes where its dependencies are: one that works on any repository is a pull request to the method's repository (with a test and a line in the scripts list); one tied to a repository goes to that repository's `scripts/`, with any general part also offered to the method. Then update memory and the CLAUDE.md layers that summarise the method, and append a line to `docs/retro-log.md`: date, proposal, the number to watch. Rejected proposals are logged too, with the reason, so they are not proposed again without new evidence.

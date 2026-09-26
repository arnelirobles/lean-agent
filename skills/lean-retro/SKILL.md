---
name: lean-retro
description: Run lean agent's retro after a batch of changes merges. Collects facts with retro-signals.sh, checks them against fixed triggers, checks whether the last method change helped, and proposes changes to the method with evidence for a person to approve. Use it at the end of every batch, weekly at the latest, and whenever asked for a retro, a method review or "what should we change about how we work".
---

# Lean retro

The method improves only through this routine, and only with a person's approval. You propose. You never change the method, a script, a CLAUDE.md or memory on your own.

## 1. Collect facts, do not reason yet

Run, for the batch's window and every repository it touched:

```bash
REVIEW_LOG=<path to review-log.tsv, if there is one> ${CLAUDE_PLUGIN_ROOT}/bin/retro-signals.sh --since <YYYY-MM-DD> owner/repo ...
```

Also gather, from the session or the coordinator's notes:
- the adversarial review findings of the batch, by category (the review reports, or `review-log.tsv`);
- tokens or cost per change if known (`workflow-cost.py` for workflow runs, agent usage lines otherwise);
- every step that needed a person (an approval, a production apply, a permission change);
- anything an agent worked out by reasoning that an earlier agent had also worked out.

## 2. Check the last change first

Read the previous retro's accepted changes (the method's history, or `retro-log.md` beside the review log). For each, find the number it said should move. Did it? If not, or it moved the wrong way, propose reverting it, with the numbers.

## 3. Apply the fixed triggers

| Signal | Proposal |
| --- | --- |
| A finding category in two or more changes | A scripted check, a Risks line (section 0) or a critic question |
| A check or critic question with no findings for three batches | Drop it for that tier |
| Reasoning repeated across agents | A script (section 4a), with a test |
| A step that needed a person in two or more changes | Automate it or script it with confirmations |
| Fix rounds or cost per change rising | Name the step that grew and a change to it |
| An escaped defect | A check that would have caught it, proven against that defect |
| Reviews not logged | Make logging part of the review script |

Propose nothing a trigger does not support. If no trigger fires, the retro says so; that is a valid result.

## 4. Write the proposal

One short file, in the owner's style (plain, short, no em or en dashes, no arrow glyphs, no marketing words), with:
- the facts table from step 1, trimmed to what the proposals use;
- the result of step 2;
- each proposal as: what changes, where (section, script or file), the signal that triggered it, and the number that should move if it works, by how much, by when (which future batch).

## 5. After approval

For each approved proposal, open one pull request to the method. A new script goes where its dependencies are: one that works on any repository is a pull request to lean-agent-method (with a test and a line in section 4's list); one tied to a repository goes to that repository's `scripts/`, with any general part also offered to the method. Then update memory and the CLAUDE.md layers that summarise the method, and append a line to `retro-log.md`: date, proposal, the number to watch. Rejected proposals are logged too, with the reason, so they are not proposed again without new evidence.

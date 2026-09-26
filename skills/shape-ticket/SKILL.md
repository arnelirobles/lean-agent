---
name: shape-ticket
description: Write or refine an issue in lean agent's agent-ready shape, after searching for an existing issue it belongs to. Use when filing an issue, turning review findings or a plan into tickets, consolidating related issues, or asked to make a ticket ready for an agent.
---

# Shape a ticket for an agent

A ticket is read by an agent that pays for every file it opens. Write it so the agent needs nothing else, sized to one agent pass, with the review's questions answered before anyone builds.

## 1. Search before filing

Search the repository's open issues for the same code area or problem (`gh issue list --search`, by title words, paths and labels). If one fits, the new item joins that issue: add it to its Covers checklist with its own check under Done when. File new only when it is truly separate. Never file across repositories; one ticket per repository.

## 2. Size it to one agent pass

Everything one agent can finish in one focused pass (one pull request or a short stack, the same area or fix pattern, reviewable together) is one ticket. Split only when it is too big for one pass. Security fixes stay separate unless they are the same fix.

## 3. Write it in this shape

```
## Goal
One sentence: what is true when this is done.

## Where
Files and code areas, as paths at the default branch.

## Covers
- [ ] repo#n: one line
- [ ] ...

## Done when
- Tests that fail before the change and pass after (name them)
- The repository's gate command passes (its preflight)
- Docs updated: which files

## Risks
Only the lines that apply, each with a check under Done when:
- Untrusted input reaching a URL, header, CSS, HTML, SQL or log: test these inputs: ...
- Bounds (rows, calls, memory, time, cache) and what happens past them, never silently
- Writes that must commit together; what a crash midway or a second run does
- Two runs or writers overlapping
- Existing dependents: published packages, stored data, defaults, contract version
- The real target environment (ownership, network, versions), not only a copy

## Constraints
The repository rules that apply.

## Out of scope
What not to touch.
```

## 4. Spec review for bigger tickets

For a ticket that touches more than one area or anything in Risks, have one fresh agent read the ticket and the code it names and list what could go wrong. Add what it finds to Risks and Done when. It reads; it does not build.

## Style

Plain and short. Link only to what the agent must read. No filler.

# The lean agent method

How I run AI coding agents over a ticket backlog without burning a plan in a night. Written after doing exactly that, on my own project, in September 2026. The story is in the article; this is the method.

The numbers here are mine, at list prices, on one .NET codebase. Treat them as a shape, not a benchmark.

## The short version

A cheaper model drafts every change. Scripts, not instructions, run the mechanical checks. A cheap critic reviews every change against six fixed questions. The expensive model only sees what the critic cannot close. At most four changes in flight.

That took me from about 66 dollars of model use per change to about 25, with no drop in what got caught.

## 1. Triage inline, no agents

Read the open issues once, yourself. Sort each into a tier and do the cheap ones by hand.

| Tier | What |
| --- | --- |
| 0 | docs, tests only, config, CI files, README sweeps |
| 1 | a feature inside one module, additive |
| 2 | core, auth, tenancy, data shape, anything reachable without a login |

Close duplicates and anything already solved by other means while you are in there. Do not file follow-ups. A review finding is fixed in the change or dropped. A backlog that grows while you work is the failure this whole method exists to prevent.

## 2. The cascade

The cheap model drafts every tier. Before a change is opened, the preflight script runs the holdout check: the change description names the production hunk each new test depends on, the script holds that hunk out and rebuilds, and the named tests must fail. A test that passes either way is a gate failure, not a nit.

Read section 10 before you trust that paragraph. It described a script I had not written for two weeks, and the critic below answered question 1 yes over a revert that never happened.

**It only answers the question when the hunk modifies a file that already existed.** A file the change adds outright is one hunk covering the whole file, so holding it out deletes the file and nothing compiles, and the run ends inconclusive having proven nothing. I found that on the first real branch I pointed it at: ten production files, all of them new, every binding inconclusive by construction. On the twelve changes merged before it, seven touched no production code at all and four of the remaining five modified existing files, so the case it handles is the common one. That is a measurement on one repository, not a law.

A second cheap agent, fresh context, reviews every change in a checked-out copy it can build and run. Six questions, each answered yes or no with the line that proves it:

1. Did the holdout check run, and is its output on the change? Not "was the hunk reverted", which an agent can answer from the description alone.
2. Is every new route in every inventory, with the gate the issue asked for?
3. Does every session and raw query in the change name its tenant?
4. Does every new document, event or index have a migration or an upcaster?
5. Does anything from an exception, a request or a user string reach a log or a response unexamined?
6. Does any read-then-write on a shared row, or any outbound call to a URL that is not a constant, name its lock or its bound?

The expensive model takes the branch and the critic's list only when a no is not fixed in one round, or when a tier 2 answer is unsure. It does not start over.

Your continuous integration and whatever code review bot you use still run. They confirm. They do not decide.

## 3. The diff decides whether a critic runs, not the ticket

Tier from the ticket title is wrong, because the tickets that read as small are the ones that touch auth. `needs-review.sh` prints every rule a change fires; any output means a critic runs.

The rules in the script are specific to my codebase. The categories transfer: authentication and permissions, anything anonymous, raw SQL, schema and migrations, secrets and logging, concurrency and background work, deletion and retention, build and dependency files, and a test file that removes assertions. Docs, additive fields and assertion-only test additions fire nothing.

## 4. Scripts, not instructions

Four scripts do what I used to write into every agent brief. They live in the repository and are public:

- `scripts/preflight.sh` builds, runs the named test classes, checks the changelog and module versions, restores in locked mode before building so a stale lock file cannot pass, fails when a test filter matches zero tests, scans added lines including untracked files for house style, and parses any changed workflow file with a duplicate-key-rejecting parser.
- `scripts/sync-master.sh` merges the default branch, regenerates lock files when a project file changed, and reports conflicts.
- `scripts/needs-review.sh` prints the rules above.
- `scripts/strip-asset-provenance.py` removes embedded provenance metadata from images, and fails the build in `--check` mode if any is left. Section 9 is why.

They are in [BaryoDev/barakoCMS](https://github.com/BaryoDev/barakoCMS) under `scripts/`. Copy the shape, replace the checks with your own.

One trap worth inheriting along with the shape: `needs-review.sh` takes no arguments and diffs the working tree, folding untracked files in as new. That is deliberate, because a rule that only reads committed changes misses the file an agent has written and not yet added. It also means running it in a checkout full of other work walks all of that too. Run it inside a clean copy of the branch. I lost a few minutes to this before writing it down.

An instruction in a prompt is forgotten within a day. A script is not, and it costs no tokens to obey.

## 5. Send work back to the agent that did it

A review finding, a failed build or a conflict goes back to the agent that wrote the change, not to a new one. It still holds the context. A fresh agent re-reads the codebase to reach the same conclusion, and pays for the reading.

I measured this rather than assuming it. Resuming a drafter to make a two-line fix its reviewer had asked for cost about 4 thousand tokens. The same agent's first pass, which included reading the repository, had cost 154 thousand. The saving is not marginal.

There is a second reason, which matters more than the money. The agent that wrote the change knows what it already checked. A new one does not, so it either re-checks everything or, more often, assumes the first agent got the basics right and looks only at what you pointed it at.

## 6. Four changes in flight

The merge queue takes one change at a time and re-tests each against everything before it. Ten in flight means every later one gets brought up to date, re-tested, and sometimes repaired. That rework costs more than the parallelism saved.

## 7. Measure every batch

`workflow-cost.py` reads the per-agent transcripts a Claude Code workflow leaves behind, sums tokens by model, prices them, and prints cost per change against a baseline.

```
python3 workflow-cost.py <run-id> --prs 4 --baseline-per-pr 66
```

Point it at a run directory or a bare run id. Prices are list prices in the `PRICES` table at the top; edit them to whatever you actually pay. This and `asset-provenance.py` are the two files here you can use unmodified.

After each batch, compare two things: cost per change, and what the critic caught versus what got past it. When the critic misses something, the fix is a new scripted check, not a more expensive critic. When a critic finds nothing on a tier for three batches, drop it from that tier.

## 8. Attack your own method

My first version of this escalated to the expensive model only when the automated checks failed. I tested that against the twelve real defects the expensive reviewers had caught that week, and every one of them was, by definition, something the checks had not noticed. The rule would have called in the strong model zero times for the things that mattered.

That is why the critic runs on every change now, and why the holdout check was written. Run your own version of that test before you trust any of this, and note that attacking the method is what eventually found that the holdout check had never run at all.

Be honest about the ceiling while you are at it. Nine of those twelve defects would still pass the holdout check, because a check cannot see what a test does not test. It closes the other three, the tests that pass either way. It does not replace the critic.

## 9. Your checks read text. Half of what an agent makes is not text.

A change that swapped the icons on fourteen published packages went through the cascade above and came out clean. Every script passed. The reviewers then found that each exported image carried an embedded provenance manifest naming the tool that generated it, signed, about five and a half kilobytes of it, and that the build packed those images into every package. It was one merge away from being published under my name on a public registry.

Nothing in the method could have caught it. The house style scan reads the text of a diff. An image is not text, so it was a blind spot by construction, not by oversight. Every check I had was pointed at the half of the output I could read.

Three things came out of it that transfer to any project where agents produce files rather than only code.

**Gate the bytes.** A script now walks each image and fails the build if it finds provenance metadata: in a PNG that is an optional named chunk, in an SVG a metadata element holding a signed blob, in a JPEG a segment near the front. Detection is cheap. `strings file.png | grep -i c2pa` is enough to tell you whether you have this problem right now, and most people who generate assets do.

`asset-provenance.py` in this repo is that script. It walks a whole tree, parses each container, and exits 1 if it finds anything that is not picture data: a C2PA manifest or a text chunk in a PNG, an EXIF, XMP or C2PA chunk in a WebP, an APP1 or APP11 segment in a JPEG, a metadata element or an editor namespace in an SVG, and bytes appended past the end of any of them. A PNG chunk it does not recognise counts as a finding too, so a provenance format that does not exist yet still trips it.

```
python3 asset-provenance.py .            # scan, exit 1 on a finding
python3 asset-provenance.py . --strip    # remove what it found, in place
```

Put it wherever your other checks run. In a Node project that is one line in each of two scripts:

```
"test":     "... && python3 asset-provenance.py .",
"prebuild": "... && python3 asset-provenance.py ."
```

**Filter, do not re-encode.** The obvious fix is to run everything through an image tool with a strip flag. That works and it rewrites every pixel, which changes the file hash. If any of your evidence is a hash, and mine was, you have just invalidated it and you will not notice. Dropping the optional chunks and leaving the rest alone keeps the image data identical byte for byte. Verify it: parse the result, check the checksums, compare the compressed image stream before and after.

`--strip` copies the chunks that are the picture and drops the rest, so the compressed stream is never rewritten. It proves that before it writes: it hashes the image data before and after and refuses the write if they differ. Planting a 5.5 KB C2PA manifest into a real shipped asset and stripping it again returned a file byte for byte identical to the original. SVG is the exception. It is text, so the script reports it and leaves the edit to you rather than guessing at your XML.

**Scan the whole repository, not the diff.** I pointed the new check at every asset rather than only the changed ones. It immediately found a file that had carried a screenshot's camera metadata since the day it was committed, months earlier. A check scoped to the diff would never have seen it, and that is true of every check scoped to the diff.

The general form: list what your agents produce that your checks cannot read. Images, generated documents, lock files, fixtures, anything binary. That list is your exposure, and it is invisible in exactly the way that matters, because everything looks green.

One process note, since it nearly cost me the fix. The change was in the merge queue when I found this, and a queued branch cannot be pushed to. I had to pull it out of the queue first. Whatever your equivalent is, know how to do it before you need to, because the window is however long the queue takes.

## 10. A check that passes having checked nothing

Every recurring failure I have had this year is one shape. Not a check that fails when it should
not. A check that reports success without having checked anything, because silence and success look
identical and nobody investigates a pass.

I had two in one afternoon, on a release that was otherwise ready.

**The preflight script passed having run no tests.** It takes the test classes to run as arguments.
Given none, it did the restore, the build and every file scan, then printed `all checks passed`. I
read that line, believed it, and pushed. CI caught the real problem eighteen minutes later.

What makes this worth writing down is that the same script already refused the identical hole one
level down: a named class matching zero tests was treated as a failure, with a comment explaining
that a filter matching nothing still "finishes cleanly, exit 0". The reasoning was right and was
applied one level too shallow. The fix is six lines.

```bash
if [ ${#classes[@]} -eq 0 ] && [ "$no_tests" -eq 0 ]; then
  echo "no -class given, so no test would run and this would pass having tested nothing."
  exit 1
fi
```

**Nothing compared three files that pin the same version.** A release moves a version in one file
and the others are forgotten. An integration test caught it, correctly, eighteen minutes into CI.
The test was doing its job. Nothing was doing the job of saying so before the build.
`check-pinned-values.sh` in this repository is the general form: give it file and capture pairs and
it fails with a diff.

Note what I did not do. The test that caught it pins the version as a literal on purpose, so that
bumping the constant is a conscious act rather than something carried along automatically. Deleting
it to stop the noise would have removed the only thing that noticed. The new check runs earlier; it
does not replace it.

**The general form.** For every check you own, ask what it does when handed nothing to do. Not what
it does when it finds a problem, which is the case you designed and tested. A test runner given an
empty filter. A linter given no files. A scanner pointed at a directory that moved. A comparison
where both sides are empty strings, which are equal. Each of those exits 0, and an exit code of 0
is the only thing anyone reads.

Then make it fail before you trust it. Revert the fix and watch the gate go red, or feed the check
the exact mistake it exists to catch. Both of the gates above were written that way, and testing
the empty-capture case is what found that two unreadable files would have compared equal and passed.
That is the bug this whole section is about, sitting inside its own fix.

**The worst one I have had was a check that did not exist.** Section 2 above has described a
mutation check since early September: name the production line a test depends on, revert it, the
test must fail. Question 1 of the critic's list asked whether that had happened. For two weeks
critics answered yes.

There was no script. The preflight file had no revert step and no hunk parsing, and not one of the
last forty merged changes carried a binding. The check I wrote to catch gates that cannot fail was
one, and it had the strongest possible output: a reviewer affirming it in writing, on every change.

Two things in that are worth more than the fix.

**An item phrased as a fact can be answered from the description.** "Was the mutation reverted" is
a claim about the world that a reader satisfies by believing the change description. "Did the
holdout check run, and is its output here" names an artefact that either exists or does not. Phrase
every checklist item as the second kind. If an item can be satisfied by an opinion, it will be.

**Nothing found it. A person did**, reading the script against the document that described it. When
the answer to "what caught this" is a name rather than a mechanism, the mechanism is the finding.

The replacement holds a hunk out in a throwaway copy, rebuilds, and requires the named test to
fail, then restores and requires it to pass. One decision in it matters more than the rest:
**a held-out tree that does not compile is inconclusive, not a pass.** Reverting one hunk of a typed
language usually breaks the build, and a tree that does not build runs no tests. Score that as
success and the gate is green forever over a check that never ran, which is this section's bug
reappearing inside the fix for this section's bug. It also fails on an unclaimed hunk, so a change
that tests nothing has to say so in writing next to the diff rather than simply staying quiet.

It ships with a fixture suite that runs a known one-hunk change past eleven cases, every failure
path included, and preflight fails if any of them stops returning its exit code.

**What running it has actually shown, after three changes.** It cost 24 to 34 seconds per binding,
two builds and two test runs included, where I had expected minutes and had written down slowness
as the thing most likely to kill it. That is one repository on one laptop, and integration tests
that need containers will cost more.

It caught one false binding: a line I added that could never execute, with a test bound to it that
passed either way. One catch on the only branch where the question could be put honestly. The other
two runs ended inconclusive for reasons that say nothing about the idea, one because every
production file on that branch was new, one because Docker was stopped and the bound class needed
containers.

Running it also found three defects in itself and one thing it had never supported. It resolved the
repository from its own file path, so a copy run from anywhere else reported a git error for what
was a path bug. It spent two builds discovering that a whole-file hunk cannot be held out, which the
diff header says for free. It reported a stopped Docker as a broken branch. And it never parsed the
`none:` declaration, although the change that introduced it put `none:` in its own description. Each
of those came from use, not from review.

Three changes is not twenty, so the number that would make me delete it still stands: twenty with
no catch, or more than half coming back inconclusive.

## 11. The machine has a ceiling too

Section 6 caps changes in flight because of the merge queue. There is a second ceiling, and it is
the machine the agents run on.

On 13 September 2026 I had thirteen agents running at once on a three core, 16 GB box. Every brief
was reasonable on its own. Five of them were building the same .NET solution and starting Postgres
containers for integration tests; the rest were installing npm packages and driving Playwright.
Load average reached 44. The same box serves live sites. They held up, the static site still
answered in about a tenth of a second, but that was down to what those sites are, not to any plan.

Nothing in this method said how many agents a machine can carry, because I had only ever measured
tokens. Cost per change looked fine. The cost that was climbing was not on the bill.

**Check before you fan out.** `nproc` and `uptime`, before the first agent starts, not after the
fans do.

**Queue the heavy commands, not the agents.** Reading code and writing a change is cheap. Compiling,
installing packages, starting containers and running a browser is what saturates a machine. So let
every agent run, and put only those commands in a queue: one lane per toolchain, one command per
lane. A .NET build and an npm install can overlap; two builds cannot.

**Run agent work at the lowest priority.** `nice -n 19` means a live service wins every contest for
CPU. For processes already running, `renice` does the same without restarting anything.

**Stop build servers from lingering.** MSBuild keeps worker nodes alive after a build for the next
one. With several agents that is a dozen idle processes holding memory. `-nodeReuse:false` makes
them exit.

After lowering the priority of what was running and putting the builds in a queue, load roughly
halved within a few minutes. Two agents finishing in that window helped, so I will not claim the
whole drop for the fix.

The mistake worth naming is how I first applied it. I sent the rule to twelve running agents as a
message. That is the instruction form section 4 warns against: it has to reach every agent, and the
next one I start will not have it. `heavy.sh` in this repository is the script form.

```
bash heavy.sh dotnet dotnet build MySolution.sln -nodeReuse:false
bash heavy.sh node   npm ci
```

It takes a lane lock with `flock`, runs the command under `nice -n 19`, says so when it is waiting,
and fails when handed no command instead of taking the lock and exiting 0. Name it once in the
agent brief; the queue does the rest.

The first version of that script had a bug the same night. It took the lock with a plain `flock`,
so every process the command started inherited the lock. `dotnet build` starts the Roslyn compiler
server, which stays up for minutes after the build exits, and it kept the lock the whole time. Four
agents waited about ten minutes on a lane where nothing was running. `flock -o` closes the lock
before the command starts, so only the command itself holds it. The test that proved it: a job
that leaves a 6 second child running made the next job wait 6.0 seconds before the change and 0.0
after, while two jobs in the same lane still took 4.0 seconds for two 2 second sleeps.

The general form is the one from section 10. A lock that is released when its holder exits is only
as reliable as your knowledge of every process that can become its holder.

## 12. The critic is a skill, and it argues with itself

The critic in section 2 is now a Claude Code skill: [`skills/adversarial-review`](skills/adversarial-review/SKILL.md). Copy the folder into `.claude/skills/` in a repository, or into `~/.claude/skills/` to use it everywhere.

Two changes made it better than the brief I used to paste.

**The finder is never the judge.** One fresh agent hunts for defects against the six questions, a contract question and the repository's own rules file. A second fresh agent, which never sees the first one's reasoning, tries to prove each finding wrong: it looks for the guard elsewhere, confirms the change caused it, and runs the smallest check that settles it. Only survivors are reported. A reviewer that scores its own findings keeps the confident ones, and confident is not the same as right.

**The repository's rules come first.** A review bot does not know that a stricter validation is a breaking change here, that a console refuses an API whose contract version it does not list, or that nothing hashed from a secret may sit in a publicly readable record. On one night in September 2026 a bot caught a stale cache timestamp, a webhook replay check in the wrong order and a missing timeout, and missed two pull requests that would have locked every released console out of the next API. The critic caught those two because the rules file lists each consumer and the version range it accepts. Keep a `docs/review-rules.md` per repository; `review-rules.example.md` shows the shape.

Run both. The bot is good at general stability and correctness patterns; the critic is good at what only this codebase knows. `log-review.sh` appends one line per review so section 7's comparison has numbers, and a category the critic keeps confirming becomes a scripted check.

## What this does not fix

A backlog with no definition of finished refills faster than it drains. Automation widens the drain. It does not close the tap. Decide what done means first.

## Licence

MIT. Take it, change it, ship it.

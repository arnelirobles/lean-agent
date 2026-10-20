# Team brain

## Who it is for

Software teams, agencies and startups that build with coding agents because of the speed. They need two things the agents don't give them: one place to see what every developer and agent is doing, and one place where the team's decisions are made, agreed and kept. Humans and agents make decisions in the same place, through the same flow.

An agency runs several clients at once. Each client is its own scope with its own visibility label, and nothing from one client's scope may reach another client's repo, tracker or text.

## Why

Lean agent works for one developer on one machine. brain is a SQLite file in the home folder, the rules are enforced by Claude Code hooks, and every call to a code host goes through `gh`. A small team breaks each of those:

- Two developers shape and claim the same ticket, because neither brain can see the other's.
- Tickets live in Jira, GitHub issues, GitLab and Azure DevOps, and nothing links an agent's work back to the ticket or moves the ticket when the work is done.
- Some developers run Codex or Cursor. Those tools never run Claude Code hooks, so none of the rules hold for them.
- "Done" means the unit tests passed. QA reopens tickets because the real flow (log in, run a transaction) was never shown working.
- A fix in a public repo is also needed in a private fork, and private context (a client name, a private ticket key) leaks into public text.
- Each developer's heavy test runs queue on their own laptop and collide on shared remote hosts.
- Decisions ("we use conjoined tenancy", "no follow-up issues from review nits") sit in one person's markdown files. Teammates' agents cannot see them, and two developers can hold opposite rules without anyone noticing.

## Goals

- Every developer keeps a local brain that works offline and with no server, exactly as today.
- A local brain can sync to a central brain. With sync on, the team shares claims, item status, questions, decisions, evidence, queue jobs and cost.
- The rules hold in Claude Code, Codex and Cursor alike. Enforcement lives in the shared layer, git hooks and CI, and each tool gets a thin adapter.
- Tickets are read from and written back to Jira, GitHub, GitLab and Azure DevOps at fixed points.
- The team's constitution (base rules) and decisions are stored centrally. A decision that conflicts with an existing one becomes a proposal and binds only after the scope's approver approves it.
- Every claim, decision, approval, write-back and agent run is traceable: who, which tool and model, what it cost in tokens and dollars, when, and when it was agreed.
- Private context never reaches a public repo, PR, comment or tracker.
- A user-facing change can carry end-to-end proof that QA accepts without rerunning it.

## Non-goals

- A tracker of its own. Jira, GitHub, GitLab and Azure DevOps stay the system of record for tickets.
- Replacing CI. Lean agent's checks run inside existing pipelines.
- Running agents for people. Each developer starts their own agents in their own tool.
- Real-time collaboration (shared editors, live cursors).

## Anti-goals (a failure even if it works)

- A single developer who must run a server to keep using lean agent.
- A rule that only holds in one tool. If Codex can skip it, it is not a rule.
- An agent that approves its own decision, or any decision.
- A tracker write-back or public text that carries private context.
- A record that can be edited in place. History is appended, never rewritten.
- Credentials in brain, evidence, logs or screenshots.

## Architecture

```
  Claude Code     Codex       Cursor      (any tool, any developer)
       |            |            |
   adapter      adapter      adapter      (plugin / AGENTS.md / rules; all call MCP or the CLI)
       \            |            /
        +--- brain CLI and MCP server ---+   git hooks + CI checks
                    |                         (hold regardless of tool)
              local brain (SQLite, event log)
                    |  sync (optional)
              central brain API
                    |
       tracker and forge adapters (Jira, GitHub, GitLab, Azure DevOps)
```

### Local brain

SQLite, as today. Two changes:

- **Storage, CLI and scheduling split apart.** `bin/brain` and `bin/test-phase` become a `lib/brain/` package (store, migrations, events, sync, queue) with thin CLIs on top. The MCP server and the CLI call the same functions.
- **An append-only event log.** Every change writes an event (`claim.requested`, `item.opened`, `decision.proposed`, `evidence.attached`, `agent.run`, ...). Today's tables become views that are rebuilt from the events. With sync off, nothing else changes.

### Event envelope

Every event carries as much of this as is known when it is written. A field that is not known is left empty, never guessed.

| Group | Fields |
| --- | --- |
| Who | developer id and name, `human` or `agent`, agent role (`draft`, `hunt`, `refute`, `gate`, ...), parent agent id |
| Tool | tool (`claude-code`, `codex`, `cursor`, `cli`), tool version, lean agent version, adapter version |
| Model | provider, model id, effort or reasoning level, model role (`fast`, `cheap`, `strong`) |
| Usage | input, output, cache read and cache write tokens; tool calls; USD at list price, with the price table version used |
| When | event time, start and end of the work it records, duration, time zone |
| Where | machine id, OS, session id (stays local), repo, remote, visibility label, branch, commit SHA |
| What | ticket (tracker and key), PR, item id, decision id, the constitution version in force |
| Outcome | result (`passed`, `failed`, `refused`, ...), exit code, reason |

- Usage is read from each tool's own records: Claude Code transcripts (counted once per message id, as `lean-stats` does), the Codex session log, and Cursor's usage export where it has one. A tool with no usage record logs the run with usage empty and `usage_source=none`.
- One `agent.run` event is written per agent pass, so cost per ticket, per developer, per tool and per model is a query, not a guess.
- Prompts and outputs are not stored, only their sizes. Raw transcripts stay on the developer's machine.

### Sync

- A local brain pushes its unsent events and pulls the team's events since its last cursor. It is configured with one URL and one token per developer, in `~/.config/lean/central.ini`.
- **Local only, never synced:** session ids, local folders, transcripts, and any item or decision marked `private`.
- **Claims are server-held.** A ticket claim counts only once the central brain accepts it, and a second claim on the same ticket is refused with the current owner's name. Offline, an agent can draft work but cannot claim.
- Everything else merges as events. Two edits to the same item both stay in history. The later one wins on the current state, and the conflict is shown in `brain show`.
- **Order and dedupe (my call).** Each event has a client-made UUID, and pushing the same id twice is a no-op. The server gives every accepted event one global sequence number. "Later" means a higher server sequence, not a later client clock, so clock skew cannot reorder history. The client's own time is still recorded.
- **Private and shared rules (my call).** A private decision that overlaps a shared rule never binds. It stays local, the agent follows the team rule, and `brain show` flags the clash so the developer can propose it properly.

### Central brain API

The public contract is HTTP and JSON, versioned under `/v1`:

- `POST /v1/events`, `GET /v1/events?since=`: push and pull.
- `POST /v1/claims`, `DELETE /v1/claims/{ticket}`: claim and release.
- `GET /v1/constitution`: the current rules for a scope.
- `POST /v1/proposals/{id}/review`: approve, reject or request changes, for approvers only.
- `POST /v1/evidence`: upload a proof bundle and get back its hash and link.

### Hosting

- Lean agent and the local brain are free and open for anyone, with no account and no server.
- Connecting a team (sync, claims, constitution, decisions, write-back, proof storage, the team dashboard) goes through one hosted service run by the lean agent author. `lean connect` signs a developer in and writes `central.ini` with the hosted URL by default.
- Sign-up starts as a waitlist. `lean connect` with no account (or the site form) adds the developer's email, team name and team size to the waitlist and says so. The host lets teams in from the waitlist by hand. The developer who joined becomes the team's first admin and approver, and invites the rest. Self-serve sign-up can replace the waitlist later without changing the client.
- The free tier has hard limits per team (seats, events per day, proof storage, proof retention), so one team cannot run up the host's bill. Paid tiers raise the limits. The limit values live on the server and are shown by `lean connect status`.
- A team admin can export everything as events, and delete the team, from the CLI or barakoBrew.

The client and the contract suite are public. The server is a private barakoCMS module.

The contract is normative and is written first (my call): a JSON Schema for every event and an OpenAPI file for `/v1`, in lean-agent, covering auth (bearer machine token), error codes, the pull cursor (the global sequence number), page size and idempotency. Step 3 starts with this contract and its suite, and both client and server are built against it.

The server, not the client, decides whether a decision overlaps (my call), so every decision event must carry a `scope` and a `rule_key`. The server answers a recorded decision with either `DecisionBound` or `DecisionProposed`.

### Identity and roles

- **Developer:** claims work, files items and proposes decisions.
- **Approver:** reviews proposals for the scopes the constitution assigns them, much like CODEOWNERS.
- **Admin:** manages identities, tracker connections and the constitution's approver lists.

An agent acts with its developer's identity plus an `agent` marker and the tool it runs in. It can never approve.

### Works in any tool

- The rules that block today (test before push, public text, commit author, merge blocked) move into `lean check <rule>` commands. A git `pre-commit` and `pre-push` hook (installed with `lean init`) and a CI job call them, so they hold whichever tool made the commit.
- The Claude Code plugin keeps its hooks, but they call the same `lean check` commands.
- **What counts as a rule (my call).** A rule blocks something at a boundary every tool crosses: commit, push, PR body, merge, tracker write-back. Those are the ones that must hold in every tool. In-session nudges that only Claude Code can give (`style-note`, `scratchpad-root`, `docs-containers`, `verify-claim`, `lesson-log`) are help, not rules. They stay Claude-only, and the same text goes into the Codex and Cursor instruction files.
- Codex gets an `AGENTS.md` block and the MCP server. Cursor gets a rules file and the MCP server. Each adapter only tells the tool where brain is and what the method says, and never re-implements a rule.

### Forge and tracker adapters

- **Forge** (code host): pull requests, reviews, checks, merge state and visibility. GitHub, GitLab and Azure DevOps. Every `gh` call in `bin/`, `hooks/` and `skills/` moves behind it.
- **Tracker:** read, assign, comment and transition tickets. Jira, GitHub issues, GitLab issues and Azure Boards.
- **Write-back points:**
  - on claim, assign the ticket and comment with the owner and branch;
  - when a PR opens, link it and its evidence;
  - when review passes, move the ticket to the team's review state (mapped per tracker in the constitution);
  - on merge, close the ticket or mark it done.
- Every write-back passes the privacy check before it is sent and is logged as an event.

### Constitution and decisions

- **Constitution:** the team's base rules, as versioned documents on the central brain, each with a scope (team, project, repo): method, review rules, house style, model roles, test policy, visibility labels, approver lists. Every agent loads the rules for its scope at session start, and the local brain caches them for offline work.
- Decisions are not only agents' rulings. A developer or lead records a decision from a meeting, a PR discussion or a ticket in the same way (`brain decide`, MCP, or barakoBrew), and it goes through the same proposal flow.
- **Decision:** a ruling made during work, with its scope, its reason, who made it and its source (session, PR, ticket).
- A decision inside the author's own scope that touches no shared rule binds at once.
- A decision that overlaps an existing rule or decision in the same or a wider scope becomes a **proposal**. It shows the old rule, the new one, the reason and what it touches. It binds only after an approver for that scope approves it. Until then agents follow the current rule and see that the proposal is open.
- Every change to the constitution itself is a proposal.
- A rejected proposal stays on record with the reason, and a new proposal that matches it is flagged.
- Overlap detection starts simple: the same scope and the same rule key. Text similarity is later work.

### Privacy boundary

- Every repo and tracker gets a visibility label in the constitution: `public`, `private` or `client:<name>`.
- Text bound for one client's repo or tracker is checked against every other client's markers as well, so one client's context never reaches another's.
- Before text goes anywhere `public` (commit message, PR body, issue comment, tracker write-back, release notes), `lean check public-text` compares it against the private labels' markers: client names, private ticket key patterns, internal hosts and URLs, plus a team deny list.
- A hit blocks the write and names the marker. The check runs in the shared layer, in `pre-push`, and in CI on PR bodies.
- Porting a fix from a public repo to a private fork (or back) is a recorded link between the two changes, with the commit text rewritten for the target's label.

### End-to-end proof

A user-facing ticket can require a proof bundle before it moves to review. The bundle holds:

1. The ticket's acceptance steps, each mapped to a step that ran. A step with no run shows as missing.
2. The environment URL, the commit SHA actually deployed there (checked, as `check-deployed-sha.sh` does), browser and viewport, and the test account's role (never its credentials).
3. Red then green: the original repro fails on the old build and passes on the new one.
4. One screenshot per step, with step name, timestamp, expected and seen. A Playwright trace or video goes with them.
5. Proof behind the screen: the transaction id and the record found through the API or database.
6. Console errors and failed network calls, which should be none or each one listed.
7. Redaction of tokens, passwords and customer data before upload.
8. The committed script that reruns it in one command, plus a hash of every file. The bundle is stored on the central brain and linked on the ticket.

The runner is Playwright first, and `lean proof run <ticket>` writes the bundle.

Size and retention (my call): a bundle is capped at 200 MB and uploaded in chunks. Videos and traces are kept 180 days. Screenshots, the step map and the hash list are kept for good.

### Dashboard

Anyone on the team, and the owner most of all, should be able to open one page and see what is happening. That page is the VS Code dashboard for a developer and barakoBrew for the team. Both read the same events.

Extra events recorded for it:

| Event | Records |
| --- | --- |
| `agent.heartbeat` | every 60 seconds while an agent runs: ticket, tool, model, tokens and USD so far, current step. Not part of the permanent log (my call): the server keeps the latest heartbeat per agent and drops it 7 days after the run ends. The final `agent.run` event is the permanent record |
| `ticket.state` | each move on the tracker (claimed, PR open, in review, QA, done, reopened) with time |
| `pr.state` | opened, review requested, approved, changes requested, merged, closed, with lines and files changed |
| `ci.result` | each pipeline run on a tracked branch: result, duration, failing job |
| `review.finding` | each critic finding: severity, confirmed or refuted, fixed in which round |
| `escalation` | a step that moved to the strong model, and why |
| `question.asked`, `question.answered` | a blocker waiting on a person, and how long it waited |
| `qa.verdict` | QA accepted or reopened, with the reason |
| `privacy.blocked` | a write stopped by the privacy check, with the marker that matched (not the text) |
| `budget.set` | a spend limit per team, project or developer, per day or month |
| `revert` | a merged change later reverted, linked to its ticket |

Views:

- **Now:** who is running what. Each active agent shows its developer, tool, model, ticket, elapsed time and spend so far. An agent with no heartbeat for 5 minutes shows as stalled.
- **Flow:** each ticket's time in each state, with lead time (claimed to done) and wait time (blocked on a person, waiting for review, waiting for a test lane). Work in progress per developer against the limit of four.
- **Waiting on people:** open questions, proposals waiting on an approver, and PRs waiting on review, each with its age. This is the owner's to-do list.
- **Quality:** critic findings per change, the refute rate, QA reopen rate, reverts, flaky tests, and changes that shipped with no proof bundle.
- **Cost:** spend per day, ticket, developer, tool and model against budget. Cost per merged change, the cost of abandoned work, and burn rate with the date the budget runs out at that rate.
- **Lanes:** test lane use, queue length and wait time per lane.
- **Rules:** recent constitution changes, open proposals, and decisions made this week.
- **Risks:** stale claims, PRs open past 5 days, red CI on the default branch, privacy blocks, and budget past 80%.

Every number on the dashboard links to the events behind it.

### Team queue and rollups

- A queue job can target a team lane. Remote hosts are registered on the central brain, and one lane claims one job at a time across the team, with the same claim rule as tickets.
- `lean-stats` rolls up cost, tokens, time and outcome per ticket, per developer, per tool and per model, from the `agent.run` events.

## Delivery order

Each step is its own project with its own spec and plan, shipped in this order.

1. **Groundwork.** Split brain into `lib/brain/`. Put the `gh` reads of merged pull requests behind a forge adapter, with GitHub only at first. The 20 links at the repo root stay, so the scripts work without the plugin. test-phase and the hooks that parse `gh` commands move in steps 2 and 8. No behaviour change, and the existing self-tests stay green.
2. **Works in any tool.** `lean check`, `lean init`, git hooks, the CI job, and the Codex and Cursor adapters.
3. **Event log and central brain.** Local events, the sync client, the `/v1` contract and its test suite. The private barakoCMS module ships in step with this.
4. **Trackers with write-back.** GitHub issues first, then Jira, GitLab and Azure Boards.
5. **Constitution and decisions.** Scoped rules, proposals, approvers, the audit trail.
6. **Privacy boundary.**
7. **End-to-end proof.**
8. **Team queue and rollups.**

Dashboard views are built in the step that first emits their events (my call). Step 3 ships Now, Cost and Rules. Flow and Waiting on people come with trackers, Quality with proof, Lanes with the team queue.

## Left to the builder

- Event names and payload shapes, as long as each event is append-only and carries actor, tool, machine and time.
- How the views are rebuilt from events (on write or on read).
- The adapter interface's exact methods.
- Where proof bundles are stored before upload.

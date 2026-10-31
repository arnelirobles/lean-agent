# Ask the brain

An optional section at the top of the brain page. You ask a question about your local brain database and get a streamed answer from Claude. It uses your own Anthropic credentials. If it is not set up, the section only shows the setup command.

Claude.ai, Pro and Max logins cannot be used here. Anthropic does not allow them in third-party apps, so this needs an API key or an `ant auth login` profile.

## Setup

1. Run `brain assistant-setup`. It creates a virtual environment named `assistant-venv` in the folder that holds `brain.db` (it follows `LEAN_BRAIN_DB`) and installs the `anthropic` package there. Nothing is written into the repository. Homebrew python refuses plain `pip install`, which is why it uses a venv. If you upgrade python to a new minor version, run the command again. The assistant needs Python 3.11 or later, because older versions cannot cap query size.
2. Give it credentials (below).
3. Restart `brain serve` if it was running, and reload the page.

## Credentials

The first match wins, in this order:

1. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the environment `brain serve` runs in. The SDK resolves it.
2. A key saved from the page. On macOS the form stores it in the Keychain under the service `lean-brain-anthropic`. It is passed to `security` on stdin, never on a command line. "Remove the saved key" deletes it. On other systems the form is hidden: set `ANTHROPIC_API_KEY` instead.
3. Whatever the SDK finds on its own, such as an `ant auth login` profile. brain does not read the files `ant` keeps.

The key is never written to `brain.db`, logs, the page, a redirect or an error message.

## What leaves your machine

Your question, the table definitions of the brain database, and every row the assistant selects while answering go to Anthropic under your account. Anything that looks like a key or token (the patterns in `tools/secret-scan`, plus `sk-` keys) is replaced with `[secret removed]` before rows are sent. Masking matches whole values, so it catches accidents, not a query built to split or encode a value; do not rely on it to keep a secret stored in the brain. Row text is treated as data, never as instructions, but it is still text from your own work items, so do not ask about a brain you would not send to Anthropic.

## What it can do

Two tools, `run_sql` and `chart`. It runs a single SELECT on a read-only connection with the sqlite authorizer set to allow only reads. INSERT, UPDATE, ATTACH, PRAGMA and stacked statements are refused. A query stops after 5 seconds. A result is cut at 200 rows or 40,000 characters, and the cut is reported so the model can narrow the query. The assistant never writes to `brain.db`.

`chart` draws a bar, line or horizontal bar chart from numbers the model gives it, as inline SVG with a table under it. The spec is checked before drawing (at most 3 series, 60 labels, finite numbers only) and a refused spec is reported back to the model. No JavaScript runs on the page.

## Activity and summaries

Activity and the summary buttons sit on the Overview tab. The Ask form is on the Ask tab, and a streamed answer links back to it.

The Activity section shows 14 local days of counts from `brain.db` (test runs and failures, items opened and closed, things agents created) and from `lean.db` when it exists (agent runs and cost). A day with no data shows as zero. If `lean.db` is missing, those charts are left out.

With a key ready, the section has two buttons, Summarise today and Last 7 days. Each asks the assistant the same fixed question (what shipped, what failed, what got worse against the prior period, what needs you) and streams the answer like any question, so follow-ups work. The result is saved in `summaries.json` next to `brain.db`, one entry per range, mode 0600. It holds the answer text and chart specs, never HTML, and the page draws them again on each load. The text comes from the brain and is masked the same way as rows, but it can still hold work item text, so treat the file like the brain itself. A corrupt or oversized file is ignored. The newest summaries show at the top of the section.

Set `LEAN_ASSISTANT_AUTO_SUMMARY=1` before `brain serve` to write the daily summary on its own. A thread checks every 10 minutes. If no summary has been written since local midnight and a key is ready, it makes one, but only when a question slot is free. A failure shows as a muted line on the page and is retried at most once an hour. It is off by default, and each summary costs a normal question.

## Models and effort

Opus 5.5 (default), Fable 5.1, Sonnet 5.5 and Haiku 5.5. Effort is medium, high (default), xhigh or max. All models except Haiku are sent with server-side refusal fallbacks (`fallbacks: "default"`).

## Limits and cost

- At most 30 tool turns per question.
- A dollar cap per question, `LEAN_ASSISTANT_MAX_USD`, default 10. It is checked between turns, so one turn can pass it. Hitting a limit ends the answer with a line that says which.
- Closing the tab stops the question at the next line written to it. The turn in progress finishes and is paid for.
- Each API request times out after 120 seconds of silence, with two retries. A question stops after 30 minutes.
- Two questions can run at once. A third gets a refusal.
- A conversation holds 150 messages, then refuses follow-ups.
- Follow-ups keep their history in memory only, for the last 20 conversations. Restarting `brain serve` clears them.

Costs are estimates from a price table in `lib/brain/web/assistant.py` (per million tokens: Opus 4/20, Fable 10/50, Sonnet 2/10, Haiku 0.10/0.50, cache writes 1.25x input). After each question one line is appended to `assistant-usage.jsonl` next to `brain.db`: time, model, token counts and estimated dollars. It never holds the question or the answer. The page shows the running total.

## Page security

The page has no JavaScript and its content security policy is unchanged. The answer is an HTML document streamed over the same connection, with every piece of model output escaped. Answers render as formatted text (headings, lists, tables, code) a block at a time, and links in answers are shown as plain text and never clickable, because answers can quote text stored in the brain. The Host, Origin, content type and per-start key checks apply to `/ask` and the key routes like the other actions.

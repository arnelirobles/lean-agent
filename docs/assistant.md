# Ask the brain

An optional section at the top of the brain page. You ask a question about your local brain database and get a streamed answer from Claude. It uses your own Anthropic credentials. If it is not set up, the page behaves as before.

Claude.ai, Pro and Max logins cannot be used here. Anthropic does not allow them in third-party apps, so this needs an API key or an `ant auth login` profile.

## Setup

1. Run `brain assistant-setup`. It creates a virtual environment named `assistant-venv` in the folder that holds `brain.db` (it follows `LEAN_BRAIN_DB`) and installs the `anthropic` package there. Nothing is written into the repository. Homebrew python refuses plain `pip install`, which is why it uses a venv. If you upgrade python to a new minor version, run the command again.
2. Give it credentials (below).
3. Restart `brain serve` if it was running, and reload the page.

## Credentials

The first match wins, in this order:

1. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the environment `brain serve` runs in. The SDK resolves it.
2. A key saved from the page. On macOS the form stores it in the Keychain under the service `lean-brain-anthropic`. It is passed to `security` on stdin, never on a command line. "Remove the saved key" deletes it. On other systems the form is hidden: set `ANTHROPIC_API_KEY` instead.
3. Whatever the SDK finds on its own, such as an `ant auth login` profile. brain does not read the files `ant` keeps.

The key is never written to `brain.db`, logs, the page, a redirect or an error message.

## What leaves your machine

Your question, the table definitions of the brain database, and every row the assistant selects while answering go to Anthropic under your account. Anything that looks like a key or token (the patterns in `tools/secret-scan`, plus `sk-` keys) is replaced with `[secret removed]` before rows are sent. Row text is treated as data, never as instructions, but it is still text from your own work items, so do not ask about a brain you would not send to Anthropic.

## What it can do

One tool, `run_sql`. It runs a single SELECT on a read-only connection with the sqlite authorizer set to allow only reads. INSERT, UPDATE, ATTACH, PRAGMA and stacked statements are refused. A query stops after 5 seconds. A result is cut at 200 rows or 40,000 characters, and the cut is reported so the model can narrow the query. The assistant never writes to `brain.db`.

## Models and effort

Opus 5.5 (default), Fable 5.1, Sonnet 5.5 and Haiku 5.5. Effort is medium, high (default), xhigh or max. All models except Haiku are sent with server-side refusal fallbacks (`fallbacks: "default"`).

## Limits and cost

- At most 30 tool turns per question.
- A dollar cap per question, `LEAN_ASSISTANT_MAX_USD`, default 10. It is checked between turns, so one turn can pass it. Hitting a limit ends the answer with a line that says which.
- Closing the tab stops the question, so no more is spent.
- Follow-ups keep their history in memory only, for the last 20 conversations. Restarting `brain serve` clears them.

Costs are estimates from a price table in `lib/brain/web/assistant.py` (per million tokens: Opus 4/20, Fable 10/50, Sonnet 2/10, Haiku 0.10/0.50, cache writes 1.25x input). After each question one line is appended to `assistant-usage.jsonl` next to `brain.db`: time, model, token counts and estimated dollars. It never holds the question or the answer. The page shows the running total.

## Page security

The page has no JavaScript and its content security policy is unchanged. The answer is an HTML document streamed over the same connection, with every piece of model output escaped. No markdown or links are rendered. The Host, Origin, content type and per-start key checks apply to `/ask` and the key routes like the other actions.

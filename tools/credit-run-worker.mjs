// Runs one job through the Claude Agent SDK. Started by credit-run, not by hand.
//
//   node credit-run-worker.mjs SDK_DIR < job.json
//
// stdin is JSON: {prompt, options, out}. options go to query() as they are, with env
// added here from this process (the key is only in the environment, never in the JSON).
// The full result message is written to `out`. One summary line goes to stdout.
// A budget cap yields a result and then throws; the result is kept and the throw is
// not a failure. No result at all exits 1 with no summary line.
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const sdkDir = process.argv[2]
const job = JSON.parse(readFileSync(0, 'utf8'))
const key = process.env.ANTHROPIC_API_KEY || ''

const scrub = (text) => (key ? String(text).split(key).join('[redacted]') : String(text))

const { query } = await import(
  pathToFileURL(join(sdkDir, 'node_modules', '@anthropic-ai', 'claude-agent-sdk', 'sdk.mjs')).href
)

let result = null
try {
  const options = { ...job.options, env: { ...process.env } }
  for await (const m of query({ prompt: job.prompt, options })) {
    if (m.type === 'result') result = m
  }
} catch (e) {
  const capped = result && result.subtype === 'error_max_budget_usd'
  if (!capped) {
    process.stderr.write(scrub(`credit-run-worker: ${e && e.message ? e.message : e}`) + '\n')
    if (!result) process.exit(1)
  }
}

if (!result) {
  process.stderr.write('credit-run-worker: the SDK returned no result\n')
  process.exit(1)
}

writeFileSync(job.out, scrub(JSON.stringify(result, null, 2)), { mode: 0o600 })
process.stdout.write(
  scrub(
    JSON.stringify({
      subtype: result.subtype,
      is_error: !!result.is_error,
      result: result.result ?? '',
      cost: result.total_cost_usd ?? null,
      turns: result.num_turns ?? null,
      session_id: result.session_id ?? null,
    }),
  ) + '\n',
)

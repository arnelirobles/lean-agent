// One Message Batches call through @anthropic-ai/sdk. Started by credit-batch, not by hand.
//
//   node credit-batch-worker.mjs < job.json
//
// stdin is JSON: {sdkDir, op, ...}. The key is only in ANTHROPIC_API_KEY, which the SDK reads.
//   op create    {requests}        prints {id, processing_status}
//   op retrieve  {batch_id}        prints {id, processing_status, request_counts, ended_at}
//   op results   {batch_id, dir}   writes dir/<custom_id>.txt for every request (the text, or
//                                  the error, expired or canceled state) and prints {items}.
//                                  A symlink at a target is refused. cache_write_1h is null when
//                                  the API gave no split.
// One JSON line goes to stdout. An SDK error exits 1 with one line on stderr.
import { closeSync, constants, lstatSync, openSync, readFileSync, writeSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const job = JSON.parse(readFileSync(0, 'utf8'))
const key = process.env.ANTHROPIC_API_KEY || ''
const scrub = (text) => (key ? String(text).split(key).join('[redacted]') : String(text))
const emit = (value) => process.stdout.write(scrub(JSON.stringify(value)) + '\n')

try {
  const { default: Anthropic } = await import(
    pathToFileURL(join(job.sdkDir, 'node_modules', '@anthropic-ai', 'sdk', 'index.mjs')).href
  )
  const client = new Anthropic()
  if (job.op === 'create') {
    const batch = await client.messages.batches.create({ requests: job.requests })
    emit({ id: batch.id, processing_status: batch.processing_status })
  } else if (job.op === 'retrieve') {
    const batch = await client.messages.batches.retrieve(job.batch_id)
    emit({
      id: batch.id,
      processing_status: batch.processing_status,
      request_counts: batch.request_counts,
      ended_at: batch.ended_at,
    })
  } else if (job.op === 'results') {
    const items = []
    for await (const entry of await client.messages.batches.results(job.batch_id)) {
      const r = entry.result
      let text
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(entry.custom_id)) throw new Error('unsafe custom_id in the results')
      const item = { custom_id: entry.custom_id, type: r.type }
      if (r.type === 'succeeded') {
        text = r.message.content.filter((b) => b.type === 'text').map((b) => b.text).join('\n')
        const u = r.message.usage || {}
        item.model = r.message.model
        item.usage = {
          input: u.input_tokens ?? 0,
          cache_write: u.cache_creation_input_tokens ?? 0,
          cache_write_1h: u.cache_creation?.ephemeral_1h_input_tokens ?? null,
          cache_read: u.cache_read_input_tokens ?? 0,
          output: u.output_tokens ?? 0,
        }
      } else if (r.type === 'errored') {
        item.error = r.error?.error?.message ?? r.error?.message ?? 'unknown error'
        text = `errored: ${item.error}`
      } else {
        text = r.type
      }
      const target = join(job.dir, `${entry.custom_id}.txt`)
      if (lstatSync(target, { throwIfNoEntry: false })?.isSymbolicLink()) {
        throw new Error(`refusing to write through a symlink at ${entry.custom_id}.txt`)
      }
      const fd = openSync(target, constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | constants.O_NOFOLLOW, 0o600)
      try { writeSync(fd, scrub(text) + '\n') } finally { closeSync(fd) }
      items.push(item)
    }
    emit({ items })
  } else {
    throw new Error(`unknown op ${job.op}`)
  }
} catch (e) {
  process.stderr.write(scrub(`credit-batch-worker: ${e && e.message ? e.message : e}`).split('\n')[0] + '\n')
  process.exit(1)
}

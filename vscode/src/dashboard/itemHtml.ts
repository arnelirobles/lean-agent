import { relativeTime } from "../db";
import type { ItemFull } from "../types";
import { esc } from "./escape";
import { page } from "./page";

export function itemBody(i: ItemFull, now: number): string {
  const field = (k: string, v: unknown) => (v === null || v === undefined || v === "" ? "" : `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`);
  const tags = i.tags.length ? i.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") : "";
  return `<h1>#${esc(i.id)} ${esc(i.title || i.ref)}</h1>` +
    `<table><tbody>` +
    field("status", i.status) + field("resolution", i.resolution) + field("kind", i.kind) + field("project", i.project) +
    field("repo", i.repo) + field("branch", i.branch) + field("ref", i.ref) + field("severity", i.severity) +
    field("phase", i.phase) + field("source", i.source) + field("model", i.model) + field("cycle", i.cycle) +
    field("commit", i.commit_hash) + field("link", i.link) + field("run id", i.run_id) + field("occurrences", i.occurrences) +
    field("first seen", i.first_seen) + field("last seen", `${i.last_seen} (${relativeTime(i.last_seen, now)})`) +
    field("closed at", i.closed_at) + (tags ? `<tr><th>tags</th><td>${tags}</td></tr>` : "") +
    `</tbody></table><h2>Body</h2>` +
    (i.body ? `<pre>${esc(i.body)}</pre>` : `<p class="muted">No body.</p>`);
}

export function itemHtml(i: ItemFull, now: number, nonce: string, cspSource: string): string {
  return page(`Item ${i.id}`, itemBody(i, now), nonce, cspSource);
}

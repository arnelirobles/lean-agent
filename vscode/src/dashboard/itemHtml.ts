import { relativeTime } from "../db";
import { kindLook, severityTone } from "../style";
import type { ItemFull } from "../types";
import { esc } from "./escape";
import { ic, page, pill } from "./page";

export function itemBody(i: ItemFull, now: number): string {
  const field = (k: string, v: unknown) => (v === null || v === undefined || v === "" ? "" : `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`);
  const tags = i.tags.length ? i.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") : "";
  const look = kindLook(i.kind);
  const pills = [
    pill(i.status, i.status === "open" ? "orange" : "green", i.status === "open" ? "circle-large-outline" : "pass-filled"),
    pill(i.kind, look.tone, look.icon),
    i.severity ? pill(i.severity, severityTone(i.severity)) : "",
    i.occurrences > 1 ? pill(`seen ${i.occurrences} times`, "purple", "history") : "",
    i.resolution ? pill(i.resolution, "green", "check") : "",
  ].join(" ");
  return `<div class="hero t-${look.tone}"><div class="grow"><h1>${ic(look.icon)}#${esc(i.id)} ${esc(i.title || i.ref)}</h1>` +
    `<div class="sub">${esc(i.project)}, last seen ${esc(relativeTime(i.last_seen, now))}</div></div>` +
    `<button data-copy="${esc(i.ref || String(i.id))}">${ic("copy")}Copy ref</button></div>` +
    `<p>${pills}</p>` +
    `<div class="sec t-blue"><span class="ico">${ic("list-unordered")}</span><h2>Fields</h2></div>` +
    `<div class="card"><table class="fields"><tbody>` +
    field("repo", i.repo) + field("branch", i.branch) + field("ref", i.ref) +
    field("phase", i.phase) + field("source", i.source) + field("model", i.model) + field("cycle", i.cycle) +
    field("commit", i.commit_hash) + field("link", i.link) + field("run id", i.run_id) + field("occurrences", i.occurrences) +
    field("first seen", i.first_seen) + field("last seen", `${i.last_seen} (${relativeTime(i.last_seen, now)})`) +
    field("closed at", i.closed_at) + (tags ? `<tr><th>tags</th><td>${tags}</td></tr>` : "") +
    `</tbody></table></div>` +
    `<div class="sec t-purple"><span class="ico">${ic("note")}</span><h2>Body</h2></div>` +
    (i.body ? `<pre>${esc(i.body)}</pre>` : `<p class="muted">No body.</p>`);
}

export function itemHtml(i: ItemFull, now: number, nonce: string, cspSource: string, codiconCss = ""): string {
  return page(`Item ${i.id}`, itemBody(i, now), nonce, cspSource, codiconCss);
}

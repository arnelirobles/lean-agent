import { relativeTime } from "../db";
import { canSend, validSession } from "../claude";
import { kindLook, severityTone } from "../style";
import type { ItemFull } from "../types";
import { esc } from "./escape";
import { ic, page, pill } from "./page";

export function itemBody(i: ItemFull, now: number): string {
  const field = (k: string, v: unknown) => (v === null || v === undefined || v === "" ? "" : `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`);
  const tags = i.tags.length ? i.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") : "";
  const look = kindLook(i.kind);
  const pills = [
    pill(i.status, i.status === "open" ? "yellow" : "green", i.status === "open" ? "circle-large-outline" : "pass-filled"),
    pill(i.kind, look.tone, look.icon),
    i.severity ? pill(i.severity, severityTone(i.severity)) : "",
    i.occurrences > 1 ? pill(`seen ${i.occurrences} times`, "purple", "history") : "",
    i.resolution ? pill(i.resolution, "green", "check") : "",
  ].join(" ");
  return `<div class="hero t-${look.tone}"><span class="mark">${ic(look.icon)}</span>` +
    `<div class="grow"><div class="eyebrow">${esc(i.kind)} #${esc(i.id)}</div><h1>${esc(i.title || i.ref)}</h1>` +
    `<div class="sub">${esc(i.project)}, last seen ${esc(relativeTime(i.last_seen, now))}</div></div>` +
    `<div class="actions">` +
    (canSend(i.kind) && i.status === "open" ? `<button class="leaf" data-send="${esc(i.id)}">${ic("rocket")}Send to Claude</button>` : "") +
    `<button data-ask="${esc(i.id)}">${ic("terminal")}Ask Claude</button>` +
    `<button class="sun" data-copy="${esc(i.ref || String(i.id))}">${ic("copy")}Copy ref</button></div></div>` +
    `<div class="pills">${pills}</div>` +
    answerBox(i) +
    `<div class="sec t-blue"><span class="ico">${ic("list-unordered")}</span><h2>Fields</h2></div>` +
    `<div class="card"><table class="fields"><tbody>` +
    field("repo", i.repo) + field("branch", i.branch) + field("ref", i.ref) +
    field("phase", i.phase) + field("source", i.source) + field("model", i.model) + field("cycle", i.cycle) +
    field("commit", i.commit_hash) + field("link", i.link) + field("run id", i.run_id) + field("occurrences", i.occurrences) +
    field("first seen", i.first_seen) + field("last seen", `${i.last_seen} (${relativeTime(i.last_seen, now)})`) +
    field("closed at", i.closed_at) + field("session", i.session_id) + field("session folder", i.session_cwd) + (tags ? `<tr><th>tags</th><td>${tags}</td></tr>` : "") +
    `</tbody></table></div>` +
    `<div class="sec t-purple"><span class="ico">${ic("note")}</span><h2>Body</h2></div>` +
    (i.body ? `<pre>${esc(i.body)}</pre>` : `<p class="muted">No body.</p>`);
}

/** An open question gets a box for the answer. Sending it closes the item as answered and takes it to the session that asked. */
function answerBox(i: ItemFull): string {
  if (!/question/i.test(i.kind) || i.status !== "open") return "";
  const where = validSession(i.session_id)
    ? "The answer goes back to the session that asked. If that session is still open in another window, you pick: its next prompt, or a fork."
    : "This question has no session on record, so the answer is saved in brain only.";
  return `<div class="sec t-green"><span class="ico">${ic("comment-discussion")}</span><div><div class="eyebrow">your call</div><h2>Answer</h2></div></div>` +
    `<div class="card pad answer"><textarea id="answer" placeholder="Type the answer" aria-label="Answer"></textarea>` +
    `<div class="row"><span class="muted">${esc(where)}</span>` +
    `<button class="leaf" data-answer="${esc(i.id)}">${ic("send")}Send answer</button></div></div>`;
}

export function itemHtml(i: ItemFull, now: number, nonce: string, cspSource: string, codiconCss = "", media = ""): string {
  return page(`Item ${i.id}`, itemBody(i, now), nonce, cspSource, codiconCss, media);
}

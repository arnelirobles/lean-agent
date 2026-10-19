import { formatDuration, relativeTime, shortTestName, STALE_DAYS } from "../db";
import { canSend } from "../claude";
import { kindLook, severityTone, type Tone } from "../style";
import type { DashboardData } from "../types";
import { bar, loadChart, sparkline, trendTone } from "./charts";
import { esc } from "./escape";
import { ic, page, pill } from "./page";

const empty = (what: string, icon = "check-all") => `<p class="empty">${ic(icon)}${esc(what)}</p>`;
const card = (inner: string) => `<div class="card">${inner}</div>`;
const sec = (title: string, icon: string, tone: Tone, hint = "") =>
  `<div class="sec t-${tone}"><span class="ico">${ic(icon)}</span><div>${hint ? `<div class="eyebrow">${esc(hint)}</div>` : ""}<h2>${esc(title)}</h2></div></div>`;
const kindPill = (kind: string) => { const l = kindLook(kind); return pill(kind, l.tone, l.icon); };
const numCell = (n: number, tone: "hot" | "good" | "warm") => `<td class="num ${n ? tone : "zero"}">${esc(n)}</td>`;

function waitingSection(d: DashboardData): string {
  if (!d.waiting.length) return empty("Nothing is waiting on you.");
  const rows = d.waiting.map((i) =>
    `<tr><td>${kindPill(i.kind)}</td><td><a data-open="${esc(i.id)}">${esc(i.title || i.ref)}</a></td>` +
    `<td>${i.severity ? pill(i.severity, severityTone(i.severity)) : ""}</td>` +
    `<td class="muted">${esc(i.project)}</td><td class="muted">${esc(relativeTime(i.last_seen, d.generatedAt))}</td>` +
    `<td>${canSend(i.kind) ? `<button class="small" data-send="${esc(i.id)}">${ic("rocket")}Send to Claude</button>` : ""}</td></tr>`).join("");
  return card(`<table><thead><tr><th>kind</th><th>item</th><th>severity</th><th>project</th><th>last seen</th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function headlineSection(d: DashboardData): string {
  const h = d.headline;
  const tile = (label: string, value: string, icon: string, tone: Tone) =>
    `<div class="tile t-${tone}"><span class="ico">${ic(icon)}</span><div><b>${esc(value)}</b><span>${esc(label)}</span></div></div>`;
  return `<div class="tiles">` +
    tile("open real failures", String(h.realOpen), "bug", h.realOpen ? "red" : "green") +
    tile("open flaky", String(h.flakyOpen), "warning", h.flakyOpen ? "yellow" : "green") +
    tile("closed, last 7 days", String(h.closed7d), "pass-filled", "green") +
    tile("runs, last 7 days", String(h.runs7d), "play-circle", "blue") +
    tile("test time, last 7 days", formatDuration(h.testSeconds7d), "watch", "purple") + `</div>`;
}

function cyclesSection(d: DashboardData): string {
  if (!d.cycles.length) return empty("No runs recorded yet.", "info");
  const max = Math.max(...d.cycles.map((c) => c.duration_s), 1);
  const rows = d.cycles.map((c) =>
    `<tr><td>${pill(c.cycle || "(no cycle)", c.failed ? "red" : c.stalls ? "orange" : "green", "history")}</td><td class="num">${esc(c.jobs)}</td>` +
    numCell(c.passed, "good") + numCell(c.failed, "hot") + numCell(c.flaky_filed, "warm") +
    `<td class="num">${esc(formatDuration(c.duration_s))}</td>` + numCell(c.stalls, "hot") +
    `<td>${bar(c.duration_s, max, 140, c.failed ? "red" : "green")}</td></tr>`).join("");
  return card(`<table><thead><tr><th>cycle</th><th class="num">jobs</th><th class="num">passed</th><th class="num">failed</th>` +
    `<th class="num">flaky filed</th><th class="num">duration</th><th class="num">stalls</th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function suitesSection(d: DashboardData): string {
  if (!d.suites.length) return empty("No finished runs with a duration yet.", "info");
  const rows = d.suites.slice(0, 15).map((s) => {
    const trend = trendTone(s.history);
    const trendIcon = trend === "red" ? "arrow-up" : trend === "green" ? "arrow-down" : "dash";
    return `<tr><td title="${esc(s.test_project)}">${esc(shortTestName(s.test_project))}</td><td class="muted">${esc(s.project)}</td>` +
      `<td class="num">${esc(formatDuration(s.median_s))}</td><td class="num">${pill(formatDuration(s.last_s), trend, trendIcon)}</td>` +
      `<td>${sparkline(s.history)}</td><td class="num">${esc(formatDuration(s.mean_lane_wait_s))}</td></tr>`;
  }).join("");
  return card(`<table><thead><tr><th>suite</th><th>project</th><th class="num">median</th><th class="num">last</th><th>last 20 runs</th>` +
    `<th class="num">lane wait</th></tr></thead><tbody>${rows}</tbody></table>`) +
    `<div class="legend"><span>${pill("slower", "red", "arrow-up")} last run 20% over the median</span>` +
    `<span>${pill("faster", "green", "arrow-down")} 20% under</span></div>`;
}

function flakySection(d: DashboardData): string {
  if (!d.flakiest.length) return empty("No open flaky items.");
  const max = Math.max(...d.flakiest.map((i) => i.occurrences), 1);
  const rows = d.flakiest.map((i) => {
    const tone: Tone = i.occurrences >= max * 0.66 ? "red" : i.occurrences >= max * 0.33 ? "orange" : "yellow";
    return `<tr><td class="num"><span class="count t-${tone}">${esc(i.occurrences)}</span></td>` +
      `<td><a data-open="${esc(i.id)}">${esc(i.title || i.ref)}</a></td><td>${bar(i.occurrences, max, 90, tone)}</td>` +
      `<td class="muted">${esc(i.project)}</td></tr>`;
  }).join("");
  return card(`<table><thead><tr><th class="num">seen</th><th>test</th><th></th><th>project</th></tr></thead><tbody>${rows}</tbody></table>`);
}

function loadSection(d: DashboardData): string {
  return `<div class="card pad">` + loadChart(d.load.map((p) => ({
    x: p.started, start: p.load_start, end: p.load_end, stalled: p.stalled, label: shortTestName(p.test_project),
  }))) + `</div>`;
}

export function listCommand(kind: string): string {
  return `brain list --open --kind ${shellQuote(kind)}`;
}

function shellQuote(s: string): string {
  return /^[A-Za-z0-9._-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;
}

function staleSection(d: DashboardData): string {
  if (!d.stale.length) return empty(`No open items older than ${STALE_DAYS} days.`, "sparkle");
  const total = d.stale.reduce((a, s) => a + s.n, 0);
  const rows = d.stale.map((s) =>
    `<tr><td>${kindPill(s.kind)}</td><td class="num"><span class="count t-${kindLook(s.kind).tone}">${esc(s.n)}</span></td>` +
    `<td class="muted">oldest ${esc(s.oldest)}</td>` +
    `<td><button data-copy="${esc(listCommand(s.kind))}">${ic("copy")}Copy list command</button></td></tr>`).join("");
  return `<p class="lead">${esc(total)} open items were last seen more than ${STALE_DAYS} days ago. The button copies a read-only command.</p>` +
    card(`<table><thead><tr><th>kind</th><th class="num">count</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table>`);
}

function hero(d: DashboardData, media: string): string {
  const h = d.headline;
  const when = new Date(d.generatedAt * 1000).toLocaleString("en-GB");
  const stat = (n: number | string, label: string, hot = false) => `<div class="stat${hot ? " hot" : ""}"><b>${esc(n)}</b><span>${esc(label)}</span></div>`;
  const logo = media ? `<img class="fox" src="${esc(media)}/fox-mark.webp" alt="">` : `<span class="mark t-green">${ic("pulse")}</span>`;
  return `<div class="hero">${logo}<div class="grow"><div class="eyebrow">Lean agent analytics</div>` +
    `<h1>lean<span class="dot">.brain</span></h1>` +
    `<div class="sub">schema ${esc(d.schemaVersion ?? "unknown")}, read ${esc(when)}</div>` +
    `<div class="stats">${stat(h.realOpen, "real failures", h.realOpen > 0)}${stat(h.flakyOpen, "flaky")}` +
    `${stat(d.waiting.length, "waiting on you", d.waiting.length > 0)}${stat(h.runs7d, "runs this week")}</div></div>` +
    `<div class="actions"><button class="leaf" data-claude="1">${ic("terminal")}Claude Code</button>` +
    `<button class="sun" data-refresh="1">${ic("refresh")}Refresh</button></div></div>`;
}

interface Tab { id: string; label: string; icon: string; tone: Tone; badge?: number; body: string }

export function dashboardBody(d: DashboardData, note = "", media = ""): string {
  const staleTotal = d.stale.reduce((a, s) => a + s.n, 0);
  const tabs: Tab[] = [
    { id: "overview", label: "Overview", icon: "dashboard", tone: "blue", badge: d.waiting.length,
      body: sec("Waiting on you", "bell", "orange", "questions, blockers and rulings") + waitingSection(d) +
        sec("Numbers", "graph", "blue") + headlineSection(d) },
    { id: "cycles", label: "Cycles", icon: "history", tone: "green", badge: d.cycles.length,
      body: sec("Cycles", "history", "green", "one row per test cycle") + cyclesSection(d) },
    { id: "suites", label: "Suites", icon: "beaker", tone: "purple",
      body: sec("Slowest suites", "beaker", "purple", "by median duration") + suitesSection(d) },
    { id: "flaky", label: "Flaky", icon: "warning", tone: "yellow", badge: d.flakiest.length,
      body: sec("Flakiest tests", "warning", "yellow", "open flaky items by times seen") + flakySection(d) },
    { id: "load", label: "Load", icon: "pulse", tone: "orange",
      body: sec("Machine load", "pulse", "orange", "load average at start and end of each run") + loadSection(d) },
    { id: "hygiene", label: "Hygiene", icon: "sparkle", tone: "gray", badge: staleTotal,
      body: sec("Hygiene", "sparkle", "gray", `open items not seen for ${STALE_DAYS} days`) + staleSection(d) },
  ];
  const bar = `<div class="tabs" role="tablist">` + tabs.map((t, i) =>
    `<button class="tab t-${t.tone}${i === 0 ? " active" : ""}" role="tab" data-tab="${t.id}">${ic(t.icon)}${esc(t.label)}` +
    (t.badge ? `<span class="badge">${esc(t.badge)}</span>` : "") + `</button>`).join("") + `</div>`;
  const panels = tabs.map((t, i) => `<section class="panel${i === 0 ? " active" : ""}" data-panel="${t.id}">${t.body}</section>`).join("");
  return hero(d, media) + (note ? `<p class="note">${esc(note)}</p>` : "") + bar + panels;
}

export function dashboardHtml(d: DashboardData, nonce: string, cspSource: string, note = "", codiconCss = "", media = ""): string {
  return page("Lean Brain", dashboardBody(d, note, media), nonce, cspSource, codiconCss, media);
}

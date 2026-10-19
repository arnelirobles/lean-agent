import { formatDuration, relativeTime, shortTestName, STALE_DAYS } from "../db";
import type { DashboardData } from "../types";
import { bar, loadChart, sparkline } from "./charts";
import { esc } from "./escape";
import { page } from "./page";

const empty = (what: string) => `<p class="muted">${esc(what)}</p>`;

function waitingSection(d: DashboardData): string {
  if (!d.waiting.length) return empty("Nothing is waiting on you.");
  const rows = d.waiting.map((i) =>
    `<tr><td><span class="tag">${esc(i.kind)}</span></td><td><a data-open="${esc(i.id)}">${esc(i.title || i.ref)}</a></td>` +
    `<td class="muted">${esc(i.project)}</td><td class="muted">${esc(relativeTime(i.last_seen, d.generatedAt))}</td></tr>`).join("");
  return `<table><thead><tr><th>kind</th><th>item</th><th>project</th><th>last seen</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function headlineSection(d: DashboardData): string {
  const h = d.headline;
  const tile = (label: string, value: string, cls = "") => `<div class="tile ${cls}"><b>${esc(value)}</b>${esc(label)}</div>`;
  return `<div class="tiles">` +
    tile("open real failures", String(h.realOpen), h.realOpen ? "bad" : "") +
    tile("open flaky", String(h.flakyOpen), h.flakyOpen ? "warn" : "") +
    tile("closed, last 7 days", String(h.closed7d)) +
    tile("runs, last 7 days", String(h.runs7d)) +
    tile("test time, last 7 days", formatDuration(h.testSeconds7d)) + `</div>`;
}

function cyclesSection(d: DashboardData): string {
  if (!d.cycles.length) return empty("No runs recorded yet.");
  const max = Math.max(...d.cycles.map((c) => c.duration_s), 1);
  const rows = d.cycles.map((c) =>
    `<tr><td>${esc(c.cycle || "(no cycle)")}</td><td class="num">${esc(c.jobs)}</td><td class="num">${esc(c.passed)}</td>` +
    `<td class="num">${esc(c.failed)}</td><td class="num">${esc(c.flaky_filed)}</td><td class="num">${esc(formatDuration(c.duration_s))}</td>` +
    `<td class="num">${esc(c.stalls)}</td><td>${bar(c.duration_s, max)}</td></tr>`).join("");
  return `<table><thead><tr><th>cycle</th><th class="num">jobs</th><th class="num">tests passed</th><th class="num">tests failed</th>` +
    `<th class="num">flaky filed</th><th class="num">duration</th><th class="num">stalls</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

function suitesSection(d: DashboardData): string {
  if (!d.suites.length) return empty("No finished runs with a duration yet.");
  const rows = d.suites.slice(0, 15).map((s) =>
    `<tr><td title="${esc(s.test_project)}">${esc(shortTestName(s.test_project))}</td><td class="muted">${esc(s.project)}</td>` +
    `<td class="num">${esc(formatDuration(s.median_s))}</td><td class="num">${esc(formatDuration(s.last_s))}</td>` +
    `<td>${sparkline(s.history)}</td><td class="num">${esc(formatDuration(s.mean_lane_wait_s))}</td></tr>`).join("");
  return `<table><thead><tr><th>suite</th><th>project</th><th class="num">median</th><th class="num">last</th><th>last 20 runs</th>` +
    `<th class="num">lane wait</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function flakySection(d: DashboardData): string {
  if (!d.flakiest.length) return empty("No open flaky items.");
  const rows = d.flakiest.map((i) =>
    `<tr><td class="num">${esc(i.occurrences)}</td><td><a data-open="${esc(i.id)}">${esc(i.title || i.ref)}</a></td>` +
    `<td class="muted">${esc(i.project)}</td></tr>`).join("");
  return `<table><thead><tr><th class="num">seen</th><th>test</th><th>project</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function loadSection(d: DashboardData): string {
  return loadChart(d.load.map((p) => ({
    x: p.started, start: p.load_start, end: p.load_end, stalled: p.stalled, label: shortTestName(p.test_project),
  })));
}

export function listCommand(kind: string): string {
  return `brain list --open --kind ${shellQuote(kind)}`;
}

function shellQuote(s: string): string {
  return /^[A-Za-z0-9._-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;
}

function staleSection(d: DashboardData): string {
  if (!d.stale.length) return empty(`No open items older than ${STALE_DAYS} days.`);
  const total = d.stale.reduce((a, s) => a + s.n, 0);
  const rows = d.stale.map((s) =>
    `<tr><td><span class="tag">${esc(s.kind)}</span></td><td class="num">${esc(s.n)}</td><td class="muted">oldest ${esc(s.oldest)}</td>` +
    `<td><button data-copy="${esc(listCommand(s.kind))}">Copy list command</button></td></tr>`).join("");
  return `<p>${esc(total)} open items were last seen more than ${STALE_DAYS} days ago. The button copies a read-only command.</p>` +
    `<table><thead><tr><th>kind</th><th class="num">count</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

export function dashboardBody(d: DashboardData, note = ""): string {
  const when = new Date(d.generatedAt * 1000).toLocaleString("en-GB");
  return `<h1>Lean Brain</h1><div class="muted">schema ${esc(d.schemaVersion ?? "unknown")}, read ${esc(when)} ` +
    `<button data-refresh="1">Refresh</button></div>` +
    (note ? `<p class="note">${esc(note)}</p>` : "") +
    `<h2>Waiting on you</h2>${waitingSection(d)}` +
    `<h2>Numbers</h2>${headlineSection(d)}` +
    `<h2>Cycles</h2>${cyclesSection(d)}` +
    `<h2>Slowest suites</h2>${suitesSection(d)}` +
    `<h2>Flakiest tests</h2>${flakySection(d)}` +
    `<h2>Machine load</h2>${loadSection(d)}` +
    `<h2>Hygiene</h2>${staleSection(d)}`;
}

export function dashboardHtml(d: DashboardData, nonce: string, cspSource: string, note = ""): string {
  return page("Lean Brain", dashboardBody(d, note), nonce, cspSource);
}

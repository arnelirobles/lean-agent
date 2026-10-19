import type { Database } from "sql.js";
import * as Q from "./queries";
import type {
  Counts, CycleRow, DashboardData, Headline, ItemFull, ItemRow, JobRow, LoadPoint, QueueData, StaleRow, SuiteRow,
} from "./types";

// Pure functions over a sql.js Database. Nothing here imports vscode.

export const REAL_KINDS = ["test-fail", "failure", "bug"];
export const FLAKY_KINDS = ["flaky"];
const WAITING_RE = /question|blocker|pending|ruling|decision|approval|waiting|needs-/i;
export const STALE_DAYS = 14;

type Row = Record<string, any>;

export function rows(db: Database, sql: string, params: (string | number)[] = []): Row[] {
  const stmt = db.prepare(sql);
  try {
    stmt.bind(params);
    const out: Row[] = [];
    while (stmt.step()) out.push(stmt.getAsObject() as Row);
    return out;
  } finally {
    stmt.free();
  }
}

function one(db: Database, sql: string, params: (string | number)[] = []): Row | undefined {
  return rows(db, sql, params)[0];
}

export function isoAt(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function parseIso(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

export function schemaVersion(db: Database): number | null {
  const r = one(db, Q.SCHEMA_VERSION);
  return r ? Number(r.value) : null;
}

export function openItems(db: Database): ItemRow[] {
  return rows(db, Q.OPEN_ITEMS) as ItemRow[];
}

export function getItem(db: Database, id: number): ItemFull | undefined {
  const r = one(db, Q.ITEM, [id]);
  if (!r) return undefined;
  const tags = rows(db, Q.ITEM_TAGS, [id]).map((t) => String(t.tag));
  return { ...(r as any), tags } as ItemFull;
}

export type SessionState = "open" | "ended" | "unknown";

/** Whether a Claude session is still open, as the session-env hook recorded it. A database before schema 5 has no sessions table. */
export function sessionState(db: Database, id: string): SessionState {
  try {
    const r = one(db, Q.SESSION, [id]);
    return !r ? "unknown" : r.ended_at ? "ended" : "open";
  } catch {
    return "unknown";
  }
}

/** Open item counts. With a project filter (case-insensitive, any of the names) only those projects count. */
export function openCounts(db: Database, projects?: string[]): { counts: Counts; scoped: boolean } {
  const want = (projects ?? []).map((p) => p.toLowerCase());
  const all = rows(db, Q.OPEN_COUNT_BY_KIND_PROJECT);
  const scopedRows = want.length ? all.filter((r) => want.includes(String(r.project).toLowerCase())) : [];
  const scoped = scopedRows.length > 0;
  const use = scoped ? scopedRows : all;
  const sum = (kinds: string[]) => use.filter((r) => kinds.includes(r.kind)).reduce((a, r) => a + Number(r.n), 0);
  return { counts: { real: sum(REAL_KINDS), flaky: sum(FLAKY_KINDS) }, scoped };
}

export function queue(db: Database): QueueData {
  return {
    running: rows(db, Q.RUNNING_JOBS) as JobRow[],
    queued: rows(db, Q.QUEUED_JOBS) as JobRow[],
    finished: rows(db, Q.FINISHED_JOBS) as JobRow[],
  };
}

/** The kinds in use that mean the user is being waited on. Picked from kinds actually present. */
export function waitingKinds(db: Database): string[] {
  return rows(db, Q.OPEN_KINDS).map((r) => String(r.kind)).filter((k) => WAITING_RE.test(k));
}

export function waiting(db: Database): ItemRow[] {
  const kinds = waitingKinds(db);
  if (!kinds.length) return [];
  const sql = Q.WAITING.replace("%KINDS%", kinds.map(() => "?").join(","));
  return rows(db, sql, kinds) as ItemRow[];
}

export function headline(db: Database, now: number): Headline {
  const counts = openCounts(db).counts;
  const since = now - 7 * 86400;
  const closed = one(db, Q.CLOSED_SINCE, [isoAt(since)]);
  const runs = one(db, Q.RUNS_SINCE, [since]);
  return {
    realOpen: counts.real,
    flakyOpen: counts.flaky,
    closed7d: Number(closed?.n ?? 0),
    runs7d: Number(runs?.n ?? 0),
    testSeconds7d: Number(runs?.total_s ?? 0),
  };
}

export function cycles(db: Database): CycleRow[] {
  return rows(db, Q.CYCLES) as CycleRow[];
}

export function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export function suites(db: Database): SuiteRow[] {
  const by = new Map<string, Row[]>();
  for (const r of rows(db, Q.SUITE_RUNS)) {
    const key = `${r.project}\u0000${r.test_project}`;
    const list = by.get(key) ?? [];
    list.push(r);
    by.set(key, list);
  }
  const out: SuiteRow[] = [];
  for (const list of by.values()) {
    const durations = list.map((r) => Number(r.duration_s));
    const waits = list.map((r) => Number(r.lane_wait_s ?? 0));
    out.push({
      project: String(list[0].project),
      test_project: String(list[0].test_project),
      runs: list.length,
      median_s: median(durations),
      last_s: durations[durations.length - 1],
      history: durations.slice(-20),
      mean_lane_wait_s: waits.reduce((a, b) => a + b, 0) / waits.length,
    });
  }
  return out.sort((a, b) => b.median_s - a.median_s);
}

export function flakiest(db: Database): ItemRow[] {
  return rows(db, Q.FLAKIEST) as ItemRow[];
}

export function loadPoints(db: Database): LoadPoint[] {
  return rows(db, Q.LOAD)
    .map((r) => ({
      id: Number(r.id), test_project: String(r.test_project), started: Number(r.started),
      load_start: r.load_start == null ? null : Number(r.load_start),
      load_end: r.load_end == null ? null : Number(r.load_end),
      stalled: Number(r.stalled) === 1,
    }))
    .reverse();
}

export function stale(db: Database, now: number): StaleRow[] {
  return rows(db, Q.STALE, [isoAt(now - STALE_DAYS * 86400)]) as StaleRow[];
}

export function dashboard(db: Database, now: number): DashboardData {
  return {
    schemaVersion: schemaVersion(db),
    waiting: waiting(db),
    headline: headline(db, now),
    cycles: cycles(db),
    suites: suites(db),
    flakiest: flakiest(db),
    load: loadPoints(db),
    stale: stale(db, now),
    generatedAt: now,
  };
}

export function shortTestName(testProject: string): string {
  const base = testProject.split(/[\\/]/).pop() ?? testProject;
  return base.replace(/\.(csproj|fsproj|sln|slnx)$/i, "");
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function relativeTime(iso: string | null | undefined, now: number): string {
  const t = parseIso(iso);
  if (t == null) return "";
  const d = now - t;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

export function jobElapsed(job: JobRow, now: number): number | null {
  const start = parseIso(job.started_at);
  if (start == null) return null;
  const end = parseIso(job.ended_at) ?? now;
  return end - start;
}

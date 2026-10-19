import * as fs from "node:fs";
import * as path from "node:path";
import initSqlJs, { Database } from "sql.js";

// Copied from bin/brain (migrate_2, migrate_3, migrate_4, and the meta table). schema.test.ts keeps it honest.
export const SCHEMA = [
  `CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
        repo TEXT NOT NULL DEFAULT '', test_project TEXT NOT NULL DEFAULT '', runner TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'full', started INTEGER, ended INTEGER, duration_s REAL,
        lane_wait_s REAL, load_start REAL, load_end REAL, passed INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        stalled INTEGER NOT NULL DEFAULT 0, exit_code INTEGER)`,
  `CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL, ref TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', cycle TEXT NOT NULL DEFAULT '', commit_hash TEXT NOT NULL DEFAULT '',
        link TEXT NOT NULL DEFAULT '', run_id INTEGER REFERENCES runs(id), occurrences INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed_at TEXT)`,
  `CREATE TABLE tags (item_id INTEGER NOT NULL REFERENCES items(id), tag TEXT NOT NULL, PRIMARY KEY (item_id, tag))`,
  `CREATE INDEX items_key ON items (project, kind, ref, status)`,
  `CREATE INDEX runs_test ON runs (test_project)`,
  `CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
        repo_dir TEXT NOT NULL, test_project TEXT NOT NULL, filter TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'full', after_id INTEGER REFERENCES jobs(id),
        status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
        enqueued_by TEXT NOT NULL DEFAULT '', enqueued_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
        run_id INTEGER REFERENCES runs(id))`,
  `CREATE INDEX jobs_status ON jobs (status, id)`,
  `ALTER TABLE jobs ADD COLUMN args TEXT NOT NULL DEFAULT ''`,
];

export const NOW = Date.UTC(2026, 9, 19, 12, 0, 0) / 1000;
export const iso = (secondsAgo: number) => new Date((NOW - secondsAgo) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
const DAY = 86400;

export async function sqlJs() {
  const wasm = fs.readFileSync(path.join(__dirname, "..", "node_modules", "sql.js", "dist", "sql-wasm.wasm"));
  return initSqlJs({ wasmBinary: wasm as unknown as ArrayBuffer });
}

export async function emptyDb(): Promise<Database> {
  const SQL = await sqlJs();
  const db = new SQL.Database();
  for (const s of SCHEMA) db.run(s);
  db.run(`INSERT INTO meta VALUES ('schema_version','4')`);
  return db;
}

function item(db: Database, o: Record<string, string | number | null>) {
  const row = { repo: "", branch: "", title: "", body: "", status: "open", resolution: "", severity: "", cycle: "",
    occurrences: 1, closed_at: null, ...o } as Record<string, string | number | null>;
  const cols = Object.keys(row);
  db.run(`INSERT INTO items (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`, Object.values(row));
}

export async function seededDb(): Promise<Database> {
  const db = await emptyDb();
  // runs: suite A has 3 runs, suite B has 1 stalled run
  const run = (cycle: string, tp: string, started: number, dur: number, wait: number, ls: number, le: number, passed: number, failed: number, stalled = 0) =>
    db.run(`INSERT INTO runs (cycle, project, test_project, tier, started, ended, duration_s, lane_wait_s, load_start, load_end, passed, failed, stalled, exit_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [cycle, "core", tp, "full", started, started + dur, dur, wait, ls, le, passed, failed, stalled, failed ? 1 : 0]);
  run("c1", "tests/A.Tests/A.Tests.csproj", NOW - 2 * DAY, 100, 5, 1.5, 2.5, 50, 0);
  run("c1", "tests/A.Tests/A.Tests.csproj", NOW - 2 * DAY + 500, 300, 10, 2.0, 3.5, 48, 2);
  run("c2", "tests/A.Tests/A.Tests.csproj", NOW - 1 * DAY, 200, 0, 1.0, 1.2, 50, 0);
  run("c2", "tests/B.Tests/B.Tests.csproj", NOW - 10 * DAY, 600, 30, 6.0, 9.0, 10, 0, 1);

  item(db, { project: "core", kind: "blocker", ref: "need-key", title: "waiting on the signing key", first_seen: iso(3600), last_seen: iso(3600) });
  item(db, { project: "core", kind: "question", ref: "q1", title: "which tier for <script>alert(1)</script>?", first_seen: iso(7200), last_seen: iso(7200), body: "body <b>bold</b> & more" });
  item(db, { project: "core", kind: "pending", ref: "p1", title: "next step", first_seen: iso(100), last_seen: iso(100) });
  item(db, { project: "core", kind: "ticket", ref: "t1", title: "a ticket, not waiting", first_seen: iso(100), last_seen: iso(100) });
  item(db, { project: "core", kind: "test-fail", ref: "A.Fresh", title: "A.Fresh", first_seen: iso(DAY), last_seen: iso(DAY), cycle: "c1", severity: "important" });
  item(db, { project: "core", kind: "test-fail", ref: "A.Old1", title: "A.Old1", first_seen: iso(40 * DAY), last_seen: iso(30 * DAY) });
  item(db, { project: "core", kind: "test-fail", ref: "A.Old2", title: "A.Old2", first_seen: iso(40 * DAY), last_seen: iso(20 * DAY) });
  item(db, { project: "other", kind: "flaky", ref: "F.Often", title: "F.Often", occurrences: 9, first_seen: iso(DAY), last_seen: iso(DAY), cycle: "c1" });
  item(db, { project: "core", kind: "flaky", ref: "F.Rare", title: "F.Rare", occurrences: 2, first_seen: iso(DAY), last_seen: iso(DAY), cycle: "c2" });
  item(db, { project: "core", kind: "test-fail", ref: "Closed.Recent", title: "Closed.Recent", status: "closed", resolution: "fixed", first_seen: iso(9 * DAY), last_seen: iso(8 * DAY), closed_at: iso(DAY) });
  item(db, { project: "core", kind: "test-fail", ref: "Closed.Long", title: "Closed.Long", status: "closed", first_seen: iso(90 * DAY), last_seen: iso(80 * DAY), closed_at: iso(60 * DAY) });
  db.run(`INSERT INTO tags VALUES (1,'user'),(1,'urgent')`);

  const job = (status: string, tp: string, enq: number, start: number | null, end: number | null) =>
    db.run(`INSERT INTO jobs (cycle, project, repo_dir, test_project, status, enqueued_at, started_at, ended_at) VALUES ('c2','core','/r',?,?,?,?,?)`,
      [tp, status, iso(enq), start == null ? null : iso(start), end == null ? null : iso(end)]);
  job("done", "tests/A.Tests/A.Tests.csproj", 900, 800, 700);
  job("running", "tests/B.Tests/B.Tests.csproj", 400, 300, null);
  job("queued", "tests/C.Tests/C.Tests.csproj", 100, null, null);
  return db;
}

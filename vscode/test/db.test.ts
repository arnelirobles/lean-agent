import { beforeAll, describe, expect, it } from "vitest";
import type { Database } from "sql.js";
import * as db from "../src/db";
import { emptyDb, iso, NOW, seededDb } from "./fixture";

let d: Database;
beforeAll(async () => { d = await seededDb(); });

describe("db queries", () => {
  it("reads the session that filed an item and whether it is still open", async () => {
    const q = db.getItem(d, 2)!;
    expect(q.session_id).toBe("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b");
    expect(q.session_cwd).toBe("/work/core");
    expect(db.sessionState(d, "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")).toBe("open");
    expect(db.sessionState(d, "ended-1")).toBe("ended");
    expect(db.sessionState(d, "nobody")).toBe("unknown");
    const old = await emptyDb();
    old.run("DROP TABLE sessions");
    expect(db.sessionState(old, "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")).toBe("unknown");
  });

  it("reads the schema version", () => {
    expect(db.schemaVersion(d)).toBe(8);
  });

  it("lists activity newest first and says which failed", () => {
    const a = db.activity(d);
    expect(a.map((r) => r.action)).toEqual(["vm exec <b>x</b>", "stack release", "deploy"]);
    expect(a[0].ok).toBe(false);
    expect(a[0].error).toBe("exit 1 <script>");
    expect(a[1].ok).toBe(true);
  });

  it("reads no activity from a database before schema 8", async () => {
    expect(db.activity(await emptyDb(7))).toEqual([]);
  });

  it("lists open items only, and the fixture is not empty", () => {
    const items = db.openItems(d);
    expect(items.length).toBeGreaterThan(5);
    expect(items.every((i) => i.status === "open")).toBe(true);
    expect(items.map((i) => i.ref)).not.toContain("Closed.Recent");
  });

  it("counts real and flaky, scoped by project case-insensitively", () => {
    const all = db.openCounts(d);
    expect(all).toEqual({ counts: { real: 3, flaky: 2 }, scoped: false });
    const core = db.openCounts(d, ["CORE"]);
    expect(core).toEqual({ counts: { real: 3, flaky: 1 }, scoped: true });
    const none = db.openCounts(d, ["nosuch"]);
    expect(none.scoped).toBe(false);
    expect(none.counts.real).toBe(3);
  });

  it("waiting picks question-like kinds present, newest first, and skips tickets", () => {
    expect(db.waitingKinds(d).sort()).toEqual(["blocker", "pending", "question"]);
    const w = db.waiting(d);
    expect(w.map((i) => i.ref)).toEqual(["p1", "need-key", "q1"]);
  });

  it("headline numbers", () => {
    const h = db.headline(d, NOW);
    expect(h.realOpen).toBe(3);
    expect(h.flakyOpen).toBe(2);
    expect(h.closed7d).toBe(1);
    expect(h.runs7d).toBe(3);
    expect(h.testSeconds7d).toBe(600);
  });

  it("cycles", () => {
    const c = db.cycles(d);
    const c1 = c.find((x) => x.cycle === "c1")!;
    expect(c1).toMatchObject({ jobs: 2, passed: 98, failed: 2, flaky_filed: 1, stalls: 0 });
    expect(c1.duration_s).toBe(400);
    expect(c.find((x) => x.cycle === "c2")).toMatchObject({ jobs: 2, stalls: 1, flaky_filed: 1 });
  });

  it("suites have median, last, history and lane wait", () => {
    const s = db.suites(d);
    const a = s.find((x) => x.test_project.includes("A.Tests"))!;
    expect(a.runs).toBe(3);
    expect(a.median_s).toBe(200);
    expect(a.last_s).toBe(200);
    expect(a.history).toEqual([100, 300, 200]);
    expect(a.mean_lane_wait_s).toBeCloseTo(5);
    expect(s[0].test_project).toContain("B.Tests"); // slowest first
  });

  it("flakiest is ranked by occurrences", () => {
    const f = db.flakiest(d);
    expect(f.map((i) => i.ref)).toEqual(["F.Often", "F.Rare"]);
  });

  it("load points are oldest first and mark stalled runs", () => {
    const l = db.loadPoints(d);
    expect(l.length).toBe(4);
    expect(l[0].started).toBeLessThan(l[3].started);
    expect(l.filter((p) => p.stalled).length).toBe(1);
  });

  it("stale counts open items not seen for 14 days by kind", () => {
    const s = db.stale(d, NOW);
    expect(s).toHaveLength(1);
    expect(s[0]).toMatchObject({ kind: "test-fail", n: 2 });
  });

  it("queue", () => {
    const q = db.queue(d);
    expect(q.running).toHaveLength(1);
    expect(q.queued).toHaveLength(1);
    expect(q.finished).toHaveLength(1);
    expect(db.jobElapsed(q.running[0], NOW)).toBe(300);
    expect(db.jobElapsed(q.finished[0], NOW)).toBe(100);
    expect(db.shortTestName(q.running[0].test_project)).toBe("B.Tests");
  });

  it("queue carries which lane may run a job, and the host of a running one", () => {
    const qid = db.queue(d).queued[0].id;
    d.run(`UPDATE jobs SET platform='remote' WHERE id=?`, [qid]);
    d.run(`UPDATE jobs SET host='dev' WHERE status='running'`);
    const q = db.queue(d);
    expect(q.queued[0].platform).toBe("remote");
    expect(q.running[0].host).toBe("dev");
    expect(db.laneLabel(q.queued[0])).toBe("remote only");
    expect(db.laneLabel({ ...q.queued[0], platform: "apple" })).toBe("this machine only");
    expect(db.laneLabel({ ...q.queued[0], platform: "" })).toBe("");
    expect(db.jobContext(q.queued[0])).toBe("job.queued.remote");
    expect(db.jobContext({ ...q.queued[0], platform: "apple" })).toBe("job.queued.apple");
    expect(db.jobContext({ ...q.queued[0], platform: "" })).toBe("job.queued.any");
    expect(db.jobContext(q.running[0])).toBe("job.running");
    d.run(`UPDATE jobs SET platform='', host=''`);
  });

  it("lists queued jobs in the order the drainer takes them", () => {
    const first = db.queue(d).queued[0].id;
    d.run(`INSERT INTO jobs (project, repo_dir, test_project, status, enqueued_at) VALUES ('core','/r','D.csproj','queued',?)`, [iso(50)]);
    const later = db.queue(d).queued[1].id;
    d.run(`UPDATE jobs SET priority=2 WHERE id=?`, [later]);
    const q = db.queue(d);
    expect(q.queued.map((j) => j.id)).toEqual([later, first]);
    expect(q.queued.map((j) => j.priority)).toEqual([2, 0]);
    d.run(`DELETE FROM jobs WHERE id=?`, [later]);
  });

  it("queue reads a database from before platform and host", async () => {
    const old = await emptyDb(5);
    old.run(`INSERT INTO jobs (project, repo_dir, test_project, enqueued_at) VALUES ('p','/r','T.csproj',?)`, [iso(60)]);
    const q = db.queue(old);
    expect(q.queued).toHaveLength(1);
    expect(q.queued[0].platform).toBe("");
    expect(db.jobContext(q.queued[0])).toBe("job.queued.old");
    expect(q.queued[0].priority).toBeNull();
  });

  it("getItem returns all fields and tags", () => {
    const i = db.getItem(d, 1)!;
    expect(i.ref).toBe("need-key");
    expect(i.tags).toEqual(["urgent", "user"]);
    expect(db.getItem(d, 9999)).toBeUndefined();
  });

  it("formats time", () => {
    expect(db.formatDuration(65)).toBe("1m 5s");
    expect(db.formatDuration(7260)).toBe("2h 1m");
    expect(db.relativeTime(new Date((NOW - 7200) * 1000).toISOString(), NOW)).toBe("2h ago");
  });

  it("handles empty tables", async () => {
    const e = await emptyDb();
    const data = db.dashboard(e, NOW);
    expect(data.waiting).toEqual([]);
    expect(data.cycles).toEqual([]);
    expect(data.suites).toEqual([]);
    expect(data.load).toEqual([]);
    expect(data.headline).toEqual({ realOpen: 0, flakyOpen: 0, closed7d: 0, runs7d: 0, testSeconds7d: 0 });
    expect(db.openCounts(e).counts).toEqual({ real: 0, flaky: 0 });
  });
});

describe("dashboard queue", () => {
  it("carries the queue", async () => {
    const e = await seededDb();
    expect(db.dashboard(e, NOW).queue).toEqual(db.queue(e));
  });
});

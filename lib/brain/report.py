import json
import os
import statistics

from .store import connect, db_path, epoch
from .items import tags_of
from .queue import job_line, project_of


RESUME_ORDER = ("blocker", "agent", "pending", "question")


def default_project():
    return project_of(os.getcwd())


def cmd_resume(a):
    if not os.path.exists(db_path()):
        return 0
    project = a.project or default_project()
    con = connect()
    rows = con.execute("SELECT id, kind, title, ref FROM items WHERE status='open' AND project=?"
                       " ORDER BY id DESC", (project,)).fetchall()
    jobs = con.execute("SELECT * FROM jobs WHERE status IN ('queued', 'running') AND project=? ORDER BY id",
                       (project,)).fetchall()
    if not rows and not jobs:
        return 0

    def rank(r):
        return RESUME_ORDER.index(r["kind"]) if r["kind"] in RESUME_ORDER else len(RESUME_ORDER)

    rows = sorted(rows, key=rank)  # stable, so newest stays first within a kind
    lines = []
    for r in rows:
        title = " ".join((r["title"] or r["ref"]).split())
        lines.append((rank(r), f"#{r['id']} {r['kind']} {title[:100]}"))
    at = sum(1 for k, _ in lines if k <= RESUME_ORDER.index("agent"))  # jobs follow blockers and agents
    lines[at:at] = [(1, job_line(j)[:100]) for j in jobs]
    total = len(lines)
    room = max(a.max, 2) - 2  # the header and the line for what was cut
    shown = lines if total <= room + 1 else lines[:room]
    print(f"brain: {len(rows)} open and {len(jobs)} queued or running for {project}")
    for _, text in shown:
        print(text)
    if len(shown) < total:
        print(f"... {total - len(shown)} more: brain list --project {project} --status open")
    return 0


def stats(con):
    out = {}
    out["by_kind_status"] = [dict(r) for r in con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM items GROUP BY kind, status ORDER BY kind, status")]
    out["open_by_project"] = [dict(r) for r in con.execute(
        "SELECT project, COUNT(*) AS n FROM items WHERE status='open' GROUP BY project ORDER BY n DESC, project")]
    spans = {}
    for r in con.execute("SELECT kind, first_seen, closed_at FROM items WHERE closed_at IS NOT NULL"):
        spans.setdefault(r["kind"], []).append(epoch(r["closed_at"]) - epoch(r["first_seen"]))
    out["median_close_s"] = {k: statistics.median(v) for k, v in sorted(spans.items())}
    out["suites"] = [dict(r) for r in con.execute(
        "SELECT r.project, r.test_project, COUNT(*) AS runs, ROUND(AVG(r.lane_wait_s), 1) AS mean_lane_wait_s,"
        " ROUND(AVG(r.duration_s), 1) AS mean_duration_s,"
        " (SELECT COUNT(*) FROM items i JOIN runs rr ON rr.id = i.run_id WHERE i.kind='flaky'"
        "  AND rr.project = r.project AND rr.test_project = r.test_project) AS flaky_items"
        " FROM runs r WHERE r.tier != 'rerun' GROUP BY r.project, r.test_project ORDER BY r.project, r.test_project")]
    for s in out["suites"]:
        s["flaky_rate"] = round(s["flaky_items"] / s["runs"], 3) if s["runs"] else 0
    out["slowest"] = [dict(r) for r in con.execute(
        "SELECT id, project, test_project, tier, duration_s FROM runs WHERE duration_s IS NOT NULL"
        " ORDER BY duration_s DESC, id LIMIT 5")]
    return out


def grid(heads, rows):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    w = [max(len(x[i]) for x in rows + [heads]) for i in range(len(heads))]
    return "\n".join("  ".join(c.ljust(n) for c, n in zip(r, w)).rstrip() for r in [heads] + rows)


def cmd_stats(a):
    s = stats(connect())
    if a.json:
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0
    print("items by kind and status")
    print(grid(["kind", "status", "count"], [[r["kind"], r["status"], r["n"]] for r in s["by_kind_status"]]))
    print("\nopen by project")
    print(grid(["project", "open"], [[r["project"], r["n"]] for r in s["open_by_project"]]))
    print("\nmedian time to close")
    print(grid(["kind", "hours"], [[k, round(v / 3600, 2)] for k, v in s["median_close_s"].items()]))
    print("\nsuites (fast and full runs)")
    print(grid(["project", "test project", "runs", "flaky items", "flaky rate", "mean lane wait s", "mean duration s"],
               [[r["project"], r["test_project"], r["runs"], r["flaky_items"], r["flaky_rate"],
                 r["mean_lane_wait_s"], r["mean_duration_s"]] for r in s["suites"]]))
    print("\nslowest runs")
    print(grid(["run", "project", "test project", "tier", "duration s"],
               [[r["id"], r["project"], r["test_project"], r["tier"], r["duration_s"]] for r in s["slowest"]]))
    return 0


def cmd_export(a):
    con = connect()
    q, args = "SELECT * FROM items WHERE 1=1", []
    for col, val in (("project", a.project), ("status", a.status)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    rows = con.execute(q + " ORDER BY project, id", args).fetchall()
    if a.format == "json":
        items = []
        for r in rows:
            d = dict(r)
            d["tags"] = tags_of(con, r["id"])
            items.append(d)
        print(json.dumps(items, indent=2, sort_keys=True))
        return 0
    print("# Brain")
    project = None
    for r in rows:
        if r["project"] != project:
            project = r["project"]
            print(f"\n## {project}\n")
        t = tags_of(con, r["id"])
        print(f"- #{r['id']} {r['status']} {r['kind']} {r['title']}"
              + (f" ({r['severity']})" if r["severity"] else "") + f", seen {r['occurrences']}x, last {r['last_seen']}"
              + (f", tags {', '.join(t)}" if t else ""))
        if r["body"]:
            first = r["body"].strip().splitlines()[0] if r["body"].strip() else ""
            if first:
                print(f"  {first}")
    return 0

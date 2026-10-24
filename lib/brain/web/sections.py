"""Gathers what the brain page shows, one section per goal.

A section that fails puts one line in errors[section] and returns an empty list, so one failure never hides
the rest. Only the forges and worktrees functions run commands; this module runs none itself.

Tested through lib/brain/web/page.py.
"""
import importlib.machinery
import importlib.util
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

import forges  # noqa: E402
import session  # noqa: E402
import worktrees  # noqa: E402

NEEDS_KINDS = ("question", "blocker", "pending", "risk")
_RECOMMEND = re.compile(r"recommend(?:ed|ation)?\s*:", re.I)
_SECTIONS = ("needs_you", "flaky", "queue", "created", "tickets", "worktrees", "tested", "notices", "usage", "decided")


def recommendation(body) -> str:
    """The text after the last Recommend:/Recommended:/Recommendation: marker, or ''."""
    marks = list(_RECOMMEND.finditer(body or ""))
    return (body or "")[marks[-1].end():].strip() if marks else ""


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _needs_you(con):
    marks = ",".join("?" * len(NEEDS_KINDS))
    rows = _rows(con, f"SELECT id, kind, project, ref, title, body, first_seen, last_seen, session_id FROM items "
                      f"WHERE status='open' AND kind IN ({marks}) ORDER BY id DESC LIMIT 200", NEEDS_KINDS)
    for r in rows:
        r["can_send"] = session.can_send(r["kind"])
        r["recommendation"] = recommendation(r["body"]) if r["kind"] == "question" else ""
    return rows


def _flaky(con):
    return _rows(con, "SELECT project, COUNT(*) AS n FROM items WHERE status='open' AND kind='flaky' "
                      "GROUP BY project ORDER BY n DESC, project")


def _queue(con):
    cols = ("j.id, j.project, j.test_project, j.tier, j.status, j.reason, j.branch, j.platform, j.host, "
            "j.enqueued_at, j.started_at, j.ended_at, r.passed, r.failed")
    live = _rows(con, f"SELECT {cols} FROM jobs j LEFT JOIN runs r ON r.id=j.run_id "
                      "WHERE j.status IN ('queued','running') ORDER BY j.status DESC, j.priority DESC, j.id")
    ended = _rows(con, f"SELECT {cols} FROM jobs j LEFT JOIN runs r ON r.id=j.run_id "
                       "WHERE j.status NOT IN ('queued','running') ORDER BY j.id DESC LIMIT 20")
    return live + ended


def _created(con, cache):
    rows = _rows(con, "SELECT id, kind, forge, repo, branch, url, number, title, session_id, cwd, created_at, state "
                      "FROM created ORDER BY id DESC LIMIT 100")
    for r in rows:
        r["pr_state"] = ""
        if r["kind"] == "pr" and r["number"] is not None:
            r["pr_state"] = cache.get(("pr", r["forge"], r["repo"], r["number"]),
                                      lambda r=r: forges.pr_state(r["forge"], r["repo"], r["number"])) or r["state"]
    return rows


def _tested(con, created, repo_of):
    out = []
    for c in created:
        if c["kind"] != "pr":
            continue
        row = {"number": c["number"], "repo": c["repo"], "branch": c["branch"], "url": c["url"],
               "state": c["pr_state"], "run_id": None, "passed": None, "failed": None, "passing": False, "flag": ""}
        mine = repo_of(c["cwd"])
        if mine and c["branch"]:
            jobs = _rows(con, "SELECT j.repo_dir, r.id AS run_id, r.passed, r.failed, r.stalled, r.exit_code "
                              "FROM jobs j JOIN runs r ON r.id=j.run_id WHERE j.branch=? ORDER BY r.id DESC",
                         (c["branch"],))
            hits = [j for j in jobs if (theirs := repo_of(j["repo_dir"])) and theirs[1] == mine[1]]
            if hits:
                row.update(run_id=hits[0]["run_id"], passed=hits[0]["passed"], failed=hits[0]["failed"])
                row["passing"] = any(not j["failed"] and j["passed"] and not j["stalled"] for j in hits)
        if c["pr_state"] == "merged" and not row["passing"]:
            row["flag"] = "merged untested"
        out.append(row)
    return out


def _decided(con):
    return _rows(con, "SELECT id, kind, project, ref, title, body, status, resolution, closed_at, last_seen FROM items "
                      "WHERE (status='closed' AND resolution='answered') OR kind='ruling' ORDER BY id DESC LIMIT 50")


def _notices(con):
    return _rows(con, "SELECT id, hook, text, session_id, cwd, created_at FROM notices "
                      "WHERE dismissed_at='' ORDER BY id DESC LIMIT 200")


def usage_rows():
    root = os.path.dirname(os.path.dirname(HERE))
    path = os.path.join(root, "bin", "lean-stats")
    loader = importlib.machinery.SourceFileLoader("lean_stats_cli", path)
    spec = importlib.util.spec_from_loader("lean_stats_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    ls = mod.leanstats
    if not os.path.exists(ls.db_path()):
        raise FileNotFoundError("lean-stats has no data yet")
    conn = ls.connect()
    try:
        return [{"label": a, "value": b, "basis": c} for a, b, c in mod.measures(conn, "")]
    finally:
        conn.close()


def _cli_notes(status):
    notes = []
    for cli, st in status.items():
        if st == "missing":
            notes.append(f"{cli} is not installed")
        elif st == "logged out":
            notes.append(f"{cli} is logged out")
    return "; ".join(notes)


def collect(con, cache, roots, which=None, run=None) -> dict:
    data = {s: [] for s in _SECTIONS}
    data["errors"] = {}
    state = {"locked": False}

    def guard(name, fn, db=True):
        if db and state["locked"]:
            data["errors"][name] = "the brain database is locked"
            return
        try:
            data[name] = fn()
        except sqlite3.OperationalError as e:
            if "lock" in str(e) or "busy" in str(e):
                state["locked"] = True
                data["errors"][name] = "the brain database is locked"
            else:
                data["errors"][name] = f"could not read: {e}"
        except Exception as e:
            data["errors"][name] = f"could not read: {e}"

    try:
        con.execute("PRAGMA busy_timeout=2000")
    except Exception:
        pass
    paths = {}

    def repo_of(path):
        if path not in paths:
            paths[path] = worktrees.repo_of(path) if path else None
        return paths[path]

    guard("needs_you", lambda: _needs_you(con))
    guard("flaky", lambda: _flaky(con))
    guard("queue", lambda: _queue(con))
    guard("created", lambda: _created(con, cache))
    guard("tested", lambda: _tested(con, data["created"], repo_of))
    guard("decided", lambda: _decided(con))
    guard("notices", lambda: _notices(con))

    def tickets():
        kw = {}
        if which:
            kw["which"] = which
        if run:
            kw["run"] = run
        status = {cli: forges.available(cli, **kw) for cli in ("gh", "az")}
        note = _cli_notes(status)
        if note:
            data["errors"]["tickets"] = note
        if "ok" not in status.values():
            return []
        return cache.get("tickets", lambda: forges.pending_tickets(**({"run": run} if run else {}))) or []
    guard("tickets", tickets, db=False)

    def trees():
        rows = []
        for repo in worktrees.known_repos(None if state["locked"] else con, roots):
            for w in worktrees.list_worktrees(repo):
                w["repo"] = repo
                rows.append(w)
        keys = set()
        for c in data["created"]:
            mine = repo_of(c["cwd"])
            if c["pr_state"] == "merged" and c["branch"] and mine:
                keys.add((mine[1], c["branch"]))
        for w in rows:
            mine = repo_of(w["repo"])
            w["merged"] = bool(mine and w["branch"] and (mine[1], w["branch"]) in keys)
        return rows
    guard("worktrees", trees, db=False)
    guard("usage", usage_rows, db=False)
    return data

"""The brain page as HTML. No JavaScript, no external assets; every value is escaped.

  python3 lib/brain/web/page.py --self-test
"""
import html
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CSS = """body{font:15px/1.4 system-ui,sans-serif;margin:1.5rem auto;max-width:70rem;padding:0 1rem;color:#222}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid #ccc}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.25rem .5rem;border-bottom:1px solid #eee;vertical-align:top}
.note{color:#8a4b00}.flag{color:#b00020;font-weight:bold}.muted{color:#666}
form{display:inline-block;margin:.2rem .4rem .2rem 0}textarea{width:30rem;height:3.5rem;display:block}
pre{white-space:pre-wrap;margin:.2rem 0}"""


def e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def link(url, text=None) -> str:
    u = "" if url is None else str(url)
    if u.lower().startswith(("http://", "https://")):
        return f'<a href="{e(u)}">{e(text if text is not None else u)}</a>'
    return e(text if text is not None else u)


def _key(key) -> str:
    return f'<input type="hidden" name="key" value="{e(key)}">'


def _form(action, key, fields, label, extra=""):
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields)
    return f'<form method="post" action="{e(action)}">{hidden}{_key(key)}{extra}<button>{e(label)}</button></form>'


def _unknown(v, yes="yes", no="no") -> str:
    return "unknown" if v is None else (yes if v else no)


def _section(title, errors, name, body, empty="Nothing here."):
    note = errors.get(name)
    out = [f"<h2>{e(title)}</h2>"]
    if note:
        out.append(f'<p class="note">{e(note)}</p>')
    out.append(body or (f'<p class="muted">{e(empty)}</p>' if not note else ""))
    return "".join(out)


def _table(head, rows):
    if not rows:
        return ""
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    return f"<table><tr>{th}</tr>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def _needs(data, key):
    rows = []
    for it in data.get("needs_you", []):
        actions = []
        if it.get("can_send"):
            actions.append(_form("/session", key, [("id", it["id"])], "Start a session"))
        if it.get("kind") == "question":
            if it.get("recommendation"):
                actions.append(f'<div>Recommended: {e(it["recommendation"])}</div>')
                actions.append(_form("/answer", key, [("id", it["id"]), ("approve", 1)], "Approve"))
            actions.append(f'<form method="post" action="/answer"><input type="hidden" name="id" value="{e(it["id"])}">'
                           f'{_key(key)}<textarea name="answer"></textarea><button>Answer</button></form>')
        body = f'<pre>{e(it.get("body"))}</pre>' if it.get("body") else ""
        rows.append([e(it["id"]), e(it.get("kind")), e(it.get("project")),
                     f'{e(it.get("title") or it.get("ref"))}{body}', "".join(actions)])
    out = _table(["#", "Kind", "Project", "Item", ""], rows)
    if (data.get("errors") or {}).get("flaky"):
        out += f'<p class="note">flaky: {e(data["errors"]["flaky"])}</p>'
    for f in data.get("flaky", []):
        out += f'<details><summary>{e(f["n"])} flaky items in {e(f["project"])}</summary>' \
               f'<p class="muted">List them with brain list --kind flaky.</p></details>'
    return out


def _queue(data):
    rows = []
    for j in data.get("queue", []):
        result = "" if j.get("passed") is None else f'{e(j["passed"])} passed, {e(j["failed"])} failed'
        rows.append([e(j["id"]), e(j.get("status")), e(j.get("project")), e(j.get("test_project")), e(j.get("tier")),
                     e(j.get("branch")), e(j.get("platform")), result, e(j.get("reason"))])
    return _table(["Job", "Status", "Project", "Test", "Tier", "Branch", "Lane", "Result", "Reason"], rows)


def _created(data):
    rows = []
    for c in data.get("created", []):
        title = link(c.get("url"), c.get("title") or c.get("url"))
        rows.append([e(c["kind"]), e(c["forge"]), e(c.get("repo")), title, e(c.get("pr_state") or c.get("state")),
                     e(c.get("session_id")), e(c.get("created_at"))])
    return _table(["Kind", "Where", "Repo", "Title", "State", "Session", "Made"], rows)


def _tickets(data):
    rows = [[e(t["forge"]), e(t.get("repo")), link(t.get("url"), f'#{t["number"]} {t.get("title")}'), e(t.get("updated"))]
            for t in data.get("tickets", [])]
    return _table(["Where", "Repo", "Ticket", "Updated"], rows)


def _trees(data):
    rows = []
    for w in data.get("worktrees", []):
        if w.get("missing"):
            state = "folder is missing"
        else:
            unpushed = "unknown" if w.get("unpushed") is None else str(w["unpushed"])
            state = f'unpushed: {e(unpushed)}, uncommitted: {e(_unknown(w.get("dirty")))}'
        rows.append([e(w.get("repo")), e(w["path"]), e(w.get("branch")), state,
                     "PR merged, safe to remove" if w.get("merged") else ""])
    return _table(["Repo", "Path", "Branch", "State", ""], rows)


def _tested(data):
    rows = []
    for t in data.get("tested", []):
        run = "no run" if t.get("run_id") is None else f'run {e(t["run_id"])}: {e(t["passed"])} passed, {e(t["failed"])} failed'
        flag = f'<span class="flag">{e(t["flag"])}</span>' if t.get("flag") else ""
        rows.append([link(t.get("url"), f'{t.get("repo")} #{t.get("number")}'), e(t.get("branch")), e(t.get("state")), run, flag])
    return _table(["PR", "Branch", "State", "Last run", ""], rows)


def _notices(data, key):
    rows = [[e(n.get("hook")), e(n.get("text")), e(n.get("created_at")),
             _form("/dismiss", key, [("id", n["id"])], "Dismiss")] for n in data.get("notices", [])]
    return _table(["Hook", "Notice", "When", ""], rows)


def _usage(data):
    return _table(["Measure", "Value", "Basis"], [[e(u["label"]), e(u["value"]), e(u["basis"])] for u in data.get("usage", [])])


def _decided(data):
    rows = [[e(d["id"]), e(d.get("kind")), e(d.get("project")), e(d.get("title") or d.get("ref")),
             f'<pre>{e(d.get("body"))}</pre>'] for d in data.get("decided", [])]
    return _table(["#", "Kind", "Project", "Item", "Answer"], rows)


def render(data: dict, key: str, port: int) -> str:
    err = data.get("errors") or {}
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f"<title>lean brain</title><style>{CSS}</style></head><body><h1>lean brain</h1>"
        f'<p>updated {time.strftime("%H:%M")} <a href="/">Refresh</a></p>',
        _section("Needs you", err, "needs_you", _needs(data, key), "Nothing is waiting on you."),
        _section("Test queue", err, "queue", _queue(data), "The queue is empty."),
        _section("Created by agents", err, "created", _created(data), "Nothing created yet."),
        _section("Pending tickets", err, "tickets", _tickets(data), "No open tickets assigned to you."),
        _section("Worktrees", err, "worktrees", _trees(data), "No worktrees found."),
        _section("Tested or not", err, "tested", _tested(data), "No pull requests to check."),
        _section("Notices", err, "notices", _notices(data, key), "No notices."),
        _section("Usage", err, "usage", _usage(data), "No usage data."),
        _section("Decided", err, "decided", _decided(data), "Nothing decided yet."),
        "</body></html>",
    ]
    return "".join(parts)


def _self_test() -> int:
    import shutil
    import sqlite3
    import subprocess
    import tempfile
    import time
    tmp = tempfile.mkdtemp()
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "brain.db")
    os.environ["LEAN_DB"] = os.path.join(tmp, "nostats", "lean.db")
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    sys.path.insert(0, os.path.join(HERE, "..", ".."))
    import forges
    import sections
    import worktrees
    from brain import store

    con = store.connect(timeout=2)
    ts = "2026-10-24T00:00:00Z"
    ins = ("INSERT INTO items (project, kind, ref, title, body, status, resolution, first_seen, last_seen) "
           "VALUES (?,?,?,?,?,?,?,?,?)")
    con.execute(ins, ("p", "question", "q1", "<script>alert(1)</script>",
                      "Which one?\nRecommend: use A\nRecommended: use B", "open", "", ts, ts))
    con.execute(ins, ("p", "question", "q2", "no marker", "just a question", "open", "", ts, ts))
    con.execute(ins, ("p", "blocker", "b1", "stuck", "", "open", "", ts, ts))
    con.execute(ins, ("p", "ruling", "r1", "a ruling", "decided so", "open", "", ts, ts))
    con.execute(ins, ("p", "question", "q3", "done q", "yes", "closed", "answered", ts, ts))
    for i in range(407):
        con.execute(ins, (f"proj{i % 2}", "flaky", f"f{i}", f"flaky {i}", "", "open", "", ts, ts))
    con.execute("INSERT INTO notices (hook, text, created_at) VALUES ('h', '<b>note</b>', ?)", (ts,))
    repo = os.path.realpath(os.path.join(tmp, "repo"))
    os.makedirs(repo)
    genv = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, capture_output=True, env=genv)
    con.execute("INSERT INTO created (kind, forge, repo, branch, url, number, title, created_at, state, cwd) "
                "VALUES ('pr','github','o/r','feat','https://github.com/o/r/pull/1',1,'t',?, 'merged', ?)", (ts, repo))
    con.execute("INSERT INTO created (kind, forge, repo, branch, url, number, title, created_at, state, cwd) "
                "VALUES ('pr','github','o/r','tested','javascript:alert(1)',2,'u',?, 'open', ?)", (ts, repo))
    con.execute("INSERT INTO runs (project, test_project, passed, failed, started) VALUES ('p','t',5,0,1)")
    con.execute("INSERT INTO jobs (project, repo_dir, test_project, status, enqueued_at, run_id, branch) "
                "VALUES ('p', ?, 't', 'done', ?, 1, 'tested')", (repo, ts))
    con.commit()

    nowhere = lambda n: None

    class R:
        stdout, returncode = "", 1
    calls = []
    live = [0, 0]

    def failing(argv, **kw):
        calls.append(argv)
        return R()
    cache = forges.Cache()
    first = sections.collect(con, cache, [], which=nowhere, run=failing)
    check("first load shows checking", [c["pr_state"] for c in first["created"]], ["checking", "merged"])
    check("tickets checking", first["errors"].get("tickets"), "checking")
    cache.wait()
    data = sections.collect(con, cache, [], which=nowhere, run=failing)
    check("pr state falls back to row", [c["pr_state"] for c in data["created"]], ["open", "merged"])
    check("merged row never calls run", any(a[:3] == ["gh", "pr", "view"] and a[3] == "1" for a in calls), False)
    check("flaky grouped", sorted(f["n"] for f in data["flaky"]), [203, 204])
    check("needs_you kinds", sorted(i["ref"] for i in data["needs_you"]), ["b1", "q1", "q2"])
    q1 = [i for i in data["needs_you"] if i["ref"] == "q1"][0]
    check("recommendation is after the last marker", q1["recommendation"], "use B")
    check("can_send", q1["can_send"], True)
    check("decided", sorted(d["ref"] for d in data["decided"]), ["q3", "r1"])
    flags = {t["number"]: (t["flag"], t["passing"], t["run_id"]) for t in data["tested"]}
    check("merged untested", flags[1], ("merged untested", False, None))
    check("tested pr passes", flags[2], ("", True, 1))
    check("gh missing note", "gh is not installed" in data["errors"].get("tickets", ""), True)
    check("usage error not crash", "usage" in data["errors"], True)
    page = render(data, "KEY123", 8770)
    check("script escaped", "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page, True)
    check("notice escaped", "&lt;b&gt;note&lt;/b&gt;" in page, True)
    check("flaky lines", page.count("flaky items in"), 2)
    check("no row per flaky", "flaky 100" in page, False)
    check("merged untested shown", "merged untested" in page, True)
    check("gh note shown", "gh is not installed" in page, True)
    check("no auto refresh", "http-equiv" in page, False)
    check("refresh link", ('<a href="/">Refresh</a>' in page, "updated " in page), (True, True))
    check("key in forms", page.count('name="key" value="KEY123"') >= 5, True)
    check("approve form", 'name="approve" value="1"' in page, True)
    check("approve only with marker", page.count('name="approve"'), 1)
    check("javascript url is text", 'href="javascript' not in page, True)
    check("https url is link", 'href="https://github.com/o/r/pull/1"' in page, True)
    check("quote in href escaped", 'href="https://x/"onmouseover=1"' not in link('https://x/"onmouseover=1'), True)
    check("unknown dirty", "uncommitted: unknown" in render({"worktrees": [{"path": "/p", "branch": "b", "unpushed": None,
          "dirty": None, "missing": False, "repo": "/r"}], "errors": {}}, "k", 1), True)
    check("missing worktree", "folder is missing" in render({"worktrees": [{"path": "/p", "branch": "b", "unpushed": 0,
          "dirty": False, "missing": True, "repo": "/r"}], "errors": {}}, "k", 1), True)
    con.close()

    # slow forge: 50 open PRs, 2 s per call, load returns at once, one worker
    con = sqlite3.connect(os.environ["LEAN_BRAIN_DB"], timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("DELETE FROM created")
    for n in range(50):
        con.execute("INSERT INTO created (kind, forge, repo, url, number, created_at, state) "
                    "VALUES ('pr','github','o/r',?,?,?,'open')", (f"https://x/{n}", n + 1, ts))
    con.commit()

    def slow(argv, **kw):
        live[0] += 1
        live[1] = max(live[1], live[0])
        time.sleep(0.05)
        live[0] -= 1
        r = R()
        r.returncode, r.stdout = 0, '{"state":"MERGED"}'
        return r
    sc = forges.Cache()
    t0 = time.monotonic()
    d1 = sections.collect(con, sc, [], which=nowhere, run=slow)
    check("slow forge load is quick", time.monotonic() - t0 < 1, True)
    check("all checking", {c["pr_state"] for c in d1["created"]}, {"checking"})
    check("checking rendered", "checking" in render(d1, "k", 1), True)
    sc.wait(30)
    d2 = sections.collect(con, sc, [], which=nowhere, run=slow)
    check("second load has states", {c["pr_state"] for c in d2["created"]}, {"merged"})
    check("one worker at a time", live[1], 1)

    # a slow worktree scan: the load returns at once, the second load has the rows
    real_repos, real_list = worktrees.repos_from, worktrees.list_worktrees
    worktrees.repos_from = lambda paths, run=None: (time.sleep(0.5), ["/r"])[1]
    worktrees.list_worktrees = lambda repo, run=None: [{"path": "/r/w", "branch": "b", "unpushed": 0, "dirty": False,
                                                        "missing": False}]
    try:
        wc = forges.Cache()
        t0 = time.monotonic()
        w1 = sections.collect(con, wc, [], which=nowhere, run=slow)
        check("slow worktree load is quick", time.monotonic() - t0 < 0.4, True)
        check("worktrees scanning", (w1["worktrees"], w1["errors"].get("worktrees")), ([], "scanning"))
        wc.wait(30)
        w2 = sections.collect(con, wc, [], which=nowhere, run=slow)
        check("worktrees after scan", ([w["path"] for w in w2["worktrees"]], "worktrees" in w2["errors"]), (["/r/w"], False))
    finally:
        worktrees.repos_from, worktrees.list_worktrees = real_repos, real_list
    con.close()


    # a locked database: rollback journal so the exclusive lock blocks readers
    lock = sqlite3.connect(os.environ["LEAN_BRAIN_DB"], isolation_level=None)
    lock.execute("PRAGMA journal_mode=DELETE")
    lock.execute("BEGIN EXCLUSIVE")
    c2 = sqlite3.connect(os.environ["LEAN_BRAIN_DB"], timeout=2)
    c2.row_factory = sqlite3.Row
    t0 = time.monotonic()
    d2 = sections.collect(c2, forges.Cache(), [], which=nowhere)
    took = time.monotonic() - t0
    check("locked collect under 3s", took < 3, True)
    check("locked errors filled", all(k in d2["errors"] for k in ("needs_you", "queue", "created", "decided")), True)
    check("locked page renders", "lean brain" in render(d2, "k", 1) and "locked" in render(d2, "k", 1), True)
    lock.execute("ROLLBACK")
    lock.close()
    c2.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("self-test: ok" if not fails else f"self-test: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)

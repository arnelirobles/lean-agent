"""The brain page as HTML. No JavaScript, no external assets; every value is escaped.

  python3 lib/brain/web/page.py --self-test
"""
import html
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import activity  # noqa: E402

CSS = """:root{--ink:#1c2321;--body:#4d5552;--muted:#6b736f;--leaf:#0b7a55;--leaf-dark:#075a3e;--sun:#ffcd3c;--cream:#fff8e8;
--sand:#fbf7ee;--line:#ece4d0;--rust:#c2461a;--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
html{overflow-x:clip}
body{font:15px/1.5 "Plus Jakarta Sans",ui-sans-serif,system-ui,sans-serif;margin:0 auto;max-width:1160px;padding:0 20px 64px;
color:var(--ink);background:#fff;-webkit-font-smoothing:antialiased;overflow-x:clip}
@media(min-width:640px){body{padding:0 40px 64px}}
a{color:var(--leaf);font-weight:600;text-underline-offset:2px}a:hover{color:var(--leaf-dark)}
h1{position:sticky;top:0;z-index:2;margin:0 -100vmax 20px;padding:14px 100vmax;background:rgba(255,255,255,.94);
border-bottom:1px solid var(--line);font-size:19px;font-weight:700;letter-spacing:-.01em;line-height:1.3}
h1::before{content:"";display:inline-block;width:12px;height:12px;margin-right:10px;border-radius:50%;background:var(--leaf)}
h2{margin:40px 0 12px;color:var(--rust);font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
h3{margin:24px 0 8px;font-size:15px}
table{border-collapse:separate;border-spacing:0;width:100%;border:1px solid var(--line);border-radius:14px;overflow:hidden;
box-shadow:0 1px 2px rgba(28,35,33,.04)}
td,th{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:break-word}
table:not(.md) td:nth-child(n+4){overflow-wrap:anywhere}
@media(max-width:800px){table:not(.md),table:not(.md) tbody,table:not(.md) tr,table:not(.md) td{display:block}
table:not(.md) tr:has(th){display:none}table:not(.md) td{border:0;padding:4px 14px;min-width:0}
table:not(.md) tr{padding:8px 0;border-bottom:1px solid var(--line)}table:not(.md) tr:last-child{border-bottom:0}
td form,td textarea,td button{display:block;margin:6px 0}}
tr:last-child td{border-bottom:0}
th{background:var(--sand);color:var(--body);font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
tr:hover td{background:var(--sand)}
.note{color:var(--rust)}.flag{color:var(--rust);font-weight:700}.muted{color:var(--muted)}
form{display:inline-block;margin:4px 8px 4px 0;vertical-align:middle}
form[action="/ask"]{box-sizing:border-box;display:block;margin:0 0 12px;padding:20px;background:var(--cream);
border:1px solid var(--line);border-radius:16px}
form[action="/assistant-key"]{display:block}
textarea,input[type=password],select{box-sizing:border-box;font:inherit;color:var(--ink);background:#fff;
border:1.5px solid var(--line);border-radius:12px;padding:10px 12px}
textarea{display:block;width:100%;height:6rem;margin:4px 0 12px;resize:vertical}
input[type=password]{max-width:100%;margin-right:8px}
select{padding-right:8px;margin-right:6px}
textarea:focus,input:focus,select:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--leaf);outline-offset:1px}
button{white-space:nowrap;font:inherit;font-size:12.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;
color:#fff;background:var(--leaf);border:0;border-radius:999px;padding:9px 18px;transition:background-color .2s,filter .2s}
button:hover{background:var(--leaf-dark)}
form[action="/ask"] button{color:var(--ink);background:var(--sun);padding:12px 26px;box-shadow:0 6px 16px rgba(255,205,60,.35)}
form[action="/ask"] button:hover{filter:brightness(.97)}
pre,code{font-family:var(--mono);font-size:13px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0;padding:12px 14px;background:var(--sand);border:1px solid var(--line);border-radius:12px}
summary{cursor:pointer;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 16px}
.tile{padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:var(--cream);font-size:13px;color:var(--body)}
.tile b{display:block;font-size:26px;line-height:1.2;color:var(--ink)}.tile span{display:block;margin-top:2px;color:var(--muted)}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:800px){.charts{grid-template-columns:1fr}}
.chart{margin:0;padding:14px 16px;min-width:0;border:1px solid var(--line);border-radius:14px;background:#fff}
.answer .chart{margin:0 0 12px}.chart figcaption{margin-bottom:6px;font-size:13px;font-weight:700;color:var(--ink)}
.chart .plot{display:block}.chart .grid{stroke:var(--line);stroke-width:1}.chart .base{stroke:var(--muted);stroke-width:1}
.chart .tick{fill:var(--muted);font-size:11px}.chart .lbl{fill:var(--ink);font-size:11px;font-weight:700}
.chart .legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0 0 6px;padding:0;list-style:none;font-size:12px;color:var(--body)}
.chart .sw{display:inline-block;width:10px;height:10px;margin-right:6px;border-radius:3px}
.sw.s0{background:#0b7a55}.sw.s1{background:#c2461a}.sw.s2{background:#7b4fa8}
.chart details{margin-top:8px;font-size:12px}.chart details table{font-size:12px}
.answer{line-height:1.6}.answer p,.answer ul,.answer ol,.answer blockquote{margin:0 0 12px}
.answer ul,.answer ol{padding-left:24px}.answer li{margin:3px 0}.answer li>ul,.answer li>ol{margin:3px 0}
.answer h3,.answer h4{margin:20px 0 6px;color:var(--ink);line-height:1.3}.answer h4{font-size:14px}
.answer blockquote{padding:2px 0 2px 14px;border-left:3px solid var(--leaf);color:var(--body)}
.answer hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.answer :not(pre)>code{padding:1px 5px;background:var(--sand);border-radius:6px}
.scroll{overflow-x:auto;margin:0 0 12px}.scroll table.md{width:auto;min-width:100%;overflow:visible}
table.md td,table.md th{overflow-wrap:normal}
details.tool{margin:8px 0;font-size:13px;color:var(--muted)}details.tool summary{font-weight:500}details.tool pre{font-size:12px}"""


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


def _options(pairs, chosen):
    return "".join(f'<option value="{e(v)}"{" selected" if v == chosen else ""}>{e(t)}</option>' for v, t in pairs)


def _ask(st, key):
    """The Ask section. st comes from Assistant.state(); it holds no key."""
    out = ["<h2>Ask</h2>"]
    if st.get("too_old"):
        out.append('<p class="note">The assistant needs Python 3.11 or later.</p>')
        return "".join(out)
    if not st.get("sdk"):
        out.append('<p class="note">The assistant is not set up. Run this once, then reload:</p>'
                   f'<pre>{e(st.get("setup"))}</pre>')
        return "".join(out)
    sources = {"env": "an Anthropic key from the environment", "keychain": "the key saved in the macOS Keychain",
               "default": "the Anthropic SDK default credentials"}
    if st.get("source"):
        models = [(m, st["labels"].get(m, m)) for m in st["models"]]
        spent, cap = f'{st["usd"]:.2f}', f'{st["limit"]:g}'
        out.append(f'<form method="post" action="/ask">{_key(key)}<textarea name="question" maxlength="{e(st["max_question"])}"'
                   ' placeholder="Ask about your brain"></textarea>'
                   f'<select name="model">{_options(models, st["default_model"])}</select> '
                   f'<select name="effort">{_options([(x, x) for x in st["efforts"]], st["default_effort"])}</select> '
                   '<button>Ask</button></form>')
        out.append(f'<p class="muted">Using {e(sources.get(st["source"], st["source"]))}. '
                   f'Spent so far: about ${e(spent)} (estimate) over {e(st["questions"])} questions. '
                   f'Cap per question: ${e(cap)}.</p>')
        if st["source"] == "keychain":
            out.append(_form("/assistant-key-remove", key, [], "Remove the saved key"))
    else:
        out.append('<p class="note">No Anthropic credentials found.</p>')
    if st.get("source") in (None, "default"):
        if st.get("mac"):
            out.append(f'<form method="post" action="/assistant-key">{_key(key)}'
                       '<input type="password" name="apikey" autocomplete="off" placeholder="sk-ant-..." size="40"> '
                       '<button>Save key in the Keychain</button></form>')
            out.append('<p class="muted">Or set ANTHROPIC_API_KEY, or run ant auth login, then restart brain serve.</p>')
        else:
            out.append('<p class="muted">Set ANTHROPIC_API_KEY in the environment brain serve runs in, '
                       'or run ant auth login, then restart brain serve.</p>')
    out.append('<p class="muted">Questions and the brain rows the assistant reads are sent to Anthropic under your own account.</p>')
    return "".join(out)


def _activity(data, err):
    if not data.get("activity"):
        return ""
    try:
        return activity.render(data["activity"])
    except Exception as ex:
        err["activity"] = f"could not draw: {type(ex).__name__}"
        return ""


def render(data: dict, key: str, port: int, ask=None) -> str:
    err = dict(data.get("errors") or {})
    parts = [
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>lean brain</title><style>{CSS}</style></head><body><h1>lean brain</h1>"
        f'<p>updated {time.strftime("%H:%M")} <a href="/">Refresh</a></p>',
        _ask(ask, key) if ask else "",
        _section("Needs you", err, "needs_you", _needs(data, key), "Nothing is waiting on you."),
        _section("Activity", err, "activity", _activity(data, err), "No activity yet."),
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

    media = CSS[CSS.index("@media(max-width:800px)"):]
    media = media[:media.index("}}") + 2].split("{", 1)[1]
    sels = [x.strip() for g in re.findall(r"([^{}@]+)\{", media) for x in g.split(",")]
    loose = [x for x in sels if not x.startswith(("table:not(.md)", "td form", "td textarea", "td button"))]
    check("stacking rule skips answer tables", (loose, "table:not(.md) td:nth-child(n+4)" in CSS, ".scroll{overflow-x:auto" in CSS), ([], True, True))

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
    check("usage error is no data, not a missing script", "no data yet" in data["errors"].get("usage", ""), True)
    page = render(data, "KEY123", 8770)
    check("activity collected", (len(data["activity"]["days"]), "activity" in data["errors"]), (14, False))
    check("activity section drawn", ("<h2>Activity</h2>" in page, page.count("<figure"), "Tests failed today" in page), (True, 5, True))
    broken = render({**data, "activity": {"days": ["2026-01-01"]}}, "k", 1)
    check("activity failure keeps the page", ("could not draw" in broken, "<h2>Needs you</h2>" in broken), (True, True))
    check("script escaped", "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page, True)
    check("notice escaped", "&lt;b&gt;note&lt;/b&gt;" in page, True)
    check("flaky lines", page.count("flaky items in"), 2)
    check("no row per flaky", "flaky 100" in page, False)
    check("merged untested shown", "merged untested" in page, True)
    check("gh note shown", "gh is not installed" in page, True)
    check("no auto refresh", "http-equiv" in page, False)
    check("refresh link", ('<a href="/">Refresh</a>' in page, "updated " in page), (True, True))
    import assistant
    off = {"sdk": False, "source": None, "mac": True, "setup": "python3 /x/brain assistant-setup", "usd": 0, "questions": 0,
           "limit": 10, "labels": assistant.LABELS, "models": assistant.MODELS, "efforts": assistant.EFFORTS,
           "default_model": assistant.DEFAULT_MODEL, "default_effort": assistant.DEFAULT_EFFORT, "max_question": 8000}
    with_ask = render(data, "KEY123", 8770, off)
    strip = lambda h: re.sub(r"updated \d\d:\d\d", "updated", h)
    check("ask hidden on old python", 'name="question"' in render(data, "k", 1, {**off, "sdk": True, "source": "env", "too_old": True}), False)
    check("ask section when sdk missing", ("<h2>Ask</h2>" in with_ask, "assistant-setup" in with_ask), (True, True))
    check("page otherwise unchanged", strip(re.sub(r"<h2>Ask</h2>.*?(?=<h2>Needs you</h2>)", "", with_ask)), strip(page))
    ready = render(data, "KEY123", 8770, {**off, "sdk": True, "source": "keychain", "setup": "<b>x</b>"})
    check("ask form and remove button", ('name="question"' in ready, "/assistant-key-remove" in ready, 'name="apikey"' in ready),
          (True, True, False))
    nokey = render(data, "KEY123", 8770, {**off, "sdk": True, "mac": False})
    check("no key form off mac", ('name="apikey"' in nokey, "ANTHROPIC_API_KEY" in nokey), (False, True))
    check("ask values escaped", "<b>x</b>" in render(data, "k", 1, {**off, "setup": "<b>x</b>"}), False)
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

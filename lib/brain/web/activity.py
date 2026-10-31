"""The Activity section of the brain page: daily counts from the brain database and lean-stats, as tiles and charts.

Days are local calendar days and every day in the window is present, zero when nothing happened. run.started
is epoch seconds; items.first_seen, items.closed_at, created.created_at and agent_runs times are UTC text, so
they are bucketed in Python after converting to local time.

  python3 lib/brain/web/activity.py --self-test
"""
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

import charts  # noqa: E402

NOISE = ("test-fail", "flaky")
TOP_PROJECTS = 8


def _local_day_text(s):
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone().date()


def _local_day_epoch(t):
    try:
        return datetime.datetime.fromtimestamp(float(t)).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _utc_text(day):
    """UTC text for the start of a local day, a day earlier, so a SQL prefilter never drops a row."""
    start = datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time()).astimezone()
    return start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(day):
    return datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time()).timestamp()


def collect(con, days=14, now=None, stats=None) -> dict:
    today = _local_day_epoch(time.time() if now is None else now)
    window = [today - datetime.timedelta(days=days - 1 - i) for i in range(days)]
    slot = {d: i for i, d in enumerate(window)}
    first = window[0]

    def zeros():
        return [0] * days

    def put(arr, day, amount=1):
        i = slot.get(day)
        if i is not None:
            arr[i] += amount

    failed, runs, minutes, opened, closed, prs, issues = (zeros() for _ in range(7))
    for r in con.execute("SELECT started, failed, duration_s FROM runs WHERE started >= ?", (_epoch(first),)):
        d = _local_day_epoch(r[0])
        put(failed, d, r[1] or 0)
        put(runs, d)
        put(minutes, d, (r[2] or 0) / 60.0)
    quiet = "kind NOT IN (%s)" % ",".join("?" * len(NOISE))
    since = _utc_text(first)
    for r in con.execute(f"SELECT first_seen FROM items WHERE {quiet} AND first_seen >= ?", NOISE + (since,)):
        put(opened, _local_day_text(r[0]))
    for r in con.execute(f"SELECT closed_at FROM items WHERE {quiet} AND closed_at >= ?", NOISE + (since,)):
        put(closed, _local_day_text(r[0]))
    for r in con.execute("SELECT kind, created_at FROM created WHERE kind IN ('pr','issue') AND created_at >= ?", (since,)):
        put(prs if r[0] == "pr" else issues, _local_day_text(r[1]))
    projects = [(r[0], r[1]) for r in con.execute(
        f"SELECT project, COUNT(*) AS n FROM items WHERE status='open' AND {quiet} GROUP BY project "
        "ORDER BY n DESC, project LIMIT ?", NOISE + (TOP_PROJECTS,))]
    data = {"days": [d.isoformat() for d in window], "failed": failed, "runs": runs,
            "minutes": [round(m, 1) for m in minutes], "opened": opened, "closed": closed, "prs": prs,
            "issues": issues, "projects": projects, "agent": None}
    if stats is not None:
        try:
            data["agent"] = _agent(stats, window, slot, since)
        except Exception:
            data["agent"] = None
    return data


def _agent(stats, window, slot, since):
    _mod, conn = stats()
    try:
        rows = conn.execute("SELECT COALESCE(NULLIF(ended_at, ''), started_at) AS t, usd FROM agent_runs "
                            "WHERE COALESCE(NULLIF(ended_at, ''), started_at) >= ?", (since,)).fetchall()
    finally:
        conn.close()
    count, usd = [0] * len(window), [0.0] * len(window)
    priced = False
    for t, u in rows:
        i = slot.get(_local_day_text(t))
        if i is None:
            continue
        count[i] += 1
        if u is not None:
            usd[i] += u
            priced = True
    if not sum(count):
        return None
    if priced:
        return {"name": "Agent spend per day (USD, list price)", "series": "USD", "values": [round(x, 2) for x in usd]}
    return {"name": "Agent runs per day", "series": "Runs", "values": count}


def _num(v):
    return f"{v:.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def tiles(data):
    out = []
    for label, key in (("Tests failed", "failed"), ("Test runs", "runs"), ("Test minutes", "minutes"),
                       ("Work items closed", "closed"), ("PRs opened", "prs")):
        v = data[key]
        out.append((label, v[-1], v[-2] if len(v) > 1 else None))
    return out


def specs(data):
    labels = [d[5:] for d in data["days"]]
    out = [
        {"title": "Tests failed per day", "kind": "bar", "labels": labels, "series": [{"name": "Failed", "values": data["failed"]}]},
        {"title": "Test minutes per day", "kind": "bar", "labels": labels, "series": [{"name": "Minutes", "values": data["minutes"]}]},
        {"title": "Work items opened and closed per day", "kind": "line", "labels": labels,
         "series": [{"name": "Opened", "values": data["opened"]}, {"name": "Closed", "values": data["closed"]}]},
        {"title": "PRs and issues created per day", "kind": "bar", "labels": labels,
         "series": [{"name": "PRs", "values": data["prs"]}, {"name": "Issues", "values": data["issues"]}]},
    ]
    if data.get("agent"):
        a = data["agent"]
        out.append({"title": a["name"], "kind": "bar", "labels": labels, "series": [{"name": a["series"], "values": a["values"]}]})
    if data["projects"]:
        out.append({"title": "Open items by project", "kind": "hbar", "labels": [p[:charts.MAX_LABEL] for p, _ in data["projects"]],
                    "series": [{"name": "Open", "values": [n for _, n in data["projects"]]}]})
    return out


def render(data) -> str:
    cells = []
    for label, today, before in tiles(data):
        yest = "no earlier day" if before is None else f"yesterday {_num(before)}"
        cells.append(f'<div class="tile"><b>{charts.esc(_num(today))}</b>{charts.esc(label)} today'
                     f'<span>{charts.esc(yest)}</span></div>')
    figs = []
    for spec in specs(data):
        try:
            figs.append(charts.svg(spec))
        except ValueError:
            continue
    return '<div class="tiles">' + "".join(cells) + '</div><div class="charts">' + "".join(figs) + "</div>"


def _self_test() -> int:
    import shutil
    import sqlite3
    import tempfile
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    tmp = tempfile.mkdtemp()
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "brain.db")
    os.environ["LEAN_DB"] = os.path.join(tmp, "nostats", "lean.db")
    from brain import store
    old_tz = os.environ.get("TZ")
    if hasattr(time, "tzset"):
        os.environ["TZ"] = "Asia/Manila"
        time.tzset()
    con = store.connect()
    # "now" is 2026-10-31 01:30 local (UTC+8), which is still 2026-10-30 in UTC
    now = datetime.datetime(2026, 10, 31, 1, 30).timestamp()
    manila = hasattr(time, "tzset")

    def at(day, hour, minute=0):
        return datetime.datetime(2026, 10, day, hour, minute).timestamp()

    def item(kind, project, first, closed=None, status=None):
        con.execute("INSERT INTO items (project, kind, ref, first_seen, last_seen, closed_at, status) VALUES (?,?,?,?,?,?,?)",
                    (project, kind, f"r{con.execute('SELECT COUNT(*) FROM items').fetchone()[0]}", first, first, closed,
                     status or ("closed" if closed else "open")))

    for started, fl, dur in ((at(31, 0, 10), 3, 120), (at(30, 23, 50), 5, 60), (at(30, 10), 1, 30), (at(1, 9), 9, 60),
                             (at(10, 12), 0, 600)):
        con.execute("INSERT INTO runs (project, started, failed, duration_s) VALUES ('p', ?, ?, ?)", (started, fl, dur))
    # 2026-10-30T17:00Z is 2026-10-31 01:00 local; 2026-10-30T15:59:59Z is 2026-10-30 23:59:59 local
    item("question", "alpha", "2026-10-30T17:00:00Z", "2026-10-30T17:30:00Z")
    item("question", "alpha", "2026-10-30T15:59:59Z")
    item("blocker", "beta", "2026-10-29T05:00:00Z")
    item("test-fail", "alpha", "2026-10-30T17:00:00Z", "2026-10-30T17:30:00Z")
    item("flaky", "alpha", "2026-10-30T17:00:00Z")
    for kind, ts, n in (("pr", "2026-10-30T17:05:00Z", 1), ("pr", "2026-10-30T10:00:00Z", 2), ("issue", "2026-10-30T17:06:00Z", 3)):
        con.execute("INSERT INTO created (kind, forge, url, number, created_at) VALUES (?, 'github', ?, ?, ?)",
                    (kind, f"https://x/{n}", n, ts))
    con.commit()
    d = collect(con, days=14, now=now)
    check("every day present", (len(d["days"]), d["days"][-1], d["days"][0]), (14, "2026-10-31", "2026-10-18"))
    if manila:
        check("runs cross local midnight", (d["runs"][-1], d["runs"][-2]), (1, 2))
        check("failed per day", (d["failed"][-1], d["failed"][-2], d["failed"][0]), (3, 6, 0))
        check("utc text crosses midnight", (d["opened"][-1], d["opened"][-2], d["opened"][-3]), (1, 1, 1))
        check("closed uses local day", (d["closed"][-1], d["closed"][-2]), (1, 0))
        check("prs and issues", (d["prs"][-1], d["prs"][-2], d["issues"][-1]), (1, 1, 1))
    check("minutes", d["minutes"][-1], 2.0)
    check("old run outside window", sum(d["runs"]), 3)
    check("noise kinds left out", (sum(d["opened"]), sum(d["closed"])), (3, 1))
    check("open by project excludes noise", d["projects"], [("alpha", 1), ("beta", 1)])
    check("no stats leaves agent out", d["agent"], None)

    html = render(d)
    check("tiles use words", ("yesterday 6" in html, "Tests failed today" in html, "&#8594;" in html or "->" in html), (True, True, False))
    check("charts present", html.count("<figure"), 5)
    check("grid and tiles", ("class=\"charts\"" in html, "class=\"tiles\"" in html), (True, True))
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "empty.db")
    econ = store.connect()
    empty = collect(econ, days=3, now=now)
    econ.close()
    check("empty window", (empty["failed"], empty["projects"]), ([0, 0, 0], []))
    eh = render(empty)
    check("empty renders", ("No data in this range." in eh, "no earlier day" in eh or "yesterday 0" in eh), (True, True))
    one = collect(con, days=1, now=now)
    check("one day has no yesterday", "no earlier day" in render(one), True)

    def no_stats():
        raise FileNotFoundError("lean-stats has no data yet")
    check("missing lean.db leaves the chart out", collect(con, now=now, stats=no_stats)["agent"], None)
    check("page still renders without it", render(collect(con, now=now, stats=no_stats)).count("<figure"), 5)

    class FakeStats:
        def __init__(self, rows):
            self.c = sqlite3.connect(":memory:")
            self.c.execute("CREATE TABLE agent_runs (started_at TEXT, ended_at TEXT, usd REAL)")
            self.c.executemany("INSERT INTO agent_runs VALUES (?,?,?)", rows)

        def __call__(self):
            c = self.c
            return None, type("C", (), {"execute": lambda s, *a: c.execute(*a), "close": lambda s: None})()
    priced = collect(con, now=now, stats=FakeStats([("", "2026-10-30T17:00:00Z", 1.5), ("", "2026-10-30T17:10:00Z", 0.25), ("", "2026-09-01T00:00:00Z", 9)]))
    check("agent spend per day", (priced["agent"] or {}).get("values", [0])[-1] if manila else 1.75, 1.75)
    unpriced = collect(con, now=now, stats=FakeStats([("2026-10-30T17:00:00Z", "", None)]))
    check("agent runs when no price", (unpriced["agent"] or {}).get("series"), "Runs")
    two = collect(con, now=now, stats=FakeStats([("2026-10-30T17:00:00Z", "", None), ("2026-10-30T17:20:00Z", "", None)]))
    check("two unpriced runs in a day", (two["agent"] or {}).get("values", [0])[-1] if manila else 2, 2)
    odd = collect(con, now=now, stats=FakeStats([("", "2026-10-30T17:00:00Z", 1.2345)]))
    check("spend rounded to cents", (odd["agent"] or {}).get("values", [0])[-1] if manila else 1.23, 1.23)
    con.execute("INSERT INTO runs (project, started, failed, duration_s) VALUES ('p', ?, 0, 50)", (at(31, 1, 0),))
    con.commit()
    check("minutes rounded to a tenth", collect(con, now=now)["minutes"][-1], 2.8)
    con.execute("DELETE FROM runs WHERE duration_s = 50")
    con.commit()
    real = collect(con)
    check("default is 14 days ending today", (len(real["days"]), real["days"][-1]), (14, datetime.date.today().isoformat()))
    check("labels are month and day", specs(d)[0]["labels"][-1], "10-31")
    check("text day inputs", (_local_day_text(None), _local_day_text(5), _local_day_text(""), _local_day_text("2026-10-30 17:00:00"),
                              _local_day_text("2026-10-30T17:00:00+00:00")),
          (None, None, None, datetime.date(2026, 10, 31) if manila else None, datetime.date(2026, 10, 31) if manila else None))
    check("agent chart rendered", render(priced).count("<figure"), 6)
    check("none in window leaves it out", collect(con, now=now, stats=FakeStats([("", "2020-01-01T00:00:00Z", 1)]))["agent"], None)
    check("bad timestamp ignored", _local_day_text("not a time"), None)

    con.close()
    if hasattr(time, "tzset"):
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"activity: {len(fails)} failed")
        return 1
    print("activity: ok")
    return 0


def main(argv) -> int:
    if "--self-test" in argv:
        return _self_test()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

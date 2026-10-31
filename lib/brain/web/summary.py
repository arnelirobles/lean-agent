"""Summaries of the brain's recent activity, written by the assistant and kept in one small file.

The file holds masked text and chart specs, never HTML. Everything is rendered again, and every chart spec
checked again, each time the page loads. A corrupt, oversized or odd file counts as no summary.

  python3 lib/brain/web/summary.py --self-test
"""
import datetime
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

import assistant  # noqa: E402
import charts  # noqa: E402
import mdlite  # noqa: E402

RANGES = ("today", "week")
LABELS = {"today": "Today", "week": "Last 7 days"}
MAX_FILE = 512 * 1024
MAX_TEXT = 40000
MAX_CHARTS = 2
INTERVAL = 600
RETRY = 3600
MAX_TRIES = 3
LOCK_WAIT = 3.0


def path():
    return os.path.join(assistant.data_dir(), "summaries.json")


def _day(now, back=0):
    return datetime.date.fromtimestamp(now) - datetime.timedelta(days=back)


def question(rng, now=None):
    """The fixed question for a range. Dates are local calendar days."""
    now = time.time() if now is None else now
    if rng == "today":
        span = f"today ({_day(now)})"
        prior = f"yesterday ({_day(now, 1)})"
    else:
        span = f"the last 7 days ({_day(now, 6)} to {_day(now)}, local time)"
        prior = f"the 7 days before ({_day(now, 13)} to {_day(now, 7)})"
    return (f"Summarise what happened in the brain over {span}. Cover what shipped, what failed, what got worse "
            f"against {prior}, and what needs me. Use short sections with a heading each, and at most "
            f"{MAX_CHARTS} charts. Use local time for every date and hour.")


def _midnight(now):
    m = time.localtime(now)
    return time.mktime((m.tm_year, m.tm_mon, m.tm_mday, 0, 0, 0, 0, 0, -1))


def decide(now, last_at, tries, active, retry=RETRY, max_tries=MAX_TRIES):
    """What the daily check does: "skip", "empty" (nothing happened, so no model call) or "run".
    tries is {"day", "n", "at"} or None; active is a callable, asked last."""
    if last_at is not None and last_at >= _midnight(now):
        return "skip"
    if tries:
        if tries["day"] == str(_day(now)) and tries["n"] >= max_tries:
            return "skip"
        if 0 <= now - tries["at"] < retry:
            return "skip"
    return "run" if active() else "empty"


def has_activity(data):
    """True when the last two days of an activity.collect result hold any run or item."""
    return any(sum(data[k][-2:]) > 0 for k in ("runs", "opened", "closed", "prs", "issues"))


def _probe():
    from brain import store
    import activity
    con = store.connect(timeout=2)
    try:
        return has_activity(activity.collect(con, days=2))
    finally:
        con.close()


class Collector:
    """Gathers what run_question emits into text and chart blocks."""

    def __init__(self):
        self.blocks = []
        self.buf = []
        self.charts = 0

    def _flush(self):
        text = "".join(self.buf)
        self.buf = []
        if text.strip():
            self.blocks.append({"t": "text", "s": text})

    def add(self, kind, text):
        if kind == "text":
            self.buf.append(text)
        elif kind in ("chart", "tool"):
            self._flush()
        if kind == "chart":
            if self.charts < MAX_CHARTS:
                try:
                    spec = charts.validate(text)
                    charts.svg(spec)
                    self.blocks.append({"t": "chart", "spec": spec})
                    self.charts += 1
                except Exception:
                    pass

    def finish(self):
        """The blocks with every text masked and the total size capped."""
        self._flush()
        out, used = [], 0
        for b in self.blocks:
            if b["t"] == "text":
                s = assistant.mask(_text(b["s"]))[:max(0, MAX_TEXT - used)]
                used += len(s)
                if s.strip():
                    out.append({"t": "text", "s": s})
            else:
                out.append(b)
        return out


def _text(s):
    """The text with lone surrogates replaced, so it always encodes."""
    return s.encode("utf-8", "replace").decode()


def _clean(entry):
    """A stored entry rebuilt from known fields, or None."""
    if not isinstance(entry, dict):
        return None
    at, usd = entry.get("at"), entry.get("usd")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or abs(at) > 1e11:
        return None
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd != usd or not 0 <= usd < 1e6:
        usd = 0.0
    model = entry.get("model")
    blocks, used = [], 0
    raw = entry.get("blocks")
    if not isinstance(raw, list) or len(raw) > 50:
        return None
    for b in raw:
        if not isinstance(b, dict):
            return None
        if b.get("t") == "text" and isinstance(b.get("s"), str):
            used += len(b["s"])
            if used > MAX_TEXT:
                return None
            blocks.append({"t": "text", "s": _text(b["s"])})
        elif b.get("t") == "chart":
            try:
                blocks.append({"t": "chart", "spec": charts.validate(b.get("spec"))})
            except ValueError:
                return None
        else:
            return None
    return {"at": float(at), "model": model if isinstance(model, str) and len(model) < 80 else "", "usd": float(usd),
            "blocks": blocks}


def _clean_tries(t):
    if not isinstance(t, dict):
        return None
    day, n, at = t.get("day"), t.get("n"), t.get("at")
    if not isinstance(day, str) or len(day) != 10 or isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 1000:
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or abs(at) > 1e11:
        return None
    return {"day": day, "n": n, "at": float(at)}


def _read(p):
    """Everything in the file: the entries by range and the auto try count under "auto"."""
    try:
        if os.path.getsize(p) > MAX_FILE:
            return {}
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for rng in RANGES:
        e = _clean(data.get(rng))
        if e:
            out[rng] = e
    t = _clean_tries(data.get("auto"))
    if t:
        out["auto"] = t
    return out


def load(p=None):
    """{range: entry} from the file; {} when it is missing, too big or not what it should be."""
    return {k: v for k, v in _read(p or path()).items() if k in RANGES}


def tries(p=None):
    return _read(p or path()).get("auto")


class _Lock:
    """A lock file beside the data, made with O_EXCL so it works on every platform. A lock older than
    10 seconds is taken as left over by a crash."""

    def __init__(self, p, wait=None):
        self.p, self.wait = p + ".lock", LOCK_WAIT if wait is None else wait

    def __enter__(self):
        os.makedirs(os.path.dirname(self.p), exist_ok=True)
        end = time.monotonic() + self.wait
        while True:
            try:
                os.close(os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.p).st_mtime > 10:
                        os.unlink(self.p)
                        continue
                except OSError:
                    continue
                if time.monotonic() >= end:
                    raise TimeoutError("the summary file is busy")
                time.sleep(0.02)

    def __exit__(self, *a):
        try:
            os.unlink(self.p)
        except OSError:
            pass
        return False


def _write(p, cur):
    folder = os.path.dirname(p)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".summaries-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(rng, blocks, model, usd, now=None, p=None, unless_today=False):
    """Store one range's summary. Atomic, mode 0600 (mkstemp), under a lock; the other range is kept.
    With unless_today, a summary that appeared since the run began is left alone. Returns True when stored."""
    p = p or path()
    now = time.time() if now is None else now
    with _Lock(p):
        cur = _read(p)
        mine = cur.get(rng)
        if unless_today and mine and mine["at"] >= _midnight(now):
            return False
        cur[rng] = {"at": now, "model": model, "usd": usd, "blocks": blocks}
        _write(p, cur)
    return True


def note_try(now, p=None):
    """Count one automatic try for the local day, so a restart does not retry at once."""
    p = p or path()
    with _Lock(p):
        cur = _read(p)
        t = cur.get("auto")
        n = t["n"] + 1 if t and t["day"] == str(_day(now)) else 1
        cur["auto"] = {"day": str(_day(now)), "n": n, "at": float(now)}
        _write(p, cur)


def render(entry, rng):
    """HTML for one stored entry."""
    e = _esc
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["at"]))
    out = [f'<div class="answer"><h3 class="lab">{e(LABELS[rng])}</h3>']
    for b in entry["blocks"]:
        if b["t"] == "text":
            md = mdlite.Stream()
            out.append(md.feed(b["s"]) + md.flush())
        else:
            out.append(charts.svg(b["spec"]))
    cost = f", about ${entry['usd']:.4f}" if entry["usd"] else ""
    out.append(f'</div><p class="muted">Written {e(when)} by {e(entry["model"] or "the assistant")}{cost} (estimate). '
               'The text comes from the brain and may hold mistakes.</p>')
    return "".join(out)


def _esc(s):
    import html
    return html.escape(str(s), quote=True)


def generate(asst, rng, now=None, p=None, limit_usd=None, unless_today=False):
    """Make and store one summary without a browser. Raises on failure. Returns the stored entry."""
    client, _src = asst.client()
    chat = {"system": assistant.build_system(), "messages": [], "model": assistant.DEFAULT_MODEL,
            "effort": assistant.DEFAULT_EFFORT}
    col = Collector()
    res = assistant.run_question(client, asst.sdk_loader(), chat, question(rng, now), chat["model"], chat["effort"],
                                 col.add, limit_usd=limit_usd)
    if res["reason"] != "done":
        raise RuntimeError("stopped: " + str(res["reason"]))
    blocks = col.finish()
    if not blocks:
        raise RuntimeError("empty")
    save(rng, blocks, chat["model"], res["usd"], now, p, unless_today)
    return load(p)[rng]


class Auto:
    """The once-a-day summary. tick() is one 10 minute check; it never raises."""

    def __init__(self, asst, clock=time.time, p=None, active=_probe):
        self.asst, self.clock, self.p, self.active = asst, clock, p, active
        self.error = None

    def tick(self):
        now = self.clock()
        st = _read(self.p or path())
        what = decide(now, (st.get("today") or {}).get("at"), st.get("auto"), self.active)
        if what != "run":
            return what
        try:
            self.asst.client()
        except Exception:
            return "no key"
        if not self.asst.slots.acquire(blocking=False):
            return "busy"
        try:
            note_try(now, self.p)
            generate(self.asst, "today", now, self.p, unless_today=True)
            self.error = None
            return "ran"
        except Exception as ex:
            self.error = f"The automatic summary failed ({type(ex).__name__}). It tries again within the hour."
            return "failed"
        finally:
            self.asst.slots.release()

    def loop(self, stop):
        while not stop.wait(INTERVAL):
            self.tick()


def start_auto(asst, env=None):
    """Start the thread when LEAN_ASSISTANT_AUTO_SUMMARY=1. Returns the Auto or None."""
    env = os.environ if env is None else env
    if env.get("LEAN_ASSISTANT_AUTO_SUMMARY") != "1":
        return None
    auto = Auto(asst)
    t = threading.Thread(target=auto.loop, args=(threading.Event(),), daemon=True, name="auto-summary")
    t.start()
    return auto


def _self_test():
    import types
    tmp = tempfile.mkdtemp()
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "brain.db")
    os.environ["TZ"] = "Asia/Manila"
    time.tzset()
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    # due(): 2026-10-31 10:00 Manila is the base
    base = datetime.datetime(2026, 10, 31, 10, 0).timestamp()
    mid = datetime.datetime(2026, 10, 31, 0, 0).timestamp()
    yes = lambda: True
    never = lambda: (_ for _ in ()).throw(AssertionError("asked"))
    t0 = {"day": "2026-10-31", "n": 1, "at": base - 7200}
    check("decide: never written", decide(base, None, None, yes), "run")
    check("decide: written today", decide(base, mid + 60, None, never), "skip")
    check("decide: written at midnight", decide(base, mid, None, never), "skip")
    check("decide: written just before midnight", decide(base, mid - 1, None, yes), "run")
    check("decide: tried 10 minutes ago", decide(base, mid - 1, {**t0, "at": base - 600}, never), "skip")
    check("decide: tried an hour ago", decide(base, mid - 1, {**t0, "at": base - 3600}, yes), "run")
    check("decide: written today beats a stale try", decide(base, mid + 5, t0, never), "skip")
    check("decide: just after midnight, yesterday's summary", decide(mid + 30, mid - 600, None, yes), "run")
    check("decide: three tries today", decide(base, None, {**t0, "n": 3}, never), "skip")
    check("decide: two tries today", decide(base, None, {**t0, "n": 2}, yes), "run")
    check("decide: yesterday's tries do not count", decide(base, None, {"day": "2026-10-30", "n": 3, "at": base - 7200}, yes), "run")
    check("decide: a try in the future waits for the clock", decide(base, None, {**t0, "at": base + 99999}, yes), "run")
    check("decide: nothing happened", decide(base, None, None, lambda: False), "empty")
    quiet = {"runs": [0, 0], "opened": [0, 0], "closed": [0, 0], "prs": [0, 0], "issues": [0, 0]}
    check("has_activity", (has_activity(quiet), has_activity({**quiet, "runs": [0, 1]}), has_activity({**quiet, "issues": [1, 0]})), (False, True, True))

    # question
    q = question("week", base)
    check("question dates", ("2026-10-25 to 2026-10-31" in q, "2026-10-18 to 2026-10-24" in q), (True, True))
    check("question topics", all(w in q for w in ("shipped", "failed", "got worse", "needs me", "2 charts", "local time")), True)
    check("question today", "today (2026-10-31)" in question("today", base) and "yesterday (2026-10-30)" in question("today", base), True)

    # file
    p = os.path.join(tmp, "summaries.json")
    check("missing file", load(p), {})
    spec = {"title": "T", "kind": "bar", "labels": ["a"], "series": [{"name": "n", "values": [1]}]}
    blocks = [{"t": "text", "s": "hi **there**"}, {"t": "chart", "spec": charts.validate(spec)}]
    save("today", blocks, "m", 0.01, base, p)
    old = os.umask(0)
    try:
        save("week", blocks, "m", 0.02, base, p)
        check("mode 0600 with an open umask", oct(os.stat(p).st_mode & 0o777), "0o600")
    finally:
        os.umask(old)
    check("mode 0600", oct(os.stat(p).st_mode & 0o777), "0o600")
    got = load(p)
    check("both kept", sorted(got), ["today", "week"])
    check("no temp files left", [f for f in os.listdir(tmp) if f.startswith(".summaries-")], [])
    html = render(got["today"], "today")
    check("render", ("<strong>there</strong>" in html, "<figure" in html, "Written 2026-10-31 10:00" in html), (True, True, True))
    evil = {"at": base, "model": "<script>", "usd": 0, "blocks": [{"t": "text", "s": "<script>x</script>"}]}
    check("entry label is a styled heading", '<h3 class="lab">Today</h3>' in html, True)
    check("evil text escaped", "<script>" in render(_clean(evil), "today"), False)
    check("evil model escaped", "&lt;script&gt;" in render(_clean(evil), "today"), True)
    for name, text in (("corrupt", "{not json"), ("array", "[]"), ("deep", "[" * 100000), ("empty", "")):
        with open(p, "w") as f:
            f.write(text)
        check("file " + name, load(p), {})
    bad = {"today": {"at": base, "usd": 0, "blocks": [{"t": "chart", "spec": {"kind": "bar", "title": "x", "labels": ["a"],
                                                                              "series": [{"name": "n", "values": [float("inf")]}]}}]}}
    with open(p, "w") as f:
        json.dump(bad, f)
    check("bad stored spec", load(p), {})
    with open(p, "w") as f:
        json.dump({"today": {"at": True, "blocks": []}, "week": {"at": base, "blocks": [{"t": "html", "s": "<b>"}]}}, f)
    check("bad shapes", load(p), {})
    with open(p, "w") as f:
        f.write(" " * (MAX_FILE + 1))
    check("oversized file", load(p), {})

    # limits, one step either side
    def ent(**kw):
        e = {"at": base, "model": "m", "usd": 1.0, "blocks": [{"t": "text", "s": "x"}]}
        e.update(kw)
        return _clean(e)
    check("text size limit", (ent(blocks=[{"t": "text", "s": "x" * MAX_TEXT}]) is not None,
                              ent(blocks=[{"t": "text", "s": "x" * (MAX_TEXT + 1)}]) is None), (True, True))
    check("text limit is a total", ent(blocks=[{"t": "text", "s": "x" * MAX_TEXT}, {"t": "text", "s": "y"}]), None)
    check("block count limit", (ent(blocks=[{"t": "text", "s": "x"}] * 50) is not None, ent(blocks=[{"t": "text", "s": "x"}] * 51)), (True, None))
    check("at limit", (ent(at=1e11) is not None, ent(at=1.0000001e11), ent(at=True), ent(at="1"), ent(at=float("nan"))), (True, None, None, None, None))
    check("usd kept or zeroed", (ent(usd=0.5)["usd"], ent(usd=-1)["usd"], ent(usd=1e6)["usd"], ent(usd=999999)["usd"], ent(usd=float("nan"))["usd"],
                                 ent(usd="9")["usd"], ent(usd=True)["usd"]), (0.5, 0.0, 0.0, 999999.0, 0.0, 0.0, 0.0))
    check("model kept or dropped", (ent(model="m" * 79)["model"], ent(model="m" * 80)["model"], ent(model=5)["model"]), ("m" * 79, "", ""))
    check("blocks must be a list", (ent(blocks="x"), ent(blocks=[5])), (None, None))
    check("no model reads as the assistant", "by the assistant" in render(ent(model=""), "today"), True)
    pad = json.dumps({"today": {"at": base, "model": "m", "usd": 0, "blocks": [{"t": "text", "s": "x"}]}})
    with open(p, "w") as f:
        f.write(pad + " " * (MAX_FILE - len(pad)))
    check("file at the size limit loads", sorted(load(p)), ["today"])
    with open(p, "w") as f:
        f.write(pad + " " * (MAX_FILE - len(pad) + 1))
    check("file over the size limit is ignored", load(p), {})
    check("default question uses the clock", datetime.date.today().isoformat() in question("today"), True)

    # lone surrogates never reach the page
    lone = "a\udc80b"
    cs = Collector()
    cs.add("text", lone)
    check("collector replaces a lone surrogate", [b["s"] for b in cs.finish()], ["a?b"])
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"today": {"at": base, "model": "m", "usd": 0, "blocks": [{"t": "text", "s": lone}]}}))
    got = load(p)
    check("load replaces a lone surrogate", got["today"]["blocks"][0]["s"], "a?b")
    check("a stored surrogate renders and encodes", render(got["today"], "today").encode("utf-8") != b"", True)

    # the lock
    lp = os.path.join(tmp, "lock", "s.json")
    os.makedirs(os.path.dirname(lp))
    old_wait = globals()["LOCK_WAIT"]
    globals()["LOCK_WAIT"] = 0.2
    try:
        with _Lock(lp):
            try:
                save("today", blocks, "m", 0, base, lp)
                got_lock = "saved"
            except TimeoutError:
                got_lock = "timeout"
        check("a held lock times out", got_lock, "timeout")
        check("the lock is released", os.path.exists(lp + ".lock"), False)
        with open(lp + ".lock", "w"):
            pass
        os.utime(lp + ".lock", (time.time() - 100, time.time() - 100))
        save("today", blocks, "m", 0, base, lp)
        check("a stale lock is cleared", sorted(load(lp)), ["today"])
    finally:
        globals()["LOCK_WAIT"] = old_wait
    errs = []

    def writer(name):
        try:
            for i in range(10):
                save(name, blocks, "m", 0, base + i, lp)
        except Exception as ex:
            errs.append(ex)
    ts = [threading.Thread(target=writer, args=(n,)) for n in ("today", "week")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("parallel saves keep both ranges", (errs, sorted(load(lp)), os.path.exists(lp + ".lock")), ([], ["today", "week"], False))
    up = os.path.join(tmp, "unless.json")
    save("today", blocks, "first", 0, base, up)
    check("unless_today leaves today's entry", (save("today", blocks, "second", 0, base + 5, up, unless_today=True), load(up)["today"]["model"]), (False, "first"))
    check("unless_today saves when none today", save("today", blocks, "third", 0, base + 86400, up, unless_today=True), True)
    check("tries survive a save", tries(up), None)
    note_try(base, up)
    note_try(base + 5, up)
    check("note_try counts the day", tries(up), {"day": "2026-10-31", "n": 2, "at": base + 5})
    save("week", blocks, "m", 0, base, up)
    check("a save keeps the tries", tries(up)["n"], 2)
    note_try(base + 86400, up)
    check("a new day starts again", tries(up)["n"], 1)
    check("load does not list the tries", sorted(load(up)), ["today", "week"])

    # the loop stops when told to, and the env switch
    class Never(Auto):
        def tick(self):
            raise AssertionError("ticked")
    gone = threading.Event()
    gone.set()
    Never(None).loop(gone)

    # collector
    c = Collector()
    key = "sk-" + "ant-" + "api03-" + "C" * 30
    c.add("text", "one " + key)
    c.add("chart", spec)
    c.add("chart", {"kind": "nope"})
    c.add("chart", spec)
    c.add("chart", spec)
    c.add("text", "two")
    c.add("tool", "select 1")
    c.add("text", "three")
    out = c.finish()
    check("collector shape", [b["t"] for b in out], ["text", "chart", "chart", "text", "text"])
    check("text stays apart across a tool call", [b["s"] for b in out if b["t"] == "text"][1:], ["two", "three"])
    c = Collector()
    real_svg = charts.svg
    charts.svg = lambda sp: (_ for _ in ()).throw(ZeroDivisionError())
    try:
        c.add("chart", spec)
    finally:
        charts.svg = real_svg
    check("a chart that cannot draw is dropped", c.finish(), [])
    check("collector masks", (key in out[0]["s"], "[secret removed]" in out[0]["s"]), (False, True))

    # generate and the auto thread, with a fake client
    turns = []
    seen = []

    class St:
        def __init__(self, kw):
            seen.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            if turns and turns[0] == "boom":
                raise KeyError("no")
            for d in ["## Shipped\n\nTwo PRs.\n\n"]:
                yield types.SimpleNamespace(type="text", text=d)

        def get_final_message(self):
            u = types.SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="x")], stop_reason="end_turn", usage=u)

    class Fake:
        def __init__(self, **kw):
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **k: St(k)))

    sdk = types.SimpleNamespace(Anthropic=Fake)
    asst = assistant.Assistant(run=lambda *a, **k: None, sdk_loader=lambda: sdk, platform="linux", env={"ANTHROPIC_API_KEY": "x"})
    from brain import store
    store.connect().close()
    clock = [base]
    ap = os.path.join(tmp, "auto", "summaries.json")
    auto = Auto(asst, lambda: clock[0], ap, yes)
    check("auto ran", (auto.tick(), "Shipped" in load(ap)["today"]["blocks"][0]["s"]), ("ran", True))
    check("fixed question sent", "what got worse" in seen[0]["messages"][0]["content"] or "got worse" in str(seen[0]["messages"][0]), True)
    check("auto skips once written", auto.tick(), "skip")
    clock[0] = base + 86400
    asst.slots.acquire()
    asst.slots.acquire()
    check("auto yields to open questions", auto.tick(), "busy")
    asst.slots.release()
    asst.slots.release()
    turns.append("boom")
    check("auto failure", auto.tick(), "failed")
    check("failure message is safe", auto.error, "The automatic summary failed (RuntimeError). It tries again within the hour.")
    clock[0] += 600
    check("no retry inside the hour", auto.tick(), "skip")
    turns.clear()
    clock[0] += 3600
    check("retry after the hour", (auto.tick(), auto.error), ("ran", None))
    n_seen = len(seen)
    clock[0] += 86400
    check("empty brain never calls the model", (Auto(asst, lambda: clock[0], ap, lambda: False).tick(), len(seen)), ("empty", n_seen))
    check("default probe sees an empty brain", Auto(asst, lambda: clock[0], ap).tick(), "empty")
    # three tries a day, kept in the file across a restart
    tp = os.path.join(tmp, "tries", "summaries.json")
    turns.append("boom")
    t1 = [base + 5 * 86400]
    first = Auto(asst, lambda: t1[0], tp, yes)
    outcome = []
    for _ in range(4):
        outcome.append(Auto(asst, lambda: t1[0], tp, yes).tick())
        t1[0] += 3600
    check("tries stop at three a day", outcome, ["failed", "failed", "failed", "skip"])
    check("tries are in the file", tries(tp)["n"], 3)
    t1[0] = base + 6 * 86400
    turns.clear()
    check("the next day tries again", Auto(asst, lambda: t1[0], tp, yes).tick(), "ran")
    # a manual summary written during the run stops the auto save
    mp = os.path.join(tmp, "manual", "summaries.json")
    real_gen_client = asst.client
    def manual_first():
        save("today", [{"t": "text", "s": "mine"}], "manual", 0, base + 7 * 86400, mp)
        return real_gen_client()
    ma = Auto(types.SimpleNamespace(client=manual_first, slots=asst.slots, sdk_loader=asst.sdk_loader), lambda: base + 7 * 86400, mp, yes)
    ma.asst.client = manual_first
    check("manual summary written meanwhile is kept", (ma.tick(), load(mp)["today"]["model"]), ("ran", "manual"))
    # a run that ends on a limit still counts
    lp2 = os.path.join(tmp, "limit", "summaries.json")
    capped = Auto(asst, lambda: base + 8 * 86400, lp2, yes)
    real_gen = globals()["generate"]
    globals()["generate"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stopped: cap"))
    try:
        r = capped.tick()
    finally:
        globals()["generate"] = real_gen
    check("a capped run counts as a try", (r, tries(lp2)["n"]), ("failed", 1))
    nokey = assistant.Assistant(run=lambda *a, **k: None, sdk_loader=lambda: None, platform="linux", env={})
    check("auto waits for a key", Auto(nokey, lambda: base, os.path.join(tmp, "n", "s.json"), yes).tick(), "no key")
    check("env off by default", start_auto(asst, {}), None)
    check("env must be 1", start_auto(asst, {"LEAN_ASSISTANT_AUTO_SUMMARY": "yes"}), None)
    check("env default reads the process environment", start_auto(asst), None)
    check("env 1 starts it", isinstance(start_auto(asst, {"LEAN_ASSISTANT_AUTO_SUMMARY": "1"}), Auto), True)

    print("summary: ok" if not fails else f"summary: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else 0)

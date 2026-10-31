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


def due(now, last_at, last_try, retry=RETRY):
    """True when the daily summary should be made: none since local midnight, and no failed try in the last hour."""
    m = time.localtime(now)
    midnight = time.mktime((m.tm_year, m.tm_mon, m.tm_mday, 0, 0, 0, 0, 0, -1))
    if last_at is not None and last_at >= midnight:
        return False
    return last_try is None or now - last_try >= retry


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
        elif kind == "chart":
            self._flush()
            if self.charts < MAX_CHARTS:
                try:
                    self.blocks.append({"t": "chart", "spec": charts.validate(text)})
                    self.charts += 1
                except ValueError:
                    pass

    def finish(self):
        """The blocks with every text masked and the total size capped."""
        self._flush()
        out, used = [], 0
        for b in self.blocks:
            if b["t"] == "text":
                s = assistant.mask(b["s"])[:max(0, MAX_TEXT - used)]
                used += len(s)
                if s.strip():
                    out.append({"t": "text", "s": s})
            else:
                out.append(b)
        return out


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
            blocks.append({"t": "text", "s": b["s"]})
        elif b.get("t") == "chart":
            try:
                blocks.append({"t": "chart", "spec": charts.validate(b.get("spec"))})
            except ValueError:
                return None
        else:
            return None
    return {"at": float(at), "model": model if isinstance(model, str) and len(model) < 80 else "", "usd": float(usd),
            "blocks": blocks}


def load(p=None):
    """{range: entry} from the file; {} when it is missing, too big or not what it should be."""
    p = p or path()
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
    return out


def save(rng, blocks, model, usd, now=None, p=None):
    """Store one range's summary. Atomic, mode 0600; the other range is kept."""
    p = p or path()
    cur = load(p)
    cur[rng] = {"at": time.time() if now is None else now, "model": model, "usd": usd, "blocks": blocks}
    folder = os.path.dirname(p)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".summaries-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def generate(asst, rng, now=None, p=None, limit_usd=None):
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
    save(rng, blocks, chat["model"], res["usd"], now, p)
    return load(p)[rng]


class Auto:
    """The once-a-day summary. tick() is one 10 minute check; it never raises."""

    def __init__(self, asst, clock=time.time, p=None):
        self.asst, self.clock, self.p = asst, clock, p
        self.last_try = None
        self.error = None

    def tick(self):
        now = self.clock()
        last = (load(self.p).get("today") or {}).get("at")
        if not due(now, last, self.last_try):
            return "skip"
        try:
            self.asst.client()
        except Exception:
            return "no key"
        if not self.asst.slots.acquire(blocking=False):
            return "busy"
        try:
            generate(self.asst, "today", now, self.p)
            self.error = None
            return "ran"
        except Exception as ex:
            self.last_try = now
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
    check("due: never written", due(base, None, None), True)
    check("due: written today", due(base, mid + 60, None), False)
    check("due: written at midnight", due(base, mid, None), False)
    check("due: written just before midnight", due(base, mid - 1, None), True)
    check("due: failed 10 minutes ago", due(base, mid - 1, base - 600), False)
    check("due: failed an hour ago", due(base, mid - 1, base - 3600), True)
    check("due: written today beats a stale try", due(base, mid + 5, base - 7200), False)
    check("due: just after midnight, yesterday's summary", due(mid + 30, mid - 600, None), True)

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
    check("mode 0600", oct(os.stat(p).st_mode & 0o777), "0o600")
    save("week", blocks, "m", 0.02, base, p)
    got = load(p)
    check("both kept", sorted(got), ["today", "week"])
    check("no temp files left", [f for f in os.listdir(tmp) if f.startswith(".summaries-")], [])
    html = render(got["today"], "today")
    check("render", ("<strong>there</strong>" in html, "<figure" in html, "Written 2026-10-31 10:00" in html), (True, True, True))
    evil = {"at": base, "model": "<script>", "usd": 0, "blocks": [{"t": "text", "s": "<script>x</script>"}]}
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
    out = c.finish()
    check("collector shape", [b["t"] for b in out], ["text", "chart", "chart", "text"])
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
    auto = Auto(asst, lambda: clock[0], ap)
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
    nokey = assistant.Assistant(run=lambda *a, **k: None, sdk_loader=lambda: None, platform="linux", env={})
    check("auto waits for a key", Auto(nokey, lambda: base, os.path.join(tmp, "n", "s.json")).tick(), "no key")
    check("env off by default", start_auto(asst, {}), None)
    check("env must be 1", start_auto(asst, {"LEAN_ASSISTANT_AUTO_SUMMARY": "yes"}), None)
    check("env default reads the process environment", start_auto(asst), None)
    check("env 1 starts it", isinstance(start_auto(asst, {"LEAN_ASSISTANT_AUTO_SUMMARY": "1"}), Auto), True)

    print("summary: ok" if not fails else f"summary: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else 0)

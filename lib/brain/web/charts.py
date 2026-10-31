"""Server side SVG charts for the brain page and the assistant's answers. Stdlib only, no JavaScript.

A spec is {title, kind: bar|line|hbar, labels: [str], series: [{name, values: [number or null]}]}. It can come
from the model, so validate() rebuilds it from known keys and every piece of text is escaped on the way out.
No text from a spec is ever placed in an attribute value.

  python3 lib/brain/web/charts.py --self-test
"""
import html
import math
import sys

KINDS = ("bar", "line", "hbar")
COLORS = ("#0b7a55", "#c2461a", "#7b4fa8")
MAX_TITLE = 120
MAX_LABELS = 60
MAX_LABEL = 24
MAX_SERIES = 3
MAX_NAME = 40
MAX_HBAR = 12
MAX_ABS = 1e15
W, H = 640, 260
GAP = 2
EMPTY = "No data in this range."


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _text(v, name, limit):
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(v) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    return v


def _number(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("values must be numbers or null")
    if not math.isfinite(v) or abs(v) > MAX_ABS:
        raise ValueError("values must be finite numbers")
    return v


def validate(spec) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("the chart must be an object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be bar, line or hbar")
    title = _text(spec.get("title"), "title", MAX_TITLE)
    labels = spec.get("labels")
    cap = MAX_HBAR if kind == "hbar" else MAX_LABELS
    if not isinstance(labels, list) or not 1 <= len(labels) <= cap:
        raise ValueError(f"labels must be a list of 1 to {cap} strings")
    labels = [_text(x, "a label", MAX_LABEL) for x in labels]
    series = spec.get("series")
    top = 1 if kind == "hbar" else MAX_SERIES
    if not isinstance(series, list) or not 1 <= len(series) <= top:
        raise ValueError(f"series must be a list of 1 to {top} items")
    out = []
    for s in series:
        if not isinstance(s, dict):
            raise ValueError("each series must be an object")
        vals = s.get("values")
        if not isinstance(vals, list) or len(vals) != len(labels):
            raise ValueError("each series needs one value per label")
        out.append({"name": _text(s.get("name"), "a series name", MAX_NAME), "values": [_number(v) for v in vals]})
    return {"title": title, "kind": kind, "labels": labels, "series": out}


def fmt(v) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.3g}G"
    if a >= 1e6:
        return f"{v / 1e6:.3g}M"
    if a >= 1e3:
        return f"{v / 1e3:.3g}k"
    return f"{v:.4g}"


def full(v) -> str:
    return "" if v is None else f"{v:.10g}"


def _ticks(lo, hi):
    """Round tick values from a lower to an upper bound that includes zero, 3 or 4 gaps."""
    lo, hi = min(lo, 0), max(hi, 0)
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / 3
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    a = math.floor(lo / step + 1e-9)
    b = math.ceil(hi / step - 1e-9)
    return [round(i * step, 10) for i in range(a, b + 1)]


def _has_data(spec) -> bool:
    return any(v not in (None, 0) for s in spec["series"] for v in s["values"])


def _every(n, span, width_each):
    return max(1, math.ceil(n * width_each / span))


def _bar_path(x, y0, y, bw):
    h = abs(y - y0)
    if h < 0.5:
        return ""
    r = min(4.0, bw / 2, h)
    if y < y0:
        return (f"M{x:.1f},{y0:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + bw - r:.1f}"
                f"Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f}V{y0:.1f}Z")
    return (f"M{x:.1f},{y0:.1f}V{y - r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + bw - r:.1f}"
            f"Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y - r:.1f}V{y0:.1f}Z")


def _hover(spec, i):
    parts = [f"{s['name']}: {full(s['values'][i]) or 'no data'}" for s in spec["series"]]
    return esc(spec["labels"][i] + " | " + ", ".join(parts))


def _vertical(spec):
    kind, labels, series = spec["kind"], spec["labels"], spec["series"]
    n, k = len(labels), len(series)
    vals = [v for s in series for v in s["values"] if v is not None]
    ticks = _ticks(min(vals), max(vals))
    lo, hi = ticks[0], ticks[-1]
    left, right, top, bottom = 48, 52 if kind == "line" else 16, 14, 30
    pw, ph = W - left - right, H - top - bottom

    def y(v):
        return top + ph * (hi - v) / (hi - lo)

    def cx(i):
        return left + pw * (i + 0.5) / n

    out = []
    for t in ticks:
        cls = "base" if t == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{left}" x2="{W - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(fmt(t))}</text>')
    step = _every(n, pw, max(len(x) for x in labels) * 6 + 10)
    for i in range(0, n, step):
        out.append(f'<text class="tick" x="{cx(i):.1f}" y="{H - 10}" text-anchor="middle">{esc(labels[i])}</text>')
    group = pw / n
    if kind == "bar":
        bw = max(1.0, min(14.0, (group * 0.8 - GAP * (k - 1)) / k))
        total = bw * k + GAP * (k - 1)
        best = None
        for j, s in enumerate(series):
            for i, v in enumerate(s["values"]):
                if v is None:
                    continue
                x = cx(i) - total / 2 + j * (bw + GAP)
                d = _bar_path(x, y(0), y(v), bw)
                if d:
                    out.append(f'<path d="{d}" fill="{COLORS[j]}"/>')
                if v != 0 and (best is None or abs(v) > abs(best[0])):
                    best = (v, x + bw / 2, y(v))
        if best:
            v, bx, by = best
            out.append(f'<text class="lbl" x="{bx:.1f}" y="{by - 5 if v > 0 else by + 13:.1f}" text-anchor="middle">{esc(fmt(v))}</text>')
    else:
        used = []
        for j, s in enumerate(series):
            seg, last = [], None
            for i, v in enumerate(s["values"]):
                if v is None:
                    seg.append(None)
                    continue
                seg.append(f"{cx(i):.1f},{y(v):.1f}")
                last = (i, v)
            d, pen = [], False
            for p in seg:
                if p is None:
                    pen = False
                    continue
                d.append(("L" if pen else "M") + p)
                pen = True
            out.append(f'<path d="{"".join(d)}" fill="none" stroke="{COLORS[j]}" stroke-width="2" '
                       'stroke-linejoin="round" stroke-linecap="round"/>')
            if n < 15:
                for i, v in enumerate(s["values"]):
                    if v is not None:
                        out.append(f'<circle cx="{cx(i):.1f}" cy="{y(v):.1f}" r="4" fill="{COLORS[j]}"/>')
            if last and all(abs(y(last[1]) - u) >= 12 for u in used):
                used.append(y(last[1]))
                out.append(f'<text class="lbl" x="{cx(last[0]) + 9:.1f}" y="{y(last[1]) + 4:.1f}">{esc(fmt(last[1]))}</text>')
    for i in range(n):
        out.append(f'<rect x="{left + group * i:.1f}" y="{top}" width="{group:.1f}" height="{ph}" fill="transparent">'
                   f"<title>{_hover(spec, i)}</title></rect>")
    return H, out


def _horizontal(spec):
    labels, vals = spec["labels"], spec["series"][0]["values"]
    n = len(labels)
    row, left, right, top, bottom = 24, 170, 40, 8, 26
    height = top + bottom + row * n
    ticks = _ticks(min([v for v in vals if v is not None] or [0]), max([v for v in vals if v is not None] or [0]))
    lo, hi = ticks[0], ticks[-1]
    pw = W - left - right

    def x(v):
        return left + pw * (v - lo) / (hi - lo)

    out = []
    for t in ticks:
        cls = "base" if t == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{height - bottom}"/>')
        out.append(f'<text class="tick" x="{x(t):.1f}" y="{height - 8}" text-anchor="middle">{esc(fmt(t))}</text>')
    best = max((abs(v), i) for i, v in enumerate(vals) if v not in (None, 0)) if _has_data(spec) else None
    for i, v in enumerate(vals):
        cy = top + row * i + row / 2
        out.append(f'<text class="tick" x="{left - 8}" y="{cy + 4:.1f}" text-anchor="end">{esc(labels[i])}</text>')
        if v:
            bt = 12
            x0, x1 = x(0), x(v)
            a, b = (x0, x1) if x1 >= x0 else (x1, x0)
            r = min(4.0, bt / 2, b - a)
            if x1 >= x0:
                d = (f"M{a:.1f},{cy - bt / 2:.1f}H{b - r:.1f}Q{b:.1f},{cy - bt / 2:.1f} {b:.1f},{cy - bt / 2 + r:.1f}"
                     f"V{cy + bt / 2 - r:.1f}Q{b:.1f},{cy + bt / 2:.1f} {b - r:.1f},{cy + bt / 2:.1f}H{a:.1f}Z")
            else:
                d = (f"M{b:.1f},{cy - bt / 2:.1f}H{a + r:.1f}Q{a:.1f},{cy - bt / 2:.1f} {a:.1f},{cy - bt / 2 + r:.1f}"
                     f"V{cy + bt / 2 - r:.1f}Q{a:.1f},{cy + bt / 2:.1f} {a + r:.1f},{cy + bt / 2:.1f}H{b:.1f}Z")
            out.append(f'<path d="{d}" fill="{COLORS[0]}"/>')
            if best and best[1] == i:
                tx = x1 + 6 if v > 0 else x1 - 6
                out.append(f'<text class="lbl" x="{tx:.1f}" y="{cy + 4:.1f}" text-anchor="{"start" if v > 0 else "end"}">{esc(fmt(v))}</text>')
        out.append(f'<rect x="0" y="{top + row * i}" width="{W}" height="{row}" fill="transparent">'
                   f"<title>{_hover(spec, i)}</title></rect>")
    return height, out


def _table(spec) -> str:
    head = "".join(f"<th>{esc(s['name'])}</th>" for s in spec["series"])
    rows = []
    for i, lab in enumerate(spec["labels"]):
        cells = "".join(f"<td>{esc(full(s['values'][i]))}</td>" for s in spec["series"])
        rows.append(f"<tr><td>{esc(lab)}</td>{cells}</tr>")
    return (f'<details><summary>Table</summary><div class="scroll"><table class="md"><tr><th>Label</th>{head}</tr>'
            + "".join(rows) + "</table></div></details>")


def svg(spec) -> str:
    spec = validate(spec)
    cap = f"<figcaption>{esc(spec['title'])}</figcaption>"
    if not _has_data(spec):
        return f'<figure class="chart">{cap}<p class="muted">{EMPTY}</p></figure>'
    height, body = _horizontal(spec) if spec["kind"] == "hbar" else _vertical(spec)
    legend = ""
    if len(spec["series"]) >= 2:
        legend = '<ul class="legend">' + "".join(
            f'<li><span class="sw s{j}"></span>{esc(s["name"])}</li>' for j, s in enumerate(spec["series"])) + "</ul>"
    pic = (f'<svg class="plot" viewBox="0 0 {W} {height}" width="100%" role="img">'
           f"<title>{esc(spec['title'])}</title>" + "".join(body) + "</svg>")
    return f'<figure class="chart">{cap}{legend}{pic}{_table(spec)}</figure>'


def _self_test() -> int:
    import time
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    def good(**kw):
        s = {"title": "T", "kind": "bar", "labels": ["a", "b", "c"], "series": [{"name": "n", "values": [1, 2, 3]}]}
        s.update(kw)
        return s

    def bad(spec):
        try:
            validate(spec)
        except ValueError:
            return True
        return False

    check("valid spec", validate(good())["labels"], ["a", "b", "c"])
    check("extra keys dropped", set(validate({**good(), "x": 1})), {"title", "kind", "labels", "series"})
    for name, spec in (
        ("not a dict", [1]), ("none", None), ("nan", good(series=[{"name": "n", "values": [1, float("nan"), 3]}])),
        ("inf", good(series=[{"name": "n", "values": [1, float("inf"), 3]}])),
        ("bool", good(series=[{"name": "n", "values": [1, True, 3]}])),
        ("string value", good(series=[{"name": "n", "values": [1, "2", 3]}])),
        ("too many labels", good(labels=["x"] * 61, series=[{"name": "n", "values": [1] * 61}])),
        ("length mismatch", good(series=[{"name": "n", "values": [1, 2]}])),
        ("long label", good(labels=["x" * 25, "b", "c"])), ("long title", good(title="t" * 121)),
        ("empty title", good(title=" ")), ("bad kind", good(kind="pie")),
        ("four series", good(series=[{"name": str(i), "values": [1, 2, 3]} for i in range(4)])),
        ("no series", good(series=[])), ("series not dict", good(series=[1])),
        ("hbar two series", good(kind="hbar", series=[{"name": str(i), "values": [1, 2, 3]} for i in range(2)])),
        ("hbar 13 labels", good(kind="hbar", labels=["x"] * 13, series=[{"name": "n", "values": [1] * 13}])),
        ("huge number", good(series=[{"name": "n", "values": [1, 1e300, 3]}])),
    ):
        check("refuses " + name, bad(spec), True)
    check("hbar 12 fits", bad(good(kind="hbar", labels=["x"] * 12, series=[{"name": "n", "values": [1] * 12}])), False)
    check("null values fine", bad(good(series=[{"name": "n", "values": [1, None, 3]}])), False)

    evil = '<script>alert("x")</script>\'&'
    for kind in ("bar", "line", "hbar"):
        two = kind != "hbar"
        series = [{"name": evil, "values": [1, 2, 3]}] + ([{"name": evil + "2", "values": [3, 2, 1]}] if two else [])
        out = svg({"title": evil, "kind": kind, "labels": ["<script>\"'", "b", "c"], "series": series})
        low = out.lower()
        check("escaped " + kind, ("<script" in low, "&lt;script&gt;" in low, "alert(\"x\")" in out, "<a " in low, "<img" in low),
              (False, True, False, False, False))
        attrs = "".join(part.split(">", 1)[0] for part in out.split("<")[1:])
        check("no model text in attributes " + kind, ("alert" in attrs or "script" in attrs), False)

    one = svg(good())
    check("one series has no legend", "legend" in one, False)
    two_s = svg(good(series=[{"name": "p", "values": [1, 2, 3]}, {"name": "q", "values": [3, 2, 1]}]))
    check("two series have a legend", ("legend" in two_s, two_s.count('class="sw s'), "#c2461a" in two_s), (True, 2, True))
    check("figure parts", ("<figure" in one, "<figcaption>T</figcaption>" in one, "viewBox" in one, 'width="100%"' in one,
                           "<details><summary>Table</summary>" in one, 'class="md"' in one, "<title>a | n: 1</title>" in one),
          (True,) * 7)
    check("first series is leaf", '<path d="M' in one and 'fill="#0b7a55"' in one, True)
    check("one top label", one.count('class="lbl"'), 1)
    line = svg(good(kind="line"))
    check("line markers 8px", (line.count('r="4"'), 'stroke-width="2"' in line), (3, True))
    big = svg(good(kind="line", labels=[str(i) for i in range(30)], series=[{"name": "n", "values": list(range(30))}]))
    check("no markers over 14 points", 'r="4"' in big, False)
    xl = [t for t in big.split("<text") if 'y="250"' in t]
    check("sparse x labels", 2 <= len(xl) < 30, True)
    gap = svg(good(kind="line", series=[{"name": "n", "values": [1, None, 3]}]))
    check("null breaks the line", gap.count("M") >= 2 and gap.count("<circle") == 2, True)
    neg = svg(good(series=[{"name": "n", "values": [-2, 1, 3]}]))
    check("negative bars draw", neg.count("<path") == 3, True)
    for name, vals in (("zeros", [0, 0, 0]), ("nulls", [None, None, None])):
        e = svg(good(series=[{"name": "n", "values": vals}]))
        check("empty state " + name, (EMPTY in e, "<svg" in e, "<figcaption>T</figcaption>" in e), (True, False, True))
    check("hbar rows", svg(good(kind="hbar")).count("<path"), 3)
    check("ticks are round", _ticks(0, 87), [0, 50, 100])
    check("ticks small", _ticks(0, 1), [0, 0.5, 1])
    check("ticks include zero", _ticks(5, 9)[0], 0)
    check("fmt", (fmt(1234567), fmt(12000), fmt(0.5), fmt(42)), ("1.23M", "12k", "0.5", "42"))
    check("fmt thresholds", (fmt(1e9), fmt(1e6), fmt(1e3), fmt(999)), ("1G", "1M", "1k", "999"))
    check("title limit", (bad(good(title="x" * MAX_TITLE)), bad(good(title="x" * (MAX_TITLE + 1)))), (False, True))
    check("label limit", (bad(good(labels=["x" * MAX_LABEL] * 3)), bad(good(labels=["x" * (MAX_LABEL + 1)] * 3))), (False, True))
    check("series name limit", (bad(good(series=[{"name": "x" * MAX_NAME, "values": [1, 2, 3]}])),
                                bad(good(series=[{"name": "x" * (MAX_NAME + 1), "values": [1, 2, 3]}]))), (False, True))
    one = good(labels=["a"], series=[{"name": "n", "values": [1]}])
    check("one label is enough", bad(one), False)
    check("no labels refused", bad(good(labels=[], series=[{"name": "n", "values": []}])), True)
    check("abs limit", (bad(good(series=[{"name": "n", "values": [MAX_ABS, 0, 0]}])),
                        bad(good(series=[{"name": "n", "values": [MAX_ABS * 2, 0, 0]}]))), (False, True))
    check("ticks steps", [_ticks(0, x) for x in (3, 6, 7.5, 15, 30, 10)],
          [[0, 1, 2, 3], [0, 2, 4, 6], [0, 2.5, 5, 7.5], [0, 5, 10, 15], [0, 10, 20, 30], [0, 5, 10]])
    check("negative ticks", (_ticks(-10, 10)[0], _ticks(-10, 10)[-1]), (-10, 10))
    check("one base line", svg(good()).count('class="base"'), 1)
    few = svg(good(kind="line", labels=[str(i) for i in range(14)], series=[{"name": "n", "values": list(range(14))}]))
    many = svg(good(kind="line", labels=[str(i) for i in range(15)], series=[{"name": "n", "values": list(range(15))}]))
    check("markers up to 14 points", (few.count('r="4"'), many.count('r="4"')), (14, 0))
    tie = svg(good(series=[{"name": "n", "values": [5, 5, 3]}]))
    check("bar label on the first of equal tops", (tie.count('class="lbl"'), tie.split('class="lbl"')[1].split("</text>")[0].endswith(">5")), (1, True))
    first_wins = svg(good(series=[{"name": "n", "values": [5, 3, 5]}]))
    lx = float(first_wins.split('class="lbl" x="')[1].split('"')[0])
    check("tied top labels the first bar", lx < W / 2, True)
    check("hbar has one base line", svg(good(kind="hbar")).count('class="base"'), 1)
    same = svg(good(kind="line", series=[{"name": "a", "values": [1, 2, 3]}, {"name": "b", "values": [2, 2, 3]}]))
    check("colliding end labels drawn once", same.count('class="lbl"'), 1)
    check("negative bar labelled below", svg(good(series=[{"name": "n", "values": [-4, -9, -1]}])).count('class="lbl"'), 1)
    hz = svg(good(kind="hbar", series=[{"name": "n", "values": [None, None, None]}]))
    check("hbar of nulls is empty", EMPTY in hz, True)
    neg = svg(good(kind="hbar", series=[{"name": "n", "values": [-3, 2, 0]}]))
    check("hbar negative and positive", (neg.count("<path"), neg.count('text-anchor="end">-3'), neg.count('class="lbl"')), (2, 1, 1))
    t0 = time.monotonic()
    svg({"title": "t", "kind": "line", "labels": [str(i) for i in range(60)],
         "series": [{"name": str(j), "values": [i * j for i in range(60)]} for j in range(3)]})
    check("big chart is fast", time.monotonic() - t0 < 1, True)

    if fails:
        print(f"charts: {len(fails)} failed")
        return 1
    print("charts: ok")
    return 0


def main(argv) -> int:
    if "--self-test" in argv:
        return _self_test()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

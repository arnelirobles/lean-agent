"""A small markdown renderer for the assistant's answers. Stdlib only.

Model text is untrusted (it can quote anything stored in the brain), so every piece is escaped, no
raw HTML passes, links and images become plain text, and no attribute takes model text except the
integer start of an ordered list.

  python3 lib/brain/web/mdlite.py --self-test
"""
import html
import re
import sys
import time

MAX_QUOTE = 8
MAX_LIST_DEPTH = 10
MAX_COLUMNS = 64
MAX_CELLS = 10000

_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\(((?:[^()\n]|\([^()\n]*\))*)\)")
_BOLD = re.compile(r"\*\*(?=\S)([^*]+?)(?<=\S)\*\*")
_EM = re.compile(r"(?<![*\w])\*(?=\S)([^*]+?)(?<=\S)\*(?![*\w])")
_MARKER = re.compile(r"([ \t]*)([-*+]|[0-9]{1,9}[.)])[ \t]+(\S.*)$")
_SEP_CELL = re.compile(r":?-+:?$")
_FENCE = re.compile(r"\s*(`{3,}|~{3,})")


def esc(text) -> str:
    return html.escape(text, quote=True)


def inline(text) -> str:
    out = []
    pos = 0
    for m in _CODE.finditer(text):
        out.append(_format(text[pos:m.start()]))
        out.append("<code>" + esc(m.group(1)) + "</code>")
        pos = m.end()
    out.append(_format(text[pos:]))
    return "".join(out)


def _format(piece) -> str:
    piece = _LINK.sub(lambda m: (m.group(1) + " " if m.group(1) else "") + "(" + m.group(2) + ")", piece)
    s = esc(piece)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    return _EM.sub(r"<em>\1</em>", s)


def fence_open(line):
    m = _FENCE.match(line)
    return (m.group(1)[0], len(m.group(1))) if m else None


def fence_closes(line, ch, n) -> bool:
    s = line.strip()
    return len(s) >= n and not s.strip(ch)


def _indent(line) -> int:
    return len(line) - len(line.lstrip(" "))


def _heading(line):
    if _indent(line) > 3:
        return None
    s = line.strip()
    hashes = len(s) - len(s.lstrip("#"))
    if not 1 <= hashes <= 6 or len(s) == hashes or s[hashes] not in " \t":
        return None
    text = s[hashes:].strip()
    k = text.rstrip("#")
    if k != text and (not k or k[-1] in " \t"):
        text = k.rstrip()
    return hashes, text


def _is_hr(line) -> bool:
    if _indent(line) > 3:
        return False
    s = line.replace(" ", "").replace("\t", "")
    return len(s) >= 3 and s[0] in "-*_" and not s.strip(s[0])


def _is_quote(line, depth) -> bool:
    return depth < MAX_QUOTE and _indent(line) <= 3 and line.lstrip(" ").startswith(">")


def _split_row(s):
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_table(lines, i) -> bool:
    if "|" not in lines[i] or i + 1 >= len(lines):
        return False
    sep = _split_row(lines[i + 1])
    if not all(_SEP_CELL.match(c) for c in sep):
        return False
    return len(sep) <= MAX_COLUMNS and len(sep) == len(_split_row(lines[i]))


def _starts_block(lines, i, depth) -> bool:
    line = lines[i]
    if fence_open(line) or _heading(line) or _is_hr(line) or _is_quote(line, depth) or _is_table(lines, i):
        return True
    m = _MARKER.match(line)
    return bool(m) and (m.group(2)[0] in "-*+" or m.group(2)[:-1] == "1")


def render(lines, depth=0) -> str:
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        f = fence_open(line)
        if f:
            i += 1
            code = []
            while i < n and not fence_closes(lines[i], *f):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            continue
        h = _heading(line)
        if h:
            tag = "h3" if h[0] <= 2 else "h4"
            out.append(f"<{tag}>{inline(h[1])}</{tag}>")
            i += 1
            continue
        if _is_hr(line):
            out.append("<hr>")
            i += 1
            continue
        if _is_table(lines, i):
            i = _table(lines, i, out, depth)
            continue
        if _is_quote(line, depth):
            inner = []
            while i < n and _is_quote(lines[i], depth):
                q = lines[i].lstrip(" ")[1:]
                inner.append(q[1:] if q.startswith(" ") else q)
                i += 1
            out.append("<blockquote>" + render(inner, depth + 1) + "</blockquote>")
            continue
        if _MARKER.match(line):
            i = _list(lines, i, out, depth)
            continue
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i, depth):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def _table(lines, i, out, depth) -> int:
    head = _split_row(lines[i])
    width = len(head)
    i += 2
    rows = []
    while i < len(lines) and lines[i].strip():
        if fence_open(lines[i]) or _heading(lines[i]) or _is_hr(lines[i]) or _is_quote(lines[i], depth):
            break
        if width * (len(rows) + 2) > MAX_CELLS:
            break
        rows.append(_split_row(lines[i]))
        i += 1
    html_rows = ["<tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>"]
    for r in rows:
        r = (r + [""] * width)[:width]
        html_rows.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    out.append('<div class="scroll"><table class="md">' + "".join(html_rows) + "</table></div>")
    return i


def _list(lines, i, out, depth) -> int:
    stack = []
    parts = []
    cur = []

    def close_item():
        if cur:
            parts.append(inline(" ".join(cur)))
            cur.clear()

    def open_list(indent, marker):
        tag = "ol" if marker[0].isdigit() else "ul"
        start = ""
        if tag == "ol":
            num = int(marker[:-1])
            if num != 1:
                start = f' start="{num}"'
        stack.append((indent, tag))
        parts.append(f"<{tag}{start}><li>")

    while i < len(lines) and lines[i].strip():
        line = lines[i]
        if _is_hr(line):
            break
        m = _MARKER.match(line)
        if not m:
            if fence_open(line) or _heading(line) or _is_quote(line, depth) or _is_table(lines, i):
                break
            cur.append(line.strip())
            i += 1
            continue
        close_item()
        indent = len(m.group(1).expandtabs(4))
        marker = m.group(2)
        tag = "ol" if marker[0].isdigit() else "ul"
        if not stack:
            open_list(indent, marker)
        else:
            while len(stack) > 1 and stack[-1][0] > indent:
                parts.append(f"</li></{stack.pop()[1]}>")
            if stack[-1][0] < indent and len(stack) < MAX_LIST_DEPTH:
                open_list(indent, marker)
            elif stack[-1][1] == tag:
                parts.append("</li><li>")
            else:
                parts.append(f"</li></{stack.pop()[1]}>")
                open_list(stack[-1][0] if stack else indent, marker)
        cur.append(m.group(3).strip())
        i += 1
    close_item()
    while stack:
        parts.append(f"</li></{stack.pop()[1]}>")
    out.append("".join(parts))
    return i


class Stream:
    """feed() takes deltas and returns HTML for the blocks that are finished; flush() renders the rest."""

    def __init__(self, clean=None):
        self.clean = clean
        self.buf = ""
        self.lines = []
        self.fence = None

    def _take(self):
        lines = [self.clean(ln) for ln in self.lines] if self.clean else self.lines
        html_out = render(lines) if lines else ""
        self.lines = []
        self.fence = None
        return html_out

    def _line(self, line):
        line = line.rstrip("\r")
        if self.fence:
            self.lines.append(line)
            return self._take() if fence_closes(line, *self.fence) else ""
        if not line.strip():
            return self._take()
        self.lines.append(line)
        self.fence = fence_open(line)
        return ""

    def feed(self, text) -> str:
        self.buf += text
        *done, self.buf = self.buf.split("\n")
        return "".join(self._line(ln) for ln in done)

    def flush(self) -> str:
        out = ""
        if self.buf:
            line, self.buf = self.buf, ""
            out = self._line(line)
        return out + self._take()


def _self_test() -> int:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    def md(text):
        s = Stream()
        return s.feed(text) + s.flush()

    check("paragraph joins lines", md("a\nb\n"), "<p>a b</p>")
    check("two paragraphs", md("a\n\nb\n"), "<p>a</p><p>b</p>")
    check("bold and em", md("**b** and *e*"), "<p><strong>b</strong> and <em>e</em></p>")
    check("snake and product are plain", md("snake_case 2*3*4"), "<p>snake_case 2*3*4</p>")
    check("em needs a word edge", md("a*b* c *d*e"), "<p>a*b* c *d*e</p>")
    check("code span not formatted", md("`**x**` & **y**"), "<p><code>**x**</code> &amp; <strong>y</strong></p>")
    check("h1 is h3", md("# T"), "<h3>T</h3>")
    check("h2 is h3", md("## T ##"), "<h3>T</h3>")
    check("h3 is h4", md("### T"), "<h4>T</h4>")
    check("h6 is h4", md("###### T"), "<h4>T</h4>")
    check("hashtag is text", md("#tag"), "<p>#tag</p>")
    check("hr", md("---"), "<hr>")
    check("hr stars", md("a\n* * *\nb"), "<p>a</p>\n<hr>\n<p>b</p>")
    check("fence", md("```py\nx < 1\n\ny\n```\nafter"), "<pre><code>x &lt; 1\n\ny</code></pre><p>after</p>")
    check("fence needs as many markers", md("````\n```\nx\n````"), "<pre><code>```\nx</code></pre>")
    check("tilde fence not closed by backticks", md("~~~\n```\nx\n~~~"), "<pre><code>```\nx</code></pre>")
    check("unclosed fence at flush", md("```\nabc"), "<pre><code>abc</code></pre>")
    check("fence ends paragraph", md("a\n```\nb\n```"), "<p>a</p>\n<pre><code>b</code></pre>")
    tbl = md("| a | b |\n|---|:-:|\n| 1 | 2 | 3 |\n| 4 |\n| x \\| y | z |")
    check("table", tbl, '<div class="scroll"><table class="md"><tr><th>a</th><th>b</th></tr>'
          "<tr><td>1</td><td>2</td></tr><tr><td>4</td><td></td></tr><tr><td>x | y</td><td>z</td></tr></table></div>")
    check("table without outer pipes", md("a | b\n--|--\n1 | 2"),
          '<div class="scroll"><table class="md"><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table></div>')
    check("pipe line without separator is text", md("a | b\nc"), "<p>a | b c</p>")
    cols = lambda n: "|" + "a|" * n
    wide = md(cols(20000) + "\n" + "|-" * 20000 + "|\n" + "\n".join(["| x |"] * 200))
    check("wide table is a paragraph", ("<table" in wide, len(wide) < 200000), (False, True))
    edge = md(cols(64) + "\n" + "|-" * 64 + "|\n| x |")
    check("64 columns still a table", (edge.count("<th>"), edge.count("<td>")), (64, 64))
    rows = "\n".join(f"| r{i} | y |" for i in range(1000))
    long = md(cols(20) + "\n" + "|-" * 20 + "|\n" + rows)
    check("tall table stops at the cell cap", (long.count("<td>") <= MAX_CELLS, "<td>r0</td>" in long, "r999" in long, "</table>" in long), (True, True, True, True))
    check("quote", md("> a\n> b"), "<blockquote><p>a b</p></blockquote>")
    check("nested quote", md("> a\n>> b"), "<blockquote><p>a</p>\n<blockquote><p>b</p></blockquote></blockquote>")
    check("quote holds a list", md("> - a\n> - b"), "<blockquote><ul><li>a</li><li>b</li></ul></blockquote>")
    check("ul", md("- a\n* b\n+ c"), "<ul><li>a</li><li>b</li><li>c</li></ul>")
    check("ol both markers", md("1. a\n2) b"), "<ol><li>a</li><li>b</li></ol>")
    check("ol start", md("3. a\n4. b"), '<ol start="3"><li>a</li><li>b</li></ol>')
    check("ol start capped to digits", md("12345678901. a"), "<p>12345678901. a</p>")
    check("list continuation", md("- a\n  more\n- b"), "<ul><li>a more</li><li>b</li></ul>")
    check("nested list", md("- a\n  1. b\n  2. c\n- d"),
          "<ul><li>a<ol><li>b</li><li>c</li></ol></li><li>d</li></ul>")
    check("indent between levels", md("- a\n    - b\n  - c\n- d"),
          "<ul><li>a<ul><li>b</li></ul><ul><li>c</li></ul></li><li>d</li></ul>")
    check("switch list kind", md("- a\n1. b"), "<ul><li>a</li></ul><ol><li>b</li></ol>")
    check("list then paragraph stops at heading", md("- a\n# H"), "<ul><li>a</li></ul>\n<h3>H</h3>")
    check("paragraph stops at list", md("intro\n- a"), "<p>intro</p>\n<ul><li>a</li></ul>")
    check("paragraph keeps 2. mid text", md("intro\n2. a"), "<p>intro 2. a</p>")
    check("dash word is text", md("-x"), "<p>-x</p>")

    for text in ("<script>alert(1)</script>", '<img src=x onerror=alert(1)>', "[x](javascript:alert(1))",
                 "![a](javascript:alert(1))", '# say "hi" <b>', "- <a href=x>y</a>", "| <i> |\n|--|\n| <u> |",
                 "> <script>", '`<script>` "q"'):
        got = md(text)
        low = got.lower()
        check("hostile " + text[:20], ("<a" in low, "<img" in low, "<script" in low, "<b>" in low, "<i>" in low, "<u>" in low),
              (False,) * 6)
    check("link is text", md("[x](javascript:alert(1))"), "<p>x (javascript:alert(1))</p>")
    check("image is text", md("![alt](http://e/x.png)"), "<p>alt (http://e/x.png)</p>")
    check("heading quote escaped", md('# a "b"'), "<h3>a &quot;b&quot;</h3>")

    doc = ("# Title\nintro **bold** and `c<d>`\n- a\n  - b\n- c\n\n```\ncode\n\nmore\n```\n| a | b |\n|---|---|\n| 1 | 2 |\n"
           "> quote\n> more\n\n3. x\n4. y\n---\nend *em*")
    whole = md(doc)
    check("sample renders blocks", all(t in whole for t in ("<h3>", "<strong>", "<ul>", "<pre>", "<table", "<blockquote>", "<ol", "<hr>", "<em>")), True)
    bad = []
    for k in range(len(doc) + 1):
        s = Stream()
        got = s.feed(doc[:k]) + s.feed(doc[k:]) + s.flush()
        if got != whole:
            bad.append(k)
    check("split at every offset", bad, [])
    s = Stream()
    got = "".join(s.feed(c) for c in doc) + s.flush()
    check("split every byte", got, whole)

    s = Stream()
    check("nothing until a block ends", s.feed("abc"), "")
    check("partial line waits", s.feed("\ndef"), "")
    check("blank line renders", s.feed("\n\n"), "<p>abc def</p>")
    s.feed("tail")
    check("flush renders the rest", s.flush(), "<p>tail</p>")
    check("flush twice is empty", s.flush(), "")
    s = Stream()
    s.feed("```\nx\n")
    check("flush mid fence", s.flush(), "<pre><code>x</code></pre>")
    seen = []
    cs = Stream(clean=lambda ln: (seen.append(ln), ln.replace("SECRET", "[x]"))[1])
    check("clean runs on finished lines", cs.feed("a SECRET\n\n") + cs.flush(), "<p>a [x]</p>")
    cs = Stream(clean=str.upper)
    check("clean covers the flushed tail", cs.feed("ab") + cs.flush(), "<p>AB</p>")
    check("no clean leaves text alone", Stream().feed("a SECRET\n\n"), "<p>a SECRET</p>")
    check("closing fence renders at once", Stream().feed("```\nx\n```\n"), "<pre><code>x</code></pre>")

    t0 = time.monotonic()
    for ch in ("*", "`", "[", "|", "#"):
        md(ch * 200000)
    md("# a" + " " * 100000 + "x")
    md("- " + "*a " * 50000)
    check("pathological input is fast", time.monotonic() - t0 < 1, True)
    try:
        deep = md("> " * 5000)
        deep2 = md("\n".join(["> "] * 5000))
        check("deep quotes", (deep.count("<blockquote>") <= MAX_QUOTE, deep2.count("<blockquote>") >= 1), (True, True))
        md("\n".join(" " * k + "- x" for k in range(0, 2000, 2)))
        check("list depth capped", md("\n".join(" " * k + "- x" for k in range(0, 100, 2))).count("<ul>") <= MAX_LIST_DEPTH, True)
    except Exception as ex:
        check("deep input raises nothing", repr(ex), "")

    if fails:
        print(f"mdlite: {len(fails)} failed")
        return 1
    print("mdlite: ok")
    return 0


def main(argv) -> int:
    if "--self-test" in argv:
        return _self_test()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

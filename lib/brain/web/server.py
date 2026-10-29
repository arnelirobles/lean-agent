"""The brain page server: serves the page on localhost and handles its three actions.

Host and Origin are checked, every POST carries the per-start key, and nothing from a request reaches
a command line except an integer item id.

  python3 lib/brain/web/server.py --self-test
"""
import hmac
import http.server
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

import forges  # noqa: E402
import page  # noqa: E402
import sections  # noqa: E402
import session  # noqa: E402

HOSTS = ("leanagent.localhost", "localhost", "127.0.0.1", "[::1]")
MAX_BODY = 16 * 1024
MAX_ANSWER = 4000
SESSION_KINDS = ("question", "blocker", "pending", "risk")
_LENGTH = re.compile(r"[0-9]{1,6}")
_ID = re.compile(r"[1-9][0-9]{0,9}")
_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"


def host_ok(host, port) -> bool:
    h = (host or "").strip().lower()
    return any(h == f"{name}:{port}" or (port == 80 and h == name) for name in HOSTS)


def origin_ok(origin, port) -> bool:
    o = (origin or "").strip().lower()
    return any(o == f"http://{name}:{port}" for name in HOSTS)


def version() -> str:
    try:
        with open(os.path.join(HERE, "..", "..", "..", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "unknown")
    except Exception:
        return "unknown"


class Flash:
    """One-line results held in memory, handed out once by a random token."""

    def __init__(self, limit=50):
        self._lock = threading.Lock()
        self._items = {}
        self._limit = limit

    def put(self, text) -> str:
        token = secrets.token_urlsafe(8)
        with self._lock:
            if len(self._items) >= self._limit:
                self._items.pop(next(iter(self._items)))
            self._items[token] = text
        return token

    def pop(self, token):
        with self._lock:
            return self._items.pop(token, None)


def _banner(text) -> str:
    return f'<p class="note" style="border:1px solid #8a4b00;padding:.4rem"><strong>{page.e(text)}</strong></p>'


class App:
    def __init__(self, port, key, roots=None):
        self.port = port
        self.key = key
        self.roots = roots or []
        self.cache = forges.Cache()
        self.flash = Flash()
        self.start_session = session.start_session
        self.recent = {}
        self.recent_lock = threading.Lock()

    def first_start(self, item_id, window=10):
        """False when a session on this item was started less than `window` seconds ago."""
        t = time.monotonic()
        with self.recent_lock:
            self.recent = {k: v for k, v in self.recent.items() if t - v < window}
            if item_id in self.recent:
                return False
            self.recent[item_id] = t
            return True


def _connect():
    from brain import store
    return store.connect(timeout=2)


def _result_session(app, con, item_id):
    row = con.execute("SELECT * FROM items WHERE id=? AND status='open' AND kind IN (?,?,?,?)",
                      (item_id, *SESSION_KINDS)).fetchone()
    if not row:
        return f"#{item_id} is not an open item that a session can start on"
    if not app.first_start(item_id):
        return f"Already opening a session on #{item_id}"
    out = app.start_session(dict(row))
    note = out.get("note") or ""
    if out.get("opened"):
        return f"Opened a session on #{item_id}" + (f" ({note})" if note else "")
    return (f"Did not open a terminal for #{item_id}" + (f" ({note})" if note else "")
            + (f". Run: {out['command']}" if out.get("command") else ""))


def _result_answer(con, item_id, form):
    from brain import items
    row = con.execute("SELECT * FROM items WHERE id=? AND status='open' AND kind='question'", (item_id,)).fetchone()
    if not row:
        return f"#{item_id} is not an open question"
    if form.get("approve") == "1":
        text = sections.recommendation(row["body"])
        if not text:
            return f"#{item_id} has no recommendation to approve"
    else:
        text = (form.get("answer") or "").strip()
        if not text:
            return "The answer was empty"
        if len(text) > MAX_ANSWER:
            return f"The answer is over {MAX_ANSWER} characters"
    if not items.close_item(con, item_id, "answered", text):
        return f"#{item_id} was already closed"
    return f"Answered #{item_id}"


def _result_dismiss(con, item_id):
    from brain import records
    return f"Dismissed notice {item_id}" if records.dismiss_notice(con, item_id) else f"Notice {item_id} not found"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "lean-brain"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 10

    def log_message(self, *a):
        pass

    @property
    def app(self):
        return self.server.app

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _other(self):
        if self._host_gate():
            self._send(405, "method not allowed\n", extra={"Allow": "GET, POST"})

    do_HEAD = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _other

    def _host_gate(self) -> bool:
        if host_ok(self.headers.get("Host"), self.app.port):
            return True
        self._send(421, "misdirected request\n")
        return False

    def do_GET(self):
        if not self._host_gate():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self._send(200, f"lean-brain {version()}\n")
        if url.path != "/":
            return self._send(404, "not found\n")
        flash = self.app.flash.pop(urllib.parse.parse_qs(url.query).get("r", [""])[0])
        try:
            con = _connect()
            try:
                data = sections.collect(con, self.app.cache, self.app.roots)
            finally:
                con.close()
        except Exception as ex:
            data = {"errors": {"needs_you": f"could not read: {type(ex).__name__}"}}
        try:
            html = page.render(data, self.app.key, self.app.port)
        except Exception as ex:
            return self._send(500, f"could not render the page: {page.e(type(ex).__name__)}\n")
        if flash:
            html = html.replace("<h1>lean brain</h1>", "<h1>lean brain</h1>" + _banner(flash), 1)
        self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_gate():
            return
        if not origin_ok(self.headers.get("Origin"), self.app.port):
            return self._send(403, "forbidden\n")
        route = urllib.parse.urlsplit(self.path).path
        if route not in ("/session", "/answer", "/dismiss"):
            return self._send(404, "not found\n")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._send(415, "unsupported media type\n")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return self._send(411, "length required\n")
        if not _LENGTH.fullmatch(raw_length.strip()):
            return self._send(400, "bad request\n")
        length = int(raw_length)
        if length > MAX_BODY:
            return self._send(413, "too large\n")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        if not hmac.compare_digest(str(form.get("key", "")).encode("utf-8"), self.app.key.encode("utf-8")):
            return self._send(403, "wrong or missing key, reload the page\n")
        item_id = str(form.get("id", ""))
        if not _ID.fullmatch(item_id):
            return self._send(400, "bad id\n")
        item_id = int(item_id)
        try:
            con = _connect()
            try:
                if route == "/session":
                    text = _result_session(self.app, con, item_id)
                elif route == "/answer":
                    text = _result_answer(con, item_id, form)
                else:
                    text = _result_dismiss(con, item_id)
                con.commit()
            finally:
                con.close()
        except Exception as ex:
            text = f"Failed: {type(ex).__name__}"
        token = self.app.flash.put(text)
        self._send(303, b"", extra={"Location": "/?r=" + token})


class Server(http.server.ThreadingHTTPServer):
    # On Windows address reuse lets a second server bind a port that is in use, so ask for exclusive use there.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port, key, db_path=None, roots=None) -> http.server.ThreadingHTTPServer:
    if db_path:
        os.environ["LEAN_BRAIN_DB"] = db_path
    srv = Server(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.app = App(srv.server_address[1], key, roots)
    return srv


def main(argv) -> int:
    if "--self-test" in argv:
        return _self_test()
    port = int(os.environ.get("LEAN_SERVE_PORT") or 8770)
    key = secrets.token_urlsafe(32)
    srv = make_server(port, key)
    print(f"http://leanagent.localhost:{srv.app.port}  (also http://127.0.0.1:{srv.app.port})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _self_test() -> int:
    import http.client
    import tempfile
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "brain.db")
    os.environ["LEAN_BRAIN_DB"] = db
    os.environ["LEAN_DB"] = os.path.join(tmp, "nostats", "lean.db")
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    from brain import store
    con = store.connect(timeout=2)
    ts = "2026-10-24T00:00:00Z"
    ins = ("INSERT INTO items (project, kind, ref, title, body, status, resolution, first_seen, last_seen, session_id)"
           " VALUES (?,?,?,?,?,?,?,?,?,?)")
    con.execute("INSERT INTO sessions (id, cwd, started_at) VALUES ('sess-1', ?, ?)", (tmp, ts))
    con.execute(ins, ("p", "question", "q1", "Which?", "Pick.\nRecommend: use A", "open", "", ts, ts, "sess-1"))
    con.execute(ins, ("p", "question", "q2", "No rec", "plain", "open", "", ts, ts, "sess-1"))
    con.execute(ins, ("p", "question", "q3", "Third", "x\nRecommend: use C", "open", "", ts, ts, "sess-1"))
    con.execute(ins, ("p", "blocker", "b1", "Stuck", "", "open", "", ts, ts, ""))
    con.execute(ins, ("p", "ruling", "r1", "Ruling", "", "open", "", ts, ts, ""))
    con.execute("INSERT INTO notices (hook, text, created_at) VALUES ('h', 'n', ?)", (ts,))
    ids = {r["ref"]: r["id"] for r in con.execute("SELECT id, ref FROM items")}
    con.commit()
    con.close()

    key = "K" * 43
    srv = make_server(0, key, db)
    port = srv.app.port
    launched = []
    srv.app.start_session = lambda item: launched.append(item["id"]) or {"opened": False, "command": "claude 'x'", "note": ""}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    check("bound to loopback", srv.server_address[0], "127.0.0.1")
    check("host_ok", [host_ok(h, 8770) for h in ("leanagent.localhost:8770", "LOCALHOST:8770", "127.0.0.1:8770", "[::1]:8770", "leanagent.local:8770",
                                                  "localhost", "evil.example:8770", "127.0.0.1:1", "")],
          [True, True, True, True, False, False, False, False, False])
    check("origin_ok", [origin_ok(o, 8770) for o in ("http://localhost:8770", "https://localhost:8770", "http://evil.example:8770", "", None)],
          [True, False, False, False, False])

    good_host = f"127.0.0.1:{port}"

    def req(method, path, body=None, headers=None, host=good_host, origin=True):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": host}
        if method == "POST":
            h["Content-Type"] = "application/x-www-form-urlencoded"
            if origin:
                h["Origin"] = f"http://127.0.0.1:{port}" if origin is True else origin
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        raw = body.encode() if isinstance(body, str) else body
        if raw is not None and "Content-Length" not in h:
            h["Content-Length"] = str(len(raw))
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(raw)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), data)
        c.close()
        return out

    def post(path, form, **kw):
        return req("POST", path, urllib.parse.urlencode(form), **kw)

    def state(ref):
        c = store.connect(timeout=2)
        r = c.execute("SELECT status, resolution, body FROM items WHERE ref=?", (ref,)).fetchone()
        c.close()
        return dict(r)

    s, h, b = req("GET", "/", host="evil.example")
    check("evil host 421", s, 421)
    check("rebinding host with port 421", req("GET", "/healthz", host=f"evil.example:{port}")[0], 421)
    s, h, b = req("GET", "/healthz")
    check("healthz", (s, b.startswith("lean-brain ")), (200, True))
    s, h, b = req("GET", "/")
    check("page 200", s, 200)
    check("page has key", f'value="{key}"' in b, True)
    check("csp", h.get("content-security-policy"), _CSP)
    check("xfo", h.get("x-frame-options"), "DENY")
    # no-referrer makes browsers send "Origin: null" on form POSTs, which origin_ok rejects
    check("referrer", h.get("referrer-policy"), "same-origin")
    check("null origin 403", post("/session", {"id": ids["q1"], "key": key}, origin="null")[0], 403)
    check("no-store", h.get("cache-control"), "no-store")
    check("no cors on get", "access-control-allow-origin" in h, False)

    good = {"id": ids["q1"], "key": key}
    s, h, b = post("/session", {**good, "key": "wrong"})
    check("wrong key 403", s, 403)
    check("no key 403", post("/session", {"id": ids["q1"]})[0], 403)
    check("no cors on 403", "access-control-allow-origin" in h, False)
    check("evil origin 403", post("/session", good, origin="https://evil.example")[0], 403)
    check("origin with other port 403", post("/session", good, origin="http://127.0.0.1:1")[0], 403)
    check("no origin 403", post("/session", good, origin=False)[0], 403)
    check("evil host post 421", post("/session", good, host="evil.example")[0], 421)
    check("bad id 400", post("/session", {"id": "1;rm", "key": key})[0], 400)
    check("zero id 400", post("/session", {"id": "0", "key": key})[0], 400)
    check("long id 400", post("/session", {"id": "1" * 11, "key": key})[0], 400)
    check("negative id 400", post("/answer", {"id": "-1", "key": key, "answer": "x"})[0], 400)
    check("too large 413", req("POST", "/answer", "a" * 10, headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
    check("json 415", req("POST", "/answer", "{}", headers={"Content-Type": "application/json"})[0], 415)
    check("bad length 400", req("POST", "/answer", "abcde", headers={"Content-Length": "+5"})[0], 400)
    check("missing length 411", req("POST", "/answer", None)[0], 411)
    check("OPTIONS evil host 421", req("OPTIONS", "/", host="evil.example")[0], 421)
    check("PUT evil host 421", req("PUT", "/", host="evil.example")[0], 421)
    s, h, b = req("OPTIONS", "/")
    check("OPTIONS 405 with csp", (s, h.get("content-security-policy")), (405, _CSP))
    s, h, b = req("GET", "/nope")
    check("404 with csp", (s, h.get("content-security-policy"), h.get("x-frame-options")), (404, _CSP, "DENY"))
    check("leanagent.local host 421", req("GET", "/healthz", host=f"leanagent.local:{port}")[0], 421)
    check("leanagent.local origin 403", post("/session", good, origin=f"http://leanagent.local:{port}")[0], 403)
    check("nothing launched yet", launched, [])

    def followed(resp):
        s, h, b = resp
        check("303", s, 303)
        loc = h.get("location", "")
        check("redirect to /", loc.startswith("/?r="), True)
        check("no cors on redirect", "access-control-allow-origin" in h, False)
        page_ = req("GET", loc)[2]
        again = req("GET", loc)[2]
        check("flash shown once", "note" in again and page_ != again, True)
        return page_

    p = followed(post("/session", good))
    check("session launched", launched, [ids["q1"]])
    check("session result shown", "Did not open a terminal" in p, True)
    p = followed(post("/session", good))
    check("double session ignored", (launched, "Already opening" in p), ([ids["q1"]], True))
    followed(post("/session", {"id": ids["r1"], "key": key}))
    check("ruling not launched", launched, [ids["q1"]])
    p = followed(post("/session", {"id": ids["b1"], "key": key}))
    check("blocker launched", launched, [ids["q1"], ids["b1"]])

    p = followed(post("/answer", {**good, "answer": "  <b>go with B</b>  "}))
    st = state("q1")
    check("answer closes", (st["status"], st["resolution"]), ("closed", "answered"))
    check("answer stored as note", st["body"].endswith("\n\nclosed: <b>go with B</b>"), True)
    check("answer escaped on page", "<b>go with B</b>" not in p, True)
    cli = __import__("subprocess").run([sys.executable, os.path.join(HERE, "..", "..", "..", "tools", "brain"), "answers",
                                        "--session", "sess-1"], capture_output=True, text=True,
                                       env={**os.environ, "LEAN_BRAIN_DB": db})
    check("brain answers returns it", "<b>go with B</b>" in cli.stdout, True)
    p = followed(post("/answer", {**good, "answer": "again"}))
    check("second answer refused", "already closed" in p or "not an open question" in p, True)
    check("body unchanged by refused answer", state("q1")["body"].count("closed:"), 1)

    followed(post("/answer", {"id": ids["q2"], "key": key, "approve": "1"}))
    check("approve without recommendation", state("q2")["status"], "open")
    followed(post("/answer", {"id": ids["q2"], "key": key, "answer": "   "}))
    check("empty answer rejected", state("q2")["status"], "open")
    followed(post("/answer", {"id": ids["q2"], "key": key, "answer": "x" * 4001}))
    check("long answer rejected", state("q2")["status"], "open")
    followed(post("/answer", {"id": ids["q2"], "key": key, "answer": "x" * 4000}))
    check("4000 chars accepted", state("q2")["status"], "closed")
    followed(post("/answer", {"id": ids["q3"], "key": key, "approve": "1", "answer": "ignored"}))
    st = state("q3")
    check("approve uses recommendation", (st["status"], st["body"].endswith("\n\nclosed: use C")), ("closed", True))
    followed(post("/answer", {"id": ids["b1"], "key": key, "answer": "nope"}))
    check("answer only on questions", state("b1")["status"], "open")

    followed(post("/dismiss", {"id": 1, "key": key}))
    c = store.connect(timeout=2)
    check("notice dismissed", bool(c.execute("SELECT dismissed_at FROM notices WHERE id=1").fetchone()[0]), True)
    c.close()
    p = followed(post("/dismiss", {"id": 99, "key": key}))
    check("missing notice reported", "not found" in p, True)

    srv.shutdown()
    srv.server_close()
    if fails:
        print(f"server: {len(fails)} failed")
        return 1
    print("server: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

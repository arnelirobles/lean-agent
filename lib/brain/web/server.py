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

import assistant  # noqa: E402
import charts  # noqa: E402
import forges  # noqa: E402
import mdlite  # noqa: E402
import page  # noqa: E402
import sections  # noqa: E402
import session  # noqa: E402
import summary  # noqa: E402

HOSTS = ("leanagent.localhost", "localhost", "127.0.0.1", "[::1]")
MAX_BODY = 16 * 1024
MAX_ASK_BODY = 96 * 1024
MAX_ANSWER = 4000
ITEM_ROUTES = ("/session", "/answer", "/dismiss")
KEY_ROUTES = ("/assistant-key", "/assistant-key-remove")
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
        self.assistant = assistant.Assistant()
        self.start_session = session.start_session
        self.recent = {}
        self.auto = None
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
            try:
                ask = self.app.assistant.state()
            except Exception:
                ask = None
            summ = {"entries": summary.load(), "error": self.app.auto.error if self.app.auto else None}
            html = page.render(data, self.app.key, self.app.port, ask, summ)
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
        if route not in ITEM_ROUTES + KEY_ROUTES + ("/ask", "/summary"):
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
        if length > (MAX_ASK_BODY if route == "/ask" else MAX_BODY):
            return self._send(413, "too large\n")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        if not hmac.compare_digest(str(form.get("key", "")).encode("utf-8"), self.app.key.encode("utf-8")):
            return self._send(403, "wrong or missing key, reload the page\n")
        if route == "/ask":
            return self._ask(form)
        if route == "/summary":
            return self._summary(form)
        if route in KEY_ROUTES:
            return self._redirect(self._key_result(route, form))
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
        self._redirect(text)

    def _redirect(self, text):
        self._send(303, b"", extra={"Location": "/?r=" + self.app.flash.put(text)})

    def _key_result(self, route, form):
        """Fixed messages only; nothing from the request is echoed."""
        asst = self.app.assistant
        if not asst.mac:
            return "Saving a key needs macOS. Set ANTHROPIC_API_KEY instead."
        if route == "/assistant-key-remove":
            return "Removed the saved key." if asst.key_delete() else "There was no saved key to remove."
        if not assistant.KEY_RX.fullmatch(str(form.get("apikey", "")).strip()):
            return "That does not look like an Anthropic API key."
        return "Saved the key in the macOS Keychain." if asst.key_set(str(form["apikey"]).strip()) else "Could not save the key."

    def _ask(self, form):
        asst = self.app.assistant
        question = (form.get("question") or "").strip()
        if not question:
            return self._send(400, "empty question\n")
        if len(question) > assistant.MAX_QUESTION:
            return self._send(413, "question too long\n")
        token = form.get("chat") or ""
        if not token:
            model, effort = form.get("model"), form.get("effort")
            if model not in assistant.MODELS:
                return self._send(400, "bad model\n")
            if effort not in assistant.EFFORTS:
                return self._send(400, "bad effort\n")
        if not asst.slots.acquire(blocking=False):
            return self._send(429, "two questions are already running, wait for one to finish\n")
        try:
            chat = None
            if token:
                chat = asst.chats.take(token)
                if chat is None:
                    return self._send(409, "that conversation is gone or still running, ask again from the page\n")
                model, effort = chat["model"], chat["effort"]
            else:
                token = asst.chats.new_token()
            self._stream_answer(token, chat, question, model, effort)
        finally:
            asst.slots.release()

    def _summary(self, form):
        asst = self.app.assistant
        rng = form.get("range")
        if rng not in summary.RANGES:
            return self._send(400, "bad range\n")
        if not asst.slots.acquire(blocking=False):
            return self._send(429, "two questions are already running, wait for one to finish\n")
        try:
            self._stream_answer(asst.chats.new_token(), None, summary.question(rng), assistant.DEFAULT_MODEL,
                                assistant.DEFAULT_EFFORT, rng)
        finally:
            asst.slots.release()

    def _stream_answer(self, token, chat, question, model, effort, rng=None):
        """Answer as one HTML document on this connection: no Content-Length, closed at the end. The
        write timeout is per send, so the 10 second handler timeout does not cut a long answer; it is
        raised for a slow reader."""
        asst = self.app.assistant
        self.close_connection = True
        e = page.e

        def out(text):
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

        md = mdlite.Stream()
        col = summary.Collector() if rng else None

        def emit(kind, text):
            if col:
                col.add(kind, text)
            if kind == "text":
                out(md.feed(text))
                return
            out(md.flush())
            if kind == "chart":
                out(charts.svg(text) + "\n")
            elif kind == "tool":
                out(f'<details class="tool"><summary>Ran a query</summary><pre>{e(text[:500])}</pre></details>\n')
            else:
                out(f'<p class="note">{e(text)}</p>\n')

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(60)
            out(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>lean brain</title><style>{page.CSS}</style>'
                f'</head><body><h1>lean brain</h1><p><a href="/">Back to the page</a></p><h2>Question</h2>'
                f'<pre>{e(question)}</pre><!-- {" " * 1024} --><h2>Answer</h2>'
                '<div class="answer">')
            try:
                client, _source = asst.client()
                if chat is None:
                    chat = {"system": assistant.build_system(), "messages": [], "model": model, "effort": effort}
            except assistant.Setup as ex:
                out(f'<p class="note">{e(ex)}</p></div></body></html>')
                return
            except Exception as ex:
                out(f'<p class="note">Could not start the assistant ({e(type(ex).__name__)}).</p></div></body></html>')
                return
            res = assistant.run_question(client, asst.sdk_loader(), chat, question, model, effort, emit)
            if res["reason"] == "disconnected":
                return
            out(md.flush())
            if col and res["reason"] == "done":
                try:
                    blocks = col.finish()
                    if blocks:
                        summary.save(rng, blocks, model, res["usd"])
                        out('<p class="muted">Saved to the Activity section.</p>')
                except Exception as ex:
                    out(f'<p class="note">Could not save the summary ({e(type(ex).__name__)}).</p>')
            total, n = assistant.usage_total()
            out(f'</div><p class="muted">This question cost about ${res["usd"]:.4f} (estimate). '
                f'Total so far about ${total:.2f} over {n} questions.</p>'
                f'<form method="post" action="/ask">{page._key(self.app.key)}'
                f'<input type="hidden" name="chat" value="{e(token)}">'
                '<textarea name="question" placeholder="Ask a follow-up"></textarea><button>Ask</button></form>'
                '</body></html>')
        except OSError:
            pass
        finally:
            if chat is not None:
                asst.chats.put(token, chat)


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
    srv.app.auto = summary.start_auto(srv.app.assistant)
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

    # the assistant routes, with a fake SDK and a fake keychain
    import types
    fake_key = "sk-" + "ant-" + "api03-" + "B" * 30
    vault, argvs, seen, turns = {}, [], [], []

    class Done:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out

    def sec(argv, input=None, **kw):
        argvs.append(argv)
        if argv[1] == "-i":
            vault["key"] = input.split("-w ")[1].strip()
            return Done()
        if argv[1] == "find-generic-password":
            return Done(0, vault["key"]) if "key" in vault else Done(44)
        return Done(0) if vault.pop("key", None) else Done(44)

    class St:
        def __init__(self, kw):
            seen.append({**kw, "messages": list(kw["messages"])})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            self.turn = turns.pop(0) if turns else None
            for d in (self.turn or {}).get("text", ["<script>alert(1)</script> & done\n\nA **bo", "ld** word"]):
                yield types.SimpleNamespace(type="text", text=d)

        def get_final_message(self):
            u = types.SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            if self.turn and self.turn.get("chart"):
                tu = types.SimpleNamespace(type="tool_use", id="c1", name="chart", input=self.turn["chart"])
                return types.SimpleNamespace(content=[tu], stop_reason="tool_use", usage=u)
            if self.turn and self.turn.get("sql"):
                tu = types.SimpleNamespace(type="tool_use", id="t1", name="run_sql", input={"sql": self.turn["sql"]})
                return types.SimpleNamespace(content=[tu], stop_reason="tool_use", usage=u)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="x")], stop_reason="end_turn", usage=u)

    class Boom(Exception):
        pass

    class Fake:
        def __init__(self, **kw):
            if boom:
                raise Boom(fake_key)
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **k: St(k)))

    boom = False
    sdk = types.SimpleNamespace(Anthropic=Fake)
    asst = assistant.Assistant(run=sec, sdk_loader=lambda: None, platform="darwin", env={})
    srv.app.assistant = asst
    p = req("GET", "/")[2]
    check("setup command shown", "assistant-setup" in p and 'name="question"' not in p, True)
    asst.sdk_loader = lambda: sdk
    boom = True
    p = req("GET", "/")[2]
    check("key form when no credentials", ('name="apikey"' in p, 'type="password"' in p, "ant auth login" in p, 'name="question"' in p),
          (True, True, True, False))
    boom = False
    p = req("GET", "/")[2]
    check("ask form when ready", ('name="question"' in p, 'name="model"' in p, "sent to Anthropic" in p), (True, True, True))
    ask_form = {"key": key, "question": "how many items?", "model": "claude-opus-5-5", "effort": "high"}
    check("ask bad key 403", post("/ask", {**ask_form, "key": "wrong"})[0], 403)
    check("ask no origin 403", post("/ask", ask_form, origin=False)[0], 403)
    check("ask evil origin 403", post("/ask", ask_form, origin="https://evil.example")[0], 403)
    check("ask evil host 421", post("/ask", ask_form, host="evil.example")[0], 421)
    check("ask json 415", req("POST", "/ask", "{}", headers={"Content-Type": "application/json"})[0], 415)
    check("ask empty 400", post("/ask", {**ask_form, "question": "  "})[0], 400)
    check("ask too long 413", post("/ask", {**ask_form, "question": "x" * 8001})[0], 413)
    check("ask 8000 fits", post("/ask", {**ask_form, "question": "x" * 8000})[0], 200)
    check("ask bad model 400", post("/ask", {**ask_form, "model": "claude-evil"})[0], 400)
    check("ask bad effort 400", post("/ask", {**ask_form, "effort": "ludicrous"})[0], 400)
    check("ask unknown chat 409", post("/ask", {"key": key, "question": "q", "chat": "nope"})[0], 409)
    seen.clear()
    s, h, b = post("/ask", {**ask_form, "question": "<b>hi</b>"})
    check("stream ok", (s, "content-length" in h, h.get("content-security-policy")), (200, False, _CSP))
    check("model text escaped", ("<script>alert(1)</script>" in b, "&lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in b), (False, True))
    check("bold split across deltas", "<strong>bold</strong>" in b and "**" not in b.split("<h2>Answer</h2>")[1], True)
    check("answer div, no pre-wrap", ('<div class="answer">' in b, "white-space:pre-wrap" in b.split("<h2>Answer</h2>")[1]), (True, False))
    check("question escaped", ("<b>hi</b>" in b, "&lt;b&gt;hi&lt;/b&gt;" in b), (False, True))
    check("stream has cost and follow-up", ("estimate" in b, 'name="chat"' in b, b.rstrip().endswith("</html>")), (True, True, True))
    tok = b.split('name="chat" value="')[1].split('"')[0]
    check("request used the picked model", (seen[0]["model"], seen[0]["output_config"]), ("claude-opus-5-5", {"effort": "high"}))
    seen.clear()
    post("/ask", {"key": key, "question": "and then?", "chat": tok})
    check("follow-up carries history", len(seen[0]["messages"]), 3)
    seen.clear()
    turns.extend([{"text": ["looking **now"], "sql": "SELECT '<b>x</b>' AS q WHERE 1 < 2"}, {"text": ["done"]}])
    s, h, b = post("/ask", ask_form)
    a = b.split("<h2>Answer</h2>")[1]
    check("tool call shows a details block", ("<details class=\"tool\"><summary>Ran a query</summary><pre>SELECT &#x27;&lt;b&gt;x&lt;/b&gt;&#x27; AS q WHERE 1 &lt; 2</pre></details>" in a,
                                              "<b>x</b>" in a), (True, False))
    check("text before a tool is flushed first", a.index("looking") < a.index("<details"), True)
    seen.clear()
    turns.extend([{"text": [], "chart": {"title": "<i>T</i>", "kind": "bar", "labels": ["a", "b"], "series": [{"name": "n", "values": [1, 2]}]}},
                  {"text": ["after"]}])
    s, h, b = post("/ask", ask_form)
    a = b.split("<h2>Answer</h2>")[1]
    check("chart drawn in the answer", (a.count("<figure"), "<svg" in a, "<i>T</i>" in a, "&lt;i&gt;T&lt;/i&gt;" in a), (1, True, False, True))
    check("chart result sent back", seen[1]["messages"][-1]["content"][0]["content"], "drawn")
    for _ in range(assistant.MAX_RUNNING):
        asst.slots.acquire()
    status = post("/ask", {"key": key, "question": "hi", "model": "claude-opus-5-5", "effort": "high"})[0]
    for _ in range(assistant.MAX_RUNNING):
        asst.slots.release()
    check("third question refused", status, 429)
    p = req("GET", "/")[2]
    check("summary buttons when ready", ('name="range" value="today"' in p, 'name="range" value="week"' in p, "Summarise today" in p), (True, True, True))
    sform = {"key": key, "range": "today"}
    check("summary bad key 403", post("/summary", {**sform, "key": "wrong"})[0], 403)
    check("summary no key 403", post("/summary", {"range": "today"})[0], 403)
    check("summary no origin 403", post("/summary", sform, origin=False)[0], 403)
    check("summary evil origin 403", post("/summary", sform, origin="https://evil.example")[0], 403)
    check("summary evil host 421", post("/summary", sform, host="evil.example")[0], 421)
    check("summary json 415", req("POST", "/summary", "{}", headers={"Content-Type": "application/json"})[0], 415)
    check("summary bad range 400", post("/summary", {**sform, "range": "year"})[0], 400)
    check("summary missing range 400", post("/summary", {"key": key})[0], 400)
    check("summary too large 413", post("/summary", {**sform, "pad": "x" * 20000})[0], 413)
    for _ in range(assistant.MAX_RUNNING):
        asst.slots.acquire()
    full = post("/summary", sform)[0]
    for _ in range(assistant.MAX_RUNNING):
        asst.slots.release()
    check("summary 429 when slots are full", full, 429)
    check("nothing stored by refusals", os.path.exists(summary.path()), False)
    seen.clear()
    turns.extend([{"text": ["## Shipped\n\nOne <b>PR</b>.\n"], "chart": {"title": "T", "kind": "bar", "labels": ["a"], "series": [{"name": "n", "values": [3]}]}},
                  {"text": ["## Needs you\n\nNothing."]}])
    s, h, b = post("/summary", sform)
    check("summary streams", (s, "content-length" in h, h.get("content-security-policy")), (200, False, _CSP))
    check("summary question sent", ("got worse" in str(seen[0]["messages"][0]), "today (" in str(seen[0]["messages"][0])), (True, True))
    check("summary has follow-up", ('name="chat"' in b, "Saved to the Activity section" in b), (True, True))
    check("summary file mode", oct(os.stat(summary.path()).st_mode & 0o777), "0o600")
    stored = open(summary.path()).read()
    check("summary file holds no html", ("<figure" in stored, "<svg" in stored, "<strong>" in stored), (False, False, False))
    p = req("GET", "/")[2]
    check("summary shown on the page", ("Written " in p, p.count("<figure") >= 1, "One &lt;b&gt;PR&lt;/b&gt;." in p, "<b>PR</b>" in p), (True, True, True, False))
    check("summary sits in Activity", p.index("<h2>Activity</h2>") < p.index("Written ") < p.index("<h2>Test queue</h2>"), True)
    tok = b.split('name="chat" value="')[1].split('"')[0]
    seen.clear()
    post("/ask", {"key": key, "question": "more?", "chat": tok})
    check("summary follow-up carries history", len(seen[0]["messages"]) >= 3, True)
    open(summary.path(), "w").write("{broken")
    s, h, p = req("GET", "/")
    check("corrupt summary file still serves the page", (s, "Written " in p, "<h2>Activity</h2>" in p), (200, False, True))
    os.unlink(summary.path())
    turns.append({"text": ["x"], "sql": "SELECT 1"})
    seen.clear()
    post("/summary", sform)
    check("summary sets effort and model from defaults", (seen[0]["model"], seen[0]["output_config"]), (assistant.DEFAULT_MODEL, {"effort": assistant.DEFAULT_EFFORT}))
    check("chat token reusable once taken and put back", asst.chats.count() >= 1, True)
    boom = True
    s, h, b = post("/ask", ask_form)
    check("no credentials message", ("No Anthropic credentials" in b, fake_key in b), (True, False))
    boom = False

    check("key save 303", post("/assistant-key", {"key": key, "apikey": fake_key})[0], 303)
    check("key saved", vault.get("key"), fake_key)
    check("key not in argv", any(fake_key in " ".join(a) for a in argvs), False)
    s, h, b = post("/assistant-key", {"key": key, "apikey": fake_key})
    flash_page = req("GET", h["location"])[2]
    check("key not in flash page", fake_key in flash_page, False)
    check("key not in redirect", fake_key in h["location"], False)
    check("key not in main page", fake_key in req("GET", "/")[2], False)
    check("saved key source shown", "Keychain" in req("GET", "/")[2], True)
    s, h, b = post("/assistant-key", {"key": key, "apikey": "sk-ant-short<script>"})
    page_ = req("GET", h["location"])[2]
    check("bad key refused, input not echoed", ("does not look like" in page_, "short" in page_), (True, False))
    check("key form needs the page key", post("/assistant-key", {"key": "wrong", "apikey": fake_key})[0], 403)
    s, h, b = post("/assistant-key-remove", {"key": key})
    check("key removed", (s, "key" in vault), (303, False))
    asst.platform = "linux"
    s, h, b = post("/assistant-key", {"key": key, "apikey": fake_key})
    page_ = req("GET", h["location"])[2]
    check("no key form off mac", ("needs macOS" in page_, "key" in vault, 'name="apikey"' in req("GET", "/")[2]), (True, False, False))
    check("item routes still need an id", post("/answer", {"key": key})[0], 400)

    srv.shutdown()
    srv.server_close()
    if fails:
        print(f"server: {len(fails)} failed")
        return 1
    print("server: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Shared store for lean-stats: the database, the price table and the transcript reader.

Hooks write here when they fire, so nothing in this file may raise into a hook
or wait long on a lock. Standard library only, Python 3.9+.

  python3 lib/leanstats.py --self-test
"""
import contextlib
import json
import os
import sqlite3
import sys
import time

# input, 5 minute cache write, cache read, output; USD per million tokens, list prices on 2026-10-08.
# The first name found in the model id wins, so a version sits above its family.
PRICES = {
    "fable-5-1": (10.0, 12.50, 0.25, 50.0),
    "fable": (10.0, 12.50, 1.00, 50.0),
    "opus-5-5": (4.0, 5.0, 0.20, 20.0),
    "opus-4-1": (15.0, 18.75, 1.50, 75.0),
    "opus-4-2025": (15.0, 18.75, 1.50, 75.0),  # Opus 4; Opus 4.5 and later fall through to "opus"
    "opus": (5.0, 6.25, 0.50, 25.0),
    "sonnet-4": (3.0, 3.75, 0.30, 15.0),
    "sonnet": (2.0, 2.50, 0.20, 10.0),
    "haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "haiku": (1.0, 1.25, 0.10, 5.0),  # Haiku 4.5 and earlier
}
# A model that charges a second rate when one request's prompt (in + cw + cr) is over the limit:
# name in PRICES, then (limit in tokens, the prices above it).
LONG_PROMPT = {"haiku-5-5": (100000, (0.50, 0.625, 0.05, 2.50))}
FAMILIES = ("opus", "fable", "sonnet", "haiku")
HOUR_WRITE = 2.0  # a 1 hour cache write costs this many times the input price

MIGRATIONS = [  # one entry per schema version, applied in order; never edit an entry that shipped
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE agent_runs (
        run_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        role TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        repo TEXT NOT NULL DEFAULT '',
        pr INTEGER,
        branch TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT '',
        ended_at TEXT NOT NULL DEFAULT '',
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_cache_write INTEGER NOT NULL DEFAULT 0,
        tokens_cache_write_1h INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        source_path TEXT NOT NULL
    );
    CREATE INDEX agent_runs_source ON agent_runs (source_path);
    CREATE TABLE reviews (
        date TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr INTEGER NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        refuted INTEGER NOT NULL DEFAULT 0,
        blockers INTEGER NOT NULL DEFAULT 0,
        categories TEXT NOT NULL DEFAULT '',
        caught_elsewhere_first TEXT NOT NULL DEFAULT '',
        pre_push INTEGER NOT NULL DEFAULT 0,
        ci INTEGER NOT NULL DEFAULT 0,
        review INTEGER NOT NULL DEFAULT 0,
        escaped INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (repo, pr)
    );
    CREATE TABLE changes (
        repo TEXT NOT NULL,
        pr INTEGER NOT NULL,
        branch TEXT NOT NULL DEFAULT '',
        opened_at TEXT NOT NULL DEFAULT '',
        merged_at TEXT NOT NULL DEFAULT '',
        fix_rounds INTEGER,
        failed_checks INTEGER,
        escaped_refs INTEGER,
        PRIMARY KEY (repo, pr)
    );
    CREATE TABLE hook_fires (
        ts TEXT NOT NULL,
        hook TEXT NOT NULL,
        outcome TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        cwd TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE imports (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL)
    """,
    "ALTER TABLE agent_runs ADD COLUMN usd REAL",  # the run's list price summed per message; NULL on older rows
    """
    ALTER TABLE agent_runs ADD COLUMN peak_context INTEGER;
    ALTER TABLE agent_runs ADD COLUMN turns_over_80k INTEGER;
    ALTER TABLE agent_runs ADD COLUMN lookup_calls INTEGER;
    ALTER TABLE agent_runs ADD COLUMN image_results INTEGER;
    ALTER TABLE agent_runs ADD COLUMN errored_results INTEGER;
    ALTER TABLE agent_runs ADD COLUMN wait_loop_calls INTEGER;
    ALTER TABLE agent_runs ADD COLUMN avoidable TEXT
    """,  # usage signals, counts only; NULL on a row collected before this version means unknown, not zero
]
CONTEXT_LIMIT = 80000
LOOKUP_TOOLS = {"Grep", "Glob", "Read"}
LOOKUP_WORDS = {"grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "ls"}
LOOKUP_GIT = {"status", "log", "diff", "show", "fetch"}
LOOKUP_GH = {"view", "list", "checks"}
LOOKUP_GH_AREAS = {"pr", "issue", "run"}
FIND_WRITES = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
MAX_LOOKUP_COMMAND = 2000
MAX_WAIT_COMMAND = 5000
SIGNALS = ("context", "lookup", "image", "error", "wait")


def family(model):
    for name in FAMILIES:
        if name in model:
            return name
    return "opus"


def cost(model, u):
    """List price of a usage dict. cw is every cache write, cw1h the part of it written for 1 hour."""
    name = next((name for name in PRICES if name in model), "opus")
    return price_usage(PRICES[name], u)


def price_usage(price, u):
    pi, pw, pr, po = price
    hour = u.get("cw1h", 0)
    return (u["in"] * pi + (u["cw"] - hour) * pw + hour * pi * HOUR_WRITE + u["cr"] * pr + u["out"] * po) / 1e6


def message_cost(m):
    """List price of one message. A model with a long-prompt rate is priced at it per request, not per total."""
    name = next((name for name in PRICES if name in m["model"]), "opus")
    limit, long_price = LONG_PROMPT.get(name, (None, None))
    if limit is not None and m["in"] + m["cw"] + m["cr"] > limit:
        return price_usage(long_price, m)
    return price_usage(PRICES[name], m)


class NewerSchema(Exception):
    """The database was written by a newer lean-stats than this one."""


def off():
    return os.environ.get("LEAN_STATS") == "off"


def db_path():
    return os.path.expanduser(os.environ.get("LEAN_DB") or "~/.lean/lean.db")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def temp_env(**values):
    """Set environment variables for a block (None unsets one) and restore them after. For tests."""
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def tx(conn):
    """One write transaction. The connection is in autocommit mode outside it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn):
    try:
        found = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    return int(found[0]) if found else 0


def migrate(conn, path=""):
    have = schema_version(conn)
    if have > len(MIGRATIONS):
        raise NewerSchema(f"{path or 'the database'} has schema version {have} and this lean-stats "
                          f"knows up to {len(MIGRATIONS)}; update the lean-agent plugin")
    if have == len(MIGRATIONS):
        return
    with tx(conn):
        have = schema_version(conn)  # another process may have migrated while this one waited
        for number, script in enumerate(MIGRATIONS[have:], have + 1):
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(number),))


def set_wal(conn, timeout):
    """Switch to WAL. SQLite does not wait on a lock for this, so wait here, up to the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def connect(path=None, timeout=5.0):
    """Open the database, creating and migrating it as needed. Raises NewerSchema."""
    path = path or db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        set_wal(conn, timeout)
        migrate(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


_WAIT_LOOP = None


def _hookkit():
    import hookkit
    return hookkit


def _wait_loop():
    global _WAIT_LOOP
    if _WAIT_LOOP is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lean_wait_loop", os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "hooks", "wait-loop.py"))
        _WAIT_LOOP = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_WAIT_LOOP)
    return _WAIT_LOOP


def writes_to_file(words):
    for i, word in enumerate(words):
        if word and ">" in word and set(word) <= set("<>&0123456789"):
            target = words[i + 1] if i + 1 < len(words) else ""
            if not (word.endswith("&") or target == "/dev/null"):
                return True
    return False


def lookup_segment(seg):
    words = seg.words
    if not words or writes_to_file(words):
        return False
    name = os.path.basename(words[0])
    if name in LOOKUP_WORDS:
        return True
    if name == "find":
        return not FIND_WRITES.intersection(words)
    if name == "sed":
        flags = [w[1:] for w in words[1:] if w.startswith("-") and not w.startswith("--")]
        return any("n" in f for f in flags) and not any("i" in f for f in flags) and "--in-place" not in words
    if name == "git":
        call = _hookkit().git_call(seg)
        return bool(call) and call[0] in LOOKUP_GIT
    if name == "gh":
        args = (_hookkit().gh_call(seg) or ([], ""))[0]
        return len(args) > 1 and args[0] in LOOKUP_GH_AREAS and args[1] in LOOKUP_GH
    return False


def is_lookup(command):
    """True when a Bash command only reads: grep, rg, cat, head, tail, ls, find, sed -n, git status, log, diff,
    show or fetch, gh pr, issue or run view, list or checks. The command is not kept."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_LOOKUP_COMMAND:
        return False
    try:
        segs = _hookkit().segments(command)
        return bool(segs) and all(lookup_segment(s) for s in segs)
    except Exception:
        return False


def is_wait_loop(command):
    """True when wait-loop.py would deny the command: a sleep and read poll, or pgrep."""
    if not isinstance(command, str) or len(command) > MAX_WAIT_COMMAND \
            or not ("sleep" in command or "pgrep" in command):
        return False
    try:
        with temp_env(LEAN_WAIT_GUARD=None):
            found = _wait_loop().verdict(command)
    except Exception:
        return False
    return bool(found) and found[0] == "deny"


def tool_kind(name, tool_input):
    """lookup, wait or other, for one tool call."""
    if name in LOOKUP_TOOLS:
        return "lookup"
    if name == "Bash" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        if is_wait_loop(command):
            return "wait"
        if is_lookup(command):
            return "lookup"
    return "other"


def usage_of(row, message):
    usage = message["usage"]
    split = usage.get("cache_creation")
    split = split if isinstance(split, dict) else {}
    written = int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "model": message.get("model") or "",
        "cwd": row.get("cwd") or "",
        "ts": row.get("timestamp") or "",
        "session": row.get("sessionId") or "",
        "in": int(usage.get("input_tokens") or 0),
        "cw": written,
        "cw1h": min(written, int(split.get("ephemeral_1h_input_tokens") or 0)),
        "cr": int(usage.get("cache_read_input_tokens") or 0),
        "out": int(usage.get("output_tokens") or 0),
    }


def prompt_size(m):
    return m["in"] + m["cw"] + m["cr"]


def last_usage(path, chunk=262144, limit=16777216):
    """The last assistant message of a transcript, read from the end of the file, or None."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            while True:
                start = max(0, size - chunk)
                fh.seek(start)
                lines = fh.read(size - start).decode("utf-8", errors="replace").splitlines()
                if start > 0:
                    lines = lines[1:]
                for line in reversed(lines):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    message = row.get("message") if isinstance(row, dict) else None
                    if isinstance(message, dict) and isinstance(message.get("usage"), dict) \
                            and message.get("model") != "<synthetic>":
                        return usage_of(row, message)
                if start == 0 or chunk >= limit:
                    return None
                chunk *= 4
    except OSError:
        return None


def read_usage(path):
    """One dict per assistant message in a transcript: model, cwd, ts, session, five token counts and
    tool counts (calls, lookups, waits, errors, images, after_image).

    Claude Code writes one row per content block and repeats the message's usage
    on each, so usage is counted once per message id and the last row wins; the
    tool calls of those rows are collected by their id. A row with no id counts
    alone. Lines that are not JSON are skipped. Only counts are kept, never a command.
    """
    seen, order, extra = {}, [], {}
    failed, pictured = set(), set()
    try:
        fh = open(path, errors="replace")
    except OSError:
        return []
    with fh:
        for number, line in enumerate(fh):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            content = content if isinstance(content, list) else ()
            if not isinstance(usage, dict):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        note_result(block, failed, pictured)
                continue
            key = message.get("id") or f"row-{number}"
            if key not in seen:
                order.append(key)
                extra[key] = {"calls": {}, "after_image": bool(pictured)}
            seen[key] = usage_of(row, message)
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    extra[key]["calls"][block["id"]] = tool_kind(block.get("name"), block.get("input"))
    for key in order:
        calls = extra[key]["calls"]
        seen[key].update(
            calls=len(calls),
            lookups=sum(1 for kind in calls.values() if kind == "lookup"),
            waits=sum(1 for kind in calls.values() if kind == "wait"),
            errors=sum(1 for tid in calls if tid in failed),
            images=sum(1 for tid in calls if tid in pictured),
            after_image=extra[key]["after_image"])
    return [seen[key] for key in order]


def note_result(block, failed, pictured):
    tid = block.get("tool_use_id")
    if block.get("is_error") is True:
        failed.add(tid)
    inner = block.get("content")
    if isinstance(inner, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in inner):
        pictured.add(tid)


def signals(messages, kind):
    """The usage signals of one run: counts, and the tokens and list price of the turns each one touches.

    A turn counts toward a signal in whole, so the signals overlap and are an upper bound on what could
    be saved. Context is the part of a prompt above 80k. Lookups only count for a coordinator on Opus.
    Returns the six counts and a JSON text of {signal: [tokens, usd]}.
    """
    spent = {name: [0, 0.0] for name in SIGNALS}

    def add(name, m, tokens, usd):
        spent[name][0] += tokens
        spent[name][1] += usd

    peak = turns = 0
    for m in messages:
        size = prompt_size(m)
        peak = max(peak, size)
        usd = message_cost(m)
        whole = size + m["out"]
        if size > CONTEXT_LIMIT:
            turns += 1
            over = size - CONTEXT_LIMIT
            add("context", m, over, message_cost(dict(m, out=0)) * over / size)
            if m.get("after_image"):
                add("image", m, whole, usd)
        if kind == "main" and "opus" in m["model"] and m.get("calls") and m.get("lookups") == m["calls"]:
            add("lookup", m, whole, usd)
        if m.get("errors"):
            add("error", m, whole, usd)
        if m.get("waits"):
            add("wait", m, whole, usd)
    counts = (peak, turns, sum(m.get("lookups", 0) for m in messages), sum(m.get("images", 0) for m in messages),
              sum(m.get("errors", 0) for m in messages), sum(m.get("waits", 0) for m in messages))
    return counts + (json.dumps({name: [tokens, round(usd, 6)] for name, (tokens, usd) in spent.items()}),)


def record_fire(hook, outcome, session_id="", cwd=""):
    """Write one hook fire. Never raises and never waits long: a hook must not slow a tool call."""
    if off():
        return
    try:
        conn = connect(timeout=0.2)
        try:
            conn.execute("INSERT INTO hook_fires (ts, hook, outcome, session_id, cwd) VALUES (?, ?, ?, ?, ?)",
                         (now(), hook, outcome, session_id, cwd))
        finally:
            conn.close()
    except Exception:
        pass


def self_test():
    import tempfile
    import threading

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    def row(mid, usage, model="claude-sonnet-5-5", cwd="/w"):
        message = {"model": model, "usage": usage}
        if mid:
            message["id"] = mid
        return json.dumps({"message": message, "cwd": cwd, "sessionId": "s1",
                           "timestamp": "2026-10-02T01:00:00.000Z"}) + "\n"

    check("family by name", family("claude-haiku-4-5"), "haiku")
    check("unknown family is opus", family("something-new"), "opus")
    check("family of a versioned id", family("claude-fable-5-1"), "fable")
    million = 1000000

    def priced(model, **tokens):
        return cost(model, dict({"in": 0, "cw": 0, "cr": 0, "out": 0}, **tokens))

    check("cost", priced("claude-sonnet-5-5", **{"in": million, "out": million}), 12.0)
    check("opus 5.5 input", priced("claude-opus-5-5", **{"in": million}), 4.0)
    check("opus before 5.5 input", priced("claude-opus-5", **{"in": million}), 5.0)
    check("opus 4.1 input", priced("claude-opus-4-1-20250805", **{"in": million}), 15.0)
    check("opus 4 output", priced("claude-opus-4-20250514", out=million), 75.0)
    check("opus 4.5 input", priced("claude-opus-4-5-20251101", **{"in": million}), 5.0)
    check("opus 5.5 cache read", priced("claude-opus-5-5", cr=million), 0.2)
    check("fable 5.1 cache read", priced("claude-fable-5-1", cr=million), 0.25)
    check("fable 5 cache read", priced("claude-fable-5", cr=million), 1.0)
    check("sonnet 4 input", priced("claude-sonnet-4-6", **{"in": million}), 3.0)
    check("an unknown model prices as opus", priced("something-new", **{"in": million}), 5.0)
    check("a 5 minute cache write", priced("claude-opus-5-5", cw=million), 5.0)
    check("a 1 hour cache write is twice input", priced("claude-opus-5-5", cw=million, cw1h=million), 8.0)
    check("haiku 5.5 input", priced("claude-haiku-5-5", **{"in": million}), 0.10)
    check("haiku 4.5 input", priced("claude-haiku-4-5", **{"in": million}), 1.0)

    def message_usd(model, **tokens):
        return message_cost(dict({"model": model, "in": 0, "cw": 0, "cw1h": 0, "cr": 0, "out": 0}, **tokens))

    check("a short haiku 5.5 message", round(message_usd("claude-haiku-5-5", **{"in": 100000}), 6), 0.01)
    check("one token over the limit prices at the long rate",
          round(message_usd("claude-haiku-5-5", **{"in": 100001}), 6), round(100001 * 0.50 / 1e6, 6))
    check("a long haiku 5.5 message prices at the long rate",
          round(message_usd("claude-haiku-5-5", **{"in": 150000}), 6), 0.075)
    check("the prompt counts cache reads and writes", round(message_usd("claude-haiku-5-5", cr=60000, cw=50000), 6),
          round((60000 * 0.05 + 50000 * 0.625) / 1e6, 6))
    check("a long prompt does not touch other models", message_usd("claude-sonnet-5-5", **{"in": 150000}),
          cost("claude-sonnet-5-5", {"in": 150000, "cw": 0, "cr": 0, "out": 0}))
    check("haiku 5.5 1 hour write is twice the long input price",
          round(message_usd("claude-haiku-5-5", cw=150000, cw1h=150000), 6), 0.15)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "nested", "dir", "lean.db")
        with temp_env(LEAN_DB=db, LEAN_STATS=None):
            check("db path from LEAN_DB", db_path(), db)
            conn = connect()
            check("missing directories are created", os.path.exists(db), True)
            check("schema version", schema_version(conn), len(MIGRATIONS))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            check("tables", tables >= {"meta", "agent_runs", "reviews", "changes", "hook_fires", "imports"}, True)
            check("wal mode", conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            conn.close()
            conn = connect()
            check("reopen keeps the version", schema_version(conn), len(MIGRATIONS))
            with tx(conn):
                conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            conn.close()
            try:
                connect()
                check("newer schema refused", "opened", "refused")
            except NewerSchema as exc:
                check("message names both versions", "99" in str(exc) and str(len(MIGRATIONS)) in str(exc), True)

        transcript = os.path.join(tmp, "t.jsonl")
        with open(transcript, "w") as fh:
            fh.write(row("m1", {"input_tokens": 100, "output_tokens": 10}))
            fh.write(row("m1", {"input_tokens": 100, "output_tokens": 10}))
            fh.write(row("m1", {"input_tokens": 100, "output_tokens": 40}))
            fh.write("not json\n")
            fh.write(json.dumps({"message": {"model": "claude-sonnet-5-5"}}) + "\n")
            fh.write(row("", {"input_tokens": 5}))
            fh.write(row("", {"input_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
                              "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2}}))
            fh.write('{"message": {"id": "m9", "usage": {"input_tok')
        usage = read_usage(transcript)
        check("one entry per message id, id-less rows alone", len(usage), 3)
        check("repeated id counted once, last row wins", (usage[0]["in"], usage[0]["out"]), (100, 40))
        check("fields", (usage[0]["model"], usage[0]["cwd"], usage[0]["session"], usage[0]["ts"]),
              ("claude-sonnet-5-5", "/w", "s1", "2026-10-02T01:00:00.000Z"))
        check("cache tokens", (usage[2]["cr"], usage[2]["cw"], usage[2]["cw1h"]), (7, 3, 2))
        check("no 1 hour part when the row has none", usage[0]["cw1h"], 0)
        check("total input", sum(u["in"] for u in usage), 110)
        check("missing file is empty", read_usage(os.path.join(tmp, "absent.jsonl")), [])
        with open(transcript, "w") as fh:
            fh.write(row("m1", {"cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 9}}))
            fh.write(row("m2", {"cache_creation_input_tokens": 4, "cache_creation": "odd"}))
        usage = read_usage(transcript)
        check("the 1 hour part is never more than the total", (usage[0]["cw"], usage[0]["cw1h"]), (0, 0))
        check("a split that is not an object is ignored", (usage[1]["cw"], usage[1]["cw1h"]), (4, 0))

        for command in ("git status", "git log --oneline | head -5", "cd /x && git diff HEAD~1", "grep -rn foo src",
                        "rg foo", "sed -n 1,20p a.py", "cat a b", "ls -la", "find . -name '*.py'", "gh pr view 5",
                        "gh issue list --state open", "gh run view 9", "gh pr checks 5", "git show abc",
                        "git fetch origin", "cat a 2>/dev/null", "ls 2>&1"):
            check("lookup: " + command, is_lookup(command), True)
        for command in ("git commit -m x", "git push", "sed -i s/a/b/ f", "sed s/a/b/ f", "find . -delete",
                        "find . -exec rm {} ;", "cat a > b", "ls >> log", "gh pr merge 5", "gh pr comment 5",
                        "gh api x", "npm test", "git status && git add .", "cat a | sh", "", None, "x" * 3000):
            check("not a lookup: %r" % (command if command is None or len(command) < 60 else "long"),
                  is_lookup(command), False)
        check("wait: sleep then cat", is_wait_loop("sleep 5; cat out.txt"), True)
        check("wait: pgrep", is_wait_loop("pgrep -f dotnet"), True)
        check("wait: poll loop", is_wait_loop("until grep -q x f; do sleep 2; done"), True)
        check("a plain sleep is not a wait loop", is_wait_loop("sleep 2"), False)
        check("a note-only command is not a wait loop", is_wait_loop("dotnet test X"), False)
        with temp_env(LEAN_WAIT_GUARD="off"):
            check("the wait guard's off switch does not hide a wait loop", is_wait_loop("pgrep x"), True)
        check("Read, Grep and Glob are lookups", [tool_kind(n, {}) for n in ("Read", "Grep", "Glob", "Edit")],
              ["lookup", "lookup", "lookup", "other"])

        def turn(mid, blocks, size=1000):
            usage = {"input_tokens": 10, "cache_read_input_tokens": size, "output_tokens": 50}
            return "".join(json.dumps({"type": "assistant", "sessionId": "s1", "message": {
                "id": mid, "model": "claude-opus-5-5", "usage": usage, "content": [b] if b else []}}) + "\n"
                           for b in blocks or [None])

        def use(tid, name, **tool_input):
            return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}

        def result(tid, error=False, image=False):
            inner = [{"type": "text", "text": "x"}] + ([{"type": "image", "source": {}}] if image else [])
            return json.dumps({"type": "user", "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tid, "is_error": error, "content": inner}]}}) + "\n"

        with open(transcript, "w") as fh:
            fh.write(turn("a", [use("t1", "Bash", command="git status"), use("t2", "Bash", command="sleep 5; cat o.txt")],
                          size=90000))
            fh.write(result("t1") + result("t2", error=True))
            fh.write(turn("b", [use("t3", "Read", file_path="/a")], size=100000))
            fh.write(result("t3", image=True))
            fh.write(turn("c", [use("t4", "Bash", command="make > out.txt"), use("t4", "Bash", command="make > out.txt")],
                          size=120000))
            fh.write(turn("d", []))
        got = read_usage(transcript)
        check("a message whose usage repeats across blocks is one message", len(got), 4)
        check("usage is not counted twice", [m["cr"] for m in got], [90000, 100000, 120000, 1000])
        check("calls", [m["calls"] for m in got], [2, 1, 1, 0])
        check("lookups", [m["lookups"] for m in got], [1, 1, 0, 0])
        check("wait loops", [m["waits"] for m in got], [1, 0, 0, 0])
        check("errored results", [m["errors"] for m in got], [1, 0, 0, 0])
        check("image results", [m["images"] for m in got], [0, 1, 0, 0])
        check("only later messages carry the image", [m["after_image"] for m in got], [False, False, True, True])
        check("no command text is kept", "git status" in json.dumps(got) or "sleep" in json.dumps(got), False)
        peak, turns, lookups, images, errors, waits, avoidable = signals(got, "main")
        check("counts", (peak, turns, lookups, images, errors, waits), (120010, 3, 2, 1, 1, 1))
        spent = json.loads(avoidable)
        check("context tokens are those above 80k", spent["context"][0], 10010 + 20010 + 40010)
        check("a turn with a lookup and a wait is not a lookup turn", spent["lookup"][0], 100000 + 10 + 50)
        check("errored turn tokens", spent["error"][0], 90000 + 10 + 50)
        check("image turn tokens", spent["image"][0], 120000 + 10 + 50)
        check("wait turn usd is that turn's list price", spent["wait"][1], round(message_cost(got[0]), 6))
        check("a subagent has no lookup share", json.loads(signals(got, "subagent")[6])["lookup"], [0, 0.0])
        check("sonnet has no lookup share", json.loads(signals([dict(m, model="claude-sonnet-5-5") for m in got], "main")[6])["lookup"][0], 0)
        check("a short run has none of the context signals", signals(got[3:], "main")[:2], (1010, 0))
        last = last_usage(transcript)
        check("last_usage reads the last message", (last["cr"], last["in"]), (1000, 10))
        with open(transcript, "w") as fh:
            fh.write(turn("a", [], size=5))
            fh.write(json.dumps({"message": {"role": "user", "content": "x" * 600000}}) + "\n")
            fh.write("not json\n")
        check("last_usage widens its read past a long line", last_usage(transcript)["cr"], 5)
        check("last_usage on a missing file", last_usage(os.path.join(tmp, "absent.jsonl")), None)

        fires = os.path.join(tmp, "fires.db")
        with temp_env(LEAN_DB=fires, LEAN_STATS=None):
            record_fire("a.py", "note", "s1", "/w")
            conn = connect()
            got = [tuple(r) for r in conn.execute("SELECT hook, outcome, session_id, cwd FROM hook_fires")]
            conn.close()
            check("fire recorded", got, [("a.py", "note", "s1", "/w")])
        quiet = os.path.join(tmp, "off.db")
        with temp_env(LEAN_DB=quiet, LEAN_STATS="off"):
            check("off", off(), True)
            record_fire("a.py", "note")
            check("off writes nothing", os.path.exists(quiet), False)
        blocker = os.path.join(tmp, "file")
        open(blocker, "w").close()
        with temp_env(LEAN_DB=os.path.join(blocker, "lean.db"), LEAN_STATS=None):
            check("unwritable path is dropped silently", record_fire("a.py", "block"), None)

        fresh = os.path.join(tmp, "race", "lean.db")
        with temp_env(LEAN_DB=fresh, LEAN_STATS=None):
            errors = []

            def fire():
                try:
                    record_fire("race.py", "note")
                except BaseException as exc:  # record_fire must never raise
                    errors.append(repr(exc))

            threads = [threading.Thread(target=fire) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            check("concurrent first writes raise nothing", errors, [])
            conn = connect()
            check("concurrent first writes leave one schema", schema_version(conn), len(MIGRATIONS))
            before = conn.execute("SELECT COUNT(*) FROM hook_fires").fetchone()[0]
            record_fire("race.py", "note")
            after = conn.execute("SELECT COUNT(*) FROM hook_fires").fetchone()[0]
            conn.close()
            check("every racing fire is stored", before, 8)
            check("the next fire is stored", after, before + 1)

    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

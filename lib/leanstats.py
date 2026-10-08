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
]


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


def read_usage(path):
    """One dict per assistant message in a transcript: model, cwd, ts, session and five token counts.

    Claude Code writes one row per content block and repeats the message's usage
    on each, so usage is counted once per message id and the last row wins. A row
    with no id counts alone. Lines that are not JSON are skipped.
    """
    seen, order = {}, []
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
            if not isinstance(usage, dict):
                continue
            key = message.get("id") or f"row-{number}"
            split = usage.get("cache_creation")
            split = split if isinstance(split, dict) else {}
            written = int(usage.get("cache_creation_input_tokens") or 0)
            if key not in seen:
                order.append(key)
            seen[key] = {
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
    return [seen[key] for key in order]


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

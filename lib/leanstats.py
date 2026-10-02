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

PRICES = {  # input, cache write, cache read, output; USD per million tokens
    "opus": (15.0, 18.75, 1.50, 75.0),
    "fable": (15.0, 18.75, 1.50, 75.0),
    "sonnet": (3.0, 3.75, 0.30, 15.0),
    "haiku": (1.0, 1.25, 0.10, 5.0),
}

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
]


def family(model):
    for name in PRICES:
        if name in model:
            return name
    return "opus"


def cost(fam, u):
    pi, pw, pr, po = PRICES[fam]
    return (u["in"] * pi + u["cw"] * pw + u["cr"] * pr + u["out"] * po) / 1e6


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


def connect(path=None, timeout=5.0):
    """Open the database, creating and migrating it as needed. Raises NewerSchema."""
    path = path or db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        migrate(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def read_usage(path):
    """One dict per assistant message in a transcript: model, cwd, ts, session and four token counts.

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
            if key not in seen:
                order.append(key)
            seen[key] = {
                "model": message.get("model") or "",
                "cwd": row.get("cwd") or "",
                "ts": row.get("timestamp") or "",
                "session": row.get("sessionId") or "",
                "in": int(usage.get("input_tokens") or 0),
                "cw": int(usage.get("cache_creation_input_tokens") or 0),
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
    check("cost", cost("sonnet", {"in": 1000000, "cw": 0, "cr": 0, "out": 1000000}), 18.0)

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
            fh.write(row("", {"input_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}))
            fh.write('{"message": {"id": "m9", "usage": {"input_tok')
        usage = read_usage(transcript)
        check("one entry per message id, id-less rows alone", len(usage), 3)
        check("repeated id counted once, last row wins", (usage[0]["in"], usage[0]["out"]), (100, 40))
        check("fields", (usage[0]["model"], usage[0]["cwd"], usage[0]["session"], usage[0]["ts"]),
              ("claude-sonnet-5-5", "/w", "s1", "2026-10-02T01:00:00.000Z"))
        check("cache tokens", (usage[2]["cr"], usage[2]["cw"]), (7, 3))
        check("total input", sum(u["in"] for u in usage), 110)
        check("missing file is empty", read_usage(os.path.join(tmp, "absent.jsonl")), [])

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
            check("at most one row per racing hook", before <= 8, True)
            check("the next fire is stored", after, before + 1)

    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

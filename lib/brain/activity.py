import hashlib
import json
import os
import re
import sqlite3
import sys
import time

from .store import connect, now


def ensure_activities(con):
    """Created on first use, not by a migration: a schema bump would lock out every older brain sharing the file."""
    con.execute("""CREATE TABLE IF NOT EXISTS activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, ts TEXT NOT NULL, action TEXT NOT NULL,
        project TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 1,
        duration_ms INTEGER, error TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
        raw TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS activities_ts ON activities (ts)")


TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z$")
# The fields a BaryoVM activity line may carry (BaryoDev/BaryoVM#126). Any other field rejects the line,
# so a secret that leaks into the log is never copied into brain's raw column.
BARYOVM_FIELDS = {"ts", "version", "action", "vm", "stack", "ok", "duration_ms", "error", "host"}


def baryovm_row(text):
    """The activities columns for one BaryoVM line, or None when the line is malformed or has unknown fields."""
    try:
        o = json.loads(text)
    except ValueError:
        return None
    if not isinstance(o, dict) or not set(o) <= BARYOVM_FIELDS:
        return None
    ts, action, ok = o.get("ts"), o.get("action"), o.get("ok")
    if not isinstance(ts, str) or not TS_RE.match(ts) or not isinstance(action, str) or not action \
            or not isinstance(ok, bool):
        return None
    dur = o.get("duration_ms")
    if dur is not None and (not isinstance(dur, int) or isinstance(dur, bool) or not 0 <= dur < 2 ** 63):
        return None
    texts = {k: o.get(k, "") for k in ("vm", "stack", "error", "host", "version")}
    if not all(isinstance(v, str) for v in texts.values()):
        return None
    return (TS_RE.match(ts).group(1) + "Z", action, texts["stack"], texts["vm"], int(ok), dur,
            texts["error"], texts["host"], text)


def since_cutoff(spec):
    m = re.fullmatch(r"(\d+)([smhdw])", spec)
    if not m:
        sys.exit(f"brain: --since wants a number and s, m, h, d or w, like 24h: {spec!r}")
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0, time.time() - secs)))


def activity_line(r):
    mark = "ok  " if r["ok"] else "FAIL"
    where = " ".join(x for x in (r["project"], r["target"]) if x)
    dur = f" {r['duration_ms']}ms" if r["duration_ms"] is not None else ""
    host = f" @{r['host']}" if r["host"] else ""
    err = f" error: {r['error']}" if r["error"] else ""
    return f"#{r['id']} {r['ts']} {mark} {r['source']} {r['action']}{' ' + where if where else ''}{dur}{host}{err}"


def first_line_hash(f):
    f.seek(0)
    return hashlib.sha1(f.readline(4096)).hexdigest()


def import_baryovm(con, path):
    """Read the lines written since the last import. The offset moves in the same transaction as the rows.

    The stored state is the offset plus a hash of the file's first line. A file whose first line changed, that
    is shorter than the offset, or whose byte before the offset is not a newline is a new file: read from 0."""
    key = "activity_offset:baryovm:" + os.path.realpath(path)
    imported = skipped = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        offset, _, saved = (row["value"] if row else "0|").partition("|")
        offset = int(offset)
        with open(path, "rb") as f:
            fp = first_line_hash(f)
            if offset:
                f.seek(offset - 1)
                if fp != saved or f.read(1) != b"\n":
                    offset = 0
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # a last line without its newline is still being written
        stamp = now()
        for raw in data[:end].split(b"\n")[:-1]:
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.strip():
                continue
            try:
                r = baryovm_row(raw.decode("utf-8"))
                if r is None:
                    skipped += 1
                    continue
                con.execute("INSERT INTO activities (source, ts, action, project, target, ok, duration_ms, error,"
                            " host, raw, imported_at) VALUES ('baryovm',?,?,?,?,?,?,?,?,?,?)", (*r, stamp))
                imported += 1
            except (UnicodeDecodeError, OverflowError, sqlite3.Error):
                skipped += 1
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, f"{offset + end}|{fp}"))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return imported, skipped


def cmd_activity(a):
    con = connect(migrate=False)  # no schema bump: an older brain sharing the file keeps working
    ensure_activities(con)
    if a.acmd == "add":
        ts = now()
        if a.ts:
            m = TS_RE.match(a.ts)
            if not m:
                sys.exit(f"brain: --ts wants UTC like 2026-10-19T08:00:00Z: {a.ts!r}")
            ts = m.group(1) + "Z"
        with con:
            cur = con.execute(
                "INSERT INTO activities (source, ts, action, project, target, ok, duration_ms, error, host, raw,"
                " imported_at) VALUES (?,?,?,?,?,?,?,?,?,'',?)",
                (a.source, ts, a.action, a.project, a.target, a.ok, a.duration_ms, a.error, a.host, now()))
        print(f"activity #{cur.lastrowid}")
        return 0
    if a.acmd == "import-baryovm":
        path = a.file or os.path.join(os.environ.get("BARYOVM_HOME") or os.path.expanduser("~/.baryovm"),
                                      "activity.jsonl")
        if not os.path.exists(path):
            print(f"brain: no activity file at {path}; nothing to import")
            return 0
        try:
            imported, skipped = import_baryovm(con, path)
        except OSError as e:
            print(f"brain: cannot read {path}: {e.strerror or e}", file=sys.stderr)
            return 1
        print(f"imported {imported}, skipped {skipped} from {path}")
        return 0
    where, params = [], []
    for col, val in (("source", a.source), ("project", a.project)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    if a.since:
        where.append("ts>=?")
        params.append(since_cutoff(a.since))
    if a.failed:
        where.append("ok=0")
    sql = "SELECT * FROM activities" + (" WHERE " + " AND ".join(where) if where else "") \
        + " ORDER BY ts DESC, id DESC LIMIT ?"
    for r in con.execute(sql, (*params, a.limit)):
        print(activity_line(r))
    return 0

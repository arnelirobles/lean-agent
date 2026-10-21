import calendar
import os
import sqlite3
import sys
import time


STATUS = ("open", "closed", "wontfix")


RESOLUTION = ("fixed", "flaky", "not-a-bug", "duplicate", "answered", "filed")


SEVERITY = ("critical", "important", "minor", "info")


PHASE = ("code", "test", "fix", "review")


SOURCE = ("test-phase", "agent", "review", "user")


MODEL = ("haiku", "sonnet", "opus", "")


TIERS = ("fast", "full", "rerun")


RUNNERS = ("mtp", "vstest", "")


ITEM_COLS = ("id project repo branch kind ref title body status resolution severity phase source model "
             "cycle commit_hash link run_id occurrences first_seen last_seen closed_at").split()


def db_path():
    return os.environ.get("LEAN_BRAIN_DB") or os.path.expanduser("~/.local/share/lean/brain.db")


def migrate_1(con):  # the first layout: one flat table
    con.execute("""CREATE TABLE entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, ref TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
        seen INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        closed_at TEXT, note TEXT NOT NULL DEFAULT '')""")


def migrate_2(con):  # items, tags and runs; carries over rows from the flat table
    con.execute("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
        repo TEXT NOT NULL DEFAULT '', test_project TEXT NOT NULL DEFAULT '', runner TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'full', started INTEGER, ended INTEGER, duration_s REAL,
        lane_wait_s REAL, load_start REAL, load_end REAL, passed INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        stalled INTEGER NOT NULL DEFAULT 0, exit_code INTEGER)""")
    con.execute("""CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL, ref TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', cycle TEXT NOT NULL DEFAULT '', commit_hash TEXT NOT NULL DEFAULT '',
        link TEXT NOT NULL DEFAULT '', run_id INTEGER REFERENCES runs(id), occurrences INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed_at TEXT)""")
    con.execute("CREATE TABLE tags (item_id INTEGER NOT NULL REFERENCES items(id), tag TEXT NOT NULL, "
                "PRIMARY KEY (item_id, tag))")
    con.execute("CREATE INDEX items_key ON items (project, kind, ref, status)")
    con.execute("CREATE INDEX runs_test ON runs (test_project)")
    if con.execute("SELECT name FROM sqlite_master WHERE name='entries'").fetchone():
        for r in con.execute("SELECT * FROM entries ORDER BY id").fetchall():
            kind = "test-fail" if r["kind"] == "test-failure" else r["kind"]
            body = r["detail"] + (("\n\nclosed: " + r["note"]) if r["note"] else "")
            con.execute("INSERT INTO items (id, project, branch, kind, ref, title, body, status, occurrences,"
                        " first_seen, last_seen, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["project"], r["branch"], kind, r["ref"], r["ref"], body, r["status"],
                         r["seen"], r["first_seen"], r["last_seen"], r["closed_at"]))
        con.execute("DROP TABLE entries")


def migrate_3(con):  # the queue test-phase drains
    con.execute("""CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
        repo_dir TEXT NOT NULL, test_project TEXT NOT NULL, filter TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'full', after_id INTEGER REFERENCES jobs(id),
        status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
        enqueued_by TEXT NOT NULL DEFAULT '', enqueued_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
        run_id INTEGER REFERENCES runs(id))""")
    con.execute("CREATE INDEX jobs_status ON jobs (status, id)")


def migrate_4(con):  # extra arguments for the test command, per job
    con.execute("ALTER TABLE jobs ADD COLUMN args TEXT NOT NULL DEFAULT ''")


def migrate_5(con):  # the session that filed an item and its folder, so an answer can resume it
    con.execute("ALTER TABLE items ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE items ADD COLUMN session_cwd TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE items ADD COLUMN answer_delivered_at TEXT NOT NULL DEFAULT ''")
    con.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL, ended_at TEXT NOT NULL DEFAULT '')""")


def migrate_6(con):  # a bumped job runs before the rest of the queue
    con.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")


def migrate_7(con):  # which platform a job needs, and which host ran a job or a run
    con.execute("ALTER TABLE jobs ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE jobs ADD COLUMN host TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE runs ADD COLUMN host TEXT NOT NULL DEFAULT ''")
    # Platform was not detected before this step; unknown means local only.
    con.execute("UPDATE jobs SET platform='apple' WHERE status IN ('queued','running')")


def migrate_8(con):  # the repo HEAD a job was queued at, so a full run is not repeated for one commit
    con.execute("ALTER TABLE jobs ADD COLUMN commit_hash TEXT NOT NULL DEFAULT ''")


MIGRATIONS = [migrate_1, migrate_2, migrate_3, migrate_4, migrate_5, migrate_6, migrate_7, migrate_8]


def connect(migrate=True):
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def current():
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if not row and con.execute("SELECT name FROM sqlite_master WHERE name='entries'").fetchone():
            version = 1  # the flat layout was written before the version table existed
        if version > len(MIGRATIONS):
            sys.exit(f"brain: {path} has schema version {version}, newer than this brain knows")
        return version

    if migrate and current() < len(MIGRATIONS):
        # One writer migrates; a second opener waits for the lock, then sees the new version and does nothing.
        # SQLite DDL is transactional, so a migration that fails leaves the old version and no half-built tables.
        con.execute("BEGIN IMMEDIATE")
        try:
            for n in range(current(), len(MIGRATIONS)):
                MIGRATIONS[n](con)
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(n + 1),))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    con.isolation_level = ""  # the rest of brain relies on `with con:` opening and committing a transaction
    return con


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def epoch(ts):
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))

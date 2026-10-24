from datetime import datetime, timedelta, timezone

from .store import now


NOTICE_WINDOW = timedelta(minutes=10)


def add_created(con, kind, forge, repo, branch, url, number, title, session_id, cwd):
    with con:
        con.execute("INSERT OR IGNORE INTO created (kind, forge, repo, branch, url, number, title, session_id, cwd,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (kind, forge, repo, branch, url, number, title, session_id, cwd, now()))
        return con.execute("SELECT id FROM created WHERE url=?", (url,)).fetchone()["id"]


def set_state(con, url, state):
    with con:
        con.execute("UPDATE created SET state=?, state_checked_at=? WHERE url=?", (state, now(), url))


def add_notice(con, hook, text, session_id, cwd):
    cutoff = (datetime.now(timezone.utc) - NOTICE_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    with con:
        same = con.execute("SELECT id FROM notices WHERE hook=? AND text=? AND created_at>=? ORDER BY id DESC LIMIT 1",
                           (hook, text, cutoff)).fetchone()
        if same:
            return same["id"]
        return con.execute("INSERT INTO notices (hook, text, session_id, cwd, created_at) VALUES (?,?,?,?,?)",
                           (hook, text, session_id, cwd, now())).lastrowid


def dismiss_notice(con, notice_id):
    with con:
        cur = con.execute("UPDATE notices SET dismissed_at=? WHERE id=?", (now(), notice_id))
        return cur.rowcount > 0

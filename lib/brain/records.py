import sys
from datetime import datetime, timedelta, timezone

from .store import connect, now


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


def created_line(r):
    where = r["repo"] + (f"#{r['number']}" if r["number"] is not None else "")
    state = f" [{r['state']}]" if r["state"] else ""
    title = f" {r['title']}" if r["title"] else ""
    return f"{r['created_at']} {r['kind']} {where}{state}{title} {r['url']}"


def cmd_created(a):
    con = connect()
    sql = "SELECT * FROM created" + (" WHERE state IN ('', 'open')" if a.open else "") \
        + " ORDER BY created_at DESC, id DESC LIMIT ?"
    for r in con.execute(sql, (a.limit,)):
        print(created_line(r))
    return 0


def notice_line(r):
    state = f" [dismissed {r['dismissed_at']}]" if r["dismissed_at"] else ""
    return f"{r['id']} {r['created_at']} {r['hook']}{state} {r['text']}"


def cmd_notice(a):
    con = connect()
    if a.ncmd == "dismiss":
        if not dismiss_notice(con, a.id):
            print(f"no notice {a.id}", file=sys.stderr)
            return 1
        print(f"dismissed {a.id}")
        return 0
    sql = "SELECT * FROM notices" + ("" if a.all else " WHERE dismissed_at IS NULL OR dismissed_at=''") \
        + " ORDER BY created_at DESC, id DESC LIMIT ?"
    for r in con.execute(sql, (a.limit,)):
        print(notice_line(r))
    return 0

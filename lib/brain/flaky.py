from .items import line
from .store import connect, epoch, now


def cmd_flaky_clear(a):
    """Close each open flaky item of a test project that has had no new occurrence in --runs passing full runs.

    A passing run is a full run of the project that exited 0 and passed tests, started after the item was last seen."""
    con = connect()
    with con:
        rows = con.execute("SELECT items.* FROM items JOIN runs ON runs.id = items.run_id"
                           " WHERE items.status='open' AND items.kind='flaky' AND items.project=?"
                           " AND runs.test_project=? ORDER BY items.id", (a.project, a.test_project)).fetchall()
        for r in rows:
            n = con.execute("SELECT COUNT(*) FROM runs WHERE project=? AND test_project=? AND tier='full'"
                            " AND exit_code=0 AND failed=0 AND passed>0 AND started>=?",
                            (a.project, a.test_project, epoch(r["last_seen"]))).fetchone()[0]
            if n < a.runs:
                continue
            con.execute("UPDATE items SET status='closed', resolution='flaky-cleared', closed_at=?, body=? WHERE id=?",
                        (now(), r["body"] + f"\n\nclosed: no new occurrence in {n} passing runs", r["id"]))
            print("closed: " + line(con, con.execute("SELECT * FROM items WHERE id=?", (r["id"],)).fetchone()))
    return 0

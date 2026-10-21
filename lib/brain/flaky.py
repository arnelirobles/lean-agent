import sys

from .items import line
from .store import connect, epoch, now


def cmd_flaky_clear(a):
    """Close each open flaky item of a test project that has had no new occurrence in --runs passing full runs.

    A passing run is an unfiltered full run of the test project that exited 0 and passed tests, started after the
    item was last seen. Runs carry no branch, so this does not look at the item's branch."""
    if a.runs < 1:
        sys.exit("brain: --runs must be at least 1")
    con = connect()
    with con:
        rows = con.execute("SELECT items.id, items.last_seen FROM items JOIN runs ON runs.id = items.run_id"
                           " WHERE items.status='open' AND items.kind='flaky' AND items.project=?"
                           " AND runs.test_project=? ORDER BY items.id", (a.project, a.test_project)).fetchall()
        for r in rows:
            n = con.execute("SELECT COUNT(*) FROM runs WHERE project=? AND test_project=? AND tier='full'"
                            " AND exit_code=0 AND failed=0 AND passed>0 AND started>=?",
                            (a.project, a.test_project, epoch(r["last_seen"]))).fetchone()[0]
            if n < a.runs:
                continue
            cur = con.execute("UPDATE items SET status='closed', resolution='flaky-cleared', closed_at=?,"
                              " body=body || ? WHERE id=? AND status='open'",
                              (now(), f"\n\nclosed: no new occurrence in {n} passing runs", r["id"]))
            if cur.rowcount:
                print("closed: " + line(con, con.execute("SELECT * FROM items WHERE id=?", (r["id"],)).fetchone()))
    return 0

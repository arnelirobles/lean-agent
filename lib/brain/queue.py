import json
import os
import re
import subprocess
import sys

from .store import connect, now


JOB_STATUS = ("queued", "running", "done", "failed", "cancelled")


JOB_TIERS = ("fast", "full")


def project_of(path):
    """The main repository's name, so a worktree and its repository are one project."""
    path = os.path.abspath(path)
    r = subprocess.run(["git", "-C", path if os.path.isdir(path) else os.path.dirname(path), "rev-parse",
                        "--git-common-dir"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        common = os.path.abspath(os.path.join(path, r.stdout.strip()))
        name = os.path.basename(common)
        return os.path.basename(os.path.dirname(common)) if name == ".git" else re.sub(r"\.git$", "", name)
    return os.path.basename(path.rstrip("/"))


def cancel_dependents(con, job_id, reason):
    for r in con.execute("SELECT id FROM jobs WHERE status='queued' AND after_id=?", (job_id,)).fetchall():
        con.execute("UPDATE jobs SET status='cancelled', reason=?, ended_at=? WHERE id=?", (reason, now(), r["id"]))
        cancel_dependents(con, r["id"], reason)


def job_line(r):
    s = f"job #{r['id']} {r['status']} {r['tier']} {r['project']} {r['test_project']}"
    if r["filter"]:
        s += f" filter={r['filter']}"
    if r["args"]:
        s += f" args={r['args']}"
    if r["cycle"]:
        s += f" cycle={r['cycle']}"
    if r["after_id"]:
        s += f" after=#{r['after_id']}"
    if r["priority"] and r["status"] == "queued":
        s += " bumped"
    if r["reason"]:
        s += f" ({r['reason']})"
    if r["run_id"]:
        s += f" run={r['run_id']}"
    if r["platform"]:
        s += f" platform={r['platform']}"
    if r["host"] and r["status"] == "running":
        s += f" on={r['host']}"
    return s


APPLE_TFM = re.compile(r"-(maccatalyst|ios|macos)", re.I)


TFM_ELEMENT = re.compile(r"<TargetFrameworks?(?:\s[^>]*)?>(.*?)</TargetFrameworks?\s*>", re.S | re.I)


USE_MAUI = re.compile(r"<UseMaui(?:\s[^>]*)?>\s*true\s*</UseMaui\s*>", re.I)


PROJECT_REF = re.compile(r"<ProjectReference\s[^>]*?Include\s*=\s*[\"']([^\"']+)[\"']", re.I)


def _apple_text(text):
    return any(APPLE_TFM.search(m.group(1)) for m in TFM_ELEMENT.finditer(text)) or bool(USE_MAUI.search(text))


def detect_platform(repo, test_project):
    """'apple' when the job may need an Apple platform, else ''.

    Conservative: the project file, Directory.Build.props from its folder up to the repo root, and every
    project it references (transitively) are checked. A project that cannot be found or read, or a folder
    with more than one csproj, gives 'apple' too: unknown means local only."""
    path = os.path.join(repo, test_project)
    try:
        if os.path.isdir(path):
            found = [f for f in os.listdir(path) if f.endswith(".csproj")]
            if len(found) != 1:
                return "apple"
            path = os.path.join(path, found[0])
        path = os.path.abspath(path)
        seen = set()
        todo = [path]
        props_done = set()
        root = os.path.abspath(repo)
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            text = open(cur, encoding="utf-8", errors="replace").read()
            if _apple_text(text):
                return "apple"
            folder = os.path.dirname(cur)
            while True:
                props = os.path.join(folder, "Directory.Build.props")
                if props not in props_done:
                    props_done.add(props)
                    if os.path.isfile(props):
                        if _apple_text(open(props, encoding="utf-8", errors="replace").read()):
                            return "apple"
                if folder == root or os.path.dirname(folder) == folder:
                    break
                if not (folder + os.sep).startswith(root + os.sep):
                    break
                folder = os.path.dirname(folder)
            for ref in PROJECT_REF.findall(text):
                todo.append(os.path.abspath(os.path.join(os.path.dirname(cur), ref.replace("\\", "/"))))
    except (OSError, ValueError):
        return "apple"
    return ""


def cmd_queue(a):
    con = connect()
    if a.qcmd == "add":
        if a.tier == "fast" and not a.filter:
            sys.exit("brain: a fast job needs --filter")
        dep = con.execute("SELECT status FROM jobs WHERE id=?", (a.after,)).fetchone() if a.after else None
        if a.after and not dep:
            sys.exit(f"brain: no job #{a.after} to run after")
        repo = os.path.abspath(a.repo_dir)
        # A job behind one that already failed or was cancelled can never run; record that at once.
        dead = dep and dep["status"] in ("failed", "cancelled")
        if not dead:
            # The same run already waiting: join it, so one build serves every agent that asked.
            same = con.execute(
                "SELECT id FROM jobs WHERE status='queued' AND repo_dir=? AND test_project=? AND filter IS ?"
                " AND tier=? AND after_id IS ? AND args IS ? ORDER BY id LIMIT 1",
                (repo, a.test_project, a.filter, a.tier, a.after, a.args)).fetchone()
            if same:
                print(f"brain: the same job is already queued as #{same['id']}; joined it", file=sys.stderr)
                print(same["id"])
                return 0
        with con:
            cur = con.execute(
                "INSERT INTO jobs (cycle, project, repo_dir, test_project, filter, tier, after_id, enqueued_by,"
                " enqueued_at, status, reason, ended_at, args, platform) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.cycle, a.project or project_of(repo), repo, a.test_project, a.filter, a.tier, a.after,
                 a.by, now(), "cancelled" if dead else "queued",
                 f"dependency #{a.after} {dep['status']}" if dead else "", now() if dead else None, a.args,
                 detect_platform(repo, a.test_project) if a.platform == "auto" else
                 ("apple" if a.platform == "apple" else "")))
        print(cur.lastrowid)
        return 0
    if a.qcmd == "list":
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        for col, val in (("cycle", a.cycle), ("status", a.status)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        # Queued jobs alone list in the order the drainer takes them.
        q += " ORDER BY priority DESC, id" if a.status == "queued" else " ORDER BY id"
        for r in con.execute(q, args).fetchall():
            print(job_line(r))
        return 0
    if a.qcmd == "cancel":
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            if r["status"] != "queued":
                sys.exit(f"brain: job #{a.id} is {r['status']}, only a queued job can be cancelled")
            con.execute("UPDATE jobs SET status='cancelled', reason='cancelled', ended_at=? WHERE id=?", (now(), a.id))
            cancel_dependents(con, a.id, f"dependency #{a.id} cancelled")
        print(f"cancelled job #{a.id}")
        return 0
    if a.qcmd == "bump":  # moves a queued job, and the queued jobs chained after it, to the front
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            if r["status"] != "queued":
                sys.exit(f"brain: job #{a.id} is {r['status']}, only a queued job can be bumped")
            top = con.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM jobs WHERE status='queued'").fetchone()[0]
            todo, bumped = [a.id], []
            while todo:
                job = todo.pop()
                con.execute("UPDATE jobs SET priority=? WHERE id=?", (top, job))
                bumped.append(job)
                todo += [d["id"] for d in con.execute(
                    "SELECT id FROM jobs WHERE status='queued' AND after_id=?", (job,)).fetchall()]
        print("bumped " + " ".join(f"#{j}" for j in sorted(bumped)))
        return 0
    if a.qcmd == "move":  # swaps a queued job with the one before or after it in run order
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            if r["status"] != "queued":
                sys.exit(f"brain: job #{a.id} is {r['status']}, only a queued job can be moved")
            jobs = con.execute("SELECT id, after_id FROM jobs WHERE status='queued' ORDER BY priority DESC, id").fetchall()
            i = [j["id"] for j in jobs].index(a.id)
            k = i - 1 if a.where == "up" else i + 1
            if not 0 <= k < len(jobs):
                print(f"job #{a.id} is already {'first' if a.where == 'up' else 'last'}")
                return 0
            first, second = (jobs[k], jobs[i]) if a.where == "up" else (jobs[i], jobs[k])
            if second["after_id"] == first["id"]:
                sys.exit(f"brain: job #{second['id']} waits on #{first['id']}, so it cannot run before it")
            jobs[i], jobs[k] = jobs[k], jobs[i]
            # The smallest priorities that give this order: from the back, a job keeps its successor's priority
            # when its id already sorts first, else it needs one more. Untouched jobs at the back stay at 0.
            prio = 0
            for n in range(len(jobs) - 1, -1, -1):
                if n < len(jobs) - 1 and jobs[n]["id"] > jobs[n + 1]["id"]:
                    prio += 1
                con.execute("UPDATE jobs SET priority=? WHERE id=?", (prio, jobs[n]["id"]))
        print(f"moved job #{a.id} {a.where}")
        return 0
    if a.qcmd == "next":  # claims the oldest ready job; prints it as one line of JSON, or nothing
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        skip = " AND j.platform != 'apple'" if a.where == "remote" else " AND j.platform != 'remote'"
        r = con.execute(
            "SELECT j.* FROM jobs j WHERE j.status='queued' AND (j.after_id IS NULL OR "
            "(SELECT status FROM jobs d WHERE d.id=j.after_id)='done')" + skip
            + " ORDER BY j.priority DESC, j.id LIMIT 1").fetchone()
        if r and not a.peek:
            con.execute("UPDATE jobs SET status='running', started_at=?, host=?, reason='' WHERE id=?",
                        (now(), a.host, r["id"]))
        con.execute("COMMIT")
        if r:
            row = dict(r)
            if not a.peek:
                row["host"] = a.host
            print(json.dumps(row))
        return 0
    if a.qcmd == "route":  # which lanes may claim a queued job
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            if r["status"] != "queued":
                sys.exit(f"brain: job #{a.id} is {r['status']}, only a queued job can be routed")
            con.execute("UPDATE jobs SET platform=? WHERE id=?",
                        ({"remote": "remote", "local": "apple", "any": ""}[a.to], a.id))
        if a.to == "remote" and detect_platform(r["repo_dir"], r["test_project"]) == "apple":
            print(f"brain: job #{a.id} may need an Apple platform; it can fail to build on a remote host",
                  file=sys.stderr)
        print(job_line(con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()))
        return 0
    if a.qcmd == "requeue":  # a running job goes back to the queue, for a lane problem
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            if r["status"] != "running":
                sys.exit(f"brain: job #{a.id} is {r['status']}, only a running job can be requeued")
            con.execute("UPDATE jobs SET status='queued', host='', started_at=NULL, reason=? WHERE id=?",
                        (a.reason, a.id))
        print(job_line(con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()))
        return 0
    if a.qcmd == "finish":
        with con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()
            if not r:
                sys.exit(f"brain: no job #{a.id}")
            con.execute("UPDATE jobs SET status=?, reason=?, run_id=COALESCE(?, run_id), ended_at=? WHERE id=?",
                        (a.status, a.reason, a.run_id, now(), a.id))
            if a.status != "done":
                cancel_dependents(con, a.id, f"dependency #{a.id} {a.status}")
        print(job_line(con.execute("SELECT * FROM jobs WHERE id=?", (a.id,)).fetchone()))
        return 0
    return 2

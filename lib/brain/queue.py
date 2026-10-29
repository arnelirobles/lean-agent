import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from .items import infer_session, session_ok
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
        name = os.path.basename(os.path.dirname(common)) if name == ".git" else re.sub(r"\.git$", "", name)
        return name.lower()
    return os.path.basename(path.rstrip("/")).lower()


def git_out(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                           timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


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


NPM_REGISTRY = re.compile(r"^\s*(?:@[^:=\s]+:)?registry\s*=\s*(\S+)", re.I)
NPM_AUTH = re.compile(r"^\s*(?://\S*:)?_?(?:authToken|auth|password|username)\s*=", re.I)


def private_npm_feed(repo, test_project):
    """True when a .npmrc from the package.json folder up to the repo root names a registry other than
    registry.npmjs.org. Its login lives on this machine (~/.npmrc), which a remote lane never gets."""
    folder = os.path.dirname(os.path.abspath(os.path.join(repo, test_project)))
    root = os.path.abspath(repo)
    while True:
        try:
            lines = open(os.path.join(folder, ".npmrc"), encoding="utf-8-sig", errors="replace").read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            if NPM_AUTH.match(line) or "${" in line:  # a login or an env placeholder: the value lives on this machine
                return True
            m = NPM_REGISTRY.match(line)
            if m:
                try:
                    host = urlparse(m.group(1).strip("\"'")).hostname
                except ValueError:
                    host = None
                if host != "registry.npmjs.org":  # a host that does not parse, such as ${REGISTRY}, counts as private
                    return True
        if folder == root or os.path.dirname(folder) == folder or not (folder + os.sep).startswith(root + os.sep):
            return False
        folder = os.path.dirname(folder)


GRADLE_SETTINGS = ("settings.gradle.kts", "settings.gradle")


def is_gradle(repo, test_project):
    """True when the test path is a Gradle settings file or a folder that holds one."""
    if os.path.basename(test_project) in GRADLE_SETTINGS:
        return True
    folder = os.path.join(repo, test_project)
    return os.path.isdir(folder) and any(os.path.isfile(os.path.join(folder, n)) for n in GRADLE_SETTINGS)


def gradle_ios(args):
    """True when a task in the job's args is an iOS test task (a name with ios and Test), which needs a Mac."""
    for tok in (args or "").split():
        if not tok.startswith("-") and re.search(r"ios", tok, re.I) and "Test" in tok:
            return True
    return False


def detect_platform(repo, test_project, args=None):
    """'apple' when the job may need an Apple platform, 'private' when an npm job needs a private feed or the job is
    a Gradle build (local only, its publishing credentials never leave this machine), else ''.

    Conservative: the project file, Directory.Build.props from its folder up to the repo root, and every
    project it references (transitively) are checked. A project that cannot be found or read, or a folder
    with more than one csproj, gives 'apple' too: unknown means local only."""
    path = os.path.join(repo, test_project)
    base = os.path.basename(test_project)
    if is_gradle(repo, test_project):
        return "apple" if gradle_ios(args) else "private"
    if base == "package.json" or base.startswith("playwright.config"):
        return "private" if private_npm_feed(repo, test_project) else ""
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


def wait_line(con, r):
    """One line for a job that ended: id, status, passed and failed of its run, the items it filed."""
    run = con.execute("SELECT passed, failed FROM runs WHERE id=?", (r["run_id"],)).fetchone() if r["run_id"] else None
    ids = [str(i["id"]) for i in con.execute("SELECT id FROM items WHERE run_id=? ORDER BY id", (r["run_id"],))] \
        if r["run_id"] else []
    s = f"job #{r['id']} {r['status']} passed={run['passed'] if run else '-'} failed={run['failed'] if run else '-'}"
    s += " items=" + (",".join("#" + i for i in ids) if ids else "-")
    if r["reason"]:
        s += " (" + " ".join(r["reason"].split())[:200] + ")"
    return s


def queue_wait(con, a):
    """Block until every named job ends. Exit 0 all done, 1 any failed or cancelled, 2 unknown id, 3 timeout."""
    ids = list(dict.fromkeys(a.ids))
    for i in ids:
        if not con.execute("SELECT 1 FROM jobs WHERE id=?", (i,)).fetchone():
            print(f"brain: no job #{i}", file=sys.stderr)
            return 2
    deadline = time.monotonic() + a.timeout
    while True:
        rows = [con.execute("SELECT * FROM jobs WHERE id=?", (i,)).fetchone() for i in ids]
        pending = [r for r in rows if r["status"] in ("queued", "running")]
        if not pending:
            break
        if time.monotonic() >= deadline:
            for r in rows:
                print(wait_line(con, r) if r not in pending else f"{job_line(r)} (still {r['status']})")
            print(f"brain: timed out after {a.timeout:g}s with {len(pending)} of {len(ids)} jobs not ended: "
                  + ", ".join(f"#{r['id']} {r['status']}" for r in pending), file=sys.stderr)
            return 3
        time.sleep(min(a.interval, max(0.0, deadline - time.monotonic())))
    for r in rows:
        print(wait_line(con, r))
    return 0 if all(r["status"] == "done" for r in rows) else 1


def cmd_queue(a):
    con = connect()
    if a.qcmd == "wait":
        return queue_wait(con, a)
    if a.qcmd == "add":
        if a.tier == "fast" and not a.filter:
            sys.exit("brain: a fast job needs --filter")
        again = " ".join(a.again.split())[:200]  # one line, so a reason cannot forge a line of `queue list`
        if a.again and not again:
            sys.exit("brain: --again needs a reason")
        repo = os.path.abspath(a.repo_dir)
        commit = git_out(repo, "rev-parse", "HEAD") or ""
        # Everything that runs git or reads files happens before the write lock is taken.
        project = a.project or project_of(repo)
        platform = detect_platform(repo, a.test_project, a.args) if a.platform == "auto" else ("apple" if a.platform == "apple" else "")
        st = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True) \
            if a.tier == "full" and commit and not again else None
        session, session_cwd = a.session, a.session_cwd if a.session else ""
        if not session and a.infer_session:
            session, session_cwd = infer_session(con)
        if session and not session_ok(session):
            sys.exit("brain: --session takes letters, digits and dashes only")
        # The checks and the insert are one transaction, so parallel adds of one job make one job.
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            dep = con.execute("SELECT status FROM jobs WHERE id=?", (a.after,)).fetchone() if a.after else None
            if a.after and not dep:
                sys.exit(f"brain: no job #{a.after} to run after")
            # A job behind one that already failed or was cancelled can never run; record that at once.
            dead = dep and dep["status"] in ("failed", "cancelled")
            if not dead:
                # The same run already waiting: join it, so one build serves every agent that asked.
                same = con.execute(
                    "SELECT id, session_id FROM jobs WHERE status='queued' AND repo_dir=? AND test_project=? AND filter IS ?"
                    " AND tier=? AND after_id IS ? AND args IS ? AND commit_hash=? ORDER BY id LIMIT 1",
                    (repo, a.test_project, a.filter, a.tier, a.after, a.args, commit)).fetchone()
                if same:
                    if session and not same["session_id"]:
                        con.execute("UPDATE jobs SET session_id=?, session_cwd=? WHERE id=?",
                                    (session, session_cwd, same["id"]))
                    con.execute("COMMIT")
                    print(f"brain: the same job is already queued as #{same['id']}; joined it", file=sys.stderr)
                    print(same["id"])
                    return 0
                # One full run per commit: the suite runs once, as the final check. A filtered full job is not it.
                if a.tier == "full" and commit and not again:
                    prior = con.execute(
                        "SELECT id, status FROM jobs WHERE tier='full' AND COALESCE(filter,'')='' AND project=? AND test_project=?"
                        " AND commit_hash=? AND status IN ('queued','running','done') ORDER BY id LIMIT 1",
                        (project, a.test_project, commit)).fetchone()
                    if prior:
                        sys.exit(f"brain: a full run of {project} {a.test_project} at {commit[:8]} already exists as job "
                                 f"#{prior['id']} ({prior['status']}); pass --again \"reason\" to run it again")
                    if st.returncode != 0:
                        sys.exit(f"brain: git status failed in {repo}, so the tree cannot be checked: "
                                 f"{st.stderr.strip()}; pass --again \"reason\" to queue anyway")
                    if st.stdout.strip():
                        paths = [ln[3:] for ln in st.stdout.splitlines()]
                        more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
                        sys.exit(f"brain: {repo} has uncommitted changes ({', '.join(paths[:5])}{more}), so a full run "
                                 f"would not be the commit {commit[:8]}; commit first, or pass --again \"reason\"")
            cur = con.execute(
                "INSERT INTO jobs (cycle, project, repo_dir, test_project, filter, tier, after_id, enqueued_by,"
                " enqueued_at, status, reason, ended_at, args, platform, commit_hash, session_id, session_cwd, branch)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.cycle, project, repo, a.test_project, a.filter, a.tier, a.after,
                 a.by, now(), "cancelled" if dead else "queued",
                 f"dependency #{a.after} {dep['status']}" if dead else
                 (f"again: {again}" if again else ""), now() if dead else None, a.args,
                 platform, commit, session, session_cwd, current_branch(repo)))
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
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
        skip = " AND j.platform NOT IN ('apple', 'private')" if a.where == "remote" else " AND j.platform != 'remote'"
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
            # Refuse only a job marked apple: detect_platform also says apple for a project it cannot read.
            why = ("is a Gradle build, and a remote lane never receives the Gradle or Maven credentials of this machine"
                   if is_gradle(r["repo_dir"], r["test_project"]) else
                   "uses a private npm feed whose login only this machine has, so npm ci would fail with E401 on a remote host")
            if a.to in ("remote", "any") and r["platform"] == "private" and not a.force:
                sys.exit(f"brain: job #{a.id} {why}; pass --force to "
                         + ("send it anyway" if a.to == "remote" else "let any lane take it"))
            if a.to == "remote" and r["platform"] == "apple" and not a.force:
                sys.exit(f"brain: job #{a.id} is marked Apple, this machine only, and would likely fail to build on a remote host; "
                         "pass --force to send it anyway")
            con.execute("UPDATE jobs SET platform=? WHERE id=?",
                        ({"remote": "remote", "local": "apple", "any": ""}[a.to], a.id))
        if a.to == "remote" and detect_platform(r["repo_dir"], r["test_project"]) == "private":
            print(f"brain: job #{a.id} " + ("is a Gradle build; test-phase refuses to run it on a remote lane"
                  if is_gradle(r["repo_dir"], r["test_project"]) else
                  "uses a private npm feed; npm ci can fail with E401 on a remote host"), file=sys.stderr)
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

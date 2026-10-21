import json
import os
import subprocess
import sys
import tempfile
import time

from .store import KIND, MIGRATIONS, connect, migrate_1
from .queue import detect_platform, project_of


def connect_rows(path, query):
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def run(brain_cli):
    fails = []
    me = brain_cli
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "sub", "brain.db"))
        env.pop("LEAN_SESSION_ID", None)
        env.pop("LEAN_SESSION_CWD", None)

        def run(*args, stdin=None):
            r = subprocess.run([sys.executable, me, *args], env=env, capture_output=True, text=True, input=stdin)
            return r.returncode, r.stdout.strip(), r.stderr.strip()

        rc, out, _ = run("add", "--project", "core", "--branch", "main", "--kind", "test-fail", "--ref", "Foo.Bar",
                         "--body", "boom", "--severity", "important", "--tag", "db")
        if rc != 0 or not out.startswith("added: #1 open test-fail core@main Foo.Bar important occ=1") \
                or "tags=db" not in out:
            fails.append(f"add: {rc} {out!r}")
        rc, out, _ = run("add", "--project", "core", "--kind", "test-fail", "--ref", "Foo.Bar", "--tag", "load")
        if "seen again: #1" not in out or "occ=2" not in out or "tags=db,load" not in out:
            fails.append(f"dedupe must bump occurrences and merge tags: {out!r}")
        rc, out, _ = run("show", "1")
        if "body: boom" not in out or "core@main" not in out:
            fails.append(f"a repeat without body or branch lost them: {out!r}")
        run("add", "--project", "core", "--kind", "flaky", "--ref", "Foo.Bar")
        rc, out, _ = run("add", "--project", "web", "--kind", "question", "--ref", "x", "--body-file", "-",
                         "--model", "sonnet", stdin="line one\nline two")
        if rc != 0:
            fails.append(f"add with body on stdin: {out!r}")
        rc, out, _ = run("show", "3")
        if "line one" not in out or "model: sonnet" not in out:
            fails.append(f"stdin body or model lost: {out!r}")
        rc, out, _ = run("list")
        if len(out.splitlines()) != 3:
            fails.append(f"kind is part of the dedupe key, want 3 rows: {out!r}")
        for args, want in ((("--project", "core", "--kind", "flaky"), 1), (("--tag", "db"), 1), (("--tag", "nope"), 0),
                           (("--status", "open"), 3), (("--cycle", "zz"), 0)):
            rc, out, _ = run("list", *args)
            if len([l for l in out.splitlines() if l]) != want:
                fails.append(f"list {args}: want {want} got {out!r}")
        rc, out, _ = run("close", "1", "--resolution", "fixed", "--commit", "abc123", "--note", "fixed in abc", "--tag", "done")
        if rc != 0 or "closed: #1 closed" not in out or "done" not in out:
            fails.append(f"close: {rc} {out!r}")
        rc, out, _ = run("show", "1")
        if "fixed in abc" not in out or "resolution: fixed" not in out or "commit: abc123" not in out:
            fails.append(f"close fields missing from show: {out!r}")
        rc, out, _ = run("list", "--open")
        if "#1 " in out or len(out.splitlines()) != 2:
            fails.append(f"--open still lists a closed item: {out!r}")
        run("add", "--project", "core", "--kind", "test-fail", "--ref", "Foo.Bar")
        rc, out, _ = run("list", "--project", "core", "--kind", "test-fail")
        if len(out.splitlines()) != 2:
            fails.append(f"a closed item must not absorb a new sighting: {out!r}")
        if run("reopen", "1")[0] == 0:
            fails.append("reopen made a second open copy of the same item")
        run("close", "4", "--wontfix", "--resolution", "not-a-bug")
        rc, out, _ = run("reopen", "4")
        if rc != 0 or "reopened: #4 open" not in out:
            fails.append(f"reopen: {rc} {out!r}")
        if "no item #99" not in run("close", "99")[2]:
            fails.append("close of a missing id")
        if run("add", "--project", "p", "--kind", "note", "--ref", "r", "--severity", "huge")[0] == 0:
            fails.append("unknown severity accepted")
        rc, _, err = run("add", "--project", "p", "--kind", "anything-goes", "--ref", "r")
        if rc == 0 or "test-fail" not in err or "review-finding" not in err:
            fails.append(f"an unknown kind must be refused with the list: {rc} {err!r}")

        # Runs and their link to items, and stats over them.
        ids = []
        for tier, dur, wait in (("full", 100, 10), ("full", 300, 30), ("rerun", 5, 0)):
            rc, out, _ = run("run-add", "--cycle", "c1", "--project", "core", "--test-project", "T.csproj",
                             "--tier", tier, "--runner", "mtp", "--started", "1000", "--ended", str(1000 + dur),
                             "--lane-wait", str(wait), "--passed", "9", "--failed", "1", "--exit-code", "1")
            ids.append(out)
        if ids != ["1", "2", "3"]:
            fails.append(f"run ids: {ids}")
        run("add", "--project", "core", "--kind", "flaky", "--ref", "Other.Flaky", "--run-id", "2", "--cycle", "c1")
        rc, out, _ = run("show", "5")
        if "run id: 2" not in out:
            fails.append(f"item not linked to its run: {out!r}")
        rc, out, _ = run("stats", "--json")
        try:
            s = json.loads(out)
        except ValueError:
            s = {}
            fails.append(f"stats --json not json: {out!r}")
        suite = (s.get("suites") or [{}])[0]
        if suite.get("runs") != 2 or suite.get("mean_duration_s") != 200 or suite.get("mean_lane_wait_s") != 20 \
                or suite.get("flaky_items") != 1 or suite.get("flaky_rate") != 0.5:
            fails.append(f"suite stats wrong (reruns must not count as runs): {suite}")
        if (s.get("slowest") or [{}])[0].get("duration_s") != 300:
            fails.append(f"slowest wrong: {s.get('slowest')}")
        if not any(r["kind"] == "flaky" and r["status"] == "open" for r in s.get("by_kind_status", [])) \
                or "test-fail" not in s.get("median_close_s", {}):
            fails.append(f"counts or median close missing: {s}")
        rc, out, _ = run("stats")
        if "flaky rate" not in out or "T.csproj" not in out or "open by project" not in out:
            fails.append(f"stats table: {out!r}")
        rc, out, _ = run("export", "--format", "md")
        if not out.startswith("# Brain") or "## core" not in out or "Foo.Bar" not in out:
            fails.append(f"export md: {out!r}")
        rc, out, _ = run("export", "--format", "json", "--status", "open")
        try:
            if not all(i["status"] == "open" and "tags" in i for i in json.loads(out)):
                fails.append("export json filter or tags")
        except ValueError:
            fails.append(f"export json: {out!r}")

        # resume: ordering, the cap, and silence with nothing to say.
        for k, t in (("question", "q1"), ("pending", "p1"), ("flaky", "f1"), ("agent", "a1"), ("blocker", "b1"),
                     ("pending", "p2")):
            run("add", "--project", "res", "--kind", k, "--ref", t, "--title", t)
        run("add", "--project", "other", "--kind", "blocker", "--ref", "x")
        rc, out, _ = run("resume", "--project", "res")
        order = [l.split()[2] for l in out.splitlines()[1:]]
        if rc != 0 or out.splitlines()[0] != "brain: 6 open and 0 queued or running for res" or order != ["b1", "a1", "p2", "p1", "q1", "f1"]:
            fails.append(f"resume order (blocker, agent, pending newest first, question, rest): {out!r}")
        rc, out, _ = run("resume", "--project", "res", "--max", "5")
        lines = out.splitlines()
        if len(lines) != 5 or "3 more" not in lines[-1] or "--project res --status open" not in lines[-1] \
                or "b1" not in lines[1]:
            fails.append(f"resume cap: {out!r}")
        for i in range(60):
            run("add", "--project", "big", "--kind", "pending", "--ref", f"n{i}")
        if len(run("resume", "--project", "big")[1].splitlines()) != 40:
            fails.append("resume default cap is not 40 lines")
        if run("resume", "--project", "empty")[1:] != ("", ""):
            fails.append("resume with nothing open must print nothing")
        env["LEAN_BRAIN_DB"] = os.path.join(d, "absent.db")
        rc, out, err = run("resume", "--project", "res")
        if (rc, out, err) != (0, "", "") or os.path.exists(env["LEAN_BRAIN_DB"]):
            fails.append(f"resume with no database must print nothing and create nothing: {rc} {out!r} {err!r}")
        env["LEAN_BRAIN_DB"] = os.path.join(d, "sub", "brain.db")

        # The queue: ordering, dependencies, cancel propagation, claiming.
        def nxt(*extra):
            out = run("queue", "next", *extra)[1]
            return json.loads(out) if out else None

        j1 = run("queue", "add", "/tmp/r", "A.csproj", "--cycle", "q", "--by", "me")[1]
        j2 = run("queue", "add", "/tmp/r", "A.csproj", "--tier", "fast", "--filter", "Unit", "--after", j1)[1]
        j3 = run("queue", "add", "/tmp/r", "B.csproj", "--after", j2)[1]
        j4 = run("queue", "add", "/tmp/r", "C.csproj")[1]
        if [j1, j2, j3, j4] != ["5", "6", "7", "8"] and not all(x.isdigit() for x in (j1, j2, j3, j4)):
            fails.append(f"queue add must print the job id: {[j1, j2, j3, j4]}")
        if run("queue", "add", "/tmp/r", "A.csproj", "--tier", "fast")[0] == 0:
            fails.append("a fast job without a filter accepted")
        if run("queue", "add", "/tmp/r", "A.csproj", "--after", "9999")[0] == 0:
            fails.append("a job after a missing job accepted")
        first = nxt()
        if not first or str(first["id"]) != j1 or first["status"] != "queued":
            fails.append(f"oldest ready job first: {first}")
        second = nxt()
        if not second or str(second["id"]) != j4:
            fails.append(f"a job whose dependency has not finished must wait, so the next ready one is {j4}: {second}")
        if nxt("--peek") is not None:
            fails.append("nothing is ready while the dependencies run")
        run("queue", "finish", j4, "--status", "done")
        run("queue", "finish", j1, "--status", "done")
        third = nxt()
        if not third or str(third["id"]) != j2:
            fails.append(f"dependent not ready after its dependency finished: {third}")
        out = run("queue", "finish", j2, "--status", "failed", "--reason", "tests failed")[1]
        rc, out, _ = run("queue", "list", "--status", "cancelled")
        if f"job #{j3} cancelled" not in out or f"dependency #{j2} failed" not in out:
            fails.append(f"a failed dependency must cancel its dependents: {out!r}")
        if nxt() is not None:
            fails.append("a cancelled job was claimed")
        k1 = run("queue", "add", "/tmp/r", "D.csproj")[1]
        k2 = run("queue", "add", "/tmp/r", "D.csproj", "--after", k1)[1]
        k3 = run("queue", "add", "/tmp/r", "D.csproj", "--after", k2)[1]
        run("queue", "cancel", k1)
        out = run("queue", "list", "--status", "cancelled")[1]
        if f"job #{k2} cancelled" not in out or f"job #{k3} cancelled" not in out:
            fails.append(f"cancel must reach every dependent: {out!r}")
        rc, out, err = run("queue", "cancel", j1)
        if rc == 0:
            fails.append("a finished job was cancelled")
        late = run("queue", "add", "/tmp/r", "F.csproj", "--after", j2)[1]
        late2 = run("queue", "add", "/tmp/r", "F.csproj", "--after", k1)[1]
        ja = run("queue", "add", "/tmp/r", "G.csproj", "--args=-p:A=1 -p:B=two", "--cycle", "qa")[1]
        got = nxt()
        if not got or str(got["id"]) != ja or got.get("args") != "-p:A=1 -p:B=two":
            fails.append(f"a job must keep its extra arguments: {got}")
        run("queue", "finish", ja, "--status", "done")
        out = run("queue", "list", "--status", "cancelled")[1]
        if f"job #{late} cancelled" not in out or f"dependency #{j2} failed" not in out \
                or f"job #{late2} cancelled" not in out or f"dependency #{k1} cancelled" not in out:
            fails.append(f"a job added after a failed or cancelled job must be cancelled with the reason: {out!r}")
        if nxt("--peek") is not None:
            fails.append("a job added behind a dead dependency became ready")
        d1 = run("queue", "add", "/tmp/r", "H.csproj", "--tier", "fast", "--filter", "X")[1]
        rc, d2, err = run("queue", "add", "/tmp/r", "H.csproj", "--tier", "fast", "--filter", "X", "--by", "other")
        if rc != 0 or d2 != d1 or f"#{d1}" not in err:
            fails.append(f"a job already queued must be joined, not added again: {d1} {d2} {err!r}")
        d3 = run("queue", "add", "/tmp/r", "H.csproj", "--tier", "fast", "--filter", "Y")[1]
        if d3 == d1:
            fails.append("a different filter is a different job")
        run("queue", "cancel", d1)
        run("queue", "cancel", d3)

        # One full run per commit, identical queued jobs joined by commit, no full run from a dirty tree.
        g = os.path.join(d, "gitrepo")
        os.makedirs(g)

        def git(*args):
            subprocess.run(["git", "-C", g, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                           capture_output=True)
        git("init", "-q")
        with open(os.path.join(g, "f.txt"), "w") as f:
            f.write("one\n")
        git("add", "f.txt")
        git("commit", "-q", "-m", "one")
        rc, c1, _ = run("queue", "add", g, "T.csproj")
        rc, out, err = run("queue", "add", g, "T.csproj", "--cycle", "other")
        if rc != 0 or out != c1:
            fails.append(f"a full job identical to a queued one must return its id: {rc} {out!r} {err!r}")
        rc, out, err = run("queue", "add", g, "T.csproj", "--args=-p:A=1")
        if rc == 0 or f"#{c1}" not in err:
            fails.append(f"a different full job for a commit with a full job queued must be refused with its id: {rc} {out!r} {err!r}")
        run("queue", "finish", c1, "--status", "done")
        rc, out, err = run("queue", "add", g, "T.csproj")
        if rc == 0 or f"#{c1}" not in err or "done" not in err:
            fails.append(f"a full job for a commit with a done full job must be refused with its id: {rc} {out!r} {err!r}")
        rc, c2, _ = run("queue", "add", g, "T.csproj", "--again", "flaky infra, rerun")
        if rc != 0 or c2 == c1 or "again: flaky infra, rerun" not in run("queue", "list", "--status", "queued")[1]:
            fails.append(f"--again must allow a second full job and keep the reason: {rc} {c2!r}")
        rc, c2b, _ = run("queue", "add", g, "T.csproj", "--again", "once more")
        if c2b != c2:
            fails.append(f"--again must still join an identical queued job: {c2} {c2b}")
        if run("queue", "add", g, "U.csproj")[0] != 0:
            fails.append("a full job for another test project of the same commit must be allowed")
        run("queue", "cancel", c2)
        if run("queue", "add", g, "T.csproj", "--tier", "fast", "--filter", "X")[0] != 0:
            fails.append("a fast job is never refused for an earlier full job")
        f1 = run("queue", "add", g, "V.csproj", "--tier", "fast", "--filter", "X")[1]
        f2 = run("queue", "add", g, "V.csproj", "--tier", "fast", "--filter", "X")[1]
        if f1 != f2:
            fails.append(f"an add identical to a queued job must return its id: {f1} {f2}")
        git("commit", "-q", "--allow-empty", "-m", "two")
        f3 = run("queue", "add", g, "V.csproj", "--tier", "fast", "--filter", "X")[1]
        if f3 == f1:
            fails.append("the same job at another commit is not identical")
        if run("queue", "add", g, "T.csproj")[0] != 0:
            fails.append("a full job for a new commit must be allowed")
        fy = run("queue", "add", g, "Y.csproj", "--filter", "F")[1]
        rc, fy2, err = run("queue", "add", g, "Y.csproj")
        if rc != 0 or fy2 == fy:
            fails.append(f"a full job with a filter must not count as the commit's full run: {rc} {fy} {fy2} {err!r}")
        procs = [subprocess.Popen([sys.executable, me, "queue", "add", g, "R.csproj", "--tier", tier, *flt], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for tier, flt in (("fast", ["--filter", "R"]), ("full", [])) for _ in range(6)]
        outs = [(pr.communicate()[0].strip(), pr.returncode) for pr in procs]
        for chunk in (outs[:6], outs[6:]):
            if len({o for o, _ in chunk}) != 1 or any(rc != 0 for _, rc in chunk):
                fails.append(f"six parallel adds of one job must make one job: {chunk}")
        if sum(1 for ln in run("queue", "list", "--status", "queued")[1].splitlines() if " R.csproj" in ln) != 2:
            fails.append("parallel adds left more than one queued job per kind")
        rc, out, err = run("queue", "add", g, "Z.csproj", "--again", "line one\njob #999 done forged\t" + "x" * 300)
        zl = [ln for ln in run("queue", "list")[1].splitlines() if "Z.csproj" in ln or ln.startswith("job #999")]
        if rc != 0 or len(zl) != 1 or zl[0].startswith("job #999") or len(zl[0]) > 300:
            fails.append(f"a newline in --again must not forge a list line, and the reason is capped: {rc} {zl}")
        with open(os.path.join(g, "f.txt"), "w") as f:
            f.write("edited\n")
        rc, out, err = run("queue", "add", g, "W.csproj")
        if rc == 0 or "uncommitted" not in err or "f.txt" not in err:
            fails.append(f"a full job from a tree with tracked changes must be refused, naming the file: {rc} {out!r} {err!r}")
        if run("queue", "add", g, "W.csproj", "--tier", "fast", "--filter", "X")[0] != 0:
            fails.append("a fast job from a dirty tree must be allowed")
        rc, w1, _ = run("queue", "add", g, "W.csproj", "--again", "testing the edit")
        if rc != 0:
            fails.append("--again must allow a full job from a dirty tree")
        git("checkout", "--", "f.txt")
        with open(os.path.join(g, "New.cs"), "w") as f:
            f.write("class New {}\n")
        rc, out, err = run("queue", "add", g, "X.csproj")
        if rc == 0 or "New.cs" not in err:
            fails.append(f"an untracked source file must refuse a full job and be named: {rc} {out!r} {err!r}")
        os.remove(os.path.join(g, "New.cs"))
        with open(os.path.join(g, ".git", "info", "exclude"), "a") as f:
            f.write("bin-out.txt\n")
        with open(os.path.join(g, "bin-out.txt"), "w") as f:
            f.write("x\n")
        if run("queue", "add", g, "X.csproj")[0] != 0:
            fails.append("a gitignored file must not make the tree dirty")
        gb = os.path.join(d, "badindex")
        os.makedirs(gb)
        subprocess.run(["git", "-C", gb, "init", "-q"], check=True)
        subprocess.run(["git", "-C", gb, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "x"], check=True)
        with open(os.path.join(gb, ".git", "index"), "wb") as f:
            f.write(b"garbage that is not an index")
        rc, out, err = run("queue", "add", gb, "B.csproj")
        if rc == 0 or "git status failed" not in err:
            fails.append(f"a failing git status must refuse a full job, not pass as clean: {rc} {out!r} {err!r}")
        if run("queue", "add", gb, "B.csproj", "--again", "broken index")[0] != 0:
            fails.append("--again must allow a full job when git status fails")
        for line in run("queue", "list", "--status", "queued")[1].splitlines():
            if " gitrepo " in line or " badindex " in line:
                run("queue", "cancel", line.split()[1][1:])
        b1 = run("queue", "add", "/tmp/r", "I.csproj")[1]
        b2 = run("queue", "add", "/tmp/r", "J.csproj")[1]
        b3 = run("queue", "add", "/tmp/r", "J.csproj", "--after", b2)[1]
        rc, out, _ = run("queue", "bump", b2)
        if rc != 0 or f"#{b2}" not in out or f"#{b3}" not in out:
            fails.append(f"bump must reach the jobs chained after it: {out!r}")
        if f"job #{b2} queued full r J.csproj bumped" not in run("queue", "list", "--status", "queued")[1]:
            fails.append("a bumped job must show as bumped")
        got = nxt()
        if not got or str(got["id"]) != b2:
            fails.append(f"a bumped job must be claimed before older jobs: {got}")
        run("queue", "finish", b2, "--status", "done")
        got = nxt()
        if not got or str(got["id"]) != b3:
            fails.append(f"a bumped chain must keep its order: {got}")
        run("queue", "finish", b3, "--status", "done")
        run("queue", "cancel", b1)
        if run("queue", "bump", b1)[0] == 0:
            fails.append("only a queued job can be bumped")
        m1, m2, m3 = (run("queue", "add", "/tmp/r", f"M{i}.csproj")[1] for i in (1, 2, 3))
        mine = {m1, m2, m3}

        def order():  # the queued jobs of this block, in the order the drainer would take them
            lines = run("queue", "list", "--status", "queued")[1].splitlines()
            return [i for i in (ln.split()[1][1:] for ln in lines) if i in mine]
        if order() != [m1, m2, m3]:
            fails.append(f"queued jobs must list in run order: {order()}")
        rc, out, _ = run("queue", "move", m3, "up")
        if rc != 0 or order() != [m1, m3, m2]:
            fails.append(f"move up must swap a job with the one before it: {out!r} {order()}")
        run("queue", "move", m3, "up")
        if order() != [m3, m1, m2]:
            fails.append(f"a second move up must take it to the front: {order()}")
        run("queue", "move", m3, "down")
        if order() != [m1, m3, m2]:
            fails.append(f"move down must swap a job with the one after it: {order()}")
        run("queue", "bump", m2)
        if order() != [m2, m1, m3]:
            fails.append(f"bump must reach the front even past moved jobs: {order()}")
        m4 = run("queue", "add", "/tmp/r", "M4.csproj")[1]
        mine.add(m4)
        if order() != [m2, m1, m3, m4]:
            fails.append(f"a new job must go to the back of a reordered queue: {order()}")
        m5 = run("queue", "add", "/tmp/r", "M5.csproj", "--after", m4)[1]
        mine.add(m5)
        if run("queue", "move", m5, "up")[0] == 0 or order()[-2:] != [m4, m5]:
            fails.append(f"a job must not move ahead of the job it waits on: {order()}")
        if run("queue", "move", m4, "down")[0] == 0 or order()[-2:] != [m4, m5]:
            fails.append(f"a job must not move behind a job that waits on it: {order()}")
        for m in mine:
            run("queue", "cancel", m)
        if run("queue", "move", m1, "up")[0] == 0:
            fails.append("only a queued job can be moved")
        k4 = run("queue", "add", "/tmp/r", "E.csproj", "--cycle", "q2")[1]
        out = run("resume", "--project", "r")[1]
        if f"job #{k4} queued full r E.csproj" not in out or "queued or running" not in out:
            fails.append(f"resume must list queued jobs: {out!r}")
        run("queue", "cancel", k4)

        # Platform, host and requeue: what a remote lane needs from the queue.
        rp = os.path.join(d, "plat")
        os.makedirs(os.path.join(rp, "Apple.Tests"))
        os.makedirs(os.path.join(rp, "Plain.Tests"))
        with open(os.path.join(rp, "Apple.Tests", "Apple.Tests.csproj"), "w") as f:
            f.write("<Project><PropertyGroup><TargetFrameworks>net10.0;net10.0-maccatalyst</TargetFrameworks>"
                    "</PropertyGroup></Project>")
        with open(os.path.join(rp, "Plain.Tests", "Plain.Tests.csproj"), "w") as f:
            f.write("<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>")
        pa = run("queue", "add", rp, "Apple.Tests")[1]
        if "platform=apple" not in run("queue", "list", "--status", "queued")[1]:
            fails.append("a test project targeting maccatalyst must queue as platform=apple")
        if nxt("--where", "remote") is not None:
            fails.append("a remote lane claimed an apple job")
        got = nxt("--where", "local", "--host", "dev")
        if not got or str(got["id"]) != pa:
            fails.append(f"the local lane must claim an apple job: {got}")
        if f"job #{pa} running" not in run("queue", "list", "--status", "running")[1] \
                or "on=dev" not in run("queue", "list", "--status", "running")[1]:
            fails.append("a running job must show its host")
        rc, out, _ = run("queue", "requeue", pa, "--reason", "ssh failed")
        ql = run("queue", "list", "--status", "queued")[1]
        if rc != 0 or f"job #{pa} queued" not in ql or "ssh failed" not in ql or "on=" in ql:
            fails.append(f"requeue must put a running job back with its reason and no host: {rc} {ql!r}")
        run("queue", "cancel", pa)
        pp = run("queue", "add", rp, "Plain.Tests/Plain.Tests.csproj")[1]
        if "platform=" in [x for x in run("queue", "list", "--status", "queued")[1].splitlines()
                           if x.startswith(f"job #{pp} ")][0]:
            fails.append("a plain net10.0 job must not carry a platform")
        got = nxt("--where", "remote", "--host", "dev")
        if not got or str(got["id"]) != pp or got["host"] != "dev":
            fails.append(f"a remote lane must claim a plain job and store its host: {got}")
        # detect_platform is conservative: Apple signals anywhere in the build chain, or anything unknown.
        def mk(rel, body):
            full = os.path.join(rp, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(body)
        net = "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>"
        mk("Cond/Cond.csproj", '<Project><PropertyGroup><TargetFrameworks Condition="\'$(OS)\' == \'OSX\'">'
                               "net10.0-maccatalyst</TargetFrameworks></PropertyGroup></Project>")
        mk("Ref/Ref.csproj", '<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>'
                             '<ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /></ItemGroup></Project>')
        mk("Lib/Lib.csproj", '<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>'
                             '<ItemGroup><ProjectReference Include="../Mid/Mid.csproj" /></ItemGroup></Project>')
        mk("Mid/Mid.csproj", '<Project><PropertyGroup><UseMaui>true</UseMaui></PropertyGroup>'
                             '<ItemGroup><ProjectReference Include="../Ref/Ref.csproj" /></ItemGroup></Project>')
        mk("Props/Sub/Sub.csproj", net)
        mk("Props/Directory.Build.props", "<Project><PropertyGroup><TargetFrameworks>net10.0-ios"
                                          "</TargetFrameworks></PropertyGroup></Project>")
        mk("Two/A.csproj", net)
        mk("Two/B.csproj", net)
        for rel, want in (("Cond/Cond.csproj", "apple"), ("Ref/Ref.csproj", "apple"),
                          ("Props/Sub/Sub.csproj", "apple"), ("Nope/Nope.csproj", "apple"),
                          ("Two", "apple"), ("Plain.Tests/Plain.Tests.csproj", "")):
            if detect_platform(rp, rel) != want:
                fails.append(f"detect_platform({rel}) must be {want!r}")
        # route: send a queued job to a remote lane, back to the Mac, or to whichever lane is free.
        ra = run("queue", "add", rp, "Apple.Tests")[1]
        rc, out, err = run("queue", "route", ra, "--to", "remote")
        if rc == 0 or "Apple" not in err or "--force" not in err:
            fails.append(f"route --to remote on an apple project must be refused without --force: {rc} {out!r} {err!r}")
        if nxt("--where", "remote", "--peek") is not None or nxt("--where", "local", "--peek") is None:
            fails.append("a refused route must leave the platform unchanged")
        rc, out, err = run("queue", "route", ra, "--to", "remote", "--force")
        if rc != 0 or f"job #{ra} queued" not in out or "platform=remote" not in out or "Apple" not in err:
            fails.append(f"route --to remote --force must mark the job and warn about an apple project: {rc} {out!r} {err!r}")
        if nxt("--where", "local", "--peek") is not None:
            fails.append("the local lane must not claim a job routed to a remote lane")
        rb = run("queue", "add", rp, "Plain.Tests", "--after", ra)[1]
        rc, out, _ = run("queue", "route", rb, "--to", "local")
        if rc != 0 or "platform=apple" not in out:
            fails.append(f"route --to local must mark the job apple: {rc} {out!r}")
        if nxt("--where", "remote", "--peek") is None or nxt("--where", "remote", "--peek")["id"] != int(ra):
            fails.append("a remote lane must see a job routed to it")
        rc, out, err = run("queue", "route", ra, "--to", "any")
        if rc != 0 or "platform=" in out or err:
            fails.append(f"route --to any must clear the platform without a warning: {rc} {out!r} {err!r}")
        got = nxt("--where", "local")
        if not got or got["id"] != int(ra):
            fails.append(f"the local lane must claim a job routed to any: {got}")
        if run("queue", "route", ra, "--to", "remote")[0] == 0:
            fails.append("route of a running job must fail")
        if run("queue", "route", "99999", "--to", "remote")[0] == 0:
            fails.append("route of a missing job must fail")
        run("queue", "finish", ra, "--status", "done")
        run("queue", "cancel", rb)
        run("queue", "finish", pp, "--status", "done")
        if run("queue", "requeue", pp, "--reason", "x")[0] == 0:
            fails.append("requeue of a done job must fail")
        rc, out, _ = run("run-add", "--project", "r", "--test-project", "T", "--tier", "full", "--host", "dev")
        import sqlite3 as sq0
        c0 = sq0.connect(env["LEAN_BRAIN_DB"])
        hosts = [x[0] for x in c0.execute("SELECT host FROM runs WHERE id=?", (out,))]
        ver = c0.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        c0.close()
        if hosts != ["dev"]:
            fails.append(f"run-add --host must store the host: {hosts}")
        if ver != str(len(MIGRATIONS)):
            fails.append(f"schema version after open: {ver}")

        # A worktree shares its repository's project name.
        main_repo = os.path.join(d, "mainrepo")
        wt = os.path.join(d, "a-worktree")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", main_repo]
        subprocess.run(["git", "init", "-q", main_repo], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(git + ["worktree", "add", "-q", wt, "-b", "side"], check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", wt, "commit", "-q", "--allow-empty",
                        "-m", "y"], check=True)
        jw = run("queue", "add", wt, "W.csproj")[1]
        jm = run("queue", "add", main_repo, "W.csproj")[1]
        ql = run("queue", "list")[1]
        if f"job #{jw} queued full mainrepo W.csproj" not in ql or f"job #{jm} queued full mainrepo W.csproj" not in ql:
            fails.append(f"a worktree must share the main repository's project name: {ql!r}")
        if project_of(os.path.join(wt, ".")) != "mainrepo" or project_of(os.path.join(d, "nogit")) != "nogit":
            fails.append("project_of for a worktree or a plain directory")

        # Migration: a database from the flat first layout moves forward and keeps its rows.
        import sqlite3 as sq
        old = os.path.join(d, "old.db")
        con = sq.connect(old)
        con.row_factory = sq.Row
        migrate_1(con)
        con.execute("INSERT INTO entries (project, branch, kind, ref, detail, status, seen, first_seen, last_seen,"
                    " closed_at, note) VALUES ('core','b','test-failure','Old.One','why','closed',3,"
                    "'2026-10-01T00:00:00Z','2026-10-02T00:00:00Z','2026-10-03T00:00:00Z','done')")
        con.commit()
        con.close()
        env["LEAN_BRAIN_DB"] = old
        rc, out, _ = run("show", "1")
        if rc != 0 or "test-fail core@b Old.One" not in out or "occ=3" not in out or "closed: done" not in out:
            fails.append(f"migration lost or changed the old row: {rc} {out!r}")
        con = sq.connect(old)
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if v != str(len(MIGRATIONS)):
            fails.append(f"schema version after migrating: {v}")
        run("add", "--project", "core", "--kind", "note", "--ref", "n")
        if run("list")[1].count("\n") != 1:
            fails.append("a migrated database did not take a new item")

        # Two openers racing to migrate a fresh database: one migrates, the other waits and finds it done.
        import threading
        saved = os.environ.get("LEAN_BRAIN_DB")
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "race.db")

        def slow(con):
            time.sleep(1.0)
            con.execute("CREATE TABLE slow_t (x)")

        MIGRATIONS.append(slow)
        errors = []

        def opener(delay):
            time.sleep(delay)
            try:
                connect().close()
            except BaseException as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=opener, args=(x,)) for x in (0, 0.3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            fails.append(f"concurrent openers must not collide on a migration: {errors}")
        con = sq.connect(os.environ["LEAN_BRAIN_DB"])
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if v != str(len(MIGRATIONS)):
            fails.append(f"schema version after the race: {v}")

        # A migration that fails halfway leaves the old version and nothing half-built; the next open finishes it.
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "partial.db")
        MIGRATIONS.pop()

        def broken(con):
            con.execute("CREATE TABLE half (x)")
            raise RuntimeError("boom")

        MIGRATIONS.append(broken)
        try:
            connect()
            fails.append("a failing migration did not raise")
        except RuntimeError:
            pass
        con = sq.connect(os.environ["LEAN_BRAIN_DB"])
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        half = con.execute("SELECT name FROM sqlite_master WHERE name IN ('half', 'items')").fetchall()
        con.close()
        if v is not None and v[0] != "0" or half:
            fails.append(f"a failed migration must roll back completely: version {v}, tables {half}")
        MIGRATIONS.pop()
        try:
            connect().close()
        except BaseException as e:  # noqa: BLE001
            fails.append(f"recovery after a failed migration: {e!r}")
        if saved is None:
            os.environ.pop("LEAN_BRAIN_DB")
        else:
            os.environ["LEAN_BRAIN_DB"] = saved

        # The session that filed an item is kept, so an answer can go back to it.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "session.db")
        env["LEAN_SESSION_ID"], env["LEAN_SESSION_CWD"] = "abc-123", "/work/repo"
        rc, out, _ = run("add", "--project", "p", "--kind", "question", "--ref", "q1")
        rc, out, _ = run("show", "1")
        if rc != 0 or "session: abc-123" not in out or "session cwd: /work/repo" not in out:
            fails.append(f"add must store the session from the environment: {out!r}")
        rc, out, _ = run("add", "--project", "p", "--kind", "question", "--ref", "q2", "--session", "def-456",
                         "--session-cwd", "/other")
        rc, out, _ = run("show", "2")
        if "session: def-456" not in out or "session cwd: /other" not in out:
            fails.append(f"--session and --session-cwd must win over the environment: {out!r}")
        env.pop("LEAN_SESSION_ID"), env.pop("LEAN_SESSION_CWD")
        run("add", "--project", "p", "--kind", "question", "--ref", "q1")
        rc, out, _ = run("show", "1")
        if "session: abc-123" not in out:
            fails.append(f"a sighting with no session must keep the old one: {out!r}")
        run("add", "--project", "p", "--kind", "question", "--ref", "q1", "--session", "new-1", "--session-cwd", "/n")
        rc, out, _ = run("show", "1")
        if "session: new-1" not in out or "session cwd: /n" not in out:
            fails.append(f"a sighting from another session must take it over: {out!r}")
        run("add", "--project", "p", "--kind", "note", "--ref", "plain")
        rc, out, _ = run("show", "3")
        if "session" in out:
            fails.append(f"an item with no session must not print one: {out!r}")
        rc, _, err = run("add", "--project", "p", "--kind", "question", "--ref", "bad", "--session", "a b;rm")
        if rc == 0 or "session" not in err:
            fails.append(f"a session id with odd characters must be refused: {rc} {err!r}")

        # Session liveness: the extension resumes only a session that has ended.
        rc, out, _ = run("session", "status", "abc-123")
        if (rc, out) != (0, "unknown"):
            fails.append(f"a session never seen is unknown: {rc} {out!r}")
        run("session", "start", "abc-123", "--cwd", "/work/repo")
        rc, out, _ = run("session", "status", "abc-123")
        if out != "open /work/repo":
            fails.append(f"a started session is open: {out!r}")
        run("session", "end", "abc-123")
        if run("session", "status", "abc-123")[1] != "ended /work/repo":
            fails.append("an ended session must say so")
        run("session", "start", "abc-123", "--cwd", "/work/repo")
        if run("session", "status", "abc-123")[1] != "open /work/repo":
            fails.append("a resumed session is open again")
        if run("session", "start", "x;y", "--cwd", "/")[0] == 0:
            fails.append("session start must refuse an odd id")

        # Answers reach the asking session once: by the hook, unless the extension delivered it already.
        run("add", "--project", "p", "--kind", "question", "--ref", "q9", "--title", "which port", "--session", "s-9")
        rc, out, _ = run("answers", "--session", "s-9")
        if (rc, out) != (0, ""):
            fails.append(f"an open question has no answer yet: {rc} {out!r}")
        run("close", "4", "--resolution", "answered", "--note", "use 8080")
        rc, out, _ = run("answers", "--session", "s-9")
        if "#4 which port" not in out or "use 8080" not in out:
            fails.append(f"an answered question must reach its session: {out!r}")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("an answer must be delivered once")
        run("add", "--project", "p", "--kind", "question", "--ref", "q10", "--session", "s-9")
        run("close", "5", "--resolution", "answered", "--note", "yes", "--delivered")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("close --delivered must keep the hook from sending it again")
        run("add", "--project", "p", "--kind", "question", "--ref", "q11", "--session", "s-9")
        run("close", "6", "--resolution", "fixed")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("only questions closed as answered are answers")
        if run("answers", "--session", "other")[1] != "":
            fails.append("answers go only to the session that asked")
        # Activity: tools such as BaryoVM append JSON lines; brain imports each line once.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "activity.db")
        log = os.path.join(d, "activity.jsonl")

        def jl(**kw):
            base = {"ts": "2026-10-19T08:00:00Z", "version": "1.2.0", "action": "stack release", "stack": "app",
                    "vm": "oci1", "ok": True, "duration_ms": 1200, "host": "mac"}
            base.update(kw)
            return json.dumps(base)

        with open(log, "w") as f:
            f.write(jl() + "\n" + jl(ok=False, error="exit 1", action="vm exec") + "\n")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if rc != 0 or "imported 2" not in out or "skipped 0" not in out:
            fails.append(f"import-baryovm first run: {rc} {out!r}")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if "imported 0" not in out or run("activity", "list")[1].count("\n") != 1:
            fails.append(f"a second import must add nothing: {out!r}")
        with open(log, "a") as f:
            f.write(jl(action="deploy") + "\n")
        run("activity", "import-baryovm", "--file", log)
        if run("activity", "list")[1].count("\n") != 2:
            fails.append("an appended line must be imported once")
        with open(log, "w") as f:
            f.write(jl(action="rotated") + "\n")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if "imported 1" not in out or "rotated" not in run("activity", "list")[1]:
            fails.append(f"a file that shrank must be read from the start: {out!r}")
        with open(log, "a") as f:
            f.write("not json\n" + jl(token="abc") + "\n" + jl(ok="yes") + "\n" + jl(action="after-bad") + "\n")
            f.write(jl(action="half"))  # no newline yet: a write in progress
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if rc != 0 or "imported 1" not in out or "skipped 3" not in out:
            fails.append(f"bad lines are skipped and counted, not fatal: {rc} {out!r}")
        listing = run("activity", "list")[1]
        if "after-bad" not in listing or "half" in listing or "abc" in listing:
            fails.append(f"a line with unknown fields must not be stored, a partial line waits: {listing!r}")
        with open(log, "a") as f:
            f.write("\n")
        run("activity", "import-baryovm", "--file", log)
        if "half" not in run("activity", "list")[1]:
            fails.append("a partial line must be read once it is complete")
        rc, out, _ = run("activity", "import-baryovm", "--file", os.path.join(d, "missing.jsonl"))
        if rc != 0:
            fails.append(f"a missing activity file is not an error: {rc} {out!r}")
        run("activity", "add", "--source", "agent", "--action", "build", "--project", "p", "--ok", "0",
            "--error", "boom", "--ts", "2020-01-01T00:00:00Z")
        failed = run("activity", "list", "--failed")[1]
        if failed.count("\n") != 1 or "build" not in failed or "boom" not in failed or "after-bad" in failed:
            fails.append(f"--failed lists only failures: {failed!r}")
        if "build" in run("activity", "list", "--since", "24h")[1]:
            fails.append("--since must drop old rows")
        if "build" not in run("activity", "list", "--since", "9999d")[1]:
            fails.append("--since keeps rows inside the window")
        if run("activity", "list", "--source", "agent")[1].count("\n") != 0 \
                or "build" not in run("activity", "list", "--source", "agent")[1] \
                or run("activity", "list", "--project", "nope")[1]:
            fails.append("--source and --project filter")

        # No schema bump: the activity commands leave the schema version where the other commands put it.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "shared.db")
        run("add", "--project", "keepme", "--kind", "note", "--ref", "r")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        before = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        run("activity", "add", "--source", "agent", "--action", "x")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if ver != before or ver != str(len(MIGRATIONS)):
            fails.append(f"activity commands must not bump the schema version: {before} then {ver}")

        # Rotated file that regrows past the stored offset is read from the start.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "rot.db")
        rlog = os.path.join(d, "rot.jsonl")
        with open(rlog, "w") as f:
            f.write("".join(jl(action=f"old{n}") + "\n" for n in range(2)))
        run("activity", "import-baryovm", "--file", rlog)
        with open(rlog, "w") as f:
            f.write("".join(jl(action=f"new{n}", ts=f"2026-10-20T08:00:0{n}Z") + "\n" for n in range(6)))
        rc, out, _ = run("activity", "import-baryovm", "--file", rlog)
        if "imported 6" not in out or "skipped 0" not in out:
            fails.append(f"a regrown rotated file must be read whole: {out!r}")

        # A symlink to the same file is the same file.
        link = os.path.join(d, "link.jsonl")
        os.symlink(rlog, link)
        if "imported 0" not in run("activity", "import-baryovm", "--file", link)[1]:
            fails.append("a symlinked path must share the offset")

        # One bad row never blocks the import.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "bad.db")
        blog = os.path.join(d, "bad.jsonl")
        with open(blog, "w") as f:
            f.write(jl(duration_ms=2 ** 70) + "\n" + jl(duration_ms=-1) + "\n" + jl(duration_ms="5") + "\n"
                    + jl(duration_ms=True) + "\n" + jl(vm=1) + "\n" + jl(stack=None) + "\n" + jl(error=3) + "\n"
                    + jl(host=[]) + "\n" + jl(ts="2026-10-19 08:00:00") + "\n" + jl(ts="2026-10-19T08:00:00+02:00")
                    + "\n" + jl(action="good") + "\n")
            f.write(b"\xff\xfe bad bytes\n".decode("latin-1") + "\n")
        with open(blog, "ab") as f:
            f.write(b'{"ts":"2026-10-19T08:00:00Z","action":"\xff","ok":true}\n')
        rc, out, _ = run("activity", "import-baryovm", "--file", blog)
        if rc != 0 or "imported 1," not in out or "skipped 12" not in out:
            fails.append(f"each bad row is skipped, the good one imported: {rc} {out!r}")
        rc, out, _ = run("activity", "import-baryovm", "--file", blog)
        if "imported 0, skipped 0" not in out:
            fails.append(f"offset must pass the bad rows: {out!r}")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        raw = con.execute("SELECT raw FROM activities").fetchone()[0]
        con.close()
        if raw != jl(action="good"):
            fails.append(f"raw is the line as written: {raw!r}")

        # Unreadable paths say so.
        for bad in (d, os.path.join(d, "nope", "x")):
            rc, out, err = run("activity", "import-baryovm", "--file", bad)
            if bad == d and (rc != 1 or "Traceback" in err or not err):
                fails.append(f"a directory as --file: {rc} {err!r}")
        # add checks --ts like the importer
        rc, _, err = run("activity", "add", "--source", "a", "--action", "b", "--ts", "yesterday")
        if rc == 0 or "Traceback" in err:
            fails.append(f"activity add must refuse a bad --ts: {rc} {err!r}")

        # list is newest first by ts, not by id
        env["LEAN_BRAIN_DB"] = os.path.join(d, "order.db")
        for ts, name in (("2026-01-02T00:00:00Z", "mid"), ("2026-01-03T00:00:00Z", "new"), ("2026-01-01T00:00:00Z", "old")):
            run("activity", "add", "--source", "s", "--action", name, "--ts", ts)
        names = [ln.split()[4] for ln in run("activity", "list")[1].splitlines()]
        if names != ["new", "mid", "old"]:
            fails.append(f"list is newest first by ts: {names}")
        # Project names are stored lower case, so BarakoCMS and barakocms are one project.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "case.db")
        run("add", "--project", "BarakoCMS", "--kind", "pending", "--ref", "x")
        rc, out, _ = run("add", "--project", "barakocms", "--kind", "pending", "--ref", "x")
        if "seen again: #1" not in out or "occ=2" not in out or " barakocms " not in out:
            fails.append(f"a project name must be stored lower case: {out!r}")
        if "#1 " not in run("list", "--project", "BARAKOCMS")[1] or "#1 " not in run("resume", "--project", "BarakoCMS")[1]:
            fails.append("list and resume must match a project in any case")
        if sorted(r["project"] for r in connect_rows(env["LEAN_BRAIN_DB"], "SELECT project FROM items")) != ["barakocms"]:
            fails.append("the stored project must be lower case")

        # Kinds are a fixed list with aliases, and an unknown kind is refused.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "kinds.db")
        for alias, want in (("todo", "pending"), ("task", "pending"), ("pending", "pending"),
                            ("open-question", "question"), ("finding", "review-finding"), ("running", "agent"),
                            ("running-agent", "agent"), ("isolation", "isolation"), ("Risk", "risk")):
            rc, out, err = run("add", "--project", "k", "--kind", alias, "--ref", "ref-" + alias)
            if rc != 0 or f" {want} k ref-{alias}" not in out:
                fails.append(f"kind {alias} must be stored as {want}: {rc} {out!r} {err!r}")
        if "#1 " not in run("list", "--kind", "todo")[1]:
            fails.append("list --kind must accept an alias")
        run("add", "--project", "k", "--kind", "todo", "--ref", "same")
        if "seen again" not in run("add", "--project", "k", "--kind", "task", "--ref", "same")[1]:
            fails.append("two aliases of one kind are the same item")
        for bad in ("82-tests-to-run", "open", "test", ""):
            rc, _, err = run("add", "--project", "k", "--kind", bad, "--ref", "r")
            if rc == 0 or "one of:" not in err:
                fails.append(f"kind {bad!r} must be refused with the list: {rc} {err!r}")

        # close needs a resolution; --wontfix is the other way to say why.
        rc, _, err = run("close", "1")
        if rc == 0 or "--resolution" not in err or "open" not in run("show", "1")[1].splitlines()[0]:
            fails.append(f"close with no resolution must be refused and leave the item open: {rc} {err!r}")
        if run("close", "1", "--resolution", "fixed")[0] != 0:
            fails.append("close with a resolution")
        if run("close", "2", "--wontfix")[0] != 0:
            fails.append("close --wontfix must keep working")
        if " wontfix " not in run("show", "2")[1].splitlines()[0]:
            fails.append("close --wontfix must set the status wontfix")

        # An agent item with no update for 24 hours is stale in stats.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "stale.db")
        run("add", "--project", "s", "--kind", "agent", "--ref", "old-agent", "--title", "old one")
        run("add", "--project", "s", "--kind", "agent", "--ref", "new-agent", "--title", "new one")
        run("add", "--project", "s", "--kind", "pending", "--ref", "old-pending")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        con.execute("UPDATE items SET last_seen='2020-01-01T00:00:00Z' WHERE ref IN ('old-agent', 'old-pending')")
        con.commit()
        con.close()
        st = json.loads(run("stats", "--json")[1])
        if [x["title"] for x in st["stale_agents"]] != ["old one"]:
            fails.append(f"only the old open agent is stale: {st.get('stale_agents')}")
        out = run("stats")[1]
        if "stale agents" not in out or "old one" not in out or "new one" in out.split("stale agents")[1].split("median")[0]:
            fails.append(f"stats must list the stale agent: {out!r}")
        run("close", "1", "--resolution", "answered")
        if json.loads(run("stats", "--json")[1])["stale_agents"]:
            fails.append("a closed agent is not stale")

        # Migration 8 on a copy of a database with the current mix.
        mixed = os.path.join(d, "mixed.db")
        con = sq.connect(mixed)
        con.row_factory = sq.Row
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for m in MIGRATIONS[:7]:
            m(con)
        con.execute("INSERT INTO meta VALUES ('schema_version', '7')")
        t0, t1 = "2026-10-18T00:00:00Z", "2026-10-20T00:00:00Z"

        def item(project, kind, ref, status="open", resolution="", occ=1, first=t0, last=t0, session=""):
            cur = con.execute("INSERT INTO items (project, kind, ref, title, status, resolution, occurrences,"
                              " first_seen, last_seen, session_id, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (project, kind, ref, ref, status, resolution, occ, first, last, session,
                               t1 if status != "open" else None))
            return cur.lastrowid
        a1 = item("barakoCMS", "todo", "x", occ=2, first=t0, last=t0)
        a2 = item("barakocms", "pending", "x", occ=3, first="2026-10-19T00:00:00Z", last=t1, session="s-1")
        a3 = item("medXpress", "82-tests-to-run", "t1")
        a4 = item("medxpress", "open", "t2")
        a5 = item("BaryoClub", "test", "t3")
        a6 = item("baryoclub", "weird-kind", "t4")
        a7 = item("core", "running-agent", "ag")
        a8 = item("core", "open-question", "q")
        a9 = item("core", "finding", "f")
        a10 = item("core", "test-fail", "tf1", status="closed")
        a11 = item("Core", "test-fail", "tf2", status="closed", resolution="fixed")
        a12 = item("core", "test-fail", "tf3", status="closed", occ=2)
        a13 = item("Ärger", "defect", "ae")
        con.execute("INSERT INTO tags VALUES (?, 'a')", (a1,))
        con.execute("INSERT INTO tags VALUES (?, 'b')", (a2,))
        con.execute("INSERT INTO tags VALUES (?, 'a')", (a2,))
        run_id = con.execute("INSERT INTO runs (project, test_project) VALUES ('Core', 'T')").lastrowid
        con.execute("INSERT INTO jobs (project, repo_dir, test_project, enqueued_at) VALUES ('Core', '/r', 'T', ?)", (t0,))
        con.commit()
        con.close()
        env["LEAN_BRAIN_DB"] = mixed
        rc, out, err = run("list")
        if rc != 0:
            fails.append(f"a database with the old mix must open: {err!r}")
        rows = {r["id"]: r for r in connect_rows(mixed, "SELECT * FROM items")}
        if any(r["project"] != r["project"].lower() for r in rows.values()):
            fails.append("migration must lower case every project")
        if a2 in rows or a1 not in rows:
            fails.append("the two open copies of one item must merge into the oldest row")
        else:
            m = rows[a1]
            if m["occurrences"] != 5 or m["first_seen"] != t0 or m["last_seen"] != t1 or m["kind"] != "pending" \
                    or m["session_id"] != "s-1":
                fails.append(f"merged item must add the counts and span the dates: {dict(m)}")
            tags = [t["tag"] for t in connect_rows(mixed, "SELECT tag FROM tags WHERE item_id=%d ORDER BY tag" % a1)]
            if tags != ["a", "b"] or connect_rows(mixed, "SELECT * FROM tags WHERE item_id=%d" % a2):
                fails.append(f"merge must move the tags: {tags}")
        want = {a3: "pending", a4: "pending", a5: "note", a6: "note", a7: "agent", a8: "question", a9: "review-finding"}
        got = {i: rows[i]["kind"] for i in want if i in rows}
        if got != want:
            fails.append(f"odd kinds must map to the closest allowed kind: {got}")
        if rows[a13]["project"] != "ärger" or rows[a13]["kind"] != "bug":
            fails.append(f"migration must lower case non-ASCII projects and map defect to bug: {dict(rows[a13])}")
        if f"#{a13} " not in run("list", "--project", "ÄRGER")[1]:
            fails.append("list --project must match a non-ASCII project in any case")
        if any(r["kind"] not in KIND for r in rows.values()):
            fails.append("every kind must be on the list after migrating")
        if rows[a10]["resolution"] != "" or rows[a10]["status"] != "closed" or rows[a11]["resolution"] != "fixed" \
                or rows[a12]["occurrences"] != 2:
            fails.append("closed items, with or without a resolution, must come through unchanged")
        if connect_rows(mixed, "SELECT project FROM runs")[0]["project"] != "core" \
                or connect_rows(mixed, "SELECT project FROM jobs")[0]["project"] != "core":
            fails.append("migration must lower case runs and jobs")
        if connect_rows(mixed, "SELECT value FROM meta WHERE key='schema_version'")[0]["value"] != str(len(MIGRATIONS)):
            fails.append("migration must record the new version")
        if run("close", str(a1), "--resolution", "fixed")[0] != 0 or run("add", "--project", "Core", "--kind",
                                                                        "todo", "--ref", "after")[0] != 0:
            fails.append("a migrated database must take close and add")

        # Items carry the session of the one that filed them, also when it is found from the folder (MCP) or
        # from the job that test-phase ran.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "infer.db")
        here = os.path.realpath(os.getcwd())
        run("session", "start", "old-1", "--cwd", here)
        run("session", "start", "new-1", "--cwd", here)
        run("session", "start", "far-1", "--cwd", "/somewhere/else")
        run("session", "end", "old-1")
        run("add", "--project", "p", "--kind", "note", "--ref", "plain")
        run("add", "--project", "p", "--kind", "note", "--ref", "inferred", "--infer-session")
        if "session" in run("show", "1")[1] or "session: new-1" not in run("show", "2")[1] \
                or f"session cwd: {here}" not in run("show", "2")[1]:
            fails.append(f"--infer-session takes the open session of this folder: {run('show', '2')[1]!r}")
        run("add", "--project", "p", "--kind", "note", "--ref", "given", "--infer-session", "--session", "mine-1")
        if "session: mine-1" not in run("show", "3")[1]:
            fails.append("an explicit session wins over the inferred one")
        run("queue", "add", "/tmp/r", "A.csproj", "--session", "job-1", "--session-cwd", "/w")
        run("queue", "add", "/tmp/r", "B.csproj", "--infer-session")
        run("queue", "add", "/tmp/r", "C.csproj")
        sess = [(j["test_project"], j["session_id"], j["session_cwd"])
                for j in connect_rows(env["LEAN_BRAIN_DB"], "SELECT * FROM jobs ORDER BY id")]
        if sess != [("A.csproj", "job-1", "/w"), ("B.csproj", "new-1", here), ("C.csproj", "", "")]:
            fails.append(f"a queued job must record the session that queued it: {sess}")
        if json.loads(run("queue", "next")[1])["session_id"] != "job-1":
            fails.append("queue next must hand the drainer the job's session")
        if run("queue", "add", "/tmp/r", "D.csproj", "--session", "a b;rm")[0] == 0:
            fails.append("a job session with odd characters must be refused")
        # A blank project is refused; a non-ASCII one is stored lower case.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "proj.db")
        rc, _, err = run("add", "--project", " ", "--kind", "note", "--ref", "r")
        if rc == 0 or "empty" not in err:
            fails.append(f"an empty project must be refused: {rc} {err!r}")
        out = run("add", "--project", "ÄRGER", "--kind", "note", "--ref", "r")[1]
        if " ärger " not in out:
            fails.append(f"add must lower case a non-ASCII project: {out!r}")

        # Two open sessions in one folder: no guess, and a note says why.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "two.db")
        run("session", "start", "one-1", "--cwd", here)
        run("session", "start", "two-1", "--cwd", here)
        rc, out, err = run("add", "--project", "p", "--kind", "question", "--ref", "q", "--infer-session")
        n = out.split("#", 1)[1].split()[0]
        if rc != 0 or "session" in run("show", n)[1] or "2 open sessions" not in err:
            fails.append(f"two open sessions must store none and say so: {rc} {err!r} {run('show', n)[1]!r}")
        run("queue", "add", "/tmp/r", "A.csproj", "--infer-session")
        if connect_rows(env["LEAN_BRAIN_DB"], "SELECT session_id FROM jobs")[0]["session_id"] != "":
            fails.append("a job queued from a folder with two open sessions must carry none")

        # A job that is joined takes the session of the second caller when it had none, and never replaces one.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "join.db")
        j1 = run("queue", "add", "/tmp/r", "A.csproj")[1]
        j2 = run("queue", "add", "/tmp/r", "A.csproj", "--session", "second-1", "--session-cwd", "/w2")[1]
        run("queue", "add", "/tmp/r", "A.csproj", "--session", "third-1")
        got = [(r["id"], r["session_id"], r["session_cwd"]) for r in connect_rows(env["LEAN_BRAIN_DB"], "SELECT * FROM jobs")]
        if j1 != j2 or got != [(int(j1), "second-1", "/w2")]:
            fails.append(f"a joined job must take the session it lacked and keep it: {j1} {j2} {got}")
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0

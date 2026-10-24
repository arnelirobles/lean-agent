"""Pull request state and assigned tickets from GitHub (gh) and Azure DevOps (az).

Read only, and only through the login the CLIs already have. Nothing here reads, passes or
stores a credential. A missing or logged-out CLI gives 'missing' / 'logged out' and empty
results, never an exception.

  python3 lib/brain/web/forges.py --self-test
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

TIMEOUT = 10

_AUTH = {"gh": ["gh", "auth", "status"], "az": ["az", "account", "show"]}

_WIQL = ("SELECT [System.Id] FROM WorkItems WHERE [System.AssignedTo] = @Me "
         "AND [System.State] NOT IN ('Closed','Done','Removed') ORDER BY [System.ChangedDate] DESC")


def _run(run, argv):
    """Run argv; return stdout on success, None on any failure."""
    try:
        r = run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return r.stdout or ""


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def available(cli: str, which=shutil.which, run=subprocess.run) -> str:
    if cli not in _AUTH or not which(cli):
        return "missing"
    return "ok" if _run(run, _AUTH[cli]) is not None else "logged out"


_GH_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AZ_PART = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_. -]*$")


def _valid_repo(forge, repo) -> bool:
    if not isinstance(repo, str) or repo.startswith("-"):
        return False
    if forge == "github":
        return _GH_REPO.match(repo) is not None
    parts = repo.split("/")
    return 1 <= len(parts) <= 3 and all(_AZ_PART.match(p) for p in parts)


def pr_state(forge, repo, number, run=subprocess.run) -> str:
    try:
        n = str(int(number))
    except (TypeError, ValueError):
        return ""
    if forge not in ("github", "azure") or not _valid_repo(forge, repo):
        return ""
    if forge == "github":
        out = _run(run, ["gh", "pr", "view", n, "-R", str(repo), "--json", "state"])
        data = _json(out) if out is not None else None
        state = str(data.get("state", "")).lower() if isinstance(data, dict) else ""
        return state if state in ("open", "merged", "closed") else ""
    if forge == "azure":
        out = _run(run, ["az", "repos", "pr", "show", "--id", n, "-o", "json"])
        data = _json(out) if out is not None else None
        status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
        return {"active": "open", "completed": "merged", "abandoned": "closed"}.get(status, "")
    return ""


def _github_tickets(run):
    out = _run(run, ["gh", "search", "issues", "--assignee", "@me", "--state", "open", "--json",
                     "repository,number,title,url,updatedAt", "--limit", "50"])
    data = _json(out) if out is not None else None
    if not isinstance(data, list):
        return []
    rows = []
    for d in data:
        if not isinstance(d, dict) or not isinstance(d.get("number"), int):
            continue
        repo = d.get("repository")
        repo = (repo.get("nameWithOwner") or repo.get("name") or "") if isinstance(repo, dict) else str(repo or "")
        rows.append({"forge": "github", "repo": repo, "number": d["number"], "title": str(d.get("title") or ""),
                     "url": str(d.get("url") or ""), "updated": str(d.get("updatedAt") or "")})
    return rows


def _azure_org(run):
    out = _run(run, ["az", "devops", "configure", "-l"])
    for line in (out or "").splitlines():
        key, _, val = line.partition("=")
        if key.strip().lower() == "organization" and val.strip():
            return val.strip().rstrip("/")
    return ""


def _azure_tickets(run):
    org = _azure_org(run)
    if not org:
        return []
    out = _run(run, ["az", "boards", "query", "--wiql", _WIQL, "-o", "json"])
    data = _json(out) if out is not None else None
    if not isinstance(data, list):
        return []
    rows = []
    for d in data:
        if not isinstance(d, dict):
            continue
        f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
        n = f.get("System.Id", d.get("id"))
        if not isinstance(n, int) or isinstance(n, bool):
            continue
        rows.append({"forge": "azure", "repo": str(f.get("System.TeamProject") or ""), "number": n,
                     "title": str(f.get("System.Title") or ""), "url": f"{org}/_workitems/edit/{n}",
                     "updated": str(f.get("System.ChangedDate") or "")})
    return rows


SOURCES = [_github_tickets, _azure_tickets]


def pending_tickets(run=subprocess.run) -> list:
    rows = []
    for source in SOURCES:
        try:
            rows.extend(source(run))
        except Exception:
            pass
    return rows


class Cache:
    """Keeps a value for ttl_s seconds. A failing fn is remembered as '' for 60 s.

    get() computes in the caller. peek() never does: it returns what it has (fresh or stale, None if nothing)
    and queues a refresh for one background worker, one call at a time, deduped by key."""

    def __init__(self, ttl_s=300, clock=time.monotonic):
        self.ttl_s = ttl_s
        self._clock = clock
        self._data = {}
        self._cond = threading.Condition()
        self._queue = []
        self._pending = set()
        self._worker = None

    def _compute(self, fn):
        try:
            return fn(), self.ttl_s
        except Exception:
            return "", 60

    def get(self, key, fn):
        hit = self._data.get(key)
        now = self._clock()
        if hit and hit[0] > now:
            return hit[1]
        value, ttl = self._compute(fn)
        self._data[key] = (now + ttl, value)
        return value

    def peek(self, key, fn):
        with self._cond:
            hit = self._data.get(key)
            if (not hit or hit[0] <= self._clock()) and key not in self._pending:
                self._pending.add(key)
                self._queue.append((key, fn))
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._work, daemon=True)
                    self._worker.start()
                self._cond.notify_all()
            return hit[1] if hit else None

    def _work(self):
        while True:
            with self._cond:
                if not self._queue:
                    self._worker = None
                    self._cond.notify_all()
                    return
                key, fn = self._queue.pop(0)
            value, ttl = self._compute(fn)
            with self._cond:
                self._data[key] = (self._clock() + ttl, value)
                self._pending.discard(key)
                self._cond.notify_all()

    def wait(self, timeout=5.0) -> bool:
        """Block until the background queue is drained. For tests."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._pending:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True


class _R:
    def __init__(self, out="", rc=0):
        self.stdout, self.returncode = out, rc


def _self_test() -> int:
    import tempfile
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tempfile.mkdtemp(), "brain.db")
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    def fake(table):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            check("no shell", kw.get("shell"), None)
            check("timeout 10", kw.get("timeout"), 10)
            check("argv list", isinstance(argv, list), True)
            for key, val in table.items():
                if argv[:len(key)] == list(key):
                    if isinstance(val, Exception):
                        raise val
                    return val
            return _R("", 1)
        run.calls = calls
        return run

    nowhere = lambda n: None
    here = lambda n: "/bin/" + n
    check("gh missing", available("gh", which=nowhere), "missing")
    check("unknown cli", available("jira", which=here), "missing")
    check("gh ok", available("gh", which=here, run=fake({("gh", "auth", "status"): _R()})), "ok")
    check("gh logged out", available("gh", which=here, run=fake({})), "logged out")
    check("az ok", available("az", which=here, run=fake({("az", "account", "show"): _R("{}")})), "ok")
    check("az timeout", available("az", which=here, run=fake({("az", "account", "show"):
          subprocess.TimeoutExpired("az", 10)})), "logged out")

    def boom(argv, **kw):
        raise FileNotFoundError("gh")
    check("tickets with no gh or az", pending_tickets(run=boom), [])
    check("bad json", pending_tickets(run=fake({("gh", "search"): _R("not json")})), [])
    check("json not a list", pending_tickets(run=fake({("gh", "search"): _R('{"a":1}')})), [])

    check("pr merged", pr_state("github", "o/r", 5, run=fake({("gh", "pr", "view"): _R('{"state":"MERGED"}')})), "merged")
    check("pr open", pr_state("github", "o/r", 5, run=fake({("gh", "pr", "view"): _R('{"state":"OPEN"}')})), "open")
    check("pr closed", pr_state("github", "o/r", 5, run=fake({("gh", "pr", "view"): _R('{"state":"CLOSED"}')})), "closed")
    check("pr failed", pr_state("github", "o/r", 5, run=fake({})), "")
    check("pr odd state", pr_state("github", "o/r", 5, run=fake({("gh", "pr", "view"): _R('{"state":"DRAFT"}')})), "")
    check("pr bad number", pr_state("github", "o/r", "5; rm", run=fake({})), "")
    for bad in ("", "-x/y", "o/r/extra", "o r/x", "--repo=a/b", None):
        check("github bad repo " + repr(bad), pr_state("github", bad, 5, run=fake({("gh", "pr", "view"): _R('{"state":"OPEN"}')})), "")
    for bad in ("", "-p", "a//b", "a/b/c/d", "p;x"):
        check("azure bad repo " + repr(bad), pr_state("azure", bad, 9, run=fake({("az", "repos", "pr", "show"): _R('{"status":"active"}')})), "")
    check("azure org/project/repo ok", pr_state("azure", "acme/My Proj/repo", 9, run=fake({("az", "repos", "pr", "show"): _R('{"status":"active"}')})), "open")
    check("pr unknown forge", pr_state("gitlab", "o/r", 5, run=fake({})), "")
    check("az pr completed", pr_state("azure", "p/r", 9, run=fake({("az", "repos", "pr", "show"): _R('{"status":"completed"}')})), "merged")
    check("az pr active", pr_state("azure", "p/r", 9, run=fake({("az", "repos", "pr", "show"): _R('{"status":"active"}')})), "open")
    check("az pr abandoned", pr_state("azure", "p/r", 9, run=fake({("az", "repos", "pr", "show"): _R('{"status":"abandoned"}')})), "closed")
    r = fake({("gh", "pr", "view"): _R('{"state":"OPEN"}')})
    pr_state("github", "o/r", 5, run=r)
    check("pr argv", r.calls[0][0], ["gh", "pr", "view", "5", "-R", "o/r", "--json", "state"])

    gh_issues = json.dumps([{"repository": {"nameWithOwner": "o/r", "name": "r"}, "number": 3, "title": "T",
                             "url": "https://github.com/o/r/issues/3", "updatedAt": "2026-10-01T00:00:00Z"},
                            {"number": "bad"}])
    rows = pending_tickets(run=fake({("gh", "search"): _R(gh_issues)}))
    check("github rows", rows, [{"forge": "github", "repo": "o/r", "number": 3, "title": "T",
                                 "url": "https://github.com/o/r/issues/3", "updated": "2026-10-01T00:00:00Z"}])
    r = fake({("gh", "search"): _R("[]")})
    pending_tickets(run=r)
    check("gh search argv", r.calls[0][0], ["gh", "search", "issues", "--assignee", "@me", "--state", "open",
                                            "--json", "repository,number,title,url,updatedAt", "--limit", "50"])
    check("az skipped without org", [c[0][:2] for c in r.calls if c[0][0] == "az"], [["az", "devops"]])

    wi = json.dumps([{"id": 41, "fields": {"System.Id": 41, "System.Title": "Fix it", "System.TeamProject": "Proj",
                                          "System.ChangedDate": "2026-10-02T00:00:00Z"}}])
    az = fake({("az", "devops", "configure"): _R("[defaults]\norganization = https://dev.azure.com/acme/\nproject = X\n"),
               ("az", "boards", "query"): _R(wi)})
    rows = pending_tickets(run=az)
    check("azure rows", rows, [{"forge": "azure", "repo": "Proj", "number": 41, "title": "Fix it",
                                "url": "https://dev.azure.com/acme/_workitems/edit/41", "updated": "2026-10-02T00:00:00Z"}])
    q = [c[0] for c in az.calls if c[0][:3] == ["az", "boards", "query"]][0]
    check("wiql uses @Me", "@Me" in q[4] and "'Closed','Done','Removed'" in q[4], True)
    check("az boards argv tail", q[5:], ["-o", "json"])
    az_bad = fake({("az", "devops", "configure"): _R("[defaults]\n"), ("az", "boards", "query"): _R(wi)})
    check("azure no org", pending_tickets(run=az_bad), [])

    t = [0.0]
    c = Cache(ttl_s=300, clock=lambda: t[0])
    n = []
    c.get("k", lambda: n.append(1) or "v")
    t[0] = 299
    check("cache hit", c.get("k", lambda: n.append(1) or "w"), "v")
    check("fn once in ttl", len(n), 1)
    t[0] = 301
    check("cache expires", c.get("k", lambda: "w"), "w")

    def bad():
        n.append("bad")
        raise RuntimeError("x")
    check("failure gives empty", c.get("f", bad), "")
    t[0] = 301 + 59
    check("failure cached 60s", (c.get("f", bad), n.count("bad")), ("", 1))
    t[0] = 301 + 61
    check("failure retried", (c.get("f", lambda: "ok"),), ("ok",))

    p = Cache(ttl_s=300, clock=lambda: t[0])
    t[0] = 0
    check("peek empty is None", p.peek("a", lambda: "va"), None)
    check("peek drains", p.wait(), True)
    check("peek fresh", p.peek("a", lambda: "other"), "va")
    t[0] = 400
    check("peek stale served", p.peek("a", lambda: "vb"), "va")
    p.wait()
    check("peek refreshed", p.peek("a", lambda: "vc"), "vb")
    check("peek failure is empty", (p.peek("z", bad), p.wait(), p.peek("z", bad)), (None, True, ""))

    print("self-test: ok" if not fails else f"self-test: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)

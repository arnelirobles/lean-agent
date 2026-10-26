#!/usr/bin/env python3
"""Record the pull requests, issues, releases and work items an agent creates.

PostToolUse hook on Bash, after gh pr/issue/release create, az repos pr create
or az boards work-item create. The URL comes from the command's output (gh
prints it on the last line, az prints JSON); no URL means the create failed and
nothing is recorded. The row goes to brain's `created` table, which the brain
web page lists. It never blocks.

  LEAN_CREATED_LOG=off   turn it off
  created-log.py --self-test
"""
import json
import os
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

GH_KINDS = {("pr", "create"): "pr", ("issue", "create"): "issue", ("release", "create"): "release"}
GH_URL = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/(?:(pull|issues)/(\d+)|releases/tag/\S+)$")


AZ_PATHS = {
    "pr": re.compile(r"(?:/[^/]+){1,2}/_git/[^/]+/pullrequest/\d+$"),
    "workitem": re.compile(r"(?:/[^/]+)*/(?:_workitems/edit|_apis/wit/workItems)/\d+$", re.I),
}


def forge_url(url, forge, kind):
    """True when URL is https, on a forge host, and has the path shape of KIND."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.query or parts.fragment or parts.username or parts.port:
        return False
    if forge == "github":
        gh_host = (os.environ.get("GH_HOST") or "").lower()
        if host != "github.com" and not (gh_host and host == gh_host):
            return False
        path = {"pr": r"/[^/]+/[^/]+/pull/\d+", "issue": r"/[^/]+/[^/]+/issues/\d+",
                "release": r"/[^/]+/[^/]+/releases/tag/[^/]+"}[kind]
        return re.fullmatch(path, parts.path) is not None
    if host != "dev.azure.com" and not host.endswith(".visualstudio.com"):
        return False
    return AZ_PATHS[kind].search(parts.path) is not None


def title_of(words):
    for i, w in enumerate(words):
        if w in ("--title", "-t") and i + 1 < len(words):
            return words[i + 1]
        if w.startswith("--title="):
            return w[len("--title="):]
    return ""


def azure_org_path(url):
    m = re.match(r"https?://dev\.azure\.com/([^/]+/[^/]+)", url or "")
    return m.group(1) if m else ""


def parse_gh(words, output):
    kind = GH_KINDS.get(tuple(words[1:3]))
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not kind or not lines:
        return None
    m = GH_URL.match(lines[-1])
    if not m:
        return None
    return {"kind": kind, "forge": "github", "repo": m.group(1),
            "number": int(m.group(3)) if m.group(3) else None, "url": lines[-1]}


def parse_az(words, output):
    area = tuple(words[1:4])
    if area not in (("repos", "pr", "create"), ("boards", "work-item", "create")):
        return None
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if area[0] == "repos":
        web = (data.get("repository") or {}).get("webUrl")
        number = data.get("pullRequestId")
        if not web or not isinstance(number, int):
            return None
        m = re.match(r"https?://dev\.azure\.com/([^/]+/[^/]+)/_git/([^/]+)", web)
        repo = f"{m.group(1)}/{m.group(2)}" if m else ""
        return {"kind": "pr", "forge": "azure", "repo": repo, "number": number,
                "url": f"{web}/pullrequest/{number}"}
    url = ((data.get("_links") or {}).get("html") or {}).get("href") or data.get("url")
    number = data.get("id")
    if not url or not isinstance(number, int):
        return None
    return {"kind": "workitem", "forge": "azure", "repo": azure_org_path(url), "number": number, "url": url}


def parse_created(words, output):
    """{kind, forge, repo, number, url} for a create command whose output carries a URL, else None."""
    if not words:
        return None
    tool = os.path.basename(words[0])
    found = parse_gh(words, output or "") if tool == "gh" else parse_az(words, output or "") if tool == "az" else None
    if not found or not forge_url(found["url"], found["forge"], found["kind"]):
        return None
    found["title"] = title_of(words)
    return found


def response_text(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return str(resp.get("stdout") or "")
    return ""


def record(command, cwd, output, session_id):
    """Save the first created item in COMMAND; return its URLs. Never raises, never exits, and opens the
    database only when there is something to save."""
    found = None
    for seg in hookkit.segments(command, cwd):
        found = parse_created(seg.words, output)
        if found:
            break
    if not found:
        return []
    try:
        from brain import records, store
        branch = hookkit.git(seg.cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
        con = store.connect(timeout=1)
        records.add_created(con, found["kind"], found["forge"], found["repo"], branch, found["url"],
                            found["number"], found["title"], session_id, seg.cwd)
        return [found["url"]]
    except (SystemExit, Exception) as exc:  # a locked or newer database must not fail the tool call
        print(f"lean-agent created-log skipped: {exc!r}", file=sys.stderr)
        return []


def main():
    if os.environ.get("LEAN_CREATED_LOG") == "off":
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if not re.search(r"\b(gh|az)\b", command):
        return 0
    record(command, event.get("cwd") or os.getcwd(), response_text(event.get("tool_response")),
           str(event.get("session_id") or ""))
    return 0


def self_test():
    import sqlite3
    import subprocess
    import tempfile
    import time

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    def parse(cmd, out):
        seg = hookkit.segments(cmd)
        return parse_created(seg[0].words, out) if seg else None

    def pick(d, *keys):
        return {k: d[k] for k in keys} if d else d

    keys = ("kind", "forge", "repo", "number")
    out = "Creating pull request for x into main\n\nhttps://github.com/o/r/pull/12\n"
    check("gh pr", pick(parse("gh pr create --fill", out), *keys),
          {"kind": "pr", "forge": "github", "repo": "o/r", "number": 12})
    check("gh issue", pick(parse("gh issue create -t Bug -b x", "https://github.com/o/r/issues/5\n"), *keys),
          {"kind": "issue", "forge": "github", "repo": "o/r", "number": 5})
    check("issue title", parse("gh issue create -t 'Fix it' -b x", "https://github.com/o/r/issues/5")["title"], "Fix it")
    check("long title", parse("gh pr create --title=\"A b\"", out)["title"], "A b")
    check("no title", parse("gh pr create --fill", out)["title"], "")
    check("gh release", pick(parse("gh release create v1", "https://github.com/o/r/releases/tag/v1\n"), *keys),
          {"kind": "release", "forge": "github", "repo": "o/r", "number": None})
    pr = '{"pullRequestId": 7, "repository": {"webUrl": "https://dev.azure.com/org/p/_git/r"}}'
    got = parse("az repos pr create --title T", pr)
    check("az pr", pick(got, *keys), {"kind": "pr", "forge": "azure", "repo": "org/p/r", "number": 7})
    check("az pr url", got["url"], "https://dev.azure.com/org/p/_git/r/pullrequest/7")
    wi = '{"id": 9, "url": "https://dev.azure.com/org/_apis/wit/workItems/9", "_links": {"html": {"href": "https://dev.azure.com/org/p/_workitems/edit/9"}}}'
    got = parse("az boards work-item create --title T --type Bug", wi)
    check("az work item", pick(got, *keys), {"kind": "workitem", "forge": "azure", "repo": "org/p", "number": 9})
    check("az work item url", got["url"], "https://dev.azure.com/org/p/_workitems/edit/9")
    check("az work item fallback url", parse("az boards work-item create", '{"id": 3, "url": "https://dev.azure.com/org/_apis/wit/workItems/3"}')["url"], "https://dev.azure.com/org/_apis/wit/workItems/3")
    check("gh pr list", parse("gh pr list", out), None)
    check("failed create", parse("gh pr create --fill", "a pull request already exists"), None)
    check("empty output", parse("gh pr create", ""), None)
    check("echo", parse("echo gh pr create", out), None)
    check("non-forge url", parse("gh pr create", "https://evil.example/o/r/pull/1"), None)
    check("http url", parse("gh pr create", "http://github.com/o/r/pull/1"), None)
    check("javascript url", parse("gh pr create", "javascript:alert(1)//github.com/o/r/pull/1"), None)
    check("wrong path for kind", parse("gh issue create", "https://github.com/o/r/pull/1"), None)
    check("host suffix trick", parse("gh pr create", "https://github.com.evil.example/o/r/pull/1"), None)
    check("az non-forge", parse("az boards work-item create", '{"id": 3, "url": "https://x/3"}'), None)
    check("az pr foreign host", parse("az repos pr create", '{"pullRequestId": 7, "repository": {"webUrl": "https://evil.example/org/p/_git/r"}}'), None)
    check("az bad json", parse("az repos pr create", "WARNING"), None)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "brain.db")
        cmd = "gh pr create --fill"
        from brain import store
        check("record", record(cmd, tmp, out, "s1"), ["https://github.com/o/r/pull/12"])
        check("record twice", len(record(cmd, tmp, out, "s1")), 1)
        rows = store.connect().execute("SELECT kind, number, session_id, cwd FROM created").fetchall()
        check("one row", [tuple(r) for r in rows], [("pr", 12, "s1", tmp)])
        check("failed create not recorded", record(cmd, tmp, "nope", "s1"), [])
        os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "none", "never.db")
        check("no create opens no database", record("gh pr list", tmp, out, "s1"), [])
        check("no database file made", os.path.exists(os.path.join(tmp, "none")), False)
        newer = os.path.join(tmp, "newer.db")
        con = sqlite3.connect(newer)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO meta VALUES ('schema_version', '99')")
        con.commit()
        con.close()
        os.environ["LEAN_BRAIN_DB"] = newer
        check("newer schema does not exit", record(cmd, tmp, out, "s1"), [])
        locked = os.path.join(tmp, "locked.db")
        os.environ["LEAN_BRAIN_DB"] = locked
        store.connect().close()
        holder = sqlite3.connect(locked, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        t0 = time.time()
        check("locked database gives up", record(cmd, tmp, out, "s1"), [])
        check("locked database waits about a second", time.time() - t0 < 5, True)
        holder.close()
        ev = os.path.join(tmp, "ev.json")
        with open(ev, "w") as fh:
            json.dump({"session_id": "s", "cwd": tmp, "tool_input": {"command": cmd},
                       "tool_response": {"stdout": out}}, fh)
        os.environ["LEAN_BRAIN_DB"] = newer
        done = subprocess.run([sys.executable, os.path.realpath(__file__)], stdin=open(ev), capture_output=True)
        check("hook exits 0 on a newer schema", done.returncode, 0)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

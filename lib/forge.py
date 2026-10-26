#!/usr/bin/env python3
"""Reads from the code host a repository lives on. Only GitHub is wired up; GitLab and Azure DevOps come later.

  forge.py kind [DIR]                 github, gitlab, azure or unknown, from DIR's origin (default .)
  forge.py merged-prs REPO SINCE      one TSV row per merged pull request: number, created_at, merged_at, head_sha
  forge.py commits-after REPO N ISO   commits on pull request N committed after ISO, not counting merges
  forge.py failed-checks REPO SHA     failed check runs on SHA
  forge.py escaped-refs REPO N ISO    issues and pull requests created after ISO whose body says #N and fix
  forge.py --self-test

REPO is owner/name (always GitHub) or a clone URL. A host other than GitHub prints one line and exits 2.

CI providers. bin/pr-wait waits on checks through a provider from ci_provider(kind, ...), kind being what
kind_of returns. A provider is a class with this interface, nothing more:

  label                       what the one-line result calls the target ("pull request 12")
  checks() -> list            one call to the CI system. Each check is a dict with "name" and "state"
                              ("pass", "fail" or "pending"), plus whatever failed_log needs to find its log;
                              "link" is printed when a failed check has no log. An empty list means the CI
                              system has reported no checks yet, which the caller treats as pending
  failed_log(check, tail)     the last `tail` lines of a failed check's log, or "" when none is available

GitHub (gh) and Azure (az) are the two. The constructor takes the CLI's options (repo; org and project) and
a `capture(argv) -> (exit code, stdout, stderr)` function, which tests replace. A CLI that fails raises
CiError; a CLI that is not installed raises ToolMissing (a CiError the caller never retries).
To add a provider, write the class, add its kind to CI_PROVIDERS and give it a test function with its own fake
CLI (see github_ci_test and azure_ci_test). Commands run as argument lists, never through a shell, and
what they print is untrusted.
"""
import json
import os
import re
import subprocess
import sys


class Unsupported(Exception):
    pass


def host_of(url):
    m = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:]+)", url) or re.match(r"^[^@/]+@([^:/]+):", url)
    return m.group(1).lower() if m else ""


def kind_of(url):
    host = host_of(url)
    if host == "github.com":
        return "github"
    if "gitlab" in host:
        return "gitlab"
    if host == "dev.azure.com" or host.endswith(".visualstudio.com") or host == "ssh.dev.azure.com":
        return "azure"
    return "unknown"


def remote_url(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def run_command(cmd, timeout=600):
    """stdout of a command, or None when it is missing, fails or runs too long."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def is_url(repo):
    return "://" in repo or re.match(r"^[^@/]+@[^:/]+:", repo) is not None


def slug(repo):
    """owner/name for a GitHub URL; anything else is returned as given."""
    if not is_url(repo):
        return repo
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:/]+:", "", repo)
    return re.sub(r"\.git$", "", path.strip("/"))


def for_repo(repo, run=run_command):
    kind = kind_of(repo) if is_url(repo) else "github"
    if kind != "github":
        raise Unsupported(f"{kind} is not supported yet; only GitHub is")
    return GitHub(run)


def count(out):
    """The sum of the integers gh printed, one per line, or 0 when it failed."""
    try:
        return sum(int(line) for line in (out or "").split())
    except ValueError:
        return 0


class GitHub:
    def __init__(self, run=run_command):
        self.run = run

    def merged_prs(self, repo, since, limit=200):
        listing = self.run(["gh", "pr", "list", "-R", slug(repo), "--state", "merged", "--search", f"merged:>={since}",
                            "--limit", str(limit), "--json", "number,headRefName,headRefOid,createdAt,mergedAt"])
        if listing is None:
            return None
        try:
            prs = json.loads(listing)
        except ValueError:
            return None
        return [{"number": pr["number"], "head_ref": pr.get("headRefName") or "", "head_sha": pr.get("headRefOid") or "",
                 "created_at": pr.get("createdAt") or "", "merged_at": pr.get("mergedAt") or ""} for pr in prs]

    def commits_after(self, repo, number, after):
        # Reviews here mostly happen outside GitHub's review feature, so a fix round is a commit pushed
        # after the pull request opened, not counting merges of the default branch.
        return count(self.run(["gh", "api", f"repos/{slug(repo)}/pulls/{number}/commits", "--paginate", "-q",
                               f'[.[] | select(.commit.committer.date > "{after}") '
                               '| select(.commit.message | startswith("Merge") | not)] | length']))

    def failed_checks(self, repo, sha):
        return count(self.run(["gh", "api", f"repos/{slug(repo)}/commits/{sha}/check-runs", "-q",
                               '[.check_runs[] | select(.conclusion=="failure")] | length']))

    def escaped_refs(self, repo, number, merged_at):
        return count(self.run(["gh", "search", "issues", f'repo:{slug(repo)} "#{number}" fix in:body created:>{merged_at}',
                               "--json", "number", "-q", "length"]))


class CiError(Exception):
    pass


class ToolMissing(CiError):
    pass


def capture_command(argv, timeout=600):
    """(exit code, stdout, stderr) of a command, whatever the exit code."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
    except OSError as ex:
        raise ToolMissing(f"cannot run {argv[0]}: {ex.strerror}")
    except subprocess.SubprocessError as ex:
        raise CiError(f"{argv[0]} did not finish: {ex}")
    return r.returncode, r.stdout, r.stderr


def first_line(text, width=200):
    for line in (text or "").splitlines():
        if line.strip():
            return "".join(ch for ch in line.strip() if ch >= " " and ch != "\x7f")[:width]
    return ""


def json_out(out, what):
    try:
        return json.loads(out)
    except ValueError:
        raise CiError(f"{what} gave no JSON")


class GitHubCi:
    """Checks of a pull request through gh. Never calls az."""

    def __init__(self, repo=None, capture=capture_command):
        self.repo, self.capture, self.label = repo, capture, ""

    def target(self, pr):
        if not (re.fullmatch(r"\d+", pr) or re.fullmatch(r"https://[^/\s]+/[^/\s]+/[^/\s]+/pull/\d+/?", pr)):
            raise CiError(f"github target must be a pull request number or URL, not {first_line(pr, 60)!r}")
        self.pr, self.label = pr, f"github pull request {pr}"
        return self

    def _repo(self):
        return ["--repo", self.repo] if self.repo else []

    def checks(self):
        # One call per poll. gh exits 8 while checks are pending and 1 when one failed, with JSON either way.
        rc, out, err = self.capture(["gh", "pr", "checks", self.pr, *self._repo(), "--json", "name,bucket,link"])
        if not out.strip():
            if "no checks reported" in err.lower():
                return []
            raise CiError(f"gh pr checks exit {rc}: {first_line(err) or 'no output'}")
        buckets = {"pass": "pass", "skipping": "pass", "fail": "fail", "cancel": "fail"}
        return [{"name": r.get("name", ""), "state": buckets.get(r.get("bucket"), "pending"),
                 "link": r.get("link", "")} for r in json_out(out, f"gh pr checks (exit {rc})")]

    def failed_log(self, check, tail):
        m = re.search(r"/actions/runs/(\d+)(?:/job/(\d+))?", check.get("link", ""))
        if not m:
            return ""
        argv = ["gh", "run", "view", m.group(1), *self._repo(), "--log-failed"]
        if m.group(2):
            argv += ["--job", m.group(2)]
        rc, out, err = self.capture(argv)
        if rc != 0:
            raise CiError(f"gh run view exit {rc}: {first_line(err)}")
        return "".join(out.splitlines(True)[-tail:])


class AzureCi:
    """One Azure Pipelines run through az. Never calls gh."""

    def __init__(self, org, project, capture=capture_command):
        self.org, self.project, self.capture, self.label = org, project, capture, ""

    def target(self, run_id):
        if not re.fullmatch(r"\d+", str(run_id)):
            raise CiError(f"azure target must be a pipeline run id (digits), not {first_line(str(run_id), 60)!r}")
        self.run_id = str(run_id)
        self.label = f"azure pipeline run {run_id} (the target is a run id, not a pull request)"
        return self

    def checks(self):
        rc, out, err = self.capture(["az", "pipelines", "runs", "show", "--id", self.run_id, "--org", self.org,
                                     "--project", self.project, "--output", "json"])
        if not out.strip():
            raise CiError(f"az pipelines runs show exit {rc}: {first_line(err) or 'no output'}")
        run = json_out(out, f"az pipelines runs show (exit {rc})")
        if run.get("status") != "completed":
            return [{"name": f"run {self.run_id}", "state": "pending"}]
        if run.get("result") == "succeeded":  # partiallySucceeded is not: it falls through to red
            return [{"name": f"run {self.run_id}", "state": "pass"}]
        return self._failed_tasks() or [{"name": f"run {self.run_id} ({run.get('result')})", "state": "fail"}]

    def _invoke(self, resource, route, *extra):
        rc, out, err = self.capture(["az", "devops", "invoke", "--area", "build", "--resource", resource,
                                     "--org", self.org, "--route-parameters", f"project={self.project}",
                                     f"buildId={self.run_id}", *route, "--api-version", "7.1", *extra])
        if rc != 0:
            print(f"pr-wait: az devops invoke {resource} exit {rc}: {first_line(err)}", file=sys.stderr)
        return rc, out

    def _failed_tasks(self):
        rc, out = self._invoke("timeline", [], "--output", "json")
        try:
            records = json.loads(out).get("records", []) if rc == 0 else []
        except (ValueError, AttributeError):
            print("pr-wait: az devops invoke timeline gave no JSON", file=sys.stderr)
            return []
        return [{"name": r.get("name", ""), "state": "fail", "log_id": (r.get("log") or {}).get("id")}
                for r in records if r.get("type") == "Task"
                and r.get("result") in ("failed", "canceled", "succeededWithIssues")]

    def failed_log(self, check, tail):
        if not check.get("log_id"):
            return ""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            path = f.name
        try:
            rc, out = self._invoke("logs", [f"logId={check['log_id']}"], "--http-method", "GET",
                                   "--accept-media-type", "text/plain", "--out-file", path)
            if rc != 0:
                return ""
            with open(path, errors="replace") as f:
                return "".join(f.readlines()[-tail:])
        finally:
            os.unlink(path)


CI_PROVIDERS = {"github": GitHubCi, "azure": AzureCi}


def ci_provider(kind, target, repo=None, org=None, project=None, capture=capture_command):
    """The provider for a kind, aimed at one target (a pull request number, or an Azure run id)."""
    if kind == "github":
        return GitHubCi(repo, capture).target(target)
    if kind == "azure":
        if not (org and project):
            raise CiError("azure needs --org and --project")
        return AzureCi(org, project, capture).target(target)
    raise CiError(f"{kind} is not supported yet; pass --provider github or azure")


def main(argv):
    usage = "usage: forge.py kind [DIR] | merged-prs REPO SINCE | commits-after REPO N ISO | failed-checks REPO SHA | escaped-refs REPO N ISO"
    shapes = {"merged-prs": 2, "commits-after": 3, "failed-checks": 2, "escaped-refs": 3}
    if argv[:1] == ["kind"] and len(argv) <= 2:
        print(kind_of(remote_url(argv[1] if len(argv) == 2 else ".")))
        return 0
    if not argv or shapes.get(argv[0]) != len(argv) - 1:
        print(usage, file=sys.stderr)
        return 2
    cmd, repo, rest = argv[0], argv[1], argv[2:]
    try:
        gh = for_repo(repo)
    except Unsupported as e:
        print(f"forge: {e}", file=sys.stderr)
        return 2
    if cmd == "merged-prs":
        prs = gh.merged_prs(repo, rest[0])
        if prs is None:
            print(f"forge: listing merged pull requests of {repo} failed", file=sys.stderr)
            return 1
        for pr in prs:
            print(f"{pr['number']}\t{pr['created_at']}\t{pr['merged_at']}\t{pr['head_sha']}")
    elif cmd == "commits-after":
        print(gh.commits_after(repo, rest[0], rest[1]))
    elif cmd == "failed-checks":
        print(gh.failed_checks(repo, rest[0]))
    else:
        print(gh.escaped_refs(repo, rest[0], rest[1]))
    return 0


FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "calls"), "a").write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\\n")
def seq(f):
    items = json.load(open(os.path.join(d, f)))
    if len(items) > 1:
        json.dump(items[1:], open(os.path.join(d, f), "w"))
    return items[0]
a = sys.argv[1:]
def val(flag):
    return a[a.index(flag) + 1] if flag in a else ""
def log(n):
    text = open(os.path.join(d, "log-" + n)).read()
    if "--out-file" in a:
        open(val("--out-file"), "w").write(text)
    else:
        sys.stdout.write(text)
"""
FAKE_GH = FAKE_CLI + """
if a[:2] == ["pr", "checks"]:
    item = seq("checks.json")
    if isinstance(item, str):
        sys.stderr.write(item + "\\n"); sys.exit(1)
    print(json.dumps(item)); sys.exit(8)
if a[:2] == ["run", "view"]:
    if not os.path.exists(os.path.join(d, "log-" + (val("--job") or a[2]))):
        sys.stderr.write("log not found\\n"); sys.exit(1)
    log(val("--job") or a[2]); sys.exit(0)
sys.exit(9)
"""
FAKE_AZ = FAKE_CLI + """
if a[:3] == ["pipelines", "runs", "show"]:
    item = seq("run.json")
    if isinstance(item, str):
        sys.stderr.write(item + "\\n"); sys.exit(1)
    print(json.dumps(item)); sys.exit(0)
if a[:2] == ["devops", "invoke"] and val("--resource") == "timeline":
    if not os.path.exists(os.path.join(d, "timeline.json")):
        sys.stderr.write("TF401019: forbidden\\nsecond line\\n"); sys.exit(1)
    print(open(os.path.join(d, "timeline.json")).read()); sys.exit(0)
if a[:2] == ["devops", "invoke"] and val("--resource") == "logs":
    log([x for x in a if x.startswith("logId=")][0][6:]); sys.exit(0)
sys.exit(9)
"""


def make_scene(root, tools, files):
    """A folder with fake CLIs ({name: script}) in bin/ and data files. Returns (dir, env with PATH and FAKE_DIR)."""
    import tempfile
    d = tempfile.mkdtemp(dir=root)
    os.mkdir(os.path.join(d, "bin"))
    for name, script in tools.items():
        path = os.path.join(d, "bin", name)
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
    return d, dict(os.environ, FAKE_DIR=d, PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"])


def fake_capture(env):
    def capture(argv, timeout=600):
        r = subprocess.run(argv, capture_output=True, text=True, env=env)
        return r.returncode, r.stdout, r.stderr
    return capture


def read_calls(d):
    path = os.path.join(d, "calls")
    return open(path).read().splitlines() if os.path.exists(path) else []


def github_ci_test(check, root):
    def scene(sequence, logs=None):
        files = {"checks.json": sequence}
        files.update({"log-" + k: v for k, v in (logs or {}).items()})
        # A fake az beside the fake gh: the GitHub path must never call it.
        d, env = make_scene(root, {"gh": FAKE_GH, "az": "#!/bin/sh\necho az >> \"$FAKE_DIR/az-called\"\n"}, files)
        return d, GitHubCi("o/r", fake_capture(env)).target("12")

    def row(n, bucket, job=1):
        return {"name": n, "bucket": bucket, "link": f"https://github.com/o/r/actions/runs/77/job/{job}"}

    d, ci = scene([[row("a", "pass"), row("b", "skipping"), row("c", "fail", 5), row("d", "cancel"),
                    row("e", "pending")]], {"5": "".join(f"line {i}\n" for i in range(100))})
    got = ci.checks()
    check("github states", [c["state"] for c in got], ["pass", "pass", "fail", "fail", "pending"])
    check("github label", ci.label, "github pull request 12")
    check("github passes the repo", any("--repo o/r" in c for c in read_calls(d)), True)
    tail = ci.failed_log(got[2], 40).splitlines()
    check("github log tail", (len(tail), tail[0], tail[-1]), (40, "line 60", "line 99"))
    check("github asks the job's log", read_calls(d)[-1], "gh run view 77 --repo o/r --log-failed --job 5")
    check("github log without a run link", ci.failed_log({"name": "x", "link": "https://example.org"}, 40), "")
    check("github never calls az", os.path.exists(os.path.join(d, "az-called")), False)
    d, ci = scene([[]])
    check("github no checks", ci.checks(), [])
    d, ci = scene(["no checks reported on the 'x' branch", "HTTP 502: bad gateway"])
    check("github empty stdout with 'no checks reported' is an empty list", ci.checks(), [])
    try:
        ci.checks()
        check("github other empty-stdout error", "no error", "CiError")
    except CiError as ex:
        check("github error names the exit code and message", str(ex), "gh pr checks exit 1: HTTP 502: bad gateway")
    for bad in ("--web", "12; rm -rf", "", "https://github.com/o/r/issues/3"):
        try:
            GitHubCi("o/r", fake_capture({})).target(bad)
            check(f"github target {bad!r} refused", "no error", "CiError")
        except CiError:
            pass
    check("github accepts a PR URL", GitHubCi().target("https://github.com/o/r/pull/12").pr,
          "https://github.com/o/r/pull/12")
    try:
        GitHubCi(None, lambda argv: (1, "not json", "")).target("1").checks()
        check("github bad output", "no error", "CiError")
    except CiError:
        pass


def azure_ci_test(check, root):
    timeline = {"records": [{"type": "Stage", "name": "S", "result": "failed"},
                            {"type": "Task", "name": "Build", "result": "succeeded", "log": {"id": 1}},
                            {"type": "Task", "name": "Test", "result": "failed", "log": {"id": 7}},
                            {"type": "Task", "name": "Pack", "result": "canceled"}]}

    def scene(runs, logs=None):
        files = {"run.json": runs, "timeline.json": timeline}
        files.update({"log-" + k: v for k, v in (logs or {}).items()})
        d, env = make_scene(root, {"az": FAKE_AZ, "gh": "#!/bin/sh\necho gh >> \"$FAKE_DIR/gh-called\"\n"}, files)
        return d, AzureCi("https://dev.azure.com/o", "p", fake_capture(env)).target("5")

    d, ci = scene([{"status": "inProgress"}, {"status": "completed", "result": "succeeded"}])
    check("azure pending", [c["state"] for c in ci.checks()], ["pending"])
    check("azure green", [c["state"] for c in ci.checks()], ["pass"])
    check("azure label", ci.label, "azure pipeline run 5 (the target is a run id, not a pull request)")
    d, ci = scene([{"status": "completed", "result": "failed"}], {"7": "".join(f"line {i}\n" for i in range(100))})
    got = ci.checks()
    check("azure failed tasks", [(c["name"], c["state"]) for c in got], [("Test", "fail"), ("Pack", "fail")])
    tail = ci.failed_log(got[0], 40).splitlines()
    check("azure log tail", (len(tail), tail[0], tail[-1]), (40, "line 60", "line 99"))
    check("azure task without a log", ci.failed_log(got[1], 40), "")
    check("azure never calls gh", os.path.exists(os.path.join(d, "gh-called")), False)
    d, ci = scene([{"status": "completed", "result": "partiallySucceeded"}])
    timeline_with_issue = {"records": [{"type": "Task", "name": "Lint", "result": "succeededWithIssues", "log": {"id": 2}}]}
    with open(os.path.join(d, "timeline.json"), "w") as f:
        json.dump(timeline_with_issue, f)
    check("azure partiallySucceeded is red", [(c["name"], c["state"]) for c in ci.checks()], [("Lint", "fail")])
    d, ci = scene([{"status": "completed", "result": "partiallySucceeded"}])
    os.unlink(os.path.join(d, "timeline.json"))
    import io
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        got = ci.checks()
    finally:
        sys.stderr = old
    check("azure timeline failure still reports red", [(c["name"], c["state"]) for c in got],
          [("run 5 (partiallySucceeded)", "fail")])
    check("azure timeline failure is shown with exit code and first line", err.getvalue(),
          "pr-wait: az devops invoke timeline exit 1: TF401019: forbidden\n")
    d, ci = scene(["TF400813: not authorized"])
    try:
        ci.checks()
        check("azure runs show failure", "no error", "CiError")
    except CiError as ex:
        check("azure runs show failure message", str(ex), "az pipelines runs show exit 1: TF400813: not authorized")
    for bad in ("abc", "--x", "5 6"):
        try:
            AzureCi("o", "p").target(bad)
            check(f"azure target {bad!r} refused", "no error", "CiError")
        except CiError:
            pass
    try:
        ci_provider("azure", "5")
        check("azure needs org and project", "no error", "CiError")
    except CiError:
        pass
    try:
        ci_provider("gitlab", "5")
        check("gitlab is not wired", "no error", "CiError")
    except CiError:
        pass


def self_test():
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    for url, want in (("https://github.com/o/r.git", "github"), ("git@github.com:o/r.git", "github"),
                      ("ssh://git@github.com/o/r.git", "github"), ("https://gitlab.com/g/r", "gitlab"),
                      ("https://gitlab.example.org/g/r", "gitlab"), ("https://dev.azure.com/org/p/_git/r", "azure"),
                      ("https://org.visualstudio.com/p/_git/r", "azure"), ("https://bitbucket.org/o/r", "unknown"),
                      ("", "unknown")):
        check(f"kind of {url!r}", kind_of(url), want)

    check("owner/name is GitHub", isinstance(for_repo("o/r"), GitHub), True)
    check("a GitHub URL is GitHub", isinstance(for_repo("https://github.com/o/r"), GitHub), True)
    try:
        for_repo("https://gitlab.com/g/r")
        check("gitlab refused", "no error", "Unsupported")
    except Unsupported as e:
        check("gitlab refused", str(e), "gitlab is not supported yet; only GitHub is")

    calls = []

    def fake(out):
        def run(cmd, timeout=600):
            calls.append(cmd)
            return out
        return run

    listing = json.dumps([{"number": 7, "headRefName": "b", "headRefOid": "abc",
                           "createdAt": "2026-10-01T00:00:00Z", "mergedAt": "2026-10-02T00:00:00Z"}])
    check("merged_prs maps the listing", GitHub(fake(listing)).merged_prs("o/r", "2026-10-01"),
          [{"number": 7, "head_ref": "b", "head_sha": "abc",
            "created_at": "2026-10-01T00:00:00Z", "merged_at": "2026-10-02T00:00:00Z"}])
    check("merged_prs argv", calls[-1],
          ["gh", "pr", "list", "-R", "o/r", "--state", "merged", "--search", "merged:>=2026-10-01",
           "--limit", "200", "--json", "number,headRefName,headRefOid,createdAt,mergedAt"])
    check("merged_prs on a failed gh", GitHub(fake(None)).merged_prs("o/r", "2026-10-01"), None)
    check("merged_prs on bad JSON", GitHub(fake("not json")).merged_prs("o/r", "2026-10-01"), None)

    for name in ("failed_checks", "commits_after", "escaped_refs"):
        args = ("o/r", "abc") if name == "failed_checks" else ("o/r", 7, "2026-10-01T00:00:00Z")
        check(f"{name} on a failed gh", getattr(GitHub(fake(None)), name)(*args), 0)
        check(f"{name} parses a count", getattr(GitHub(fake("3\n")), name)(*args), 3)
    check("commits_after sums pages", GitHub(fake("2\n1\n")).commits_after("o/r", 7, "x"), 3)

    here = os.path.realpath(__file__)
    r = subprocess.run([sys.executable, here, "merged-prs", "https://dev.azure.com/o/p/_git/r", "2026-10-01"],
                       capture_output=True, text=True)
    check("azure CLI exit", r.returncode, 2)
    check("azure CLI message", r.stderr.strip(), "forge: azure is not supported yet; only GitHub is")

    retro = os.path.join(os.path.dirname(here), "..", "tools", "retro-signals.sh")
    r = subprocess.run(["bash", retro, "--since", "2026-10-01", "https://dev.azure.com/o/p/_git/r"],
                       capture_output=True, text=True)
    check("retro-signals on azure exits 2", r.returncode, 2)
    check("retro-signals on azure says why", r.stderr.strip(), "forge: azure is not supported yet; only GitHub is")
    check("retro-signals on azure prints only the header", len(r.stdout.splitlines()), 1)

    import tempfile
    with tempfile.TemporaryDirectory() as root:
        github_ci_test(check, root)
        azure_ci_test(check, root)

    for f in fails:
        print("FAIL", f)
    print("self-test failed" if fails else "self-test passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Reads from the code host a repository lives on. Only GitHub is wired up; GitLab and Azure DevOps come later.

  forge.py kind [DIR]                 github, gitlab, azure or unknown, from DIR's origin (default .)
  forge.py merged-prs REPO SINCE      one TSV row per merged pull request: number, created_at, merged_at, head_sha
  forge.py commits-after REPO N ISO   commits on pull request N committed after ISO, not counting merges
  forge.py failed-checks REPO SHA     failed check runs on SHA
  forge.py escaped-refs REPO N ISO    issues and pull requests created after ISO whose body says #N and fix
  forge.py --self-test

REPO is owner/name (always GitHub) or a clone URL. A host other than GitHub prints one line and exits 2.
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

    retro = os.path.join(os.path.dirname(here), "..", "bin", "retro-signals.sh")
    r = subprocess.run(["bash", retro, "--since", "2026-10-01", "https://dev.azure.com/o/p/_git/r"],
                       capture_output=True, text=True)
    check("retro-signals on azure exits 2", r.returncode, 2)
    check("retro-signals on azure says why", r.stderr.strip(), "forge: azure is not supported yet; only GitHub is")
    check("retro-signals on azure prints only the header", len(r.stdout.splitlines()), 1)

    for f in fails:
        print("FAIL", f)
    print("self-test failed" if fails else "self-test passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(main(sys.argv[1:]))

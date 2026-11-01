#!/usr/bin/env python3
"""Reads from the code host a repository lives on: GitHub (gh), GitLab (glab) and Azure DevOps (az).

  forge.py kind [DIR]                 github, gitlab, azure or unknown, from DIR's origin (default .)
  forge.py repo [DIR]                 the repository name the other commands and pr-blockers.sh take, from DIR
  forge.py repo-kind REPO             github, gitlab, azure or unknown for a repository name
  forge.py merged-prs REPO SINCE      one TSV row per merged pull request: number, created_at, merged_at, head_sha
  forge.py commits-after REPO N ISO   commits on pull request N committed after ISO, not counting merges
  forge.py failed-checks REPO SHA     failed check runs on SHA
  forge.py escaped-refs REPO N ISO    issues and pull requests created after ISO whose body says #N and fix
  forge.py blockers-data REPO N DIR   the reads pr-blockers.sh analyses, written as files into DIR
  (the three counts print unknown when the call failed, never 0)
  forge.py --self-test

Every script and hook of lean-agent reaches a code host through this file or through the one command table in
lib/hookkit.py. Nothing else calls gh, glab or az.

The host is picked from the origin URL (kind_of): github.com is GitHub, a host with "gitlab" in its name is
GitLab, dev.azure.com and visualstudio.com are Azure DevOps. LEAN_FORGE=github|gitlab|azure overrides that, for a
self-hosted GitLab whose name says nothing. REPO is a clone URL, or a name:
  GitHub        owner/name
  GitLab        a clone URL, HOST/group/project, or group/project (the host is glab's default; with LEAN_FORGE=gitlab)
  Azure DevOps  ORG/PROJECT/REPO (three parts, names percent-decoded) when ORG is one of the `orgs` a caller passes
                to repo_kind and for_repo, or any Azure clone URL (azure_slug turns one into that name)
A host whose CLI is not installed prints one line naming the CLI to install and exits 2.

Each host is one provider class with the same four reads, so the commands print the same TSV shape for all:
  merged_prs(repo, since)         [{"number", "head_ref", "head_sha", "created_at", "merged_at"}] or None
  commits_after(repo, n, iso)     int, or None when the call failed
  failed_checks(repo, sha)        int, or None (Azure: None, a check run is not found by commit through az)
  escaped_refs(repo, n, iso)      int, or None (Azure: None, az has no search over work item text)
Every list call is paged with a cap. A cap that is hit says so on stderr and the result is the part read.
GitHub reads through gh as it always did. GitLab reads `glab api` (projects/ID/merge_requests, .../commits,
.../statuses), Azure reads az repos pr list and az devops invoke.

CI providers. tools/pr-wait waits on checks through a provider from ci_provider(kind, ...), kind being what
kind_of returns. A provider is a class with this interface, nothing more:

  label                       what the one-line result calls the target ("pull request 12")
  checks() -> list            one call to the CI system. Each check is a dict with "name" and "state"
                              ("pass", "fail" or "pending"), plus whatever failed_log needs to find its log;
                              "link" is printed when a failed check has no log. An empty list means the CI
                              system has reported no checks yet, which the caller treats as pending
  failed_log(check, tail)     the last `tail` lines of a failed check's log, or "" when none is available

GitHub (gh), GitLab (glab) and Azure (az) are the three; Azure's target is a pipeline run id, or with
pull_request=True a pull request id (its policy evaluations are the checks). The constructor takes the CLI's
options (repo; org and project) and a `capture(argv) -> (exit code, stdout, stderr)` function, which tests
replace. A CLI that fails raises CiError; a CLI that is not installed raises ToolMissing (a CiError the caller
never retries).

Add a code host: write one provider class (the four reads above, plus blocker_reads and the hint methods),
write its CI class if the host has checks, add the kind to PROVIDERS, CI_PROVIDERS, CLI_OF and INSTALL, give it a fake
CLI in the self-test (see FAKE_GLAB) and a test function that runs every command against it, and add its
CLI to the table in lib/hookkit.py. The README section "Add a code host" lists the steps.
Commands run as argument lists, never through a shell, and what they print is untrusted.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time


class Unsupported(Exception):
    pass


KINDS = ("github", "gitlab", "azure")
CLI_OF = {"github": "gh", "gitlab": "glab", "azure": "az"}
INSTALL = {"github": "gh, the GitHub CLI (https://cli.github.com)",
           "gitlab": "glab, the GitLab CLI (https://gitlab.com/gitlab-org/cli)",
           "azure": "az, the Azure CLI (https://aka.ms/installazurecli), then: az extension add --name azure-devops"}
_WARNED = set()


def forced_kind():
    """The host LEAN_FORGE names, or "" when it is unset or not one of github, gitlab, azure (said once on stderr)."""
    value = os.environ.get("LEAN_FORGE", "").strip().lower()
    if value in KINDS:
        return value
    if value and value not in _WARNED:
        _WARNED.add(value)
        print(f"forge: LEAN_FORGE={value[:40]!r} is not github, gitlab or azure, ignored", file=sys.stderr)
    return ""


def detect(url):
    """The host of a clone URL: LEAN_FORGE when set, else what kind_of says."""
    return forced_kind() or kind_of(url)


def missing_cli(kind, which=shutil.which):
    """One line naming the CLI to install when the CLI of `kind` is not on PATH, else None."""
    cli = CLI_OF.get(kind)
    if cli and not which(cli):
        return f"{cli} is not installed; install {INSTALL[kind]}"
    return None


def host_of(url):
    m = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:]+)", url) or re.match(r"^[^@/]+@([^:/]+):", url)
    return m.group(1).lower() if m else ""


def kind_of(url):
    host = host_of(url)
    if host == "github.com":
        return "github"
    if re.search(r"(^|\.)gitlab(\.|$)", host):
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def redact(text):
    """text with the userinfo (user, user:password or a token) of any URL in it removed."""
    return re.sub(r"(?<=://)[^/@\s]*@", "", text or "")


def is_url(repo):
    return "://" in repo or re.match(r"^[^@/]+@[^:/]+:", repo) is not None


def slug(repo):
    """owner/name for a GitHub URL; anything else is returned as given."""
    if not is_url(repo):
        return repo
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:/]+:", "", repo)
    return re.sub(r"\.git$", "", path.strip("/"))


def repo_kind(repo, orgs=()):
    """github, gitlab, azure or unknown for a clone URL, an owner/name or an ORG/PROJECT/REPO.

    A bare three-part name is Azure only when its first part is one of `orgs` (case-insensitive), or when
    LEAN_FORGE=azure. HOST/group/project is GitLab when HOST is a name kind_of calls GitLab. Any other bare name
    is GitHub, or whatever LEAN_FORGE says.
    """
    if is_url(repo):
        return detect(repo)
    parts = repo.split("/")
    if len(parts) == 3 and parts[0].lower() in {o.lower() for o in orgs}:
        return "azure"
    forced = forced_kind()
    if forced == "azure":
        return "azure" if len(parts) == 3 else "unknown"
    if forced:
        return forced
    if len(parts) >= 3 and "." in parts[0] and kind_of("https://" + repo) == "gitlab":
        return "gitlab"
    return "github"


def for_repo(repo, run=run_command, orgs=()):
    kind = repo_kind(repo, orgs)
    if kind not in PROVIDERS:
        raise Unsupported(f"{kind} is not a code host forge.py reads: {redact(repo)}")
    return PROVIDERS[kind](run)


def parse_azure(repo):
    """(org, project, repo) from an Azure DevOps clone URL or ORG/PROJECT/REPO, percent-decoded, or None.

    https://[user@]dev.azure.com/ORG/PROJECT/_git/REPO, https://ORG.visualstudio.com/[DefaultCollection/]PROJECT/_git/REPO,
    git@ssh.dev.azure.com:v3/ORG/PROJECT/REPO, ORG@vs-ssh.visualstudio.com:v3/ORG/PROJECT/REPO,
    ssh://git@ssh.dev.azure.com/v3/ORG/PROJECT/REPO. A trailing .git is dropped. A URL without a
    project (.../ORG/_git/REPO) has the project named after the repository. A name that holds a slash
    after decoding (%2F) is refused.
    """
    from urllib.parse import unquote
    repo = (repo or "").strip()
    if not is_url(repo):
        parts = repo.strip("/").split("/")
        return tuple(parts) if len(parts) == 3 and all(parts) else None
    host = host_of(repo)
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:/]+:", "", repo).split("?")[0].strip("/")
    path = re.sub(r"\.git$", "", path)
    raw = path.split("/")
    parts = [unquote(p) for p in raw]
    if any("/" in p for p in parts):
        return None
    if host == "ssh.dev.azure.com" or (host.startswith("vs-ssh.") and host.endswith(".visualstudio.com")):
        parts = parts[1:] if parts[:1] == ["v3"] else []
        found = parts if len(parts) == 3 else None
    elif host == "dev.azure.com":
        found = _git_parts(parts[0], parts[1:]) if parts else None
    elif host.endswith(".visualstudio.com"):
        rest = parts[1:] if parts[:1] and parts[0].lower() == "defaultcollection" else parts
        found = _git_parts(host[:-len(".visualstudio.com")], rest)
    else:
        found = None
    return tuple(found) if found and all(found) else None


def _git_parts(org, rest):
    """[org, project, repo] from the path after the organization: PROJECT/_git/REPO or _git/REPO."""
    if len(rest) == 3 and rest[1] == "_git":
        return [org, rest[0], rest[2]]
    if len(rest) == 2 and rest[0] == "_git":
        return [org, rest[1], rest[1]]
    return None


def azure_slug(repo):
    """ORG/PROJECT/REPO for an Azure DevOps clone URL or name, or empty when it is not one."""
    found = parse_azure(repo)
    return "/".join(found) if found else ""


def count(out):
    """The sum of the integers gh printed, one per line, or None when the call failed or printed nothing usable."""
    try:
        parts = (out or "").split()
        return sum(int(line) for line in parts) if parts else None
    except ValueError:
        return None


class CiError(Exception):
    pass


class ToolMissing(CiError):
    pass


def capture_command(argv, timeout=600):
    """(exit code, stdout, stderr) of a command, whatever the exit code."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
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


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class GitHub:
    """Pull requests and checks of GitHub through gh. Never calls glab or az."""
    cli = "gh"
    host_name = "GitHub"

    def __init__(self, run=run_command):
        self.run = run
        self.cut = False

    def merged_prs(self, repo, since, limit=200):
        self.cut = False
        listing = self.run(["gh", "pr", "list", "-R", slug(repo), "--state", "merged", "--search", f"merged:>={since}",
                            "--limit", str(limit + 1), "--json", "number,headRefName,headRefOid,createdAt,mergedAt"])
        if listing is None:
            return None
        try:
            prs = json.loads(listing)
        except ValueError:
            return None
        if len(prs) > limit:
            prs = prs[:limit]
            self.cut = True
            print(f"forge: stopped at {limit} merged pull requests of {slug(repo)}, older ones are not counted",
                  file=sys.stderr)
        return [{"number": pr["number"], "head_ref": pr.get("headRefName") or "", "head_sha": pr.get("headRefOid") or "",
                 "created_at": pr.get("createdAt") or "", "merged_at": pr.get("mergedAt") or ""} for pr in prs]

    def commits_after(self, repo, number, after):
        # Reviews here mostly happen outside GitHub's review feature, so a fix round is a commit pushed
        # after the pull request opened, not counting merges of the default branch.
        return count(self.run(["gh", "api", f"repos/{slug(repo)}/pulls/{number}/commits", "--paginate", "-q",
                               f'[.[] | select(.commit.committer.date > "{after}") '
                               '| select(.commit.message | startswith("Merge") | not)] | length']))

    def failed_checks(self, repo, sha):
        return count(self.run(["gh", "api", f"repos/{slug(repo)}/commits/{sha}/check-runs?per_page=100", "--paginate", "-q",
                               '[.check_runs[] | select(.conclusion=="failure")] | length']))

    def escaped_refs(self, repo, number, merged_at, limit=100):
        found = count(self.run(["gh", "search", "issues", f'repo:{slug(repo)} "#{number}" fix in:body created:>{merged_at}',
                                "--limit", str(limit + 1), "--json", "number", "-q", "length"]))
        if found is not None and found > limit:
            print(f"forge: escaped_refs for {slug(repo)}#{number} stopped at {limit} issues, more were not counted",
                  file=sys.stderr)
            return limit
        return found

    # What pr-blockers.sh calls the host and the commands it points at.
    def ready_command(self, number):
        return f"gh pr ready {number}"

    def merge_command(self, number, method=""):
        return f"gh pr merge {number}" + (f" {method}" if method else "")

    unexplained = "read the rulesets with: gh api repos/OWNER/REPO/rulesets"

    def blocker_reads(self, repo, number, out, capture=capture_command, sleep=5):
        """Write the reads pr-blockers.sh analyses into the folder `out`: the pull request, its branch protection,
        rulesets, runs waiting for approval, head commit, comparison with the base, repository settings, review
        threads and merge queue. A failed read leaves NAME.err, never a guess."""
        reads = Reads(out, capture)
        name = slug(repo)
        owner, _, short = name.partition("/")
        fields = "number,state,isDraft,mergeable,mergeStateStatus,reviewDecision,baseRefName,headRefOid,latestReviews,statusCheckRollup"
        view = ["gh", "pr", "view", str(int(number)), "-R", name, "--json", fields]
        pr = reads.get("pr", view)
        if pr is None:
            return
        # GitHub computes mergeability lazily; the first read after a push often says UNKNOWN. One retry.
        if isinstance(pr, dict) and pr.get("state") == "OPEN" and "UNKNOWN" in (pr.get("mergeable"), pr.get("mergeStateStatus")):
            time.sleep(sleep)
            pr = reads.get("pr", view)
            if pr is None:
                return
        base, sha = pr["baseRefName"], pr["headRefOid"]
        reads.get("branch", ["gh", "api", f"repos/{name}/branches/{base}"])
        reads.get("rules", ["gh", "api", f"repos/{name}/rules/branches/{base}"])
        reads.get("runs", ["gh", "api", f"repos/{name}/actions/runs?head_sha={sha}&status=action_required&per_page=50"])
        reads.get("commit", ["gh", "api", f"repos/{name}/commits/{sha}"])
        reads.get("compare", ["gh", "api", f"repos/{name}/compare/{base}...{sha}"])
        reads.get("repo", ["gh", "api", f"repos/{name}"])
        reads.get("threads", ["gh", "api", "graphql", "-F", f"owner={owner}", "-F", f"name={short}", "-F", f"number={int(number)}",
                              "-f", "query=\nquery($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){\n"
                              "  reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated path line comments(first:1){nodes{author{login}}}}}}}}"])
        reads.get("mq", ["gh", "api", "graphql", "-F", f"owner={owner}", "-F", f"name={short}", "-F", f"branch={base}",
                         "-f", "query=\nquery($owner:String!,$name:String!,$branch:String!){repository(owner:$owner,name:$name){mergeQueue(branch:$branch){id}}}"])
        # The branch endpoint 404s on an unprotected branch with no rules; that is an answer, not a failure.
        branch_err = reads._path("branch", ".err")
        if os.path.exists(branch_err) and "HTTP 404" in open(branch_err).read():
            os.unlink(branch_err)


AZURE_PAGE = 100
AZURE_CAP = 1000
AZ_TIMEOUT = 60


def azure_time(text):
    """An Azure timestamp as YYYY-MM-DDTHH:MM:SSZ, the shape GitHub gives. Anything else is returned as it came."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(?:Z|[+-]00:?00)", text or "")
    return m.group(1) + "Z" if m else (text or "")


class Azure:
    """Pull requests and repository lists of Azure DevOps through az. Never calls gh or glab.

    The repository is a clone URL or ORG/PROJECT/REPO. Every az call has a 60 second timeout and no stdin.
    merged_prs returns the shape GitHub's does and pages az 100 at a time, newest closed first, until a page
    ends before `since`, a short page, or `limit` (default 1000) pull requests were read. It asks for one more
    than the limit to know the limit was really hit, trims to the limit, and reports a hit on stderr.
    A completed pull request with no close date is skipped. default_org, projects and repos answer the
    listing questions lean-stats needs; each gives None when az fails. commits_after counts the commits on a
    pull request committed after a time, merges not counted, from the first 500; nothing calls it yet.
    failed_checks and escaped_refs return None: check runs cannot be found by commit through az, and az has
    no search over work item text. A failed az call gives None.
    """

    cli = "az"
    host_name = "Azure DevOps"

    def __init__(self, run=run_command, cap=AZURE_CAP, page=AZURE_PAGE):
        self.run, self.cap, self.page = run, cap, page

    def _az(self, argv):
        return self.run(argv, timeout=AZ_TIMEOUT)

    def _target(self, repo):
        found = parse_azure(repo)
        if not found:
            raise Unsupported(f"not an Azure DevOps repository: {redact(repo)}")
        return f"https://dev.azure.com/{found[0]}", found[1], found[2]

    def default_org(self):
        """The organization name az has as its default: "" when it has none, None when az fails."""
        out = self._az(["az", "devops", "configure", "-l"])
        if out is None:
            return None
        m = re.search(r"organization\s*=\s*(\S+)", out)
        if not m:
            return ""
        url = m.group(1)
        if host_of(url) == "dev.azure.com":
            org = re.sub(r"^[a-z+]+://[^/]+/", "", url).strip("/").split("/")[0]
        elif host_of(url).endswith(".visualstudio.com"):
            org = host_of(url)[:-len(".visualstudio.com")]
        else:
            org = ""
        return org

    def projects(self, org):
        """Project names of an organization, or None when az fails."""
        out = self._az(["az", "devops", "project", "list", "--org", f"https://dev.azure.com/{org}", "--top", "500",
                        "--query", "value[].name", "--output", "json"])
        return self._names(out)

    def repos(self, org, project):
        """Repository names of one project, or None when az fails."""
        out = self._az(["az", "repos", "list", "--project", project, "--org", f"https://dev.azure.com/{org}",
                        "--query", "[].name", "--output", "json"])
        return self._names(out)

    @staticmethod
    def _names(out):
        try:
            names = json.loads(out)
        except (ValueError, TypeError):
            return None
        return names if isinstance(names, list) and all(isinstance(n, str) for n in names) else None

    def merged_prs(self, repo, since, limit=None):
        org, project, name = self._target(repo)
        cap = limit or self.cap
        out = []
        read = 0
        while True:
            top = min(self.page, cap + 1 - read)
            listing = self._az(["az", "repos", "pr", "list", "--status", "completed", "--repository", name,
                                "--project", project, "--org", org, "--top", str(top), "--skip", str(read),
                                "--output", "json"])
            if listing is None:
                return None
            try:
                prs = json.loads(listing)
                batch = [{"number": pr["pullRequestId"],
                          "head_ref": re.sub(r"^refs/heads/", "", pr.get("sourceRefName") or ""),
                          "head_sha": (pr.get("lastMergeSourceCommit") or {}).get("commitId") or "",
                          "created_at": azure_time(pr.get("creationDate")),
                          "merged_at": azure_time(pr.get("closedDate"))}
                         for pr in prs]
                closed = [bool(pr.get("closedDate")) for pr in prs]
            except (ValueError, KeyError, TypeError, AttributeError):
                return None
            over = read + len(prs) > cap
            if over:
                batch, closed = batch[:cap - read], closed[:cap - read]
            dated = [pr for pr, ok in zip(batch, closed) if ok]
            out += [pr for pr in dated if pr["merged_at"] >= since]
            read += len(batch)
            if over:
                print(f"forge: stopped at {cap} pull requests of {azure_slug(repo)}, older ones are not counted",
                      file=sys.stderr)
                return out
            if len(prs) < top or (dated and dated[-1]["merged_at"] < since):
                return out

    def commits_after(self, repo, number, after):
        org, project, name = self._target(repo)
        out = self._az(["az", "devops", "invoke", "--area", "git", "--resource", "pullRequestCommits",
                        "--org", org, "--route-parameters", f"project={project}", f"repositoryId={name}",
                        f"pullRequestId={number}", "--query-parameters", "$top=500", "--api-version", "7.1",
                        "--output", "json"])
        try:
            commits = json.loads(out)["value"]
            return sum(1 for c in commits
                       if azure_time(c["committer"]["date"]) > azure_time(after)
                       and not (c.get("comment") or "").startswith("Merge"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def failed_checks(self, repo, sha):
        return None

    def escaped_refs(self, repo, number, merged_at):
        return None

    # What pr-blockers.sh calls the host and the commands it points at.
    def ready_command(self, number):
        return f"az repos pr update --id {number} --draft false"

    def merge_command(self, number, method=""):
        return f"az repos pr update --id {number} --status completed" + (" --squash true" if method == "--squash" else "")

    unexplained = "read the branch policies with: az repos policy list"

    def blocker_reads(self, repo, number, out, capture=capture_command, sleep=5):
        """Write the reads pr-blockers.sh analyses into the folder `out`, in the shape the GitHub reads have:
        the pull request, its policy evaluations as checks, its threads and the project's merge strategy."""
        org, project, name = self._target(repo)
        reads = Reads(out, capture)
        pr_id = str(int(number))
        show = ["az", "repos", "pr", "show", "--id", pr_id, "--org", org, "--output", "json"]
        for attempt in (0, 1):
            pr = reads.get("show", show)
            if not isinstance(pr, dict):
                reads.fail("pr", reads.errors.get("show", "no data"))
                return
            if attempt == 0 and pr.get("status") == "active" and pr.get("mergeStatus") in ("queued", None):
                time.sleep(sleep)
                continue
            break
        reads.discard("show")
        policies = reads.get("policies", ["az", "repos", "pr", "policy", "list", "--id", pr_id, "--org", org, "--output", "json"])
        reads.discard("policies")
        policies = policies if isinstance(policies, list) else []
        rollup, required, need, resolve, strategy = [], [], 0, False, None
        for ev in policies:
            conf = ev.get("configuration") or {}
            ctype = conf.get("type") or {}
            settings = conf.get("settings") or {}
            label = CONTROL.sub("", str(ctype.get("displayName") or "policy"))
            if settings.get("displayName"):
                label += ": " + CONTROL.sub("", str(settings["displayName"]))
            status = ev.get("status")
            result = ("SUCCESS" if status == "approved" else "SKIPPED" if status == "notApplicable" else
                      "" if status in ("queued", "running") else "FAILURE")
            rollup.append({"__typename": "CheckRun", "name": label, "status": "IN_PROGRESS" if result == "" else "COMPLETED",
                           "conclusion": result})
            if conf.get("isBlocking") and conf.get("isEnabled", True):
                required.append(label)
            if ctype.get("id") == "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd" and conf.get("isEnabled", True):
                need = max(need, int(settings.get("minimumApproverCount") or 0))
            if ctype.get("id") == "c6a1889d-b943-4856-b76f-9e46bb6b0df2" and conf.get("isBlocking"):
                resolve = True
            if ctype.get("id") == "fa4e907d-c16b-4a4c-9dfa-4916e5d171ab":
                strategy = settings
        votes = [(r.get("displayName") or r.get("uniqueName") or "?", r.get("vote") or 0) for r in pr.get("reviewers") or []]
        waiting = [n for n, v in votes if v < 0]
        decision = "CHANGES_REQUESTED" if waiting else "REVIEW_REQUIRED" if need and not any(v > 0 for _n, v in votes) else \
            "APPROVED" if any(v > 0 for _n, v in votes) else ""
        merge_status = pr.get("mergeStatus")
        failing = any(r["conclusion"] == "FAILURE" for r in rollup)
        merge_state = ("DIRTY" if merge_status == "conflicts" else
                       "UNKNOWN" if merge_status in ("queued", None) else
                       "BLOCKED" if failing or any(r["conclusion"] == "" for r in rollup) or waiting else "CLEAN")
        reads.put("pr", {
            "number": pr.get("pullRequestId", int(number)),
            "state": {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}.get(pr.get("status"), "OPEN"),
            "isDraft": bool(pr.get("isDraft")),
            "mergeable": {"succeeded": "MERGEABLE", "conflicts": "CONFLICTING"}.get(merge_status, "UNKNOWN"),
            "mergeStateStatus": merge_state, "reviewDecision": decision,
            "baseRefName": re.sub(r"^refs/heads/", "", pr.get("targetRefName") or "?"),
            "headRefOid": (pr.get("lastMergeSourceCommit") or {}).get("commitId") or "",
            "statusCheckRollup": rollup,
            "latestReviews": [{"author": {"login": CONTROL.sub("", str(n))}, "state": "APPROVED" if v > 0 else "CHANGES_REQUESTED"}
                              for n, v in votes if v]})
        reads.put("branch", {"protection": {"required_status_checks": {"contexts": required}}})
        threads = reads.get("threads_raw", ["az", "devops", "invoke", "--area", "git", "--resource", "pullRequestThreads",
                                            "--org", org, "--route-parameters", f"project={project}", f"repositoryId={name}",
                                            f"pullRequestId={pr_id}", "--api-version", "7.1", "--output", "json"])
        reads.discard("threads_raw")
        if isinstance(threads, dict) and isinstance(threads.get("value"), list):
            nodes = []
            for th in threads["value"]:
                comments = [c for c in th.get("comments") or [] if not c.get("isDeleted")]
                first = comments[0] if comments else {}
                if th.get("isDeleted") or first.get("commentType") == "system" or not comments:
                    continue
                if th.get("status") in ("active", "pending"):
                    ctx = th.get("threadContext") or {}
                    nodes.append({"isResolved": False, "path": ctx.get("filePath") or "?",
                                  "line": ((ctx.get("rightFileStart") or ctx.get("leftFileStart")) or {}).get("line"),
                                  "comments": {"nodes": [{"author": {"login": CONTROL.sub("", str((first.get("author") or {}).get("displayName", "?")))}}]}})
            reads.put("threads", {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False}}}}}})
        else:
            reads.fail("threads", reads.errors.get("threads_raw", "no data"))
        reads.put("rules", [{"type": "pull_request", "parameters": {
            "required_approving_review_count": need, "required_review_thread_resolution": resolve}}])
        reads.put("runs", {"workflow_runs": []})
        if strategy:
            reads.put("repo", {"allow_squash_merge": bool(strategy.get("allowSquash")),
                               "allow_merge_commit": bool(strategy.get("allowNoFastForward")),
                               "allow_rebase_merge": bool(strategy.get("allowRebase") or strategy.get("allowRebaseMerge"))})
        reads.put("mq", {"data": {"repository": {"mergeQueue": None}}})


GITLAB_PAGE = 100
GITLAB_CAP = 200
GITLAB_TIMEOUT = 60


def utc_time(text):
    """A timestamp with any offset as YYYY-MM-DDTHH:MM:SSZ in UTC. Anything else is returned as it came."""
    from datetime import datetime, timedelta, timezone
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?", text or "")
    if not m:
        return text or ""
    y, mo, d, h, mi, sec = (int(g) for g in m.groups()[:6])
    zone = m.group(7)
    when = datetime(y, mo, d, h, mi, sec)
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        when -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return when.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gitlab_target(repo):
    """(host, project path) for a GitLab clone URL, HOST/group/project or group/project. The host is "" when the
    name has none, so glab uses its own default host."""
    repo = (repo or "").strip()
    if is_url(repo):
        return host_of(repo), slug(repo)
    parts = repo.strip("/").split("/")
    if len(parts) >= 3 and "." in parts[0]:
        return parts[0].lower(), "/".join(parts[1:])
    return "", repo.strip("/")


def gitlab_slug(repo):
    """The name lean-stats stores for a GitLab repository: HOST/group/project, or group/project with no host."""
    host, path = gitlab_target(repo)
    return f"{host}/{path}" if host else path


def gitlab_api(repo, endpoint):
    """The glab argv that GETs `endpoint` (after projects/ID/) of a repository. Query text is already encoded."""
    from urllib.parse import quote
    host, path = gitlab_target(repo)
    return ["glab", "api"] + (["--hostname", host] if host else []) + [
        f"projects/{quote(path, safe='')}" + (f"/{endpoint}" if endpoint else "")]


def gitlab_ci_api(repo, endpoint):
    """Like gitlab_api; with no repo glab fills :id from the current folder."""
    return gitlab_api(repo, endpoint) if repo else ["glab", "api", f"projects/:id/{endpoint}"]


class GitLab:
    """Merge requests of GitLab through glab api. Never calls gh or az.

    The repository is a clone URL, HOST/group/project or group/project. Every glab call has a 60 second timeout
    and no stdin. merged_prs returns the shape GitHub's does, pages 100 at a time newest first until a page ends
    before `since`, a short page, or `limit` (default 200) merge requests were read; it reads one more than the
    limit to know the limit was really hit, trims to the limit and reports a hit on stderr. A failed glab call
    gives None. head_sha is the merge request's head commit.
    """
    cli = "glab"
    host_name = "GitLab"

    def __init__(self, run=run_command, cap=GITLAB_CAP, page=GITLAB_PAGE):
        self.run, self.cap, self.page = run, cap, page
        self.cut = False

    def _get(self, repo, endpoint):
        out = self.run(gitlab_api(repo, endpoint), timeout=GITLAB_TIMEOUT)
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return None

    def _pages(self, repo, endpoint, cap, keep=None, stop=None):
        """(items, cut) reading `endpoint` page by page, or None when a call failed. `keep` filters an item,
        `stop` ends the reading after a page whose last item it accepts. Asks for one item more than `cap`."""
        joiner = "&" if "?" in endpoint else "?"
        items, number = [], 0
        while True:
            number += 1
            batch = self._get(repo, f"{endpoint}{joiner}per_page={self.page}&page={number}")
            if not isinstance(batch, list):
                return None
            items += [i for i in batch if isinstance(i, dict) and (keep is None or keep(i))]
            if len(items) > cap:
                return items[:cap], True
            if len(batch) < self.page or (batch and stop and stop(batch[-1])):
                return items, False

    def merged_prs(self, repo, since, limit=None):
        from urllib.parse import quote
        self.cut = False
        cap = limit or self.cap
        after = since if "T" in since else since + "T00:00:00Z"
        found = self._pages(repo, f"merge_requests?state=merged&order_by=updated_at&sort=desc&updated_after={quote(after)}",
                            cap, keep=lambda mr: bool(mr.get("merged_at")) and utc_time(mr["merged_at"]) >= since,
                            stop=lambda mr: utc_time(mr.get("updated_at")) < since)
        if found is None:
            return None
        mrs, self.cut = found
        if self.cut:
            print(f"forge: stopped at {cap} merged pull requests of {gitlab_slug(repo)}, older ones are not counted",
                  file=sys.stderr)
        try:
            return [{"number": mr["iid"], "head_ref": mr.get("source_branch") or "", "head_sha": mr.get("sha") or "",
                     "created_at": utc_time(mr.get("created_at")), "merged_at": utc_time(mr.get("merged_at"))}
                    for mr in mrs]
        except (KeyError, TypeError):
            return None

    def commits_after(self, repo, number, after, limit=500):
        found = self._pages(repo, f"merge_requests/{int(number)}/commits", limit)
        if found is None:
            return None
        commits, cut = found
        if cut:
            print(f"forge: commits_after for {gitlab_slug(repo)}!{number} stopped at {limit} commits, "
                  "more were not counted", file=sys.stderr)
        return sum(1 for c in commits if utc_time(c.get("committed_date")) > utc_time(after)
                   and not (c.get("title") or c.get("message") or "").startswith("Merge"))

    def failed_checks(self, repo, sha, limit=500):
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", sha or ""):
            return None
        found = self._pages(repo, f"repository/commits/{sha}/statuses", limit)
        if found is None:
            return None
        statuses, cut = found
        if cut:
            print(f"forge: failed_checks for {gitlab_slug(repo)}@{sha[:8]} stopped at {limit} statuses, "
                  "more were not counted", file=sys.stderr)
        return sum(1 for st in statuses if st.get("status") == "failed" and not st.get("allow_failure"))

    def escaped_refs(self, repo, number, merged_at, limit=100):
        from urllib.parse import quote
        ref = re.compile(r"[#!]%d(?!\d)" % int(number))
        total = 0
        for kind in ("issues", "merge_requests"):
            found = self._pages(repo, f"{kind}?search=fix&in=description&order_by=created_at&sort=desc"
                                f"&created_after={quote(merged_at)}", limit * 3,
                                keep=lambda i: bool(ref.search(i.get("description") or ""))
                                and "fix" in (i.get("description") or "").lower())
            if found is None:
                return None
            total += len(found[0])
            if found[1]:
                print(f"forge: escaped_refs for {gitlab_slug(repo)}#{number} stopped at {limit * 3} {kind}, "
                      "more were not counted", file=sys.stderr)
        if total > limit:
            print(f"forge: escaped_refs for {gitlab_slug(repo)}#{number} stopped at {limit} issues, "
                  "more were not counted", file=sys.stderr)
            return limit
        return total

    # What pr-blockers.sh calls the host and the commands it points at.
    def ready_command(self, number):
        return f"glab mr update {number} --ready"

    def merge_command(self, number, method=""):
        flag = {"--squash": " --squash", "--rebase": " --rebase"}.get(method, "")
        return f"glab mr merge {number}{flag}"

    unexplained = "read the project's merge request settings with: glab api projects/:id"

    def blocker_reads(self, repo, number, out, capture=capture_command, sleep=5):
        """Write the reads pr-blockers.sh analyses into the folder `out`, in the shape the GitHub reads have."""
        reads = Reads(out, capture)
        mr_endpoint = f"merge_requests/{int(number)}"
        for attempt in (0, 1):
            mr = reads.get("mr", gitlab_api(repo, mr_endpoint))
            if not isinstance(mr, dict):
                reads.fail("pr", reads.errors.get("mr", "no data"))
                return
            if attempt == 0 and mr.get("state") == "opened" and mr.get("detailed_merge_status") in (
                    "checking", "unchecked", "preparing"):
                time.sleep(sleep)
                continue
            break
        reads.discard("mr")
        status = mr.get("detailed_merge_status") or ""
        blocked_states = {"ci_must_pass", "ci_still_running", "not_approved", "discussions_not_resolved",
                          "blocked_status", "external_status_checks", "jira_association_missing",
                          "requested_changes", "locked_paths", "locked_lfs_files", "title_regex"}
        state = {"opened": "OPEN", "merged": "MERGED", "closed": "CLOSED", "locked": "CLOSED"}.get(mr.get("state"), "OPEN")
        merge_state = ("DIRTY" if status == "conflict" or mr.get("has_conflicts") else
                       "BEHIND" if status == "need_rebase" else
                       "BLOCKED" if status in blocked_states else
                       "UNKNOWN" if status in ("checking", "unchecked", "preparing") else "CLEAN")
        approvals = reads.get("approvals", gitlab_api(repo, mr_endpoint + "/approvals"))
        reads.discard("approvals")
        settings = reads.get("project", gitlab_api(repo, ""))
        reads.discard("project")
        approved = [((a.get("user") or {}).get("username") or "?") for a in (approvals or {}).get("approved_by") or []]
        left = (approvals or {}).get("approvals_left") or 0
        decision = ("CHANGES_REQUESTED" if status == "requested_changes" else
                    "REVIEW_REQUIRED" if left or status == "not_approved" else "APPROVED" if approved else "")
        pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
        rollup = []
        if pipeline.get("id"):
            jobs = reads.get("jobs", gitlab_api(repo, f"pipelines/{int(pipeline['id'])}/jobs?per_page=100"))
            reads.discard("jobs")
            for job in jobs if isinstance(jobs, list) else []:
                rollup.append(self._job_check(job))
            if not isinstance(jobs, list):
                reads.note_error("jobs")
        pr = {"number": mr.get("iid", number), "state": state, "isDraft": bool(mr.get("draft")),
              "mergeable": "CONFLICTING" if merge_state == "DIRTY" else "UNKNOWN" if merge_state == "UNKNOWN" else "MERGEABLE",
              "mergeStateStatus": merge_state, "reviewDecision": decision, "baseRefName": mr.get("target_branch") or "?",
              "headRefOid": mr.get("sha") or "", "statusCheckRollup": rollup,
              "latestReviews": [{"author": {"login": u}, "state": "APPROVED"} for u in approved]}
        reads.put("pr", pr)
        discussions = reads.get("threads", gitlab_api(repo, mr_endpoint + "/discussions?per_page=100"))
        reads.discard("threads")
        if isinstance(discussions, list):
            nodes = []
            for d in discussions:
                notes = [n for n in d.get("notes") or [] if isinstance(n, dict)]
                first = notes[0] if notes else {}
                if first.get("resolvable") and not all(n.get("resolved") for n in notes if n.get("resolvable")):
                    pos = first.get("position") or {}
                    nodes.append({"isResolved": False, "path": pos.get("new_path") or pos.get("old_path") or "?",
                                  "line": pos.get("new_line") or pos.get("old_line"),
                                  "comments": {"nodes": [{"author": {"login": (first.get("author") or {}).get("username", "?")}}]}})
            reads.put("threads", {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": nodes, "pageInfo": {"hasNextPage": len(discussions) >= 100}}}}}})
        else:
            reads.note_error("threads")
        need = (approvals or {}).get("approvals_required") or 0
        resolve = bool((settings or {}).get("only_allow_merge_if_all_discussions_are_resolved"))
        reads.put("rules", [{"type": "pull_request", "parameters": {
            "required_approving_review_count": need, "required_review_thread_resolution": resolve}}])
        reads.put("compare", {"behind_by": mr.get("diverged_commits_count") or 0})
        reads.put("runs", {"workflow_runs": []})
        method = (settings or {}).get("merge_method")
        squash = (settings or {}).get("squash_option")
        reads.put("repo", {"allow_squash_merge": squash in ("default_on", "default_off", "always") or squash is None,
                           "allow_merge_commit": method in ("merge", None),
                           "allow_rebase_merge": method in ("rebase_merge", "ff")})
        reads.put("mq", {"data": {"repository": {"mergeQueue": None}}})

    @staticmethod
    def _job_check(job):
        status = job.get("status")
        if status in ("success", "skipped", "manual") or (status == "failed" and job.get("allow_failure")):
            state = "pass"
        elif status in ("failed", "canceled", "canceling"):
            state = "fail"
        else:
            state = "pending"
        return {"name": CONTROL.sub("", str(job.get("name") or "")), "status": "COMPLETED" if state != "pending" else "IN_PROGRESS",
                "conclusion": {"pass": "SUCCESS", "fail": "FAILURE", "pending": ""}[state], "__typename": "CheckRun"}


class Reads:
    """The files pr-blockers.sh analyses: NAME.json for a read that worked, NAME.err for one that failed."""

    def __init__(self, out, capture):
        self.out, self.capture, self.errors = out, capture, {}

    def _path(self, stem, ext):
        return os.path.join(self.out, stem + ext)

    def put(self, stem, data):
        with open(self._path(stem, ".json"), "w") as f:
            json.dump(data, f)

    def discard(self, stem):
        for ext in (".json", ".err"):
            if os.path.exists(self._path(stem, ext)):
                os.unlink(self._path(stem, ext))

    def fail(self, stem, message):
        with open(self._path(stem, ".err"), "w") as f:
            f.write((first_line(message) or "unknown error") + "\n")

    def note_error(self, stem):
        self.fail(stem, self.errors.get(stem) or "no data")

    def get(self, stem, argv):
        """Run a read. Writes NAME.json (the output as it came) or NAME.err, and returns the parsed JSON,
        or None when the read failed or its output is not JSON (the analysis reports a bad file itself)."""
        try:
            rc, out, err = self.capture(argv)
        except ToolMissing as ex:
            rc, out, err = 127, "", str(ex)
        except CiError as ex:
            rc, out, err = 1, "", str(ex)
        if rc != 0:
            self.errors[stem] = first_line((err or "").strip().splitlines()[-1] if (err or "").strip() else "") \
                or f"{argv[0]} exited nonzero"
            self.fail(stem, self.errors[stem])
            return None
        with open(self._path(stem, ".json"), "w") as f:
            f.write(out)
        try:
            return json.loads(out)
        except ValueError:
            return None


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


class GitLabCi:
    """Jobs of a merge request's head pipeline through glab api. Never calls gh or az."""

    def __init__(self, repo=None, capture=capture_command):
        self.repo, self.capture, self.label = repo, capture, ""

    def target(self, pr):
        if not re.fullmatch(r"\d+", str(pr)):
            raise CiError(f"gitlab target must be a merge request number, not {first_line(str(pr), 60)!r}")
        self.pr, self.label = str(pr), f"gitlab merge request {pr}"
        return self

    def _get(self, endpoint):
        rc, out, err = self.capture(gitlab_ci_api(self.repo, endpoint))
        if rc != 0 or not out.strip():
            raise CiError(f"glab api exit {rc}: {first_line(err) or 'no output'}")
        return json_out(out, "glab api")

    def checks(self):
        mr = self._get(f"merge_requests/{self.pr}")
        pipeline = (mr.get("head_pipeline") or mr.get("pipeline") or {}) if isinstance(mr, dict) else {}
        if not pipeline.get("id"):
            return []
        jobs = self._get(f"pipelines/{int(pipeline['id'])}/jobs?per_page=100")
        out = []
        for job in jobs if isinstance(jobs, list) else []:
            check = GitLab._job_check(job)
            state = {"SUCCESS": "pass", "FAILURE": "fail", "": "pending"}[check["conclusion"]]
            out.append({"name": check["name"], "state": state, "link": job.get("web_url") or "", "job_id": job.get("id")})
        return out

    def failed_log(self, check, tail):
        if not isinstance(check.get("job_id"), int):
            return ""
        rc, out, err = self.capture(gitlab_ci_api(self.repo, f"jobs/{check['job_id']}/trace"))
        if rc != 0:
            raise CiError(f"glab api exit {rc}: {first_line(err)}")
        return "".join(out.splitlines(True)[-tail:])


class AzurePrCi:
    """Policy evaluations of an Azure DevOps pull request through az repos pr policy list. Never calls gh."""

    def __init__(self, org, project, capture=capture_command):
        self.org, self.project, self.capture, self.label = org, project, capture, ""

    def target(self, pr):
        if not re.fullmatch(r"\d+", str(pr)):
            raise CiError(f"azure pull request target must be digits, not {first_line(str(pr), 60)!r}")
        self.pr, self.label = str(pr), f"azure pull request {pr}"
        return self

    def checks(self):
        rc, out, err = self.capture(["az", "repos", "pr", "policy", "list", "--id", self.pr, "--org", self.org,
                                     "--output", "json"])
        if not out.strip():
            raise CiError(f"az repos pr policy list exit {rc}: {first_line(err) or 'no output'}")
        out_rows = []
        for ev in json_out(out, f"az repos pr policy list (exit {rc})"):
            conf = ev.get("configuration") or {}
            if not conf.get("isBlocking") or not conf.get("isEnabled", True):
                continue
            status = ev.get("status")
            state = "pass" if status in ("approved", "notApplicable") else "pending" if status in ("queued", "running") else "fail"
            out_rows.append({"name": CONTROL.sub("", str((conf.get("type") or {}).get("displayName") or "policy")), "state": state})
        return out_rows

    def failed_log(self, check, tail):
        return ""


PROVIDERS = {"github": GitHub, "gitlab": GitLab, "azure": Azure}
CI_PROVIDERS = {"github": GitHubCi, "gitlab": GitLabCi, "azure": AzureCi}


def ci_provider(kind, target, repo=None, org=None, project=None, capture=capture_command, pull_request=False):
    """The provider for a kind, aimed at one target (a pull request number, or an Azure run id, or with
    pull_request=True an Azure pull request id)."""
    if kind == "github":
        return GitHubCi(repo, capture).target(target)
    if kind == "gitlab":
        return GitLabCi(repo, capture).target(target)
    if kind == "azure":
        if not org:
            raise CiError("azure needs --org" + ("" if pull_request else " and --project"))
        if pull_request:
            return AzurePrCi(org, project, capture).target(target)
        if not project:
            raise CiError("azure needs --org and --project")
        return AzureCi(org, project, capture).target(target)
    raise CiError(f"{kind} is not a supported host; pass --provider github, gitlab or azure")


def pr_url(text):
    """(repository name, number) from a pull request or merge request URL of any host, else None."""
    from urllib.parse import unquote
    text = (text or "").split("#")[0].split("?")[0]
    m = re.search(r"([^/]+/[^/]+)/pull/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"https?://[^/]+/(.+?)/-/merge_requests/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"/_git/([^/]+)/pullrequest/(\d+)", text)
    return (unquote(m.group(1)), int(m.group(2))) if m else None


def capture_in(cwd, timeout=60):
    """A capture function that runs in folder cwd, with no stdin and a time limit."""
    def capture(argv, limit=timeout):
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=limit,
                               stdin=subprocess.DEVNULL)
        except OSError as ex:
            raise ToolMissing(f"cannot run {argv[0]}: {ex.strerror}")
        except subprocess.SubprocessError as ex:
            raise CiError(f"{argv[0]} did not finish: {ex}")
        return r.returncode, r.stdout, r.stderr
    return capture


def _rollup(rows):
    """GitHub statusCheckRollup items for checks given as name and pass, fail or pending."""
    return [{"conclusion": "SUCCESS"} if r["state"] == "pass" else {"conclusion": "FAILURE"} if r["state"] == "fail"
            else {"status": "PENDING"} for r in rows]


def pr_info(kind, cwd, ref="", repo=None, checks=False, timeout=10, problems=None):
    """The pull request that `ref` (a number, URL or branch name; empty means the current branch) names on the
    host `kind`, read from folder cwd. A dict: number, url, repo (the name its URL gives, "" when none), open
    (True while it is open) and, with checks=True, rollup (GitHub statusCheckRollup items; None when the host
    could not give the checks). None when the CLI is missing, a call fails, there is no such pull request or
    the answer is not what the host's CLI prints (then "unreadable" is added to the list `problems`, when given).
    `repo` is the -R value (GitHub, GitLab), else the folder's own.
    Host text only goes into argument lists."""
    from urllib.parse import quote
    capture = capture_in(cwd, timeout)
    try:
        if kind == "github":
            fields = "number,url,state,statusCheckRollup" if checks else "number,url"
            rc, out, _ = capture(["gh", "pr", "view"] + ([ref] if ref else []) + (["-R", repo] if repo else [])
                                 + ["--json", fields])
            if rc != 0:
                return None
            data = json.loads(out)
            url = str(data.get("url") or "")
            return {"number": int(data["number"]), "url": url, "repo": (pr_url(url) or ("", 0))[0],
                    "open": data.get("state") in (None, "OPEN"), "rollup": data.get("statusCheckRollup") or []}
        if kind == "gitlab":
            found = pr_url(ref)
            if found and is_url(ref):
                repo = repo or gitlab_slug(ref.split("/-/")[0])
            number = found[1] if found else int(ref.lstrip("#")) if re.fullmatch(r"#?\d+", ref) else None
            if number is not None:
                rc, out, _ = capture(gitlab_ci_api(repo, f"merge_requests/{number}"))
                mr = json.loads(out) if rc == 0 else None
            else:
                branch = ref or _current_branch(cwd)
                if not branch:
                    return None
                rc, out, _ = capture(gitlab_ci_api(repo, f"merge_requests?source_branch={quote(branch, safe='')}"
                                                         "&state=opened&per_page=1"))
                rows = json.loads(out) if rc == 0 else None
                mr = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(mr, dict) or not isinstance(mr.get("iid"), int):
                return None
            url = str(mr.get("web_url") or "")
            info = {"number": mr["iid"], "url": url, "repo": (pr_url(url) or ("", 0))[0],
                    "open": mr.get("state") == "opened", "rollup": []}
            if checks:
                try:
                    info["rollup"] = _rollup(GitLabCi(repo, capture).target(mr["iid"]).checks())
                except CiError:
                    info["rollup"] = None
            return info
        if kind == "azure":
            found = parse_azure(repo or remote_url(cwd))
            if not found:
                return None
            org, project, name = f"https://dev.azure.com/{found[0]}", found[1], found[2]
            shown = pr_url(ref)
            number = shown[1] if shown else int(ref.lstrip("#")) if re.fullmatch(r"#?\d+", ref) else None
            if number is not None:
                rc, out, _ = capture(["az", "repos", "pr", "show", "--id", str(number), "--org", org, "--output", "json"])
                pr = json.loads(out) if rc == 0 else None
            else:
                branch = ref or _current_branch(cwd)
                if not branch:
                    return None
                rc, out, _ = capture(["az", "repos", "pr", "list", "--source-branch", branch, "--status", "active",
                                      "--repository", name, "--project", project, "--org", org, "--top", "1",
                                      "--output", "json"])
                rows = json.loads(out) if rc == 0 else None
                pr = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(pr, dict) or not isinstance(pr.get("pullRequestId"), int):
                return None
            number = pr["pullRequestId"]
            info = {"number": number, "repo": name, "open": pr.get("status") == "active", "rollup": [],
                    "url": f"{org}/{quote(project)}/_git/{quote(name)}/pullrequest/{number}"}
            if checks:
                try:
                    info["rollup"] = _rollup(AzurePrCi(org, project, capture).target(number).checks())
                except CiError:
                    info["rollup"] = None
            return info
    except (ValueError, KeyError, TypeError):
        if problems is not None:
            problems.append("unreadable")
        return None
    except CiError:
        return None
    return None


def _current_branch(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True, text=True)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_name(url):
    """The REPO name the commands take, from an origin URL. Azure gives the https://dev.azure.com/ORG/PROJECT/_git/REPO
    form, which names its org; no userinfo of the origin URL is kept."""
    from urllib.parse import quote
    kind = detect(url)
    if kind == "azure":
        found = parse_azure(url)
        return "https://dev.azure.com/%s/%s/_git/%s" % tuple(quote(p, safe="") for p in found) if found else ""
    if kind == "gitlab":
        return gitlab_slug(url)
    return slug(url) if kind == "github" else ""


def github_current_repo(cwd):
    """owner/name as gh sees the folder (a remote of any name, GH_REPO), or "" when gh does not know."""
    out = None
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=cwd,
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            out = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            out = None
    return out or ""


def main(argv):
    usage = ("usage: forge.py kind [DIR] | repo [DIR] | repo-kind REPO | merged-prs REPO SINCE | commits-after REPO N ISO | "
             "failed-checks REPO SHA | escaped-refs REPO N ISO | blockers-data REPO N DIR")
    shapes = {"merged-prs": 2, "commits-after": 3, "failed-checks": 2, "escaped-refs": 3, "blockers-data": 3}
    if argv[:1] == ["kind"] and len(argv) <= 2:
        print(detect(remote_url(argv[1] if len(argv) == 2 else ".")))
        return 0
    if argv[:1] == ["repo"] and len(argv) <= 2:
        cwd = argv[1] if len(argv) == 2 else "."
        url = remote_url(cwd)
        kind = detect(url)
        name = ""
        # gh knows a GitHub repository by any remote or GH_REPO, so it is asked first unless the host is forced.
        if kind in ("github", "unknown", "gitlab") and forced_kind() in ("", "github"):
            name = github_current_repo(cwd)
        name = name or repo_name(url)
        print(name)
        return 0 if name else 1
    if argv[:1] == ["repo-kind"] and len(argv) == 2:
        print(repo_kind(argv[1]))
        return 0
    if not argv or shapes.get(argv[0]) != len(argv) - 1:
        print(usage, file=sys.stderr)
        return 2
    cmd, repo, rest = argv[0], argv[1], argv[2:]
    kind = repo_kind(repo)
    problem = missing_cli(kind)
    if problem:
        print(f"forge: {problem}", file=sys.stderr)
        return 2
    try:
        provider = for_repo(repo)
    except Unsupported as e:
        print(f"forge: {e}", file=sys.stderr)
        return 2
    if cmd == "blockers-data":
        if not re.fullmatch(r"\d+", rest[0]):
            print("forge: blockers-data needs a pull request number", file=sys.stderr)
            return 2
        try:
            nap = float(os.environ.get("PR_BLOCKERS_RETRY_SLEEP", "5"))
        except ValueError:
            nap = 5.0
        provider.blocker_reads(repo, rest[0], rest[1], sleep=nap)
        return 0
    if cmd == "merged-prs":
        prs = provider.merged_prs(repo, rest[0])
        if prs is None:
            print(f"forge: listing merged pull requests of {redact(repo)} failed", file=sys.stderr)
            return 1
        for pr in prs:
            print(f"{pr['number']}\t{pr['created_at']}\t{pr['merged_at']}\t{pr['head_sha']}")
    else:
        if cmd == "commits-after":
            got = provider.commits_after(repo, rest[0], rest[1])
        elif cmd == "failed-checks":
            got = provider.failed_checks(repo, rest[0])
        else:
            got = provider.escaped_refs(repo, rest[0], rest[1])
        print("unknown" if got is None else got)
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


FAKE_AZ_PR = FAKE_CLI + """
if a[:3] == ["repos", "pr", "list"]:
    if os.path.exists(os.path.join(d, "fail")):
        sys.stderr.write("TF400813: not authorized\\n"); sys.exit(1)
    prs = json.load(open(os.path.join(d, "prs.json")))
    skip, top = int(val("--skip")), int(val("--top"))
    print(json.dumps(prs[skip:skip + top])); sys.exit(0)
if a[:2] == ["devops", "invoke"] and val("--resource") == "pullRequestCommits":
    print(open(os.path.join(d, "commits.json")).read()); sys.exit(0)
sys.exit(9)
"""


FAKE_GLAB = FAKE_CLI + """
from urllib.parse import parse_qs, unquote, urlsplit
if a[:1] != ["api"]:
    sys.exit(9)
if os.path.exists(os.path.join(d, "fail")):
    sys.stderr.write("401 Unauthorized\\n"); sys.exit(1)
if "--hostname" in a:
    open(os.path.join(d, "host"), "w").write(val("--hostname"))
url = urlsplit(a[-1])
rest = url.path.split("/", 2)
tail = rest[2] if len(rest) > 2 else ""
q = parse_qs(url.query)
def page(name):
    items = json.load(open(os.path.join(d, name)))
    per, num = int(q.get("per_page", ["100"])[0]), int(q.get("page", ["1"])[0])
    print(json.dumps(items[(num - 1) * per:num * per])); sys.exit(0)
if tail == "merge_requests":
    page("mrs.json")
if tail.endswith("/commits") and tail.startswith("merge_requests/"):
    page("commits.json")
if tail.startswith("repository/commits/") and tail.endswith("/statuses"):
    page("statuses.json")
if tail in ("issues", "merge_requests") or tail.startswith("issues"):
    page("issues.json")
print(open(os.path.join(d, tail.split("/")[0] + ".json")).read() if os.path.exists(os.path.join(d, tail.split("/")[0] + ".json")) else "{}")
"""
FAKE_GH_READS = FAKE_CLI + """
if a[:2] == ["pr", "list"]:
    print(open(os.path.join(d, "prs.json")).read()); sys.exit(0)
if a[:1] == ["api"]:
    print(open(os.path.join(d, "count.json")).read().strip()); sys.exit(0)
if a[:2] == ["search", "issues"]:
    print(open(os.path.join(d, "count.json")).read().strip()); sys.exit(0)
sys.exit(9)
"""


def make_scene(root, tools, files):
    """A folder with fake CLIs ({name: script}) in a bin folder and data files. Returns (dir, env with PATH and FAKE_DIR)."""
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
        ci_provider("bitbucket", "5")
        check("an unknown host is not wired", "no error", "CiError")
    except CiError:
        pass


def azure_test(check, root):
    urls = (("https://dev.azure.com/org/proj/_git/repo", ("org", "proj", "repo")),
            ("https://org@dev.azure.com/org/proj/_git/repo", ("org", "proj", "repo")),
            ("https://dev.azure.com/org/My%20Proj/_git/my%20repo", ("org", "My Proj", "my repo")),
            ("git@ssh.dev.azure.com:v3/org/proj/repo", ("org", "proj", "repo")),
            ("git@ssh.dev.azure.com:v3/org/My%20Proj/repo", ("org", "My Proj", "repo")),
            ("ssh://git@ssh.dev.azure.com/v3/org/proj/repo", ("org", "proj", "repo")),
            ("https://org.visualstudio.com/proj/_git/repo", ("org", "proj", "repo")),
            ("https://org.visualstudio.com/DefaultCollection/proj/_git/repo", ("org", "proj", "repo")),
            ("https://org.visualstudio.com/_git/repo", ("org", "repo", "repo")),
            ("https://dev.azure.com/org/_git/repo", ("org", "repo", "repo")),
            ("org/proj/repo", ("org", "proj", "repo")),
            ("https://dev.azure.com/org/proj/_git/repo.git", ("org", "proj", "repo")),
            ("git@ssh.dev.azure.com:v3/org/proj/repo.git", ("org", "proj", "repo")),
            ("org@vs-ssh.visualstudio.com:v3/org/proj/repo", ("org", "proj", "repo")),
            ("org@vs-ssh.visualstudio.com:v3/org/My%20Proj/my%20repo", ("org", "My Proj", "my repo")),
            ("https://dev.azure.com/org/proj%2Fx/_git/repo", None),
            ("https://dev.azure.com/org/proj/_git/a%2Fb", None),
            ("https://dev.azure.com/org/proj", None), ("https://github.com/o/r", None), ("o/r", None), ("", None))
    for url, want in urls:
        check(f"azure parse {url!r}", parse_azure(url), want)
    check("azure slug", azure_slug("https://dev.azure.com/baryodev/DopaDopa/_git/ToGo"), "baryodev/DopaDopa/ToGo")
    check("azure slug of a GitHub URL is empty", azure_slug("https://github.com/o/r"), "")
    check("owner/name/repo is Azure when the org is known",
          isinstance(for_repo("org/proj/repo", orgs=("ORG",)), Azure), True)
    check("a bare three-part name with an unknown org is not Azure",
          [repo_kind("lib/brain/x", ("org",)), repo_kind("lib/brain/x")], ["github", "github"])
    check("a URL is Azure whatever the orgs", repo_kind("https://dev.azure.com/o/p/_git/r"), "azure")
    check("an Azure URL is Azure", isinstance(for_repo("https://dev.azure.com/o/p/_git/r"), Azure), True)
    check("owner/name stays GitHub", isinstance(for_repo("o/r"), GitHub), True)
    check("azure time", [azure_time(t) for t in ("2026-10-25T12:37:30.535651+00:00", "2026-10-25T12:37:30Z", "x", None)],
          ["2026-10-25T12:37:30Z", "2026-10-25T12:37:30Z", "x", ""])

    def pr(n, closed, ref="refs/heads/b", sha="abc"):
        return {"pullRequestId": n, "sourceRefName": ref, "creationDate": "2026-10-01T00:00:00.123456+00:00",
                "closedDate": closed, "lastMergeSourceCommit": {"commitId": sha}, "status": "completed",
                "title": "$(id)"}

    calls = []

    def fake(out):
        def run(cmd, timeout=600):
            calls.append(cmd)
            return out
        return run

    one = json.dumps([pr(7, "2026-10-02T03:04:05.1+00:00", "refs/heads/feat/x", "deadbeef")])
    got = Azure(fake(one)).merged_prs("https://dev.azure.com/org/My%20Proj/_git/r", "2026-10-01")
    argv = calls[-1]
    check("azure merged_prs has GitHub's shape", got, [{"number": 7, "head_ref": "feat/x", "head_sha": "deadbeef",
          "created_at": "2026-10-01T00:00:00Z", "merged_at": "2026-10-02T03:04:05Z"}])
    check("azure merged_prs keys match GitHub's",
          sorted(got[0]), sorted(GitHub(fake(json.dumps([{"number": 1}]))).merged_prs("o/r", "x")[0]))
    check("azure argv", argv, ["az", "repos", "pr", "list", "--status", "completed", "--repository", "r",
          "--project", "My Proj", "--org", "https://dev.azure.com/org", "--top", "100", "--skip", "0",
          "--output", "json"])
    check("azure merged_prs on a failed az", Azure(fake(None)).merged_prs("org/p/r", "2026-10-01"), None)
    check("azure merged_prs on bad JSON", Azure(fake("not json")).merged_prs("org/p/r", "2026-10-01"), None)
    check("azure merged_prs on a wrong shape", Azure(fake('[{"id": 1}]')).merged_prs("org/p/r", "2026-10-01"), None)
    try:
        Azure(fake("[]")).merged_prs("o/r", "2026-10-01")
        check("azure refuses a non-Azure name", "no error", "Unsupported")
    except Unsupported:
        pass

    d, env = make_scene(root, {"az": FAKE_AZ_PR}, {"prs.json": [pr(n, f"2026-10-{30 - n:02d}T00:00:00Z") for n in range(1, 26)]})

    def run_fake(cmd, timeout=600):
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return r.stdout if r.returncode == 0 else None

    az = Azure(run_fake, page=10)
    got = az.merged_prs("org/p/r", "2026-10-01")
    check("azure pages until a short page", [x["number"] for x in got], list(range(1, 26)))
    check("azure paged three times", len([c for c in read_calls(d) if c.startswith("az repos pr list")]), 3)
    d2, env = make_scene(root, {"az": FAKE_AZ_PR}, {"prs.json": [pr(n, f"2026-10-{30 - n:02d}T00:00:00Z") for n in range(1, 26)]})
    got = Azure(run_fake, page=10).merged_prs("org/p/r", "2026-10-20")
    check("azure stops once a page is older than since", ([x["number"] for x in got], len(read_calls(d2))),
          (list(range(1, 11)), 2))
    d3, env = make_scene(root, {"az": FAKE_AZ_PR}, {"prs.json": [pr(n, "2026-10-20T00:00:00Z") for n in range(1, 26)]})
    import io
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        got = Azure(run_fake, cap=20, page=10).merged_prs("org/p/r", "2026-10-01")
    finally:
        sys.stderr = old
    check("azure cap keeps what it read", len(got), 20)
    check("azure cap hit is reported", err.getvalue(), "forge: stopped at 20 pull requests of org/p/r, older ones are not counted\n")
    open(os.path.join(d3, "fail"), "w").close()
    check("azure failing az gives None", Azure(run_fake, page=10).merged_prs("org/p/r", "2026-10-01"), None)

    d4, env = make_scene(root, {"az": FAKE_AZ_PR}, {"prs.json": [pr(3, "2026-10-20T00:00:00Z", "refs/heads/$(touch MARK)")]})
    got = Azure(run_fake).merged_prs("org/p/r", "2026-10-01")
    check("azure branch text with $(...) is returned as text", got[0]["head_ref"], "$(touch MARK)")
    check("azure never reaches a shell", os.path.exists("MARK") or os.path.exists(os.path.join(d4, "MARK")), False)

    commits = {"count": 3, "value": [
        {"commitId": "a", "committer": {"date": "2026-10-01T00:00:00Z"}, "comment": "first"},
        {"commitId": "b", "committer": {"date": "2026-10-02T00:00:00Z"}, "comment": "fix"},
        {"commitId": "c", "committer": {"date": "2026-10-03T00:00:00Z"}, "comment": "Merge branch 'main'"}]}
    check("azure commits_after skips merges and old ones",
          Azure(fake(json.dumps(commits))).commits_after("org/p/r", 7, "2026-10-01T12:00:00Z"), 1)
    check("azure commits_after argv", calls[-1][:8], ["az", "devops", "invoke", "--area", "git", "--resource",
          "pullRequestCommits", "--org"])
    check("azure commits_after on a failed az", Azure(fake(None)).commits_after("org/p/r", 7, "x"), None)
    check("azure failed_checks is unknown", Azure(fake("1")).failed_checks("org/p/r", "abc"), None)
    check("azure escaped_refs is unknown", Azure(fake("1")).escaped_refs("org/p/r", 7, "x"), None)


    # cap: not a multiple of the page, exactly at the cap, below the page size
    def many(n, closed="2026-10-20T00:00:00Z"):
        return {"prs.json": [pr(i, closed) for i in range(1, n + 1)]}

    def listed(files, cap, page, since="2026-10-01"):
        dd, e = make_scene(root, {"az": FAKE_AZ_PR}, files)

        def run_here(cmd, timeout=600):
            r = subprocess.run(cmd, capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)
            return r.stdout if r.returncode == 0 else None

        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            got = Azure(run_here, cap=cap, page=page).merged_prs("org/p/r", since)
        finally:
            sys.stderr = old
        return got, err.getvalue(), dd

    got, msg, _ = listed(many(40), 15, 10)
    check("azure cap not a multiple of the page", (len(got), msg),
          (15, "forge: stopped at 15 pull requests of org/p/r, older ones are not counted\n"))
    got, msg, _ = listed(many(20), 20, 10)
    check("azure exactly the cap is not a cap hit", (len(got), msg), (20, ""))
    got, msg, _ = listed(many(30), 5, 10)
    check("azure a limit below the page size is respected", (len(got), msg.startswith("forge: stopped at 5 ")), (5, True))
    nulls = [dict(pr(1, "2026-10-20T00:00:00Z"), closedDate=None), pr(2, "2026-10-20T00:00:00Z"),
             dict(pr(3, "2026-10-19T00:00:00Z"), closedDate=None), pr(4, "2026-10-19T00:00:00Z")]
    got, msg, _ = listed({"prs.json": nulls}, 100, 2, "2026-10-01")
    check("azure skips a PR with no close date and keeps paging", [x["number"] for x in got], [2, 4])
    got, msg, dd = listed({"prs.json": [dict(pr(9, "x"), closedDate=None)] * 2 + [pr(5, "2026-10-20T00:00:00Z")]},
                          100, 2, "2026-10-01")
    check("a null close date is not a stop signal", [x["number"] for x in got], [5])

    # timeout and the listing questions
    seen = []

    def recording(out):
        def run(cmd, timeout=None):
            seen.append((cmd, timeout))
            return out
        return run

    Azure(recording("[]")).merged_prs("org/p/r", "2026-10-01")
    check("azure calls have a 60 second timeout", seen[-1][1], 60)
    for cfg, want in (("[defaults]\norganization = https://dev.azure.com/baryodev\n", "baryodev"),
                      ("organization = https://acme.visualstudio.com/\n", "acme"),
                      ("[defaults]\nproject = x\n", ""), ("", ""), (None, None)):
        check(f"azure default org from {cfg!r}", Azure(recording(cfg)).default_org(), want)
    check("azure projects", Azure(recording('["A", "B"]')).projects("org"), ["A", "B"])
    check("azure projects argv", seen[-1][0][:5], ["az", "devops", "project", "list", "--org"])
    check("azure repos", Azure(recording('["r"]')).repos("org", "My Proj"), ["r"])
    check("azure repos argv", seen[-1][0][:6], ["az", "repos", "list", "--project", "My Proj", "--org"])
    check("azure lists on a failed az", (Azure(recording(None)).projects("o"), Azure(recording(None)).repos("o", "p")),
          (None, None))
    check("azure lists on bad JSON", (Azure(recording("x")).projects("o"), Azure(recording('{"a": 1}')).repos("o", "p")),
          (None, None))

    # a command never waits on stdin
    with Deadline(15), open_stdin():
        check("run_command gives a command no stdin", run_command(["sh", "-c", "cat; echo done"]), "done\n")
    with Deadline(15), open_stdin():
        check("capture_command gives a command no stdin", capture_command(["sh", "-c", "cat; echo done"])[1], "done\n")


class open_stdin:
    """`with open_stdin():` makes this process's stdin a pipe that never closes, so a command that
    inherits stdin blocks (and a Deadline fails the check) instead of reading the harness's /dev/null."""

    def __enter__(self):
        self.read, self.write = os.pipe()
        self.saved = os.dup(0)
        os.dup2(self.read, 0)

    def __exit__(self, *exc):
        os.dup2(self.saved, 0)
        for fd in (self.saved, self.read, self.write):
            os.close(fd)


class Deadline:
    """`with Deadline(20):` raises TimeoutError in the block after that many seconds, so a self-test that
    would hang (a mutated loop, a real CLI waiting for a login) fails instead."""

    def __init__(self, seconds):
        self.seconds = seconds

    def __enter__(self):
        import signal

        def fire(signum, frame):
            raise TimeoutError(f"no result after {self.seconds} seconds")

        self.old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(self.seconds)

    def __exit__(self, *exc):
        import signal
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)
        return False


def guarded(check, name, seconds, fn, *args):
    """Run a test function under a deadline; a hang or a crash is one failed check, not the end of the run."""
    try:
        with Deadline(seconds):
            fn(check, *args)
    except Exception as ex:  # noqa: BLE001
        check(name, f"{type(ex).__name__}: {ex}", "finished")


def forge_cli(env, *args):
    r = subprocess.run([sys.executable, "-I", os.path.realpath(__file__), *args], capture_output=True, text=True,
                       env=env, timeout=30, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr


def userinfo_test(check, root):
    """No form of an origin URL with userinfo reaches the output of repo, kind or an error."""
    token = "TOKEN123"
    forms = ["https://%s@dev.azure.com/org/proj/_git/repo" % token, "https://user:%s@dev.azure.com/org/proj/_git/repo" % token,
             "https://%s@org.visualstudio.com/proj/_git/repo" % token, "ssh://%s@ssh.dev.azure.com/v3/org/proj/repo" % token,
             "https://%s@github.com/o/r.git" % token, "https://%s@gitlab.example.com/g/r.git" % token,
             "https://%s@bitbucket.org/o/r.git" % token]
    d, env = make_scene(root, {}, {})
    git = shutil.which("git")
    os.symlink(git, os.path.join(d, "bin", "git"))
    env = dict(env, PATH=os.path.join(d, "bin"))
    for url in forms:
        work = os.path.join(d, "w")
        subprocess.run([git, "init", "-q", work], check=True)
        subprocess.run([git, "-C", work, "remote", "add", "origin", url], check=True)
        for args in (("repo", work), ("kind", work), ("merged-prs", url, "2026-01-01"), ("failed-checks", url, "abc")):
            rc, out, err = forge_cli(env, *args)
            check(f"no token in forge {args[0]} for {url.split('@')[-1]}", token in out + err, False)
        shutil.rmtree(work)


def missing_cli_test(check, root):
    """A host whose CLI is not on PATH gives one line naming the CLI to install, and exit 2."""
    import tempfile
    with tempfile.TemporaryDirectory() as empty:
        env = dict(os.environ, PATH=empty)
        for repo, cli in (("https://gitlab.com/g/r", "glab"), ("https://dev.azure.com/o/p/_git/r", "az"),
                          ("o/r", "gh")):
            rc, out, err = forge_cli(env, "merged-prs", repo, "2026-10-01")
            check(f"missing {cli}: exit", rc, 2)
            check(f"missing {cli}: one line naming the CLI", (len(err.splitlines()), f"{cli} is not installed" in err),
                  (1, True))
            check(f"missing {cli}: nothing on stdout", out, "")


GLAB_MRS = [{"iid": 11, "title": "$(id)", "source_branch": "$(touch MARK)", "sha": "cafe01", "state": "merged",
             "created_at": "2026-10-01T00:00:00.000Z", "merged_at": "2026-10-03T08:00:00.000+02:00",
             "updated_at": "2026-10-03T08:00:00.000+02:00"},
            {"iid": 12, "title": "two", "source_branch": "b", "sha": "cafe02", "state": "merged",
             "created_at": "2026-10-02T00:00:00Z", "merged_at": "2026-10-04T00:00:00Z", "updated_at": "2026-10-04T00:00:00Z"}]
TSV_ROW = re.compile(r"\d+\t\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\t\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\t[0-9a-f]*")


def hosts_test(check, root):
    """The same commands, against a fake CLI of each host on PATH, print the same TSV shape."""
    gh_prs = [{"number": 11, "headRefName": "$(touch MARK)", "headRefOid": "cafe01", "createdAt": "2026-10-01T00:00:00Z",
               "mergedAt": "2026-10-03T06:00:00Z"}]
    az_prs = [{"pullRequestId": 11, "sourceRefName": "refs/heads/b", "creationDate": "2026-10-01T00:00:00.5+00:00",
               "closedDate": "2026-10-03T06:00:00.5+00:00", "lastMergeSourceCommit": {"commitId": "cafe01"},
               "status": "completed", "title": "$(id)"}]
    commits_az = {"count": 2, "value": [
        {"commitId": "a", "committer": {"date": "2026-10-02T00:00:00Z"}, "comment": "fix"},
        {"commitId": "b", "committer": {"date": "2026-10-02T01:00:00Z"}, "comment": "Merge branch 'main'"}]}
    commits_gl = [{"id": "a", "title": "fix", "committed_date": "2026-10-02T00:00:00.000Z"},
                  {"id": "b", "title": "Merge branch 'main'", "committed_date": "2026-10-02T01:00:00.000Z"},
                  {"id": "c", "title": "early", "committed_date": "2026-09-30T00:00:00.000Z"}]
    statuses = [{"status": "failed", "allow_failure": False}, {"status": "success"},
                {"status": "failed", "allow_failure": True}]
    issues = [{"iid": 30, "description": "fix for !11"}, {"iid": 31, "description": "unrelated #11 text"},
              {"iid": 32, "description": "Fixes #11"}]
    scenes = {
        "github": ("o/r", {"gh": FAKE_GH_READS}, {"prs.json": gh_prs, "count.json": "2"}),
        "gitlab": ("https://gitlab.example.com/g/sub/r", {"glab": FAKE_GLAB},
                   {"mrs.json": GLAB_MRS, "commits.json": commits_gl, "statuses.json": statuses, "issues.json": issues}),
        "azure": ("https://dev.azure.com/org/p/_git/r", {"az": FAKE_AZ_PR},
                  {"prs.json": az_prs, "commits.json": commits_az}),
    }
    shapes = {}
    for kind, (repo, tools, files) in scenes.items():
        d, env = make_scene(root, tools, files)
        env = dict(env, PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"])
        rc, out, err = forge_cli(env, "merged-prs", repo, "2026-10-01")
        rows = out.splitlines()
        check(f"{kind} merged-prs exit and rows", (rc, len(rows) >= 1, all(TSV_ROW.fullmatch(r) for r in rows)),
              (0, True, True))
        check(f"{kind} merged-prs first row", rows[0].split("\t")[0:1], ["11"])
        shapes[kind] = [len(r.split("\t")) for r in rows]
        for cmd, args in (("commits-after", ["11", "2026-10-01T12:00:00Z"]), ("failed-checks", ["cafe01"]),
                          ("escaped-refs", ["11", "2026-10-03T00:00:00Z"])):
            rc, out, err = forge_cli(env, cmd, repo, *args)
            check(f"{kind} {cmd} prints one count or unknown", (rc, re.fullmatch(r"\d+|unknown", out.strip()) is not None),
                  (0, True))
        check(f"{kind} text from the host never reaches a shell", os.path.exists(os.path.join(d, "MARK")), False)
        check(f"{kind} text from the host never reaches the working folder", os.path.exists("MARK"), False)
    check("every host prints four columns", sorted({n for v in shapes.values() for n in v}), [4])
    d, env = make_scene(root, scenes["gitlab"][1], scenes["gitlab"][2])
    rc, out, err = forge_cli(env, "merged-prs", "https://gitlab.example.com/g/sub/r", "2026-10-01")
    check("gitlab merged-prs rows", out.splitlines(), [
        "11\t2026-10-01T00:00:00Z\t2026-10-03T06:00:00Z\tcafe01", "12\t2026-10-02T00:00:00Z\t2026-10-04T00:00:00Z\tcafe02"])
    check("gitlab host is passed to glab", open(os.path.join(d, "host")).read(), "gitlab.example.com")
    check("gitlab project path is url-encoded", "projects/g%2Fsub%2Fr/merge_requests" in "\n".join(read_calls(d)), True)
    rc, out, err = forge_cli(env, "commits-after", "https://gitlab.example.com/g/sub/r", "11", "2026-10-01T12:00:00Z")
    check("gitlab commits-after skips merges and old commits", out.strip(), "1")
    rc, out, err = forge_cli(env, "failed-checks", "https://gitlab.example.com/g/sub/r", "cafe01")
    check("gitlab failed-checks skips allowed failures", out.strip(), "1")
    rc, out, err = forge_cli(env, "escaped-refs", "https://gitlab.example.com/g/sub/r", "11", "2026-10-03T00:00:00Z")
    check("gitlab escaped-refs counts fixes that name the merge request", out.strip(), "2")
    open(os.path.join(d, "fail"), "w").close()
    rc, out, err = forge_cli(env, "merged-prs", "https://gitlab.example.com/g/sub/r", "2026-10-01")
    check("gitlab failing glab gives exit 1", (rc, out), (1, ""))
    rc, out, err = forge_cli(env, "commits-after", "https://gitlab.example.com/g/sub/r", "11", "x")
    check("gitlab failing glab gives unknown, not 0", out.strip(), "unknown")

    # LEAN_FORGE picks the host for a name that says nothing.
    d, env = make_scene(root, scenes["gitlab"][1], scenes["gitlab"][2])
    rc, out, err = forge_cli(dict(env, LEAN_FORGE="gitlab"), "merged-prs", "g/sub/r", "2026-10-01")
    check("LEAN_FORGE=gitlab reads a bare name through glab", (rc, len(out.splitlines())), (0, 2))
    rc, out, err = forge_cli(dict(env, LEAN_FORGE="nonsense"), "merged-prs", "o/r", "2026-10-01")
    check("a bad LEAN_FORGE is said once and ignored", ("ignored" in err, rc != 2 or "not installed" in err), (True, True))

    # The caps.
    many = [dict(GLAB_MRS[1], iid=100 + i) for i in range(25)]
    d, env = make_scene(root, {"glab": FAKE_GLAB}, {"mrs.json": many})
    got = GitLab(lambda cmd, timeout=600: (lambda r: r.stdout if r.returncode == 0 else None)(
        subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)), cap=20, page=10)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        prs = got.merged_prs("https://gitlab.example.com/g/r", "2026-10-01")
    check("gitlab cap keeps what it read", len(prs), 20)
    check("gitlab cap hit is reported", err.getvalue(),
          "forge: stopped at 20 merged pull requests of gitlab.example.com/g/r, older ones are not counted\n")


def gitlab_ci_test(check, root):
    jobs = [{"id": 5, "name": "build", "status": "success", "web_url": "u1"},
            {"id": 6, "name": "test", "status": "failed", "web_url": "u2"},
            {"id": 7, "name": "lint", "status": "failed", "allow_failure": True},
            {"id": 8, "name": "deploy", "status": "running"}]
    d, env = make_scene(root, {"glab": FAKE_GLAB, "gh": "#!/bin/sh\necho gh >> \"$FAKE_DIR/gh-called\"\n"},
                        {"merge_requests.json": {"iid": 3, "head_pipeline": {"id": 99}}, "pipelines.json": jobs,
                         "jobs.json": "line1\nline2\nline3"})
    ci = ci_provider("gitlab", "3", repo="https://gitlab.example.com/g/r", capture=fake_capture(env))
    check("gitlab ci label", ci.label, "gitlab merge request 3")
    got = ci.checks()
    check("gitlab ci states", [(c["name"], c["state"]) for c in got],
          [("build", "pass"), ("test", "fail"), ("lint", "pass"), ("deploy", "pending")])
    check("gitlab ci failed log tail", ci.failed_log(got[1], 2), "line2\nline3\n")
    check("gitlab ci never calls gh", os.path.exists(os.path.join(d, "gh-called")), False)
    try:
        ci_provider("gitlab", "$(id)")
        check("gitlab ci refuses a target that is not a number", "no error", "CiError")
    except CiError:
        pass
    az = ci_provider("azure", "4", org="https://dev.azure.com/o", pull_request=True,
                     capture=lambda argv, timeout=600: (0, json.dumps([
                         {"status": "approved", "configuration": {"isBlocking": True, "isEnabled": True,
                                                                   "type": {"displayName": "Minimum number of reviewers"}}},
                         {"status": "rejected", "configuration": {"isBlocking": True, "isEnabled": True,
                                                                   "type": {"displayName": "Build"}}},
                         {"status": "queued", "configuration": {"isBlocking": True, "isEnabled": True,
                                                                 "type": {"displayName": "Status"}}},
                         {"status": "rejected", "configuration": {"isBlocking": False, "isEnabled": True,
                                                                   "type": {"displayName": "Optional"}}}]), ""))
    check("azure pull request ci states", [(c["name"], c["state"]) for c in az.checks()],
          [("Minimum number of reviewers", "pass"), ("Build", "fail"), ("Status", "pending")])


def pr_info_test(check, root):
    """pr_info reads one pull request and its checks the same way on each host, and calls only that host's CLI."""
    gh = FAKE_CLI + """
if a[:2] == ["pr", "view"]:
    print(open(os.path.join(d, "gh.json")).read()); sys.exit(0)
sys.exit(9)
"""
    glab = FAKE_CLI + """
u = a[-1]
if "merge_requests?source_branch=" in u:
    open(os.path.join(d, "branch"), "w").write(u)
    print(json.dumps([json.load(open(os.path.join(d, "mr.json")))])); sys.exit(0)
if u.endswith("/merge_requests/7"):
    print(open(os.path.join(d, "mr.json")).read()); sys.exit(0)
if u.endswith("/pipelines/99/jobs?per_page=100"):
    print(open(os.path.join(d, "jobs.json")).read()); sys.exit(0)
sys.exit(9)
"""
    az = FAKE_CLI + """
if a[:3] == ["repos", "pr", "list"]:
    open(os.path.join(d, "branch"), "w").write(val("--source-branch"))
    print(json.dumps([json.load(open(os.path.join(d, "pr.json")))])); sys.exit(0)
if a[:3] == ["repos", "pr", "show"]:
    print(open(os.path.join(d, "pr.json")).read()); sys.exit(0)
if a[:4] == ["repos", "pr", "policy", "list"]:
    print(open(os.path.join(d, "policies.json")).read()); sys.exit(0)
sys.exit(9)
"""
    mr = {"iid": 7, "state": "opened", "web_url": "https://gitlab.example.com/g/sub/r/-/merge_requests/7",
          "head_pipeline": {"id": 99}, "title": "$(id)"}
    jobs = [{"id": 1, "name": "build", "status": "success"}, {"id": 2, "name": "test", "status": "failed"},
            {"id": 3, "name": "deploy", "status": "running"}]
    pr = {"pullRequestId": 4, "status": "active", "title": "$(id)"}
    pol = [{"status": "approved", "configuration": {"isBlocking": True, "isEnabled": True, "type": {"displayName": "Reviewers"}}}]
    d, env = make_scene(root, {"gh": gh, "glab": glab, "az": az},
                        {"gh.json": {"number": 3, "url": "https://github.com/o/r/pull/3", "state": "OPEN",
                                     "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
                         "mr.json": mr, "jobs.json": jobs, "pr.json": pr, "policies.json": pol})
    old = os.environ.copy()
    os.environ.update(env)
    try:
        got = pr_info("github", root, "feat/x", checks=True)
        check("github pr_info", (got["number"], got["repo"], got["open"], got["rollup"]),
              (3, "o/r", True, [{"conclusion": "SUCCESS"}]))
        got = pr_info("gitlab", root, "feat/$(id)", repo="gitlab.example.com/g/sub/r", checks=True)
        check("gitlab pr_info", (got["number"], got["repo"], got["open"], [r.get("conclusion", r.get("status")) for r in got["rollup"]]),
              (7, "g/sub/r", True, ["SUCCESS", "FAILURE", "PENDING"]))
        check("gitlab branch name is encoded, not run", "source_branch=feat%2F%24%28id%29" in open(os.path.join(d, "branch")).read(), True)
        check("gitlab by number", (pr_info("gitlab", root, "7", repo="g/r") or {}).get("number"), 7)
        check("gitlab by url", (pr_info("gitlab", root, "https://gitlab.example.com/g/sub/r/-/merge_requests/7") or {}).get("number"), 7)
        check("gitlab no such merge request", pr_info("gitlab", root, "8", repo="g/r"), None)
        got = pr_info("azure", root, "feat/x", repo="https://dev.azure.com/o/p/_git/r", checks=True)
        check("azure pr_info", (got["number"], got["repo"], got["open"], got["rollup"]),
              (4, "r", True, [{"conclusion": "SUCCESS"}]))
        check("azure branch is passed whole", open(os.path.join(d, "branch")).read(), "feat/x")
        check("azure by id", (pr_info("azure", root, "4", repo="o/p/r") or {}).get("number"), 4)
        check("azure with no organization", pr_info("azure", root, "4", repo="nonsense"), None)
        calls = " ".join(read_calls(d))
        check("pr_info reads shapes only, no shell ran the title", os.path.exists(os.path.join(root, "MARK")), False)
        check("each host called only its CLI", ("gh pr view" in calls, "glab api" in calls, "az repos" in calls), (True, True, True))
    finally:
        os.environ.clear()
        os.environ.update(old)
    empty = os.path.join(root, "nobin")
    os.makedirs(empty, exist_ok=True)
    old_path, os.environ["PATH"] = os.environ["PATH"], empty
    try:
        check("missing CLI gives None", [pr_info(k, root, "1") for k in KINDS], [None, None, None])
    finally:
        os.environ["PATH"] = old_path
    check("pr_url forms", (pr_url("https://github.com/o/r/pull/6"), pr_url("https://h.com/g/s/r/-/merge_requests/7?x=1"),
                           pr_url("https://dev.azure.com/o/p/_git/My%20Repo/pullrequest/4"), pr_url("12")),
          (("o/r", 6), ("g/s/r", 7), ("My Repo", 4), None))


def blockers_test(check, root):
    """blocker_reads of Azure and GitLab write GitHub-shaped files; a failed read leaves .err, never a guess."""
    import tempfile
    pr = {"pullRequestId": 4, "status": "active", "isDraft": True, "mergeStatus": "succeeded",
          "targetRefName": "refs/heads/main", "lastMergeSourceCommit": {"commitId": "cafe"},
          "reviewers": [{"displayName": "al\x1b[2Jice", "vote": -10}], "title": "$(id)"}
    policies = [{"status": "rejected", "configuration": {"isBlocking": True, "isEnabled": True, "type": {"displayName": "Build"},
                                                          "settings": {"displayName": "ci"}}},
                {"status": "approved", "configuration": {"isBlocking": True, "isEnabled": True, "settings": {"minimumApproverCount": 2},
                                                          "type": {"id": "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd", "displayName": "Min"}}}]
    threads = {"value": [{"status": "active", "comments": [{"author": {"displayName": "bot"}, "commentType": "text"}],
                          "threadContext": {"filePath": "/a.py", "rightFileStart": {"line": 3}}},
                         {"status": "closed", "comments": [{"author": {"displayName": "x"}, "commentType": "text"}]}]}

    def capture(argv, timeout=600):
        text = " ".join(argv)
        if argv[:4] == ["az", "repos", "pr", "show"]:
            return 0, json.dumps(pr), ""
        if "policy list" in text:
            return 0, json.dumps(policies), ""
        if "pullRequestThreads" in text:
            return 0, json.dumps(threads), ""
        return 1, "", "unexpected " + text

    with tempfile.TemporaryDirectory(dir=root) as out:
        Azure().blocker_reads("https://dev.azure.com/org/proj/_git/repo", "4", out, capture=capture, sleep=0)
        got = json.load(open(os.path.join(out, "pr.json")))
        check("azure blocker pr state", (got["state"], got["isDraft"], got["reviewDecision"], got["baseRefName"]),
              ("OPEN", True, "CHANGES_REQUESTED", "main"))
        check("azure blocker checks", [(c["name"], c["conclusion"]) for c in got["statusCheckRollup"]],
              [("Build: ci", "FAILURE"), ("Min", "SUCCESS")])
        check("azure control characters are stripped", got["latestReviews"][0]["author"]["login"].count("\x1b"), 0)
        rules = json.load(open(os.path.join(out, "rules.json")))
        check("azure approver count", rules[0]["parameters"]["required_approving_review_count"], 2)
        nodes = json.load(open(os.path.join(out, "threads.json")))["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        check("azure open threads only", [(n["path"], n["line"]) for n in nodes], [("/a.py", 3)])
        check("azure branch policy names", json.load(open(os.path.join(out, "branch.json")))["protection"]
              ["required_status_checks"]["contexts"], ["Build: ci", "Min"])

    def broken(argv, timeout=600):
        return 1, "", "TF400813: not authorized"

    with tempfile.TemporaryDirectory(dir=root) as out:
        Azure().blocker_reads("org/proj/repo", "4", out, capture=broken, sleep=0)
        check("azure failed read leaves an error file", (os.path.exists(os.path.join(out, "pr.json")),
              open(os.path.join(out, "pr.err")).read().strip()), (False, "TF400813: not authorized"))
    with tempfile.TemporaryDirectory(dir=root) as out:
        GitLab().blocker_reads("https://gitlab.com/g/r", "4", out, capture=broken, sleep=0)
        check("gitlab failed read leaves an error file", os.path.exists(os.path.join(out, "pr.err")), True)


def unknown_cli_test(check, root):
    import tempfile
    with tempfile.TemporaryDirectory() as bindir:
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(gh, 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        me = os.path.abspath(__file__)
        for cmd, rest in (("failed-checks", ["abc"]), ("commits-after", ["7", "2026-10-01T00:00:00Z"]),
                          ("escaped-refs", ["7", "2026-10-01T00:00:00Z"])):
            r = subprocess.run([sys.executable, "-I", me, cmd, "o/r"] + rest, capture_output=True, text=True, env=env)
            check(f"{cmd} prints unknown when gh fails", (r.returncode, r.stdout), (0, "unknown\n"))


def self_test():
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    for url, want in (("https://github.com/o/r.git", "github"), ("git@github.com:o/r.git", "github"),
                      ("ssh://git@github.com/o/r.git", "github"), ("https://gitlab.com/g/r", "gitlab"),
                      ("https://gitlab.example.org/g/r", "gitlab"), ("https://dev.azure.com/org/p/_git/r", "azure"),
                      ("https://org.visualstudio.com/p/_git/r", "azure"), ("https://bitbucket.org/o/r", "unknown"),
                      ("https://mygitlabfoo.com/g/r", "unknown"), ("https://gitlab.mycorp.com/g/r", "gitlab"),
                      ("https://notgitlab.com/g/r", "unknown"), ("git@gitlab.mycorp.com:g/r.git", "gitlab"),
                      ("", "unknown")):
        check(f"kind of {url!r}", kind_of(url), want)
    os.environ["LEAN_FORGE"] = "gitlab"
    try:
        check("LEAN_FORGE covers an odd host", detect("https://code.mycorp.com/g/r"), "gitlab")
    finally:
        del os.environ["LEAN_FORGE"]

    token = "TOKEN123"
    for url, want in (("https://%s@dev.azure.com/org/proj/_git/repo" % token, "https://dev.azure.com/org/proj/_git/repo"),
                      ("https://user:%s@dev.azure.com/org/proj/_git/My%%20Repo" % token,
                       "https://dev.azure.com/org/proj/_git/My%20Repo"),
                      ("https://%s@org.visualstudio.com/proj/_git/repo" % token, "https://dev.azure.com/org/proj/_git/repo"),
                      ("https://org.visualstudio.com/DefaultCollection/proj/_git/repo", "https://dev.azure.com/org/proj/_git/repo"),
                      ("ssh://%s@ssh.dev.azure.com/v3/org/proj/repo" % token, "https://dev.azure.com/org/proj/_git/repo"),
                      ("git@ssh.dev.azure.com:v3/org/proj/repo", "https://dev.azure.com/org/proj/_git/repo"),
                      ("https://%s@github.com/o/r.git" % token, "o/r"),
                      ("https://%s@gitlab.example.com/g/s/r.git" % token, "gitlab.example.com/g/s/r")):
        check(f"repo_name {want}", repo_name(url), want)
    check("redact strips userinfo", redact("see https://u:%s@h.example/x and http://%s@h/y" % (token, token)),
          "see https://h.example/x and http://h/y")

    check("owner/name is GitHub", isinstance(for_repo("o/r"), GitHub), True)
    check("a GitHub URL is GitHub", isinstance(for_repo("https://github.com/o/r"), GitHub), True)
    check("a GitLab URL is GitLab", isinstance(for_repo("https://gitlab.com/g/r"), GitLab), True)
    try:
        for_repo("https://bitbucket.org/o/r")
        check("an unknown host is refused", "no error", "Unsupported")
    except Unsupported:
        pass

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
           "--limit", "201", "--json", "number,headRefName,headRefOid,createdAt,mergedAt"])
    check("merged_prs on a failed gh", GitHub(fake(None)).merged_prs("o/r", "2026-10-01"), None)
    check("merged_prs on bad JSON", GitHub(fake("not json")).merged_prs("o/r", "2026-10-01"), None)

    for name in ("failed_checks", "commits_after", "escaped_refs"):
        args = ("o/r", "abc") if name == "failed_checks" else ("o/r", 7, "2026-10-01T00:00:00Z")
        check(f"{name} on a failed gh is unknown", getattr(GitHub(fake(None)), name)(*args), None)
        check(f"{name} on odd output is unknown", getattr(GitHub(fake("oops")), name)(*args), None)
        check(f"{name} parses a count", getattr(GitHub(fake("3\n")), name)(*args), 3)
        check(f"{name} keeps a real zero", getattr(GitHub(fake("0\n")), name)(*args), 0)
    check("commits_after sums pages", GitHub(fake("2\n1\n")).commits_after("o/r", 7, "x"), 3)
    check("failed_checks sums pages", GitHub(fake("2\n1\n")).failed_checks("o/r", "abc"), 3)
    GitHub(fake("0\n")).failed_checks("o/r", "abc")
    check("failed_checks reads every page", "--paginate" in calls[-1], True)
    check("count of nothing is unknown", count(""), None)

    full = json.dumps([{"number": n, "headRefName": "b", "headRefOid": "a", "createdAt": "x", "mergedAt": "y"}
                       for n in range(3)])
    err = io.StringIO()
    more = GitHub(fake(full))
    with contextlib.redirect_stderr(err):
        check("merged_prs past the limit is trimmed", len(more.merged_prs("o/r", "2026-10-01", limit=2)), 2)
    check("merged_prs asks for one more than the limit", calls[-1][calls[-1].index("--limit") + 1], "3")
    check("merged_prs says it was cut", err.getvalue(),
          "forge: stopped at 2 merged pull requests of o/r, older ones are not counted\n")
    check("merged_prs remembers the cut", more.cut, True)
    for exact in (3, 4):
        err = io.StringIO()
        quiet = GitHub(fake(full))
        with contextlib.redirect_stderr(err):
            check(f"merged_prs with limit {exact} returns all", len(quiet.merged_prs("o/r", "2026-10-01", limit=exact)), 3)
        check(f"merged_prs with exactly the limit or fewer is silent ({exact})", (err.getvalue(), quiet.cut), ("", False))

    seen = GitHub(fake("2\n"))
    check("escaped_refs asks for a bounded list", (seen.escaped_refs("o/r", 7, "x"), "--limit" in calls[-1]), (2, True))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        check("escaped_refs past its limit is capped", GitHub(fake("101\n")).escaped_refs("o/r", 7, "x"), 100)
    check("escaped_refs says it was cut", err.getvalue(),
          "forge: escaped_refs for o/r#7 stopped at 100 issues, more were not counted\n")

    guarded(check, "unknown on the command line", 60, unknown_cli_test, None)

    import tempfile
    with tempfile.TemporaryDirectory() as root:
        guarded(check, "missing CLI", 60, missing_cli_test, root)
        guarded(check, "userinfo", 60, userinfo_test, root)
        guarded(check, "hosts", 120, hosts_test, root)
        guarded(check, "gitlab ci", 60, gitlab_ci_test, root)
        guarded(check, "pr_info", 60, pr_info_test, root)
        guarded(check, "blocker reads", 60, blockers_test, root)
        guarded(check, "github ci", 60, github_ci_test, root)
        guarded(check, "azure ci", 60, azure_ci_test, root)
        guarded(check, "azure", 120, azure_test, root)

    for f in fails:
        print("FAIL", f)
    print("self-test failed" if fails else "self-test passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(main(sys.argv[1:]))
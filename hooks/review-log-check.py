#!/usr/bin/env python3
"""Refuse to mark a pull request ready or merge it when no review is logged for it.

PreToolUse hook on Bash, for gh pr ready, gh pr merge, and az repos pr update with
--draft false or --status completed. The retro reads the reviews table in lean.db, which
log-review.sh fills by pull request number. A review that was done but never logged leaves
the retro with no data for that change.

The pull request number comes from the argument (a number, #number or URL). With no argument,
or a branch name, the hook asks `gh pr view --json number,url` once (5 seconds; any failure
allows). For az it is --id. The hook then looks in the reviews table of lean.db for that number.

The repo column is not consistent in practice (BaryoDev/barakoCMS, barakoCMS and barakocms
all exist), so the repo is compared loosely: the last path part, case-insensitive, against the
repo from -R, the URL, gh pr view, or the origin remote. When none of those is known, the
number alone decides.

The database is LEAN_DB (default ~/.lean/lean.db), as in lean-stats. LEAN_STATS=off, no
database file, no reviews table, or any error allows. --help and -h allow.

  LEAN_SKIP_REVIEW_LOG_CHECK=1   turn it off (environment, settings env, or inline)
  review-log-check.py --self-test
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SKIP = "LEAN_SKIP_REVIEW_LOG_CHECK"
GH_GLOBAL_VALUE = {"-R", "--repo", "--hostname"}
GH_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject",
                  "-A", "--author-email", "--match-head-commit"}
AZ_VALUE_FLAGS = {"--id", "--status", "--draft", "--organization", "--org", "--detect", "--title",
                  "--description", "--auto-complete", "--squash", "--delete-source-branch",
                  "--bypass-policy", "--bypass-policy-reason", "--merge-commit-message",
                  "--transition-work-items", "--output", "-o", "--query", "--subscription"}
DENY = (
    "Pull request {name} has no review logged in lean.db, which the retro reads. Run the review "
    "(adversarial-review), then log it with "
    "`${{CLAUDE_PLUGIN_ROOT}}/skills/adversarial-review/log-review.sh {repo} {number} <confirmed> "
    "<refuted> <blockers> \"<categories>\" \"<caught elsewhere first>\"`. "
    "If the change was deliberately not reviewed, set " + SKIP + "=1 for this command.")


def last_part(repo):
    """The comparable form of a repo string: last path part, lower case, no .git."""
    text = (repo or "").strip().rstrip("/")
    text = re.split(r"[/:]", text)[-1] if text else ""
    return text[:-4].lower() if text.lower().endswith(".git") else text.lower()


def full_name(url):
    """owner/repo from a github remote or pull request URL, else the last two path parts."""
    found = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?(?:/pull/\d+.*)?/?$", url or "")
    return found.group(1) if found else ""


def parse_gh(words):
    """(action, argument, repo flag, help) for gh pr ready|merge, else None."""
    i, repo = 0, None
    while i < len(words) and words[i] != "pr":
        w = words[i]
        if w in GH_GLOBAL_VALUE and i + 1 < len(words):
            if w in ("-R", "--repo"):
                repo = words[i + 1]
            i += 2
        elif w.startswith("--repo="):
            repo, i = w.split("=", 1)[1], i + 1
        elif w.startswith("-R") and len(w) > 2:
            repo, i = w[2:], i + 1
        elif w.startswith("-"):
            i += 1
        else:
            return None
    if i + 1 >= len(words) or words[i] != "pr" or words[i + 1] not in ("ready", "merge"):
        return None
    action, arg, helped = words[i + 1], None, False
    j = i + 2
    while j < len(words):
        w = words[j]
        if w in ("-h", "--help"):
            helped = True
        elif w in ("-R", "--repo") and j + 1 < len(words):
            repo, j = words[j + 1], j + 1
        elif w.startswith("--repo="):
            repo = w.split("=", 1)[1]
        elif w in GH_VALUE_FLAGS:
            j += 1
        elif w.startswith("-"):
            pass
        elif arg is None:
            arg = w
        j += 1
    return action, arg, repo, helped


def parse_az(words):
    """(id, help) for az repos pr update that completes or undrafts, else None."""
    if words[:3] != ["repos", "pr", "update"]:
        return None
    pairs, flags, helped = {}, set(), False
    j = 3
    while j < len(words):
        w = words[j]
        if w in ("-h", "--help"):
            helped = True
        elif "=" in w and w.startswith("--"):
            key, value = w.split("=", 1)
            pairs[key] = value
        elif w in AZ_VALUE_FLAGS and j + 1 < len(words):
            pairs[w] = words[j + 1]
            j += 1
        elif w.startswith("-"):
            flags.add(w)
        j += 1
    if pairs.get("--draft", "").lower() != "false" and pairs.get("--status", "").lower() != "completed":
        return None
    return pairs.get("--id"), helped


def view(cwd, arg, repo):
    """(number, url) from gh pr view, else None."""
    cmd = ["gh", "pr", "view"] + ([arg] if arg else []) + (["-R", repo] if repo else []) + ["--json", "number,url"]
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        return int(data["number"]), str(data.get("url") or "")
    except Exception:
        return None


def target(seg, kind, parsed):
    """(number, repo name as owner/repo or '') for the pull request this command acts on, else None."""
    if kind == "az":
        number = parsed[0]
        if not number or not number.isdigit():
            return None
        return int(number), full_name(hookkit.git(seg.cwd, "remote", "get-url", "origin") or "")
    action, arg, repo, helped = parsed
    if arg:
        found = re.fullmatch(r"#?(\d+)", arg)
        if found:
            return int(found.group(1)), repo or full_name(hookkit.git(seg.cwd, "remote", "get-url", "origin") or "")
        found = re.search(r"([^/]+/[^/]+)/pull/(\d+)", arg)
        if found:
            return int(found.group(2)), found.group(1)
    got = view(seg.cwd, arg, repo)
    if not got:
        return None
    return got[0], full_name(got[1]) or repo or ""


def logged(number, repo):
    """False only when the database is readable, has a reviews table, and holds no row for the pull request."""
    import leanstats
    if leanstats.off():
        return True
    path = leanstats.db_path()
    if not os.path.isfile(path):
        return True
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=2)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone():
            return True
        rows = [r[0] for r in conn.execute("SELECT repo FROM reviews WHERE pr = ?", (number,))]
    finally:
        conn.close()
    want = last_part(repo)
    return any(not want or last_part(r) == want for r in rows)


def message(command, cwd):
    """The deny text for the first command whose pull request has no logged review, else None."""
    for seg in hookkit.segments(command, cwd):
        if hookkit.disabled(SKIP, seg) or not seg.words:
            continue
        prog = os.path.basename(seg.words[0])
        try:
            if prog == "gh":
                parsed = parse_gh(seg.words[1:])
                if not parsed or parsed[3]:
                    continue
                kind = "gh"
            elif prog == "az":
                parsed = parse_az(seg.words[1:])
                if not parsed or parsed[1]:
                    continue
                kind = "az"
            else:
                continue
            found = target(seg, kind, parsed)
            if not found or logged(*found):
                continue
        except Exception:
            continue
        number, repo = found
        return DENY.format(name=f"{repo}#{number}" if repo else f"#{number}", repo=repo or "<owner/repo>", number=number)
    return None


def main():
    if hookkit.disabled(SKIP):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command")
    if not isinstance(command, str) or "pr" not in command or not any(w in command for w in ("ready", "merge", "update")):
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        print(text, file=sys.stderr)
        return 2
    return 0


def self_test():
    import stat
    import tempfile

    import leanstats

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if bool(got) != want:
            print(f"FAIL {name}: got {got!r}")
            failures += 1

    for var in (SKIP, "LEAN_STATS", "LEAN_DB"):
        os.environ.pop(var, None)
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "work")
        os.mkdir(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://github.com/o/r.git"],
                       check=True, capture_output=True)
        db = os.path.join(tmp, "lean.db")
        os.environ["LEAN_DB"] = db
        check("missing db allows", message("gh pr merge 5", repo), False)
        conn = leanstats.connect(db)
        conn.execute("INSERT INTO reviews (date, repo, pr) VALUES ('2026-01-01', 'BaryoDev/R', 5)")
        conn.execute("INSERT INTO reviews (date, repo, pr) VALUES ('2026-01-01', 'other/zzz', 9)")
        conn.close()
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        fake = os.path.join(bindir, "gh")
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")

        def fake_gh(body):
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)

        fake_gh("exit 1")
        check("logged number allows", message("gh pr merge 5", repo), False)
        check("logged ready allows", message("gh pr ready 5", repo), False)
        check("unlogged merge denies", message("gh pr merge 6", repo), True)
        check("unlogged ready denies", message("gh pr ready 6", repo), True)
        deny = message("gh pr merge 6 --squash", repo) or ""
        check("deny names the pr", "o/r#6" in deny, True)
        check("deny gives the log command", "log-review.sh o/r 6 <confirmed>" in deny, True)
        check("deny names the skip variable", SKIP in deny, True)
        check("flag value is not the number", message("gh pr merge --body 5 6", repo), True)
        check("--repo= form denies", message("gh pr ready 5 --repo=o/zzz", repo), True)
        check("global --repo= form denies", message("gh --repo=o/zzz pr ready 5", repo), True)
        check("global -R denies", message("gh -R o/r pr ready 6", repo), True)
        check("global --repo denies", message("gh --repo o/r pr merge 6", repo), True)
        check("-R after subcommand denies", message("gh pr merge 6 -R o/r", repo), True)
        check("global -R logged allows", message("gh -R o/r pr ready 5", repo), False)
        check("-R names the repo in the deny", "x/r#6" in (message("gh -R x/r pr ready 6", repo) or ""), True)
        check("url form denies", message("gh pr merge https://github.com/o/r/pull/6", repo), True)
        check("url form logged allows", message("gh pr merge https://github.com/o/r/pull/5", repo), False)
        check("other repo same number denies", message("gh pr ready 5 -R o/zzz", repo), True)
        check("other repo own row allows", message("gh pr ready 9 -R q/zzz", repo), False)
        check("repo case is loose", message("gh pr ready 5 -R BARYODEV/r", repo), False)
        check("--help allows", message("gh pr merge --help", repo), False)
        check("-h allows", message("gh pr ready 6 -h", repo), False)
        check("inline skip allows", message(SKIP + "=1 gh pr merge 6", repo), False)
        os.environ[SKIP] = "1"
        check("environment skip allows", message_via_main("gh pr merge 6", repo), True)
        del os.environ[SKIP]
        check("environment unset denies via main", message_via_main("gh pr merge 6", repo), False)
        os.environ["LEAN_STATS"] = "off"
        check("LEAN_STATS=off allows", message("gh pr merge 6", repo), False)
        del os.environ["LEAN_STATS"]
        bare = os.path.join(tmp, "bare.db")
        sqlite3.connect(bare).close()
        os.environ["LEAN_DB"] = bare
        check("no reviews table allows", message("gh pr merge 6", repo), False)
        os.environ["LEAN_DB"] = db
        check("unrelated gh allows", message("gh pr view 6", repo), False)
        check("gh pr create allows", message("gh pr create --fill", repo), False)
        check("git push allows", message("git push origin HEAD", repo), False)
        check("echo mention allows", message("echo gh pr merge 6", repo), False)
        fake_gh("echo '{\"number\": 6, \"url\": \"https://github.com/o/r/pull/6\"}'")
        check("branch form denies", message("gh pr merge feat/x", repo), True)
        check("no argument denies", message("gh pr ready", repo), True)
        fake_gh("echo '{\"number\": 5, \"url\": \"https://github.com/o/r/pull/5\"}'")
        check("branch form logged allows", message("gh pr merge feat/x", repo), False)
        fake_gh("echo '{\"number\": 5, \"url\": \"https://github.com/o/other/pull/5\"}'")
        check("branch form other repo denies", message("gh pr merge feat/x", repo), True)
        fake_gh("exit 1")
        check("gh failure allows", message("gh pr merge feat/x", repo), False)
        fake_gh("echo not json")
        check("gh garbage allows", message("gh pr merge", repo), False)
        check("az draft false denies", message("az repos pr update --id 6 --draft false", repo), True)
        check("az status completed denies", message("az repos pr update --id 6 --status completed", repo), True)
        check("az draft equals form denies", message("az repos pr update --id 6 --draft=false", repo), True)
        check("az logged allows", message("az repos pr update --id 5 --draft false", repo), False)
        check("az draft true allows", message("az repos pr update --id 6 --draft true", repo), False)
        check("az title update allows", message("az repos pr update --id 6 --title t", repo), False)
        check("az no id allows", message("az repos pr update --draft false", repo), False)
        check("az help allows", message("az repos pr update --id 6 --draft false --help", repo), False)
        check("az create allows", message("az repos pr create --title t", repo), False)
        check("az skip inline allows", message(SKIP + "=1 az repos pr update --id 6 --draft false", repo), False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


def message_via_main(command, cwd):
    """True when main() exits 0 for the command with the current environment."""
    import io
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"tool_input": {"command": command}, "cwd": cwd}))
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        return main() == 0
    finally:
        sys.stdin, sys.stderr = old, err


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

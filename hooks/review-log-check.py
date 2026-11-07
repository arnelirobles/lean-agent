#!/usr/bin/env python3
"""Refuse to mark a pull request ready or merge it when no review is logged for it.

PreToolUse hook on Bash, for the commands the table in lib/hookkit.py calls "merge" or "ready": gh pr ready|merge,
glab mr merge and glab mr update --ready, az repos pr update with --draft false or --status completed. The retro reads the reviews table in lean.db, which
log-review.sh fills by pull request number. A review that was done but never logged leaves
the retro with no data for that change.

The pull request number comes from the argument (a number, #number or URL). With no argument,
or a branch name, the hook asks the host once through lib/forge.py (gh pr view, or glab api for GitLab;
5 seconds; any failure or missing CLI allows). For az it is --id. The hook then looks in the reviews table of lean.db for that number.

The repo column is not consistent in practice (BaryoDev/barakoCMS, barakoCMS and barakocms
all exist), so the repo is compared loosely: the last path part, case-insensitive, against the
repo from -R, the URL, gh pr view, or the origin remote. When none of those is known, the
number alone decides.

It only enforces for a repo that already has at least one logged review (same loose match),
because lean-agent hooks create lean.db on their own: once a repo has a logged review, an
unlogged pull request is refused; a repo that never logs reviews is left alone. `gh pr ready
--undo` (back to draft) allows. GH_REPO is honoured after -R/--repo. Remotes are compared
percent-decoded, without a fragment.

az repos pr update has no repository argument and az pull request ids are organization wide,
so for az the rule is: allow when any reviews row has that number, whatever the repo; deny only
when the origin repo has logged reviews and none has that number.

Best effort: wrappers (`bash -c`, `sudo`) and `api` merges are not caught.

The database is LEAN_DB (default ~/.lean/lean.db), as in lean-stats. LEAN_STATS=off, no
database file, no reviews table, or any error allows. --help and -h allow.

  LEAN_SKIP_REVIEW_LOG_CHECK=1   turn it off (environment, settings env, or inline)
  review-log-check.py --self-test
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

HOOK = {
    "events": [
        {"event": "PreToolUse", "matcher": "Bash", "if": "Bash(gh *)", "group": 1, "order": 5},
        {"event": "PreToolUse", "matcher": "Bash", "if": "Bash(az *)", "group": 1, "order": 6},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": False,
    "default": {"strict": "block", "standard": "off"},
}

try:
    import forge  # noqa: E402
except Exception:  # a missing or broken lib/forge.py: GitHub keeps its own lookup below, other hosts allow
    forge = None

SKIP = "LEAN_SKIP_REVIEW_LOG_CHECK"
# Flags before the command word that take a value, and flags after it that take a value, per CLI. -R/--repo
# names the repository on gh and glab.
GLOBAL_VALUE = {"gh": {"-R", "--repo", "--hostname"}, "glab": {"-R", "--repo"}}
VALUE_FLAGS = {
    "gh": {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
           "--match-head-commit"},
    "glab": {"-R", "--repo", "-m", "--message", "--squash-message", "--sha", "-t", "--title", "-d", "--description",
             "-a", "--assignee", "-l", "--label", "--unlabel", "--milestone", "--target-branch", "--reviewer"},
}
AZ_VALUE_FLAGS = {"--id", "--status", "--draft", "--organization", "--org", "--detect", "--title",
                  "--description", "--auto-complete", "--squash", "--delete-source-branch",
                  "--bypass-policy", "--bypass-policy-reason", "--merge-commit-message",
                  "--transition-work-items", "--output", "-o", "--query", "--subscription"}
LOG_REVIEW = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                                           "skills", "adversarial-review", "log-review.sh"))
DENY = (
    "Pull request {name} has no review logged in lean.db, which the retro reads. Run the review "
    "(adversarial-review), then log it with "
    "`{script} {repo} {number} <confirmed> "
    "<refuted> <blockers> \"<categories>\" \"<caught elsewhere first>\"`. "
    "log-review.sh hides lean-stats errors, so if it was run and this still refuses, check `lean-stats` "
    "or set " + SKIP + "=1. If the change was deliberately not reviewed, set " + SKIP + "=1 for this command.")


def last_part(repo):
    """The comparable form of a repo string: last path part, lower case, no .git."""
    text = urllib.parse.unquote((repo or "").split("#")[0].split("?")[0]).strip().rstrip("/")
    text = re.split(r"[/:]", text)[-1] if text else ""
    return text[:-4].lower() if text.lower().endswith(".git") else text.lower()


def full_name(url):
    """owner/repo from a github remote or pull request URL, else the last two path parts."""
    url = urllib.parse.unquote((url or "").split("#")[0].split("?")[0])
    found = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?(?:/pull/\d+.*)?/?$", url)
    return found.group(1) if found else ""


def split_global(cli, words):
    """(repo from a flag before the command, the words from the command on) for a gh or glab call. A flag that
    is not a repo or host flag is skipped; a bare word before the command means it is not one we read."""
    noun = {"gh": "pr", "glab": "mr"}[cli]
    i, repo = 0, None
    while i < len(words) and words[i] != noun:
        w = words[i]
        if w in GLOBAL_VALUE[cli] and i + 1 < len(words):
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
    return repo, words[i:]


def parse_flags(call, repo):
    """(argument, repo, help) of a gh or glab merge or ready call: the first word that is not a flag or a flag's
    value, the -R/--repo value, and whether it only asks for help (or, for gh pr ready, --undo)."""
    cli, words = call.cli, call.rest
    arg, helped, j = None, False, 0
    value_flags = VALUE_FLAGS[cli]
    if cli == "glab" and call.command == ("mr", "merge"):
        value_flags = value_flags - {"-d", "--description"}
    while j < len(words):
        w = words[j]
        if w in ("-h", "--help") or (w == "--undo" and cli == "gh"):
            helped = True
        elif w in ("-R", "--repo") and j + 1 < len(words):
            repo, j = words[j + 1], j + 1
        elif w.startswith("--repo="):
            repo = w.split("=", 1)[1]
        elif w in value_flags:
            j += 1
        elif w.startswith("-"):
            pass
        elif arg is None:
            arg = w
        j += 1
    return arg, repo, helped


def parse_az(call):
    """(id, help) for an az repos pr update the table calls merge or ready."""
    words = call.rest
    pairs, helped = {}, False
    j = 0
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
        j += 1
    return pairs.get("--id"), helped


def pr_url(text):
    """(repository name, number) from a pull request URL: lib/forge.py's reading, or GitHub's alone without it."""
    if forge is not None:
        return forge.pr_url(text)
    found = re.search(r"([^/]+/[^/]+)/pull/(\d+)", (text or "").split("#")[0].split("?")[0])
    return (found.group(1), int(found.group(2))) if found else None


def view(call, arg, repo):
    """(number, url) from the host's own pull request lookup (lib/forge.py), else None. Runs with the hook's own
    environment, never the command's inline one: the hook runs before the command is approved, so an inline PATH
    must not pick which CLI runs. Without forge only GitHub is looked up, with gh directly."""
    if forge is None:
        if call.kind != "github":
            return None
        cmd = ["gh", "pr", "view"] + ([arg] if arg else []) + (["-R", repo] if repo else []) + ["--json", "number,url"]
        try:
            out = subprocess.run(cmd, cwd=call.cwd, capture_output=True, text=True, timeout=5)
            if out.returncode != 0:
                return None
            data = json.loads(out.stdout)
            return int(data["number"]), str(data.get("url") or "")
        except Exception:
            return None
    info = forge.pr_info(call.kind, call.cwd, arg or "", repo=repo, timeout=5)
    return (info["number"], info["url"]) if info else None


def target(seg, call, parsed):
    """(number, repo name as owner/repo or '') for the pull request this command acts on, else None."""
    if call.kind == "azure":
        number = parsed[0]
        if not number or not number.isdigit():
            return None
        return int(number), full_name(hookkit.git(seg.cwd, "remote", "get-url", "origin") or "")
    arg, repo, _helped = parsed
    if call.kind == "github":
        repo = repo or seg.env.get("GH_REPO") or os.environ.get("GH_REPO") or None
    if arg:
        found = re.fullmatch(r"#?(\d+)", arg)
        if found:
            return int(found.group(1)), repo or full_name(hookkit.git(seg.cwd, "remote", "get-url", "origin") or "")
        shown = pr_url(arg)
        if shown:
            return shown[1], shown[0]
    got = view(call, arg, repo)
    if not got:
        return None
    return got[0], (pr_url(got[1]) or ("",))[0] or repo or ""


def logged(number, repo, kind="github"):
    """False only when the database has a reviews table, the repo has logged reviews, and none is for the pull request."""
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
        rows = [(r[0], r[1]) for r in conn.execute("SELECT repo, pr FROM reviews")]
    finally:
        conn.close()
    if kind == "azure" and any(pr == number for _, pr in rows):
        return True
    want = last_part(repo)
    mine = [(r, pr) for r, pr in rows if not want or last_part(r) == want]
    if not mine:
        return True
    return any(pr == number for _, pr in mine)


def message(command, cwd):
    """The deny text for the first command whose pull request has no logged review, else None."""
    for seg in hookkit.segments(command, cwd):
        if hookkit.disabled(SKIP, seg) or not seg.words:
            continue
        try:
            repo = None
            call = hookkit.forge_call(seg)
            if call and call.kind != "azure" and not call.command:
                # Flags before the command word: `gh -R o/r pr merge 6`.
                split = split_global(call.cli, call.args)
                if split:
                    repo, call = split[0], hookkit.ForgeCall(call.cli, split[1], seg.cwd)
            if not call or hookkit.forge_action(call) not in ("merge", "ready"):
                continue
            if call.kind == "azure":
                parsed = parse_az(call)
                if parsed[1]:
                    continue
            else:
                parsed = parse_flags(call, repo)
                if parsed[2]:
                    continue
            found = target(seg, call, parsed)
            if not found or logged(*found, call.kind):
                continue
        except Exception:
            continue
        number, repo = found
        return DENY.format(name=f"{repo}#{number}" if repo else f"#{number}", repo=repo or "<owner/repo>", number=number, script=LOG_REVIEW)
    return None


def main():
    if hookkit.disabled(SKIP):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command")
    if not isinstance(command, str) or not any(cli in command for cli in hookkit.FORGE_CLIS) \
            or not any(w in command for w in ("ready", "merge", "update")):
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        return hookkit.refuse("PreToolUse", text)
    return 0


def self_test():
    os.environ["LEAN_PROFILE"] = "strict"
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
        check("missing db passes", message("gh pr merge 5", repo), False)
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
        evil = os.path.join(tmp, "evil")
        os.mkdir(evil)
        marker = os.path.join(tmp, "evil-ran")
        with open(os.path.join(evil, "gh"), "w") as fh:
            fh.write("#!/bin/sh\n/usr/bin/touch " + marker + "\n")
        os.chmod(os.path.join(evil, "gh"), 0o755)
        message("PATH=" + evil + " gh pr merge feat/x", repo)
        check("inline PATH does not pick the gh the hook runs", os.path.exists(marker), False)
        check("logged number passes", message("gh pr merge 5", repo), False)
        check("logged ready passes", message("gh pr ready 5", repo), False)
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
        check("global -R logged passes", message("gh -R o/r pr ready 5", repo), False)
        check("-R names the repo in the deny", "x/r#6" in (message("gh -R x/r pr ready 6", repo) or ""), True)
        check("url form denies", message("gh pr merge https://github.com/o/r/pull/6", repo), True)
        check("url form logged passes", message("gh pr merge https://github.com/o/r/pull/5", repo), False)
        check("other repo same number denies", message("gh pr ready 5 -R o/zzz", repo), True)
        check("other repo own row passes", message("gh pr ready 9 -R q/zzz", repo), False)
        check("repo case is loose", message("gh pr ready 5 -R BARYODEV/r", repo), False)
        check("repo with no logged reviews passes", message("gh pr merge 6 -R o/nobody", repo), False)
        check("--undo before the number passes", message("gh pr ready --undo 6", repo), False)
        check("--undo after the number passes", message("gh pr ready 6 --undo", repo), False)
        check("inline GH_REPO is used", message("GH_REPO=o/zzz gh pr ready 5", repo), True)
        os.environ["GH_REPO"] = "o/zzz"
        check("environment GH_REPO is used", message("gh pr ready 5", repo), True)
        check("-R beats GH_REPO", message("gh pr ready 5 -R o/r", repo), False)
        del os.environ["GH_REPO"]
        check("deny has the real script path", LOG_REVIEW + " o/r 6" in (message("gh pr merge 6", repo) or ""), True)
        check("deny has no plugin root variable", "CLAUDE_PLUGIN_ROOT" in (message("gh pr merge 6", repo) or ""), False)
        check("deny mentions lean-stats", "check `lean-stats`" in (message("gh pr merge 6", repo) or ""), True)
        enc = os.path.join(tmp, "enc")
        os.mkdir(enc)
        subprocess.run(["git", "-C", enc, "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", enc, "remote", "add", "origin",
                        "https://dev.azure.com/org/proj/_git/My%20Repo#frag"], check=True, capture_output=True)
        c = leanstats.connect(db)
        c.execute("INSERT INTO reviews (date, repo, pr) VALUES ('2026-01-01', 'My Repo', 11)")
        c.close()
        check("encoded remote logged passes", message("az repos pr update --id 11 --draft false", enc), False)
        check("encoded remote unlogged denies", message("az repos pr update --id 13 --draft false", enc), True)
        check("encoded remote gh logged passes", message("gh pr merge 11", enc), False)
        check("encoded remote gh unlogged denies", message("gh pr merge 13", enc), True)
        check("az number logged in another repo passes", message("az repos pr update --id 9 --draft false", repo), False)
        check("--help passes", message("gh pr merge --help", repo), False)
        check("-h passes", message("gh pr ready 6 -h", repo), False)
        check("inline skip passes", message(SKIP + "=1 gh pr merge 6", repo), False)
        os.environ[SKIP] = "1"
        check("environment skip passes", message_via_main("gh pr merge 6", repo), True)
        del os.environ[SKIP]
        check("environment unset denies via main", message_via_main("gh pr merge 6", repo), False)
        os.environ["LEAN_STATS"] = "off"
        check("LEAN_STATS=off passes", message("gh pr merge 6", repo), False)
        del os.environ["LEAN_STATS"]
        bare = os.path.join(tmp, "bare.db")
        sqlite3.connect(bare).close()
        os.environ["LEAN_DB"] = bare
        check("no reviews table passes", message("gh pr merge 6", repo), False)
        os.environ["LEAN_DB"] = db
        check("unrelated gh passes", message("gh pr view 6", repo), False)
        check("gh pr create passes", message("gh pr create --fill", repo), False)
        check("git push passes", message("git push origin HEAD", repo), False)
        check("echo mention passes", message("echo gh pr merge 6", repo), False)
        fake_gh("echo '{\"number\": 6, \"url\": \"https://github.com/o/r/pull/6\"}'")
        check("branch form denies", message("gh pr merge feat/x", repo), True)
        check("no argument denies", message("gh pr ready", repo), True)
        fake_gh("echo '{\"number\": 5, \"url\": \"https://github.com/o/r/pull/5\"}'")
        check("branch form logged passes", message("gh pr merge feat/x", repo), False)
        fake_gh("echo '{\"number\": 5, \"url\": \"https://github.com/o/zzz/pull/5\"}'")
        check("branch form other repo denies", message("gh pr merge feat/x", repo), True)
        fake_gh("exit 1")
        check("gh failure passes", message("gh pr merge feat/x", repo), False)
        fake_gh("echo not json")
        check("gh garbage passes", message("gh pr merge", repo), False)
        check("az draft false denies", message("az repos pr update --id 6 --draft false", repo), True)
        check("az status completed denies", message("az repos pr update --id 6 --status completed", repo), True)
        check("az draft equals form denies", message("az repos pr update --id 6 --draft=false", repo), True)
        check("az logged passes", message("az repos pr update --id 5 --draft false", repo), False)
        check("az draft true passes", message("az repos pr update --id 6 --draft true", repo), False)
        check("az title update passes", message("az repos pr update --id 6 --title t", repo), False)
        check("az no id passes", message("az repos pr update --draft false", repo), False)
        check("az help passes", message("az repos pr update --id 6 --draft false --help", repo), False)
        check("az create passes", message("az repos pr create --title t", repo), False)
        check("az skip inline passes", message(SKIP + "=1 az repos pr update --id 6 --draft false", repo), False)
        check("az status Completed denies", message("az repos pr update --id 6 --status Completed", repo), True)
        # GitLab: glab mr merge and glab mr update --ready, through the same table and the same lookup.
        check("glab merge unlogged denies", message("glab mr merge 6 --squash", repo), True)
        check("glab merge logged passes", message("glab mr merge 5", repo), False)
        check("glab update --ready denies", message("glab mr update 6 --ready", repo), True)
        check("glab update title passes", message("glab mr update 6 --title t", repo), False)
        check("glab update --draft passes", message("glab mr update 6 --draft", repo), False)
        check("glab merge --help passes", message("glab mr merge --help", repo), False)
        check("glab merge message is not the number", message("glab mr merge -m 5 6", repo), True)
        check("glab global -R denies", message("glab -R g/r mr merge 6", repo), True)
        check("glab --repo denies", message("glab mr merge 6 --repo g/r", repo), True)
        check("glab other repo with no reviews passes", message("glab mr merge 6 -R g/nobody", repo), False)
        check("glab url form denies", message("glab mr merge https://gitlab.example.com/g/r/-/merge_requests/6", repo), True)
        check("glab url form logged passes", message("glab mr merge https://gitlab.example.com/g/r/-/merge_requests/5", repo), False)
        check("glab skip inline passes", message(SKIP + "=1 glab mr merge 6", repo), False)
        check("glab create passes", message("glab mr create --fill", repo), False)
        check("glab list passes", message("glab mr list", repo), False)
        glab = os.path.join(bindir, "glab")

        def fake_glab(body):
            with open(glab, "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(glab, os.stat(glab).st_mode | stat.S_IXUSR)

        gl_mr = '[{"iid": %d, "state": "opened", "web_url": "https://gitlab.example.com/g/r/-/merge_requests/%d"}]'
        fake_glab("echo '" + gl_mr % (6, 6) + "'")
        check("glab branch form denies", message("glab mr merge feat/x", repo), True)
        check("glab no argument denies", message("glab mr merge", repo), True)
        fake_glab("echo '" + gl_mr % (5, 5) + "'")
        check("glab branch form logged passes", message("glab mr merge feat/x", repo), False)
        fake_glab("exit 1")
        check("glab failure passes", message("glab mr merge feat/x", repo), False)
        fake_glab("echo not json")
        check("glab garbage passes", message("glab mr merge feat/x", repo), False)
        os.remove(glab)
        check("glab missing passes", message("glab mr merge feat/x", repo), False)
        check("glab unrelated api passes", message("glab api projects/1/merge_requests/6/merge", repo), False)
        # A missing or broken lib/forge.py: no traceback on import, GitHub keeps working, other hosts allow.
        fake_gh("echo '{\"number\": 6, \"url\": \"https://github.com/o/r/pull/6\"}'")
        fake_glab("echo '" + gl_mr % (6, 6) + "'")
        broken = os.path.join(tmp, "broken-lib")
        shutil.copytree(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"), broken,
                        ignore=shutil.ignore_patterns("__pycache__", "brain"))
        with open(os.path.join(broken, "forge.py"), "w") as fh:
            fh.write("raise RuntimeError('broken forge')\n")
        hook = os.path.realpath(__file__)
        hooks_copy = os.path.join(tmp, "hooks-copy")
        os.mkdir(hooks_copy)
        shutil.copy(hook, os.path.join(hooks_copy, "review-log-check.py"))
        os.symlink(broken, os.path.join(tmp, "lib"))

        def run_hook(command):
            event = json.dumps({"tool_input": {"command": command}, "cwd": repo})
            return subprocess.run([sys.executable, os.path.join(hooks_copy, "review-log-check.py")], input=event,
                                  capture_output=True, text=True, env=dict(os.environ, LEAN_PROFILE="strict"))

        r = run_hook("gh pr merge feat/x")
        check("broken forge: no traceback", "Traceback" in r.stderr, False)
        check("broken forge: gh branch lookup still denies", r.returncode != 0 or "has no review logged" in r.stdout + r.stderr, True)
        r = run_hook("gh pr merge 5")
        check("broken forge: logged gh merge passes", r.returncode == 0 and "Traceback" not in r.stderr, True)
        r = run_hook("glab mr merge feat/x")
        check("broken forge: glab branch lookup passes quietly", r.returncode == 0 and "Traceback" not in r.stderr, True)
        r = run_hook("glab mr merge 6")
        check("broken forge: glab number still denies", r.returncode != 0 or "has no review logged" in r.stdout + r.stderr, True)
        fake_gh("exit 1")
        os.environ["LEAN_PROFILE"] = "standard"
        try:
            check("standard: unlogged merge passes", message_via_main("gh pr merge 6", repo), True)
        finally:
            os.environ["LEAN_PROFILE"] = "strict"
        check("strict: unlogged merge still refused", message_via_main("gh pr merge 6", repo), False)
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

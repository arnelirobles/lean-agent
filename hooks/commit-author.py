#!/usr/bin/env python3
"""Block a git commit whose author email would be wrong on a public repository.

PreToolUse hook on Bash. On an agent box the git identity is often unset, so
git falls back to user@hostname, or it was set by hand to something local.
Those commits show up on GitHub with no account attached, and some rulesets
reject them only at push time, after the history is already written. This
checks the email git would actually use before the commit is made:

  GIT_AUTHOR_EMAIL (inline or in the environment), --author, -c user.email,
  then author.email / user.email from git config, then $EMAIL.

It blocks when that email is unset, when its domain is a local hostname (no
public top-level domain, a .local/.lan style suffix, or this machine's
hostname), or when it is a users.noreply.github.com address without the
numeric id prefix (12345+name@...), which GitHub does not link to anyone.

When git cannot be asked (the directory is not a repository, for example
because it came from a shell expression the hook cannot evaluate), it lets
the commit through rather than guess.

  LEAN_ALLOW_COMMIT_AUTHOR=1   turn it off, in the environment, in the env
                               block of Claude Code settings, or inline in
                               front of the command
  commit-author.py --self-test
"""
import os
import re
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

LOCAL_SUFFIXES = {"local", "localdomain", "localhost", "lan", "home", "internal", "intranet",
                  "corp", "private", "test", "invalid", "example", "arpa"}


def hostnames():
    names = set()
    for name in (socket.gethostname(), os.uname().nodename):
        if name:
            names.add(name.lower())
            names.add(name.lower().split(".")[0])
    try:
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    names.discard("")
    return names


def email_problem(email, hosts=None, github=True):
    """Why this author email is wrong, or None when it is fine.

    The .local/.lan/.internal style suffixes count only when github is true (a
    remote points at github.com): a company mail domain like corp.internal is
    fine on an internal server, and GitHub just cannot link it.
    """
    if not email:
        return "no author email is set, so git will guess one from the user and host name"
    email = email.strip().strip("<>").lower()
    if "@" not in email:
        return f"'{email}' is not an email address"
    local, domain = email.rsplit("@", 1)
    if domain == "users.noreply.github.com":
        if not re.fullmatch(r"\d+\+[^@]+", local):
            return (f"'{email}' has no numeric id prefix, so GitHub does not link it to an account "
                    "(use the 12345+name@users.noreply.github.com form from your email settings)")
        return None
    labels = domain.split(".")
    tld = labels[-1]
    if len(labels) < 2 or not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", tld):
        return f"'{email}' ends in a local host name, not a public domain"
    if github and tld in LOCAL_SUFFIXES:
        return f"'{email}' ends in a local domain, which GitHub cannot link to an account"
    if domain in (hostnames() if hosts is None else hosts):
        return f"'{email}' ends in this machine's host name"
    return None


def author_from_flag(args):
    for i, word in enumerate(args):
        value = None
        if word.startswith("--author="):
            value = word.split("=", 1)[1]
        elif word == "--author" and i + 1 < len(args):
            value = args[i + 1]
        if value is not None:
            match = re.search(r"<([^>]*)>", value)
            return match.group(1) if match else None
    return None


def effective_email(seg, call):
    """The author email git would use; "" when unset, None when it cannot be looked up."""
    _sub, args, cwd, config = call
    for candidate in (seg.env.get("GIT_AUTHOR_EMAIL"), os.environ.get("GIT_AUTHOR_EMAIL"),
                      author_from_flag(args), config.get("author.email"), config.get("user.email")):
        if candidate:
            return candidate
    if not hookkit.git(cwd, "rev-parse", "--git-dir"):
        return None
    for key in ("author.email", "user.email"):
        value = hookkit.git(cwd, "config", "--get", key)
        if value:
            return value
    return seg.env.get("EMAIL") or os.environ.get("EMAIL") or ""


def problems(command, cwd, hosts=None):
    found = []
    for seg in hookkit.segments(command, cwd):
        call = hookkit.git_call(seg)
        if not call or call[0] != "commit" or "--dry-run" in call[1]:
            continue
        if hookkit.disabled("LEAN_ALLOW_COMMIT_AUTHOR", seg):
            continue
        email = effective_email(seg, call)
        if email is None:
            continue
        github = "github.com" in (hookkit.git(call[2], "remote", "-v") or "")
        problem = email_problem(email, hosts, github)
        if problem:
            found.append(problem)
    return found


def main():
    if hookkit.disabled("LEAN_ALLOW_COMMIT_AUTHOR"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "commit" not in command:
        return 0
    found = problems(command, event.get("cwd") or os.getcwd())
    if found:
        print("Blocked by lean-agent, the commit author email is wrong: " + "; ".join(found)
              + ". Set it with git config user.email (or -c user.email=... on this commit) "
              "and run the command again. LEAN_ALLOW_COMMIT_AUTHOR=1 in front of the command, or in "
              "the env block of Claude Code settings, turns this check off.",
              file=sys.stderr)
        return 2
    return 0


def self_test():
    import subprocess
    import tempfile

    failures = 0
    hosts = {"devbox", "devbox.example.org"}
    bad = ["", "user@devbox", "me@devbox.localdomain", "me@box.lan", "me@devbox.example.org",
           "jane@users.noreply.github.com", "me@10.0.0.1", "nobody"]
    good = ["jane@example.org", "12345+jane@users.noreply.github.com", "a.b@mail.co.uk",
            "a@xn--80ak6aa92e.xn--p1ai"]
    for email in bad:
        if not email_problem(email, hosts):
            print(f"FAIL allowed bad email {email!r}")
            failures += 1
    for email in ("a@acme.internal", "a@corp.local", "a@build.lan"):
        if not email_problem(email, hosts, github=True) or email_problem(email, hosts, github=False):
            print(f"FAIL {email!r} should block only with a github.com remote")
            failures += 1
    for email in good:
        if email_problem(email, hosts):
            print(f"FAIL blocked good email {email!r}: {email_problem(email, hosts)}")
            failures += 1

    saved = {k: os.environ.pop(k, None) for k in ("GIT_AUTHOR_EMAIL", "EMAIL")}
    old_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOME"] = tmp
            os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
            repo = os.path.join(tmp, "repo")
            subprocess.run(["git", "init", "-q", repo], check=True)
            cases = {
                "git commit -m x": True,
                "git -c user.email=jane@example.org commit -m x": False,
                "GIT_AUTHOR_EMAIL=opc@devbox git commit -m x": True,
                "git commit --author='Jane <jane@example.org>' -m x": False,
                "git commit --dry-run": False,
                "git log --grep 'git commit'": False,
                "echo git commit": False,
            }
            for command, want in cases.items():
                if bool(problems(command, repo, hosts)) != want:
                    print(f"FAIL {command!r}: want blocked={want}")
                    failures += 1
            for command in ('cd "$(git rev-parse --show-toplevel)" && git commit -m x',
                            'cd "$WT" && git commit -m x', 'git -C "$WT" commit -m x'):
                if problems(command, tmp, hosts):
                    print(f"FAIL outside a repository {command!r} should fail open")
                    failures += 1
            subprocess.run(["git", "-C", repo, "config", "user.email", "jane@example.org"], check=True)
            for command in ('cd "$(git rev-parse --show-toplevel)" && git commit -m x',
                            'git -C "$WT" commit -m x', "cd nowhere && git commit -m x",
                            "LEAN_ALLOW_COMMIT_AUTHOR=1 git -c user.email=user@devbox commit -m x",
                            "env LEAN_ALLOW_COMMIT_AUTHOR=1 git -c user.email=user@devbox commit -m x"):
                if problems(command, repo, hosts):
                    print(f"FAIL {command!r} blocked: {problems(command, repo, hosts)}")
                    failures += 1
            subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
            if not problems("GIT_AUTHOR_EMAIL=a@corp.internal git commit -m x", repo, hosts):
                print("FAIL .internal not blocked with a github.com remote")
                failures += 1
            subprocess.run(["git", "-C", repo, "config", "user.email", "user@devbox"], check=True)
            if not problems("git commit -m x", repo, hosts):
                print("FAIL local config email not blocked")
                failures += 1
            if not problems(f"cd {tmp} && git -C repo commit -m x", tmp, hosts):
                print("FAIL git -C path not followed")
                failures += 1
            subprocess.run(["git", "-C", repo, "config", "user.email", "jane@example.org"], check=True)
            if problems("git commit -m x", repo, hosts):
                print("FAIL good config email blocked")
                failures += 1
    finally:
        os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
        if old_home is not None:
            os.environ["HOME"] = old_home
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

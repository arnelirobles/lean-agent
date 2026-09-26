#!/usr/bin/env python3
"""Note when containers are started to check a change that is only docs.

PreToolUse hook on Bash, for docker compose up/run, docker-compose up/run and
docker run. Starting a stack to confirm a documented command can pull
gigabytes and still time out, on a diff that adds one Markdown file. Reading
the compose file answers the same question in one call. It never blocks, and
it stays quiet unless every changed path (commits since the default branch,
the index, the working tree and untracked files) is documentation: *.md,
*.markdown, *.rst, *.txt, *.adoc, or anything under docs/.

  LEAN_SKIP_DOCS_CONTAINERS=1   turn it off
  docs-containers.py --self-test
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

COMPOSE_VALUE_FLAGS = {"-f", "--file", "-p", "--project-name", "--env-file", "--profile",
                       "--project-directory", "--ansi", "--parallel"}
DOCKER_VALUE_FLAGS = {"-c", "--context", "-H", "--host", "--config", "-l", "--log-level"}
DOC = re.compile(r"(^|/)docs/|\.(md|markdown|rst|txt|adoc)$", re.I)


def subcommand(words, value_flags):
    i = 0
    while i < len(words):
        word = words[i]
        if word in value_flags:
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return word, words[i + 1:]
    return None, []


def starts_containers(words):
    if not words:
        return False
    tool = os.path.basename(words[0])
    if tool == "docker-compose":
        return subcommand(words[1:], COMPOSE_VALUE_FLAGS)[0] in ("up", "run")
    if tool != "docker":
        return False
    sub, rest = subcommand(words[1:], DOCKER_VALUE_FLAGS)
    if sub == "run":
        return True
    return sub == "compose" and subcommand(rest, COMPOSE_VALUE_FLAGS)[0] in ("up", "run")


def changed_paths(root):
    names = set()
    for args in (("diff", "--name-only", "HEAD"), ("diff", "--name-only", "--cached"),
                 ("ls-files", "--others", "--exclude-standard")):
        names.update((hookkit.git(root, *args) or "").splitlines())
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = hookkit.git(root, "merge-base", ref, "HEAD")
        if base:
            names.update((hookkit.git(root, "diff", "--name-only", base, "HEAD") or "").splitlines())
            break
    return sorted(n for n in names if n)


def message(command, cwd):
    for seg in hookkit.segments(command, cwd):
        if not starts_containers(seg.words):
            continue
        root = hookkit.git(seg.cwd, "rev-parse", "--show-toplevel")
        if not root:
            return None
        changed = changed_paths(root)
        if not changed or not all(DOC.search(n) for n in changed):
            return None
        return ("Everything changed here is documentation (" + ", ".join(changed[:8])
                + (", ..." if len(changed) > 8 else "") + "). If the aim is to confirm a command "
                "you are writing down, read the compose file or image docs instead of starting "
                "containers. If it cannot be checked cheaply, document how to run it without "
                "claiming what it prints. LEAN_SKIP_DOCS_CONTAINERS=1 turns this note off.")
    return None


def main():
    if hookkit.disabled("LEAN_SKIP_DOCS_CONTAINERS"):
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if "docker" not in command:
        return 0
    text = message(command, event.get("cwd") or os.getcwd())
    if text:
        hookkit.note("PreToolUse", text)
    return 0


def self_test():
    import subprocess
    import tempfile

    failures = 0
    for command, want in {"docker compose up -d": True, "docker compose -f a.yml run --rm api": True,
                          "docker-compose up": True, "docker run --rm alpine true": True,
                          "docker compose ps": False, "docker ps": False, "docker build .": False,
                          "docker compose logs up": False, "docker --context x compose -p a up": True}.items():
        if starts_containers(command.split()) != want:
            print(f"FAIL starts_containers({command!r}) != {want}")
            failures += 1
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@example.org", "-c", "user.name=t", *args],
                           check=True, capture_output=True)

        def write(name, text="x\n"):
            os.makedirs(os.path.dirname(os.path.join(tmp, name)), exist_ok=True)
            with open(os.path.join(tmp, name), "a") as fh:
                fh.write(text)

        git("init", "-q")
        write("app.py")
        write("README.md")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        def expect(name, command, want):
            nonlocal failures
            if bool(message(command, tmp)) != want:
                print(f"FAIL {name}: want note={want}")
                failures += 1

        expect("clean tree", "docker compose up -d", False)
        write("README.md")
        write("docs/setup/run.sh")
        expect("docs only", "docker compose up -d", True)
        expect("not starting containers", "docker compose ps", False)
        write("new_module.py")
        expect("untracked source file", "docker run --rm img", False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

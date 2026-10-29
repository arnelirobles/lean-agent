#!/usr/bin/env python3
"""Catch wait loops and direct test runs when the command is written.

PreToolUse hook on Bash. It denies:
  - sleep followed by cat, tail, head, grep, less or wc (sleep 100; cat x.output)
  - a while, until or for loop that contains sleep and whose condition or body reads a
    file or task output (cat, tail, head, grep, less, wc, ls, stat, test, [) or calls
    pgrep; a loop reading redirected input (done < file) is not a poll
  - pgrep
It looks inside bash -c, sh -c and zsh -c arguments too. It adds a note (never a deny)
on dotnet test, npx vitest, go test and pytest run outside test-phase and heavy.sh.
The note only fires when the brain script is present, since it names `brain queue add`.
Quoted strings and heredoc bodies are not commands, so a sleep inside them passes. A
run_in_background command is checked the same way. A hook error never fails the session.

  LEAN_WAIT_GUARD=off   turn it off (environment, settings env, or inline)
  wait-loop.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

BRAIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools", "brain")
KEYWORDS = {"do", "then", "else", "elif", "!", "{", "}", "time"}
LOOP_OPEN = {"until", "while", "for"}
SLEEP_READERS = {"cat", "tail", "head", "grep", "egrep", "fgrep", "rg", "less", "more", "wc"}
LOOP_READERS = SLEEP_READERS | {"ls", "stat", "test", "[", "[["}
SHELLS = {"bash", "sh", "zsh"}
LANES = {"test-phase", "heavy.sh"}

DENY_SLEEP_READ = (
    "Do not sleep and then read a file or task output. End the turn and wait for the "
    "notification, or start `{brain} queue wait ID...` (test jobs) or `{pr_wait}` (checks) in the "
    "background, or check a lane with `{heavy} status <lane>`.")
DENY_LOOP = (
    "Do not poll a file or process in a loop with sleep. End the turn and wait for the "
    "notification, or start `{brain} queue wait ID...` (test jobs) or `{pr_wait}` (checks) in the "
    "background, or check a lane with `{heavy} status <lane>`.")
DENY_PGREP = (
    "Do not use pgrep to wait on a process. End the turn and wait for the notification, "
    "or start `{brain} queue wait ID...` (test jobs) or `{pr_wait}` (checks) in the background, "
    "or check a lane with `{heavy} status <lane>`.")
TURN_OFF = " LEAN_WAIT_GUARD=off turns this check off."
NOTE_TEST = (
    "This runs tests outside the lane lock, and its failures never reach brain. Queue it: "
    "`{brain} queue add <repo> <project> --filter ...`. LEAN_WAIT_GUARD=off turns this note off.")


def fill(text):
    """The notes name the tools by bare name when session-env put them on PATH, else by full path."""
    return text.format(brain=hookkit.tool_path("brain"), pr_wait=hookkit.tool_path("pr-wait"),
                       heavy=hookkit.tool_path("heavy.sh"))


def is_off(segs):
    value = os.environ.get("LEAN_WAIT_GUARD")
    if value is None:
        value = next((s.env["LEAN_WAIT_GUARD"] for s in segs if "LEAN_WAIT_GUARD" in s.env), "")
    return value.lower() == "off"


def command_words(seg):
    return hookkit.strip_wrappers(after_keywords(seg.words), {})


def after_keywords(words):
    words = list(words)
    while words and words[0] in KEYWORDS:
        words.pop(0)
    return words


def name(words):
    return os.path.basename(words[0]) if words else ""


def is_test_run(words):
    cmd, rest = name(words), words[1:]
    if cmd == "dotnet":
        return "test" in rest[:2]
    if cmd == "go":
        return rest[:1] == ["test"]
    if cmd in ("npx", "pnpx", "bunx"):
        return name(rest) == "vitest"
    if cmd in ("vitest", "pytest"):
        return True
    if cmd.startswith("python") and rest[:2] == ["-m", "pytest"]:
        return True
    return False


def shell_script(words):
    """The argument of bash -c, sh -c or zsh -c, or None."""
    if name(words) not in SHELLS:
        return None
    for i, word in enumerate(words[1:], 1):
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return words[i + 1] if i + 1 < len(words) else None
    return None


def loop_poll(cmds):
    """True when a loop sleeps and reads a file or calls pgrep, outside redirected input."""
    stack, found = [], False
    for words in cmds:
        cmd = name(words)
        if cmd in LOOP_OPEN:
            stack.append({"sleep": False, "read": False})
            words = list(words[1:])
            while words and words[0] in KEYWORDS:
                words.pop(0)
            cmd = name(words)
        if cmd == "done":
            if stack:
                loop = stack.pop()
                if loop["sleep"] and loop["read"] and "<" not in words:
                    found = True
            continue
        for loop in stack:
            if cmd == "sleep":
                loop["sleep"] = True
            elif cmd == "pgrep" or cmd in LOOP_READERS:
                loop["read"] = True
    return found or any(loop["sleep"] and loop["read"] for loop in stack)


def verdict(command, cwd="."):
    """Return ("deny", reason), ("note", text) or None."""
    segs = hookkit.segments(command, cwd)
    if is_off(segs):
        return None
    cmds = [command_words(s) for s in segs]
    for words in cmds:
        script = shell_script(words)
        if script is not None:
            inner = verdict(script, cwd)
            if inner and inner[0] == "deny":
                return inner
    if any(name(w) == "pgrep" or (name(w) in LOOP_OPEN | {"if"} and name(after_keywords(w[1:])) == "pgrep")
           for w in cmds):
        return "deny", fill(DENY_PGREP + TURN_OFF)
    if loop_poll(cmds):
        return "deny", fill(DENY_LOOP + TURN_OFF)
    for i, words in enumerate(cmds[:-1]):
        if name(words) == "sleep" and name(cmds[i + 1]) in SLEEP_READERS:
            return "deny", fill(DENY_SLEEP_READ + TURN_OFF)
    if os.path.exists(BRAIN) and any(is_test_run(w) for w in cmds) \
            and not any(name(w) in LANES for w in cmds):
        return "note", fill(NOTE_TEST)
    return None


def main():
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command")
    if not isinstance(command, str):
        return 0
    if not any(w in command for w in ("sleep", "pgrep", "test", "vitest")):
        return 0
    found = verdict(command, event.get("cwd") or os.getcwd())
    if not found:
        return 0
    kind, text = found
    if kind == "deny":
        print(text, file=sys.stderr)
        return 2
    hookkit.note("PreToolUse", text)
    return 0


def self_test():
    import contextlib
    import io
    import json
    import tempfile

    global BRAIN
    failures = 0
    real_brain = BRAIN
    os.environ.pop("LEAN_WAIT_GUARD", None)
    tmp = tempfile.TemporaryDirectory()
    fake = os.path.join(tmp.name, "brain")
    open(fake, "w").close()
    BRAIN = fake

    def check(label, command, want):
        nonlocal failures
        found = verdict(command)
        got = found[0] if found else None
        if got != want:
            print(f"FAIL {label}: got {got!r}, want {want!r}")
            failures += 1

    def run_main(stdin):
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main()
        finally:
            sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    def event(command):
        return json.dumps({"tool_input": {"command": command}, "cwd": tmp.name})

    # denied
    check("until grep loop", "until grep -q DONE out.txt; do sleep 15; done", "deny")
    check("while test loop", "while ! test -f x; do sleep 5; done", "deny")
    check("for loop with test", "for i in $(seq 60); do test -f f && break; sleep 10; done", "deny")
    check("nested loops count right",
          "while read l; do for i in 1 2; do echo; done; sleep 1; done < f; until grep -q x f; do sleep 1; done", "deny")
    check("nested for, inner polls", "for a in 1 2; do for i in 1 2; do cat f; sleep 1; done; done", "deny")
    check("sleep then cat output", "sleep 100; cat /tmp/tasks/x.output", "deny")
    check("sleep then tail", "sleep 30 && tail -20 build.log", "deny")
    check("pgrep", "pgrep -f dotnet", "deny")
    check("pgrep in a loop", "while pgrep -f dotnet; do echo w; done", "deny")
    check("negated pgrep in a loop", "until ! pgrep -f dotnet; do echo w; done", "deny")
    check("bash -c loop", "bash -c 'until grep -q x f; do sleep 15; done'", "deny")
    check("sh -c pgrep", 'sh -c "pgrep -f foo"', "deny")
    check("timeout bash -c loop", "timeout 60 bash -c 'until grep -q x f; do sleep 1; done'", "deny")
    check("zsh -lc loop", "zsh -lc 'sleep 5; cat f'", "deny")

    # passes
    check("sleep alone", "sleep 2", None)
    check("sleep then a build", "sleep 2 && make", None)
    check("sleep then curl", "sleep 1 && curl x", None)
    check("sleep then ls", "sleep 5; ls", None)
    check("sleep then test -x", "sleep 2 && test -x ./app && ./app", None)
    check("health check retry loop", "while ! curl -sf localhost:8080/health; do sleep 2; done", None)
    check("redirected read loop", "while IFS= read -r l; do sleep 0.1; echo $l; done < f", None)
    check("cat sleep.log", "cat sleep.log", None)
    check("echo pgrep", 'echo "pgrep"', None)
    check("git log grep pgrep", "git log --grep pgrep", None)
    check("sleep in a quoted string", "echo 'until x; do sleep 1; done'", None)
    check("sleep in a double quoted string", 'git commit -m "no sleep 5; cat x"', None)
    check("sleep in a heredoc body",
          "cat > run.sh <<'EOF'\nuntil grep -q x f; do sleep 1; done\nsleep 5; cat f\nEOF", None)
    check("while read without sleep", "while read line; do echo $line; done < list.txt", None)
    check("sleep after a loop ends", "while read l; do echo $l; done < f; sleep 1", None)
    check("bash -c harmless", "bash -c 'echo hi'", None)
    check("comment", "echo hi # until x; do sleep 1; done", None)
    check("off switch inline", "LEAN_WAIT_GUARD=off sleep 100; cat x.output", None)
    os.environ["LEAN_WAIT_GUARD"] = "off"
    check("off switch environment", "pgrep -f dotnet", None)
    del os.environ["LEAN_WAIT_GUARD"]
    reason = (verdict("pgrep x") or ("", ""))[1]
    if "LEAN_WAIT_GUARD=off" not in reason or f"`{hookkit.tool_path('heavy.sh')} status" not in reason:
        print("FAIL deny reason names the fix and the off switch")
        failures += 1
    for label, command in (("sleep", "sleep 100; cat x.output"), ("loop", "until grep -q x f; do sleep 1; done"),
                           ("pgrep", "pgrep -f x")):
        text = (verdict(command) or ("", ""))[1]
        if f"`{hookkit.tool_path('brain')} queue wait" not in text or f"`{hookkit.tool_path('pr-wait')}" not in text:
            print(f"FAIL {label} deny text names brain queue wait and pr-wait")
            failures += 1

    # notes
    check("bare dotnet test notes", "dotnet test X | tail -20", "note")
    check("npx vitest notes", "npx vitest run", "note")
    check("go test notes", "cd svc && go test ./...", "note")
    check("pytest notes", "python3 -m pytest -q", "note")
    check("test inside test-phase passes", "tools/test-phase run -- dotnet test X", None)
    check("test inside heavy.sh passes", "heavy.sh run lane dotnet test X", None)
    check("queued test passes", "brain queue add repo proj --filter 'dotnet test'", None)
    check("dotnet build is quiet", "dotnet build X", None)
    BRAIN = os.path.join(tmp.name, "missing")
    check("no note without the brain script", "dotnet test X", None)
    BRAIN = fake

    # through main()
    rc, out, err = run_main(event("pgrep -f foo"))
    if rc != 2 or "pgrep" not in err or out:
        print(f"FAIL main deny: rc={rc} out={out!r} err={err!r}")
        failures += 1
    rc, out, err = run_main(event("dotnet test X | tail -5"))
    try:
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError):
        context = ""
    if rc != 0 or f"`{hookkit.tool_path('brain')} queue add" not in context or err:
        print(f"FAIL main note: rc={rc} out={out!r} err={err!r}")
        failures += 1
    for label, stdin in (("junk stdin", "not json"), ("non-string command", '{"tool_input":{"command":5}}'),
                         ("no tool_input", "{}")):
        rc, out, err = run_main(stdin)
        if rc != 0 or out or err:
            print(f"FAIL main {label}: rc={rc} out={out!r} err={err!r}")
            failures += 1

    BRAIN = real_brain
    tmp.cleanup()
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

#!/usr/bin/env python3
"""Make every background shell bounded.

PreToolUse hook on Bash. A run_in_background command that does not start with
tools/bounded, tools/test-phase or tools/heavy.sh is denied, and the message gives the
same command wrapped in bounded to run instead. bounded stops the command's whole
process group after LEAN_BG_MINUTES (default 60), when the task is stopped, and
when its shell dies, so a stopped task cannot leave a process spinning for hours.

It denies rather than rewrites: a hook can only change a command if it also
approves it, which would skip the user's permission rules.

  LEAN_BG_GUARD=off   turn it off (environment, settings env, or inline)
  bg-bound.py --self-test
"""
import os
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

BOUNDED = os.path.join(hookkit.TOOLS, "bounded")
OWN_LANES = ("bounded", "test-phase", "heavy.sh")


def lane(words):
    """True when one simple command starts with a lane (after env assignments and an interpreter)."""
    for w in words[:3]:
        if "=" in w and not w.startswith("/") and w.split("=", 1)[0].isidentifier():
            continue
        name = os.path.basename(w)
        if name in ("python3", "python", "bash", "sh"):
            continue
        return name in OWN_LANES
    return False


def exempt(command):
    """True when every command in the line runs under bounded or a lean lane runner.

    Comments and heredoc bodies are dropped first. A line that will not parse is not exempt.
    """
    text, _ = hookkit.prepare(command)
    try:
        toks = hookkit.tokens(text, strict=True)
    except ValueError:
        return False
    parts, current = [], []
    for tok in toks:
        if hookkit.is_separator(tok):
            parts.append(current)
            current = []
        else:
            current.append(tok)
    parts.append(current)
    parts = [p for p in parts if p]
    return bool(parts) and all(lane(p) for p in parts)


def wrapped(command):
    shell = os.path.basename(os.environ.get("SHELL") or "bash")
    if shell not in ("bash", "zsh", "sh"):
        shell = "bash"
    return f"{shlex.quote(BOUNDED)} -- {shell} -c {shlex.quote(command)}"


def verdict(tool_input):
    if tool_input.get("run_in_background") is not True:
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip() or exempt(command):
        return None
    return ("A background command must run under bounded, so it stops after LEAN_BG_MINUTES "
            "(default 60), when the task is stopped, and when its shell dies. Run this instead, "
            "still in the background (add --minutes N after bounded if it needs longer):\n"
            f"{wrapped(command)}\nLEAN_BG_GUARD=off turns this check off.")


def main():
    if (os.environ.get("LEAN_BG_GUARD") or "").lower() == "off":
        return 0
    event = hookkit.read_event()
    text = verdict(hookkit.tool_input(event))
    if not text:
        return 0
    print(text, file=sys.stderr)
    return 2


def self_test():
    failures = 0

    def check(label, tool_input, want_deny):
        nonlocal failures
        got = verdict(tool_input) is not None
        if got != want_deny:
            print(f"FAIL {label}: denied={got}, want {want_deny}")
            failures += 1

    bg = lambda c: {"command": c, "run_in_background": True}  # noqa: E731
    check("foreground passes", {"command": "bash scripts/preflight.sh"}, False)
    check("background denied", bg("cd /x && bash scripts/preflight.sh > log 2>&1"), True)
    check("bounded passes", bg(f"{BOUNDED} -- bash -c 'make'"), False)
    check("python3 bounded passes", bg(f"python3 {BOUNDED} --minutes 90 -- make"), False)
    check("test-phase passes", bg("~/.claude/plugins/cache/lean-agent/lean-agent/1.19.0/tools/test-phase --project x"), False)
    check("heavy.sh passes", bg("bash tools/heavy.sh dotnet dotnet test"), False)
    check("env prefix then bounded", bg(f"LEAN_BG_MINUTES=120 {BOUNDED} -- make"), False)
    check("bounded later in a chain is not enough", bg(f"make; {BOUNDED} -- true"), True)
    for sep in ("&&", ";", "|", "&", "||", "\n"):
        check(f"bounded then sleep after {sep!r}", bg(f"{BOUNDED} -- true {sep} sleep 999"), True)
        check(f"sleep then bounded after {sep!r}", bg(f"sleep 999 {sep} {BOUNDED} -- true"), True)
    check("heavy.sh then sleep", bg("bash heavy.sh && sleep 999"), True)
    check("two lanes chained pass", bg(f"{BOUNDED} -- true && bash tools/heavy.sh x"), False)
    check("quoted separator stays one argument", bg(f"{BOUNDED} -- sh -c 'a && b; c | d'"), False)
    check("glued separator before an unbalanced quote", bg(f"{BOUNDED} -- true&&sleep 999 # don't"), True)
    check("unbalanced quote is denied", bg(f"{BOUNDED} -- echo 'oops"), True)
    check("separator inside a comment passes", bg(f"{BOUNDED} -- npm test # a && b"), False)
    check("separator inside a heredoc body passes", bg(f"{BOUNDED} -- cat <<EOF\nfoo && bar\nEOF"), False)
    check("quoted heredoc end word cannot hide a command", bg(f"{BOUNDED} -- cat <<E\"O\"F\nx\nEOF\nsleep 999"), True)
    check("two env assignments then bounded", bg(f"A=1 B=2 {BOUNDED} -- true"), False)
    check("three env assignments then bounded is denied", bg(f"A=1 B=2 C=3 {BOUNDED} -- true"), True)
    check("background false passes", {"command": "make", "run_in_background": False}, False)

    # The suggested command really runs the original under bounded.
    import subprocess
    os.environ["SHELL"] = "/bin/bash"
    cmd = wrapped("echo 'it ran' && exit 7")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if proc.returncode != 7 or proc.stdout.strip() != "it ran":
        print(f"FAIL wrapped command: rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
        failures += 1

    if failures:
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

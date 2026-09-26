"""Shared plumbing for the lean-agent hooks.

Every hook reads one JSON event on stdin and must never break the tool call it
watches because of its own bug. So `run` turns any internal error into exit 0,
and every git call has a short timeout. Standard library only, Python 3.9+.

  python3 lib/hookkit.py --self-test
"""
import json
import os
import re
import shlex
import subprocess
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;", "|&"}
PIPES = {"|", "|&"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|()<>]+))")
HEREDOC_MARK = re.compile(r"__lean_heredoc_(\d+)__")
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def run(main):
    """Run a hook's main and exit with its code; an internal error exits 0."""
    try:
        code = main()
    except SystemExit:
        raise
    except Exception as exc:  # a broken hook must not block the tool call
        print(f"lean-agent hook error ignored: {exc!r}", file=sys.stderr)
        code = 0
    sys.exit(code or 0)


def read_event():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def tool_input(event):
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def note(event_name, text):
    """Print a non-blocking note that Claude reads with the tool result."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}))


def disabled(name, seg=None):
    """True when NAME=1 is in the environment, or in the env prefix of this command."""
    return os.environ.get(name) == "1" or bool(seg and seg.env.get(name) == "1")


def resolve(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def git(cwd, *args, timeout=3):
    """stdout of a git command, stripped, or None when it fails."""
    try:
        result = subprocess.run(["git", "-C", cwd or ".", *args], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def test_marker(cwd):
    """Path of the file recording this worktree's last compiled test run, or None outside git."""
    git_dir = git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(git_dir, "lean-last-test-run") if git_dir else None


def prepare(command):
    """Drop shell comments and cut heredoc bodies out of a command.

    Returns the remaining text, with each heredoc replaced by a marker word, and
    the list of bodies. Without this a `# note` swallows the next line and a
    heredoc line such as `cd /` reads as a command.
    """
    out, bodies, pending = [], [], []
    i, n, quote = 0, len(command), None
    while i < n:
        ch = command[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            match = HEREDOC.match(command, i)
            if match:
                delim = next(g for g in match.groups()[1:] if g is not None)
                out.append(f" __lean_heredoc_{len(bodies) + len(pending)}__ ")
                pending.append((delim, bool(match.group(1))))
                i = match.end()
                continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, strip_tabs in pending:
                lines = []
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    lines.append(line)
                bodies.append("".join(line + "\n" for line in lines))
            pending = []
            continue
        out.append(ch)
        i += 1
    bodies.extend("" for _ in pending)
    return "".join(out), bodies


def tokens(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def safe_dir(target, cwd, base):
    """Resolve a cd or -C target; fall back to base for a shell expression or a missing dir."""
    if "$" in target or "`" in target:
        return base
    path = resolve(target, cwd)
    return path if os.path.isdir(path) else base


def skip_options(words, i, with_value=()):
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in with_value else 1
    return i


def strip_wrappers(words, env):
    """Drop nice, timeout, flock, env, time, command, exec and nohup in front of a command."""
    while words:
        name = os.path.basename(words[0])
        if name == "env":
            i = skip_options(words, 1, ("-u", "--unset", "-C", "--chdir"))
            while i < len(words) and ASSIGNMENT.fullmatch(words[i]):
                key, value = words[i].split("=", 1)
                env[key] = value
                i += 1
        elif name == "nice":
            i = skip_options(words, 1, ("-n", "--adjustment"))
        elif name == "timeout":
            i = skip_options(words, 1, ("-s", "--signal", "-k", "--kill-after")) + 1
        elif name == "flock":
            i = skip_options(words, 1, ("-w", "--timeout", "-E", "--conflict-exit-code")) + 1
            i = skip_options(words, i, ("-w", "--timeout", "-E", "--conflict-exit-code"))
            if i < len(words) and words[i - 1] in ("-c", "--command"):
                return []
        elif name in ("time", "command", "exec", "nohup"):
            i = skip_options(words, 1)
        else:
            return words
        words = words[i:]
    return words


class Segment:
    """One simple command out of a shell line.

    env is its VAR=value prefix (and env wrapper assignments), words the command
    with wrappers removed, cwd where it runs after any earlier `cd`, base the cwd
    the hook was given, heredocs the bodies fed to it, piped whether its output
    goes into a pipe (so its exit code is not the line's).
    """

    def __init__(self, env, words, cwd, base, heredocs=(), piped=False):
        self.env, self.words, self.cwd, self.base = env, words, cwd, base
        self.heredocs, self.piped = list(heredocs), piped

    def __repr__(self):
        return f"Segment({self.env!r}, {self.words!r}, {self.cwd!r})"


def is_separator(tok):
    return tok in SEPARATORS or (tok and set(tok) <= set(";&|()\n"))


def segments(command, cwd="."):
    """Split a shell command into simple commands, following `cd DIR`."""
    text, bodies = prepare(command)
    base = cwd
    out, current = [], []
    for tok in tokens(text) + [";"]:
        if not is_separator(tok):
            current.append(tok)
            continue
        env, heredocs, words = {}, [], []
        for word in current:
            mark = HEREDOC_MARK.fullmatch(word)
            if mark:
                heredocs.append(bodies[int(mark.group(1))] if int(mark.group(1)) < len(bodies) else "")
            else:
                words.append(word)
        while words and ASSIGNMENT.fullmatch(words[0]):
            key, value = words.pop(0).split("=", 1)
            env[key] = value
        words = strip_wrappers(words, env)
        if words and words[0] == "cd":
            cwd = safe_dir(words[1] if len(words) > 1 else "~", cwd, base)
        elif words:
            out.append(Segment(env, words, cwd, base, heredocs, tok in PIPES))
        current = []
    return out


def git_call(seg):
    """For a git segment return (subcommand, args, cwd, config) or None.

    config holds `-c key=value` pairs given before the subcommand.
    """
    words = seg.words
    if not words or os.path.basename(words[0]) != "git":
        return None
    cwd, config, i = seg.cwd, {}, 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in GIT_GLOBAL_WITH_VALUE and i + 1 < len(words):
            value = words[i + 1]
            if word == "-C":
                cwd = safe_dir(value, cwd, seg.base)
            elif word == "-c" and "=" in value:
                key, val = value.split("=", 1)
                config[key.lower()] = val
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1:], cwd, config


def gh_call(seg):
    """For a gh segment return (words after gh, cwd) or None."""
    words = seg.words
    if not words or os.path.basename(words[0]) != "gh":
        return None
    return words[1:], seg.cwd


def self_test():
    import tempfile

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        base, r = os.path.join(tmp, "w"), os.path.join(tmp, "r")
        os.makedirs(os.path.join(r, "sub"))
        os.makedirs(base)
        segs = segments(f'cd {r} && GIT_AUTHOR_EMAIL=a@b git -C sub -c user.email=x@y commit -m "a && b"\ngit push', base)
        check("segment count", len(segs), 2)
        check("env prefix", segs[0].env, {"GIT_AUTHOR_EMAIL": "a@b"})
        check("cd followed", segs[0].cwd, r)
        call = git_call(segs[0])
        check("git subcommand", call[0], "commit")
        check("git -C", call[2], os.path.join(r, "sub"))
        check("git -c", call[3], {"user.email": "x@y"})
        check("quoted && kept", call[1], ["-m", "a && b"])
        check("second segment", git_call(segs[1])[0], "push")
        check("cd to a shell expression keeps the base", segments('cd "$(git rev-parse --show-toplevel)" && git status', base)[0].cwd, base)
        check("cd to $VAR keeps the base", segments('cd "$WT" && git status', base)[0].cwd, base)
        check("cd to a missing dir keeps the base", segments("cd nowhere && git status", base)[0].cwd, base)
        check("git -C $VAR keeps the base", git_call(segments('git -C "$WT" commit', base)[0])[2], base)
        os.makedirs(os.path.join(base, "$WT"))
        check("cd $VAR ignores a dir literally named $WT", segments('cd "$WT" && git status', base)[0].cwd, base)
    check("echo is not git", git_call(segments('echo "git commit"')[0]), None)
    check("grep is not git", [git_call(s) for s in segments("git log --grep 'git commit'")][0][0], "log")
    check("gh call", gh_call(segments("gh pr create --fill | cat")[0])[0][:2], ["pr", "create"])
    check("unbalanced quote falls back", len(segments('git commit -m "oops')) >= 1, True)
    check("resolve relative", resolve("a/b", "/x"), "/x/a/b")
    check("comment ends at the newline", [s.words for s in segments("git add . # stage\ngit commit -m x")],
          [["git", "add", "."], ["git", "commit", "-m", "x"]])
    check("hash inside a word is not a comment", segments("echo a#b")[0].words, ["echo", "a#b"])
    check("quoted hash is not a comment", segments("git commit -m '# 1'")[0].words[-1], "# 1")
    heredoc = segments("cd / && git commit -F - <<'EOF'\nsubject\n\ncd /tmp\nEOF\ngit push", "/")
    check("heredoc body is not a command", [s.words[:2] for s in heredoc], [["git", "commit"], ["git", "push"]])
    check("heredoc body attached", heredoc[0].heredocs, ["subject\n\ncd /tmp\n"])
    check("heredoc <<- strips tabs", segments("cat <<-X\n\tbody\n\tX\necho done")[0].heredocs, ["\tbody\n"])
    wrapped = segments("flock -o /tmp/l nice -n 5 timeout 60 dotnet test 2>&1 | tail -5")
    check("wrappers stripped", wrapped[0].words[:2], ["dotnet", "test"])
    check("piped segment", [s.piped for s in wrapped], [True, False])
    check("env wrapper", segments("env LEAN_X=1 nice git commit -m x")[0].env, {"LEAN_X": "1"})
    check("time wrapper", git_call(segments("time git commit -m x")[0])[0], "commit")
    check("inline off switch", disabled("LEAN_NOT_SET_ANYWHERE", segments("LEAN_NOT_SET_ANYWHERE=1 git commit")[0]), True)
    check("off switch not set", disabled("LEAN_NOT_SET_ANYWHERE", segments("git commit")[0]), False)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

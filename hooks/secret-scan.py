#!/usr/bin/env python3
"""Keep keys and tokens out of git.

PreToolUse hook. The scan itself runs in git's own pre-commit and pre-push
hooks, which tools/secret-scan installs (secret-scan install REPO), so a commit
or push made any way at all is checked. This hook makes sure they run:

  Bash   a command that commits or pushes (git commit, git push, an alias of
         either, a git command it does not know, or a git call it cannot place)
         is blocked when the repository has no working secret-scan pre-commit
         and pre-push hooks, with the command that installs them. It is also
         blocked when it skips them: --no-verify, commit -n, core.hooksPath,
         include.path or includeIf set with -c, --config-env or git config, or
         GIT_CONFIG_*, HOME or XDG_CONFIG_HOME set in the command. Commands that
         run commands (submodule foreach, rebase --exec, bisect run, filter-branch,
         difftool, for-each-repo) count as commits and what they run is read.
         Only read-only forms of external commands (git lfs pull, svn log) pass. git send-pack and git http-push
         never run pre-push and are blocked too.
  Write, Edit, MultiEdit, NotebookEdit
         the text being written, when the file is tracked or would be tracked.
         A gitignored file such as .env.local is fine, and so is a file inside
         the git dir.

It looks inside bash -c, sh -c, eval, braces, if/then and xargs, and follows
cd, pushd, -C, --git-dir, --work-tree, GIT_DIR and GIT_WORK_TREE. When a path is
a variable or a command (cd $(...)), or a cd target does not exist yet, it
checks the starting repository, and blocks when that is not one. If the
scanner cannot be loaded the call is blocked with the reason.

The message names the file, the line and the pattern, never the value. A line
with `allow-secret` on it is skipped. Each block is recorded as a hook fire in
lean.db, so a weekly count is possible.

  LEAN_SKIP_SECRET_SCAN=1   turn it off, here and in the git hooks: in the
                            environment, in the env block of Claude Code
                            settings, or inline in front of the command
  secret-scan.py --self-test
"""
import importlib.machinery
import importlib.util
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

SKIP = "LEAN_SKIP_SECRET_SCAN"


def scanner():
    path = os.path.join(hookkit.TOOLS, "secret-scan")
    loader = importlib.machinery.SourceFileLoader("secret_scan_tool", path)
    spec = importlib.util.spec_from_loader("secret_scan_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"}
QUIET = {"echo", "printf", "grep", "rg", "cat", "man", "which", "head", "tail", "less", "type", "jq"}
EXPORTS = {"export", "declare", "typeset", "local", "readonly"}
# git commands that neither commit through pre-commit nor push. Anything else that is not an
# alias (git-foo on PATH, a typo) counts as a commit or push, since it might be one.
BUILTINS = set("""add am annotate apply archive backfill bisect blame branch bugreport bundle cat-file
check-attr check-ignore check-mailmap check-ref-format checkout checkout-index cherry cherry-pick clean
clone column commit-graph commit-tree config count-objects credential describe diagnose diff diff-files
diff-index diff-pairs diff-tree fetch fetch-pack fmt-merge-msg for-each-ref
format-patch fsck gc grep hash-object help hook index-pack init init-db interpret-trailers
log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-tree
mktag mktree multi-pack-index mv name-rev notes pack-objects pack-refs patch-id prune pull
range-diff read-tree rebase reflog refs remote repack replace replay repo rerere reset restore rev-list
rev-parse revert rm shortlog show show-branch show-index show-ref sparse-checkout stage stash status
stripspace submodule switch symbolic-ref tag unpack-file unpack-objects update-index update-ref var
verify-commit verify-pack verify-tag version whatchanged worktree""".split())
# git commands that run other commands for you. Always possible commits; the command is read where it can be.
RUNNERS = {"for-each-repo", "filter-branch", "difftool", "mergetool"}
# Known external and read-only commands that neither commit through pre-commit nor push. filter-repo,
# fast-import and subtree rewrite history, so they are not here: they need the hooks, like commit.
EXTERNALS = set("""request-pull instaweb daemon scalar update-server-info quiltimport sh-setup delta
last-modified""".split())
# Externals with subcommands: only these read-only or local forms pass. Anything else (svn dcommit,
# p4 submit, lfs push, machete traverse) may commit or push, and so does absorb, send-email, gui, citool.
EXTERNAL_FORMS = {
    "lfs": {"pull", "fetch", "install", "ls-files", "status", "track", "untrack", "env", "version", "prune",
            "update"},
    "svn": {"log", "fetch", "rebase", "info", "clone", "init", "find-rev", "blame"},
    "p4": {"sync", "clone"},
    "machete": {"status", "discover", "show", "list", "log", "version", "help", "file", "is-managed"},
}


def external_ok(call):
    """True for a known external git command in a form that neither commits nor pushes."""
    if call.sub in EXTERNALS:
        return True
    words = [a for a in call.args if not a.startswith("-")]
    if call.sub == "lfs":
        if any(a in ("--force", "-f") for a in call.args):
            return False  # lfs install --force and update --force overwrite hooks
        if call.args[:1] and call.args[0] in ("--version", "-h", "--help"):
            return True
    if call.sub == "flow":
        if words[:1] and words[0] in ("init", "version", "help"):
            return True
        return bool(set(words) & {"list", "start", "checkout", "track", "diff", "pull"}) and \
            not set(words) & {"finish", "publish"}
    forms = EXTERNAL_FORMS.get(call.sub)
    return bool(forms and words and words[0] in forms and "--push" not in call.args)
NO_HOOK_PUSH = {"send-pack", "http-push"}
COMMIT_VALUE_SHORT = set("mFCctSu")
VALUE_LONG = {"--author", "--date", "--cleanup", "--fixup", "--squash", "--trailer", "--message", "--file",
              "--reuse-message", "--reedit-message", "--template", "--repo", "--receive-pack", "--exec",
              "--push-option", "--pathspec-from-file"}
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
# a variable that may hold git, then commit or push, in a command that never says git
GIT_WORD = re.compile(r"\$\{?(\w*git\w*|g)\}?\s(?:.*\s)?(commit|push)\b", re.I | re.S)
BYPASS_ENV = re.compile(r"GIT_CONFIG\w*|HOME|XDG_CONFIG_HOME")
ENV_PATH = re.compile(r"""\b(GIT_DIR|GIT_WORK_TREE)=(?:'([^']*)'|"([^"]*)"|([^\s;&|()]+))""")


class Call:
    """One git invocation found in a command. dynamic: its folder could not be worked out."""

    def __init__(self, sub, args, cwd, base, git_dir, work_tree, env, dynamic, config):
        self.sub, self.args, self.cwd, self.base = sub, args, cwd, base
        self.git_dir, self.work_tree, self.env, self.dynamic = git_dir, work_tree, env, dynamic
        self.config = config  # [(key, value)] from -c and --config-env


def is_dynamic(text):
    return "$" in text or "`" in text


def shell_string(words):
    """The script text of `sh -c STRING`, or of eval, else None."""
    if os.path.basename(words[0]) == "eval":
        return " ".join(words[1:])
    for i in range(1, len(words)):
        w = words[i]
        if not w.startswith("-") or w.startswith("--"):
            return None
        if "c" in w[1:]:
            return words[i + 1] if i + 1 < len(words) else None
    return None


def parse_git(words, seg_cwd, base, env, dynamic, env_dirs):
    cwd, git_dir, work_tree, config, i = seg_cwd, None, None, [], 1
    while i < len(words) and words[i].startswith("-"):
        w = words[i]
        name, eq, val = w.partition("=") if w.startswith("--") else (w, "", "")
        if w == "-C" and i + 1 < len(words):
            dynamic = dynamic or is_dynamic(words[i + 1]) or not os.path.isdir(hookkit.resolve(words[i + 1], cwd))
            cwd = hookkit.safe_dir(words[i + 1], cwd, base)
            i += 2
        elif name in ("--git-dir", "--work-tree", "--config-env"):
            if not eq:
                if i + 1 >= len(words):
                    break
                val, i = words[i + 1], i + 1
            i += 1
            if name == "--config-env":
                key, _eq, var = val.partition("=")
                config.append((key, "$" + var))
                continue
            dynamic = dynamic or is_dynamic(val)
            if name == "--git-dir":
                git_dir = hookkit.resolve(val, cwd)
            else:
                work_tree = hookkit.resolve(val, cwd)
        elif w == "-c" and i + 1 < len(words):
            key, _eq, value = words[i + 1].partition("=")
            config.append((key, value))
            i += 2
        elif w in ("--namespace", "--exec-path"):
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    if git_dir is None:
        value = env.get("GIT_DIR") or env_dirs.get("GIT_DIR") or os.environ.get("GIT_DIR")
        if value:
            dynamic = dynamic or is_dynamic(value)
            git_dir = hookkit.resolve(value, cwd)
    if work_tree is None:
        value = env.get("GIT_WORK_TREE") or env_dirs.get("GIT_WORK_TREE") or os.environ.get("GIT_WORK_TREE")
        if value:
            dynamic = dynamic or is_dynamic(value)
            work_tree = hookkit.resolve(value, cwd)
    return Call(words[i], words[i + 1:], cwd, base, git_dir, work_tree, env, dynamic, config)


def backticks(text):
    """Backtick bodies written as $( ), so they are split like one. Single quotes and escapes are left alone."""
    out, i, n, quote, open_ = [], 0, len(text), None, False
    while i < n:
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "`":
            out.append(" ) " if open_ else " $( ")
            open_ = not open_
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def quoted_substitutions(text, depth=0):
    """Bodies of $( ) and backtick substitutions inside double quotes, which the word split keeps as
    plain text. A heredoc inside a body is cut out first, so its text is not read as commands."""
    out, i, n, quote = [], 0, len(text), None
    while i < n:
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < n:
            i += 2
            continue
        if quote == "'":
            quote = None if ch == "'" else quote
        elif quote == '"':
            if ch == '"':
                quote = None
            elif text.startswith("$(", i):
                level, j = 1, i + 2
                while j < n and level:
                    if text[j] == "\\":
                        j += 2
                        continue
                    level += (text[j] == "(") - (text[j] == ")")
                    j += 1
                out.append(text[i + 2:j - 1 if level == 0 else j])
                i = j
                continue
            elif ch == "`":
                j = i + 1
                while j < n and text[j] != "`":
                    j += 2 if text[j] == "\\" else 1
                out.append(text[i + 1:j])
                i = j + 1
                continue
        elif ch in "'\"":
            quote = ch
        i += 1
    found = [hookkit.HEREDOC_MARK.sub("", hookkit.prepare(b)[0]) if "<<" in b else b for b in out]
    if depth < 3:
        for b in list(found):
            found += quoted_substitutions(b, depth + 1)
    return found


def split(text, cwd, base):
    """Simple commands of a shell line as (segment, dynamic, assigned names). Like hookkit.segments,
    and it follows pushd too. After a cd it cannot follow (a variable, a command, a folder that does
    not exist yet, cd -, popd) every later segment is dynamic and runs, as far as we know, in base."""
    text, bodies = hookkit.prepare(text)
    extra = quoted_substitutions(text)
    text = backticks(text)
    out, current, dyn = [], [], False
    all_tokens = hookkit.tokens(text) + [";"]
    for body in extra:
        all_tokens += hookkit.tokens(backticks(body)) + [";"]
    for tok in all_tokens:
        if not hookkit.is_separator(tok):
            current.append(tok)
            continue
        env, heredocs, words = {}, [], []
        for word in current:
            mark = hookkit.HEREDOC_MARK.fullmatch(word)
            if mark:
                n = int(mark.group(1))
                heredocs.append(bodies[n] if n < len(bodies) else "")
            else:
                words.append(word)
        current = []
        while words and hookkit.ASSIGNMENT.fullmatch(words[0]):
            key, value = words.pop(0).split("=", 1)
            env[key] = value
        words = hookkit.strip_wrappers(words, env)
        assigned = set(env)
        if words and words[0] in EXPORTS:
            assigned |= {w.split("=", 1)[0] for w in words[1:] if hookkit.ASSIGNMENT.fullmatch(w)}
        if words and words[0] in ("cd", "pushd", "popd"):
            args = [w for w in words[1:] if w == "-" or not w.startswith("-")]
            target = args[0] if args else "~"
            path = hookkit.resolve(target, cwd)
            if words[0] == "popd" or target == "-" or is_dynamic(target) or not os.path.isdir(path):
                dyn, cwd = True, base
            else:
                cwd = path
            words = []
        out.append((hookkit.Segment(env, words, cwd, base, heredocs, tok in hookkit.PIPES), dyn, assigned))
    return out


def find_calls(command, cwd):
    """(calls, odd, assigned): the git invocations in a command, looking inside sh -c, eval, braces,
    if/then and xargs; odd lists the arguments of each command whose command word is itself a
    variable or a command substitution (a git call may hide there; for a substitution it is every word
    of the command); assigned is every variable the command sets."""
    calls, odd, assigned = [], [], set()
    env_dirs = {}
    for m in ENV_PATH.finditer(command):
        env_dirs[m.group(1)] = m.group(2) or m.group(3) or m.group(4) or ""

    def walk(text, where, env, depth, dyn0):
        for seg, dyn, names in split(text, where, cwd):
            assigned.update(names)
            words, senv = list(seg.words), dict(env)
            senv.update(seg.env)
            while True:
                size = len(words)
                while words and words[0] in KEYWORDS:
                    words.pop(0)
                while words and hookkit.ASSIGNMENT.fullmatch(words[0]):
                    key, value = words.pop(0).split("=", 1)
                    senv[key] = value
                    assigned.add(key)
                words = hookkit.strip_wrappers(words, senv)
                assigned.update(senv)
                if len(words) == size:
                    break
            if not words:
                continue
            first = os.path.basename(words[0])
            if is_dynamic(words[0]) or words[0] == "$":
                if words[0] == "$":
                    odd.append([w for w in hookkit.tokens(backticks(hookkit.prepare(command)[0]))
                                if not hookkit.is_separator(w)])
                else:
                    odd.append(words[1:])
            if first in SHELLS or first == "eval":
                inner = shell_string(words)
                if inner is not None and depth < 4:
                    walk(inner, seg.cwd, senv, depth + 1, dyn0 or dyn)
                    continue
            idx = next((j for j, w in enumerate(words) if os.path.basename(w) == "git"
                        and (j == 0 or first not in QUIET)), None)
            if idx is None:
                continue
            call = parse_git(words[idx:], seg.cwd, cwd, senv, dyn0 or dyn, env_dirs)
            if call:
                calls.append(call)

    walk(command, cwd, {}, 0, False)
    return calls, odd, assigned


def alias_value(call):
    """The alias call.sub stands for: from -c alias.X=... on the command, else from git config."""
    want = "alias." + call.sub.lower()
    for key, value in reversed(call.config):
        if key.lower() == want:
            return value
    argv = ["git"] + (["--git-dir", call.git_dir] if call.git_dir else []) + ["config", "--get", want]
    try:
        p = subprocess.run(argv, cwd=call.cwd if os.path.isdir(call.cwd or "") else None,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def exec_flag(word):
    return word in ("-x", "--exec") or word.startswith("--exec=") or (word.startswith("-x") and len(word) > 2)


FILTER_OPTIONS = {"--env-filter", "--tree-filter", "--index-filter", "--parent-filter", "--msg-filter",
                  "--commit-filter", "--tag-name-filter", "--setup"}


def runs_commands(call):
    """True for a git subcommand that runs other commands for you (a hook skipping flag may sit in them)."""
    a = call.args
    if call.sub in RUNNERS:
        return True
    if call.sub == "submodule":
        return "foreach" in a
    if call.sub == "bisect":
        return "run" in a
    return call.sub == "rebase" and any(exec_flag(w) for w in a)


def option_values(args, names, short=()):
    """Values of options that take a command: --name VALUE, --name=VALUE, and -xVALUE for a short name."""
    out = []
    for i, w in enumerate(args):
        name, eq, val = w.partition("=")
        if name in names:
            if eq:
                out.append(val)
            elif i + 1 < len(args):
                out.append(args[i + 1])
        elif w in short and i + 1 < len(args):
            out.append(args[i + 1])
        elif any(w.startswith(sh) and len(w) > 2 and not w.startswith("--") for sh in short):
            out.append(w[2:])
    return out


def inner_commands(call):
    """The command text a runner will run, where it can be read. git joins the words of submodule
    foreach with spaces; bisect run and for-each-repo take an argument list, so those are quoted."""
    a = call.args
    if call.sub == "submodule" and "foreach" in a:
        rest = [w for w in a[a.index("foreach") + 1:] if w != "--recursive"]
        return [" ".join(rest)] if rest else []
    if call.sub == "bisect" and "run" in a:
        rest = a[a.index("run") + 1:]
        return [shlex.join(rest)] if rest else []
    if call.sub == "rebase":
        return option_values(a, {"--exec"}, ("-x",))
    if call.sub == "filter-branch":
        return option_values(a, FILTER_OPTIONS)
    if call.sub in ("difftool", "mergetool"):
        return option_values(a, {"--extcmd"}, ("-x",))
    if call.sub == "for-each-repo":
        i = 0
        while i < len(a) and (a[i] == "--keep-going" or a[i].startswith("--config=") or a[i] == "--config"):
            i += 2 if a[i] == "--config" else 1
        rest = a[i:]  # git's own options (-C, -c) stay, so find_calls reads them
        return ["git " + shlex.join(rest)] if rest else []
    return []


def effective(call, depth=0):
    """The calls that commit or push, once aliases are expanded: [] for a git command that does
    neither. A shell alias (!...) counts as one, and the git calls in its text are added."""
    sub = call.sub
    if sub in ("commit", "push") or sub in NO_HOOK_PUSH or is_dynamic(sub):
        return [call]
    if sub in RUNNERS or (sub in BUILTINS and runs_commands(call)):
        out = [call]
        for text in inner_commands(call) if depth < 5 else []:
            inner, _odd, _names = find_calls(text, call.cwd)
            for c in inner:
                c.dynamic = c.dynamic or call.dynamic
                out += effective(c, depth + 1)
        return out
    if sub in BUILTINS:
        return []
    alias = alias_value(call) if depth < 5 else None
    if alias is None:
        return [] if external_ok(call) else [call]
    if alias.startswith("!"):
        inner, _odd, _names = find_calls(alias[1:], call.cwd)
        out = [call]
        for c in inner:
            c.dynamic = c.dynamic or call.dynamic
            out += effective(c, depth + 1)
        return out
    new = parse_git(["git"] + hookkit.tokens(alias) + list(call.args), call.cwd, call.base, call.env,
                    call.dynamic, {})
    if new is None:
        return []
    new.git_dir = new.git_dir or call.git_dir
    new.work_tree = new.work_tree or call.work_tree
    new.config = call.config + new.config
    return effective(new, depth + 1)


def skips_hooks(call):
    """Why this commit or push would not run its git hook, or None."""
    if call.sub in NO_HOOK_PUSH:
        return f"git {call.sub} never runs the pre-push hook"
    i = 0
    while i < len(call.args):
        a = call.args[i]
        name = a.split("=", 1)[0]
        if a == "--":
            break
        if len(name) >= 7 and "--no-verify".startswith(name):
            return a
        if a.startswith("--"):
            i += 2 if name in VALUE_LONG and "=" not in a else 1
            continue
        if call.sub == "commit" and a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch == "n":
                    return "commit -n"
                if ch in COMMIT_VALUE_SHORT:
                    if k == len(a) - 1 and ch not in "Su":
                        i += 1
                    break
        i += 1
    return None


def bypasses(calls, touching, assigned):
    """Ways this command turns the git hooks off."""
    found = []
    for name in sorted(assigned):
        if BYPASS_ENV.fullmatch(name):
            found.append(f"{name}= in the command")
    for c in calls + touching:
        for key, _value in c.config:
            low = key.lower()
            if low == "core.hookspath":
                found.append("core.hooksPath on the command line")
            elif low == "include.path" or low.startswith("includeif."):
                found.append(f"{low.split('.')[0]} config include on the command line")
        # a read has a read action and no value word after the key; --show-origin and --show-scope only
        # change the output, so `git config --show-scope include.path /x` still writes
        reading = (c.sub == "config" and any(a in CONFIG_READS for a in c.args)
                   and len([a for a in c.args if not a.startswith("-")]) <= 1)
        if c.sub == "config" and not reading and any("core.hookspath" in a.lower() for a in c.args):
            found.append("git config core.hooksPath")
        if c.sub == "config" and not reading and any(
                re.search(r"\binclude(if\..*)?\.path\b", a.lower()) for a in c.args):
            found.append("git config include.path")
    for c in touching:
        why = skips_hooks(c)
        if why:
            found.append(why)
    return sorted(set(found))


def bash_check(command, cwd):
    """None when the command may run, else the message that blocks it."""
    calls, odd, assigned = find_calls(command, cwd)
    touching = []
    for c in calls:
        if not hookkit.disabled(SKIP, c):
            touching += effective(c)
    unplaced = odd and re.search(r"\b(commit|push)\b", hookkit.prepare(command)[0])
    if unplaced and SKIP + "=1" not in command:
        for args in odd:
            touching.append(Call("commit", args, cwd, cwd, None, None, {}, True, []))
    touching = [c for c in touching if not hookkit.disabled(SKIP, c)]
    if not touching:
        return None
    skip_note = f"Put {SKIP}=1 in front of the command to skip the scan on purpose."
    found = bypasses(calls, touching, assigned)
    if found:
        return ("Blocked by lean-agent: this command commits or pushes without the git hooks that scan "
                f"for keys ({', '.join(found)}). Run it without that. " + skip_note)
    scan = os.path.join(hookkit.TOOLS, "secret-scan")
    if any(c.sub in ("init", "clone") for c in calls):
        return ("Blocked by lean-agent: this command makes a repository and commits or pushes in it in one go, "
                "before the secret-scan git hooks can be installed. Run git init or git clone on its own, then "
                f"python3 '{scan}' install DIR, then the rest. " + skip_note)
    tool = scanner()
    seen = set()
    for c in touching:
        repo = tool.Repo(c.base) if c.dynamic else tool.Repo(c.cwd, c.git_dir, c.work_tree)
        if not tool.is_repo(repo):
            if c.dynamic:
                return ("Blocked by lean-agent: cannot tell which repository this command commits or pushes in "
                        "(a path is a variable or a command, or a cd target does not exist yet), and "
                        f"{c.base} is not a git repository. Use a literal path that exists. " + skip_note)
            continue
        key = (repo.cwd, repo.git_dir, repo.work_tree)
        if key in seen:
            continue
        seen.add(key)
        ok, why = tool.hooks_installed(repo)
        if not ok:
            note = tool.install_note(repo)
            return (f"Blocked by lean-agent: {tool.toplevel(repo)} cannot scan this commit or push for keys: {why}. "
                    f"Install the git hooks, then run the command again:\n  {tool.install_command(repo)}\n"
                    + (note + "\n" if note else "") + skip_note)
    return None


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(as_text(v) for v in value)
    return str(value)


def written_text(name, data):
    if name == "Write":
        return as_text(data.get("content"))
    if name == "MultiEdit":
        return "\n".join(as_text(e.get("new_string")) for e in data.get("edits") or [] if isinstance(e, dict))
    if name == "NotebookEdit":
        return as_text(data.get("new_source"))
    return as_text(data.get("new_string"))


def write_findings(name, data, cwd, tool):
    """Line numbers count the new text of an edit, not the whole file."""
    path = data.get("file_path") or data.get("notebook_path")
    if not isinstance(path, str) or not path:
        return []
    path = hookkit.resolve(path, cwd)
    folder = os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return []
        folder = parent
    folder = os.path.realpath(folder)
    repo = tool.Repo(folder)
    if not tool.is_repo(repo):
        return []
    real = os.path.realpath(path)
    for flag in ("--absolute-git-dir", "--git-common-dir"):
        gd = (tool.git(repo, "rev-parse", flag, check=False) or "").strip()
        if gd:
            gd = os.path.realpath(hookkit.resolve(gd, folder))
            if real == gd or real.startswith(gd + os.sep):
                return []  # git's own files (config, hooks) are never committed
    top = os.path.realpath(tool.toplevel(repo))
    rel = os.path.relpath(real, top)
    if rel.startswith(".."):
        return []
    # rel is relative to the top, so ask git from the top, not from the folder the file is in
    at_top = tool.Repo(top)
    tracked = tool.git(at_top, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel, check=False)
    if tracked is None and tool.git(at_top, "check-ignore", "-q", "--", rel, check=False) is not None:
        return []
    return tool.scan_text(written_text(name, data), rel)


def blocked(reason):
    print(f"Blocked by lean-agent: the secret scan check could not run ({reason}), so nothing was checked. "
          f"Fix that, or set {SKIP}=1 in front of the command or in the env block of Claude Code settings.",
          file=sys.stderr)
    return 2


def main():
    if hookkit.disabled(SKIP):
        return 0
    event = hookkit.read_event()
    name = event.get("tool_name")
    data = hookkit.tool_input(event)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    if name == "Bash":
        command = data.get("command")
        if not isinstance(command, str):
            return 0
        if "git" not in command and not GIT_WORD.search(command):
            return 0
        try:
            text = bash_check(command, cwd)
        except Exception as exc:
            return blocked(f"{type(exc).__name__}: {exc}")
        if text:
            print(text, file=sys.stderr)
            return 2
        return 0
    if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return 0
    what = "this " + name.lower()
    try:
        tool = scanner()
        found = write_findings(name, data, cwd, tool)
    except Exception as exc:
        return blocked(f"{type(exc).__name__}: {exc}")
    if found:
        print(tool.message(found, what)
              + f"\n{SKIP}=1 in front of the command, or in the env block of Claude Code settings, turns this check off.",
              file=sys.stderr)
        return 2
    return 0


def self_test():
    import json
    import shutil
    import sqlite3
    import tempfile

    failures = 0

    def check(label, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {label}: got {got!r}, want {want!r}")
            failures += 1

    tool = scanner()
    keys = tool.fake_keys()
    google = keys["google-api-key (AIza)"]
    npm = keys["npm-token (npm_)"]
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="secret-hook-"))
    try:
        nogl = os.path.join(tmp, "nogl")
        os.makedirs(nogl)
        for name in ("git", "sh", "cat", "touch", "sleep", "printf", "xargs", "echo", "bash", "python3"):
            real = shutil.which(name)
            if real:
                os.symlink(real, os.path.join(nogl, name))
        env = dict(os.environ, HOME=tmp, LEAN_DB=os.path.join(tmp, "lean.db"), GIT_CONFIG_NOSYSTEM="1",
                   LEAN_SECRET_SCAN_HOME=os.path.join(tmp, "home"), PATH=nogl)
        for var in (SKIP, "LEAN_STATS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT",
                    "GIT_CONFIG_PARAMETERS"):
            env.pop(var, None)
        here = os.path.realpath(__file__)
        scan = os.path.join(hookkit.TOOLS, "secret-scan")

        def sh(*argv, cwd=None, extra=None):
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=dict(env, **(extra or {})))

        def hook(tool_name, tool_input, cwd, extra=None, script=None):
            e = dict(env, **(extra or {}))
            event = {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd, "session_id": "t1"}
            return subprocess.run([sys.executable, script or here], input=json.dumps(event), capture_output=True,
                                  text=True, env=e)

        def bash(command, cwd, extra=None, script=None):
            return hook("Bash", {"command": command}, cwd, extra, script)

        def make(name):
            r = os.path.join(tmp, name)
            sh("git", "init", "-q", "-b", "main", r)
            sh("git", "-C", r, "config", "user.email", "t@example.org")
            sh("git", "-C", r, "config", "user.name", "t")
            return r

        def put(r, rel, text):
            path = os.path.join(r, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

        def commit(r, msg="c"):
            sh("git", "-C", r, "add", "-A")
            sh("git", "-C", r, "commit", "-qm", msg, "--no-verify")

        def install(r):
            return sh("python3", scan, "install", r)

        # a repository without the git hooks: anything that commits or pushes is stopped
        bare = make("bare")
        put(bare, "a.txt", "a\n")
        commit(bare, "base")
        p = bash("git commit -m x", bare)
        check("no hooks: commit blocks", p.returncode, 2)
        check("and says how to install them", "install" in p.stderr and scan in p.stderr and bare in p.stderr, True)
        check("and why", "no secret-scan pre-commit hook" in p.stderr, True)
        check("no hooks: push blocks", bash("git push origin main", bare).returncode, 2)
        check("no hooks: status passes", bash("git status", bare).returncode, 0)
        check("no hooks: log --grep commit passes", bash("git log --grep commit", bare).returncode, 0)
        check("no hooks: echo git commit passes", bash("echo git commit", bare).returncode, 0)
        check("no hooks: gh pr create text that says git commit passes",
              bash('gh pr create --title "x" --body "run git commit, then git push; cd $HOME"', bare).returncode, 0)
        check("no hooks: skip inline", bash("LEAN_SKIP_SECRET_SCAN=1 git commit -m x", bare).returncode, 0)
        check("no hooks: skip in env", bash("git commit -m x", bare, {SKIP: "1"}).returncode, 0)
        check("no hooks: skip inside bash -c", bash("bash -c 'LEAN_SKIP_SECRET_SCAN=1 git commit -m x'", bare).returncode, 0)
        check("outside a repository a literal commit passes", bash("git commit -m x", tmp).returncode, 0)
        sh("git", "-C", bare, "config", "alias.ci", "commit")
        sh("git", "-C", bare, "config", "alias.st", "status")
        sh("git", "-C", bare, "config", "alias.sci", "!git commit")
        sh("git", "-C", bare, "config", "alias.deep", "ci -q")
        for label, cmd in (("bash -c", "bash -c 'git commit -m x'"), ("sh -c", 'sh -c "git commit -m x"'),
                           ("bash -lc", "bash -lc 'git commit -m x'"), ("eval", "eval 'git commit -m x'"),
                           ("braces", "{ git commit -m x; }"), ("if/then", "if true; then git commit -m x; fi"),
                           ("negation", "! git commit -m x"), ("xargs", "echo m | xargs git commit -m"),
                           ("nested", "bash -c \"sh -c 'git commit -m x'\""),
                           ("env wrapper", "env FOO=1 git commit -m x"), ("while", "while true; do git commit -m x; done"),
                           ("alias git ci", "git ci -m x"), ("alias of an alias", "git deep -m x"),
                           ("shell alias", "git sci -m x"), ("-c alias", "git -c alias.c=commit c -m x"),
                           ("an unknown git command", "git frobnicate"), ("a git subcommand in a variable", "git $SUB -m x"),
                           ("-C into it", f"git -C {bare} commit -m x"), ("cd into it", f"cd {bare} && git commit -m x"),
                           ("pushd into it", f"pushd {bare} && git commit -m x"),
                           ("commit -a", "git commit -am x"), ("--pathspec-from-file", "git commit -m x --pathspec-from-file=l"),
                           ("GIT_INDEX_FILE", "GIT_INDEX_FILE=/tmp/i git commit -m x")):
            check(f"no hooks, {label}: blocks", bash(cmd, bare if not label.endswith("into it") else tmp).returncode, 2)
        check("an alias for status passes", bash("git st", bare).returncode, 0)

        # once installed, commits and pushes go through to git, which scans them
        p = install(bare)
        check("install", p.returncode, 0)
        put(bare, "k.txt", "k = " + google + "\n")
        sh("git", "-C", bare, "add", "k.txt")
        check("installed: a commit with a staged key is left to the git hook", bash("git commit -m x", bare).returncode, 0)
        p = sh("git", "-C", bare, "commit", "-qm", "x")
        check("and the git hook blocks it", p.returncode != 0 and "k.txt:1" in p.stderr, True)
        sh("git", "-C", bare, "reset", "-q")
        os.remove(os.path.join(bare, "k.txt"))
        for label, cmd in (("alias git ci", "git ci -m x"), ("shell alias", "git sci -m x"),
                           ("bash -c", "bash -c 'git commit -m x'"), ("cd $(...) from inside", "cd $(echo /) && git commit -m x"),
                           ("$(printf git) commit", "$(printf git) commit -m x"), ("git -C $DIR", "git -C $DIR commit -m x"),
                           ("cd to a folder that does not exist yet", "cd nosuch && git commit -m x")):
            check(f"installed, {label}: passes", bash(cmd, bare).returncode, 0)

        # the regression set: ordinary work in a repository with the hooks passes
        origin = os.path.join(tmp, "origin.git")
        sh("git", "init", "-q", "--bare", "-b", "main", origin)
        sh("git", "-C", bare, "remote", "add", "origin", origin)
        for label, cmd in (("plain commit", "git commit -m x"), ("first push -u", "git push -u origin main"),
                           ("push with upstream", "git push"), ("detached HEAD push", "git push origin HEAD:refs/heads/d"),
                           ("push --delete", "git push origin --delete d"),
                           ("heredoc message with cd $HOME", "git commit -F - <<'EOF'\nfix\ncd $HOME\nEOF"),
                           ("$(cat <<EOF) message with cd $HOME", "git commit -m \"$(cat <<'EOF'\nfix\ncd $HOME\nEOF\n)\""),
                           ("add then commit", "git add c.txt && git commit -m c"),
                           ("commit -m -n is a message", "git commit -m -n"),
                           ("gh pr create", 'gh pr create --body "git commit -n and git push --no-verify"')):
            check(f"regression: {label}", bash(cmd, bare).returncode, 0)

        # skipping the hooks is blocked, even where they are installed
        for label, cmd in (("--no-verify", "git commit --no-verify -m x"), ("--no-verif", "git commit --no-verif -m x"),
                           ("-n", "git commit -n -m x"), ("-anm", "git commit -anm x"), ("push --no-verify", "git push --no-verify"),
                           ("-c core.hooksPath", "git -c core.hooksPath=/dev/null commit -m x"),
                           ("-c CORE.HOOKSPATH", "git -c CORE.HOOKSPATH=/x commit -m x"),
                           ("--config-env", "git --config-env=core.hooksPath=HP commit -m x"),
                           ("GIT_CONFIG_COUNT", "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/x git commit -m x"),
                           ("export GIT_CONFIG_PARAMETERS", "export GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/x'\"; git commit -m x"),
                           ("env GIT_CONFIG_GLOBAL", "env GIT_CONFIG_GLOBAL=/tmp/g git push"),
                           ("HOME", "HOME=/tmp/h git commit -m x"),
                           ("git config core.hooksPath", "git config core.hooksPath /x && git commit -m x"),
                           ("an alias that adds --no-verify", "git -c alias.c='commit --no-verify' c -m x"),
                           ("an alias that sets hooksPath", "git -c 'alias.c=-c core.hooksPath=/x commit' c -m x"),
                           ("a shell alias that skips", "git -c 'alias.c=!git commit -n' c"),
                           ("send-pack", f"git send-pack {origin} main"), ("wrapped", "bash -c 'git commit --no-verify -m x'"),
                           ("-c include.path", "git -c include.path=/x commit -m x"),
                           ("-c INCLUDE.PATH", "git -c INCLUDE.PATH=/x commit -m x"),
                           ("-c includeIf", "git -c includeIf.gitdir:/x/.path=/y commit -m x"),
                           ("--config-env include.path", "git --config-env=include.path=P commit -m x"),
                           ("--config-env includeIf", "git --config-env includeIf.gitdir:/x/.path=P commit -m x"),
                           ("git config include.path", "git config include.path /x && git commit -m x"),
                           ("a variable git word with -n", "g=git; $g commit -n -m x"),
                           ("a variable git word with --no-verify", "g=git; $g commit --no-verify -m x"),
                           ("a substituted git word with -n", "$(printf git) commit -n -m x"),
                           ("a backtick commit with -n", "echo `git commit -n -m x`"),
                           ("a backtick push with --no-verify", "x=`git push --no-verify`"),
                           ("submodule foreach with -n", "git submodule foreach 'git commit -n -m x'"),
                           ("rebase --exec with --no-verify", "git rebase --exec 'git commit --no-verify -m x' main"),
                           ("rebase -x with -n", "git rebase -x 'git commit -n -m x' main"),
                           ("bisect run with -n", "git bisect run git commit -n -m x"),
                           ("bisect run sh -c with -n", "git bisect run sh -c 'git commit -n -m x'"),
                           ("filter-branch --tree-filter", "git filter-branch --tree-filter 'git commit -n -m x' HEAD"),
                           ("filter-branch --msg-filter=", "git filter-branch --msg-filter='git commit --no-verify -m x'"),
                           ("filter-branch --index-filter", "git filter-branch --index-filter 'git push --no-verify' HEAD"),
                           ("difftool -x", "git difftool -x 'git commit -n -m x'"),
                           ("difftool --extcmd=", "git difftool --extcmd='git commit -n -m x'"),
                           ("mergetool --extcmd", "git mergetool --extcmd 'git commit -n -m x'"),
                           ("for-each-repo", "git for-each-repo --config=x.y commit -n -m x"),
                           ("a git variable word set earlier, -n", "$GIT commit -n -m x"),
                           ("a braced git variable word, --no-verify", "${G} push --no-verify"),
                           ("a substitution in double quotes", 'echo "$(git commit -n -m x)"'),
                           ("backticks in double quotes", 'echo "`git commit --no-verify -m x`"'),
                           ("a nested quoted substitution", 'echo "$(echo "$(git commit -n -m x)")"'),
                           ("config --show-scope still sets", "git config --show-scope include.path /x && git commit -m x"),
                           ("for-each-repo -C", "git for-each-repo --config=x.y -C /x commit -n -m x"),
                           ("for-each-repo -c include.path", "git for-each-repo --config=x.y -c include.path=x commit -m x"),
                           ("for-each-repo -c core.hooksPath", "git for-each-repo --config=x.y -c core.hooksPath=/x push"),
                           ("a heredoc substitution then a commit -n",
                            'echo "$(cat <<EOF\nx\nEOF\n) $(git commit -n -m x)"')):
            p = bash(cmd, bare)
            check(f"bypass, {label}: blocks", p.returncode, 2)
            check(f"bypass, {label}: says why", "without the git hooks" in p.stderr, True)
        check("bypass with skip set is allowed", bash("LEAN_SKIP_SECRET_SCAN=1 git commit --no-verify -m x", bare).returncode, 0)
        for label, cmd in (("a variable git word without a skip flag", "g=git; $g commit -m x"),
                           ("a backtick body that is not a commit", "echo `git status`"),
                           ("a backtick in a commit message", 'git commit -m "use `foo` here"'),
                           ("submodule foreach of a read-only command", "git submodule foreach 'git status'"),
                           ("rebase --exec of a build", "git rebase -x make main"),
                           ("bisect run of a test", "git bisect run make test"),
                           ("filter-branch", "git filter-branch --tree-filter true"),
                           ("filter-repo", "git filter-repo --path x"),
                           ("git config --get include.path", "git config --get include.path; git commit -m x"),
                           ("git config --get-all includeIf.x.path", "git config --get-all includeIf.x.path && git commit -m x"),
                           ("git config --list", "git config --list | grep include.path; git commit -m x"),
                           ("git config -l --show-origin", "git config -l --show-origin; git commit -m x"),
                           ("git config --get core.hooksPath", "git config --get core.hooksPath && git commit -m x"),
                           ("bisect run of sh -c without a skip", "git bisect run sh -c 'make test'"),
                           ("for-each-repo of maintenance", "git for-each-repo --config=x.y maintenance run"),
                           ("a variable word that is not git", "$EDITOR commit -n"),
                           ("a git variable word without a skip flag", "$GIT commit -m x"),
                           ("a heredoc message that says git commit -n",
                            "git commit -m \"$(cat <<'EOF'\nuse git commit -n\nEOF\n)\""),
                           ("a quoted substitution that is not a commit", 'echo "$(git status)"')):
            check(f"installed, {label}: passes", bash(cmd, bare).returncode, 0)

        # externals and read-only commands pass without hooks; history rewriters and unknown ones do not
        nohooks = make("nohooks")
        put(nohooks, "a.txt", "a\n")
        commit(nohooks, "base")
        for sub in ("lfs pull", "lfs fetch", "lfs install", "lfs ls-files", "lfs status", "lfs track '*.psd'",
                    "lfs --version", "lfs -h", "lfs version", "lfs prune", "lfs update",
                    "svn log", "svn fetch", "svn rebase", "svn info", "svn clone x", "p4 sync", "p4 clone x",
                    "machete status", "machete discover", "flow init", "flow feature start x",
                    "request-pull v1 origin", "instaweb", "daemon", "scalar list",
                    "update-server-info", "quiltimport", "sh-setup", "delta", "last-modified", "submodule status",
                    "bisect start", "rebase main"):
            check(f"no hooks, git {sub}: passes", bash(f"git {sub}", nohooks).returncode, 0)
        for sub in ("filter-repo --path x", "fast-import", "subtree add x", "frobnicate", "svn dcommit",
                    "svn set-tree x", "svn frob", "p4 submit", "p4 frob", "send-email x.patch", "flow feature finish x",
                    "flow release publish x", "flow frob", "machete traverse", "machete advance",
                    "machete traverse --push", "machete", "absorb", "lfs push origin", "lfs frob", "lfs", "gui",
                    "lfs install --force", "lfs install -f", "lfs update --force",
                    "citool"):
            check(f"no hooks, git {sub}: blocks", bash(f"git {sub}", nohooks).returncode, 2)
        # an alias named like an external wins over it
        aliased = make("aliased")
        sh("git", "-C", aliased, "config", "alias.lfs", "push")
        check("an alias named lfs is its target, not the external", bash("git lfs pull", aliased).returncode, 2)
        check("an alias for an external that is read-only passes", bash("git st", bare).returncode, 0)
        for label, cmd in (("a backtick commit", "echo `git commit -m x`"),
                           ("submodule foreach", "git submodule foreach 'git status'"),
                           ("rebase --exec", "git rebase --exec make main"), ("rebase -x", "git rebase -x make main"),
                           ("bisect run", "git bisect run make test"), ("for-each-repo", "git for-each-repo --config=x.y fetch"),
                           ("filter-branch", "git filter-branch --tree-filter true"), ("difftool", "git difftool"),
                           ("mergetool", "git mergetool")):
            check(f"no hooks, {label}: blocks", bash(cmd, nohooks).returncode, 2)

        # a shared or tracked hooks folder: the block message gives a command that works
        shared = make("shared")
        sh("git", "-C", shared, "config", "core.hooksPath", ".githooks")
        p = bash("git commit -m x", shared)
        check("shared hooks folder: blocks", p.returncode, 2)
        check("and the message names --force-shared and the skip", "--force-shared" in p.stderr and SKIP + "=1" in p.stderr, True)
        cmd_line = [l.strip() for l in p.stderr.splitlines() if "install" in l and "--force-shared" in l and scan in l]
        check("and the install command is on a line of its own", len(cmd_line) >= 1, True)
        p = sh("python3", scan, "install", "--force-shared", shared)
        check("that command installs", p.returncode, 0)
        check("and then the commit passes", bash("git commit -m x", shared).returncode, 0)

        # an edited hook is not an installed hook
        edited = make("edited")
        install(edited)
        with open(os.path.join(edited, ".git", "hooks", "pre-commit"), "a") as f:
            f.write("echo edit\n")
        p = bash("git commit -m x", edited)
        check("an edited hook blocks", p.returncode, 2)
        check("and the message says it was changed", "changed after install" in p.stderr and "install again" in p.stderr, True)

        # where the repository cannot be worked out
        outer = os.path.join(tmp, "elsewhere")
        os.makedirs(outer)
        for label, cmd in (("cd $(...)", "cd $(echo /) && git commit -m x"), ("pushd $X", "pushd $X && git commit -m x"),
                           ("git -C $DIR", "git -C $DIR commit -m x"), ("$(printf git) commit", "$(printf git) commit -m x"),
                           ("cd to a missing folder", "cd nosuch && git push")):
            p = bash(cmd, outer)
            check(f"from a non-repo, {label}: blocks", p.returncode, 2)
            check(f"from a non-repo, {label}: says it cannot tell", "cannot tell which repository" in p.stderr, True)
        p = bash("git init x && cd x && git commit -m x", outer)
        check("init and commit in one command blocks", p.returncode == 2 and "git init" in p.stderr, True)
        check("clone and push in one command blocks", bash(f"git clone {origin} y && cd y && git push", outer).returncode, 2)

        # another repository by option or variable, from a repository that has the hooks
        other = make("other")
        gd = os.path.join(other, ".git")
        for label, cmd in (("--git-dir", f"git --git-dir={gd} --work-tree={other} commit -m x"),
                           ("GIT_DIR inline", f"GIT_DIR={gd} GIT_WORK_TREE={other} git commit -m x"),
                           ("GIT_DIR exported", f"export GIT_DIR={gd}; git commit -m x"),
                           ("-C", f"git -C {other} push"), ("cd", f"cd {other} && git commit -m x"),
                           ("bash -c cd", f"bash -c 'cd {other} && git commit -m x'")):
            check(f"another repo without hooks, {label}: blocks", bash(cmd, bare).returncode, 2)
        check("GIT_DIR in the environment", bash("git commit -m x", outer, {"GIT_DIR": gd, "GIT_WORK_TREE": other}).returncode, 2)
        # a hooks path of the repo's own is followed
        hp = make("hp")
        sh("git", "-C", hp, "config", "core.hooksPath", os.path.join(tmp, "hp-hooks"))
        check("a repo-level hooksPath without hooks blocks", bash("git commit -m x", hp).returncode, 2)
        install(hp)
        check("and passes once installed there", bash("git commit -m x", hp).returncode, 0)
        # the scanner copy the hooks run is gone
        gone = make("gone")
        sh("python3", scan, "install", gone, extra={"LEAN_SECRET_SCAN_HOME": os.path.join(tmp, "home2")})
        os.remove(os.path.join(tmp, "home2", "secret-scan"))
        p = bash("git commit -m x", gone)
        check("a missing scanner copy blocks", p.returncode == 2 and "missing" in p.stderr, True)

        # Write / Edit / MultiEdit / NotebookEdit
        repo = bare
        write = lambda rel, text: put(repo, rel, text)  # noqa: E731
        write(".gitignore", ".env.local\n")
        commit(repo, "ignore")
        check("Write of a key into a tracked path blocks",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "k=" + google}, repo).returncode, 2)
        p = hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "a\nk=" + google}, repo)
        check("Write message names file and line", "src.js:2  google-api-key (AIza)" in p.stderr, True)
        check("Write message hides the value", google in p.stderr, False)
        check("Write into a gitignored file passes",
              hook("Write", {"file_path": os.path.join(repo, ".env.local"), "content": "K=" + google}, repo).returncode, 0)
        check("Write of clean text passes",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": "hello"}, repo).returncode, 0)
        check("Edit blocks",
              hook("Edit", {"file_path": os.path.join(repo, "a.txt"), "old_string": "a", "new_string": google}, repo).returncode, 2)
        check("MultiEdit blocks", hook("MultiEdit", {"file_path": os.path.join(repo, "a.txt"), "edits": [
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": google}]}, repo).returncode, 2)
        check("Write outside a repository passes",
              hook("Write", {"file_path": os.path.join(tmp, "loose", "x.txt"), "content": google}, tmp).returncode, 0)
        check("Write into a new folder of a repo blocks",
              hook("Write", {"file_path": os.path.join(repo, "new", "deep", "x.js"), "content": google}, repo).returncode, 2)
        check("allow-secret on a written line",
              hook("Write", {"file_path": os.path.join(repo, "s.js"), "content": google + " // allow-secret"}, repo).returncode, 0)
        check("skip variable on a write",
              hook("Write", {"file_path": os.path.join(repo, "s.js"), "content": google}, repo, {SKIP: "1"}).returncode, 0)
        p = hook("NotebookEdit", {"notebook_path": os.path.join(repo, "n.ipynb"), "new_source": "k=" + google}, repo)
        check("NotebookEdit blocks", p.returncode == 2 and "n.ipynb:1" in p.stderr, True)
        check("NotebookEdit with a list source blocks",
              hook("NotebookEdit", {"notebook_path": os.path.join(repo, "n.ipynb"), "new_source": ["a", google]}, repo).returncode, 2)
        check("a non-string content does not crash",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": 5}, repo).returncode, 0)
        check("a list content is scanned",
              hook("Write", {"file_path": os.path.join(repo, "src.js"), "content": ["x", google]}, repo).returncode, 2)
        write("ign.txt", "x\n")
        sh("git", "-C", repo, "add", "-f", "ign.txt")
        write(".gitignore", ".env.local\nign.txt\n")
        commit(repo, "ign")
        check("a tracked file that is also gitignored is scanned",
              hook("Write", {"file_path": os.path.join(repo, "ign.txt"), "content": google}, repo).returncode, 2)
        # C: the folder the file sits in is not the top of the repo
        write("sub/deep/conf.js", "x\n")
        sh("git", "-C", repo, "add", "-f", "sub/deep/conf.js")
        write(".gitignore", ".env.local\nign.txt\nconf.js\n")
        commit(repo, "sub")
        check("a tracked, ignored-by-name file in a subfolder is scanned",
              hook("Write", {"file_path": os.path.join(repo, "sub", "deep", "conf.js"), "content": google}, repo).returncode, 2)
        check("an untracked ignored file in a subfolder passes",
              hook("Write", {"file_path": os.path.join(repo, "sub", "deep", "x", "conf.js"), "content": google},
                   os.path.join(repo, "sub")).returncode, 0)
        check("a file inside .git passes",
              hook("Write", {"file_path": os.path.join(repo, ".git", "config"), "content": "url = " + google}, repo).returncode, 0)
        check("a file inside .git, from a subfolder, passes",
              hook("Write", {"file_path": os.path.join(repo, ".git", "hooks", "x"), "content": google},
                   os.path.join(repo, "sub")).returncode, 0)
        wt = os.path.join(tmp, "wt")
        sh("git", "-C", repo, "worktree", "add", "-q", wt)
        check("a file in a worktree's own git dir passes",
              hook("Write", {"file_path": os.path.join(repo, ".git", "worktrees", "wt", "x"), "content": google}, wt).returncode, 0)
        check("a tracked file in a worktree is scanned",
              hook("Write", {"file_path": os.path.join(wt, "a.txt"), "content": google}, wt).returncode, 2)
        link = os.path.join(tmp, "lnk")
        os.symlink(repo, link)
        p = hook("Write", {"file_path": os.path.join(link, "sub", "src.js"), "content": google}, tmp)
        check("a write through a symlinked folder blocks", p.returncode, 2)
        check("and is labelled by the path inside the repo", "sub/src.js:1" in p.stderr and ".." not in p.stderr, True)

        # a repo's own config must not run code during a write scan
        trap = make("trap")
        put(trap, "a.txt", "hi\n")
        commit(trap, "base")
        mark = os.path.join(tmp, "ran")
        script = os.path.join(tmp, "trap.sh")
        with open(script, "w") as f:
            f.write(f"#!/bin/sh\ntouch '{mark}'\ncat \"$1\" 2>/dev/null\n")
        os.chmod(script, 0o755)
        sh("git", "-C", trap, "config", "core.fsmonitor", script)
        sh("git", "-C", trap, "status")
        check("the trap fires on plain git", os.path.exists(mark), True)
        os.remove(mark)
        check("a write scan finds the key",
              hook("Write", {"file_path": os.path.join(trap, "a.txt"), "content": google}, trap).returncode, 2)
        check("and ran no repo-configured command", os.path.exists(mark), False)

        # a scanner that is missing or crashes blocks with the reason
        plug = os.path.join(tmp, "plug")
        shutil.copytree(os.path.join(os.path.dirname(here), "..", "lib"), os.path.join(plug, "lib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(plug, "hooks"))
        os.makedirs(os.path.join(plug, "tools"))
        copy = os.path.join(plug, "hooks", "secret-scan.py")
        shutil.copy(here, copy)
        p = bash("git commit -m x", repo, script=copy)
        check("a missing scanner blocks", p.returncode, 2)
        check("and says why", "could not run" in p.stderr and "nothing was checked" in p.stderr, True)
        check("hook error ignored is not printed", "hook error ignored" in p.stderr, False)
        check("unrelated commands still pass without the scanner", bash("git status", repo, script=copy).returncode, 0)
        with open(os.path.join(plug, "tools", "secret-scan"), "w") as f:
            f.write("raise RuntimeError('boom')\n")
        p = bash("git commit -m x", repo, script=copy)
        check("a crashing scanner blocks", p.returncode == 2 and "boom" in p.stderr, True)
        check("a write is blocked too", hook("Write", {"file_path": os.path.join(repo, "a.js"), "content": "x"},
                                             repo, script=copy).returncode, 2)
        check("skip still works without the scanner", bash("git commit -m x", repo, {SKIP: "1"}, script=copy).returncode, 0)

        # blocks are recorded as hook fires
        conn = sqlite3.connect(env["LEAN_DB"])
        rows = conn.execute("SELECT hook, outcome FROM hook_fires WHERE hook = 'secret-scan.py'").fetchall()
        conn.close()
        check("blocks are recorded in hook_fires", bool(rows) and all(r == ("secret-scan.py", "block") for r in rows), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

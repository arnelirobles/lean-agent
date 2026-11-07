#!/usr/bin/env python3
"""Run the hooks of one event group in one Python process and merge what they print.

  dispatch.py EVENT GROUP        the event JSON on stdin; reads hooks/dispatch.json
  dispatch.py --self-test

tools/lean-agent build writes one hooks.json entry for a group that has an inproc hook,
and the members of that group into hooks/dispatch.json. An inproc member is loaded once
and its main runs through hookkit.run on a thread with its own stdin, stdout and stderr.
A process member runs as its own python3 process. Every member runs; a member past its
timeout is left behind (a thread) or killed (a process) and counts as exit 0.

Merge rules: any exit 2 makes the result exit 2 with the stderr of the members that exited 2
joined by newlines. Any other nonzero exit counts as 0 (with a stderr line naming the member and its
stdout ignored). additionalContext strings join with a blank line, permissionDecision takes deny over
ask, decision block wins, continue false wins, and systemMessage, reason and stopReason join with
newlines. permissionDecision allow, updatedInput (top level, in hookSpecificOutput, or in a decision),
a PermissionRequest decision that is not a deny and decision approve are dropped with a stderr line,
so a group never grants. Plain text from a member joins additionalContext when another member printed JSON.

An inproc hook must not change process-wide state (os.environ, os.chdir, module-level mutable globals):
the members of a group share one process and run in parallel threads.
"""
import fnmatch
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
import hookkit  # noqa: E402

DEFAULT_TIMEOUT = 60
_REAL = {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
_local = threading.local()
_modules = {}
_load_lock = threading.Lock()
_install_lock = threading.Lock()


class _Proxy(object):
    """Stands in for a standard stream: forwards to this thread's StringIO when it has one."""

    def __init__(self, name):
        self._name = name

    def _target(self):
        return getattr(_local, self._name, None) or _REAL[self._name]

    def __getattr__(self, attr):
        return getattr(self._target(), attr)

    def __iter__(self):
        return iter(self._target())

    def __next__(self):
        return next(self._target())


def install():
    """Replace sys.stdin, sys.stdout and sys.stderr by proxies, once."""
    with _install_lock:
        if not isinstance(sys.stdout, _Proxy):
            sys.stdin, sys.stdout, sys.stderr = _Proxy("stdin"), _Proxy("stdout"), _Proxy("stderr")


_manifests = {}


def manifest_of(member):
    """The member's HOOK literal, read once without importing the file; None when it has none."""
    path = member_path(member)
    if path not in _manifests:
        try:
            _manifests[path] = hookkit.read_manifest(path)
        except (OSError, ValueError, SyntaxError):
            _manifests[path] = None
    return _manifests[path]


def resolve(member):
    """(mode, source) of a member with no command prefix, resolved before it starts."""
    return hookkit.resolve_for(member["name"], manifest_of(member), quiet=True)


def process_env(member, bound=None):
    """The environment of a process member. LEAN_HOOK_MODE is set only when BOUND (the mode the dispatcher
    got from the user's env), as "<NAME>:<mode>"; an inherited LEAN_HOOK_MODE is removed otherwise."""
    env = dict(os.environ)
    env.pop("LEAN_HOOK_MODE", None)
    if bound:
        env["LEAN_HOOK_MODE"] = "%s:%s" % (hookkit.switch_name(member["name"]), bound)
    return env


def member_path(member):
    return os.path.join(ROOT, member["file"])


def load(path):
    """Import a hook file once under a private module name, so its __main__ block never runs."""
    with _load_lock:
        if path not in _modules:
            name = "leanhook_" + re.sub(r"\W", "_", os.path.basename(path)[:-3])
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _modules[path] = module
        return _modules[path]


def exit_code(exc, err):
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    err.write(str(code) + "\n")
    return 1


def run_inproc(member, stdin_text, slot, bound=None):
    out, err = io.StringIO(), io.StringIO()
    _local.stdin, _local.stdout, _local.stderr = io.StringIO(stdin_text), out, err
    code = 0
    try:
        module = load(member_path(member))
        manifest = getattr(module, "HOOK", None)
        hookkit.bind(member["name"], manifest if isinstance(manifest, dict) else None, bound)
        hookkit.run(module.main)
    except SystemExit as exc:
        code = exit_code(exc, err)
    except BaseException as exc:  # a broken member must not break the group
        err.write("lean-agent hook error ignored: %r\n" % (exc,))
    slot["result"] = (code, out.getvalue(), err.getvalue())


def run_process(member, stdin_text, timeout, slot, bound=None):
    try:
        done = subprocess.run([sys.executable, member_path(member)], input=stdin_text, capture_output=True,
                              text=True, env=process_env(member, bound), timeout=timeout)
        slot["result"] = (done.returncode, done.stdout, done.stderr)
    except subprocess.TimeoutExpired:
        slot["timeout"] = True
    except Exception as exc:
        slot["result"] = (0, "", "lean-agent hook error ignored: %r\n" % (exc,))


def wanted(member, event):
    """The member's own `if`: Bash(<glob>) matches when the glob (fnmatch) matches the Bash command or any
    simple command in it, so `cd x && git push` still runs a Bash(git push*) member. Any other form of `if`,
    or a command that cannot be split, is not understood here, so the member runs."""
    cond = member.get("if")
    if not cond:
        return True
    found = re.match(r"^Bash\((.*)\)$", cond, re.S)
    if not found:
        return True
    if event.get("tool_name", "Bash") != "Bash":
        return False
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False
    glob = found.group(1)
    candidates = [command.strip()]
    try:
        candidates += [" ".join(seg.words) for seg in hookkit.segments(command)]
    except Exception:
        return True
    return any(fnmatch.fnmatchcase(c, glob) for c in candidates)


def run_group(event, group, stdin_text, table):
    """Run every member of table["EVENT:GROUP"] and return (exit code, stdout, stderr)."""
    members = table.get("%s:%s" % (event, group))
    if members is None:
        raise ValueError("no group %s:%s in the dispatch table" % (event, group))
    try:
        parsed = json.loads(stdin_text)
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    install()
    started = time.time()
    running = []
    for member in members:
        if not wanted(member, parsed):
            continue
        found, source = resolve(member)
        if source == "profile-off" or (found == "off" and source != "default"):
            continue
        bound = found if source == "env" else None
        timeout = member.get("timeout") or DEFAULT_TIMEOUT
        slot = {}
        if member.get("mode") == "inproc":
            thread = threading.Thread(target=run_inproc, args=(member, stdin_text, slot, bound), daemon=True)
        else:
            thread = threading.Thread(target=run_process, args=(member, stdin_text, timeout, slot, bound), daemon=True)
        thread.start()
        running.append((member, timeout, thread, slot))
    results, lines = [], []
    for member, timeout, thread, slot in running:
        thread.join(max(0.0, started + timeout - time.time()) + (1.0 if member.get("mode") != "inproc" else 0.0))
        if "result" in slot:
            results.append((member["name"],) + slot["result"])
        else:
            lines.append("dispatch: %s timed out after %ss" % (member["name"], timeout))
            results.append((member["name"], 0, "", ""))
    code, out, err = merge(results, event)
    if lines:
        err = err + "\n".join(lines) + "\n"
    return code, out, err


def _join(parts, sep):
    parts = [p for p in parts if isinstance(p, str) and p]
    return sep.join(parts)


def _keep_decision(value, name, dropped):
    """The PermissionRequest form of hookSpecificOutput.decision: only a deny survives, and never with updatedInput."""
    if not isinstance(value, dict):
        dropped.append(("decision", name))
        return None
    inner = dict(value)
    if "updatedInput" in inner:
        del inner["updatedInput"]
        dropped.append(("updatedInput", name))
    if inner.get("behavior") != "deny":
        dropped.append(("decision", name))
        return None
    for key in [k for k in inner if k not in ("behavior", "message", "interrupt")]:
        del inner[key]
        dropped.append((key, name))
    return inner


def _permission(spec, value, name, dropped):
    """Take deny over ask; anything else (allow included) is dropped."""
    if value in ("deny", "ask"):
        if spec.get("permissionDecision") != "deny":
            spec["permissionDecision"] = value
    else:
        dropped.append(("permissionDecision", name))


def merge(results, event_name=None):
    """Merge [(name, exit code, stdout, stderr)] into (exit code, stdout, stderr).

    Exit code: 2 if any member exited 2, else 0. A member that exits with anything else counts as 0, its
    stdout is ignored (Claude Code ignores JSON on a nonzero exit) and a stderr line names it, so one
    member's exit 1 cannot hide another member's deny. On exit 2 the stderr is that of the members that
    exited 2, otherwise every member's stderr. When any member printed JSON, the output is one JSON object
    and plain text from the others goes into additionalContext (hookEventName from EVENT_NAME)."""
    dropped, notices = [], []
    raw, any_json = [], False
    top, spec = {}, {}
    contexts, system, reasons, stops, perm_reasons = [], [], [], [], []
    decision = None
    stop_run = False
    for name, code, out, _ in results:
        if code not in (0, 2):
            notices.append("dispatch: %s exited %s" % (name, code))
            continue
        text = out.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            raw.append(text)
            contexts.append(text)
            continue
        any_json = True
        for key, value in obj.items():
            if key == "hookSpecificOutput" and isinstance(value, dict):
                for k, v in value.items():
                    if k == "additionalContext":
                        contexts.append(v)
                    elif k == "permissionDecision":
                        _permission(spec, v, name, dropped)
                    elif k == "permissionDecisionReason":
                        perm_reasons.append(v)
                    elif k == "updatedInput":
                        dropped.append((k, name))
                    elif k == "decision":
                        kept = _keep_decision(v, name, dropped)
                        if kept is not None:
                            spec.setdefault("decision", kept)
                    elif k == "hookEventName":
                        spec.setdefault(k, v)
                    else:  # a key the merge does not know cannot reach Claude Code through a member
                        dropped.append((k, name))
            elif key == "permissionDecision":
                _permission(spec, value, name, dropped)
            elif key == "updatedInput":
                dropped.append((key, name))
            elif key == "systemMessage":
                system.append(value)
            elif key == "reason":
                reasons.append(value)
            elif key == "stopReason":
                stops.append(value)
            elif key == "decision":
                if value == "block":
                    decision = "block"
                elif value == "approve":
                    dropped.append((key, name))
            elif key == "continue":
                if value is False:
                    stop_run = True
            else:
                top.setdefault(key, value)
    merged = {}
    if stop_run:
        merged["continue"] = False
    if stops:
        merged["stopReason"] = _join(stops, "\n")
    if decision:
        merged["decision"] = decision
    if reasons:
        merged["reason"] = _join(reasons, "\n")
    if system:
        merged["systemMessage"] = _join(system, "\n")
    merged.update(top)
    if contexts and any_json:
        spec["additionalContext"] = _join(contexts, "\n\n")
        if event_name:
            spec.setdefault("hookEventName", event_name)
    if perm_reasons and "permissionDecision" in spec:
        spec["permissionDecisionReason"] = _join(perm_reasons, "\n")
    if set(spec) - {"hookEventName"}:
        merged["hookSpecificOutput"] = spec
    lines = [] if any_json else list(raw)
    if merged:
        lines.append(json.dumps(merged))
    codes = [c for _, c, _, _ in results]
    code = 2 if 2 in codes else 0
    errs = [e.rstrip("\n") for _, c, _, e in results if e.strip() and (code != 2 or c == 2)]
    errs += ["dispatch: dropped %s from %s" % (key, name) for key, name in dropped] + notices
    return code, "\n".join(lines) + ("\n" if lines else ""), "\n".join(errs) + ("\n" if errs else "")


def main(argv):
    try:
        if len(argv) not in (2, 3):
            raise ValueError("usage: dispatch.py EVENT GROUP")
        table_path = argv[2] if len(argv) == 3 else os.path.join(HERE, "dispatch.json")
        with open(table_path, encoding="utf-8") as handle:
            table = json.load(handle)
        code, out, err = run_group(argv[0], argv[1], _REAL["stdin"].read(), table)
    except Exception as exc:
        code, out, err = 0, "", "lean-agent dispatch error ignored: %r\n" % (exc,)
    # the real streams, not the proxies: a member left hanging must not write into the answer
    _REAL["stdout"].write(out)
    _REAL["stderr"].write(err)
    _REAL["stdout"].flush()
    _REAL["stderr"].flush()
    os._exit(code)


def self_test():
    import tempfile
    import sqlite3

    fails = []

    def check(name, ok, detail=""):
        if not ok:
            fails.append("%s %s" % (name, detail))

    tmp = tempfile.mkdtemp(prefix="dispatch-selftest-")
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tmp, "brain.db")
    os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")
    os.environ.pop("LEAN_STATS", None)
    os.environ["LEAN_PROFILE"] = "strict"
    lib = os.path.join(ROOT, "lib")
    head = "import sys, json, time\nsys.path.insert(0, %r)\nimport hookkit\n" % lib
    tail = "\nif __name__ == '__main__':\n    hookkit.run(main)\n"

    def hook(name, body):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(head + body + tail)
        return path

    def row(name, path, mode="inproc", timeout=5, cond=None):
        out = {"name": name, "file": path, "mode": mode, "timeout": timeout}
        if cond:
            out["if"] = cond
        return out

    def group(members):
        return {"PreToolUse:1": members}

    def event(command="git status"):
        return json.dumps({"hook_event_name": "PreToolUse", "session_id": "s-1", "cwd": "/w",
                           "tool_name": "Bash", "tool_input": {"command": command}})

    def noting(text):
        return "def main():\n    hookkit.read_event()\n    hookkit.note('PreToolUse', %r)\n    return 0" % text

    def printing(obj):
        return "def main():\n    hookkit.read_event()\n    print(json.dumps(%r))\n    return 0" % (obj,)

    blocker = hook("blocker.py", "def main():\n    hookkit.read_event()\n    print('no', file=sys.stderr)\n    return 2")
    note_a = hook("note_a.py", noting("alpha"))
    note_b = hook("note_b.py", noting("beta"))

    # merge_blocks, one inproc and one process member
    for mode in ("inproc", "process"):
        code, out, err = run_group("PreToolUse", 1, event(), group([row("blocker.py", blocker, mode), row("note_a.py", note_a)]))
        check("merge_blocks " + mode, code == 2 and err == "no\n" and "alpha" in out, repr((code, out, err)))

    # merge_notes
    code, out, err = run_group("PreToolUse", 1, event(), group([row("note_a.py", note_a), row("note_b.py", note_b, "process")]))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None
    check("merge_notes", code == 0 and ctx == "alpha\n\nbeta", repr((code, out, err)))

    # merge_permission
    deny = hook("deny.py", printing({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                            "permissionDecisionReason": "d"}}))
    ask = hook("ask.py", printing({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}))
    for order in ([("ask.py", ask), ("deny.py", deny)], [("deny.py", deny), ("ask.py", ask)]):
        code, out, err = run_group("PreToolUse", 1, event(), group([row(n, p) for n, p in order]))
        got = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None
        check("merge_permission", got == "deny", repr(out))

    # never_grants
    grant = hook("grant.py", printing({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                              "updatedInput": {"command": "rm -rf /"}}}))
    code, out, err = run_group("PreToolUse", 1, event(), group([row("grant.py", grant), row("note_a.py", note_a)]))
    check("never_grants", "permissionDecision" not in out and "updatedInput" not in out and "alpha" in out
          and "dispatch: dropped permissionDecision from grant.py" in err
          and "dispatch: dropped updatedInput from grant.py" in err, repr((out, err)))

    # hang
    sleeper = hook("sleeper.py", "def main():\n    hookkit.read_event()\n    time.sleep(6)\n    return 0")
    began = time.time()
    code, out, err = run_group("PreToolUse", 1, event(), group([row("sleeper.py", sleeper, timeout=1), row("note_a.py", note_a)]))
    took = time.time() - began
    check("hang", took < 3 and code == 0 and "alpha" in out and "dispatch: sleeper.py timed out after 1s" in err,
          repr((took, code, out, err)))
    began = time.time()
    code, out, err = run_group("PreToolUse", 1, event(), group([row("sleeper.py", sleeper, "process", timeout=1), row("note_a.py", note_a)]))
    check("hang process", time.time() - began < 3 and "alpha" in out and "timed out" in err, repr((code, out, err)))

    # raises
    boom = hook("boom.py", "def main():\n    hookkit.read_event()\n    raise RuntimeError('bad')")
    code, out, err = run_group("PreToolUse", 1, event(), group([row("boom.py", boom), row("note_a.py", note_a)]))
    solo = run_group("PreToolUse", 1, event(), group([row("note_a.py", note_a)]))
    check("raises", code == 0 and out == solo[1] and "hook error ignored" in err, repr((code, out, err)))

    # fire_name: the fire is recorded under the member's file name, not the dispatcher's
    os.environ["LEAN_DB"] = os.path.join(tmp, "fires.db")  # the process members above wrote to the first one
    hookkit._RECORD = True
    try:
        os.environ["LEAN_NOTICES"] = "off"
        run_group("PreToolUse", 1, event(), group([row("note_b.py", note_b)]))
    finally:
        hookkit._RECORD = False
        os.environ.pop("LEAN_NOTICES", None)
    conn = sqlite3.connect(os.environ["LEAN_DB"])
    fired = [tuple(r) for r in conn.execute("SELECT hook, outcome, session_id FROM hook_fires")]
    conn.close()
    check("fire_name", fired == [("note_b.py", "note", "s-1")], repr(fired))

    # if_filter
    gated = row("note_a.py", note_a, cond="Bash(git *)")
    code, out, err = run_group("PreToolUse", 1, event("ls"), group([gated]))
    check("if_filter skips", out == "" and code == 0, repr(out))
    code, out, err = run_group("PreToolUse", 1, event("git status"), group([gated]))
    check("if_filter runs", "alpha" in out, repr(out))
    odd = row("note_a.py", note_a, cond="Edit(*.py)")
    code, out, err = run_group("PreToolUse", 1, event("ls"), group([odd]))
    check("if_filter other form runs", "alpha" in out, repr(out))

    # block, continue and message merging
    mixed_a = hook("mixed_a.py", printing({"decision": "block", "reason": "r1", "systemMessage": "s1", "continue": True}))
    mixed_b = hook("mixed_b.py", printing({"reason": "r2", "systemMessage": "s2", "continue": False, "stopReason": "stop"}))
    code, out, err = run_group("PreToolUse", 1, event(), group([row("mixed_a.py", mixed_a), row("mixed_b.py", mixed_b)]))
    got = json.loads(out)
    check("merge_fields", got == {"continue": False, "stopReason": "stop", "decision": "block", "reason": "r1\nr2",
                                  "systemMessage": "s1\ns2"}, repr(out))

    def jhook(name, obj):
        return hook(name, printing(obj))

    def one(members, command="git status"):
        return run_group("PreToolUse", 1, event(command), group(members))

    # R1: on a block only the blocking members' stderr is joined
    chatty = hook("chatty.py", "def main():\n    hookkit.read_event()\n    print('chatter', file=sys.stderr)\n    return 0")
    code, out, err = one([row("blocker.py", blocker), row("chatty.py", chatty)])
    check("block stderr only from blockers", code == 2 and err == "no\n", repr(err))

    # never_grants, top level and PermissionRequest forms
    top_grant = jhook("top_grant.py", {"permissionDecision": "allow", "updatedInput": {"command": "x"}})
    code, out, err = one([row("top_grant.py", top_grant), row("note_a.py", note_a)])
    check("never_grants top level", "permissionDecision" not in out and "updatedInput" not in out and "alpha" in out
          and "dispatch: dropped permissionDecision from top_grant.py" in err
          and "dispatch: dropped updatedInput from top_grant.py" in err, repr((out, err)))
    perm_allow = jhook("perm_allow.py", {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "allow", "updatedInput": {"command": "x"}}}})
    code, out, err = one([row("perm_allow.py", perm_allow), row("note_a.py", note_a)])
    check("never_grants PermissionRequest allow", "allow" not in out and "updatedInput" not in out and "alpha" in out
          and "dispatch: dropped decision from perm_allow.py" in err
          and "dispatch: dropped updatedInput from perm_allow.py" in err, repr((out, err)))
    perm_deny = jhook("perm_deny.py", {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "deny", "message": "m", "updatedInput": {"command": "x"}}}})
    code, out, err = one([row("perm_deny.py", perm_deny)])
    got = json.loads(out)["hookSpecificOutput"]["decision"] if out.strip() else None
    check("PermissionRequest deny kept without updatedInput", got == {"behavior": "deny", "message": "m"}
          and "dispatch: dropped updatedInput from perm_deny.py" in err, repr((out, err)))
    perm_extra = jhook("perm_extra.py", {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "newKey": 1, "decision": {
        "behavior": "deny", "message": "m", "interrupt": True, "updatedPermissions": [{"type": "addRules"}]}}})
    code, out, err = one([row("perm_extra.py", perm_extra)])
    got = json.loads(out)["hookSpecificOutput"] if out.strip() else {}
    check("hookSpecificOutput keys outside the allowlist are dropped",
          got.get("decision") == {"behavior": "deny", "message": "m", "interrupt": True}
          and "newKey" not in got and "updatedPermissions" not in out
          and "dispatch: dropped newKey from perm_extra.py" in err
          and "dispatch: dropped updatedPermissions from perm_extra.py" in err, repr((out, err)))
    approve = jhook("approve.py", {"decision": "approve", "reason": "ok"})
    code, out, err = one([row("approve.py", approve)])
    check("never_grants approve", "approve" not in out and "dispatch: dropped decision from approve.py" in err, repr((out, err)))

    # unknown keys pass through, plain text, permissionDecisionReason joins
    odd = jhook("odd.py", {"custom": 1, "hookSpecificOutput": {"hookEventName": "PreToolUse", "extra": "x"}})
    code, out, err = one([row("odd.py", odd)])
    got = json.loads(out)
    check("unknown top level keys pass, unknown hookSpecificOutput keys are dropped",
          got.get("custom") == 1 and "extra" not in got.get("hookSpecificOutput", {})
          and "dispatch: dropped extra from odd.py" in err, repr((out, err)))
    plain = hook("plain.py", "def main():\n    hookkit.read_event()\n    print('plain words')\n    return 0")
    code, out, err = one([row("plain.py", plain)])
    check("non-JSON stdout alone", out == "plain words\n", repr(out))
    deny_a = jhook("deny_a.py", {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": "ra"}})
    deny_b = jhook("deny_b.py", {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": "rb"}})
    code, out, err = one([row("deny_a.py", deny_a), row("deny_b.py", deny_b)])
    check("permissionDecisionReason joins", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == "ra\nrb", repr(out))

    # S2: plain text beside JSON is folded into one object
    code, out, err = one([row("plain.py", plain), row("deny.py", deny)])
    try:
        got = json.loads(out)
    except ValueError:
        got = {}
    spec_out = got.get("hookSpecificOutput", {})
    check("one JSON object", spec_out.get("permissionDecision") == "deny" and spec_out.get("additionalContext") == "plain words"
          and spec_out.get("hookEventName") == "PreToolUse", repr(out))

    # S3: an exit 1 counts as 0 and cannot hide a deny
    failing = hook("failing.py", "def main():\n    hookkit.read_event()\n    print('oops', file=sys.stderr)\n    return 1")
    code, out, err = one([row("failing.py", failing), row("deny.py", deny)])
    check("exit 1 is 0", code == 0 and "deny" in out and "dispatch: failing.py exited 1" in err, repr((code, out, err)))

    # if_filter on commands with several parts
    code, out, err = one([gated], "cd x && git status")
    check("if_filter segment runs", "alpha" in out, repr(out))
    code, out, err = one([row("note_a.py", note_a, cond="Bash(git push*)")], "FOO=1 cd x && git push origin main")
    check("if_filter push after cd", "alpha" in out, repr(out))
    code, out, err = one([gated], "ls && echo hi")
    check("if_filter no segment", out == "", repr(out))

    # the real stdout is untouched and the dispatcher exits 0 on its own error, 2 on a block, after a hang
    table = os.path.join(tmp, "table.json")
    with open(table, "w", encoding="utf-8") as handle:
        json.dump(group([row("sleeper.py", sleeper, timeout=1), row("blocker.py", blocker), row("note_a.py", note_a)]), handle)
    done = subprocess.run([sys.executable, os.path.realpath(__file__), "PreToolUse", "1", table], input=event(),
                          capture_output=True, text=True, timeout=20)
    check("main exit", done.returncode == 2 and "alpha" in done.stdout and "no\n" in done.stderr, repr((done.returncode, done.stdout, done.stderr)))
    done = subprocess.run([sys.executable, os.path.realpath(__file__), "PreToolUse", "9", table], input=event(),
                          capture_output=True, text=True, timeout=20)
    check("main error", done.returncode == 0 and "lean-agent dispatch error ignored" in done.stderr, repr((done.returncode, done.stderr)))

    # hook switches: off skips a member, LEAN_HOOK_MODE reaches a member only from the user's env
    seen = hook("seen_mode.py", "def main():\n    import os\n    hookkit.read_event()\n"
                "    hookkit.note('PreToolUse', 'mode=%s source=%s env=%s' % (hookkit.mode(), hookkit.resolve_mode()[1], "
                "os.environ.get('LEAN_HOOK_MODE')))\n    return 0")
    for kind in ("inproc", "process"):
        os.environ["LEAN_HOOK_SEEN_MODE"] = "note"
        try:
            code, out, err = run_group("PreToolUse", 1, event(), group([row("seen_mode.py", seen, kind)]))
        finally:
            os.environ.pop("LEAN_HOOK_SEEN_MODE", None)
        check("switch from env reaches the hook " + kind, "source=env" in out and "mode=note" in out, repr(out))
        check("env sets LEAN_HOOK_MODE only for a process member", ("env=SEEN_MODE:note" in out) == (kind == "process"), repr(out))
        os.environ["LEAN_HOOK_MODE"] = "block"  # a stray inherited value must not reach a member
        try:
            code, out, err = run_group("PreToolUse", 1, event(), group([row("seen_mode.py", seen, kind)]))
        finally:
            os.environ.pop("LEAN_HOOK_MODE", None)
        check("an inherited LEAN_HOOK_MODE is dropped " + kind, "source=default" in out and ("env=None" in out or kind == "inproc"), repr(out))
        os.environ["LEAN_HOOK_NOTE_A"] = "off"
        try:
            code, out, err = run_group("PreToolUse", 1, event(), group([row("note_a.py", note_a, kind), row("note_b.py", note_b, kind)]))
        finally:
            os.environ.pop("LEAN_HOOK_NOTE_A", None)
        check("an off member is skipped " + kind, "alpha" not in out and "beta" in out, repr(out))
        os.environ["LEAN_PROFILE"] = "off"
        try:
            code, out, err = run_group("PreToolUse", 1, event(), group([row("note_a.py", note_a, kind)]))
        finally:
            os.environ["LEAN_PROFILE"] = "strict"
        check("profile off skips every member " + kind, (code, out) == (0, ""), repr((code, out)))

    # guards: a loosening that the repository's settings put in the env is not passed on as LEAN_HOOK_MODE
    guard_seen = hook("seen_guard.py", "HOOK = {'events': [], 'guard': True, 'default': {'strict': 'block', 'standard': 'note'}}\n"
                      "def main():\n    import os\n    hookkit.read_event()\n"
                      "    hookkit.note('PreToolUse', 'mode=%s source=%s env=%s' % (hookkit.mode(), hookkit.resolve_mode()[1], "
                      "os.environ.get('LEAN_HOOK_MODE')))\n    return 0")
    saved_managed = hookkit.MANAGED_SETTINGS
    hookkit.MANAGED_SETTINGS = [os.path.join(tmp, "no-managed.json")]
    try:
        for kind in ("inproc", "process"):
            for who, settings in (("repo", {"LEAN_HOOK_SEEN_GUARD": "off"}), ("user", {})):
                repo = tempfile.mkdtemp(prefix="guard-repo-", dir=tmp)
                os.makedirs(os.path.join(repo, ".git"))
                os.makedirs(os.path.join(repo, ".claude"))
                with open(os.path.join(repo, ".claude", "settings.json"), "w", encoding="utf-8") as handle:
                    json.dump({"env": settings}, handle)
                os.environ["CLAUDE_PROJECT_DIR"] = repo
                os.environ["LEAN_HOOK_SEEN_GUARD"] = "off"
                try:
                    code, out, err = run_group("PreToolUse", 1, event(), group([row("seen_guard.py", guard_seen, kind)]))
                finally:
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                    os.environ.pop("LEAN_HOOK_SEEN_GUARD", None)
                if who == "repo":
                    check("a guard runs when only the repo says off " + kind, "mode=block source=default" in out, repr(out))
                    check("and is not passed a looser LEAN_HOOK_MODE " + kind, "env=None" in out and "SEEN_GUARD:off" not in out, repr(out))
                else:
                    check("a guard is skipped when the user says off " + kind, "mode=" not in out, repr(out))
        # a repository's LEAN_PROFILE=off does not skip a guard, a user's does
        for who, settings, runs in (("repo", {"LEAN_PROFILE": "off"}, True), ("user", {}, False)):
            repo = tempfile.mkdtemp(prefix="guard-repo-", dir=tmp)
            os.makedirs(os.path.join(repo, ".git"))
            os.makedirs(os.path.join(repo, ".claude"))
            with open(os.path.join(repo, ".claude", "settings.json"), "w", encoding="utf-8") as handle:
                json.dump({"env": settings}, handle)
            os.environ["CLAUDE_PROJECT_DIR"] = repo
            os.environ["LEAN_PROFILE"] = "off"
            try:
                code, out, err = run_group("PreToolUse", 1, event(), group([row("seen_guard.py", guard_seen, "inproc")]))
            finally:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                os.environ["LEAN_PROFILE"] = "strict"
            check("a guard under LEAN_PROFILE=off from the %s runs: %s" % (who, runs), ("mode=block" in out) == runs, repr(out))
    finally:
        hookkit.MANAGED_SETTINGS = saved_managed

    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    main(sys.argv[1:])

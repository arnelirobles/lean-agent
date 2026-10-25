#!/usr/bin/env python3
"""Tell a coordinator while it happens when its context or its own lookups are costing the most.

PostToolUse hook on every tool. For the main session only, never a subagent, it adds a note:
  - once context passes LEAN_CONTEXT_WARN tokens (default 100000), and again at each further
    50000: the size, what a turn costs at list price, and to end the batch and resume from brain
  - once per session after LEAN_LOOKUP_WARN read-only lookups (default 10) by an Opus
    coordinator: send them to lean:search on the fast model
It only ever notes, never blocks. It reads the tail of the transcript, not the whole file, and
keeps a small counter file per session in the temp directory.

  LEAN_CONTEXT_WARN=off   turn off the context note (a number sets the threshold)
  LEAN_LOOKUP_WARN=off    turn off the lookup note (a number sets the count)
  context-cost.py --self-test
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
import leanstats  # noqa: E402

STEP = 50000
CONTEXT_DEFAULT = 100000
LOOKUP_DEFAULT = 10


def setting(name, default):
    """The number in an environment variable, default when unset or odd, None when it is off or 0."""
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    if value == "off":
        return None
    try:
        number = int(value)
    except ValueError:
        return default
    return number if number > 0 else None


def state_path(sid):
    return os.path.join(tempfile.gettempdir(), "lean-context-cost", sid + ".json")


def load(sid):
    try:
        with open(state_path(sid)) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save(sid, state):
    path = state_path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)


def is_subagent(event, path):
    return bool(event.get("agent_id")) or f"{os.sep}subagents{os.sep}" in path


def context_note(last, state, limit):
    """The context note when this turn passes a new step, else None. Updates state."""
    size = leanstats.prompt_size(last)
    if limit is None or size < limit:
        state["step"] = -1
        return None
    step = (size - limit) // STEP
    if step <= state.get("step", -1):
        return None
    state["step"] = step
    return (f"Context is {size // 1000}k tokens. A turn at this size costs about "
            f"{leanstats.message_cost(last):.2f} USD at list price, and every later turn costs more. "
            "End the batch and resume from brain: file what is open with `brain add`, then continue "
            "in a fresh session. LEAN_CONTEXT_WARN=off turns this note off.")


def lookup_note(event, last, state, limit):
    """Count a read-only lookup by an Opus coordinator and return the note once the count is reached."""
    if limit is None or "opus" not in last["model"]:
        return None
    if leanstats.tool_kind(event.get("tool_name"), event.get("tool_input")) != "lookup":
        return None
    state["lookups"] = state.get("lookups", 0) + 1
    if state["lookups"] < limit or state.get("lookup_noted"):
        return None
    state["lookup_noted"] = True
    return (f"This session has run {state['lookups']} read-only lookups (grep, cat, git log, gh view, "
            "Read, Grep, Glob) on Opus. Send them to a lean:search agent on the fast model "
            "instead. LEAN_LOOKUP_WARN=off turns this note off.")


def main():
    event = hookkit.read_event()
    sid = str(event.get("session_id") or "")
    path = event.get("transcript_path")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", sid) or not isinstance(path, str) or is_subagent(event, path):
        return 0
    limit, lookups = setting("LEAN_CONTEXT_WARN", CONTEXT_DEFAULT), setting("LEAN_LOOKUP_WARN", LOOKUP_DEFAULT)
    if limit is None and lookups is None:
        return 0
    last = leanstats.last_usage(path)
    if not last:
        return 0
    state = load(sid)
    notes = [n for n in (context_note(last, state, limit), lookup_note(event, last, state, lookups)) if n]
    save(sid, state)
    if notes:
        hookkit.note("PostToolUse", " ".join(notes))
    return 0


def self_test():
    import contextlib
    import io
    import time

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    with tempfile.TemporaryDirectory() as tmp:
        saved_tmp = tempfile.tempdir
        tempfile.tempdir = tmp
        saved = {k: os.environ.pop(k, None) for k in ("LEAN_CONTEXT_WARN", "LEAN_LOOKUP_WARN")}
        try:
            transcript = os.path.join(tmp, "s.jsonl")

            def turn(size, model="claude-opus-5-5"):
                usage = {"input_tokens": 1, "cache_read_input_tokens": size - 1, "output_tokens": 10}
                return json.dumps({"type": "assistant", "message": {"id": "m%d" % size, "model": model, "usage": usage}}) + "\n"

            def fire(size, name="Edit", tool_input=None, sid="sess-1", extra=None, path=None, model="claude-opus-5-5"):
                with open(path or transcript, "a") as fh:
                    fh.write(turn(size, model))
                event = {"session_id": sid, "transcript_path": path or transcript, "tool_name": name,
                         "tool_input": tool_input or {}, "hook_event_name": "PostToolUse"}
                event.update(extra or {})
                old = sys.stdin
                sys.stdin = io.StringIO(json.dumps(event))
                out, err = io.StringIO(), io.StringIO()
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = main()
                finally:
                    sys.stdin = old
                text = ""
                if out.getvalue():
                    text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
                return code, text, err.getvalue()

            check("below the threshold is quiet", fire(60000), (0, "", ""))
            check("just under it is quiet", fire(99999), (0, "", ""))
            code, text, err = fire(100000)
            check("at the threshold it notes", (code, err, "100k tokens" in text, "brain" in text, "USD at list price" in text),
                  (0, "", True, True, True))
            cost = leanstats.message_cost(leanstats.usage_of({}, {"model": "claude-opus-5-5", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 99999, "output_tokens": 10}}))
            check("it names the turn's list price", "%.2f USD" % cost in text, True)
            check("the same step is not noted twice", fire(120000)[1], "")
            check("nor the end of the step", fire(149999)[1], "")
            check("the next 50k step notes again", "150k tokens" in fire(150000)[1], True)
            check("and once only", fire(160000)[1], "")
            check("a further step notes", "200k tokens" in fire(200000)[1], True)
            check("a compaction below the threshold is quiet", fire(30000)[1], "")
            check("and the threshold notes again after it", "110k tokens" in fire(110000)[1], True)

            os.environ["LEAN_CONTEXT_WARN"] = "50000"
            check("the threshold is a setting", "60k tokens" in fire(60000, sid="sess-2")[1], True)
            os.environ["LEAN_CONTEXT_WARN"] = "off"
            check("and can be turned off", fire(300000, sid="sess-3")[1], "")
            os.environ["LEAN_CONTEXT_WARN"] = "junk"
            check("a junk value is the default", fire(50000, sid="sess-4")[1], "")
            del os.environ["LEAN_CONTEXT_WARN"]

            sub = os.path.join(tmp, "s", "subagents")
            os.makedirs(sub)
            sub_path = os.path.join(sub, "agent-a.jsonl")
            with open(sub_path, "w") as fh:
                fh.write(turn(300000))
            for label, kwargs in (("a subagent by its path", {"sid": "sess-5", "path": sub_path}),
                                  ("a subagent by its id", {"sid": "sess-6", "extra": {"agent_id": "a1"}})):
                code, text, err = fire(300000, **kwargs)
                check(label + " never gets the coordinator warning", (code, text, err), (0, "", ""))
            check("and its counter is not touched", os.path.exists(state_path("sess-5")), False)

            lookup = {"name": "Bash", "tool_input": {"command": "git log --oneline"}}
            for i in range(9):
                check("lookup %d is quiet" % (i + 1), fire(1000 + i, sid="sess-7", **lookup)[1], "")
            code, text, err = fire(2000, sid="sess-7", **lookup)
            check("the tenth lookup notes", (code, "10 read-only lookups" in text, "lean:search" in text, "fast model" in text),
                  (0, True, True, True))
            check("once", fire(2001, sid="sess-7", **lookup)[1], "")
            check("counting goes on after the note", load("sess-7")["lookups"], 11)
            fire(2002, sid="sess-8", name="Edit")
            fire(2003, sid="sess-8", name="Bash", tool_input={"command": "git push"})
            check("edits and writes do not count", load("sess-8").get("lookups", 0), 0)
            for i in range(12):
                fire(1000, sid="sess-9", name="Read", model="claude-sonnet-5-5")
            check("a Sonnet coordinator is not counted", load("sess-9").get("lookups", 0), 0)
            os.environ["LEAN_LOOKUP_WARN"] = "2"
            fire(1000, sid="sess-10", name="Grep")
            check("the count is a setting", "2 read-only lookups" in fire(1000, sid="sess-10", name="Glob")[1], True)
            os.environ["LEAN_LOOKUP_WARN"] = "off"
            check("and can be turned off", fire(1000, sid="sess-11", name="Grep")[1], "")
            del os.environ["LEAN_LOOKUP_WARN"]

            os.environ["LEAN_LOOKUP_WARN"] = "1"
            code, text, err = fire(120000, sid="sess-12", name="Read")
            check("both notes can arrive together", ("120k tokens" in text, "lean:search" in text), (True, True))
            del os.environ["LEAN_LOOKUP_WARN"]

            for label, event in (("bad json", "not json"), ("a bad session id", {"session_id": "a/../b", "transcript_path": transcript}),
                                 ("no transcript", {"session_id": "sess-13"}),
                                 ("a missing transcript", {"session_id": "sess-14", "transcript_path": os.path.join(tmp, "no.jsonl")})):
                old = sys.stdin
                sys.stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
                out = io.StringIO()
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                        code = main()
                finally:
                    sys.stdin = old
                check(label + " is quiet", (code, out.getvalue()), (0, ""))

            big = os.path.join(tmp, "big.jsonl")
            with open(big, "w") as fh:
                filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 1000000}}) + "\n"
                for _ in range(50):
                    fh.write(filler)
                fh.write(turn(150000))
            began = time.monotonic()
            code, text, err = fire(170000, sid="sess-15", path=big)
            took = time.monotonic() - began
            check("a 50 MB transcript is read from the tail", (code, "170k" in text or "150k" in text, took < 1.0),
                  (0, True, True))
        finally:
            tempfile.tempdir = saved_tmp
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
    print("self-test: ok" if not failures else "self-test failed:\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

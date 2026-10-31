#!/usr/bin/env python3
"""Tell a coordinator while it happens when its context, its own lookups or its images are costing the most.

One script, two hook events. For the main session only, never a subagent:

PostToolUse (notes):
  - once context passes LEAN_CONTEXT_WARN tokens (default 100000), a note on every turn: the
    size, what the turn costs at list price, the running total spent since it passed the
    threshold, and to /compact or file what is open in brain and continue in a fresh session
  - once per session after LEAN_LOOKUP_WARN read-only lookups (default 10) by an Opus
    coordinator, Read included: send them to lean:search on the fast model
  - with LEAN_IMAGE_MODE=note, a note after the first image tool result of a turn (Read of an
    image file or a pdf with pages, a screenshot or thumbnail tool) once context is above LEAN_IMAGE_CONTEXT tokens (default 60000)
PreToolUse (blocks, exit 2 with the reason on stderr, both off by default):
  - LEAN_LOOKUP_MODE=block: an Opus coordinator that has made more than LEAN_LOOKUP_WARN
    read-only lookups since it last started an Agent has the next Grep, Glob or Bash grep, rg,
    find or cat refused. Read, ls and git or gh state checks count but are never refused
  - LEAN_IMAGE_MODE=block: above LEAN_IMAGE_CONTEXT tokens a tool that returns an image is refused

It reads the tail of the transcript, not the whole file, and keeps a small counter file per
session in a context-cost folder next to the lean-stats database (~/.lean/context-cost, 0700).
The counters stay when a session is resumed.

  LEAN_CONTEXT_WARN=off   turn off the context note (a number sets the threshold)
  LEAN_LOOKUP_WARN=off    turn off the lookup note and block (a number sets the count)
  LEAN_LOOKUP_MODE=block  refuse lookups past the count instead of noting once (default note)
  LEAN_IMAGE_MODE=block   refuse image tools above LEAN_IMAGE_CONTEXT (default note)
  LEAN_IMAGE_CONTEXT=off  turn off the image note and block (a number sets the size)
  context-cost.py --self-test
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # no flock on this platform: the counter can lose an update, nothing else changes
    fcntl = None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
import leanstats  # noqa: E402

CONTEXT_DEFAULT = 100000
LOOKUP_DEFAULT = 10
IMAGE_DEFAULT = 60000
AGENT_TOOLS = {"Agent", "Task"}


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


def mode(name):
    """block when the setting says so, otherwise note."""
    return "block" if (os.environ.get(name) or "").strip().lower() == "block" else "note"


SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,100}")


def state_dir():
    """The counter folder, or None when it is not safe to use: a link, or not ours."""
    path = os.path.join(os.path.dirname(os.path.abspath(leanstats.db_path())), "context-cost")
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None
    if hasattr(os, "getuid") and info.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)
        except OSError:
            return None
    return path


def state_path(sid, suffix=".json"):
    if not SESSION_ID.fullmatch(sid):
        raise ValueError("bad session id")
    folder = state_dir()
    if folder is None:
        raise OSError("no safe state folder")
    return os.path.join(folder, sid + suffix)


def load(sid):
    try:
        fd = os.open(state_path(sid), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


KEEP_DAYS = 14


def prune(folder, sid):
    """Remove the counter and lock files of sessions untouched for KEEP_DAYS, never the current one."""
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot or ext not in ("json", "lock") or stem == sid:
            continue
        try:
            if os.lstat(os.path.join(folder, name)).st_mtime < cutoff:
                os.unlink(os.path.join(folder, name))
        except OSError:
            pass


def save(sid, state):
    path = state_path(sid)
    prune(os.path.dirname(path), sid)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class Locked:
    """Holds the session's lock file for the whole load, change and save, so parallel hooks do not lose updates."""

    def __init__(self, sid):
        self.path = state_path(sid, ".lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def is_subagent(event, path):
    return bool(event.get("agent_id") or event.get("agent_type")) or f"{os.sep}subagents{os.sep}" in path


def context_note(last, state, limit):
    """The context note on each new turn at or above the threshold, else None. Updates state."""
    size = leanstats.prompt_size(last)
    if limit is None or size < limit:
        state["over_usd"], state["over_id"] = 0.0, ""
        return None
    if last["id"] and last["id"] == state.get("over_id"):
        return None
    cost = leanstats.message_cost(last)
    state["over_id"] = last["id"]
    state["over_usd"] = state.get("over_usd", 0.0) + cost
    return (f"Context is {size // 1000}k tokens. This turn cost about {cost:.2f} USD, at least "
            f"{state['over_usd']:.2f} USD since it passed {limit // 1000}k. Run /compact, or file what is "
            "open with `brain add` and start a fresh session.")


def lookup_note(event, last, state, limit):
    """Count a read-only lookup by an Opus coordinator and return the note once the count is reached."""
    if limit is None or "opus" not in last["model"]:
        return None
    if leanstats.tool_kind(event.get("tool_name"), event.get("tool_input")) != "lookup":
        return None
    state["lookups"] = state.get("lookups", 0) + 1
    state["since_agent"] = state.get("since_agent", 0) + 1
    if state["lookups"] < limit or state.get("lookup_noted"):
        return None
    state["lookup_noted"] = True
    return (f"This session has run {state['lookups']} read-only lookups (grep, cat, git log, gh view, "
            "Read, Grep, Glob) on Opus. Send them to a lean:search agent on the fast model "
            "instead. LEAN_LOOKUP_WARN=off turns this note off.")


def image_note(event, last, state, limit):
    """A note after an image tool result once the context is above the limit, once per turn (note mode)."""
    if limit is None or not leanstats.is_image_call(event.get("tool_name"), event.get("tool_input")):
        return None
    size = leanstats.prompt_size(last)
    if size <= limit or (last["id"] and last["id"] == state.get("image_id")):
        return None
    state["image_id"] = last["id"]
    return (f"That image stays in the {size // 1000}k context and is paid for again on every later turn. "
            "Have a subagent look at images and report in text. LEAN_IMAGE_MODE=block refuses image tools "
            "above this size, LEAN_IMAGE_CONTEXT=off turns this note off.")


REFUSE_LOOKUP = ("{n} read-only lookups since the last delegation, on Opus. Refused. Start a lean:search "
                 "agent on the fast model with the questions and read its answer. Read is never refused, nor ls, git or gh. "
                 "Starting any Agent resets the count. LEAN_LOOKUP_MODE=note stops this refusal, LEAN_LOOKUP_WARN=off turns it off.")
REFUSE_IMAGE = ("The context is {k}k tokens and an image would stay in it for every later turn. Refused. Have "
                "a subagent open the image or take the screenshot and report in text. LEAN_IMAGE_MODE=note "
                "stops this refusal, LEAN_IMAGE_CONTEXT=off turns it off.")


def pre(event, sid):
    """PreToolUse: reset the lookup count on an Agent, refuse a search or image tool past its limit. 0 or 2."""
    name, tool_input = event.get("tool_name"), event.get("tool_input")
    lookups, images = mode("LEAN_LOOKUP_MODE") == "block", mode("LEAN_IMAGE_MODE") == "block"
    if name in AGENT_TOOLS:
        with Locked(sid):
            state = load(sid)
            state["since_agent"] = 0
            save(sid, state)
        return 0
    if images and leanstats.is_image_call(name, tool_input):
        limit = setting("LEAN_IMAGE_CONTEXT", IMAGE_DEFAULT)
        last = leanstats.last_usage(event["transcript_path"]) if limit is not None else None
        if last and leanstats.prompt_size(last) > limit:
            return hookkit.refuse("PreToolUse", REFUSE_IMAGE.format(k=leanstats.prompt_size(last) // 1000),
                                  standard="block")
    if lookups and leanstats.is_search(name, tool_input):
        limit = setting("LEAN_LOOKUP_WARN", LOOKUP_DEFAULT)
        if limit is None:
            return 0
        last = leanstats.last_usage(event["transcript_path"])
        if last and "opus" in last["model"]:
            count = load(sid).get("since_agent", 0)
            if count > limit:
                return hookkit.refuse("PreToolUse", REFUSE_LOOKUP.format(n=count), standard="block")
    return 0


def main():
    event = hookkit.read_event()
    sid = str(event.get("session_id") or "")
    path = event.get("transcript_path")
    if not SESSION_ID.fullmatch(sid) or not isinstance(path, str) or is_subagent(event, path):
        return 0
    if event.get("hook_event_name") == "PreToolUse":
        try:
            return pre(event, sid)
        except Exception:
            return 0
    limit, lookups = setting("LEAN_CONTEXT_WARN", CONTEXT_DEFAULT), setting("LEAN_LOOKUP_WARN", LOOKUP_DEFAULT)
    pictures = setting("LEAN_IMAGE_CONTEXT", IMAGE_DEFAULT) if mode("LEAN_IMAGE_MODE") == "note" else None
    if limit is None and lookups is None and pictures is None:
        return 0
    last = leanstats.last_usage(path)
    if not last:
        return 0
    try:
        with Locked(sid):
            state = load(sid)
            notes = [n for n in (context_note(last, state, limit), lookup_note(event, last, state, lookups),
                                 image_note(event, last, state, pictures)) if n]
            save(sid, state)
    except (OSError, ValueError):
        return 0
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
        names = ("LEAN_CONTEXT_WARN", "LEAN_LOOKUP_WARN", "LEAN_LOOKUP_MODE", "LEAN_IMAGE_MODE", "LEAN_IMAGE_CONTEXT",
                 "LEAN_DB", "LEAN_STATS", "HOME")
        saved = {k: os.environ.get(k) for k in names}
        for k in names[:7]:
            os.environ.pop(k, None)
        os.environ["HOME"] = os.path.join(tmp, "home")
        os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")
        try:
            transcript = os.path.join(tmp, "s.jsonl")

            def turn(size, model="claude-opus-5-5"):
                usage = {"input_tokens": 1, "cache_read_input_tokens": size - 1, "output_tokens": 10}
                return json.dumps({"type": "assistant", "message": {"id": "m%d" % size, "model": model, "usage": usage}}) + "\n"

            def fire(size, name="Edit", tool_input=None, sid="sess-1", extra=None, path=None, model="claude-opus-5-5",
                     phase="PostToolUse", append=True):
                if append:
                    with open(path or transcript, "a") as fh:
                        fh.write(turn(size, model))
                event = {"session_id": sid, "transcript_path": path or transcript, "tool_name": name,
                         "tool_input": tool_input or {}, "hook_event_name": phase}
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
            check("at the threshold it notes", (code, err, "100k tokens" in text, "brain" in text, "USD, at least" in text),
                  (0, "", True, True, True))
            cost = leanstats.message_cost(leanstats.usage_of({}, {"model": "claude-opus-5-5", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 99999, "output_tokens": 10}}))
            check("it names the turn's list price", "%.2f USD" % cost in text, True)
            def cost_of(size):
                return leanstats.message_cost(leanstats.usage_of({}, {"model": "claude-opus-5-5", "usage": {
                    "input_tokens": 1, "cache_read_input_tokens": size - 1, "output_tokens": 10}}))

            text = fire(120000)[1]
            check("a later turn notes again, with the running total", ("120k tokens" in text, "%.2f USD since it passed 100k" % (
                cost_of(100000) + cost_of(120000)) in text), (True, True))
            check("the note names compact and brain", ("/compact" in text, "brain" in text), (True, True))
            check("the note is short and has no path or later-turn clause", (len(text) < 200, "/" in text.replace("/compact", ""),
                                                                           "later turn" in text), (True, False, False))
            text = fire(130000)[1]
            check("every turn notes", "130k tokens" in text, True)
            check("the total adds each turn", "%.2f USD since" % (cost_of(100000) + cost_of(120000) + cost_of(130000)) in text, True)
            check("a second call in the same turn is quiet", fire(130000, append=False)[1], "")
            check("the next turn notes", "150k tokens" in fire(150000)[1], True)
            check("a compaction below the threshold is quiet", fire(30000)[1], "")
            text = fire(110000)[1]
            check("and the threshold notes again after it, with the total started over",
                  ("110k tokens" in text, "%.2f USD since" % cost_of(110000) in text), (True, True))

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

            grep = {"name": "Grep"}
            glog = {"name": "Bash", "tool_input": {"command": "git log --oneline"}}
            gpush = {"name": "Bash", "tool_input": {"command": "git push"}}
            for i in range(12):
                fire(1000 + i, sid="lk-1", **grep)
            check("lookup mode note never refuses", fire(1000, sid="lk-1", phase="PreToolUse", **grep), (0, "", ""))

            os.environ["LEAN_LOOKUP_MODE"] = "block"
            os.environ["LEAN_LOOKUP_WARN"] = "3"
            for i in range(3):
                fire(1000 + i, sid="lk-2", **grep)
            check("block mode allows the lookup when the count is not past the limit",
                  fire(1000, sid="lk-2", phase="PreToolUse", **grep), (0, "", ""))
            fire(1000, sid="lk-2", **glog)
            code, text, err = fire(1000, sid="lk-2", phase="PreToolUse", **grep)
            check("block mode refuses the next lookup past the limit", (code, text, "lean:search" in err, "fast model" in err),
                  (2, "", True, True))
            check("a git log is never refused", fire(1000, sid="lk-2", phase="PreToolUse", **glog)[0], 0)
            for cmd in ("git status", "git diff", "gh pr view 110", "ls -la"):
                check(f"{cmd} is never refused", fire(1000, sid="lk-2", phase="PreToolUse", name="Bash",
                                                       tool_input={"command": cmd})[0], 0)
            for cmd in ("rg foo", "cat a.py", "find . -name x", "git log | grep fix"):
                check(f"{cmd} is refused", fire(1000, sid="lk-2", phase="PreToolUse", name="Bash",
                                                 tool_input={"command": cmd})[0], 2)
            check("a write is never refused", fire(1000, sid="lk-2", phase="PreToolUse", name="Edit")[0], 0)
            check("Read is never refused", fire(1000, sid="lk-2", phase="PreToolUse", name="Read",
                                                 tool_input={"file_path": "/a/b.py"})[0], 0)
            check("Glob is refused", fire(1000, sid="lk-2", phase="PreToolUse", name="Glob")[0], 2)
            before = load("lk-2")["lookups"]
            fire(1000, sid="lk-2", name="Read", tool_input={"file_path": "/a/b.py"})
            check("but Read still counts", load("lk-2")["lookups"], before + 1)
            check("a git push is never refused", fire(1000, sid="lk-2", phase="PreToolUse", **gpush)[0], 0)
            check("a refusal does not count", load("lk-2")["since_agent"], 5)
            check("a refusal text says Read is not refused", "Read is never refused" in fire(1000, sid="lk-2", phase="PreToolUse", **grep)[2], True)
            check("starting an Agent is allowed", fire(1000, sid="lk-2", phase="PreToolUse", name="Agent")[0], 0)
            check("and resets the count", load("lk-2")["since_agent"], 0)
            fire(1000, sid="lk-2", **grep)
            fire(1000, sid="lk-2", **grep)
            fire(1000, sid="lk-2", **grep)
            fire(1000, sid="lk-2", **grep)
            check("Task resets the count like Agent", (fire(1000, sid="lk-2", phase="PreToolUse", name="Task")[0],
                                                       load("lk-2")["since_agent"]), (0, 0))
            check("so a lookup is allowed again", fire(1000, sid="lk-2", phase="PreToolUse", **grep)[0], 0)
            check("the total count is kept", load("lk-2")["lookups"], 9)

            fire(1000, sid="lk-4", name="Grep")
            fire(1000, sid="lk-4", name="Grep")
            check("note mode: an Agent resets the count too", (load("lk-4")["since_agent"],
                  fire(1000, sid="lk-4", phase="PreToolUse", name="Agent")[0], load("lk-4")["since_agent"]), (2, 0, 0))

            with Locked("lk-3"):
                save("lk-3", {"since_agent": 99})
            check("a Sonnet main session is never refused", fire(1000, sid="lk-3", model="claude-sonnet-5-5", phase="PreToolUse",
                                                                  **grep), (0, "", ""))
            check("the same count on Opus is refused", fire(1000, sid="lk-3", phase="PreToolUse", **grep)[0], 2)
            check("a subagent by its path is never refused", fire(1000, sid="lk-3", phase="PreToolUse", path=sub_path, **grep),
                  (0, "", ""))
            check("a subagent by its id is never refused", fire(1000, sid="lk-3", phase="PreToolUse",
                                                                  extra={"agent_id": "a1"}, **grep), (0, "", ""))
            check("a subagent by its type is never refused", fire(1000, sid="lk-3", phase="PreToolUse",
                                                                    extra={"agent_type": "Explore"}, **grep), (0, "", ""))
            os.environ["LEAN_LOOKUP_WARN"] = "off"
            check("turning the count off turns the block off", fire(1000, sid="lk-3", phase="PreToolUse", **grep)[0], 0)
            os.environ["LEAN_LOOKUP_WARN"] = "3"
            os.environ["LEAN_LOOKUP_MODE"] = "junk"
            check("a junk mode is note", fire(1000, sid="lk-3", phase="PreToolUse", **grep)[0], 0)
            del os.environ["LEAN_LOOKUP_MODE"]
            del os.environ["LEAN_LOOKUP_WARN"]

            png = {"name": "Read", "tool_input": {"file_path": "/a/shot.png"}}
            shots = ({"name": "mcp__plugin_playwright_playwright__browser_take_screenshot"},
                     {"name": "mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_screenshot"})
            text = fire(70000, sid="im-1", **png)[1]
            check("note mode notes an image above 60k", ("image" in text, "subagent" in text, "70k" in text), (True, True, True))
            check("once per image", "That image" in fire(71000, sid="im-1", **png)[1], True)
            check("not at 60k", "That image" in fire(60000, sid="im-1", **png)[1], False)
            check("not for a text file", "That image" in fire(70000, sid="im-1", name="Read", tool_input={"file_path": "a.py"})[1],
                  False)
            more = ({"name": "mcp__plugin_figma_figma__get_screenshot"},
                    {"name": "mcp__claude_ai_Google_Slides__read_slide_page_thumbnail"},
                    {"name": "Read", "tool_input": {"file_path": "/a/b.pdf", "pages": "1-2"}})
            check("figma, slide thumbnail and pdf pages notes", ["That image" in fire(70100 + i, sid="im-1", **shot)[1]
                                                                                for i, shot in enumerate(more)],
                  [True] * 3)
            check("a pdf without pages is not an image", "That image" in fire(70200, sid="im-1", name="Read",
                  tool_input={"file_path": "/a/b.pdf"})[1], False)
            check("a screenshot tool notes", ["That image" in fire(70300 + i, sid="im-1", **shot)[1]
                                                                  for i, shot in enumerate(shots)], [True, True])
            first = fire(75000, sid="im-3", **png)[1]
            second = fire(75000, sid="im-3", append=False, **png)[1]
            third = fire(75000, sid="im-3", append=False, **shots[0])[1]
            check("three image calls in one turn give one note", ("That image" in first, second, third), (True, "", ""))
            check("the next turn notes again", "That image" in fire(76000, sid="im-3", **png)[1], True)
            check("note mode never refuses", fire(70000, sid="im-1", phase="PreToolUse", **png), (0, "", ""))
            os.environ["LEAN_IMAGE_CONTEXT"] = "1000"
            check("the size is a setting", "That image" in fire(2000, sid="im-1", **png)[1], True)
            os.environ["LEAN_IMAGE_CONTEXT"] = "off"
            check("and can be turned off", "That image" in fire(200000, sid="im-1", **png)[1], False)
            del os.environ["LEAN_IMAGE_CONTEXT"]

            os.environ["LEAN_IMAGE_MODE"] = "block"
            check("block mode has no note", "That image" in fire(70000, sid="im-2", **png)[1], False)
            code, text, err = fire(70000, sid="im-2", phase="PreToolUse", **png)
            check("block mode refuses an image read above 60k", (code, text, "subagent" in err, "text" in err), (2, "", True, True))
            check("not at exactly 60k", fire(60000, sid="im-2", phase="PreToolUse", **png)[0], 0)
            check("but at 60001", fire(60001, sid="im-2", phase="PreToolUse", **png)[0], 2)
            check("a text read is allowed", fire(70000, sid="im-2", phase="PreToolUse", name="Read",
                                                  tool_input={"file_path": "a.md"})[0], 0)
            check("both screenshot tools are refused", [fire(70000, sid="im-2", phase="PreToolUse", **shot)[0] for shot in shots], [2, 2])
            check("a Sonnet coordinator is refused too", fire(70000, sid="im-2", model="claude-sonnet-5-5", phase="PreToolUse",
                                                               **png)[0], 2)
            check("a subagent by its path is never refused", fire(70000, sid="im-2", phase="PreToolUse", path=sub_path, **png),
                  (0, "", ""))
            check("a subagent by its id is never refused", fire(70000, sid="im-2", phase="PreToolUse",
                                                                  extra={"agent_id": "a1"}, **png), (0, "", ""))
            check("an Edit is never refused", fire(70000, sid="im-2", phase="PreToolUse", name="Edit")[0], 0)
            os.environ["LEAN_IMAGE_CONTEXT"] = "off"
            check("turning the size off turns the block off", fire(300000, sid="im-2", phase="PreToolUse", **png)[0], 0)
            os.environ["LEAN_IMAGE_CONTEXT"] = "1000"
            check("the size is a setting for the block", fire(2000, sid="im-2", phase="PreToolUse", **png)[0], 2)
            del os.environ["LEAN_IMAGE_CONTEXT"]
            del os.environ["LEAN_IMAGE_MODE"]

            for value, want in (("0", None), ("-5", None), ("1", 1), (" 7 ", 7), ("off", None), ("", 9), ("3.5", 9)):
                os.environ["LEAN_CONTEXT_WARN"] = value
                check("setting %r" % value, setting("LEAN_CONTEXT_WARN", 9), want)
            del os.environ["LEAN_CONTEXT_WARN"]
            os.environ["LEAN_CONTEXT_WARN"] = "0"
            check("a setting of 0 turns the context note off", fire(300000, sid="sess-16")[1], "")
            del os.environ["LEAN_CONTEXT_WARN"]

            folder = os.path.join(tmp, "context-cost")
            check("the counters sit next to the database, private", (os.path.dirname(state_path("sess-7")) == folder,
                                                                    stat.S_IMODE(os.lstat(folder).st_mode)), (True, 0o700))
            check("no temp file is left behind", [n for n in os.listdir(folder) if n.endswith(".tmp")], [])
            check("a counter file is not shared with the temp directory", os.path.exists(
                os.path.join(tempfile.gettempdir(), "lean-context-cost")), False)
            for bad in ("a/../b", "../x", "a.b", "", "x" * 101, "a b"):
                try:
                    state_path(bad)
                    check("session id %r is refused" % bad, "accepted", "refused")
                except ValueError:
                    pass

            here = os.path.join(tmp, "parallel")
            os.makedirs(here)
            log = os.path.join(here, "s.jsonl")
            with open(log, "w") as fh:
                fh.write(turn(1000))
            env = dict(os.environ, LEAN_DB=os.path.join(here, "lean.db"), HOME=here)
            event = json.dumps({"session_id": "par-1", "transcript_path": log, "tool_name": "Grep", "tool_input": {},
                                "hook_event_name": "PostToolUse"})
            procs = [subprocess.Popen([sys.executable, os.path.realpath(__file__)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(20)]
            outs = [p.communicate(event) for p in procs]
            check("parallel hooks all succeed", [p.returncode for p in procs], [0] * 20)
            noted = [o for o, _ in outs if "read-only lookups" in o]
            check("parallel hooks lose no count", json.load(open(os.path.join(here, "context-cost", "par-1.json")))["lookups"], 20)
            check("and exactly one of them gives the lookup note", len(noted), 1)

            os.environ["LEAN_DB"] = env["LEAN_DB"]
            try:
                with Locked("par-2"):
                    held = subprocess.Popen([sys.executable, os.path.realpath(__file__)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    held.stdin.write(event.replace("par-1", "par-2"))
                    held.stdin.close()
                    try:
                        held.wait(timeout=0.7)
                        check("a hook waits for the session's lock", "finished", "waiting")
                    except subprocess.TimeoutExpired:
                        pass
                check("and goes on when it is released", held.wait(timeout=10), 0)
                held.stdout.close()
                held.stderr.close()
                check("having counted once", json.load(open(os.path.join(here, "context-cost", "par-2.json")))["lookups"], 1)
            finally:
                os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")

            linked = os.path.join(tmp, "linked")
            elsewhere = os.path.join(tmp, "elsewhere")
            os.makedirs(linked)
            os.makedirs(elsewhere)
            os.symlink(elsewhere, os.path.join(linked, "context-cost"))
            os.environ["LEAN_DB"] = os.path.join(linked, "lean.db")
            check("a state folder that is a link is refused", (fire(300000, sid="sess-17")[:1], os.listdir(elsewhere)),
                  ((0,), []))
            os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")
            real_uid = os.getuid
            os.getuid = lambda: real_uid() + 1
            try:
                check("a state folder that is not ours is refused", state_dir(), None)
            finally:
                os.getuid = real_uid
            old = time.time() - 15 * 86400
            for name in ("gone-1.json", "gone-1.lock", "fresh-1.json", "sess-18.lock"):
                with open(os.path.join(folder, name), "w") as fh:
                    fh.write("{}")
            for name in ("gone-1.json", "gone-1.lock", "sess-18.lock"):
                os.utime(os.path.join(folder, name), (old, old))
            fire(1000, sid="sess-18")
            check("counters untouched for 14 days are removed", sorted(n for n in os.listdir(folder) if n.startswith(("gone", "fresh"))),
                  ["fresh-1.json"])
            check("the current session's old lock is kept", os.path.exists(os.path.join(folder, "sess-18.lock")), True)

            os.mkdir(os.path.join(tmp, "rel"))
            cwd_before = os.getcwd()
            os.chdir(os.path.join(tmp, "rel"))
            os.environ["LEAN_DB"] = "rel.db"
            try:
                check("a relative LEAN_DB still keeps state", "300k tokens" in fire(300000, sid="sess-19")[1], True)
                check("beside the database", os.path.exists(os.path.join(tmp, "rel", "context-cost", "sess-19.json")), True)
            finally:
                os.chdir(cwd_before)
                os.environ["LEAN_DB"] = os.path.join(tmp, "lean.db")

            link = os.path.join(folder, "sess-20.json")
            os.symlink(os.path.join(elsewhere, "x.json"), link)
            with open(os.path.join(elsewhere, "x.json"), "w") as fh:
                fh.write('{"lookups": 99}')
            check("a state file that is a link is not read", load("sess-20"), {})
            os.unlink(link)

            real_getuid = os.getuid
            del os.getuid
            try:
                check("without getuid the owner check is skipped", state_dir(), folder)
                check("and the hook still notes", "300k tokens" in fire(300000, sid="sess-21")[1], True)
            finally:
                os.getuid = real_getuid
            os.chmod(folder, 0o755)
            check("a loose folder is made private again", (state_dir() == folder, stat.S_IMODE(os.lstat(folder).st_mode)),
                  (True, 0o700))

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
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("self-test: ok" if not failures else "self-test failed:\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

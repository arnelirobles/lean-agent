"""Prompts for a brain item and opening a Claude session on it.

Ports clean, askPrompt, workPrompt and canSend from vscode/src/claude.ts. The prompt is
built from agent-written text, so it is only ever one argv element, or shlex-quoted in
the copyable command.

  python3 lib/brain/web/session.py --self-test
"""
import os
import re
import shlex
import subprocess
import sys

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def clean(s) -> str:
    """Control characters and line breaks become spaces."""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", "" if s is None else str(s))).strip()


def _id(item: dict) -> int:
    i = item.get("id")
    return i if isinstance(i, int) and not isinstance(i, bool) else 0


def _head(verb: str, item: dict) -> str:
    i = _id(item)
    return (f"{verb} lean brain item #{i} ({clean(item.get('kind'))}, project {clean(item.get('project'))}, "
            f"ref {clean(item.get('ref'))}): {clean(item.get('title') or item.get('ref'))}.")


def ask_prompt(item: dict) -> str:
    i = _id(item)
    return _head("Look at", item) + f" Read it with brain show {i}, then tell me what you find before changing anything."


def can_send(kind: str) -> bool:
    """Kinds Claude can take on by itself: pending work, risks and questions."""
    return re.search(r"pending|risk|question", kind or "", re.I) is not None


def work_prompt(item: dict) -> str:
    i = _id(item)
    head = _head("Work on", item) + f" Read it first with brain show {i}. "
    if re.search(r"question", str(item.get("kind") or ""), re.I):
        return head + ("Work out the answer from the code and the docs, then recommend one with your reasons. "
                       "Do not close the item, I decide.")
    return head + (f"Fix it, verify the fix, then close it with brain close {i} --resolution fixed "
                   "--note followed by one line on what you did.")


def session_folder(item: dict) -> tuple:
    for key in ("session_cwd", "repo_dir", "repo"):
        p = item.get(key)
        if isinstance(p, str) and p and os.path.isdir(p):
            return p, ""
    gone = item.get("session_cwd") or item.get("repo_dir") or item.get("repo")
    home = os.path.expanduser("~")
    return home, (f"folder {gone} is gone, opened in home" if gone else "")


_MAC_SCRIPT = ("on run argv\n"
               "set f to item 1 of argv\n"
               "set p to item 2 of argv\n"
               'tell application "Terminal"\n'
               "activate\n"
               'do script "cd " & quoted form of f & " && claude " & quoted form of p\n'
               "end tell\n"
               "end run")


def launch_argv(platform: str, folder: str, prompt: str):
    if platform == "darwin":
        return ["osascript", "-e", _MAC_SCRIPT, folder, prompt]
    if platform.startswith("win"):
        return ["wt.exe", "-d", folder, "claude", prompt]
    return None


def copy_command(folder: str, prompt: str) -> str:
    return f"cd {shlex.quote(folder)} && claude {shlex.quote(prompt)}"


def start_session(item: dict, run=subprocess.run) -> dict:
    folder, note = session_folder(item)
    prompt = work_prompt(item) if can_send(str(item.get("kind") or "")) else ask_prompt(item)
    command = copy_command(folder, prompt)
    argv = launch_argv(sys.platform, folder, prompt)
    if argv is None:
        return {"opened": False, "command": command, "note": note or "no terminal to open here, copy the command"}
    try:
        run(argv, check=True, timeout=30, capture_output=True)
    except Exception as e:
        return {"opened": False, "command": command, "note": (note + "; " if note else "") + f"could not open a terminal ({type(e).__name__}), copy the command"}
    return {"opened": True, "command": command, "note": note}


def _self_test() -> int:
    import tempfile
    os.environ["LEAN_BRAIN_DB"] = os.path.join(tempfile.mkdtemp(), "brain.db")
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"FAIL {name}: got {got!r}, want {want!r}")

    item = {"id": 12, "kind": "test-fail", "project": "demo", "ref": "T.Suite.It", "title": "it's broken"}
    p = ask_prompt(item)
    check("ask exact", p, "Look at lean brain item #12 (test-fail, project demo, ref T.Suite.It): it's broken. "
          "Read it with brain show 12, then tell me what you find before changing anything.")
    nasty = ask_prompt({"id": 1, "kind": "a\nrm -rf ~\n", "project": "p\r\x1b[2J", "ref": "r\u2028x\x00", "title": "t\x07"})
    check("ask no control", re.search("[\x00-\x1f\x7f-\x9f\u2028\u2029]", nasty), None)
    check("clean joins", clean("a\n\nb"), "a b")
    check("clean none", clean(None), "")
    for k, w in [("pending", True), ("tests-pending", True), ("risk", True), ("open-question", True),
                 ("test-fail", False), ("ruling", False)]:
        check("can_send " + k, can_send(k), w)
    w = work_prompt({**item, "kind": "risk"})
    check("work risk exact", w, "Work on lean brain item #12 (risk, project demo, ref T.Suite.It): it's broken. "
          "Read it first with brain show 12. Fix it, verify the fix, then close it with brain close 12 "
          "--resolution fixed --note followed by one line on what you did.")
    q = work_prompt({**item, "kind": "question"})
    check("work question exact", q, "Work on lean brain item #12 (question, project demo, ref T.Suite.It): it's broken. "
          "Read it first with brain show 12. Work out the answer from the code and the docs, then recommend one "
          "with your reasons. Do not close the item, I decide.")
    check("question no close", "brain close" in q, False)
    check("bad id is 0", "#0 " in ask_prompt({"id": "7", "kind": "k"}), True)

    evil = {"id": 3, "kind": "risk", "project": "p", "ref": "r", "title": "x\n\x1b[2J $(rm -rf ~) `id`; y"}
    ep = work_prompt(evil)
    check("evil one line", "\n" in ep or "\x1b" in ep, False)
    check("evil intact", "$(rm -rf ~)" in ep, True)
    argv = launch_argv("darwin", "/tmp/a b", ep)
    check("mac prompt own element", argv[4], ep)
    check("mac folder own element", argv[3], "/tmp/a b")
    check("mac script has no prompt", ep in argv[2] or "rm -rf" in argv[2], False)
    check("mac script uses argv", "quoted form of p" in argv[2] and "item 2 of argv" in argv[2], True)
    check("mac head", argv[:2], ["osascript", "-e"])
    check("win", launch_argv("win32", "C:\\x", ep), ["wt.exe", "-d", "C:\\x", "claude", ep])
    check("linux none", launch_argv("linux", "/x", ep), None)
    cc = copy_command("/tmp/a b", ep)
    check("copy round trips", shlex.split(cc), ["cd", "/tmp/a b", "&&", "claude", ep])

    d = tempfile.mkdtemp()
    check("cwd used", session_folder({"session_cwd": d, "repo": "/nope"}), (d, ""))
    check("repo_dir next", session_folder({"session_cwd": "/nope/x", "repo_dir": d}), (d, ""))
    check("repo last", session_folder({"repo": d}), (d, ""))
    gone = os.path.join(d, "deleted")
    check("gone", session_folder({"session_cwd": gone}), (os.path.expanduser("~"), f"folder {gone} is gone, opened in home"))

    def boom(*a, **k):
        raise OSError("no terminal")
    r = start_session({**item, "kind": "risk", "session_cwd": d}, run=boom)
    check("raise not opened", r["opened"], False)
    check("raise command", r["command"], copy_command(d, work_prompt({**item, "kind": "risk"})))
    calls = []
    r = start_session({**item, "kind": "test-fail", "session_cwd": d}, run=lambda *a, **k: calls.append(a))
    if sys.platform in ("darwin", "win32"):
        check("ok opened", r["opened"], True)
        check("ask used for non-sendable", calls[0][0][-1], ask_prompt(item))
    else:
        check("unsupported falls back", r["opened"], False)

    print("self-test: ok" if not fails else f"self-test: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)

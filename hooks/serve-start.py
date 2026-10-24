#!/usr/bin/env python3
"""Start the brain page server when a session starts, if the serve mode asks for it.

SessionStart. The mode is the first argument, else $CLAUDE_PLUGIN_OPTION_SERVE_MODE (the plugin's serve_mode
setting), else manual. session: start `brain serve --idle-exit 30` detached when nothing answers on the port.
always: start it the same way (without the idle exit) unless the login unit is installed, and note once that
`brain serve --install always` starts it at login. manual: nothing. Never blocks the session and always exits 0.

  serve-start.py [MODE]
  serve-start.py --self-test
"""
import os
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import hookkit  # noqa: E402

BRAIN = os.path.join(HERE, "..", "bin", "brain")
MODES = ("always", "session", "manual")


def port():
    try:
        return int(os.environ.get("LEAN_SERVE_PORT") or 8770)
    except ValueError:
        return 8770


def answering(p):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/healthz", timeout=0.5) as r:
            return r.read(64).startswith(b"lean-brain")
    except (OSError, ValueError):
        return False


def spawn(args):
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([sys.executable, BRAIN, "serve", *args], **kw)


def noted_marker():
    return os.path.join(os.path.dirname(os.path.abspath(os.environ.get("LEAN_BRAIN_DB")
                                                        or os.path.expanduser("~/.local/share/lean/brain.db"))),
                        "serve-install-noted")


def start(mode, spawner=spawn, probe=answering, installed=None):
    """Returns what it did: nothing, started, or started and noted."""
    if mode not in ("session", "always") or probe(port()):
        return "nothing"
    if mode == "always":
        if installed is None:
            from brain.web import install
            installed = install.installed()
        if installed:
            return "nothing"
    roots = os.environ.get("CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS", "")
    spawner((["--idle-exit", "30"] if mode == "session" else []) + (["--roots", roots] if roots else []))
    if mode == "always" and not os.path.exists(noted_marker()):
        try:
            os.makedirs(os.path.dirname(noted_marker()), exist_ok=True)
            open(noted_marker(), "w").close()
        except OSError:
            pass
        return "noted"
    return "started"


def main():
    hookkit.read_event()
    mode = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("CLAUDE_PLUGIN_OPTION_SERVE_MODE") or "manual"
    mode = mode.strip().lower()
    if mode.startswith("${"):  # a placeholder nothing substituted
        mode = os.environ.get("CLAUDE_PLUGIN_OPTION_SERVE_MODE") or "manual"
    try:
        done = start(mode)
    except Exception:
        return 0
    if done == "noted":
        hookkit.note("SessionStart", "The brain page was started for this session. "
                     "`brain serve --install always` starts it at every login instead.")
    return 0


def self_test():
    fails = []
    calls = []

    def spawner(args):
        calls.append(args)

    with tempfile.TemporaryDirectory() as d:
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "brain.db")
        os.environ.pop("CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS", None)
        down, up = (lambda p: False), (lambda p: True)
        for mode, probe, inst, want in (("manual", down, False, "nothing"), ("bogus", down, False, "nothing"),
                                        ("session", up, False, "nothing"), ("session", down, False, "started"),
                                        ("always", down, True, "nothing"), ("always", up, False, "nothing")):
            calls.clear()
            got = start(mode, spawner, probe, inst)
            if got != want or bool(calls) != (want != "nothing"):
                fails.append(f"{mode} probe={probe(0)} installed={inst}: {got} {calls}")
        calls.clear()
        start("session", spawner, down, False)
        if calls != [["--idle-exit", "30"]]:
            fails.append(f"session args: {calls}")
        calls.clear()
        os.environ["CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS"] = "/a,/b"
        if start("always", spawner, down, False) != "noted" or calls != [["--roots", "/a,/b"]]:
            fails.append(f"always first start must note and pass roots: {calls}")
        if start("always", spawner, down, False) != "started":
            fails.append("the install note is shown once")
        # the script itself: manual starts nothing, a dead port and a bad mode exit 0 and print nothing
        env = dict(os.environ, LEAN_SERVE_PORT="1")
        for args in (["manual"], ["bogus"], []):
            r = subprocess.run([sys.executable, os.path.realpath(__file__), *args], input="{}", env=env,
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0 or r.stdout:
                fails.append(f"{args}: {r.returncode} {r.stdout!r}")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

#!/usr/bin/env python3
"""Start the brain page server when a session starts, if the serve mode asks for it.

SessionStart. The mode is the first argument, else $CLAUDE_PLUGIN_OPTION_SERVE_MODE (the plugin's serve_mode
setting), else manual; any other value is manual and noted once. session: start `brain serve --idle-exit 30` detached when nothing answers on the port.
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


def noted(name):
    """True the first time it is asked for this name; the marker sits next to brain.db."""
    db = os.environ.get("LEAN_BRAIN_DB") or os.path.expanduser("~/.local/share/lean/brain.db")
    marker = os.path.join(os.path.dirname(os.path.abspath(db)), "serve-" + name)
    if os.path.exists(marker):
        return False
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()
    except OSError:
        pass
    return True


def start(mode, spawner=spawn, probe=answering, installed=None, stale=None, reinstall=None):
    """Starts the server when the mode asks for it. Returns what it did and the notes to show."""
    said = []
    if mode not in MODES:
        if noted("bad-mode"):
            said.append(f"serve_mode is {mode!r}; use always, session or manual. Treated as manual.")
        return "nothing", said
    roots = os.environ.get("CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS", "")
    if mode == "always":
        from brain.web import install
        if installed is None:
            installed = install.installed()
        brain = os.path.abspath(BRAIN)
        if installed and (install.stale(brain) if stale is None else stale):
            plat = install.platform_name()
            (reinstall or (lambda: install.install(plat, sys.executable, brain, port(), roots)))()
            said.append("The brain page login unit pointed at an old plugin folder; it was rewritten for this one.")
            return "reinstalled", said
        if installed:
            return "nothing", said
    if mode == "manual" or probe(port()):
        return "nothing", said
    spawner((["--idle-exit", "30"] if mode == "session" else []) + (["--roots", roots] if roots else []))
    if mode == "always" and noted("install-noted"):
        said.append("The brain page was started for this session. "
                    "`brain serve --install always` starts it at every login instead.")
    return "started", said


def main():
    hookkit.read_event()
    mode = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("CLAUDE_PLUGIN_OPTION_SERVE_MODE") or "manual"
    mode = mode.strip().lower()
    if mode.startswith("${"):  # a placeholder nothing substituted
        mode = os.environ.get("CLAUDE_PLUGIN_OPTION_SERVE_MODE") or "manual"
        mode = mode.strip().lower()
    try:
        done, said = start(mode)
    except Exception:
        return 0
    if said:
        hookkit.note("SessionStart", "\n".join(said))
    return 0


def self_test():
    fails = []
    calls = []
    re_ = []

    def spawner(args):
        calls.append(args)

    with tempfile.TemporaryDirectory() as d:
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "brain.db")
        for name in ("CLAUDE_PLUGIN_OPTION_SERVE_MODE", "CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS", "LEAN_SERVE_PORT"):
            os.environ.pop(name, None)
        home = os.path.join(d, "home")
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        os.environ["APPDATA"] = os.path.join(home, "AppData")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(home, "xdg")
        down, up = (lambda p: False), (lambda p: True)
        for mode, probe, inst, want in (("manual", down, False, "nothing"), ("session", up, False, "nothing"),
                                        ("session", down, False, "started"), ("always", down, True, "nothing"),
                                        ("always", up, False, "nothing")):
            calls.clear()
            got = start(mode, spawner, probe, inst, False)[0]
            if got != want or bool(calls) != (want == "started"):
                fails.append(f"{mode} probe={probe(0)} installed={inst}: {got} {calls}")
        calls.clear()
        got, said = start("bogus", spawner, down, False)
        if got != "nothing" or calls or len(said) != 1 or start("bogus", spawner, down, False)[1]:
            fails.append(f"a bad mode is manual and noted once: {got} {said}")
        start("session", spawner, down, False)
        if calls != [["--idle-exit", "30"]]:
            fails.append(f"session args: {calls}")
        calls.clear()
        got, said = start("always", spawner, down, True, True, lambda: re_.append(1))
        if got != "reinstalled" or re_ != [1] or calls or len(said) != 1:
            fails.append(f"a stale login unit must be rewritten and noted: {got} {said} {re_}")
        os.environ["CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS"] = "/a,/b"
        got, said = start("always", spawner, down, False, False)
        if got != "started" or calls != [["--roots", "/a,/b"]] or len(said) != 1:
            fails.append(f"always first start must note and pass roots: {calls} {said}")
        if start("always", spawner, down, False, False)[1]:
            fails.append("the install note is shown once")
        # the script itself: a dead port and a bad mode exit 0 and start nothing
        env = dict(os.environ, LEAN_SERVE_PORT="1")
        env.pop("CLAUDE_PLUGIN_OPTION_SERVE_MODE", None)
        for args in (["manual"], ["bogus"], []):
            r = subprocess.run([sys.executable, os.path.realpath(__file__), *args], input="{}", env=env,
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0 or (args != ["bogus"] and r.stdout):
                fails.append(f"{args}: {r.returncode} {r.stdout!r}")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

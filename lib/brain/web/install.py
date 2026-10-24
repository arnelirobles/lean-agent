"""Launch the brain page server at login: a launchd agent, a systemd user unit or a Windows Startup entry.

No admin rights. The runner that calls launchctl or systemctl is passed in, so the self-test never calls them.

  python3 lib/brain/web/install.py --self-test
"""
import os
import subprocess
import sys

LABEL = "dev.baryo.lean-brain"


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _args(python, brain_path, port, roots):
    args = [python, brain_path, "serve", "--port", str(port)]
    if roots:
        args += ["--roots", roots]
    return args


def unit_for(platform, python, brain_path, port, roots=""):
    """The path of the login unit for this platform and its text."""
    args = _args(python, brain_path, port, roots)
    if platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")
        items = "".join(f"\n    <string>{_xml(a)}</string>" for a in args)
        text = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                f"  <key>Label</key>\n  <string>{LABEL}</string>\n"
                f"  <key>ProgramArguments</key>\n  <array>{items}\n  </array>\n"
                "  <key>RunAtLoad</key>\n  <true/>\n"
                "  <key>KeepAlive</key>\n  <true/>\n"
                "</dict>\n</plist>\n")
        return path, text
    if platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        path = os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "lean-brain.cmd")
        line = " ".join('"%s"' % a.replace('"', "") for a in args)
        return path, f'@echo off\r\nstart "" /min {line}\r\n'
    path = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "lean-brain.service")
    exec_start = " ".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') for a in args)
    text = ("[Unit]\nDescription=lean-agent brain page\n\n"
            f"[Service]\nExecStart={exec_start}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n")
    return path, text


def _run(runner, cmd):
    try:
        return runner(cmd, capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _start(platform, path, runner):
    if platform == "darwin":
        return _run(runner, ["launchctl", "bootstrap", f"gui/{os.getuid()}", path]) \
            or _run(runner, ["launchctl", "load", "-w", path])
    if platform == "win32":
        return True
    return _run(runner, ["systemctl", "--user", "daemon-reload"]) \
        and _run(runner, ["systemctl", "--user", "enable", "--now", "lean-brain.service"])


def _stop(platform, path, runner):
    if platform == "darwin":
        _run(runner, ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]) \
            or _run(runner, ["launchctl", "unload", "-w", path])
    elif platform != "win32":
        _run(runner, ["systemctl", "--user", "disable", "--now", "lean-brain.service"])


def install(platform, python, brain_path, port, roots="", runner=subprocess.run):
    """Write the unit and start it. Returns (path, started)."""
    path, text = unit_for(platform, python, brain_path, port, roots)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        _stop(platform, path, runner)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path, _start(platform, path, runner)


def uninstall(platform, runner=subprocess.run):
    """Stop and remove the unit. False when there was none."""
    path = unit_for(platform, sys.executable, "brain", 0)[0]
    if not os.path.exists(path):
        return False
    _stop(platform, path, runner)
    os.remove(path)
    if platform not in ("darwin", "win32"):
        _run(runner, ["systemctl", "--user", "daemon-reload"])
    return True


def platform_name():
    return "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux"


def installed(platform=None):
    return os.path.exists(unit_for(platform or platform_name(), sys.executable, "brain", 0)[0])


def _self_test():
    import plistlib
    import tempfile
    fails = []
    calls = []

    class Done:
        returncode = 0

    def fake(cmd, **kw):
        calls.append(cmd)
        return Done()

    with tempfile.TemporaryDirectory() as d:
        os.environ["HOME"] = os.environ["USERPROFILE"] = d
        os.environ["APPDATA"] = os.path.join(d, "AppData")
        py, brain = "/usr/bin/py thon", "/p/bin/brain"
        path, text = unit_for("darwin", py, brain, 8771)
        if path != os.path.join(d, "Library", "LaunchAgents", "dev.baryo.lean-brain.plist"):
            fails.append(f"plist path: {path}")
        plist = plistlib.loads(text.encode())
        if plist["ProgramArguments"] != [py, brain, "serve", "--port", "8771"] or plist["Label"] != LABEL \
                or plist["RunAtLoad"] is not True:
            fails.append(f"plist content: {plist}")
        if plistlib.loads(unit_for("darwin", "a&b", brain, 1, "x<y")[1].encode())["ProgramArguments"][0] != "a&b":
            fails.append("plist must escape XML")
        path, text = unit_for("win32", py, brain, 8771)
        if not path.startswith(os.path.join(d, "AppData")) or not path.endswith(
                os.path.join("Microsoft", "Windows", "Start Menu", "Programs", "Startup", "lean-brain.cmd")):
            fails.append(f"windows path: {path}")
        if f'"{py}" "{brain}" "serve" "--port" "8771"' not in text:
            fails.append(f"windows text: {text!r}")
        path, text = unit_for("linux", py, brain, 8771)
        if path != os.path.join(d, ".config", "systemd", "user", "lean-brain.service") \
                or f'ExecStart="{py}" "{brain}" "serve" "--port" "8771"' not in text \
                or "WantedBy=default.target" not in text:
            fails.append(f"systemd unit: {path} {text!r}")
        for plat in ("darwin", "linux", "win32"):
            if uninstall(plat, fake) or installed(plat):
                fails.append(f"{plat}: uninstall of a missing unit must return False")
            calls.clear()
            p, started = install(plat, py, brain, 8771, "/r1,/r2", fake)
            if not os.path.exists(p) or not started or not installed(plat) or "/r1,/r2" not in open(p).read():
                fails.append(f"{plat}: install did not write and start: {p} {started}")
            if plat == "linux" and ["systemctl", "--user", "enable", "--now", "lean-brain.service"] not in calls:
                fails.append(f"linux enable call missing: {calls}")
            if plat == "darwin" and not calls[0][:2] == ["launchctl", "bootstrap"]:
                fails.append(f"darwin bootstrap call missing: {calls}")
            if plat == "win32" and calls:
                fails.append("windows must call nothing")
            if not uninstall(plat, fake) or os.path.exists(p):
                fails.append(f"{plat}: uninstall did not remove the unit")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print("usage: install.py --self-test", file=sys.stderr)
    sys.exit(2)

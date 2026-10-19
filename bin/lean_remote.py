#!/usr/bin/env python3
"""Everything test-phase needs to run a job on an SSH host: config, preflight, copy, command, cleanup.

  lean_remote.py --self-test

The config is ${TEST_PHASE_REMOTES_FILE:-~/.config/lean/remotes.ini}. TEST_PHASE_REMOTES=off turns every
remote off. One section per host and, optionally, one per repository:

  [remote dev]
  host = dev                  ; an ssh config Host name (required)
  workdir = ~/lean-runs
  cpus = 2
  memory = 8g
  min_free_gb = 15
  cache_max_gb = 10           ; the NuGet cache folder is emptied when it passes this, 0 keeps it
  image = mcr.microsoft.com/dotnet/sdk:10.0
  extra_paths = local-packages
  exclude = .env*, *.key, *.pem, *license-key*, secrets*

  [repo app]                  ; app is the brain project name of the job
  sidecars = db=postgres:16, cache=redis:7
  env_file = ~/.config/lean/app.env
  ready = pg_isready -U postgres

Nothing here runs a test. test-phase calls these functions and keeps the results, parsing and brain on
the local machine. The docs are in docs/remote-lane.md.
"""
import configparser
import fnmatch
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

DEFAULT_EXCLUDE = [".env*", "*.key", "*.pem", "*license-key*", "secrets*"]
DEFAULT_IMAGE = "mcr.microsoft.com/dotnet/sdk:10.0"
MARKER = "lean-remote: started"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]


class LaneProblem(Exception):
    """The host, the connection or the copy failed; the test command never ran."""


@dataclass
class Remote:
    name: str
    host: str
    workdir: str = "~/lean-runs"
    cpus: str = "2"
    memory: str = "8g"
    min_free_gb: int = 15
    image: str = DEFAULT_IMAGE
    extra_paths: list = field(default_factory=list)
    exclude: list = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    cache_max_gb: int = 10


@dataclass
class RepoCfg:
    name: str
    sidecars: dict = field(default_factory=dict)  # alias to image, in the order given
    env_file: str = ""
    ready: str = ""


def default_path(env):
    return env.get("TEST_PHASE_REMOTES_FILE") or os.path.expanduser("~/.config/lean/remotes.ini")


def split_list(text):
    return [x.strip() for x in text.split(",") if x.strip()]


def load(path=None, env=os.environ):
    """([Remote], {repo name: RepoCfg}). Empty when the file is missing or TEST_PHASE_REMOTES=off."""
    if env.get("TEST_PHASE_REMOTES") == "off":
        return [], {}
    path = path or default_path(env)
    if not os.path.isfile(path):
        return [], {}
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    cp.optionxform = str
    try:
        cp.read(path)
    except configparser.Error as e:
        raise ValueError(f"{path}: {e}")
    remotes, repos = [], {}
    for section in cp.sections():
        kind, _, name = section.partition(" ")
        name = name.strip()
        if kind not in ("remote", "repo") or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"[{section}]: a section is [remote NAME] or [repo NAME], NAME in letters, digits, - and _")
        sec = cp[section]

        def bad(key, why):
            raise ValueError(f"[{section}] {key}: {why}")

        known = REMOTE_KEYS if kind == "remote" else REPO_KEYS
        for key in sec:
            if key not in known:
                bad(key, "unknown key")
        if kind == "remote":
            r = Remote(name=name, host=sec.get("host", "").strip())
            if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", r.host) or r.host.startswith("-"):
                bad("host", "an ssh Host name is required")
            r.workdir = sec.get("workdir", r.workdir).strip()
            if not r.workdir or r.workdir in ("/", "~", "~/"):
                bad("workdir", "a folder of its own is required")
            r.cpus = sec.get("cpus", r.cpus).strip()
            if not re.fullmatch(r"\d+(\.\d+)?", r.cpus) or float(r.cpus) <= 0:
                bad("cpus", f"want a positive number, got {r.cpus!r}")
            r.memory = sec.get("memory", r.memory).strip()
            if not re.fullmatch(r"\d+[bkmgBKMG]?", r.memory):
                bad("memory", f"want a size like 8g, got {r.memory!r}")
            for key in ("min_free_gb", "cache_max_gb"):
                try:
                    val = int(sec.get(key, str(getattr(r, key))).strip())
                    if val < 0:
                        raise ValueError
                except ValueError:
                    bad(key, "want a whole number of GB")
                setattr(r, key, val)
            r.image = sec.get("image", r.image).strip()
            if not IMAGE.fullmatch(r.image):
                bad("image", f"not an image name: {r.image!r}")
            r.extra_paths = split_list(sec.get("extra_paths", ""))
            for p in r.extra_paths:
                if os.path.isabs(p) or ".." in p.split("/"):
                    bad("extra_paths", f"{p!r} must stay inside the repository")
            r.exclude = split_list(sec["exclude"]) if "exclude" in sec else list(DEFAULT_EXCLUDE)
            remotes.append(r)
        else:
            c = RepoCfg(name=name)
            for item in split_list(sec.get("sidecars", "")):
                alias, eq, image = item.partition("=")
                alias, image = alias.strip(), image.strip()
                if not eq or not re.fullmatch(r"[A-Za-z0-9_-]+", alias) or not IMAGE.fullmatch(image):
                    bad("sidecars", f"want alias=image, got {item!r}")
                c.sidecars[alias] = image
            c.env_file = os.path.expanduser(sec.get("env_file", "").strip())
            c.ready = sec.get("ready", "").strip()
            if c.ready and not c.sidecars:
                bad("ready", "needs at least one sidecar to run in")
            repos[name] = c
    names = [r.name for r in remotes]
    if len(set(names)) != len(names):
        raise ValueError("two [remote ...] sections share a name")
    return remotes, repos


IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*")
REMOTE_KEYS = {"host", "workdir", "cpus", "memory", "min_free_gb", "cache_max_gb", "image", "extra_paths",
               "exclude"}
REPO_KEYS = {"sidecars", "env_file", "ready"}


def sh_path(p):
    """A path for a remote shell: a leading ~/ becomes "$HOME"/, the rest is quoted."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def ssh_cmd(r):
    return ["ssh"] + SSH_OPTS + [r.host]


def ssh_argv(r, *remote_cmd):
    """ssh to the host; several words are quoted into the one string the remote shell reads."""
    argv = ssh_cmd(r)
    if remote_cmd:
        argv.append(" ".join(shlex.quote(w) for w in remote_cmd) if len(remote_cmd) > 1 else remote_cmd[0])
    return argv


def preflight(r, run=subprocess.run):
    """(True, "") when the host answers, has the disk and has docker up; else (False, why)."""
    wd = sh_path(r.workdir)
    script = (f"mkdir -p {wd} 2>/dev/null; echo free_gb=$(df -Pk {wd} 2>/dev/null | awk 'NR==2{{print int($4/1048576)}}'); "
              "if docker info >/dev/null 2>&1; then echo docker=ok; else echo docker=down; fi")
    try:
        p = run(ssh_argv(r, script), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{r.name}: no answer from {r.host} ({type(e).__name__})"
    free = re.search(r"^free_gb=(\d+)$", p.stdout or "", re.M)
    if p.returncode != 0 or not free:
        why = ((p.stderr or "").strip().splitlines() or [""])[-1][:120]
        return False, f"{r.name}: no answer from {r.host}" + (f" ({why})" if why else "")
    if int(free.group(1)) < r.min_free_gb:
        return False, f"{r.name}: {free.group(1)} GB free under {r.workdir}, need {r.min_free_gb} GB"
    if "docker=ok" not in p.stdout:
        return False, f"{r.name}: docker is not running on {r.host}"
    return True, ""


def remote_load(r, run=subprocess.run):
    """The host's 1 minute load average, or None."""
    try:
        p = run(ssh_argv(r, "cat /proc/loadavg"), capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL)
        return float((p.stdout or "").split()[0]) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def self_test():
    fails = []
    with tempfile.TemporaryDirectory() as d:
        ini = os.path.join(d, "remotes.ini")
        with open(ini, "w") as fh:
            fh.write("""[remote dev]
host = devbox            ; from ssh config
workdir = ~/lean-runs
cpus = 2
memory = 8g
min_free_gb = 15
extra_paths = local-packages, tools

[repo app]
sidecars = db=postgres:16, cache=redis:7
env_file = /tmp/app.env
ready = pg_isready -U postgres
""")
        remotes, repos = load(ini, {})
        r = remotes[0] if remotes else None
        if not r or (r.name, r.host, r.workdir, r.cpus, r.memory, r.min_free_gb) != ("dev", "devbox", "~/lean-runs", "2", "8g", 15):
            fails.append(f"remote parse: {remotes}")
        elif r.exclude != DEFAULT_EXCLUDE or r.extra_paths != ["local-packages", "tools"] or r.image != DEFAULT_IMAGE:
            fails.append(f"remote defaults or lists: {r}")
        c = repos.get("app")
        if not c or list(c.sidecars.items()) != [("db", "postgres:16"), ("cache", "redis:7")] \
                or c.env_file != "/tmp/app.env" or c.ready != "pg_isready -U postgres":
            fails.append(f"repo parse: {repos}")
        if load(os.path.join(d, "none.ini"), {}) != ([], {}):
            fails.append("a missing file must give no remotes")
        if load(ini, {"TEST_PHASE_REMOTES": "off"}) != ([], {}):
            fails.append("TEST_PHASE_REMOTES=off must give no remotes")
        if load(None, {"TEST_PHASE_REMOTES_FILE": ini})[0][0].name != "dev":
            fails.append("TEST_PHASE_REMOTES_FILE must point at the config")
        for body, want in (("[remote x]\nworkdir = /w\n", "[remote x] host"),
                           ("[remote x]\nhost = h\ncpus = lots\n", "[remote x] cpus"),
                           ("[remote x]\nhost = h\nmemory = 8 giga\n", "[remote x] memory"),
                           ("[remote x]\nhost = h\nmin_free_gb = ten\n", "[remote x] min_free_gb"),
                           ("[remote x]\nhost = h\nbogus = 1\n", "[remote x] bogus"),
                           ("[remote x]\nhost = -oProxyCommand=y\n", "[remote x] host"),
                           ("[repo y]\nsidecars = postgres:16\n", "[repo y] sidecars"),
                           ("[box z]\nhost = h\n", "[box z]")):
            with open(ini, "w") as fh:
                fh.write(body)
            try:
                load(ini, {})
                fails.append(f"a bad config must raise: {body!r}")
            except ValueError as e:
                if want not in str(e):
                    fails.append(f"error must name the section and key {want!r}: {e}")
        argv = ssh_argv(r, "echo", "a b")
        if "BatchMode=yes" not in argv or "StrictHostKeyChecking=yes" not in argv or argv[-2] != "devbox" \
                or argv[-1] != "echo 'a b'":
            fails.append(f"ssh_argv: {argv}")

        class Fake:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake(p):
            return lambda argv, **kw: p

        ok, why = preflight(r, fake(Fake(255, "", "ssh: Could not resolve hostname\n")))
        if ok or "no answer" not in why or "devbox" not in why:
            fails.append(f"an unreachable host: {ok} {why!r}")
        ok, why = preflight(r, fake(Fake(0, "free_gb=9\ndocker=ok\n")))
        if ok or "9 GB" not in why or "15 GB" not in why:
            fails.append(f"short disk must name both numbers: {ok} {why!r}")
        ok, why = preflight(r, fake(Fake(0, "free_gb=40\ndocker=down\n")))
        if ok or "docker" not in why:
            fails.append(f"docker down: {ok} {why!r}")
        if preflight(r, fake(Fake(0, "free_gb=40\ndocker=ok\n"))) != (True, ""):
            fails.append("a good answer must pass")

        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)

        if preflight(r, boom)[0]:
            fails.append("a timeout must not pass")
        if remote_load(r, fake(Fake(0, "0.52 0.40 0.30 1/200 99\n"))) != 0.52 or remote_load(r, fake(Fake(255))) is not None:
            fails.append("remote_load")
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(2)

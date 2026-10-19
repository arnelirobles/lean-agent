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
  ready = pg_isready -h 127.0.0.1 -U postgres

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


def excluded(rel, patterns):
    """True when the path, its basename or any folder on the way matches an exclude glob."""
    parts = rel.split("/")
    return any(fnmatch.fnmatchcase(rel, pat) or any(fnmatch.fnmatchcase(x, pat) for x in parts) for pat in patterns)


def file_list(repo, extra_paths, exclude):
    """Relative paths to copy: tracked and untracked files git does not ignore, plus each extra path."""
    p = subprocess.run(["git", "-C", repo, "ls-files", "-z", "-co", "--exclude-standard"], capture_output=True)
    if p.returncode != 0:
        raise LaneProblem("git ls-files failed in " + repo)
    found = {x.decode("utf-8", "surrogateescape") for x in p.stdout.split(b"\0") if x}
    for extra in extra_paths:
        top = os.path.join(repo, extra)
        if os.path.isdir(top) and not os.path.islink(top):
            for root, _, files in os.walk(top):
                for f in files:
                    found.add(os.path.relpath(os.path.join(root, f), repo))
        elif os.path.lexists(top):
            found.add(os.path.normpath(extra))
    # .lean.env is where send_env writes; a repo file of that name must not be copied over it.
    return sorted(f for f in found if os.path.lexists(os.path.join(repo, f)) and not excluded(f, exclude)
                  and f != ENV_NAME)


ENV_NAME = ".lean.env"


def job_dir(r, job_id):
    return f"{r.workdir.rstrip('/')}/job-{job_id}"


def sync_argv(r, repo, job_id):
    """rsync argv; the caller writes the NUL-joined file_list on its stdin."""
    jd = sh_path(job_dir(r, job_id))
    # --safe-links skips a symlink that points outside the repo, so nothing outside the job folder is reached.
    return ["rsync", "-a", "--safe-links", "--timeout=600", "--files-from=-", "--from0", "-e", " ".join(ssh_cmd_opts()),
            f"--rsync-path=mkdir -p {jd} && rsync", repo.rstrip("/") + "/", f"{r.host}:{job_dir(r, job_id)}/"]


def ssh_cmd_opts():
    return ["ssh"] + SSH_OPTS


def send_env(r, job_id, env_file, run=subprocess.run):
    """Put the env file in the job folder, mode 600, by ssh stdin. Never on a command line."""
    jd = sh_path(job_dir(r, job_id))
    try:
        with open(env_file, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise LaneProblem(f"cannot read env_file {env_file}: {e.strerror}")
    try:
        p = run(ssh_argv(r, # A fresh temp file renamed into place, so a symlink already at that name is replaced, never followed.
            f"umask 077; mkdir -p {jd} && t=$(mktemp {jd}/.lean.env.XXXXXX) && cat > \"$t\" && mv -f \"$t\" {jd}/.lean.env"),
                input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise LaneProblem(f"{r.name}: env file not sent ({type(e).__name__})")
    if p.returncode != 0:
        raise LaneProblem(f"{r.name}: env file not sent (exit {p.returncode})")


def run_script(r, cfg, job_id, cmd):
    """One shell script for ssh: network, sidecars, then the capped test container. Its exit code is the test's."""
    jd = sh_path(job_dir(r, job_id))
    nuget = sh_path(r.workdir.rstrip("/") + "/.nuget")
    label, net = f"lean.job={job_id}", f"lean-{job_id}"
    caps = f"--cpus {shlex.quote(r.cpus)} --memory {shlex.quote(r.memory)}"
    env = f" --env-file {jd}/.lean.env" if cfg and cfg.env_file else ""
    fail = lambda why: f'{{ echo "lean-remote: {why}"; exit 125; }}'
    # A job runs several commands (its tier, then class reruns); each starts from a clean network and sidecars.
    out = remove_labelled(f"label={label}") + [f"install -d {nuget}",
           f"docker network create --label {label} {net} >/dev/null || " + fail("docker network failed")]
    sidecars = list(cfg.sidecars.items()) if cfg else []
    for alias, image in sidecars:
        out.append(f"docker run -d --rm --name {net}-{alias} --label {label} --network {net} "
                   f"--network-alias {alias} {caps}{env} {shlex.quote(image)} >/dev/null || " + fail(f"sidecar {alias} failed"))
    if sidecars and cfg.ready:
        out.append("n=0")
        # Two passes a second apart: an image's temporary init server can answer once and then restart.
        probe = f"docker exec {net}-{sidecars[0][0]} sh -c {shlex.quote(cfg.ready)} >/dev/null 2>&1"
        out.append(f"until {probe} && sleep 1 && {probe}; do")
        out.append('  n=$((n + 1)); [ "$n" -ge 60 ] && ' + fail("sidecar not ready") + "; sleep 1")
        out.append("done")
    inner = f'echo "{MARKER}"; exec nice -n 19 ' + " ".join(shlex.quote(c) for c in cmd)
    out.append(f'docker run --rm --name {net}-run --label {label} --network {net} {caps} --user "$(id -u):$(id -g)" '
               f"-e HOME=/tmp -e NUGET_PACKAGES=/nuget -v {nuget}:/nuget -v {jd}:/work -w /work{env} "
               f"{shlex.quote(r.image)} sh -c {shlex.quote(inner)}")
    out.append("rc=$?")
    out += remove_labelled(f"label={label}")
    out.append('exit "$rc"')
    return "\n".join(out) + "\n"


def remove_labelled(selector):
    """Shell lines that remove every container and network matching a docker label filter."""
    return [f'ids=$(docker ps -aq --filter {selector} 2>/dev/null); [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1',
            f'nets=$(docker network ls -q --filter {selector} 2>/dev/null); [ -n "$nets" ] && docker network rm $nets >/dev/null 2>&1']


def cleanup_script(r, job_id):
    """Remove the job's containers, network and folder. Never fails."""
    out = remove_labelled(f"label=lean.job={job_id}")
    out.append(f"rm -rf {sh_path(job_dir(r, job_id))}")
    return "\n".join(out) + "\nexit 0\n"


def sweep_script(r, running_ids):
    """Remove what belongs to no running job: labelled objects, job folders, and folders over a day old.
    The package cache is emptied when it passes cache_max_gb and no job is using it. Never fails."""
    keep = " " + " ".join(str(int(i)) for i in running_ids) + " "
    wd = sh_path(r.workdir.rstrip("/"))
    out = [f'keep="{keep}"',
           'for id in $(docker ps -aq --filter label=lean.job 2>/dev/null); do',
           '  j=$(docker inspect -f \'{{index .Config.Labels "lean.job"}}\' "$id" 2>/dev/null)',
           '  case "$keep" in *" $j "*) ;; *) docker rm -f "$id" >/dev/null 2>&1 ;; esac',
           "done",
           'for net in $(docker network ls -q --filter label=lean.job 2>/dev/null); do',
           '  j=$(docker network inspect -f \'{{index .Labels "lean.job"}}\' "$net" 2>/dev/null)',
           '  case "$keep" in *" $j "*) ;; *) docker network rm "$net" >/dev/null 2>&1 ;; esac',
           "done",
           f"for d in {wd}/job-*; do",
           '  [ -d "$d" ] || continue',
           '  n=${d##*/job-}',
           '  case "$keep" in *" $n "*) [ -n "$(find "$d" -maxdepth 0 -mtime +1 2>/dev/null)" ] && rm -rf "$d" ;; *) rm -rf "$d" ;; esac',
           "done"]
    if r.cache_max_gb > 0:
        out += [f"if [ -d {wd}/.nuget ] && [ -z \"$(docker ps -q --filter label=lean.job 2>/dev/null)\" ]; then",
                f'  kb=$(du -sk {wd}/.nuget 2>/dev/null | cut -f1)',
                f'  [ "${{kb:-0}}" -gt {r.cache_max_gb * 1048576} ] && rm -rf {wd}/.nuget',
                "fi"]
    return "\n".join(out) + "\nexit 0\n"


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
ready = pg_isready -h 127.0.0.1 -U postgres
""")
        remotes, repos = load(ini, {})
        r = remotes[0] if remotes else None
        if not r or (r.name, r.host, r.workdir, r.cpus, r.memory, r.min_free_gb) != ("dev", "devbox", "~/lean-runs", "2", "8g", 15):
            fails.append(f"remote parse: {remotes}")
        elif r.exclude != DEFAULT_EXCLUDE or r.extra_paths != ["local-packages", "tools"] or r.image != DEFAULT_IMAGE:
            fails.append(f"remote defaults or lists: {r}")
        c = repos.get("app")
        if not c or list(c.sidecars.items()) != [("db", "postgres:16"), ("cache", "redis:7")] \
                or c.env_file != "/tmp/app.env" or c.ready != "pg_isready -h 127.0.0.1 -U postgres":
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
    with tempfile.TemporaryDirectory() as d:
        repo = os.path.join(d, "repo")
        os.makedirs(os.path.join(repo, "local-packages"))
        for name, text in (("a.cs", "x"), (".env.local", "TOKEN=abc123"), ("secrets.json", "{}"),
                           ("local-packages/p.nupkg", "p"), ("local-packages/x.key", "k")):
            with open(os.path.join(repo, name), "w") as fh:
                fh.write(text)
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write("local-packages/\n")
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "add", "a.cs", ".env.local"], check=True)
        got = file_list(repo, ["local-packages"], DEFAULT_EXCLUDE)
        if got != [".gitignore", "a.cs", "local-packages/p.nupkg"]:
            fails.append(f"file_list must drop excluded files, extra paths included, and keep ignored extra paths: {got}")
        if file_list(repo, [], ["a.*"]) != [".env.local", ".gitignore", "secrets.json"]:
            fails.append("exclude must apply to a custom list")
        os.symlink("/etc/hosts", os.path.join(repo, ".lean.env"))
        if ".lean.env" in file_list(repo, [], []):
            fails.append("a repo file named .lean.env must never be copied over the env file")
        os.remove(os.path.join(repo, ".lean.env"))
        if "--safe-links" not in sync_argv(Remote(name="dev", host="h", workdir="~/w"), repo, 1):
            fails.append("rsync must skip symlinks that point outside the repo")
        if file_list(repo, ["local-packages/p.nupkg"], DEFAULT_EXCLUDE)[-1] != "local-packages/p.nupkg":
            fails.append("an extra path may be one file")
        r = Remote(name="dev", host="devbox", workdir="~/lean-runs", cpus="2", memory="8g")
        cfg = RepoCfg(name="app", sidecars={"db": "postgres:16"}, env_file=os.path.join(d, "app.env"),
                      ready="pg_isready -h 127.0.0.1 -U postgres")
        with open(cfg.env_file, "w") as fh:
            fh.write("POSTGRES_PASSWORD=hunter2\n")
        sc = run_script(r, cfg, 7, ["dotnet", "test", "--project", "T.csproj"])
        for want in ("--label lean.job=7", "--cpus 2", "--memory 8g", "--user", "lean-remote: started",
                     "--network lean-7", "--network-alias db", "--env-file", "nice -n 19",
                     "pg_isready -h 127.0.0.1 -U postgres",
                     "NUGET_PACKAGES=/nuget", "install -d"):
            if want not in sc:
                fails.append(f"run_script must contain {want!r}")
        if sc.count("docker exec lean-7-db") != 2 or "&& sleep 1 &&" not in sc:
            fails.append("the ready check must pass twice, a second apart")
        if " -p " in sc or "-p " in sc or "hunter2" in sc:
            fails.append("run_script must publish no ports and hold no env file value")
        bare = run_script(r, None, 8, ["dotnet", "test"])
        if "--env-file" in bare or "network-alias" in bare or "until " in bare:
            fails.append("a job with no repo section has no sidecars and no env file")
        cs = cleanup_script(r, 7)
        if "lean.job=7" not in cs or "job-7" not in cs or "lean.job=8" in cs:
            fails.append(f"cleanup_script: {cs!r}")
        ss = sweep_script(r, [3])
        if 'keep=" 3 "' not in ss or "job-*" not in ss or "-mtime +1" not in ss:
            fails.append(f"sweep_script must keep running ids and drop old folders: {ss!r}")
        for script in (sc, cs, ss, bare):
            if subprocess.run(["sh", "-n", "-c", script]).returncode != 0:
                fails.append("a generated script is not valid shell")
        sa = sync_argv(r, repo, 7)
        if sa[0] != "rsync" or "--files-from=-" not in sa or sa[-1] != "devbox:~/lean-runs/job-7/" \
                or "StrictHostKeyChecking=yes" not in sa[sa.index("-e") + 1]:
            fails.append(f"sync_argv: {sa}")

        class Rec:
            def __init__(self, rc=0):
                self.rc, self.calls = rc, []

            def __call__(self, argv, **kw):
                self.calls.append((argv, kw))
                return subprocess.CompletedProcess(argv, self.rc, b"", b"")

        rec = Rec()
        send_env(r, 7, cfg.env_file, rec)
        argv, kw = rec.calls[0]
        if kw.get("input") != b"POSTGRES_PASSWORD=hunter2\n" or "hunter2" in " ".join(argv) \
                or "umask 077" not in argv[-1] or "mktemp" not in argv[-1] or "mv -f" not in argv[-1] \
                or "cat > /" in argv[-1] or "cat > ~" in argv[-1]:
            fails.append(f"send_env must pass the file on stdin, mode 600, never in argv: {argv}")
        try:
            send_env(r, 7, cfg.env_file, Rec(255))
            fails.append("a failed env upload must raise LaneProblem")
        except LaneProblem:
            pass
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

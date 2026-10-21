#!/usr/bin/env python3
"""Lane lock and stall watchdog for bin/heavy.sh, for machines without util-linux flock (macOS).

  heavy_lock.py probe <lockfile>                          exit 0 if free, 1 if held
  heavy_lock.py run <lockfile> <wait> <marker> <cmd...>   take the lock, run cmd at nice 19

run waits up to <wait> seconds for the lock and exits 75 without creating <marker> on timeout.
Once it has the lock it creates <marker>, so the caller can tell a timeout from a command that
exits 75 itself. The lock fd is close-on-exec, so a child that outlives the command never keeps the
lane locked; the lock covers the command because this process holds it until the command exits.

HEAVY_STALL (seconds) turns on the watchdog. The command then runs in its own process group with
its output piped through. A progress line is one that matches HEAVY_PROGRESS and whose matched text
differs from the last progress line, so a repeating "[+12/x0/?0]" line counts only while the counts
move. After HEAVY_STALL seconds without progress the group gets SIGTERM, then SIGKILL, and this
exits 76. Only the command heavy.sh started is stopped. After the SIGTERM the group always gets a
SIGKILL too, because a child that ignores SIGTERM would otherwise outlive the lock.

HEAVY_FAIL_STALL (seconds) stops a run that adds only failures. The clock starts at the first failed test
after the last passed one and resets whenever a test passes; after that many seconds of failures with no pass
the group is stopped and this exits 78 with "stalled-failing". Passed and failed are counted from the
"[+N/xM]" counter when the output has one, else from result lines. Both stall messages end with
"(passed P, failed F, last test: NAME)". The two watchdogs are independent: either, both or neither may be set.

HEAVY_HELD=<lock path>:<pid>:<nonce> says the caller already holds the lane lock for the whole job (bin/test-phase
does, so a job's alone reruns keep the lane). run then takes no lock, waits for none and reaps no orphan.
It counts only when the lock path is this lane's, <lock>.held holds the nonce (claim() writes it), the lock is
held, and pid is one of our parents; otherwise the lane is taken as usual. The command does not inherit it.
  heavy_lock.py held <lockfile>                           exit 0 if HEAVY_HELD holds for this lane

The watchdog's process group is recorded in <lockfile>.pgid while the command runs and removed when it
ends. It is written 0600 with O_NOFOLLOW into a lock directory that must be a real directory owned by the
caller (made 0700); otherwise no record is kept. It holds the group id, the leader's start time and a random
token also passed to the command as HEAVY_LANE_TOKEN. If this process is itself killed with SIGKILL, the group is orphaned in its own session; the next
run that gets the lock finds the record, and a group whose leader still has the recorded start time is killed (one line on stderr) before
the new command starts. A reused pid, a missing leader or a record that does not parse kills nothing.

  heavy_lock.py --self-test
"""
import fcntl
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time

DEFAULT_PROGRESS = (r"\[\+\d+/x\d+(?:/\?\d+)?\]"
                    r"|^\s*(?:passed|failed|Passed|Failed)\b.*")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
COUNTER = re.compile(r"\[\+(\d+)/x(\d+)(?:/\?\d+)?\]")
PASS_LINE = re.compile(r"^(?:passed|  Passed)\s+(\S.*)")
FAIL_LINE = re.compile(r"^(?:failed|  Failed)\s+(\S.*)")
START_LINE = re.compile(r"^\s*(?:started|starting|running)\s+\S.*", re.I)


def acquire(path, wait):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)  # non-inheritable by default
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.2)


def ancestors():
    """Pids of this process's parents, nearest first."""
    pid, out = os.getppid(), []
    while pid > 1 and len(out) < 32:
        out.append(pid)
        try:
            pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                     timeout=10).stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            break
    return out


def claim(path):
    """Write the claim file for a lock this process holds; returns the HEAVY_HELD value that names it."""
    nonce = secrets.token_hex(16)
    fd = os.open(path + ".held", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(nonce)
    return f"{os.path.realpath(path)}:{os.getpid()}:{nonce}"


def unclaim(path):
    try:
        os.remove(path + ".held")
    except OSError:
        pass


def held_here(path):
    """True when HEAVY_HELD (<lock path>:<holder pid>:<nonce>) is a live claim on this lane: the claim file
    carries the nonce, the lock is held, and the holder is one of our own parents. Anything else, such as a
    stray value, another lane's claim or a lane that is free, is ignored and the lane is taken as usual."""
    parts = os.environ.get("HEAVY_HELD", "").rsplit(":", 2)
    if len(parts) != 3 or not parts[1].isdigit() or os.path.realpath(parts[0]) != os.path.realpath(path):
        return False
    try:
        with open(path + ".held") as fh:
            if not parts[2] or fh.read().strip() != parts[2]:
                return False
    except OSError:
        return False
    return int(parts[1]) in ancestors() and probe(path) == 1


def probe(path):
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1
    finally:
        os.close(fd)
    return 0


class Watch:
    def __init__(self, pattern):
        self.pattern = pattern
        self.mutex = threading.Lock()
        self.last = time.monotonic()
        self.sig = None
        self.passed = self.failed = 0  # totals over every assembly
        self.base_p = self.base_f = self.cur_p = self.cur_f = 0  # finished assemblies, and the one running
        self.counter = False  # the "[+N/xM]" counter is exact, so once seen it replaces counting result lines
        self.fail_since = None  # when the first failure after the last pass arrived
        self.last_test = ""

    def count(self, text):
        m = COUNTER.search(text)
        shown = text.strip()[:150]
        with self.mutex:
            if m:
                self.counter = True
                self.last_test = shown
                p, f = int(m.group(1)), int(m.group(2))
                if p < self.cur_p or f < self.cur_f:  # the counter restarted: the next test assembly
                    self.base_p, self.base_f = self.passed, self.failed
                    self.cur_p = self.cur_f = 0
                    self.fail_since = None
            elif PASS_LINE.match(text) or FAIL_LINE.match(text):
                self.last_test = shown
                if self.counter:  # the counter is exact; the result line only names the test
                    return
                ok = bool(PASS_LINE.match(text))
                p, f = self.cur_p + ok, self.cur_f + (not ok)
            elif START_LINE.match(text):
                self.last_test = shown
                return
            else:
                return
            if p > self.cur_p:
                self.cur_p, self.fail_since = p, None
            if f > self.cur_f:
                self.cur_f = f
                if self.fail_since is None:
                    self.fail_since = time.monotonic()
            self.passed, self.failed = self.base_p + self.cur_p, self.base_f + self.cur_f

    def line(self, text):
        text = ANSI.sub("", text)
        self.count(text)
        m = self.pattern.search(text)
        if not m:
            return
        with self.mutex:
            if m.group(0) != self.sig:
                self.sig = m.group(0)
                self.last = time.monotonic()

    def idle(self):
        with self.mutex:
            return time.monotonic() - self.last

    def fail_idle(self):
        """Seconds of failures with no pass since, 0 when the last thing to happen was a pass."""
        with self.mutex:
            return 0 if self.fail_since is None else time.monotonic() - self.fail_since

    def facts(self):
        with self.mutex:
            return f"passed {self.passed}, failed {self.failed}, last test: {self.last_test or 'none seen'}"


def pump(fd, out, watch):
    buf = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        try:
            out.write(chunk)
            out.flush()
        except (OSError, ValueError):
            pass
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            if part:
                watch.line(part.decode("utf-8", "replace"))
    if buf:
        watch.line(buf.decode("utf-8", "replace"))


def private_dir(path):
    """True when the lock directory is a real directory of ours, made private. Else the record is not used."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)  # lstat: a symlink in place of the directory is refused
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
        return True
    except OSError:
        return False


def start_time(pid):
    """The process start time as ps prints it, or '' when there is no such process."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(r.stdout.split()) if r.returncode == 0 else ""


def write_record(record, pgid, nonce):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(record, flags, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{pgid}\n{start_time(pgid)}\n{nonce}\n")


def read_record(record):
    try:
        fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as fh:
        if os.fstat(fh.fileno()).st_uid != os.getuid():
            return None
        parts = fh.read().split("\n")
    try:
        return int(parts[0]), parts[1], parts[2]
    except (ValueError, IndexError):
        return None


def token_ok(pid, nonce):
    """False only when the leader's environment is readable and carries a different token."""
    try:
        r = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    text = r.stdout if r.returncode == 0 else ""
    if "HEAVY_LANE_TOKEN=" not in text:
        return True  # cannot read it here; the start time still has to match
    return f"HEAVY_LANE_TOKEN={nonce}" in text


def reap_orphan(record):
    """Kill the process group a dead earlier run left behind. Call only while holding the lock.

    The record is believed only if the group leader is still there with the recorded start time (and token):
    a reused pid is another process, and nothing is killed."""
    rec = read_record(record)
    try:
        os.remove(record)
    except OSError:
        pass
    if not rec:
        return
    pgid, started, nonce = rec
    if pgid <= 1 or not started or start_time(pgid) != started or not token_ok(pgid, nonce):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return
    sys.stderr.write(f"heavy.sh: killed process group {pgid} left by an earlier run that was killed\n")
    sys.stderr.flush()


def clean_env():
    """The command's environment without HEAVY_HELD: the claim is for this lane only, so a heavy.sh the
    command starts must take its own lock."""
    return {k: v for k, v in os.environ.items() if k != "HEAVY_HELD"}


def run(argv, stall, pattern, record=None, fail_stall=0):
    nonce = secrets.token_hex(16)
    cmd = ["nice", "-n", "19"] + argv
    if not stall and not fail_stall:
        proc = subprocess.Popen(cmd, env=clean_env())

        def forward(signum, _frame):
            if signum != signal.SIGINT:  # the terminal already sent ^C to the whole group
                try:
                    proc.send_signal(signum)
                except OSError:
                    pass

        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, forward)
        while True:
            try:
                rc = proc.wait()
                return rc if rc >= 0 else 128 - rc  # by signal: 128 + n, as a shell reports it
            except KeyboardInterrupt:
                continue

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                            env=dict(clean_env(), HEAVY_LANE_TOKEN=nonce))
    if record:
        try:
            write_record(record, proc.pid, nonce)
        except OSError:
            pass

    def killgroup(sig):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass

    def forward(signum, _frame):
        killgroup(signum)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, forward)
    watch = Watch(pattern)
    threads = []
    for stream, out in ((proc.stdout, sys.stdout.buffer), (proc.stderr, sys.stderr.buffer)):
        t = threading.Thread(target=pump, args=(stream.fileno(), out, watch), daemon=True)
        t.start()
        threads.append(t)
    stalled = ""
    while proc.poll() is None:
        if stall and watch.idle() >= stall:
            stalled = "stalled"
        elif fail_stall and watch.fail_idle() >= fail_stall:
            stalled = "stalled-failing"
        if stalled:
            killgroup(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            killgroup(signal.SIGKILL)  # a child that ignored SIGTERM must not outlive the lock
            proc.wait()
            break
        time.sleep(0.1)
    for t in threads:  # a leftover child can hold the pipe open; do not wait on it for long
        t.join(1)
    if stalled == "stalled":
        sys.stderr.write(f"heavy.sh: stalled: no test progress for {stall} s; likely load, not code"
                         f" ({watch.facts()})\n")
        sys.stderr.flush()
        return 76
    if stalled:
        sys.stderr.write(f"heavy.sh: stalled-failing: only failures for {fail_stall} s, no test passed"
                         f" ({watch.facts()})\n")
        sys.stderr.flush()
        return 78
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def self_test():
    import tempfile
    fails = []
    me = os.path.realpath(__file__)
    with tempfile.TemporaryDirectory() as d:
        lock, marker = os.path.join(d, "lane.lock"), os.path.join(d, "marker")
        env = dict(os.environ, HEAVY_STALL="1")

        def alive(pidfile):
            try:
                os.kill(int(open(pidfile).read().strip()), 0)
                return True
            except (OSError, ValueError):
                return False

        def wait_for(pred, secs=10):
            end = time.monotonic() + secs
            while time.monotonic() < end:
                if pred():
                    return True
                time.sleep(0.1)
            return False

        def cleanup(pidfile):
            try:
                os.kill(int(open(pidfile).read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        # 1. A child that ignores SIGTERM is killed after the stall, before the lock is released.
        pid1 = os.path.join(d, "ignore.pid")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            '(trap "" TERM; exec sleep 60) & echo $! > "$0"; wait', pid1],
                           env=env, capture_output=True, text=True, timeout=60)
        if r.returncode != 76:
            fails.append(f"a stall must exit 76, got {r.returncode}: {r.stderr}")
        if wait_for(lambda: not alive(pid1), 3) is False:
            fails.append("a child that ignored SIGTERM survived the stall")
            cleanup(pid1)

        # 2. SIGKILL of the caller orphans the group; the next run kills it and says so.
        pid2 = os.path.join(d, "orphan.pid")
        env2 = dict(os.environ, HEAVY_STALL="60")
        p = subprocess.Popen([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                              'sleep 60 & echo $! > "$0"; wait', pid2], env=env2,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(lambda: os.path.exists(lock + ".pgid") and os.path.exists(pid2)
                        and open(pid2).read().strip() != ""):
            fails.append("the child's process group was not recorded next to the lock")
        p.kill()
        p.wait()
        if not alive(pid2):
            fails.append("test setup: the orphan should outlive its caller")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "true"], env=env2,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or "killed process group" not in r.stderr:
            fails.append(f"the orphaned group must be killed with one line on stderr ({r.returncode}): {r.stderr!r}")
        if wait_for(lambda: not alive(pid2), 3) is False:
            fails.append("the orphaned group was still alive after the next run got the lock")
            cleanup(pid2)
        if os.path.exists(lock + ".pgid"):
            fails.append("the record must be cleared on a normal exit")

        # 3. A record is not trusted when the leader's start time differs (a reused pid), or it is a symlink,
        # or the directory is not ours. Nothing is killed.
        pid3 = os.path.join(d, "bystander.pid")
        by = subprocess.Popen(["sh", "-c", 'echo $$ > "$0"; exec sleep 60', pid3], start_new_session=True)
        wait_for(lambda: os.path.exists(pid3) and open(pid3).read().strip() != "")
        rec = lock + ".pgid"
        with open(rec, "w") as fh:
            fh.write(f"{by.pid}\nMon Jan  1 00:00:00 2001\nnotatoken\n")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "true"], env=env2,
                           capture_output=True, text=True, timeout=30)
        if by.poll() is not None or "killed" in r.stderr:
            fails.append(f"a record with another start time must kill nothing: {r.stderr!r}")
        if os.path.exists(rec):
            fails.append("a rejected record must be deleted")
        real = os.path.join(d, "real.rec")
        with open(real, "w") as fh:
            fh.write(f"{by.pid}\n{start_time(by.pid)}\nx\n")
        os.symlink(real, rec)
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "true"], env=env2,
                           capture_output=True, text=True, timeout=30)
        if by.poll() is not None or "killed" in r.stderr:
            fails.append(f"a symlinked record must be refused: {r.stderr!r}")
        if os.path.islink(rec):
            os.remove(rec)
        # a matching record does kill; a different token in a readable environment does not
        if read_record(real) is None:
            fails.append("a plain record file must be readable")
        by.kill()
        by.wait()
        link_dir = os.path.join(d, "linkdir")
        os.symlink(d, link_dir)
        if private_dir(os.path.join(link_dir, "x.lock")):
            fails.append("a symlinked lock directory must be refused")
        wide = os.path.join(d, "wide")
        os.mkdir(wide, 0o755)
        if not private_dir(os.path.join(wide, "x.lock")) or os.stat(wide).st_mode & 0o077:
            fails.append("our own directory must be accepted and made private")
        probe = os.path.join(wide, "rec")
        write_record(probe, os.getpid(), "n")
        if os.stat(probe).st_mode & 0o777 != 0o600:
            fails.append("the record must be mode 0600")
        os.remove(probe)
        os.symlink(real, probe)
        try:
            write_record(probe, os.getpid(), "n")
            fails.append("write_record followed a symlink")
        except OSError:
            pass
        # 4. A run that adds only failures is stopped after HEAVY_FAIL_STALL and exits 78 with the counts; a
        # run that keeps passing tests is not, even though it fails as many.
        envf = {k: v for k, v in os.environ.items() if k != "HEAVY_STALL"}
        envf["HEAVY_FAIL_STALL"] = "3"
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            'echo "passed A.B.One (1ms)"; while :; do echo "failed A.B.Two (1ms)"; sleep 0.3; done'],
                           env=envf, capture_output=True, text=True, timeout=60)
        if r.returncode != 78 or "stalled-failing" not in r.stderr or "(passed 1, failed " not in r.stderr \
                or "last test: failed A.B.Two" not in r.stderr:
            fails.append(f"only failures must stop the run with counts, exit 78 ({r.returncode}): {r.stderr!r}")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            'for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo "failed F.$i"; echo "passed P.$i"; sleep 0.4; done'],
                           env=envf, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            fails.append(f"a run that keeps passing tests must not be stopped ({r.returncode}): {r.stderr!r}")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            'for i in 1 2 3 4 5 6 7 8; do echo "[+$i/x$((i * 2))/?0] t"; sleep 0.5; done'],
                           env=envf, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            fails.append(f"a moving [+N/xM] counter with passes must not be stopped ({r.returncode}): {r.stderr!r}")

        # 5. A run killed by a signal exits 128 + n, with and without the watchdog.
        for extra in ({}, {"HEAVY_STALL": "60"}):
            r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c", "kill -TERM $$"],
                               env={**{k: v for k, v in os.environ.items() if k != "HEAVY_STALL"}, **extra},
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 143:
                fails.append(f"a command killed by SIGTERM must exit 143 {extra} ({r.returncode}): {r.stderr!r}")

        # 6. HEAVY_HELD=<lock>:<our pid> runs without taking the lock we hold; it is not passed to the command.
        fd = acquire(lock, 1)
        held_env = dict(os.environ, HEAVY_HELD=claim(lock))
        r = subprocess.run([sys.executable, me, "run", lock, "1", marker, "true"], env=held_env,
                           capture_output=True, text=True, timeout=30)
        busy = subprocess.run([sys.executable, me, "run", lock, "1", marker, "true"], env=envf,
                              capture_output=True, text=True, timeout=30)
        os.close(fd)
        if r.returncode != 0 or busy.returncode != 75:
            fails.append(f"HEAVY_HELD must skip the lock and a plain run must still wait ({r.returncode}, {busy.returncode})")
        fd = acquire(lock, 1)
        for extra in ({}, {"HEAVY_STALL": "60"}):
            r = subprocess.run([sys.executable, me, "run", lock, "1", marker, "sh", "-c", 'echo "[${HEAVY_HELD:-none}]"'],
                               env={**held_env, **extra}, capture_output=True, text=True, timeout=30)
            if r.stdout.strip() != "[none]":
                fails.append(f"the command must not inherit HEAVY_HELD {extra}: {r.stdout!r} {r.stderr!r}")
        os.close(fd)
        # A claim that is not ours does nothing: not another pid, not another lane, not a free lane.
        fd = acquire(lock, 1)
        good = held_env["HEAVY_HELD"]
        lk, pid, nonce = good.rsplit(":", 2)
        for bad in (f"{lk}:{int(pid) + 7}:{nonce}", f"{lk}x:{pid}:{nonce}", f"{lk}:{pid}:wrong", f"{lk}:{pid}", "1"):
            r = subprocess.run([sys.executable, me, "run", lock, "1", marker, "true"],
                               env=dict(os.environ, HEAVY_HELD=bad), capture_output=True, text=True, timeout=30)
            if r.returncode != 75:
                fails.append(f"HEAVY_HELD={bad!r} must not skip the lock ({r.returncode})")
        os.close(fd)
        r = subprocess.run([sys.executable, me, "held", lock], env=held_env, timeout=30)
        if r.returncode != 1:
            fails.append("a lane that is not held cannot be claimed as held")
        unclaim(lock)
        # Another session's watched run holds the lane. A stray HEAVY_HELD=1 must not reap or kill it.
        pidfile = os.path.join(d, "a.pid")
        a = subprocess.Popen([sys.executable, me, "run", lock, "5", marker, "sh", "-c", f"echo $$ > {pidfile}; sleep 4"],
                             env=envf)
        for _ in range(50):
            if os.path.exists(pidfile) and open(pidfile).read().strip():
                break
            time.sleep(0.1)
        b = subprocess.run([sys.executable, me, "run", lock, "1", marker, "true"], env=dict(envf, HEAVY_HELD="1"),
                           capture_output=True, text=True, timeout=30)
        still = alive(pidfile)
        arc = a.wait(timeout=30)
        if b.returncode != 75 or not still or arc != 0:
            fails.append(f"a stray HEAVY_HELD must leave the holder running (B {b.returncode}, alive {still}, A {arc})")
        # 7. A second assembly restarts the counter at zero: that is progress, not a failures-only run. A counter
        # alone still names the last test.
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            'echo "[+50/x2/?0] A.T"; for i in 1 2 3 4 5 6; do echo "[+$i/x0/?0] B.T"; sleep 0.5; done'],
                           env=envf, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            fails.append(f"a counter that restarts for the next assembly must not be stopped ({r.returncode}): {r.stderr!r}")
        r = subprocess.run([sys.executable, me, "run", lock, "5", marker, "sh", "-c",
                            'echo "[+50/x2/?0] A.T"; echo "[+3/x1/?0] Some.Test"; sleep 30'],
                           env=dict(os.environ, HEAVY_STALL="1"), capture_output=True, text=True, timeout=60)
        if r.returncode != 76 or "(passed 53, failed 3, last test: [+3/x1/?0] Some.Test)" not in r.stderr:
            fails.append(f"counts must add up across assemblies and the counter must name the last test ({r.returncode}): {r.stderr!r}")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


def main(args):
    if args == ["--self-test"]:
        return self_test()
    if len(args) == 2 and args[0] == "probe":
        return probe(args[1])
    if len(args) == 2 and args[0] == "held":
        return 0 if held_here(args[1]) else 1
    if len(args) >= 5 and args[0] == "run":
        path, wait, marker, argv = args[1], args[2], args[3], args[4:]
        try:
            wait = int(wait)
            stall = int(os.environ.get("HEAVY_STALL") or 0)
            fail_stall = int(os.environ.get("HEAVY_FAIL_STALL") or 0)
            pattern = re.compile(os.environ.get("HEAVY_PROGRESS") or DEFAULT_PROGRESS)
        except (ValueError, re.error) as e:
            sys.stderr.write(f"heavy.sh: bad HEAVY_WAIT, HEAVY_STALL, HEAVY_FAIL_STALL or HEAVY_PROGRESS: {e}\n")
            return 2
        held = held_here(path)  # the caller holds the lane for the whole job
        fd = None if held else acquire(path, wait)
        if fd is None and not held:
            return 75
        open(marker, "w").close()
        record = path + ".pgid" if private_dir(path) else None
        if record and not held:  # a held lane's record belongs to its holder, never to an orphan
            reap_orphan(record)
        try:
            return run(argv, stall, pattern, record, fail_stall)
        except FileNotFoundError as e:
            sys.stderr.write(f"heavy.sh: {e}\n")
            return 127
        finally:
            if record:
                try:
                    os.remove(record)
                except OSError:
                    pass
            if fd is not None:
                os.close(fd)
    sys.stderr.write("usage: heavy_lock.py probe <lock> | run <lock> <wait> <marker> <cmd...>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

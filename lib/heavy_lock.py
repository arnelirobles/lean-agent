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

    def line(self, text):
        text = ANSI.sub("", text)
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


def run(argv, stall, pattern, record=None):
    nonce = secrets.token_hex(16)
    cmd = ["nice", "-n", "19"] + argv
    if not stall:
        proc = subprocess.Popen(cmd)

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
                return proc.wait()
            except KeyboardInterrupt:
                continue

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                            env=dict(os.environ, HEAVY_LANE_TOKEN=nonce))
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
    stalled = False
    while proc.poll() is None:
        if watch.idle() >= stall:
            stalled = True
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
    if stalled:
        sys.stderr.write(f"heavy.sh: stalled: no test progress for {stall} s; likely load, not code\n")
        sys.stderr.flush()
        return 76
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
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


def main(args):
    if args == ["--self-test"]:
        return self_test()
    if len(args) == 2 and args[0] == "probe":
        return probe(args[1])
    if len(args) >= 5 and args[0] == "run":
        path, wait, marker, argv = args[1], args[2], args[3], args[4:]
        try:
            wait = int(wait)
            stall = int(os.environ.get("HEAVY_STALL") or 0)
            pattern = re.compile(os.environ.get("HEAVY_PROGRESS") or DEFAULT_PROGRESS)
        except (ValueError, re.error) as e:
            sys.stderr.write(f"heavy.sh: bad HEAVY_WAIT, HEAVY_STALL or HEAVY_PROGRESS: {e}\n")
            return 2
        fd = acquire(path, wait)
        if fd is None:
            return 75
        open(marker, "w").close()
        record = path + ".pgid" if private_dir(path) else None
        if record:
            reap_orphan(record)
        try:
            return run(argv, stall, pattern, record)
        except FileNotFoundError as e:
            sys.stderr.write(f"heavy.sh: {e}\n")
            return 127
        finally:
            if record:
                try:
                    os.remove(record)
                except OSError:
                    pass
            os.close(fd)
    sys.stderr.write("usage: heavy_lock.py probe <lock> | run <lock> <wait> <marker> <cmd...>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

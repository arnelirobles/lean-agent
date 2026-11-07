# Hook latency baseline

Wall time of today's PreToolUse Bash hooks, each run as its own process, taken before any hook moves to the dispatcher. Nothing else in the repository makes a speed claim.

Taken on 2026-11-07, Darwin arm64, Python 3.14.7, branch extensible-core at 19a9670 plus the dispatcher change.

## What was run

The members of the PreToolUse Bash group (group 1 in hooks/hooks.json) that apply to a `git status` command: commit-author, secret-scan, docs-containers, public-text, test-before-push, wait-loop, bg-bound, script-note. review-log-check and verify-claim carry an `if` for `gh` or `az` and do not run for `git status`.

Each hook is one `python3 hooks/<name>` process with the event on stdin, with `LEAN_PROFILE=strict` and scratch `LEAN_BRAIN_DB` and `LEAN_DB`. One warm-up run per hook is not counted. Then 20 rounds; each round runs the eight hooks one after another, and the group figure is the sum of one round. Median and p90 (the 18th of 20 sorted runs) are in milliseconds.

## Numbers

| hook | median ms | p90 ms |
|---|---|---|
| commit-author.py | 33.7 | 34.4 |
| secret-scan.py | 39.4 | 40.5 |
| docs-containers.py | 32.2 | 33.7 |
| public-text.py | 33.1 | 34.0 |
| test-before-push.py | 32.1 | 33.3 |
| wait-loop.py | 34.2 | 35.3 |
| bg-bound.py | 32.1 | 34.3 |
| script-note.py | 47.9 | 49.7 |
| group, run serially | 285.8 | 292.6 |

Claude Code starts the group's hooks in parallel, so the serial sum is not what a tool call waits for. It is the total process work.

## Script

Run from the repository root with `python3 latency.py`.

```python
import json, os, statistics, subprocess, sys, tempfile, time
root = os.getcwd()
tmp = tempfile.mkdtemp()
env = dict(os.environ, LEAN_PROFILE="strict", LEAN_BRAIN_DB=tmp + "/brain.db", LEAN_DB=tmp + "/lean.db")
event = json.dumps({"hook_event_name": "PreToolUse", "session_id": "latency", "cwd": root,
                    "tool_name": "Bash", "tool_input": {"command": "git status"}})
members = ["commit-author.py", "secret-scan.py", "docs-containers.py", "public-text.py",
           "test-before-push.py", "wait-loop.py", "bg-bound.py", "script-note.py"]
def once(name):
    t = time.perf_counter()
    subprocess.run([sys.executable, "hooks/" + name], input=event, capture_output=True, text=True, env=env, cwd=root)
    return time.perf_counter() - t
def stats(xs):
    xs = sorted(xs)
    return statistics.median(xs) * 1000, xs[int(round(0.9 * (len(xs) - 1)))] * 1000
for name in members: once(name)  # one warm-up run each, not counted
per = {m: [] for m in members}
group = []
for _ in range(20):
    total = 0
    for m in members:
        d = once(m); per[m].append(d); total += d
    group.append(total)
print("| hook | median ms | p90 ms |\n|---|---|---|")
for m in members:
    a, b = stats(per[m]); print("| %s | %.1f | %.1f |" % (m, a, b))
a, b = stats(group); print("| group, serial | %.1f | %.1f |" % (a, b))
```

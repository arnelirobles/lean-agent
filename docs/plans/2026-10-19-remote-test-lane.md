# Remote Test Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** test-phase drains the queue on the local machine and on SSH hosts at the same time, with results filed in brain exactly as today.

**Architecture:** brain gets the columns and queue verbs a lane needs (platform, host, `--where`, requeue). A new stdlib module `bin/lean_remote.py` holds everything about a host: config, preflight, file list, copy, the docker command, cleanup. test-phase runs one thread per lane; a remote lane wraps `ssh` in `heavy.sh` so the stall watchdog and parser are unchanged.

**Tech Stack:** Python 3 stdlib, bash, sqlite3, ssh, rsync, docker on the host.

**Spec:** docs/specs/2026-10-19-remote-test-lane-design.md

## Global Constraints

- No new dependencies. Python stdlib only; works on macOS bash 3.2.
- No remote configured, or `TEST_PHASE_REMOTES=off`: behavior identical to today. The existing test-phase self-test must pass unchanged.
- Config file: `~/.config/lean/remotes.ini` (override with `TEST_PHASE_REMOTES_FILE`). Keys per `[remote <name>]`: `host`, `workdir`, `cpus`, `memory`, `min_free_gb`, `image`, `extra_paths`, `exclude`. Default exclude: `.env*, *.key, *.pem, *license-key*, secrets*`.
- SSH always with `-o BatchMode=yes -o StrictHostKeyChecking=yes`.
- No secret on any command line. The env file goes over ssh stdin, mode 600, deleted at cleanup.
- Marker line: exactly `lean-remote: started`.
- Labels: `lean.job=<id>` on every container and network. Lane name: `remote-<name>`.
- Rest after a lane problem: 600 seconds, cut short when the drain ends.
- New migration only (`migrate_7`); never edit 1 to 6.
- Public repo: no host IPs, no private project or paid-module names in code, docs or tests.
- Style: no em dashes, no arrow glyphs, short lowercase imperative commit subjects, no trailers.

## Review Focus

1. A container that writes `bin/` and `obj/` as root leaves files the SSH user cannot delete: run the container as the SSH user's uid:gid and keep the NuGet cache in a host folder owned by that user (Task 3 test: the docker argv carries `--user`).
2. A remote lane that rests while the local lane finishes the queue must not keep the drainer alive for 10 minutes (Task 4 test: drain ends within seconds when the queue empties during a rest).
3. A file under `extra_paths` that matches an exclude (for example `local-packages/x.key`) is never copied (Task 3 test).
4. A rerun of a failing class on a remote lane runs on the same host and records the host (Task 4 test).
5. The new brain opening a schema-6 database migrates it, and an old brain then refuses it: the rollout must replace every brain copy together (Task 5 rollout note).

---

### Task 1: brain columns and queue verbs

**Files:**
- Modify: `bin/brain` (migrations, `cmd_queue`, `job_line`, `cmd_run_add`, parsers, `self_test`)

**Interfaces:**
- Produces:
  - `migrate_7`: `jobs.platform TEXT NOT NULL DEFAULT ''`, `jobs.host TEXT NOT NULL DEFAULT ''`, `runs.host TEXT NOT NULL DEFAULT ''`.
  - `brain queue add ... [--platform apple|auto|none]` (default `auto`): `auto` reads `repo_dir/test_project` (a `.csproj`, or the single `.csproj` in that folder) and sets `apple` when `TargetFramework` or `TargetFrameworks` contains `-maccatalyst`, `-ios` or `-macos`; unreadable means empty.
  - `brain queue next [--peek] [--where local|remote] [--host NAME]`: `--where remote` skips `platform='apple'`; `--host` is stored on the claimed job.
  - `brain queue requeue ID --reason R`: a running job goes back to `queued`, `host` and `started_at` cleared, reason kept. Any other status errors.
  - `brain run-add ... [--host NAME]`.
  - `job_line`: appends ` platform=apple` when set and ` on=<host>` while running on a host.
  - `queue list --status running` output keeps the form `job #N running ...` (test-phase parses it).

- [ ] **Step 1: Write the failing self-test cases** in brain's `self_test`, next to the bump cases:
  - a csproj with `<TargetFrameworks>net10.0;net10.0-maccatalyst</TargetFrameworks>` queued with no flag shows `platform=apple`; a plain `net10.0` one does not.
  - with only the apple job queued, `queue next --where remote` prints nothing and `queue next --where local` claims it.
  - `queue next --host dev` then `queue list` shows `on=dev`; `queue requeue <id> --reason "ssh failed"` shows it `queued` again with no `on=`.
  - `queue requeue` on a done job exits nonzero.
  - `run-add ... --host dev` stores `dev` in `runs.host`.
  - the schema version after open is 7.
- [ ] **Step 2:** Implement the above in `bin/brain`. Bump the usage text at the top of the file for the new flags.
- [ ] **Step 3: Commit** `git commit -m "let a queue job carry its platform and the host that runs it"`

### Task 2: lean_remote config and preflight

**Files:**
- Create: `bin/lean_remote.py` (with `--self-test`, so `tests/run-all.sh` picks it up)

**Interfaces:**
- Produces:
  - `@dataclass Remote(name, host, workdir, cpus, memory, min_free_gb, image, extra_paths: list[str], exclude: list[str])`
  - `@dataclass RepoCfg(name, sidecars: dict[str, str], env_file: str, ready: str)`; `sidecars = alias=image, alias=image`; `ready` is a shell command run with `docker exec` in the first sidecar until it exits 0, at most 60 s.
  - `load(path=None, env=os.environ) -> tuple[list[Remote], dict[str, RepoCfg]]`: empty when the file is missing or `TEST_PHASE_REMOTES=off`. A bad section raises `ValueError` naming the section and key.
  - `ssh_argv(r: Remote, *remote_cmd: str) -> list[str]`
  - `preflight(r: Remote, run=subprocess.run) -> tuple[bool, str]`: one ssh call that prints free GB under `workdir` and runs `docker info`; returns `(False, reason)` for no answer, disk short (reason names both numbers), or docker down.
  - `remote_load(r, run=subprocess.run) -> float | None` from `/proc/loadavg`.
- [ ] **Step 1: Write the failing self-test:** parse a sample ini (a remote and a `[repo app]` section) into the dataclasses with the default exclude; missing file gives `([], {})`; `TEST_PHASE_REMOTES=off` gives `([], {})`; `ssh_argv` contains `BatchMode=yes` and `StrictHostKeyChecking=yes`; `preflight` with a fake `run` returns the three failure reasons and `(True, "")` for a good answer.
- [ ] **Step 2:** Implement.
- [ ] **Step 3: Commit** `git commit -m "read remote hosts from remotes.ini and check one before a claim"`

### Task 3: copy, run command and cleanup

**Files:**
- Modify: `bin/lean_remote.py`

**Interfaces:**
- Consumes: `Remote`, `RepoCfg`, `ssh_argv` from Task 2.
- Produces:
  - `file_list(repo: str, extra_paths: list[str], exclude: list[str]) -> list[str]`: `git ls-files -co --exclude-standard` plus every file under each extra path, minus any path whose basename or full relative path matches an exclude glob (`fnmatch`). Sorted, relative.
  - `job_dir(r, job_id) -> str` = `<workdir>/job-<id>`
  - `sync_argv(r, repo, job_id) -> list[str]`: `rsync -a --files-from=- --from0 -e "<ssh with the options>" <repo>/ <host>:<job_dir>/`; the caller feeds `file_list` NUL-joined on stdin.
  - `send_env(r, job_id, env_file, run=subprocess.run) -> None`: `ssh host 'umask 077; cat > <job_dir>/.lean.env'` with the file as stdin.
  - `run_script(r, cfg: RepoCfg | None, job_id, cmd: list[str]) -> str`: one shell script for ssh that creates network `lean-<id>` with label `lean.job=<id>`, starts each sidecar (`--network-alias <alias>`, label, same env file, no `-p`), waits on `cfg.ready`, then `docker run --rm --label lean.job=<id> --network lean-<id> --cpus <cpus> --memory <memory> --user "$(id -u):$(id -g)" -e HOME=/tmp -e NUGET_PACKAGES=/nuget -v <workdir>/.nuget:/nuget -v <job_dir>:/work -w /work [--env-file <job_dir>/.lean.env] <image> sh -c 'echo "lean-remote: started"; exec nice -n 19 <cmd quoted>'`, and exits with that exit code. Ruling: a host folder instead of a named volume for the NuGet cache, so the files belong to the SSH user.
  - `cleanup_script(r, job_id) -> str`: removes containers and networks labelled `lean.job=<id>` and `rm -rf <job_dir>`; never fails.
  - `sweep_script(r, running_ids: list[int]) -> str`: the same for every `lean.job` label and `job-*` folder not in `running_ids`, plus `job-*` folders older than a day.
  - `MARKER = "lean-remote: started"`
- [ ] **Step 1: Write the failing self-test:** in a temp git repo with `a.cs`, `.env.local`, `secrets.json`, `local-packages/p.nupkg`, `local-packages/x.key`: `file_list(repo, ["local-packages"], DEFAULT_EXCLUDE)` is `["a.cs", "local-packages/p.nupkg"]`. `run_script` for job 7 contains `--label lean.job=7`, `--cpus 2`, `--memory 8g`, `--user`, `lean-remote: started`, no `-p `, and no value from the env file. `cleanup_script` names `lean.job=7` and `job-7`. `sweep_script(r, [3])` keeps 3.
- [ ] **Step 2:** Implement.
- [ ] **Step 3: Commit** `git commit -m "copy a job to a host and run it in a capped container"`

### Task 4: lanes in test-phase

**Files:**
- Modify: `bin/test-phase` (`heavy`, `record_run`, `run_job`, `drain`, `main`, docstring, `self_test`)

**Interfaces:**
- Consumes: brain verbs from Task 1, everything in `lean_remote` (import it from `HERE`).
- Produces:
  - `class Lane: name, where ("local"|"remote"), remote: Remote | None`
  - `run_job(job, a, run_dir, runner, lane)`: a remote lane, per command (the job's tier and each class rerun): `sync` once per job, `send_env` once if the repo has an env file, then `heavy(f"remote-{name}", ssh_argv(r, "sh", "-c", run_script(...)), ...)`. Output without `MARKER` is a lane problem: `row["lane_problem"] = reason`, no item filed. `record_run` passes `--host`.
  - `drain(a, run_dir, runner, lanes)`: one thread per lane. Each loop: remote lanes run `preflight` and sweep first; claim with `queue next --where <where> [--host name]`; nothing to claim ends that lane. A lane problem calls `queue requeue` with the reason, prints it once, and rests on a shared `threading.Event` for 600 s (`TEST_PHASE_REST` overrides, for tests); the event is set when every lane that can claim has ended, so a resting lane wakes and ends too. Cleanup script runs after every job, in a `finally`.
  - Stale running jobs at start: failed as today, and when the job has a host, that host's cleanup runs first.
  - Printing goes through one lock. The summary table gains a `host` column; exit code rules unchanged across lanes.
- [ ] **Step 1: Write the failing self-test cases**, with fake `ssh` (runs its last argument with `sh -c` locally), fake `rsync` (copies the `--files-from` list into the target folder) and fake `docker` (`run` executes the trailing `sh -c` in the `-w` folder mapped from `-v`; `network`, `rm`, `ps`, `info` succeed), and a `remotes.ini` in the temp dir pointing `workdir` at a temp folder:
  - two jobs queued, one local and one remote lane: both run, and the remote run's brain row has `host=dev`; the drain took less than the two jobs' summed sleep (each fake test sleeps 2 s).
  - an apple-platform job is run by the local lane only.
  - fake ssh exits 255 before printing the marker: the job is `queued` again, no `test-fail` item exists, the lane rests, and the local lane still drains the rest.
  - with the queue emptied by the local lane while the remote lane rests, the drainer exits within 10 s.
  - `.env.local` in the repo never appears in the host folder; the job folder and the fake docker's labelled objects are gone after a pass, a fail and a stall.
  - a failing class reruns on the remote lane and its run row also has `host=dev`.
  - `TEST_PHASE_REMOTES=off`: the existing cases pass with no ssh call logged.
- [ ] **Step 2:** Implement.
- [ ] **Step 3: Commit** `git commit -m "drain the queue on remote hosts alongside the local lane"`

### Task 5: docs and version

**Files:**
- Modify: `README.md` (queue paragraph), `.claude-plugin/plugin.json` (1.10.0), `bin/test-phase` docstring (already in Task 4)
- Create: `docs/remote-lane.md`: the config file with every key, the repo section, what is never copied, and the rollout note below.

- [ ] **Step 1:** Write the doc. Rollout note: the first new brain to open the database moves it to schema 7, after which any older brain refuses it, so stop the drainer and replace every brain copy (plugin cache and any worktree a drainer runs from) in one step.
- [ ] **Step 2: Commit** `git commit -m "document remote lanes and bump to 1.10.0"`

## Verification (the orchestrator runs this, not the coding agents)

`tests/run-all.sh` from the worktree: every self-test passes, the count goes up by one (lean_remote). Then one manual run against the real host, pasted into the PR body.

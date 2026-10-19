# Remote test lane

## Why

test-phase drains the queue one heavy run at a time on the machine it runs on. A full suite can hold the lane for an hour, and every small retest added after it waits that hour. A second machine reachable over SSH can run the next job at the same time. Results must land in brain exactly as they do today, so nobody reading `brain list`, `brain queue list` or the dashboard has to know where a run happened.

## Goals

- One drainer runs two or more lanes at once: the local lane as today, plus one per configured remote host. A free lane claims the next ready job with `brain queue next`, so bump, `--after` chains and dedupe keep working unchanged.
- A remote run copies the job's worktree to the host, runs the same test command there, and streams its output back into the same log file. Parsing, masking and filing into brain stay on the local machine.
- A remote host that also serves live traffic stays responsive: every remote run is capped in CPU and memory and runs at the lowest priority.
- Nothing changes for a user with no remote configured.

## Non-goals

- Provisioning or creating VMs. The host already exists and is reachable with `ssh <host>`.
- Running brain or the drainer on the remote host.
- Splitting one test project across machines.

## Anti-goals (a failure even if it works)

- A remote run that slows a live service on the host. The cap is required, not optional.
- Secrets leaving the local machine. Files matching the exclude list (by default `.env*`, `*.key`, `*.pem`, `*license-key*`, `secrets*`) are never copied, and no secret is ever passed on a command line.
- A broken SSH connection, a full disk or an unreachable host recorded as a test failure. Those are lane problems: the job goes back to `queued` and the lane rests.
- Leftover job folders, containers or networks on the host after a run, passed or failed.

## Configuration

A file at `~/.config/lean/remotes.ini`, one section per host:

```ini
[remote dev]
host = dev                  ; an ssh config Host name
workdir = ~/lean-runs
cpus = 2
memory = 8g
min_free_gb = 15
image = mcr.microsoft.com/dotnet/sdk:10.0
extra_paths = local-packages ; git-ignored folders the build needs, copied as well
exclude = .env*, *.key, *.pem, *license-key*, secrets*
```

`TEST_PHASE_REMOTES=off` turns every remote lane off without editing the file.

## How a remote run goes

1. **Claim.** Before claiming, the lane checks over SSH that the host answers, that free disk under `workdir` is at least `min_free_gb`, and that Docker is up. If a check fails it claims nothing, logs the reason once, and tries again after 10 minutes.
2. **Which jobs.** A job whose test project targets an Apple platform (`-maccatalyst`, `-ios`, `-macos` in its TargetFramework(s)) can only run locally. A remote lane must not claim one. `queue next` takes `--where local|remote` and skips jobs the caller cannot run (see the calls below).
3. **Copy.** `rsync` sends `git ls-files -co --exclude-standard` from the worktree plus `extra_paths`, minus `exclude`, to `workdir/job-<id>/`. Host keys are checked (`StrictHostKeyChecking=yes`, `BatchMode=yes`).
4. **Run.** On the host, in a fresh container from `image` with `--cpus`, `--memory` and `nice 19`, on a private Docker network named for the job. The NuGet package cache is a named volume shared across runs so packages are not downloaded every time. Services the tests need (for example a Postgres the tests connect to) run as sidecar containers on that network with no host ports, so they never collide with services already on the host. A repository declares its sidecars and settings in `remotes.ini` (see the calls below).
5. **Output.** The command's stdout and stderr stream back over SSH into the job's log, so `heavy.sh`'s stall watchdog and the existing parser work unchanged. The exit code is the test command's exit code.
6. **Clean up.** The job folder, the container, the sidecars and the network are removed in every outcome, including a stall stop and a dropped connection. A stale `job-*` folder older than a day is removed at the next claim. The shared package cache is pruned when it passes a size set in config.

## Recording

`runs` gets a `host` column (new migration; empty means local). `brain queue list` and the dashboard show where a running job is. Load at start and end is read from the host that ran the job.

## Failure handling

| What happened | Job | Lane |
| --- | --- | --- |
| Tests ran and some failed | failed, as today | keeps going |
| SSH or rsync failed, host down, disk short | back to `queued`, reason noted | rests 10 minutes |
| Stall watchdog stopped it | as today | keeps going, after cleanup |
| The drainer itself is stopped | failed at the next start, dependents cancelled, as today | the next start removes that job's folder and containers on the host first |

## Proof

Self-tests with a fake `ssh` and `rsync` (scripts on PATH that act on a local folder) show: two lanes run two jobs at once; a remote lane skips an Apple-platform job; a lost connection puts the job back to `queued` and is not filed as a test failure; excluded files are never copied; cleanup runs after a pass, a fail and a stall; `TEST_PHASE_REMOTES=off` gives today's behavior. One manual run against a real host goes in the PR body.

## Calls made after the builder check

These were open after the first draft. Each is the author's call, not something Arnel decided.

- **Locks.** Each remote lane takes its own `heavy.sh` lane named `remote-<name>`, so it never queues behind the local `dotnet` lane. `heavy.sh` wraps the `ssh` command, so the stall watchdog works unchanged. Low priority on the host comes from the container's `--cpus` cap and `nice` inside it.
- **Lane problem or test failure.** The remote wrapper prints one marker line, `lean-remote: started`, right before the test command. No marker in the output means the run never started: a lane problem, and the job goes back to `queued`. With the marker, the exit code is the test command's.
- **Stall stop and cleanup.** Every container, sidecar and network carries a `lean.job=<id>` label. Cleanup is `docker rm -f` and `docker network rm` by label over a second SSH connection. If that fails too, the next claim on that host sweeps everything labelled with a job that is no longer running.
- **Apple-only jobs.** A new `jobs.platform` column (same migration as `runs.host`) is set when the job is added, from the test project's TargetFramework(s): `apple` or empty. `queue next --where remote` skips `apple` jobs. The local lane claims anything, in the usual order.
- **Lanes.** One drainer process, one thread per lane, the one `drainer.lock` as today. A second `test-phase` still exits 0. The summary and exit code cover every lane.
- **Sidecars and settings.** A `[repo <name>]` section names sidecar images (for example `postgres:16`) and an env file on the local machine. The env file goes to the host over SSH stdin, sits in the job folder with mode 600, is handed to the container with `--env-file`, and is deleted at cleanup. Never on a command line.
- **Output.** stdout is enough; the parser reads the log text only. Paths in the output are not rewritten.
- **Excludes.** The exclude list applies to everything copied, `extra_paths` included.

## Left to the builder

The exact `remotes.ini` keys beyond those above, how the env file and sidecar names are spelled, and the internal shape of the lane threads.

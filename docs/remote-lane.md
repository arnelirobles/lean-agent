# Remote lanes

`test-phase` can run queued jobs on SSH hosts as well as on this machine. Each host is one more lane. A lane takes one job at a time, so with one remote host two jobs run at once: one local, one remote. Parsing, reruns, the stall watchdog and brain stay on the local machine. Only the test command runs on the host, inside a docker container with a CPU and memory cap.

## What a host needs

- ssh access with a key, no password prompt, and the host key already in `known_hosts` (`StrictHostKeyChecking=yes`, `BatchMode=yes`).
- `docker` usable by the ssh user, and `rsync`. The host does not need git.
- Free disk above `min_free_gb` in the workdir.

## Config

`~/.config/lean/remotes.ini`, or the file named by `TEST_PHASE_REMOTES_FILE`. `TEST_PHASE_REMOTES=off` turns every remote off. A bad file prints a warning and the drainer runs the local lane only.

```ini
[remote dev]
host = dev
workdir = ~/lean-runs
cpus = 2
memory = 8g
min_free_gb = 15
cache_max_gb = 10
image = mcr.microsoft.com/dotnet/sdk:10.0
extra_paths = local-packages
exclude = .env*, *.key, *.pem, *license-key*, secrets*

[repo app]
sidecars = db=postgres:16, cache=redis:7
env_file = ~/.config/lean/app.env
ready = pg_isready -h 127.0.0.1 -U postgres
```

Remote section keys:

| Key | Meaning | Default |
|---|---|---|
| `host` | An ssh config `Host` name. Required. | none |
| `workdir` | Folder on the host for job folders and the NuGet cache. | `~/lean-runs` |
| `cpus` | `--cpus` for every container of a job. | `2` |
| `memory` | `--memory` for every container of a job. | `8g` |
| `min_free_gb` | The lane does not claim a job when the workdir has less free space. | `15` |
| `cache_max_gb` | The shared NuGet cache (`<workdir>/.nuget`) is emptied when it is bigger and no job runs. `0` keeps it. | `10` |
| `reuse_build` | `yes` keeps one build folder per key and reuses it (see Build reuse). `no` copies and builds in a fresh folder for every job. | `yes` |
| `build_keep` | Build folders kept per repository. | `3` |
| `image` | The test container image. | `mcr.microsoft.com/dotnet/sdk:10.0` |
| `extra_paths` | Git-ignored paths to copy anyway, for example a local package folder. | none |
| `exclude` | Patterns never copied. A pattern matches a file name, a path, or any folder in the path. | the list above |

Repo section keys (the section name is the brain project name of the job):

| Key | Meaning |
|---|---|
| `sidecars` | `alias=image` pairs. Each starts on a private network for the job, reachable by its alias. |
| `env_file` | A local file of `NAME=value` lines. It is sent as `.lean.env` (mode 600) and loaded into the test container. It never appears on a command line. |
| `ready` | A command run in the first sidecar until it exits 0 twice in a row, one second apart, before the tests start. Use `-h 127.0.0.1` with `pg_isready`, because the postgres image's temporary init server answers only on the socket. |
| `docker` | `yes` starts a Docker daemon for the job (see below). Default `no`. |

Sidecars get the same `cpus` and `memory` caps as the test container, so a job with a sidecar can use up to twice the cap.

`docker = yes` gives the job its own Docker daemon, for tests that start containers themselves (Testcontainers). A `docker:27-dind` sidecar runs privileged on the job's network as `docker`, and the test container gets `DOCKER_HOST=tcp://docker:2375`, `TESTCONTAINERS_HOST_OVERRIDE=docker` and `TESTCONTAINERS_RYUK_DISABLED=true`. Containers the tests start live inside that daemon, so they never touch containers already on the host, and they go when the sidecar goes. Pulled images stay in the `lean-dind` volume between runs; images unused for a week are pruned at the start of a run.

The test command runs with `ASPNETCORE_HTTP_PORTS` and `ASPNETCORE_HTTPS_PORTS` unset. The sdk image sets the first, and `WebApplicationFactory` turns it into a `--HTTP_PORTS` argument for the app under test. A host that parses its own command line rejects it and exits before the test server starts.

## What is copied and what is not

The copy is the files `git ls-files` lists (tracked and untracked, not ignored) plus `extra_paths`, minus anything matching `exclude`. Files deleted in the working tree are skipped. Nothing else leaves this machine. Secrets go only through `env_file`.

## Build reuse

A remote job used to copy the repository into `job-<id>` and build it, which took minutes where the tests took seconds. With `reuse_build = yes` the host keeps `build-<repo>-<path hash>-<key>` folders instead. The key is a hash of the commit, the copied file list with each file's contents, the image and the build arguments, so a new commit, a dirty tree with a different edit, or other `args=` never match an earlier build.

The first job on a key copies the files and runs `dotnet build` for its test project in the container, then writes a mark file in the folder, and every `dotnet test` of the job runs with `--no-build`. A later job on the same key copies nothing, builds nothing and runs `dotnet test --no-build`. A second test project of the same key builds once itself. A failed build writes no mark, so the next job builds again.

A lock directory next to the folder (`<folder>.lock`) is held from the claim until the build ends. A second job on the same key waits for it (up to 30 minutes, then the job goes back to queued), so it never reads a half-built folder. A lock older than two hours is treated as left by a dead job. When a job is cleaned up, it releases a lock it still holds and deletes a folder with no finished build.

Each claim prunes: the newest `build_keep` folders of the repository stay (newest by last use), the rest go, except any with a lock. Build folders are not swept by the `job-*` rule below. `env_file` is sent again on every job. The local lane is unchanged.

## Which jobs go where

A job whose test project targets `-maccatalyst`, `-ios` or `-macos` is marked `platform=apple` and runs on the local lane only. Everything else can run on any lane. `brain queue add --platform apple|auto|none` overrides the detection.

To choose for a job already queued, use `brain queue route ID --to remote|local|any`. `remote` sends it to a remote lane only, so the local lane skips it even when idle. `local` pins it to this machine. `any` lets whichever lane is free take it. Jobs queued before the upgrade to schema 7 are all marked `platform=apple`, so route the ones you want on a remote host. Routing a job to `remote` whose project looks Apple-only prints a warning and still routes it. A job routed to `remote` with no remote lane up stays queued, and test-phase names it at the end of the drain.

## Lane problems

If ssh, the copy or docker fails before the test command starts, the job goes back to queued (`brain queue requeue`), no failure is filed, and that lane rests for `TEST_PHASE_REST` seconds (default 600). The local lane, or another host, picks the job up. The rest ends early once no lane is working. If the connection drops during a class rerun, the unfinished classes are marked unverified and the lane rests.

## Cleanup

Every container and network carries the label `lean.job=<id>`. A job removes its own after it ends, whatever the result. Before each claim a remote lane sweeps its host for labelled objects that belong to no running job, and for `job-*` folders older than a day. When the drainer finds a job left running by a dead drainer on a host, it cleans that host first and then fails the job with "drainer died".

## One drainer per host

Run one `test-phase` per host list. The sweep assumes every running job it sees in brain is its own. Two drainers sharing a host would remove each other's work.

## Reading the output

`brain queue list` shows `on=<host>` for a running remote job and `platform=apple` or `platform=remote` for routed jobs. Run rows carry the host. The summary table has a host column only when more than one lane exists.

## Rollout

The first new `brain` moves the database to schema 7 (`jobs.platform`, `jobs.host`, `runs.host`). Older brains then refuse the database. Stop the drainer, then replace every copy of `brain` and `test-phase` in one step, then start the drainer.

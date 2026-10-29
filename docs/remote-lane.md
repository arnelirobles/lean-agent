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
node_image = node:22-bookworm-slim
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
| `image` | The test container image for .NET jobs. | `mcr.microsoft.com/dotnet/sdk:10.0` |
| `node_image` | The test container image for `package.json` (Vitest) jobs. | `node:22-bookworm-slim` |
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

The copy is the files `git ls-files` lists (tracked and untracked, not ignored) plus `extra_paths`, minus anything matching `exclude`. A Vitest job also leaves out every `node_modules` folder, because the local binaries are for this machine, not the host. Files deleted in the working tree are skipped. Nothing else leaves this machine. Secrets go only through `env_file`.

## Vitest jobs

A job whose test path is a `package.json` (for example `brain queue add ~/app web/package.json`) runs Vitest, not `dotnet test`. In that folder it installs with the nearest lockfile (`pnpm install --frozen-lockfile`, through corepack when pnpm is not installed, or `npm ci` for a `package-lock.json`), then runs `vitest run --reporter=verbose` (`pnpm exec`, or `npx` for npm). The fast filter pieces are file filters, and a failed test file is rerun alone.

On a host it runs in `node_image`. The container sits on the job's bridge network, so it has outbound network for the install. pnpm's store and corepack's download are kept in `<workdir>/.node-cache`, which `cache_max_gb` also caps. The host needs no Node.

The lockfile is looked up from the package folder to the repo root, so a pnpm workspace with one root `pnpm-lock.yaml` installs frozen. The nearest lockfile wins: `pnpm-lock.yaml` means pnpm, `package-lock.json` means `npm ci` in the lockfile's folder (so an npm workspace root installs once for its packages). A project whose nearest lockfile is `yarn.lock` is refused with a `run-failed` item that names it, because only pnpm and npm are run. `LEAN_PNPM_VERSION` (default 10, used when pnpm is not installed and `package.json` has no `packageManager`) is passed to the container.

A job whose test path is a `playwright.config.ts` (or `.js`, `.mjs`, `.cjs`) runs Playwright from that folder: the same install, then `playwright test --reporter=list`. The fast filter pieces are file filters, and a failed test is named `file > title`, so a rerun is per spec file. The default `node_image` has no Playwright browsers, so on a host set `node_image` to an image that has them (for example `mcr.microsoft.com/playwright`) or run the job on the local lane.

Three things to know:

- A drainer from before the Vitest runner does not know `package.json` jobs. It runs one as `dotnet test` and fails it with a `run-failed` item. Replace every copy of `test-phase` before queueing one.
- The copy never sends `.npmrc`, `.yarnrc`, `.yarnrc.yml` or `.pnpmrc` (a token may be in them), and `exclude =` cannot bring them back. The host installs from the public registry only; a job that needs a private feed stays on the local lane.
- A cold first run downloads pnpm and installs with little output. If it trips `--stall` (default 600 seconds), raise `--stall` for that run.

## Build reuse

A remote job used to copy the repository into `job-<id>` and build it, which took minutes where the tests took seconds. With `reuse_build = yes` the host keeps `build-<repo>-<path hash>-<key>` folders instead. The key is a hash of the commit, the copied file list with each file's contents, the image and the build arguments, so a new commit, a dirty tree with a different edit, or other `args=` never match an earlier build.

The first job on a key copies the files and runs `dotnet build` for its test project in the container, then writes a mark file in the folder, and every `dotnet test` of the job runs with `--no-build`. A later job on the same key copies nothing, builds nothing and runs `dotnet test --no-build`. A second test project of the same key builds once itself. A failed build writes no mark, so the next job builds again.

A lock directory next to the folder (`<folder>.lock`) is held from the claim until the build ends. A second job on the same key waits for it (up to 30 minutes, then the job goes back to queued), so it never reads a half-built folder. A lock older than two hours is treated as left by a dead job. When a job is cleaned up, it releases a lock it still holds and deletes a folder with no finished build.

A job whose extra arguments include one only `dotnet test` takes (`--settings`, `--logger`, `-e`, `--results-directory`, ...) cannot share a build and uses a fresh `job-<id>` folder. Build-safe ones (`-c`, `-f`, `-r`, `-v`, `--arch`, `--os`, `-p:`, `--no-restore`) go to the build too. The env file is written to `<workdir>/env-<id>`, outside the kept folders, and removed with the job.

Limits: reuse assumes one drainer per host and workdir, because the prune and the lock are not safe across two drainers sharing them. A miss holds the lock through its test run, so a second job on that key waits for the tests too. Submodule contents are not in the key, so a dirty submodule under an unchanged pointer reuses the build. A workdir cannot contain whitespace. The sweep removes locks over two hours old, env files of no running job, and unlocked build folders unused for 7 days.

Each claim prunes: the newest `build_keep` folders of the repository stay (newest by last use), the rest go, except any with a lock. Build folders are not swept by the `job-*` rule below. `env_file` is sent again on every job. The local lane is unchanged.

## Which jobs go where

A job whose test project targets `-maccatalyst`, `-ios` or `-macos` is marked `platform=apple` and runs on the local lane only. An npm job (a `package.json` or Playwright config) whose `.npmrc`, in its folder or any folder up to the repo root, names a registry other than registry.npmjs.org is marked `platform=private` and also runs on the local lane only: its login lives in `~/.npmrc` on this machine, and the copy to a remote host never sends secrets. Everything else can run on any lane. `brain queue add --platform apple|auto|none` overrides the detection.

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

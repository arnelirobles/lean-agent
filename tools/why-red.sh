#!/usr/bin/env bash
# Names known environmental causes in a failed test or build log, before anyone debugs the code.
#
#   why-red.sh <log-file>        (or - for stdin)
#   why-red.sh --self-test
#
# Exit 1 when a known cause is found (and prints each one with the first line that matched), 0 when
# none is, 2 when the log is missing or empty.
#
# Why this exists. Each of these has turned a green change red and cost real debugging time,
# because the failure list looks like a regression: eight "unrelated" test failures that were the
# machine's inotify instance limit; a unit suite failing inside an HTTP library that was the wrong
# Node major; a container test run with no Docker; an image with no build for the machine's
# architecture. The log says so, in one line, somewhere in thousands. Read the exception before
# believing the failure list.

set -uo pipefail

scan_py=$(cat <<'PY'
import re, sys

CAUSES = [
    ("docker unavailable", "start Docker, or run the suite where Docker is available; these failures are not regressions",
     r"DockerUnavailableException|Cannot connect to the Docker daemon|docker\.sock: connect: (permission denied|no such file)"
     r"|Could not find a (valid|working) Docker environment|Docker is either not running|error during connect: .*docker"),
    ("port already in use", "another process (often another agent's server or test host) holds the port; pick a free port or stop the holder",
     r"EADDRINUSE|[Aa]ddress already in use|Failed to bind to address|port is already allocated|Only one usage of each socket address"),
    ("inotify limit", "the per-user inotify limit is exhausted (check /proc/sys/fs/inotify/max_user_instances and max_user_watches); run fewer test hosts at once or raise the limit",
     r"inotify instances has been reached|user limit \(\d+\) on the number of inotify|System limit for number of file watchers reached"
     r"|inotify_add_watch.*(No space left|ENOSPC)|too many open files.*inotify"),
    ("wrong architecture", "the image or binary has no build for this machine's architecture; build it here or pull a multi-arch tag",
     r"no matching manifest for|exec format error|image's platform \([^)]*\) does not match the detected host platform"
     r"|cannot execute binary file"),
    ("wrong Node version", "the active Node does not match what the project declares; run check-runtime.sh and switch Node before rerunning",
     r"EBADENGINE|Unsupported engine|The engine \"node\" is incompatible|markAsUncloneable is not a function"
     r"|requires (a )?Node(\.js)? (version )?(>=|v?\d)|You are using Node\.js [\d.]+\. .* requires Node\.js"),
    ("disk full", "free disk space (worktrees, build output and container images are the usual culprits) and rerun",
     r"(?i:no space left on device)|ENOSPC: no space left|[Dd]isk quota exceeded|There is not enough space on the disk"),
    ("DNS or network", "name resolution failed; check network and DNS on this machine rather than the code",
     r"Temporary failure in name resolution|getaddrinfo (ENOTFOUND|EAI_AGAIN)|Name or service not known|Could not resolve host"
     r"|No such host is known|dial tcp: lookup .*: (no such host|server misbehaving|i/o timeout)"),
]
compiled = [(n, h, re.compile(p)) for n, h, p in CAUSES]

src = sys.argv[1]
try:
    data = sys.stdin.buffer.read() if src == "-" else open(src, "rb").read()
except OSError as ex:
    print("why-red: cannot read %s: %s" % (src, ex.strerror))
    sys.exit(2)
lines = data.decode("utf-8", "replace").splitlines()
if not lines:
    print("why-red: %s is empty, so there is nothing to explain" % src)
    sys.exit(2)
found = []
for name, hint, rx in compiled:
    for i, line in enumerate(lines, 1):
        if rx.search(line):
            found.append((name, hint, i, line.strip()[:160]))
            break
for name, hint, i, line in found:
    print("why-red: %s (line %d): %s" % (name, i, line))
    print("         %s" % hint)
print("why-red: scanned %d lines, %d known environmental cause(s)" % (len(lines), len(found)))
sys.exit(1 if found else 0)
PY
)

self_test() {
  local fails=0
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  expect() { # $1 label, $2 want exit, $3 want cause text (or ""), $4 log line
    printf 'Starting test run\n%s\nFailed!  - Failed: 3\n' "$4" > "$t/log"
    local out rc; out=$(python3 -c "$scan_py" "$t/log"); rc=$?
    if [ "$rc" != "$2" ] || { [ -n "$3" ] && ! grep -q "why-red: $3 (line 2)" <<<"$out"; }; then
      echo "self-test failed: $1: exit $rc want $2, out: $out"; fails=$((fails + 1))
    fi
  }
  expect docker   1 "docker unavailable"  "DotNet.Testcontainers.Builders.DockerUnavailableException: Docker is either not running or misconfigured."
  expect port     1 "port already in use" "Error: listen EADDRINUSE: address already in use :::3000"
  expect inotify1 1 "inotify limit"       "System.IO.IOException: The configured user limit (128) on the number of inotify instances has been reached"
  expect inotify2 1 "inotify limit"       "Error: ENOSPC: System limit for number of file watchers reached, watch '/src'"
  expect arch     1 "wrong architecture"  "Error response from daemon: no matching manifest for linux/arm64/v8 in the manifest list entries"
  expect node     1 "wrong Node version"  "npm WARN EBADENGINE Unsupported engine { package: 'x', required: { node: '>=22.11.0' } }"
  expect undici   1 "wrong Node version"  "TypeError: webidl.util.markAsUncloneable is not a function"
  expect disk     1 "disk full"           "write /var/lib/docker/tmp/x: no space left on device"
  expect disk2    1 "disk full"           "IOException: No space left on device"
  expect dns      1 "DNS or network"      "getaddrinfo ENOTFOUND registry.npmjs.org"
  expect real     0 ""                    "Expected: 3, Actual: 4 at ContentTests.Create_returns_201"
  : > "$t/empty"
  python3 -c "$scan_py" "$t/empty" >/dev/null; [ $? = 2 ] || { echo "self-test failed: empty log not exit 2"; fails=$((fails + 1)); }
  python3 -c "$scan_py" "$t/missing" >/dev/null; [ $? = 2 ] || { echo "self-test failed: missing log not exit 2"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

case "${1:-}" in
  --self-test) self_test; exit $? ;;
  "") echo "usage: why-red.sh <log-file|->" >&2; exit 2 ;;
esac
python3 -c "$scan_py" "$1"

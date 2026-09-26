#!/usr/bin/env bash
# Confirms a deployment is running one specific build, by commit sha rather than version string.
#
#   check-deployed-sha.sh <url> <expected-sha> [--field a.b] [--header Name]
#   check-deployed-sha.sh --self-test
#
# <url> is a version or build endpoint. The sha is read, in order, from: the --header given; the
# --field given (a dotted path into a JSON body); common headers (X-Commit-Sha, X-Git-Sha,
# X-Revision, X-Build-Sha, X-Version-Sha); common JSON fields (sha, commit, commitSha, gitSha,
# git_sha, revision, build.sha, build.commit, git.commit, version.commit). A short sha (7 or more
# characters) matches a full one it is a prefix of, in either direction.
#
# Why this exists. A release used to prove its deploy by getting a 200 and reading a version string
# back. Neither tells builds apart: the previous build also returns 200, and every build of 1.4.0
# says 1.4.0. A deploy that pulled nothing and restarted nothing passed both. The body is parsed,
# not grepped, because a 404 page or a login redirect that happens to contain the sha would satisfy
# a substring match.
#
# Retries: ATTEMPTS (default 20) times, SLEEP (default 6) seconds apart, each request capped at 10
# seconds, so the whole wait has a deadline. An empty, missing or unparseable sha fails: not knowing
# what is deployed is not evidence that the right thing is.

set -uo pipefail

extract_py=$(cat <<'PY'
import json, sys
headers_file, body_file, field, header = sys.argv[1:5]
HEADERS = ["x-commit-sha", "x-git-sha", "x-revision", "x-build-sha", "x-version-sha"]
FIELDS = ["sha", "commit", "commitSha", "gitSha", "git_sha", "revision", "build.sha", "build.commit", "git.commit", "version.commit"]
hdrs = {}
for line in open(headers_file, encoding="utf-8", errors="replace"):
    if ":" in line and not line.startswith("HTTP/"):
        k, v = line.split(":", 1)
        hdrs[k.strip().lower()] = v.strip()
def dig(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc if isinstance(doc, str) else None
for h in ([header.lower()] if header else []) + ([] if field else HEADERS):
    if hdrs.get(h):
        print(hdrs[h]); sys.exit(0)
try:
    doc = json.load(open(body_file, encoding="utf-8", errors="replace"))
except Exception:
    doc = None
for f in ([field] if field else FIELDS):
    v = dig(doc, f)
    if v:
        print(v.strip()); sys.exit(0)
print("")
PY
)

matches() { # $1 actual, $2 expected
  local a=${1,,} e=${2,,}
  [ -n "$a" ] && [ -n "$e" ] || return 1
  [ "$a" = "$e" ] && return 0
  [ ${#a} -ge 7 ] && [ ${#e} -ge 7 ] || return 1
  case "$a" in "$e"*) return 0 ;; esac
  case "$e" in "$a"*) return 0 ;; esac
  return 1
}

self_test() {
  local fails=0 got
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  x() { python3 -c "$extract_py" "$t/h" "$t/b" "${1:-}" "${2:-}"; }
  want() { got=$(x "${@:3}"); [ "$got" = "$2" ] || { echo "self-test failed: $1: got '$got' want '$2'"; fails=$((fails + 1)); }; }
  printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n' > "$t/h"
  echo '{"sha":"abc1234def5678","version":"1.4.0"}' > "$t/b";                 want "top-level sha"        abc1234def5678
  echo '{"version":"1.4.0","build":{"commit":"f00dcafe99"}}' > "$t/b";        want "nested build.commit"  f00dcafe99
  echo '{"meta":{"rev":"0123456789"}}' > "$t/b";                              want "--field path"         0123456789 meta.rev
  echo '{"version":"1.4.0"}' > "$t/b";                                         want "version string only"  ""
  echo '<html>Not found abc1234def5678</html>' > "$t/b";                       want "html page with sha"   ""
  printf 'HTTP/1.1 200 OK\r\nX-Revision: 5555555aaaa\r\n' > "$t/h";            want "common header"        5555555aaaa
  printf 'HTTP/1.1 200 OK\r\nX-Deployed: 7777777bbbb\r\n' > "$t/h";            want "--header name"        7777777bbbb "" X-Deployed
  matches abc1234 abc1234def5678 || { echo "self-test failed: short prefix did not match"; fails=$((fails + 1)); }
  matches ABC1234DEF abc1234def || { echo "self-test failed: case differs"; fails=$((fails + 1)); }
  matches abc abcdef0123 && { echo "self-test failed: a 3 character prefix matched"; fails=$((fails + 1)); }
  matches abc1234 abc9999999 && { echo "self-test failed: a different sha matched"; fails=$((fails + 1)); }
  matches "" abc1234 && { echo "self-test failed: an empty sha matched"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

[ "${1:-}" = "--self-test" ] && { self_test; exit $?; }

url=${1:-}; expected=${2:-}
[ -n "$url" ] && [ -n "$expected" ] || { echo "usage: check-deployed-sha.sh <url> <expected-sha> [--field a.b] [--header Name]" >&2; exit 2; }
shift 2
field=""; header=""
while [ $# -gt 0 ]; do
  case "$1" in
    --field) field=${2:-}; shift 2 ;;
    --header) header=${2:-}; shift 2 ;;
    *) echo "check-deployed-sha: unknown argument $1" >&2; exit 2 ;;
  esac
done
attempts=${ATTEMPTS:-20}; pause=${SLEEP:-6}
w=$(mktemp -d); trap 'rm -rf "$w"' EXIT

echo "check-deployed-sha: expecting $expected from $url"
actual=""
for i in $(seq 1 "$attempts"); do
  : > "$w/h"; : > "$w/b"
  code=$(curl -s -o "$w/b" -D "$w/h" -w '%{http_code}' --max-time 10 "$url" 2>/dev/null) || code="error"
  actual=$(python3 -c "$extract_py" "$w/h" "$w/b" "$field" "$header")
  if matches "$actual" "$expected"; then
    echo "check-deployed-sha: deployed build is $actual (attempt $i)"
    exit 0
  fi
  echo "  attempt $i: HTTP $code, sha '${actual:-none found}'"
  [ "$i" -lt "$attempts" ] && sleep "$pause"
done
echo "check-deployed-sha: the deployment is not running $expected (last reported: ${actual:-no sha found})."
echo "  No sha at all means the endpoint does not report one, or the image was built without it."
echo "  Either way this deploy has not been proven."
exit 1

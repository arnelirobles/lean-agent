#!/usr/bin/env bash
# Fails when a test run executed zero tests, or fewer than --min N, reading the runner's own
# machine output rather than its console text.
#
#   assert-ran.sh [--min N] <report> [<report> ...]
#   assert-ran.sh --self-test
#
# Reports it reads, by content rather than extension:
#   vitest or jest  --reporter=json / --json --outputFile   numTotalTests minus pending and todo
#   dotnet test     --logger trx                             <Counters executed="N">
#   go test -json   (NDJSON)                                 pass and fail events that name a Test
#   pytest --junitxml, and any JUnit XML                     tests minus skipped, summed over suites
#
# Why this exists. A filter that matches nothing, a renamed test class, or a runner pointed at the
# wrong directory all exit 0 with a green summary. The console says "0 passed" in a line nobody
# reads, and the gate that ran it reports success having checked nothing. The count is in the
# report, so read it there.
#
# Several reports are summed, then compared with --min (default 1). An unreadable, empty or
# unrecognised report fails: a gate that cannot read its evidence has not passed.

set -uo pipefail

count_py=$(cat <<'PY'
import json, sys
import xml.etree.ElementTree as ET

def local(tag):
    return tag.rsplit("}", 1)[-1]

def count(path):
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", "replace").lstrip("﻿").strip()
    if not text:
        raise ValueError("empty file")
    if text.startswith("<"):
        root = ET.fromstring(raw)
        if local(root.tag) == "TestRun":
            for el in root.iter():
                if local(el.tag) == "Counters":
                    return int(el.get("executed", "0")), "trx"
            raise ValueError("trx without Counters")
        suites = [root] if local(root.tag) == "testsuite" else [s for s in root if local(s.tag) == "testsuite"]
        if local(root.tag) not in ("testsuite", "testsuites"):
            raise ValueError("XML root <%s> is not trx or JUnit" % local(root.tag))
        if not suites and root.get("tests") is not None:
            suites = [root]
        n = 0
        for s in suites:
            tests = int(s.get("tests", "0"))
            skipped = int(s.get("skipped", s.get("skips", "0")) or 0) + int(s.get("disabled", "0") or 0)
            n += tests - skipped
        return n, "junit"
    if text.startswith("{") and "\n" not in text.split("}", 1)[0] and '"Action"' in text.splitlines()[0]:
        n = 0
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            ev = json.loads(line)
            if ev.get("Test") and ev.get("Action") in ("pass", "fail"):
                n += 1
        return n, "go test -json"
    doc = json.loads(text)
    if isinstance(doc, dict) and "numTotalTests" in doc:
        n = doc["numTotalTests"] - doc.get("numPendingTests", 0) - doc.get("numTodoTests", 0)
        return n, "jest/vitest json"
    raise ValueError("unrecognised report format")

args = sys.argv[1:]
minimum = 1
files = []
i = 0
while i < len(args):
    if args[i] == "--min":
        minimum = int(args[i + 1]); i += 2; continue
    files.append(args[i]); i += 1
if not files:
    print("assert-ran: no report given, so there is nothing to count", file=sys.stderr)
    sys.exit(2)
total = 0
bad = False
for f in files:
    try:
        n, kind = count(f)
    except Exception as ex:
        print("assert-ran: %s: could not read a test count (%s)" % (f, ex))
        bad = True
        continue
    print("assert-ran: %s: %d executed (%s)" % (f, n, kind))
    total += n
if bad:
    sys.exit(1)
if total < minimum:
    print("assert-ran: %d tests executed across %d report(s), want at least %d. A filter or path matched nothing." % (total, len(files), minimum))
    sys.exit(1)
print("assert-ran: %d tests executed across %d report(s), at least %d required" % (total, len(files), minimum))
PY
)

self_test() {
  local fails=0
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  run() { python3 -c "$count_py" "$@" >/dev/null 2>&1; echo $?; }
  want() { local got; got=$(run "${@:3}"); [ "$got" = "$2" ] || { echo "self-test failed: $1: exit $got, want $2"; fails=$((fails + 1)); }; }

  echo '{"numTotalTests":5,"numPendingTests":1,"numTodoTests":0,"success":true}' > "$t/v.json"
  echo '{"numTotalTests":2,"numPendingTests":2,"success":true}' > "$t/v0.json"
  cat > "$t/r.trx" <<'X'
<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary outcome="Completed"><Counters total="3" executed="3" passed="3" failed="0"/></ResultSummary></TestRun>
X
  cat > "$t/r0.trx" <<'X'
<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary outcome="Completed"><Counters total="0" executed="0" passed="0" failed="0"/></ResultSummary></TestRun>
X
  printf '%s\n' '{"Action":"run","Package":"p","Test":"TestA"}' '{"Action":"pass","Package":"p","Test":"TestA"}' \
    '{"Action":"output","Package":"p","Output":"ok"}' '{"Action":"pass","Package":"p"}' > "$t/go.json"
  printf '%s\n' '{"Action":"start","Package":"p"}' '{"Action":"output","Package":"p","Output":"testing: warning: no tests to run"}' '{"Action":"pass","Package":"p"}' > "$t/go0.json"
  echo '<testsuites><testsuite name="a" tests="4" skipped="1"/><testsuite name="b" tests="2" skipped="0"/></testsuites>' > "$t/j.xml"
  echo '<testsuite name="pytest" tests="3" skipped="3" errors="0" failures="0"/>' > "$t/j0.xml"
  : > "$t/empty.json"
  echo 'Build FAILED' > "$t/garbage.txt"

  want "vitest 4 executed"      0 "$t/v.json"
  want "vitest all skipped"     1 "$t/v0.json"
  want "trx 3 executed"         0 "$t/r.trx"
  want "trx zero"               1 "$t/r0.trx"
  want "go test one test"       0 "$t/go.json"
  want "go test no tests"       1 "$t/go0.json"
  want "junit 5 executed"       0 "$t/j.xml"
  want "junit all skipped"      1 "$t/j0.xml"
  want "min above count"        1 --min 6 "$t/j.xml"
  want "min met by the sum"     0 --min 8 "$t/j.xml" "$t/r.trx"
  want "empty report"           1 "$t/empty.json"
  want "garbage report"         1 "$t/garbage.txt"
  want "one bad report of two"  1 "$t/v.json" "$t/garbage.txt"
  want "missing file"           1 "$t/nope.json"
  want "no report given"        2
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
python3 -c "$count_py" "$@"

#!/usr/bin/env bash
# Compares the runtimes a repository declares with the binaries on PATH, and exits 1 on a mismatch.
#
#   check-runtime.sh [dir]        (defaults to the repository root, else the current directory)
#   check-runtime.sh --self-test
#
# Reads: package.json engines.node (ranges like >=22.11.0, ^20, ~18.2, 20.x, a || b), .nvmrc and
# .node-version (a version or major; lts/* and aliases are reported and skipped), global.json
# sdk.version with rollForward, and go.mod's go and toolchain lines.
#
# Why this exists. A machine's default Node was 20 while every project on it needed 22. A unit
# suite failed inside an HTTP library with "markAsUncloneable is not a function", which reads like
# a broken test and was a wrong Node. The declaration was in package.json the whole time. Run this
# before trusting a red test run, and before a build.
#
# A declaration with no binary to satisfy it is a mismatch. No declarations at all is a normal
# answer and exits 0, after saying it found none.

set -uo pipefail

check_py=$(cat <<'PY'
import json, os, re, subprocess, sys

root = sys.argv[1]
problems, ok, skipped = [], [], []

def ver(s):
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", s or "")
    return tuple(int(x) if x else 0 for x in m.groups()) if m else None

def active(cmd, env=None):
    try:
        e = dict(os.environ); e.update(env or {})
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=root, env=e, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, "not found on PATH"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return None, msg[-1] if msg else "exit %d" % r.returncode
    return r.stdout.strip(), None

def satisfies(v, rng):
    """npm-style range subset: comparators, ^, ~, x-ranges, hyphen-free. True, False, or None if unparsed."""
    for alt in rng.split("||"):
        parts = alt.replace(">= ", ">=").replace("<= ", "<=").replace("> ", ">").replace("< ", "<").split()
        good = True
        if not parts:
            return True
        for p in parts:
            m = re.fullmatch(r"(\^|~|>=|<=|>|<|=)?v?(\d+|x|\*)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", p)
            if not m:
                return None
            op, *nums = m.groups()
            given = [n for n in nums if n is not None and n not in ("x", "*")]
            want = tuple(int(n) for n in given) + (0,) * (3 - len(given))
            n = len(given)
            if op in (None, "="):
                if tuple(v[:n]) != tuple(want[:n]):
                    good = False
            elif op == ">=":
                good &= v >= want
            elif op == ">":
                good &= v > want if n == 3 else tuple(v[:n]) > tuple(want[:n])
            elif op == "<=":
                good &= v <= want if n == 3 else tuple(v[:n]) <= tuple(want[:n])
            elif op == "<":
                good &= v < want
            elif op == "^":
                lead = next((i for i, x in enumerate(want) if x), 0) if n else 0
                lead = min(lead, max(n - 1, 0))
                good &= v >= want and tuple(v[:lead + 1]) == tuple(want[:lead + 1])
            elif op == "~":
                keep = 2 if n >= 2 else 1
                good &= v >= want and tuple(v[:keep]) == tuple(want[:keep])
        if good:
            return True
    return False

node_decls = []
pj = os.path.join(root, "package.json")
if os.path.isfile(pj):
    try:
        eng = (json.load(open(pj)).get("engines") or {}).get("node")
        if eng:
            node_decls.append(("package.json engines.node", eng.strip()))
    except ValueError as ex:
        problems.append("package.json: cannot parse (%s)" % ex)
for f in (".nvmrc", ".node-version"):
    p = os.path.join(root, f)
    if os.path.isfile(p):
        lines = open(p).read().split()
        v = lines[0] if lines else ""
        if re.fullmatch(r"v?\d+(\.\d+){0,2}", v):
            node_decls.append((f, v.lstrip("v")))
        elif v:
            skipped.append("%s says %r, an alias this check does not resolve" % (f, v))
if node_decls:
    out, err = active(["node", "--version"])
    for src, want in node_decls:
        if out is None:
            problems.append("%s wants node %s, and node is %s" % (src, want, err))
            continue
        v = ver(out)
        res = satisfies(v, want) if src.startswith("package.json") else tuple(v[:len(want.split("."))]) == ver(want)[:len(want.split("."))]
        if res is None:
            skipped.append("%s range %r is not one this check parses" % (src, want))
        elif res:
            ok.append("%s %s, active node %s" % (src, want, out))
        else:
            problems.append("%s wants node %s, active node is %s" % (src, want, out))

gj = os.path.join(root, "global.json")
if os.path.isfile(gj):
    try:
        sdk = json.load(open(gj)).get("sdk") or {}
    except ValueError as ex:
        sdk = {}
        problems.append("global.json: cannot parse (%s)" % ex)
    want = sdk.get("version")
    if want:
        roll = sdk.get("rollForward", "patch")
        out, err = active(["dotnet", "--version"])
        if out is None:
            problems.append("global.json wants .NET SDK %s (rollForward %s), and dotnet says: %s" % (want, roll, err))
        else:
            v, w = ver(out), ver(want)
            band = lambda x: (x[0], x[1], x[2] // 100)
            rules = {
                "disable": v == w, "patch": band(v) == band(w) and v >= w, "latestPatch": band(v) == band(w) and v >= w,
                "feature": v[:2] == w[:2] and v >= w, "latestFeature": v[:2] == w[:2] and v >= w,
                "minor": v[0] == w[0] and v >= w, "latestMinor": v[0] == w[0] and v >= w,
                "major": v >= w, "latestMajor": v >= w,
            }
            if roll not in rules:
                skipped.append("global.json rollForward %r is not one this check knows" % roll)
            elif rules[roll]:
                ok.append("global.json sdk %s (rollForward %s), active dotnet %s" % (want, roll, out))
            else:
                problems.append("global.json wants .NET SDK %s (rollForward %s), active dotnet is %s" % (want, roll, out))

gm = os.path.join(root, "go.mod")
if os.path.isfile(gm):
    text = open(gm).read()
    gov = re.search(r"^go\s+(\S+)", text, re.M)
    tc = re.search(r"^toolchain\s+go(\S+)", text, re.M)
    if gov or tc:
        out, err = active(["go", "env", "GOVERSION"], {"GOTOOLCHAIN": "local"})
        if out is None:
            problems.append("go.mod declares go %s, and go is %s" % ((tc or gov).group(1), err))
        else:
            v = ver(out)
            if gov and v < ver(gov.group(1)):
                problems.append("go.mod wants go >= %s, local go is %s" % (gov.group(1), out))
            elif gov:
                ok.append("go.mod go %s, local %s" % (gov.group(1), out))
            if tc and v < ver(tc.group(1)):
                problems.append("go.mod toolchain go%s is newer than local %s (go will try to download it unless GOTOOLCHAIN=local)" % (tc.group(1), out))
            elif tc:
                ok.append("go.mod toolchain go%s, local %s" % (tc.group(1), out))

for line in ok:
    print("  ok        " + line)
for line in skipped:
    print("  skipped   " + line)
for line in problems:
    print("  MISMATCH  " + line)
n = len(ok) + len(problems) + len(skipped)
if n == 0:
    print("check-runtime: no runtime declarations found in %s" % root)
else:
    print("check-runtime: %d declaration(s) checked, %d mismatch(es)" % (n, len(problems)))
sys.exit(1 if problems else 0)
PY
)

self_test() {
  local fails=0 out rc
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  mkdir -p "$t/bin" "$t/p"
  stub() { printf '#!/bin/sh\necho "%s"\n' "$2" > "$t/bin/$1"; chmod +x "$t/bin/$1"; }
  local py; py=$(command -v python3)
  run() { (PATH="$t/bin" "$py" -c "$check_py" "$t/p"); }
  expect() { out=$(run); rc=$?; { [ "$rc" = "$2" ] && grep -qE "$3" <<<"$out"; } || { echo "self-test failed: $1: exit $rc want $2: $out"; fails=$((fails + 1)); }; }
  expect "nothing declared" 0 'no runtime declarations'
  stub node v20.18.0
  echo '{"engines":{"node":">=22.11.0"}}' > "$t/p/package.json"
  expect "node too old for engines" 1 'MISMATCH  package.json engines.node wants node >=22.11.0, active node is v20.18.0'
  stub node v22.12.0
  expect "node satisfies engines" 0 'ok        package.json'
  for pair in '^22:0' '^20:1' '~22.12:0' '~22.11:1' '22.x:0' '18 || 22:0' '>=18 <21:1'; do
    echo "{\"engines\":{\"node\":\"${pair%:*}\"}}" > "$t/p/package.json"
    out=$(run); [ $? = "${pair##*:}" ] || { echo "self-test failed: range '${pair%:*}' against 22.12.0: $out"; fails=$((fails + 1)); }
  done
  rm "$t/p/package.json"; echo 20 > "$t/p/.nvmrc"
  expect ".nvmrc major differs" 1 'MISMATCH  .nvmrc wants node 20'
  echo lts/iron > "$t/p/.nvmrc"
  expect ".nvmrc alias skipped" 0 'skipped   .nvmrc'
  rm "$t/p/.nvmrc"; rm "$t/bin/node"; echo 22 > "$t/p/.node-version"
  expect "declared, no binary" 1 'MISMATCH  .node-version wants node 22, and node is not found'
  rm "$t/p/.node-version"
  echo '{"sdk":{"version":"8.0.100"}}' > "$t/p/global.json"; stub dotnet 8.0.204
  expect "sdk other feature band" 1 'MISMATCH  global.json'
  echo '{"sdk":{"version":"8.0.100","rollForward":"latestFeature"}}' > "$t/p/global.json"
  expect "sdk roll forward feature" 0 'ok        global.json'
  rm "$t/p/global.json"; printf 'module x\n\ngo 1.22\n\ntoolchain go1.23.1\n' > "$t/p/go.mod"; stub go go1.22.5
  expect "go toolchain newer than local" 1 'MISMATCH  go.mod toolchain go1.23.1'
  stub go go1.23.4
  expect "go satisfied" 0 'ok        go.mod toolchain'
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

[ "${1:-}" = "--self-test" ] && { self_test; exit $?; }
dir=${1:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
[ -d "$dir" ] || { echo "check-runtime: $dir is not a directory" >&2; exit 2; }
python3 -c "$check_py" "$(cd "$dir" && pwd)"

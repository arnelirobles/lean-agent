#!/usr/bin/env bash
# Move a Node package to a new version, including the two places in the lock file that belong to it
# and none of the places that do not.
#
# Three version bumps in one day went wrong in three different ways, so this exists instead of care:
#
#   1. package.json moved and package-lock.json did not, so the published metadata claimed two
#      different versions. A review bot caught it on the release pull request.
#   2. Regenerating the lock with `npm install --package-lock-only` fixed the version and silently
#      dropped the `libc` fields on ten optional platform packages, because the lock had been
#      written by a newer npm than the one on that machine. Those fields are what select musl over
#      glibc builds inside an image.
#   3. A lock file held four occurrences of the old version string and two of them belonged to
#      unrelated dependencies that happened to sit at the same number. A blind replace would have
#      rewritten two dependency versions in a published lock file.
#
# So: edit exactly two fields, by path rather than by pattern, and prove the result parses and that
# nothing else moved.
#
# Usage:  bump-version.sh <new-version> [path-to-package-dir]
#
# It does not touch a changelog, a tag or a csproj. Those differ per repository and are worth doing
# with your eyes open. This closes the one that kept going wrong quietly.

set -euo pipefail

NEW=${1:-}
DIR=${2:-.}

if [ -z "$NEW" ]; then
    echo "usage: bump-version.sh <new-version> [dir]" >&2
    exit 2
fi
case "$NEW" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "bump-version: '$NEW' does not look like a version" >&2; exit 2;;
esac

cd "$DIR"
[ -f package.json ] || { echo "bump-version: no package.json in $DIR" >&2; exit 2; }

OLD=$(python3 -c "import json;print(json.load(open('package.json'))['version'])")
if [ "$OLD" = "$NEW" ]; then
    echo "bump-version: already at $NEW, nothing to do"
    exit 0
fi
echo "bump-version: $OLD -> $NEW"

python3 - "$NEW" <<'PY'
import io, json, sys, re

new = sys.argv[1]

# package.json, by path. json.dump would reformat the whole file, so the one line is edited in place
# and the result is parsed to prove it is still valid.
src = io.open('package.json', encoding='utf-8').read()
old = json.loads(src)['version']
out, n = re.subn(r'("version"\s*:\s*)"%s"' % re.escape(old), r'\1"%s"' % new, src, count=1)
if n != 1:
    raise SystemExit('bump-version: could not find the version field in package.json')
json.loads(out)
io.open('package.json', 'w', encoding='utf-8').write(out)

try:
    lock_src = io.open('package-lock.json', encoding='utf-8').read()
except FileNotFoundError:
    print('  package.json updated; no lock file present')
    raise SystemExit(0)

lock = json.loads(lock_src)
targets = []
if lock.get('version') == old:
    targets.append('root')
if lock.get('packages', {}).get('', {}).get('version') == old:
    targets.append('packages[""]')
if not targets:
    raise SystemExit('bump-version: lock file does not carry %s; check it by hand' % old)

# Count every occurrence first, so a lock where a dependency shares the version is visible rather
# than quietly half-edited.
total = len(re.findall(r'"version"\s*:\s*"%s"' % re.escape(old), lock_src))
lines = lock_src.split('\n')
pat = re.compile(r'^(\s*)"version"(\s*:\s*)"%s"' % re.escape(old))

changed = 0
depth = 0
in_packages = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('"packages"'):
        in_packages = True
    # The two fields that belong to this package sit before any node_modules entry appears.
    if '"node_modules/' in line:
        break
    m = pat.match(line)
    if m and changed < len(targets):
        lines[i] = pat.sub(lambda mm: '%s"version"%s"%s"' % (mm.group(1), mm.group(2), new), line)
        changed += 1

if changed != len(targets):
    raise SystemExit('bump-version: expected to change %d field(s), changed %d' % (len(targets), changed))

out_lock = '\n'.join(lines)
parsed = json.loads(out_lock)
assert parsed.get('version', new) == new
assert parsed.get('packages', {}).get('', {}).get('version', new) == new
io.open('package-lock.json', 'w', encoding='utf-8').write(out_lock)

others = total - changed
print('  package.json and %d lock field(s) updated' % changed)
if others:
    print('  %d other occurrence(s) of %s left alone, they belong to dependencies' % (others, old))
PY

echo "bump-version: done. The changelog, the tag and any project files are still yours to move."

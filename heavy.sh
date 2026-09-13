#!/usr/bin/env bash
# Runs a heavy command in a lane that every agent on the machine shares, one command per lane.
#
#   bash heavy.sh <lane> <command> [args...]
#
#   bash heavy.sh dotnet dotnet build MySolution.sln -nodeReuse:false
#   bash heavy.sh node   npm ci
#   bash heavy.sh node   npx playwright test e2e/login.spec.ts
#
# Why this exists. Thirteen agents on a three core machine each ran a reasonable brief, and five of
# them compiled the same solution at once. Load average reached 44 on a box that also serves live
# sites. Reading and writing code is cheap; compiling, installing packages, starting containers and
# driving browsers is what saturates a machine. So the agents all keep running and only those
# commands queue.
#
# A lane is just a name. Use one per toolchain, so a .NET build and an npm install can run side by
# side but two builds cannot. Everything runs at the lowest CPU priority, so a live service on the
# same machine wins every contest.
#
# HEAVY_LOCK_DIR sets where the lock files live. Every agent has to agree on it, which is the point
# of the default being one fixed path rather than a per-session temp dir.
#
# Given no command it fails rather than taking the lock and exiting 0. A queue step that succeeds
# having run nothing is the failure section 10 is about.

set -uo pipefail

if [ $# -lt 2 ]; then
  echo "usage: heavy.sh <lane> <command> [args...]" >&2
  echo "no command given, so nothing would run and this would report success." >&2
  exit 2
fi

lane=$1
shift

case "$lane" in
  *[!A-Za-z0-9._-]*) echo "lane '$lane' must be letters, digits, dot, dash or underscore" >&2; exit 2 ;;
esac

dir=${HEAVY_LOCK_DIR:-/tmp/heavy-locks}
mkdir -p "$dir"
lock="$dir/$lane.lock"

if ! flock -n "$lock" true 2>/dev/null; then
  echo "heavy.sh: lane '$lane' is busy, waiting" >&2
fi

# -o closes the lock before the command runs. Without it every child the command starts inherits
# the lock, and a child that outlives the build keeps it. The Roslyn compiler server does exactly
# that: it stays up for minutes after `dotnet build` exits, so the lane stayed locked with nothing
# building and every other agent waited on it. The lock still covers the whole command, because
# flock keeps its own copy until the command exits.
exec flock -o "$lock" nice -n 19 "$@"

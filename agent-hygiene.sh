#!/usr/bin/env bash
# What parallel agents leave behind. Run it between batches, not once a month.
#
# Every check here exists because the thing it looks for actually happened, and in each case nothing
# announced itself. That is the common thread: a stuck wait and a working one look identical from
# outside, an orphaned server answers requests normally, and a worktree on a merged branch looks
# exactly like one on live work. None of it appears in a task list, a test run or CI.
#
# In one day of four to six agents running in parallel, this found: twelve shells polling forever on
# a pattern that matched their own command line, three more waiting on work that had been overtaken,
# four web servers orphaned for nine days by sessions that ended, two agents on the same fixed test
# port driving each other's code, two agents publishing each other's pull request bodies, and about
# sixty five gigabytes of worktrees on branches that had all merged.
#
# Reports only. It never kills, never deletes, and always exits 0. Read it and decide.
#
# Usage:  agent-hygiene.sh [repo-path ...]     (defaults to the current repository)

set -uo pipefail
found=0
note() { found=1; printf '\n%s\n' "$1"; }

echo "== agent hygiene =="

# 1. Wait loops. A loop whose condition stopped being reachable polls until something kills it.
# Container workloads are excluded by parent: a poller inside a container is its job, and a check
# that reports it every run teaches people to skip the output.
long_shells=""
while read -r pid etimes rest; do
    [ -z "${pid:-}" ] && continue
    case "$pid" in ''|*[!0-9]*) continue;; esac
    [ "$etimes" -gt 900 ] 2>/dev/null || continue
    ppcomm=$(ps -o comm= -p "$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" 2>/dev/null)
    case "$ppcomm" in containerd*|dockerd|conmon|runc) continue;; esac
    long_shells="$long_shells  $pid  $((etimes/60))m  $(printf '%s' "$rest" | cut -c1-110)
"
done < <(ps -eo pid,etimes,args 2>/dev/null \
  | grep -E 'until |while .*sleep |wait-on|pgrep -f' \
  | grep -v 'agent-hygiene' | grep -v grep || true)
if [ -n "$long_shells" ]; then
    note "Wait loops running over 15 minutes. Check each one's condition is still reachable:"
    printf '%s' "$long_shells"
    echo "  A loop waiting on a pull request should stop when it merges, whatever its head says."
fi

# 2. Orphaned servers. Parent is init, so whatever started them is gone.
orphans=""
for p in $(pgrep -f 'next start|next dev|npm run dev|vite|serve ' 2>/dev/null || true); do
    ppid=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    age=$(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' ')
    [ -z "$ppid" ] || [ -z "$age" ] && continue
    if [ "$ppid" = "1" ] && [ "$age" -gt 3600 ]; then
        orphans="$orphans  pid $p, $((age/3600))h, $(ps -o args= -p "$p" | cut -c1-70)
"
    fi
done
if [ -n "$orphans" ]; then
    note "Servers orphaned by a dead session (parent is init):"
    printf '%s' "$orphans"
    echo "  Check nothing proxies to their ports before stopping them, and note they usually"
    echo "  listen on every interface rather than loopback."
fi

# 3. Scratchpad collisions. Two agents independently invent the same three filenames.
for d in "${CLAUDE_SCRATCHPAD:-}" /tmp/claude-*/*/*/scratchpad; do
    [ -d "$d" ] || continue
    bare=$(find "$d" -maxdepth 1 -type f \( -name 'pr-body.md' -o -name 'notes.md' -o -name 'plan.md' -o -name 'body.md' \) 2>/dev/null || true)
    if [ -n "$bare" ]; then
        note "Generic filenames at a shared scratchpad root:"
        echo "$bare" | sed 's/^/  /'
        echo "  Two agents writing these collide silently. A published body is visible; a clobbered"
        echo "  plan or notes file is just quietly wrong. Give each agent its own subdirectory."
    fi
done

# 4. Worktrees on merged branches, which is most of them after a batch.
for repo in "${@:-$(pwd)}"; do
    git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || continue
    name=$(basename "$repo")
    count=$(git -C "$repo" worktree list 2>/dev/null | wc -l)
    [ "$count" -le 3 ] && continue
    dirty=""
    while read -r d _; do
        [ -d "$d" ] || continue
        n=$(git -C "$d" status --porcelain 2>/dev/null | wc -l)
        [ "$n" != "0" ] && dirty="$dirty  $d ($n uncommitted)
"
    done < <(git -C "$repo" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}')
    note "$name has $count worktrees. After a batch most are on merged branches."
    echo "  Removing a worktree does not remove its branch, so nothing is lost. Verify merged"
    echo "  state from the pull request, not from a commit count: a squash merge leaves the"
    echo "  original commits looking unmerged."
    [ -n "$dirty" ] && { echo "  These have uncommitted work, look before removing:"; printf '%s' "$dirty"; }
done

# 5. Disk, since the above is what fills it.
use=$(df -P / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')
if [ -n "$use" ] && [ "$use" -gt 70 ]; then
    note "Root filesystem at ${use}%."
    du -sh /tmp/claude-* 2>/dev/null | sort -rh | head -3 | sed 's/^/  /'
fi

[ "$found" = "0" ] && echo "nothing to report"
exit 0

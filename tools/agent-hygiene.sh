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
#         agent-hygiene.sh --self-test

set -uo pipefail
found=0
note() { found=1; printf '\n%s\n' "$1"; }

# Prints a report line when used is at or above 75% of limit. $3 names the resource.
inotify_report() {
    local used=$1 limit=$2 what=$3
    case "$used$limit" in ''|*[!0-9]*) return 0 ;; esac
    [ "$limit" -gt 0 ] || return 0
    [ $((used * 100 / limit)) -ge 75 ] || return 0
    echo "  inotify $what: $used of $limit in use ($((used * 100 / limit))%)"
}

# Prints each agent worktree directory inside the repository that git would not ignore.
worktree_ignore_report() {
    local repo=$1 top d rel
    top=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || return 0
    {
        for d in .claude/worktrees .worktrees worktrees; do [ -d "$top/$d" ] && echo "$top/$d"; done
        git -C "$top" worktree list --porcelain 2>/dev/null | awk '/^worktree /{sub(/^worktree /,""); print}'
    } | sort -u | while IFS= read -r d; do
        [ "$d" = "$top" ] && continue
        case "$d" in "$top"/*) ;; *) continue ;; esac
        rel=${d#"$top"/}
        git -C "$top" check-ignore -q "$rel/" 2>/dev/null || echo "  $rel"
    done
}

if [ "${1:-}" = "--self-test" ]; then
    fails=0
    [ -n "$(inotify_report 120 128 instances)" ] || { echo "self-test failed: 120 of 128 not reported"; fails=$((fails + 1)); }
    [ -z "$(inotify_report 10 128 instances)" ] || { echo "self-test failed: 10 of 128 reported"; fails=$((fails + 1)); }
    [ -z "$(inotify_report x 128 watches)" ] || { echo "self-test failed: garbage reported"; fails=$((fails + 1)); }
    t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
    git init -q "$t/r" && mkdir -p "$t/r/.claude/worktrees/a" "$t/r/.worktrees/b"
    out=$(worktree_ignore_report "$t/r")
    { grep -qx '  .claude/worktrees' <<<"$out" && grep -qx '  .worktrees' <<<"$out"; } || { echo "self-test failed: unignored worktree dirs not listed: '$out'"; fails=$((fails + 1)); }
    printf '.claude/worktrees/\n' > "$t/r/.gitignore"
    out=$(worktree_ignore_report "$t/r")
    [ "$out" = "  .worktrees" ] || { echo "self-test failed: ignored dir still listed: '$out'"; fails=$((fails + 1)); }
    [ "$fails" = 0 ] && echo "self-test passed" && exit 0
    exit 1
fi

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

# 5. Agent worktrees inside a repository that git does not ignore. `git add -A` then stages a whole
# second checkout as an embedded repository, and every repo-wide scan walks it twice.
for repo in "${@:-$(pwd)}"; do
    out=$(worktree_ignore_report "$repo")
    if [ -n "$out" ]; then
        note "$(basename "$repo") has agent worktree directories that are not gitignored:"
        printf '%s\n' "$out"
        echo "  Add them to .gitignore (for example .claude/worktrees/)."
    fi
done

# 6. inotify. Every test host that watches config files takes an instance; a full suite that starts
# over a hundred hosts reached 128 of 128, and every host after that died at startup with a failure
# list that looked like eight unrelated regressions. Counts cover the processes you can see.
inst_limit=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null)
watch_limit=$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null)
inst_fds=$(find /proc/[0-9]*/fd -lname 'anon_inode:inotify' 2>/dev/null)
inst_used=$(printf '%s' "$inst_fds" | grep -c . )
watch_used=0
if [ -n "$inst_fds" ]; then
    watch_used=$(printf '%s\n' "$inst_fds" | sed 's|/fd/|/fdinfo/|' | xargs cat 2>/dev/null | grep -c '^inotify')
fi
out=$( inotify_report "$inst_used" "$inst_limit" instances; inotify_report "$watch_used" "$watch_limit" watches )
if [ -n "$out" ]; then
    note "inotify near its limit:"
    printf '%s\n' "$out"
    echo "  Tests that start after the limit fail at startup. Run fewer test hosts at once, or ask"
    echo "  whoever owns the machine to raise fs.inotify.max_user_instances."
fi

# 7. Disk, since the above is what fills it.
use=$(df -P / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')
if [ -n "$use" ] && [ "$use" -gt 70 ]; then
    note "Root filesystem at ${use}%."
    du -sh /tmp/claude-* 2>/dev/null | sort -rh | head -3 | sed 's/^/  /'
fi

[ "$found" = "0" ] && echo "nothing to report"
exit 0

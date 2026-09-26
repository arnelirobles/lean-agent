#!/usr/bin/env bash
# Lists every reason a pull request cannot merge, in one call.
#
#   pr-blockers.sh <pr-number> [-R owner/repo]
#   pr-blockers.sh --self-test
#
# Why this exists. A refused merge used to be diagnosed one blocker at a time: read the checks, then
# the threads, then the ruleset, then the author. On a pull request that added one documentation
# file that took four round trips, and the causes were independent facts that three reads could
# have answered together. Two of them never show in the checks list at all: a fork's workflow runs
# waiting at action_required (so the required checks can never report), and a ruleset that wants an
# extra approval because the head commit's author matches no account.
#
# What it reads: the pull request, its check rollup, the branch's classic protection and ruleset
# rules, workflow runs waiting for approval on the head commit, review threads (GraphQL), the head
# commit's author, how far behind the base the head is, and the repository's merge settings.
#
# Read-only. It never merges, reruns, approves, resolves or comments. Every call is a GET or a
# GraphQL query.
#
# Exit codes: 0 nothing found blocking, 1 blockers found, 2 a read failed. A failed read is never
# reported as "no blockers": not knowing is not the same as clear.

set -uo pipefail

analyze_py=$(cat <<'PY'
import json, os, re, sys

d = sys.argv[1]
blockers, notes, errors = [], [], []

def load(name):
    p = os.path.join(d, name + ".json")
    e = os.path.join(d, name + ".err")
    if not (os.path.exists(p) and os.path.getsize(p) > 0) and os.path.exists(e) and os.path.getsize(e) > 0:
        lines = open(e).read().strip().splitlines() or ["unknown error"]
        errors.append("could not read %s: %s" % (name, lines[-1]))
        return None
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception as ex:
        errors.append("could not parse %s: %s" % (name, ex))
        return None

pr = load("pr")
if pr is None:
    if not errors:
        errors.append("could not read pr: no data")
    for e in errors:
        print("ERROR    " + e)
    print("0 blockers, %d read errors. Treat this as unknown, not clear." % len(errors))
    sys.exit(2)

base = pr.get("baseRefName", "?")
if "UNKNOWN" in (pr.get("mergeable"), pr.get("mergeStateStatus")) and pr.get("state", "OPEN") == "OPEN":
    errors.append("GitHub has not computed mergeability yet (still UNKNOWN after a retry); run this again in a minute")
state = pr.get("state")
if state and state != "OPEN":
    blockers.append("the pull request is %s" % state.lower())
if pr.get("isDraft"):
    blockers.append("it is a draft; mark it ready with: gh pr ready %s" % pr.get("number", ""))
if pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY":
    blockers.append("it conflicts with %s; merge the base and resolve" % base)

# Reviews.
if pr.get("reviewDecision") == "CHANGES_REQUESTED":
    who = sorted({(r.get("author") or {}).get("login", "?") for r in pr.get("latestReviews") or [] if r.get("state") == "CHANGES_REQUESTED"})
    blockers.append("changes requested by %s" % (", ".join(who) or "a reviewer"))
elif pr.get("reviewDecision") == "REVIEW_REQUIRED":
    blockers.append("an approving review is still required")

# Required checks, from classic protection and from rulesets.
required = set()
strict = False
branch = load("branch")
if branch:
    rsc = ((branch.get("protection") or {}).get("required_status_checks") or {})
    for c in rsc.get("contexts") or []:
        required.add(c)
    for c in rsc.get("checks") or []:
        if c.get("context"):
            required.add(c["context"])
rules = load("rules")
merge_queue = False
pr_rule = {}
if isinstance(rules, list):
    for r in rules:
        t = r.get("type")
        p = r.get("parameters") or {}
        if t == "required_status_checks":
            for c in p.get("required_status_checks") or []:
                if c.get("context"):
                    required.add(c["context"])
            strict = strict or bool(p.get("strict_required_status_checks_policy"))
        elif t == "merge_queue":
            merge_queue = True
        elif t == "pull_request":
            for k, v in p.items():
                if isinstance(v, bool):
                    pr_rule[k] = pr_rule.get(k, False) or v
                elif isinstance(v, int):
                    pr_rule[k] = max(pr_rule.get(k, 0), v)
                else:
                    pr_rule.setdefault(k, v)

seen = {}
for c in pr.get("statusCheckRollup") or []:
    name = c.get("name") or c.get("context") or "?"
    if c.get("__typename") == "StatusContext" or "state" in c and "conclusion" not in c:
        st = (c.get("state") or "").upper()
        result = {"SUCCESS": "pass", "PENDING": "pending", "EXPECTED": "pending"}.get(st, "fail")
    else:
        status = (c.get("status") or "").upper()
        concl = (c.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            result = "pending"
        elif concl in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            result = "pass"
        else:
            result = "fail"
    # A rerun appears twice; any pass for the name counts, as GitHub does for the newest run.
    prev = seen.get(name)
    if prev != "pass":
        seen[name] = result

for name, res in sorted(seen.items()):
    if res == "fail":
        blockers.append("check failing: %s%s" % (name, " (required)" if name in required else ""))
    elif res == "pending" and name in required:
        blockers.append("required check still running: %s" % name)
for name in sorted(required - set(seen)):
    blockers.append("required check has not run on the head commit: %s (after a base change or a new push it has to run again)" % name)

runs = load("runs")
if isinstance(runs, dict):
    waiting = runs.get("workflow_runs") or []
    if waiting:
        names = sorted({w.get("name") or "?" for w in waiting})
        blockers.append("%d workflow run(s) waiting at action_required (fork approval): %s. A maintainer approves them in the Actions tab; until then the checks never report" % (len(waiting), ", ".join(names)))

# Review threads.
threads = load("threads")
if isinstance(threads, dict):
    if threads.get("errors"):
        errors.append("GraphQL errors reading review threads: %s" % "; ".join(e.get("message", "?") for e in threads["errors"]))
    else:
        try:
            rt = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
            open_threads = [t for t in rt.get("nodes") or [] if not t.get("isResolved")]
            if rt.get("pageInfo", {}).get("hasNextPage"):
                notes.append("more than 100 review threads; only the first 100 were read")
            if open_threads:
                where = []
                for t in open_threads[:10]:
                    first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
                    who = (first.get("author") or {}).get("login", "?")
                    where.append("%s:%s by %s" % (t.get("path", "?"), t.get("line") or "?", who))
                resolution = pr_rule.get("required_review_thread_resolution")
                msg = "%d unresolved review thread(s): %s" % (len(open_threads), "; ".join(where))
                if resolution:
                    blockers.append(msg + ". The ruleset requires them resolved. Read each one before resolving it")
                else:
                    notes.append(msg)
        except (KeyError, TypeError):
            errors.append("review threads response had an unexpected shape")

# Approval count from the ruleset.
need = pr_rule.get("required_approving_review_count", 0) or 0
if need:
    approvals = sum(1 for r in pr.get("latestReviews") or [] if r.get("state") == "APPROVED")
    if approvals < need:
        blockers.append("the ruleset wants %d approving review(s), it has %d" % (need, approvals))

# Head commit author identity.
commit = load("commit")
unattributed_rule = any(k for k, v in pr_rule.items() if "unattributed" in k and v)
if isinstance(commit, dict):
    ca = (commit.get("commit") or {}).get("author") or {}
    ident = "%s <%s>" % (ca.get("name", "?"), ca.get("email", "?"))
    if commit.get("author") is None:
        msg = "head commit author %s matches no GitHub account" % ident
        if unattributed_rule:
            blockers.append(msg + ", and the ruleset requires an extra approval for unattributed changes. Set git user.name and user.email to your account and amend, or get the approval")
        else:
            notes.append(msg + ". Some rulesets hold such a pull request for an extra approval")
    else:
        notes.append("head commit author: %s (account %s)" % (ident, commit["author"].get("login", "?")))
elif unattributed_rule:
    notes.append("the ruleset requires extra approval for unattributed changes, and the head commit could not be read")

# Behind the base.
compare = load("compare")
behind = compare.get("behind_by") if isinstance(compare, dict) else None
if pr.get("mergeStateStatus") == "BEHIND" or (behind and strict):
    blockers.append("head is %s commit(s) behind %s and the branch requires it up to date" % (behind if behind is not None else "some", base))
elif behind:
    notes.append("head is %d commit(s) behind %s (not required to be current)" % (behind, base))

# Merge queue: rulesets show it as a rule, classic protection does not, so GraphQL decides too.
mq = load("mq")
if isinstance(mq, dict) and not mq.get("errors"):
    try:
        if mq["data"]["repository"]["mergeQueue"]:
            merge_queue = True
    except (KeyError, TypeError):
        errors.append("merge queue response had an unexpected shape")
elif not merge_queue:
    if isinstance(mq, dict):
        errors.append("GraphQL errors reading the merge queue: %s" % "; ".join(e.get("message", "?") for e in mq["errors"]))
    elif not any(e.startswith("could not read mq") for e in errors):
        errors.append("could not read mq: no data")

# How to merge.
repo = load("repo")
if merge_queue:
    notes.append("merge queue is on for %s: use `gh pr merge %s` with no strategy flag (a strategy flag is refused) and no --delete-branch" % (base, pr.get("number", "<n>")))
else:
    methods = []
    if isinstance(repo, dict):
        for flag, key in (("--squash", "allow_squash_merge"), ("--merge", "allow_merge_commit"), ("--rebase", "allow_rebase_merge")):
            if repo.get(key):
                methods.append(flag)
    allowed = pr_rule.get("allowed_merge_methods")
    if isinstance(allowed, list) and allowed:
        methods = [m for m in methods if m.lstrip("-") in allowed] or ["--" + a for a in allowed]
    notes.append("no merge queue on %s: use `gh pr merge %s %s`" % (base, pr.get("number", "<n>"), methods[0] if methods else "--squash"))

if pr.get("mergeStateStatus") == "BLOCKED" and not blockers:
    blockers.append("GitHub reports BLOCKED but none of the reads above explains it; read the rulesets with: gh api repos/OWNER/REPO/rulesets")

for b in blockers:
    print("BLOCKER  " + b)
for e in errors:
    print("ERROR    " + e)
for n in notes:
    print("NOTE     " + n)
print("%d blockers, %d read errors (merge state %s)" % (len(blockers), len(errors), pr.get("mergeStateStatus", "?")))
if errors:
    sys.exit(2)
sys.exit(1 if blockers else 0)
PY
)

analyze() { python3 -c "$analyze_py" "$1"; }

self_test() {
  local rc out fails=0
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  expect() { # $1 name, $2 want exit, $3 grep pattern that must appear
    out=$(analyze "$t/$1"); rc=$?
    if [ "$rc" != "$2" ] || ! grep -qE "$3" <<<"$out"; then
      echo "self-test failed: $1 exit $rc (want $2), pattern '$3' in:"; echo "$out"; fails=$((fails + 1))
    fi
  }
  mkdir -p "$t/clean" "$t/blocked" "$t/apierr"
  cat > "$t/clean/pr.json" <<'J'
{"number":7,"state":"OPEN","isDraft":false,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","reviewDecision":"APPROVED","baseRefName":"main",
 "latestReviews":[{"author":{"login":"rev"},"state":"APPROVED"}],
 "statusCheckRollup":[{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}]}
J
  echo '[{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"build"}]}}]' > "$t/clean/rules.json"
  echo '{"total_count":0,"workflow_runs":[]}' > "$t/clean/runs.json"
  echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false}}}}}}' > "$t/clean/threads.json"
  echo '{"author":{"login":"dev"},"commit":{"author":{"name":"Dev","email":"dev@example.com"}}}' > "$t/clean/commit.json"
  echo '{"behind_by":0}' > "$t/clean/compare.json"
  echo '{"allow_squash_merge":true,"allow_merge_commit":false}' > "$t/clean/repo.json"
  echo '{"data":{"repository":{"mergeQueue":null}}}' > "$t/clean/mq.json"
  expect clean 0 'gh pr merge 7 --squash'

  cat > "$t/blocked/pr.json" <<'J'
{"number":9,"state":"OPEN","isDraft":true,"mergeable":"MERGEABLE","mergeStateStatus":"BLOCKED","reviewDecision":"CHANGES_REQUESTED","baseRefName":"main",
 "latestReviews":[{"author":{"login":"alice"},"state":"CHANGES_REQUESTED"}],
 "statusCheckRollup":[{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"FAILURE"},
                      {"__typename":"StatusContext","context":"ci/legacy","state":"PENDING"}]}
J
  cat > "$t/blocked/rules.json" <<'J'
[{"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":true,"required_status_checks":[{"context":"build"},{"context":"ci/legacy"}]}},
 {"type":"merge_queue","parameters":{}},
 {"type":"pull_request","parameters":{"required_approving_review_count":1,"required_review_thread_resolution":true,"require_extra_approval_for_unattributed_changes":true}}]
J
  echo '{"total_count":1,"workflow_runs":[{"name":"CI"}]}' > "$t/blocked/runs.json"
  echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":false,"path":"a.py","line":3,"comments":{"nodes":[{"author":{"login":"bot"}}]}}],"pageInfo":{"hasNextPage":false}}}}}}' > "$t/blocked/threads.json"
  echo '{"author":null,"commit":{"author":{"name":"Box User","email":"user@localhost"}}}' > "$t/blocked/commit.json"
  echo '{"behind_by":4}' > "$t/blocked/compare.json"
  expect blocked 1 'is a draft'
  expect blocked 1 'changes requested by alice'
  expect blocked 1 'check failing: lint'
  expect blocked 1 'required check has not run on the head commit: build'
  expect blocked 1 'still running: ci/legacy'
  expect blocked 1 'action_required'
  expect blocked 1 '1 unresolved review thread.*a.py:3 by bot'
  expect blocked 1 'wants 1 approving review'
  expect blocked 1 'matches no GitHub account.*extra approval'
  expect blocked 1 '4 commit.*behind main'
  expect blocked 1 'merge queue is on.*no strategy flag'

  mkdir -p "$t/classicq" "$t/unknown" "$t/mqerr"
  cp "$t/clean/"*.json "$t/classicq/"
  echo '{"data":{"repository":{"mergeQueue":{"id":"MQ_1"}}}}' > "$t/classicq/mq.json"
  expect classicq 0 'merge queue is on for main'
  cp "$t/clean/"*.json "$t/unknown/"
  sed -i 's/"MERGEABLE","mergeStateStatus":"CLEAN"/"UNKNOWN","mergeStateStatus":"UNKNOWN"/' "$t/unknown/pr.json"
  expect unknown 2 'not computed mergeability'
  cp "$t/clean/"*.json "$t/mqerr/"
  rm "$t/mqerr/mq.json"; echo 'HTTP 502' > "$t/mqerr/mq.err"
  expect mqerr 2 'could not read mq'

  cp "$t/clean/"*.json "$t/apierr/"
  rm "$t/apierr/threads.json"
  echo 'HTTP 502: Bad Gateway' > "$t/apierr/threads.err"
  expect apierr 2 'ERROR +could not read threads'
  echo '{"errors":[{"message":"rate limited"}]}' > "$t/apierr/threads.json"
  rm "$t/apierr/threads.err"
  expect apierr 2 'GraphQL errors.*rate limited'

  mkdir -p "$t/nopr"; echo 'HTTP 404' > "$t/nopr/pr.err"
  expect nopr 2 'could not read pr'
  timeout 10 bash "$(readlink -f "$0")" 9 -R >/dev/null 2>&1; rc=$?
  [ "$rc" = 2 ] || { echo "self-test failed: -R with no value gave exit $rc, want 2"; fails=$((fails + 1)); }

  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi

pr=""; repo=""
while [ $# -gt 0 ]; do
  case "$1" in
    -R|--repo) [ $# -ge 2 ] || { echo "pr-blockers: $1 needs owner/repo" >&2; exit 2; }; repo=$2; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) pr=$1; shift ;;
  esac
done
case "$pr" in ''|*[!0-9]*) echo "usage: pr-blockers.sh <pr-number> [-R owner/repo]" >&2; exit 2 ;; esac
command -v gh >/dev/null || { echo "pr-blockers: gh is not installed" >&2; exit 2; }

if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null) || repo=""
  [ -n "$repo" ] || { echo "pr-blockers: not in a GitHub repository; pass -R owner/repo" >&2; exit 2; }
fi
owner=${repo%%/*}; name=${repo#*/}

w=$(mktemp -d)
trap 'rm -rf "$w"' EXIT

get() { # $1 file stem, rest: gh args
  local stem=$1; shift
  gh "$@" > "$w/$stem.json" 2> "$w/$stem.err" || { [ -s "$w/$stem.err" ] || echo "gh exited nonzero" > "$w/$stem.err"; : > "$w/$stem.json"; rm -f "$w/$stem.json"; }
}

fields=number,state,isDraft,mergeable,mergeStateStatus,reviewDecision,baseRefName,headRefOid,latestReviews,statusCheckRollup
get pr pr view "$pr" -R "$repo" --json "$fields"
if [ ! -s "$w/pr.json" ]; then analyze "$w"; exit $?; fi
# GitHub computes mergeability lazily; the first read after a push often says UNKNOWN. One retry.
if python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); sys.exit(0 if p.get("state")=="OPEN" and "UNKNOWN" in (p.get("mergeable"), p.get("mergeStateStatus")) else 1)' "$w/pr.json"; then
  sleep "${PR_BLOCKERS_RETRY_SLEEP:-5}"
  get pr pr view "$pr" -R "$repo" --json "$fields"
  if [ ! -s "$w/pr.json" ]; then analyze "$w"; exit $?; fi
fi
base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["baseRefName"])' "$w/pr.json")
sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$w/pr.json")

get branch  api "repos/$repo/branches/$base"
get rules   api "repos/$repo/rules/branches/$base"
get runs    api "repos/$repo/actions/runs?head_sha=$sha&status=action_required&per_page=50"
get commit  api "repos/$repo/commits/$sha"
get compare api "repos/$repo/compare/$base...$sha"
get repo    api "repos/$repo"
get threads api graphql -F owner="$owner" -F name="$name" -F number="$pr" -f query='
query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){
  reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated path line comments(first:1){nodes{author{login}}}}}}}}'

get mq api graphql -F owner="$owner" -F name="$name" -F branch="$base" -f query='
query($owner:String!,$name:String!,$branch:String!){repository(owner:$owner,name:$name){mergeQueue(branch:$branch){id}}}'

# The branch endpoint 404s on an unprotected branch with no rules; that is an answer, not a failure.
if [ -s "$w/branch.err" ] && grep -q 'HTTP 404' "$w/branch.err"; then rm -f "$w/branch.err"; fi

analyze "$w"

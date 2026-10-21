"""A local list of what went wrong and is not fixed yet, shared by every session on the machine.

  brain add --project P --kind K --ref R [--repo R] [--branch B] [--title T] [--body B | --body-file F]
            [--severity S] [--phase P] [--source S] [--model M] [--cycle C] [--commit H] [--link L]
            [--run-id N] [--tag T ...] [--session ID] [--session-cwd DIR]
  brain list [--open] [--status S] [--project P] [--kind K] [--tag T] [--cycle C]
  brain show ID
  brain close ID [--wontfix] [--resolution R] [--note N] [--commit H] [--tag T ...]
  brain reopen ID
  brain run-add --cycle C --project P --test-project T --tier fast|full|rerun [--repo R] [--runner mtp|vstest]
            [--started E] [--ended E] [--lane-wait N] [--load-start X] [--load-end X] [--passed N]
            [--failed N] [--skipped N] [--stalled 0|1] [--exit-code N] [--host NAME]      prints the run id
  brain flaky-clear --project P --test-project T [--runs 5]      closes each open flaky item of T with no new
            occurrence in that many passing full runs of T, with resolution flaky-cleared (test-phase calls it)
  brain queue add <repo-dir> <test-project> [--filter F] [--tier fast|full] [--after ID] [--cycle C]
            [--by NAME] [--project P] [--platform apple|auto|none] [--args=EXTRA]      prints the job id and returns at once; EXTRA is
            extra dotnet arguments for this job, written --args=-p:X=1 so a leading dash is not read as an option.
            --platform auto (the default) marks the job apple when the test project targets -maccatalyst, -ios
            or -macos; a remote lane never claims an apple job
  brain queue list [--cycle C] [--status S]     --status queued lists in the order the drainer takes them
  brain queue bump ID                      a queued job, and the jobs chained after it, to the front
  brain queue move ID up|down              swap a queued job with the one before or after it; a job never
            moves ahead of the job it waits on
  brain queue cancel ID                    a queued job; jobs waiting on it are cancelled too
  brain queue route ID --to remote|local|any [--force]   remote: only a remote lane claims it; local: only this
            machine (platform=apple); any: whichever lane is free. remote refuses an Apple job without --force
  brain queue next [--peek] [--where local|remote] [--host NAME]   (what test-phase uses to drain)
  brain queue finish ID --status done|failed | requeue ID --reason R   (requeue puts a running job back)
  brain activity add --source S --action A [--ts T] [--project P] [--target T] [--ok 0|1] [--duration-ms N]
            [--error E] [--host H]
  brain activity list [--source S] [--project P] [--since 24h] [--failed] [--limit N]      newest first
  brain activity import-baryovm [--file PATH]   reads BaryoVM's activity.jsonl from where the last import
            stopped, so a rerun adds nothing; a file that shrank is read from the start
  brain resume [--project P] [--max N]
  brain stats [--json]
  brain export [--format md|json] [--project P] [--status S]
  brain session start|end|status ID [--cwd DIR]
  brain answers --session ID
  brain --self-test

The file is ${LEAN_BRAIN_DB:-~/.local/share/lean/brain.db}, one sqlite database with a schema version
(table meta) that is migrated forward on open. Nothing leaves the machine. Output is plain text, one
line per item. --body-file - reads the body from stdin.

Kinds in use: test-fail, flaky, minor, question, ticket, security, and three for surviving a context
compaction: pending (the next step), blocker (waiting on the user, or a denied action that must not be
worked around) and agent (a running subagent: id, task and worktree in the title or body; closed when it
reports back). Others are allowed. Never store secrets, tokens or passwords.
resume prints the open items of one project as a short digest: blockers, agents, queued and running
jobs, pending, questions, then the rest, newest first, titles only, at most --max lines (default 40). The project defaults to the
git toplevel's name in the current directory. It prints nothing and exits 0 when there is no database
or nothing open. Queued and running jobs are listed after the blockers and agents. The SessionStart hook runs it on startup and after a compaction.
Status: open, closed, wontfix. Resolution: fixed, flaky, not-a-bug, duplicate, answered, filed.
Severity: critical, important, minor, info. Phase: code, test, fix, review.
Source: test-phase, agent, review, user. Model: haiku, sonnet, opus, or empty.
add stores the Claude session that filed the item and its folder (--session, --session-cwd, defaulting
to $LEAN_SESSION_ID and $LEAN_SESSION_CWD, which the session-env hook sets), so an answer can resume it.
session records whether a session is open or ended (the session-env hook calls it), so an answer
never starts a second copy of a live session. answers prints the answered questions a session filed that
it has not had yet and marks them delivered; close --delivered marks one delivered at once.

Adding an open item with the same project, kind and ref does not make a new row: it raises
occurrences and moves last_seen, so a failure seen in ten runs is one line.

The queue replaces wait loops. Never write a sleep or pgrep loop to wait for the test lane: enqueue and
end the turn. bin/test-phase drains the queue, one drainer at a time, oldest ready job first. A job
whose --after job failed or was cancelled is cancelled with the reason. States: queued, running, done,
failed, cancelled.

What goes where. Brain holds anything with a status that changes: test failures, flaky classes,
deferred minors, open questions for the user, unfiled or filed tickets, security findings. Markdown
ledgers keep rulings (decisions, with the cost if wrong), task complete lines and user decisions.
Memory files keep standing preferences. Run logs are scratch and can expire once their failures are in
brain. A ledger points to open items with a line like "open items: brain list --project X --status open".
"""
import argparse
import socket
import os

from .store import MODEL, PHASE, RESOLUTION, RUNNERS, SEVERITY, SOURCE, STATUS, TIERS
from .items import cmd_add, cmd_answers, cmd_close, cmd_list, cmd_reopen, cmd_run_add, cmd_session, cmd_show
from .flaky import cmd_flaky_clear
from .activity import cmd_activity
from .queue import JOB_STATUS, JOB_TIERS, cmd_queue
from .report import cmd_export, cmd_resume, cmd_stats


def parser():
    p = argparse.ArgumentParser(prog="brain", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--project", required=True)
    s.add_argument("--repo", default="")
    s.add_argument("--branch", default="")
    s.add_argument("--kind", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--title", default="")
    s.add_argument("--body", "--detail", default="")
    s.add_argument("--body-file")
    s.add_argument("--severity", default="", choices=SEVERITY + ("",))
    s.add_argument("--phase", default="", choices=PHASE + ("",))
    s.add_argument("--source", default="", choices=SOURCE + ("",))
    s.add_argument("--model", default="", choices=MODEL)
    s.add_argument("--cycle", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--link", default="")
    s.add_argument("--run-id", type=int)
    s.add_argument("--tag", action="append")
    s.add_argument("--session", default=os.environ.get("LEAN_SESSION_ID", ""),
                   help="the Claude session that filed it (default $LEAN_SESSION_ID)")
    s.add_argument("--session-cwd", default=os.environ.get("LEAN_SESSION_CWD", ""),
                   help="that session's folder (default $LEAN_SESSION_CWD)")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("list")
    s.add_argument("--open", action="store_true")
    s.add_argument("--status", choices=STATUS)
    s.add_argument("--project")
    s.add_argument("--kind")
    s.add_argument("--tag")
    s.add_argument("--cycle")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("close")
    s.add_argument("id", type=int)
    s.add_argument("--wontfix", action="store_true")
    s.add_argument("--resolution", default="", choices=RESOLUTION + ("",))
    s.add_argument("--note", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--tag", action="append")
    s.add_argument("--delivered", action="store_true", help="the answer already reached the asking session")
    s.set_defaults(fn=cmd_close)
    s = sub.add_parser("session")
    s.add_argument("action", choices=("start", "end", "status"))
    s.add_argument("id")
    s.add_argument("--cwd", default="")
    s.set_defaults(fn=cmd_session)
    s = sub.add_parser("answers")
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_answers)
    s = sub.add_parser("reopen")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_reopen)
    s = sub.add_parser("run-add")
    s.add_argument("--cycle", default="")
    s.add_argument("--project", required=True)
    s.add_argument("--repo", default="")
    s.add_argument("--test-project", required=True)
    s.add_argument("--runner", default="", choices=RUNNERS)
    s.add_argument("--tier", required=True, choices=TIERS)
    s.add_argument("--started", type=int)
    s.add_argument("--ended", type=int)
    s.add_argument("--lane-wait", type=float)
    s.add_argument("--load-start", type=float)
    s.add_argument("--load-end", type=float)
    s.add_argument("--passed", type=int, default=0)
    s.add_argument("--failed", type=int, default=0)
    s.add_argument("--skipped", type=int, default=0)
    s.add_argument("--stalled", type=int, default=0, choices=(0, 1))
    s.add_argument("--exit-code", type=int)
    s.add_argument("--host", default="")
    s.set_defaults(fn=cmd_run_add)
    s = sub.add_parser("flaky-clear")
    s.add_argument("--project", required=True)
    s.add_argument("--test-project", required=True)
    s.add_argument("--runs", type=int, default=5)
    s.set_defaults(fn=cmd_flaky_clear)
    s = sub.add_parser("queue")
    qs = s.add_subparsers(dest="qcmd", required=True)
    q = qs.add_parser("add")
    q.add_argument("repo_dir")
    q.add_argument("test_project")
    q.add_argument("--filter", default="")
    q.add_argument("--tier", choices=JOB_TIERS, default="full")
    q.add_argument("--after", type=int)
    q.add_argument("--cycle", default="")
    q.add_argument("--by", default="")
    q.add_argument("--project", default="")
    q.add_argument("--args", default="")
    q.add_argument("--platform", choices=("apple", "auto", "none"), default="auto")
    q = qs.add_parser("list")
    q.add_argument("--cycle")
    q.add_argument("--status", choices=JOB_STATUS)
    q = qs.add_parser("cancel")
    q.add_argument("id", type=int)
    q = qs.add_parser("bump")
    q.add_argument("id", type=int)
    q = qs.add_parser("move")
    q.add_argument("id", type=int)
    q.add_argument("where", choices=("up", "down"))
    q = qs.add_parser("next")
    q.add_argument("--peek", action="store_true")
    q.add_argument("--where", choices=("local", "remote"), default="local")
    q.add_argument("--host", default="")
    q = qs.add_parser("route")
    q.add_argument("id", type=int)
    q.add_argument("--to", choices=("remote", "local", "any"), required=True)
    q.add_argument("--force", action="store_true", help="send an Apple job to a remote lane anyway")
    q = qs.add_parser("requeue")
    q.add_argument("id", type=int)
    q.add_argument("--reason", required=True)
    q = qs.add_parser("finish")
    q.add_argument("id", type=int)
    q.add_argument("--status", choices=("done", "failed"), required=True)
    q.add_argument("--run-id", type=int)
    q.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_queue)
    s = sub.add_parser("activity")
    acs = s.add_subparsers(dest="acmd", required=True)
    q = acs.add_parser("add")
    q.add_argument("--source", required=True)
    q.add_argument("--action", required=True)
    q.add_argument("--ts", default="")
    q.add_argument("--project", default="")
    q.add_argument("--target", default="")
    q.add_argument("--ok", type=int, choices=(0, 1), default=1)
    q.add_argument("--duration-ms", type=int)
    q.add_argument("--error", default="")
    q.add_argument("--host", default=socket.gethostname())
    q = acs.add_parser("list")
    q.add_argument("--source")
    q.add_argument("--project")
    q.add_argument("--since", help="like 24h, 7d, 30m")
    q.add_argument("--failed", action="store_true")
    q.add_argument("--limit", type=int, default=50)
    q = acs.add_parser("import-baryovm")
    q.add_argument("--file", help="default $BARYOVM_HOME/activity.jsonl, or ~/.baryovm/activity.jsonl")
    s.set_defaults(fn=cmd_activity)
    s = sub.add_parser("resume")
    s.add_argument("--project")
    s.add_argument("--max", type=int, default=40)
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("export")
    s.add_argument("--format", choices=("md", "json"), default="md")
    s.add_argument("--project")
    s.add_argument("--status", choices=STATUS)
    s.set_defaults(fn=cmd_export)
    return p


def main(argv):
    args = parser().parse_args(argv)
    return args.fn(args)

"""Shared by tools/credit-run and tools/credit-batch: the key, the child environment and the ledger.

One ledger, ~/.lean/credit-runs.jsonl, and one total cap (LEAN_CREDIT_TOTAL_USD) for both tools.
A job writes a reserve line before it starts under a file lock that also checks the cap, and a
final line when it ends. A reserve line with no final line counts at its reserved amount.
"""
import fcntl
import json
import os
import subprocess
import sys

PROG = "credit-run"
DROP_ENV = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def die(msg, code=2):
    print(f"{PROG}: {msg}", file=sys.stderr)
    sys.exit(code)


def home():
    return os.path.expanduser("~")


def ledger_path():
    return os.path.join(home(), ".lean", "credit-runs.jsonl")


def sdk_dir():
    return os.path.join(home(), ".lean", "credit-sdk")


def money(text, name):
    try:
        v = float(text)
    except (TypeError, ValueError):
        v = -1.0
    if not (v > 0) or v == float("inf"):
        die(f"{name} must be a positive dollar amount, got {text!r}")
    return v


def key_item():
    return os.environ.get("LEAN_CREDIT_KEY_ITEM") or "lean-api-credit"


def key_command():
    item = key_item()
    if sys.platform == "darwin":
        return ["security", "find-generic-password", "-s", item, "-w"]
    if sys.platform.startswith("linux"):
        return ["secret-tool", "lookup", "service", item]
    return None


def read_key():
    cmd = key_command()
    if cmd is None:
        die("only macOS and Linux are supported")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        die(f"{cmd[0]} not found; run credit-run --key-help")
    key = r.stdout.strip() if r.returncode == 0 else ""
    if not key:
        die(f"no key in the keychain item {key_item()!r}; run credit-run --key-help")
    return key


def scrub(text, key):
    return text.replace(key, "[redacted]") if key and text else text


def child_env(key, ttl=None):
    env = dict(os.environ)
    for name in DROP_ENV:
        env.pop(name, None)
    env["ANTHROPIC_API_KEY"] = key
    if ttl:
        env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = ttl
    return env


def read_ledger():
    """Every ledger line as a row; a line that is not valid JSON is None."""
    rows = []
    try:
        with open(ledger_path()) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append(None)
    except OSError:
        pass
    return rows


def number(v):
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")
    return float(v) if ok else None


def session_of(row):
    """The session id of a row: session_id here, session in the older single-line rows."""
    return row.get("session_id") or row.get("session")


def totals_ex(rows):
    """(spent, reserved, unknown, skipped) in dollars and row counts.

    A run with a reserve line and no final line is reserved. A row with no phase is an older
    single-line row: a finished run at its cost, or at its budget (counted as unknown) when the
    cost is null or cost_known is false. Rows that are not JSON objects, or whose cost or budget
    is not a number, are skipped."""
    reserved = {}
    finals = {}
    spent = 0.0
    unknown = skipped = 0
    for r in rows:
        if not isinstance(r, dict):
            skipped += 1
            continue
        phase = r.get("phase")
        if phase == "reserve":
            reserved[r.get("run")] = number(r.get("reserved")) or 0.0
        elif phase == "final":
            finals[r.get("run")] = r.get("cost")
        elif phase is None:
            cost = r.get("cost")
            if cost is None or r.get("cost_known") is False:
                amount = number(r.get("budget"))
                if amount is None:
                    skipped += 1
                    continue
                unknown += 1
            else:
                amount = number(cost)
                if amount is None:
                    skipped += 1
                    continue
            spent += amount
    for run, cost in finals.items():
        n = number(cost)
        spent += n if n is not None else reserved.get(run, 0.0)
    held = sum(v for k, v in reserved.items() if k not in finals)
    return spent, held, unknown, skipped


def totals(rows):
    return totals_ex(rows)[:2]


def end_line(f):
    """A cut-off last line must not swallow the next record."""
    size = os.fstat(f.fileno()).st_size
    if size and os.pread(f.fileno(), 1, size - 1) != b"\n":
        f.write("\n")


def append_line(record):
    path = ledger_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        end_line(f)
        f.write(json.dumps(record) + "\n")
        f.flush()


def reserve(record, total):
    """Check the cap and append the reservation under one lock."""
    path = ledger_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        rows = []
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append(None)
        spent, held = totals(rows)
        need = record["reserved"]
        if spent + held + need > total + 1e-9:
            die(f"refused: spent ${spent:.4f} plus reserved ${held:.4f} plus this job's "
                f"${need:.4f} is over LEAN_CREDIT_TOTAL_USD ${total:.4f}")
        end_line(f)
        f.write(json.dumps(record) + "\n")
        f.flush()


def spent_report():
    spent, held, unknown, skipped = totals_ex(read_ledger())
    total = os.environ.get("LEAN_CREDIT_TOTAL_USD")
    print(f"spent    ${spent:.4f}")
    print(f"reserved ${held:.4f} (jobs with no final line, at their reserved amount)")
    if unknown:
        print(f"unknown  {unknown} older row(s) with no known cost, counted at their budget")
    if skipped:
        print(f"skipped  {skipped} ledger row(s) that are not valid JSON or have no numeric cost")
    if total:
        t = money(total, "LEAN_CREDIT_TOTAL_USD")
        print(f"total    ${t:.4f}")
        print(f"remaining ${t - spent - held:.4f}")
    else:
        print("total    not set (LEAN_CREDIT_TOTAL_USD)")
    print("This is a local estimate from total_cost_usd. The Console workspace spend limit "
          "is the only hard limit.")
    print("Batch costs are computed from token counts, not a billed figure.")
    return 0

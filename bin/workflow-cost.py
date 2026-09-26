#!/usr/bin/env python3
"""Sum token usage per agent and per model for one or more workflow runs.

usage: workflow-cost.py <run-dir-or-run-id> [...] [--prs N] [--baseline-per-pr USD]

A run dir is .../subagents/workflows/wf_<id>; a bare id is looked up under every
project dir in ~/.claude/projects. Prices are notional list prices per million
tokens, set in PRICES; Fable is priced as Opus until a real number exists.
"""
import glob
import json
import os
import sys

PRICES = {  # input, cache write, cache read, output; USD per million tokens
    "opus": (15.0, 18.75, 1.50, 75.0),
    "fable": (15.0, 18.75, 1.50, 75.0),
    "sonnet": (3.0, 3.75, 0.30, 15.0),
    "haiku": (1.0, 1.25, 0.10, 5.0),
}


def family(model):
    for name in PRICES:
        if name in model:
            return name
    return "opus"


def cost(fam, u):
    pi, pw, pr, po = PRICES[fam]
    return (u["in"] * pi + u["cw"] * pw + u["cr"] * pr + u["out"] * po) / 1e6


def find_run(arg):
    if os.path.isdir(arg):
        return arg
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/*/subagents/workflows/{arg}*"))
    if not hits:
        sys.exit(f"no run dir for {arg}")
    return hits[0]


def read_agent(path):
    usage = {"in": 0, "cw": 0, "cr": 0, "out": 0}
    model = None
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message") if isinstance(o.get("message"), dict) else None
            if not m:
                continue
            if m.get("model"):
                model = m["model"]
            u = m.get("usage")
            if not u:
                continue
            usage["in"] += u.get("input_tokens", 0)
            usage["cw"] += u.get("cache_creation_input_tokens", 0)
            usage["cr"] += u.get("cache_read_input_tokens", 0)
            usage["out"] += u.get("output_tokens", 0)
    return model or "unknown", usage


def label_for(run, agent_id):
    meta = os.path.join(run, f"agent-{agent_id}.meta.json")
    if os.path.exists(meta):
        try:
            with open(meta) as fh:
                return json.load(fh).get("label") or json.load(open(meta)).get("description") or ""
        except (ValueError, OSError):
            return ""
    return ""


def main(argv):
    prs = None
    baseline = None
    runs = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--prs":
            prs = int(argv[i + 1]); i += 2; continue
        if a == "--baseline-per-pr":
            baseline = float(argv[i + 1]); i += 2; continue
        runs.append(find_run(a)); i += 1
    if not runs:
        sys.exit(__doc__)

    per_model = {}
    rows = []
    for run in runs:
        for path in sorted(glob.glob(os.path.join(run, "agent-*.jsonl"))):
            agent_id = os.path.basename(path)[6:-6]
            model, u = read_agent(path)
            fam = family(model)
            c = cost(fam, u)
            rows.append((os.path.basename(run), label_for(run, agent_id) or agent_id, fam, u, c))
            pm = per_model.setdefault(fam, {"agents": 0, "in": 0, "cw": 0, "cr": 0, "out": 0, "usd": 0.0})
            pm["agents"] += 1
            for k in ("in", "cw", "cr", "out"):
                pm[k] += u[k]
            pm["usd"] += c

    print(f"{'run':14} {'agent':34} {'model':7} {'output':>8} {'cache rd':>10} {'usd':>7}")
    for run, label, fam, u, c in rows:
        print(f"{run[:14]:14} {label[:34]:34} {fam:7} {u['out']:>8} {u['cr']:>10} {c:>7.2f}")
    total = 0.0
    print()
    for fam, pm in per_model.items():
        total += pm["usd"]
        print(f"{fam:7} agents={pm['agents']:3} output={pm['out']:>9} cache_write={pm['cw']:>10} cache_read={pm['cr']:>11} usd={pm['usd']:.2f}")
    print(f"total usd={total:.2f} (list prices, Fable priced as Opus)")
    if prs:
        per_pr = total / prs
        print(f"per PR usd={per_pr:.2f} over {prs} PRs")
        if baseline:
            print(f"saving vs baseline {baseline:.2f}/PR: {100 * (1 - per_pr / baseline):.0f}%")


if __name__ == "__main__":
    main(sys.argv[1:])

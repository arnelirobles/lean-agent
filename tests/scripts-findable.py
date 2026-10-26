#!/usr/bin/env python3
"""Checks that agents can find the scripts the plugin ships.

Two rules, both from issue 68:

1. No skill, full-text.md or the README names a script by the old ${CLAUDE_PLUGIN_ROOT}/bin/ or by a
   versioned plugin path. Scripts live in tools/ and are not on PATH; the path to use is
   ${CLAUDE_PLUGIN_ROOT}/tools/<name>, and a versioned path breaks after an upgrade.
2. Every script in tools/ that has a --self-test is named in the "Scripts" block of
   skills/method/SKILL.md. Only scripts that exist in tools/ are checked.

  tests/scripts-findable.py              check this repository, exit 1 on a problem
  tests/scripts-findable.py --self-test  check the checks against small fixtures, then the repository
"""
import glob
import os
import re
import sys

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
METHOD = "skills/method/SKILL.md"
BLOCK_HEADING = "## Scripts\n"

# Not commands an agent runs: the MCP server entry point and a shared module.
NOT_COMMANDS = {"brain-mcp", "lean_remote.py"}

PATH_PATTERNS = [
    re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?\"?/bin/"),
    re.compile(r"lean-agent/\d+\.\d+\.\d+/"),
    re.compile(r"plugins/cache/"),
]


def path_problems(name, text):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for pat in PATH_PATTERNS:
            if pat.search(line):
                out.append(f"{name}:{n}: names a script by a stale path, use CLAUDE_PLUGIN_ROOT/tools/<name>: {line.strip()[:100]}")
                break
    return out


def scripts_block(text):
    start = text.find(BLOCK_HEADING)
    if start < 0:
        return None
    rest = text[start + len(BLOCK_HEADING):]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def missing_from_block(block, names):
    return [n for n in names if not re.search(r"`" + re.escape(n) + r"[ `]", block)]


def self_test_scripts(tools_dir):
    names = []
    for path in sorted(glob.glob(os.path.join(tools_dir, "*"))):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        if name in NOT_COMMANDS:
            continue
        with open(path, errors="replace") as f:
            if "--self-test" in f.read():
                names.append(name)
    return names


def check_repo():
    problems = []
    files = sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))
    files.append(os.path.join(ROOT, "skills", "method", "full-text.md"))
    files.append(os.path.join(ROOT, "README.md"))
    for path in files:
        with open(path) as f:
            problems += path_problems(os.path.relpath(path, ROOT), f.read())
    with open(os.path.join(ROOT, METHOD)) as f:
        block = scripts_block(f.read())
    if block is None:
        problems.append(f'{METHOD}: no "{BLOCK_HEADING.strip()}" block')
    else:
        for n in missing_from_block(block, self_test_scripts(os.path.join(ROOT, "tools"))):
            problems.append(f"{METHOD}: {n} has a --self-test but is missing from the scripts block")
    return problems


def self_test():
    fails = []

    def expect(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    expect("plugin root bin path", len(path_problems("x", "run ${CLAUDE_PLUGIN_ROOT}/bin/brain list")), 1)
    expect("unbraced plugin root bin path", len(path_problems("x", "$CLAUDE_PLUGIN_ROOT/bin/brain")), 1)
    expect("quoted braced form", len(path_problems("x", 'run "${CLAUDE_PLUGIN_ROOT}"/bin/brain')), 1)
    expect("quoted unbraced form", len(path_problems("x", 'run "$CLAUDE_PLUGIN_ROOT"/bin/brain')), 1)
    expect("versioned path", len(path_problems("x", "~/.claude/plugins/cache/x/lean-agent/1.9.0/bin/brain")), 1)
    expect("tools path", path_problems("x", "run ${CLAUDE_PLUGIN_ROOT}/tools/brain list"), [])
    expect("skill path is not a bin path", path_problems("x", "${CLAUDE_PLUGIN_ROOT}/skills/method/full-text.md"), [])

    doc = f"# t\n{BLOCK_HEADING}\n- `style-scan`: a\n- `a.sh <x>`: b\n- `c.sh`, `d.py`: e\n## Next\n- `missing.sh`: f\n"
    block = scripts_block(doc)
    expect("block stops at the next heading", "missing.sh" in block, False)
    expect("all named", missing_from_block(block, ["style-scan", "a.sh", "c.sh", "d.py"]), [])
    expect("one missing", missing_from_block(block, ["a.sh", "gone.sh"]), ["gone.sh"])
    expect("prefix is not a match", missing_from_block(block, ["style"]), ["style"])
    expect("no block", scripts_block("# t\nnothing\n"), None)

    fails += check_repo()
    if fails:
        for f in fails:
            print("self-test failed: " + f)
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    found = check_repo()
    for p in found:
        print(p)
    if not found:
        print("ok: scripts are named by bare command and listed in the scripts block")
    sys.exit(1 if found else 0)

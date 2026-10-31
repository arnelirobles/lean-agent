#!/usr/bin/env python3
"""LEAN_PROFILE: off does nothing, standard is quiet, strict is the old behaviour byte for byte.

  tests/profile.py --self-test

a. No hook ends a call with a bare exit 2 outside hookkit.refuse, and every hook runs through hookkit.run.
b. LEAN_PROFILE=off: every hooks.json command exits 0 with no output and writes nothing.
c. LEAN_PROFILE unset: the owner's checks do not refuse a stranger, a key in a Write still does.
d. LEAN_PROFILE=strict (and an unknown value): the hook from origin/main and the current hook give the
   same exit code, stdout and stderr on a corpus with at least one case per refuse site.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))
failures = 0


def check(name, got, want):
    global failures
    if got != want:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
        failures += 1


def hook_commands():
    with open(os.path.join(ROOT, "hooks", "hooks.json")) as f:
        data = json.load(f)
    found = []
    for groups in data["hooks"].values():
        for group in groups:
            for h in group["hooks"]:
                found.append(h["command"].replace("${CLAUDE_PLUGIN_ROOT}", ROOT))
    return found


def run_cmd(command, event, env, cwd=None):
    p = subprocess.run(command, shell=True, input=event, capture_output=True, text=True, env=env, cwd=cwd, timeout=60)
    return p.returncode, p.stdout, p.stderr


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LEAN_", "GIT_", "CLAUDE_"))}
    env.update(HOME=home, LEAN_DB=os.path.join(home, "lean.db"), LEAN_BRAIN_DB=os.path.join(home, "brain.db"),
               GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def refuse_sites():
    """File and line of every exit 2 outside a self-test function and outside hookkit.refuse itself."""
    bad = []
    folder = os.path.join(ROOT, "hooks")
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(folder, name)).read())
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "self_test":
                skip.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value == 2:
                bad.append(f"{name}:{node.lineno} return 2")
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if called in ("exit", "_exit") and node.args and isinstance(node.args[0], ast.Constant) \
                        and node.args[0].value == 2:
                    bad.append(f"{name}:{node.lineno} exit(2)")
    return bad


def part_a():
    check("no exit 2 outside hookkit.refuse", refuse_sites(), [])
    names = {os.path.basename(c.split('"')[-2] if '"' in c else c.split()[-1]) for c in hook_commands()}
    for name in sorted(names):
        source = open(os.path.join(ROOT, "hooks", name)).read()
        check(f"{name} ends in hookkit.run(main)", source.rstrip().endswith("hookkit.run(main)"), True)


def part_b():
    with tempfile.TemporaryDirectory() as scratch:
        env = clean_env(scratch, LEAN_PROFILE="off")
        for command in hook_commands():
            got = run_cmd(command, "", env, cwd=scratch)
            check("off: " + os.path.basename(command.split()[-1].strip('"')), got, (0, "", ""))
        check("off writes nothing", os.listdir(scratch), [])


def key():
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    loader = SourceFileLoader("secret_scan_tool", os.path.join(ROOT, "tools", "secret-scan"))
    spec = spec_from_loader("secret_scan_tool", loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module.fake_keys()["google-api-key (AIza)"]


def part_c():
    with tempfile.TemporaryDirectory() as scratch:
        home = os.path.join(scratch, "home")
        work = os.path.join(scratch, "work")
        os.makedirs(home)
        os.makedirs(work)
        env = clean_env(home)
        subprocess.run(["git", "init", "-q", work], check=True, env=env)
        subprocess.run(["git", "-C", work, "config", "user.email", "t@example.org"], check=True, env=env)
        open(os.path.join(work, "pnpm-lock.yaml"), "w").close()
        h = lambda name: f'python3 "{ROOT}/hooks/{name}"'  # noqa: E731

        def ev(command, tool="Bash"):
            return json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": work, "session_id": "p1"})

        slop = "a rob" + "ust retry"
        check("standard: slop commit", run_cmd(h("public-text.py"), ev(f'git commit -m "{slop}"'), env)[0], 0)
        check("standard: direct pnpm test", run_cmd(h("script-note.py"), ev("pnpm test"), env)[::2], (0, ""))
        check("standard: wait loop", run_cmd(h("wait-loop.py"), ev("sleep 5; cat out.log"), env)[0], 0)
        check("standard: commit without git hooks", run_cmd(h("secret-scan.py"), ev("git commit -m x"), env)[0], 0)
        write = json.dumps({"tool_name": "Write", "tool_input": {"file_path": os.path.join(work, "k.txt"),
                                                                 "content": "k = " + key() + "\n"},
                            "cwd": work, "session_id": "p1"})
        got = run_cmd(h("secret-scan.py"), write, env)
        check("standard: a key in a Write blocks", (got[0], "allow-secret" in got[2]), (2, True))


# Strict is compared with this release. Move the tag when a strict message changes on purpose.
STRICT_TAG = "v1.21.0"


def extract(dest, source_dir=None):
    """Copy hooks, lib and tools: from STRICT_TAG when source_dir is None, else from the working tree."""
    if source_dir is None:
        found = subprocess.run(["git", "-C", ROOT, "rev-parse", "--verify", "-q", f"refs/tags/{STRICT_TAG}^{{commit}}"],
                               capture_output=True, text=True)
        if found.returncode != 0:
            print(f"FAIL tag {STRICT_TAG} is missing; fetch tags (git fetch --tags) before running this test")
            sys.exit(1)
        archive = subprocess.run(["git", "-C", ROOT, "archive", f"refs/tags/{STRICT_TAG}", "hooks", "lib", "tools"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
    else:
        for part in ("hooks", "lib", "tools"):
            shutil.copytree(os.path.join(source_dir, part), os.path.join(dest, part), ignore=shutil.ignore_patterns("__pycache__"))


def corpus(work, repo_hooks, db):
    """(label, hook, event, env) cases, one per refuse site."""
    def bash(command, cwd=None):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd or work, "session_id": "p1"}

    slop = "a rob" + "ust retry"
    em = "a " + chr(0x2014) + " b"
    cases = [
        ("bg-bound", "bg-bound.py", {"tool_input": {"command": "sleep 99", "run_in_background": True}}, {}),
        ("bg-bound inline off", "bg-bound.py",
         {"tool_input": {"command": "LEAN_BG_GUARD=off sleep 99", "run_in_background": True}}, {}),
        ("commit-author", "commit-author.py", bash("git -c user.email=nobody commit -m x"), {}),
        ("scratchpad-root", "scratchpad-root.py",
         {"tool_name": "Write", "tool_input": {"file_path": "/tmp/s/scratchpad/notes.md"}, "cwd": "/"}, {}),
        ("public-text slop", "public-text.py", bash(f'git commit -m "{slop}"'), {}),
        ("public-text dash", "public-text.py", bash(f'gh pr create --title t --body "{em}"'), {}),
        ("wait-loop sleep", "wait-loop.py", bash("sleep 5; cat out.log"), {}),
        ("wait-loop pgrep", "wait-loop.py", bash("pgrep -f dotnet"), {}),
        ("wait-loop loop", "wait-loop.py", bash("until grep -q x f; do sleep 5; done"), {}),
        ("review-log-check", "review-log-check.py", bash("gh pr merge 6", repo_hooks["review"]), {"LEAN_DB": db}),
        ("script-note direct test", "script-note.py", bash("pnpm test", repo_hooks["pnpm"]), {}),
        ("script-note direct test allowed", "script-note.py", bash("LEAN_DIRECT_TEST=allow pnpm test", repo_hooks["pnpm"]), {}),
        ("secret-scan write key", "secret-scan.py",
         {"tool_name": "Write", "tool_input": {"file_path": os.path.join(work, "k.txt"), "content": "k = " + key() + "\n"},
          "cwd": work, "session_id": "p1"}, {}),
        ("secret-scan edit key", "secret-scan.py",
         {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(work, "k.txt"), "old_string": "a",
                                              "new_string": "k = " + key() + "\n"}, "cwd": work, "session_id": "p1"}, {}),
        ("secret-scan notebook key", "secret-scan.py",
         {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": os.path.join(work, "n.ipynb"),
                                                      "new_source": "k = " + key() + "\n"}, "cwd": work, "session_id": "p1"}, {}),
        ("secret-scan bypass", "secret-scan.py", bash("git -c core.hooksPath=/x commit -m x"), {}),
        ("secret-scan init and commit", "secret-scan.py", bash("git init -q d && git -C d commit -m x"), {}),
        ("secret-scan unknown repo", "secret-scan.py", bash('git -C "$WT" commit -m x', repo_hooks["plain"]), {}),
        ("secret-scan no hooks", "secret-scan.py", bash("git commit -m x"), {}),
        ("secret-scan skipped", "secret-scan.py", bash("LEAN_SKIP_SECRET_SCAN=1 git commit -m x"), {}),
        ("context-cost image", "context-cost.py",
         {"hook_event_name": "PreToolUse", "session_id": "ps-1", "tool_name": "Read",
          "tool_input": {"file_path": "a.png"}, "transcript_path": repo_hooks["transcript"]}, {"LEAN_IMAGE_MODE": "block"}),
        ("context-cost lookup", "context-cost.py",
         {"hook_event_name": "PreToolUse", "session_id": "ps-2", "tool_name": "Grep",
          "tool_input": {"pattern": "x"}, "transcript_path": repo_hooks["transcript"]}, {"LEAN_LOOKUP_MODE": "block"}),
        ("mode", "mode.py", {"hook_event_name": "SessionStart"}, {}),
    ]
    return cases


def normalise(text, roots):
    for r in roots:
        text = text.replace(r, "<TREE>")
    return text


def part_d():
    with tempfile.TemporaryDirectory() as scratch:
        old, new = os.path.join(scratch, "old"), os.path.join(scratch, "new")
        os.makedirs(old)
        os.makedirs(new)
        extract(old)
        extract(new, ROOT)
        home = os.path.join(scratch, "home")
        work = os.path.join(scratch, "work")
        os.makedirs(home)
        os.makedirs(work)
        env0 = clean_env(home)
        subprocess.run(["git", "init", "-q", work], check=True, env=env0)
        subprocess.run(["git", "-C", work, "config", "user.email", "t@example.org"], check=True, env=env0)
        places = {}
        for label in ("review", "pnpm", "plain"):
            d = os.path.join(scratch, label)
            os.makedirs(d)
            places[label] = d
        subprocess.run(["git", "init", "-q", places["review"]], check=True, env=env0)
        subprocess.run(["git", "-C", places["review"], "remote", "add", "origin", "https://github.com/o/r.git"],
                       check=True, env=env0)
        open(os.path.join(places["pnpm"], "pnpm-lock.yaml"), "w").close()
        db = os.path.join(scratch, "review.db")
        import leanstats
        con = leanstats.connect(db)
        con.execute("INSERT INTO reviews (date, repo, pr) VALUES ('2026-01-01', 'o/r', 5)")
        con.commit()
        con.close()
        transcript = os.path.join(scratch, "t.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5", "usage": {
                "input_tokens": 90000, "output_tokens": 5}}}) + "\n")
        places["transcript"] = transcript
        counted = os.path.join(home, "context-cost")
        os.makedirs(counted, mode=0o700)
        with open(os.path.join(counted, "ps-2.json"), "w") as f:
            json.dump({"since_agent": 50}, f)
        cases = corpus(work, places, db)
        for variant in ("strict", "Loose"):
            for label, name, event, extra in cases:
                results = []
                for tree in (old, new):
                    # a scratch crash case: no scanner in either tree
                    env = clean_env(home, LEAN_PROFILE=variant, **extra)
                    results.append(run_cmd(f'python3 "{tree}/hooks/{name}"', json.dumps(event), env, cwd=work))
                a, b = results
                a = (a[0], normalise(a[1], [old]), normalise(a[2], [old]))
                b = (b[0], normalise(b[1], [new]), normalise(b[2], [new]))
                if label == "mode" and variant != "strict":
                    check(f"{variant}: mode warns about the unknown value", "LEAN_PROFILE=loose is not one of" in b[1], True)
                    continue
                check(f"{variant} identical: {label}", b, a)
        # the corpus must hold refusals, or the comparison proves nothing
        refused = 0
        for label, name, event, extra in cases:
            env = clean_env(home, LEAN_PROFILE="strict", **extra)
            if run_cmd(f'python3 "{new}/hooks/{name}"', json.dumps(event), env, cwd=work)[0] == 2:
                refused += 1
        check("the corpus has at least 14 refusals", refused >= 14, True)
        # the crash site: no scanner in either tree
        for tree in (old, new):
            os.remove(os.path.join(tree, "tools", "secret-scan"))
        crash = {}
        for tree in (old, new):
            env = clean_env(home, LEAN_PROFILE="strict")
            ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": work}
            r = run_cmd(f'python3 "{tree}/hooks/secret-scan.py"', json.dumps(ev), env, cwd=work)
            crash[tree] = (r[0], normalise(r[1], [tree]), normalise(r[2], [tree]))
        check("strict identical: secret-scan crash", crash[new], crash[old])
        check("the crash case blocks", crash[new][0], 2)
        env = clean_env(home, LEAN_PROFILE="standard")
        check("standard: secret-scan crash passes", run_cmd(f'python3 "{new}/hooks/secret-scan.py"', json.dumps(ev), env, cwd=work)[::2],
              (0, ""))
        wev = {"tool_name": "Write", "tool_input": {"file_path": os.path.join(work, "w.txt"), "content": "hello\n"},
               "cwd": work, "session_id": "p1"}
        wr = {}
        for tree in (old, new):
            env = clean_env(home, LEAN_PROFILE="strict")
            r = run_cmd(f'python3 "{tree}/hooks/secret-scan.py"', json.dumps(wev), env, cwd=work)
            wr[tree] = (r[0], normalise(r[1], [tree]), normalise(r[2], [tree]))
        check("strict identical: secret-scan crash on Write", wr[new], wr[old])
        check("the Write crash blocks in strict", wr[new][0], 2)
        r = run_cmd(f'python3 "{new}/hooks/secret-scan.py"', json.dumps(wev), clean_env(home, LEAN_PROFILE="standard"), cwd=work)
        check("standard: Write crash is a note", (r[0], "did not run" in r[1]), (0, True))


def self_test():
    part_a()
    part_b()
    part_c()
    part_d()
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(2)

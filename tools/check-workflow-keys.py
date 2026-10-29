#!/usr/bin/env python3
"""Fail on duplicate mapping keys in GitHub Actions workflow files.

  check-workflow-keys.py [--base B]      changed and untracked .github/workflows/*.yml|*.yaml
  check-workflow-keys.py --all           every workflow file
  check-workflow-keys.py FILE [FILE...]  these files
  check-workflow-keys.py --self-test

The base defaults to $LEAN_BASE, then origin/HEAD, origin/main, origin/master.

Why this exists. YAML says duplicate keys are an error, and most loaders keep the last one
silently. A workflow with two `env:` blocks, or two `if:` on one step, runs with half of what was
written and reports green. The duplicate usually arrives from a merge that kept both sides.

With PyYAML installed, files are loaded with a loader that rejects duplicates, so the check is
exact. Without it (or with LEAN_NO_PYYAML=1) a small detector reads indentation instead. Its limits:
it understands block mappings, block sequences ("- key: v") and block scalars (| and >). It does
not parse flow mappings ({a: 1, a: 2}), keys spread over several lines, anchors merged with <<,
or multi-line quoted strings, and a line inside such a string that looks like a key can confuse
it. Workflow files rarely use those; install PyYAML where they do.

Prints how many files it checked. Given explicit files or --all, zero files is a failure; with no
arguments, zero changed workflows is a normal answer.
"""
import os
import re
import subprocess
import sys

KEY = re.compile(r"""^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^\s#'"{\[][^#]*?))\s*:(?:\s|$)""")
BLOCK_SCALAR = re.compile(r":\s*[|>][-+0-9]*\s*(#.*)?$")


def strip_comment(line):
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def minimal_duplicates(text):
    """Return [(line_number, key)] for duplicate keys, reading indentation only."""
    found = []
    stack = [(-1, set())]
    block_indent = None
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.strip() in ("---", "..."):
            stack = [(-1, set())]
            block_indent = None
            continue
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        line = strip_comment(raw)
        if not line.strip():
            continue
        col = indent
        body = line[indent:]
        while body.startswith("- ") or body == "-":
            item_col = col + 2
            while stack[-1][0] >= item_col:
                stack.pop()
            stack.append((item_col, set()))
            body = body[2:]
            lead = len(body) - len(body.lstrip(" "))
            col = item_col + lead
            body = body.lstrip(" ")
            if col != item_col:
                stack[-1] = (col, set())
        m = KEY.match(body)
        if not m:
            continue
        key = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3).strip())
        while stack[-1][0] > col:
            stack.pop()
        if stack[-1][0] < col:
            stack.append((col, set()))
        keys = stack[-1][1]
        if key in keys:
            found.append((n, key))
        keys.add(key)
        if BLOCK_SCALAR.search(line):
            block_indent = col
    return found


def pyyaml_duplicates(text):
    import yaml

    found = []

    class Loader(yaml.SafeLoader):
        pass

    def construct(loader, node, deep=False):
        seen = set()
        for k, _ in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in seen:
                found.append((k.start_mark.line + 1, str(key)))
            seen.add(key)
        return loader.construct_mapping(node, deep)

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    list(yaml.load_all(text, Loader=Loader))
    return found


def have_pyyaml():
    if os.environ.get("LEAN_NO_PYYAML") == "1":
        return False
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


def check_file(path, use_yaml):
    text = open(path, encoding="utf-8", errors="replace").read()
    if use_yaml:
        import yaml
        try:
            return pyyaml_duplicates(text), None
        except yaml.YAMLError as ex:
            return [], "invalid YAML: %s" % str(ex).splitlines()[0]
    return minimal_duplicates(text), None


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True)


def default_base():
    if os.environ.get("LEAN_BASE"):
        return os.environ["LEAN_BASE"]
    r = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for b in ("origin/main", "origin/master", "main", "master"):
        if git("rev-parse", "-q", "--verify", b + "^{commit}").returncode == 0:
            return b
    return None


def changed_workflows(base):
    spec = [".github/workflows/*.yml", ".github/workflows/*.yaml"]
    mb = git("merge-base", base, "HEAD") if base else None
    if not mb or mb.returncode != 0:
        return None
    names = git("diff", "--name-only", "--diff-filter=d", mb.stdout.strip(), "--", *spec).stdout.split("\n")
    names += git("ls-files", "--others", "--exclude-standard", "--", *spec).stdout.split("\n")
    return sorted({n for n in names if n and os.path.isfile(n)})


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    base = None
    if argv[:1] == ["--base"]:
        if len(argv) < 2:
            print("check-workflow-keys: --base needs a branch")
            return 2
        base, argv = argv[1], argv[2:]
    if argv and argv != ["--all"] and not base:
        files = argv
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            print("check-workflow-keys: missing: %s, so nothing was checked" % ", ".join(missing))
            return 1
    else:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            print("check-workflow-keys: not inside a git repository")
            return 2
        os.chdir(top.stdout.strip())
        if argv == ["--all"]:
            d = ".github/workflows"
            files = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith((".yml", ".yaml"))) if os.path.isdir(d) else []
            if not files:
                print("check-workflow-keys: --all found no workflow files, so nothing was checked")
                return 1
        elif argv:
            print("check-workflow-keys: give --base B, --all, or files, not a mix")
            return 2
        else:
            base = base or default_base()
            files = changed_workflows(base)
            if files is None:
                print("check-workflow-keys: no merge base with %r, so nothing was checked. Pass --base." % base)
                return 2
    use_yaml = have_pyyaml()
    bad = 0
    for f in files:
        dups, err = check_file(f, use_yaml)
        if err:
            print("  %s: %s" % (f, err))
            bad += 1
        for line, key in dups:
            print("  %s:%d: duplicate key %r" % (f, line, key))
            bad += 1
    mode = "PyYAML" if use_yaml else "indentation detector (no PyYAML)"
    print("check-workflow-keys: checked %d workflow file(s) with %s, %d problem(s)" % (len(files), mode, bad))
    return 1 if bad else 0


GOOD = """name: ci
on:
  push:
    branches: [main]
env:
  A: "1"
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: one
        run: |
          echo name: not a key
          echo name: still not
      - name: two
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
  test:
    runs-on: ubuntu-latest
    steps:
      - name: one
        run: echo ok # name: comment
"""
DUP_TOP = GOOD + "env:\n  B: 2\n"
DUP_STEP = GOOD.replace("        uses: actions/checkout@v4\n", "        uses: actions/checkout@v4\n        name: again\n")
DUP_JOB = GOOD.replace("    runs-on: ubuntu-latest\n    steps:\n      - name: one\n        run: |",
                       "    runs-on: ubuntu-latest\n    runs-on: other\n    steps:\n      - name: one\n        run: |")


def self_test():
    fails = []
    detectors = [("minimal", minimal_duplicates)]
    try:
        import yaml  # noqa: F401
        detectors.append(("pyyaml", pyyaml_duplicates))
    except ImportError:
        pass
    for label, fn in detectors:
        cases = [("clean", GOOD, []), ("top env twice", DUP_TOP, ["env"]), ("step name twice", DUP_STEP, ["name"]),
                 ("job runs-on twice", DUP_JOB, ["runs-on"])]
        for name, text, want in cases:
            got = [k for _, k in fn(text)]
            if got != want:
                fails.append("%s/%s: want %r, got %r" % (label, name, want, got))
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
            os.makedirs(".github/workflows")
            open(".github/workflows/ci.yml", "w").write(GOOD)
            git("add", "-A")
            git("commit", "-qm", "base")
            git("branch", "base")
            import io
            import contextlib
            for argv, want_rc, want_text in (
                (["--base", "base"], 0, "checked 0 workflow"),
                (["--all"], 0, "checked 1 workflow"),
                ([".github/workflows/ci.yml"], 0, "checked 1 workflow"),
                (["nope.yml"], 1, "missing"),
                (["--base", "no-such-branch"], 2, "no merge base"),
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = main(argv)
                if rc != want_rc or want_text not in buf.getvalue():
                    fails.append("main %r: rc %s want %s, out %r" % (argv, rc, want_rc, buf.getvalue()))
            open(".github/workflows/new.yaml", "w").write(DUP_TOP)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--base", "base"])
            if rc != 1 or "new.yaml" not in buf.getvalue() or "checked 1 workflow" not in buf.getvalue():
                fails.append("untracked duplicate: rc %s, out %r" % (rc, buf.getvalue()))
        finally:
            os.chdir(cwd)
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed (%s)" % ", ".join(l for l, _ in detectors))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

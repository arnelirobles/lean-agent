"""Rule sets: JSON files of regular expressions that the style checks load.

A set is rules/<name>.json:
  {"name": "plain-prose", "applies_to": ["prose"],
   "rules": [{"id": "em-dash", "pattern": "\\u2014", "flags": "im", "say": "an em dash"}]}
"flags" is any of i, m, s. "say" is what a hit is called; without it the hit is the matched text.

LEAN_RULES (environment only) picks the sets. The defaults are attribution and plain-prose.
A name adds rules/<name>.json, a name with a leading dash drops one:
LEAN_RULES=-plain-prose,extra.

  python3 lib/rules.py --self-test
"""
import json
import os
import re
import sys
import threading

RULES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "rules"))
DEFAULTS = ["attribution", "plain-prose"]
FLAG_BITS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_lock = threading.Lock()
_compiled = {}


def _warn(text):
    print(f"lean-agent rules: {text}", file=sys.stderr)


def _compile(directory, name):
    """The compiled set for NAME in DIRECTORY, or None when it is missing or unreadable."""
    path = os.path.join(directory, name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data["rules"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _warn(f"{name}: cannot read {path}: {exc}")
        return None
    out = {"name": str(data.get("name") or name), "applies_to": list(data.get("applies_to") or []), "rules": []}
    for rule in raw if isinstance(raw, list) else []:
        try:
            flags = 0
            for ch in rule.get("flags") or "":
                flags |= FLAG_BITS[ch]
            out["rules"].append({"id": rule["id"], "say": rule.get("say"),
                                 "re": re.compile(rule["pattern"], flags)})
        except re.error as exc:
            _warn(f"{name}/{rule.get('id')}: {exc}")
        except (KeyError, TypeError, AttributeError) as exc:
            _warn(f"{name}: bad rule {rule!r}: {exc!r}")
    return out


def _get(name):
    key = (RULES_DIR, name)
    with _lock:
        if key not in _compiled:
            if not NAME.match(name):
                return None
            found = _compile(RULES_DIR, name)
            if found is None:
                return None
            _compiled[key] = found
        return _compiled[key]


def selected(spec):
    """The set names for a LEAN_RULES value (None or empty gives the defaults)."""
    names = list(DEFAULTS)
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("-"):
            names = [n for n in names if n != item[1:]]
        elif item not in names:
            names.append(item)
    return names


def load(names=None):
    """The compiled sets. NAMES None means the defaults changed by LEAN_RULES in the environment."""
    if names is None:
        names = selected(os.environ.get("LEAN_RULES"))
    sets = []
    for name in names:
        found = _get(name)
        if found is None:
            if not NAME.match(name) or not os.path.exists(os.path.join(RULES_DIR, name + ".json")):
                _warn(f"unknown set '{name}'")
            continue
        sets.append(found)
    return sets


def scan(text, sets):
    """List of (set name, what) for each rule that matches TEXT."""
    found = []
    for one in sets:
        for rule in one["rules"]:
            match = rule["re"].search(text)
            if match:
                found.append((one["name"], rule["say"] or f"'{match.group(0).strip()}'"))
    return found


def self_test():
    import contextlib
    import io
    import tempfile

    failures = 0

    def check(label, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {label}: got {got!r}, want {want!r}")
            failures += 1

    def quiet(fn):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = fn()
        return result, err.getvalue()

    saved = os.environ.pop("LEAN_RULES", None)
    try:
        # the same texts and kinds as house_style's self-test
        kind_of = {"attribution": "attribution", "plain-prose": "slop"}
        hits = {
            "Co-Authored-By: Cla" + "ude <x@example.com>": "attribution",
            "see https://claude.ai/code/" + "session_abc": "attribution",
            "Plan page: https://claude.ai/code/" + "artifact/09aef3dd": "attribution",
            "see https://claude.ai/" + "share/abc": "attribution",
            "a " + "rob" + "ust retry": "slop",
            "fast \u2014 and safe": "slop",
            "Done. Addition" + "ally, it logs.": "slop",
        }
        sets = load()
        check("default sets", [s["name"] for s in sets], ["attribution", "plain-prose"])
        for text, kind in hits.items():
            check(f"same_as_today hit {text!r}", kind in [kind_of[n] for n, _ in scan(text, sets)], True)
        for text in ("wait for database health", "Co-Authored-By: Jane <jane@example.com>",
                     "log the retry count additionally_named_var"):
            check(f"same_as_today clean {text!r}", scan(text, sets), [])
        check("say text", scan("fast \u2014 and safe", sets), [("plain-prose", "an em dash")])
        check("matched text", scan("a " + "rob" + "ust retry", sets), [("plain-prose", "'rob" + "ust'")])

        os.environ["LEAN_RULES"] = "-plain-prose"
        mixed = "a " + "rob" + "ust retry, noreply@anthropic.com"  # style-scan: allow
        check("drop_set", [n for n, _ in scan(mixed, load())], ["attribution"])
        os.environ.pop("LEAN_RULES")

        real_dir = RULES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("attribution", "plain-prose"):
                with open(os.path.join(real_dir, name + ".json"), encoding="utf-8") as src:
                    with open(os.path.join(tmp, name + ".json"), "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            with open(os.path.join(tmp, "extra.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": "extra", "applies_to": ["prose"],
                           "rules": [{"id": "plain-x", "pattern": "\\bzork\\b", "flags": "i", "say": "zork"}]}, fh)
            with open(os.path.join(tmp, "broken.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": "broken", "applies_to": ["prose"],
                           "rules": [{"id": "open-paren", "pattern": "("},
                                     {"id": "fine-rule", "pattern": "\\bquux\\b"}]}, fh)
            globals()["RULES_DIR"] = tmp
            try:
                os.environ["LEAN_RULES"] = "extra"
                got, err = quiet(load)
                check("add_set names", [s["name"] for s in got], ["attribution", "plain-prose", "extra"])
                check("add_set hit", scan("Zork here", got), [("extra", "zork")])
                os.environ["LEAN_RULES"] = "nope"
                got, err = quiet(load)
                check("unknown_set names", [s["name"] for s in got], ["attribution", "plain-prose"])
                check("unknown_set message", "unknown set 'nope'" in err, True)
                os.environ["LEAN_RULES"] = "broken"
                got, err = quiet(load)
                check("bad_regex message", "lean-agent rules: broken/open-paren:" in err, True)
                check("bad_regex keeps the other rules", scan("a quux", got), [("broken", "'quux'")])
                os.environ.pop("LEAN_RULES")
            finally:
                globals()["RULES_DIR"] = real_dir

        # own_files_clean: the shipped sets do not match themselves
        sets = load()
        for name in ("attribution", "plain-prose"):
            with open(os.path.join(RULES_DIR, name + ".json"), encoding="utf-8") as fh:
                check(f"own_files_clean {name}", scan(fh.read(), sets), [])

        # two threads loading at once get the same compiled sets
        results = []
        workers = [threading.Thread(target=lambda: results.append(load())) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        check("threads agree", len({id(r[0]) for r in results}), 1)
    finally:
        os.environ.pop("LEAN_RULES", None)
        if saved is not None:
            os.environ["LEAN_RULES"] = saved
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

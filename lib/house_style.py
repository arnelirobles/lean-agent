"""The house-style lists and the scan every lean-agent check shares.

The lists live in rules/attribution.json and rules/plain-prose.json, loaded by
lib/rules.py (LEAN_RULES picks the sets). ATTRIBUTION and SLOP are those two
pattern lists, kept as names for old importers. scan() is the wrapper the
hooks and tools/style-scan share. wide_char stays here.

  python3 lib/house_style.py --self-test
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import rules  # noqa: E402

KINDS = {"attribution": "attribution", "plain-prose": "slop"}
WIDE_FROM = 0x2000


def _patterns(name):
    """The pattern strings of one shipped rule set, for old importers of ATTRIBUTION and SLOP."""
    path = os.path.join(rules.RULES_DIR, name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            return [r["pattern"] for r in json.load(fh)["rules"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


ATTRIBUTION = _patterns("attribution")
SLOP = _patterns("plain-prose")


def scan_sets(text, sets, attribution=True, slop=True):
    """List of (kind, what) for each rule in SETS that matches text. The attribution set is the
    attribution kind, plain-prose is slop, and any other set is slop under its own name."""
    chosen = [s for s in sets if (attribution if s["name"] == "attribution" else slop)]
    return [(KINDS.get(name, name), what) for name, what in rules.scan(text, chosen)]


def scan(text, attribution=True, slop=True):
    """List of (kind, what) for each rule of the loaded sets (LEAN_RULES) that matches text."""
    return scan_sets(text, rules.load(), attribution, slop)


def wide_char(text):
    """The first character at or above U+2000 (dashes, curly quotes, arrows, emoji), or None."""
    for ch in text:
        if ord(ch) >= WIDE_FROM:
            return ch
    return None


def self_test():
    failures = 0
    hits = {
        "Co-Authored-By: Cla" + "ude <x@example.com>": "attribution",
        "see https://claude.ai/code/" + "session_abc": "attribution",
        "Plan page: https://claude.ai/code/" + "artifact/09aef3dd": "attribution",
        "see https://claude.ai/" + "share/abc": "attribution",
        "a " + "rob" + "ust retry": "slop",
        "fast \u2014 and safe": "slop",
        "Done. Addition" + "ally, it logs.": "slop",
    }
    for text, kind in hits.items():
        kinds = [k for k, _ in scan(text)]
        if kind not in kinds:
            print(f"FAIL no {kind} hit: {text!r}")
            failures += 1
    for text in ("wait for database health", "Co-Authored-By: Jane <jane@example.com>",
                 "log the retry count additionally_named_var"):
        if scan(text):
            print(f"FAIL false hit: {text!r}: {scan(text)}")
            failures += 1
    if scan("a " + "rob" + "ust retry", slop=False):
        print("FAIL slop=False still reports slop")
        failures += 1
    with open(__file__, encoding="utf-8") as fh:
        own = fh.read()
    if scan(own):
        print(f"FAIL this file matches its own patterns: {scan(own)}")
        failures += 1
    if wide_char("plain ascii, caf\u00e9") is not None or wide_char("curly \u201cquote\u201d") != "\u201c":
        print("FAIL wide_char")
        failures += 1
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

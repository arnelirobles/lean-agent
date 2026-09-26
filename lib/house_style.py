"""The house-style lists and the scan every lean-agent check shares.

ATTRIBUTION catches agent attribution (an AI Co-Authored-By, a session link, a
"Generated with" line). SLOP catches dashes, arrow glyphs and filler words.
The hooks and bin/style-scan import this file so there is one list to edit.
The patterns are written so this file does not match itself: `\\bword\\b` in
source has a letter before the word, so the word boundary is not there.

  python3 lib/house_style.py --self-test
"""
import re
import sys

ATTRIBUTION = [
    r"(^|[\"'])\s*co-authored-by:[^\n]*(claude|anthropic|copilot|cursor|codex|gemini|\[bot\])",
    r"(^|[\"'])\s*claude-session:",
    r"claude\.ai/code/session_",
    r"claude\.ai/(code/)?(artifact|share|chat)/",
    r"generated with \[?claude code",
    r"noreply@anthropic\.com",
]
SLOP = [
    "\u2014",
    "\u2013",
    "\u2192",
    r"\bcomprehensive\b",
    r"\brobust\b",
    r"\bseamless(ly)?\b",
    r"\bdelv(e|es|ing)\b",
    r"\bleverag(e|es|ed|ing)\b",
    r"\bstreamlin(e|es|ed|ing)\b",
    r"\bcutting-edge\b",
    r"\belevat(e|es|ed|ing)\b",
    r"\bsupercharg(e|es|ed|ing)\b",
    r"\bgame-changing\b",
    r"\bit'?s important to note\b",
    r"\bin conclusion\b",
    r"\bfurthermore\b",
    r"(^|[.!?]\s+|[\"'])additionally\b",
]
SLOP_NAMES = {"\u2014": "an em dash", "\u2013": "an en dash", "\u2192": "an arrow glyph"}
WIDE_FROM = 0x2000
FLAGS = re.IGNORECASE | re.MULTILINE


def scan(text, attribution=True, slop=True):
    """List of (kind, what) for each pattern that matches text."""
    checks = []
    if attribution:
        checks += [("attribution", p) for p in ATTRIBUTION]
    if slop:
        checks += [("slop", p) for p in SLOP]
    found = []
    for kind, pattern in checks:
        match = re.search(pattern, text, FLAGS)
        if match:
            found.append((kind, SLOP_NAMES.get(pattern) or f"'{match.group(0).strip()}'"))
    return found


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

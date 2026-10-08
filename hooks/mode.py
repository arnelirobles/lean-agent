#!/usr/bin/env python3
"""Tell the session which working mode the method runs in.

SessionStart hook. The method assumes many agents (a drafter, a fresh critic, a
refuter, up to four changes in flight) and a mix of models (a cheap one drafts,
an expensive one takes what the critic cannot close, a fast one reads and reports). A strong enough model can
make one or both of those not worth it, so each is a setting. Set them in the
env block of Claude Code settings. The defaults print nothing.

  LEAN_AGENTS=many   default: subagents for drafting, hunting and refuting
  LEAN_AGENTS=one    everything in this session, no subagents or workflows
  LEAN_MODELS=mixed  default: cheap model drafts, expensive model escalates
  LEAN_MODELS=one    every step on the session's model, no switching
  LEAN_MODEL_CHEAP   default sonnet: drafts, spec review, the critic's hunt and refute
  LEAN_MODEL_STRONG  default opus: takes what the cheap model did not close in one round
  LEAN_MODEL_FAST    default haiku: search, gate and retro agents, which read and report
  mode.py --self-test

The three role settings take a Claude Code model alias (sonnet, opus, haiku, fable)
and apply only with LEAN_MODELS=mixed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

CHOICES = {"LEAN_AGENTS": ("many", "one"), "LEAN_MODELS": ("mixed", "one")}
ALIASES = ("sonnet", "opus", "haiku", "fable")
ROLES = {"LEAN_MODEL_CHEAP": "sonnet", "LEAN_MODEL_STRONG": "opus", "LEAN_MODEL_FAST": "haiku"}

ONE_AGENT = (
    "Lean agent mode, LEAN_AGENTS=one: do the work in this session. Do not start subagents or "
    "workflows. Work one ticket at a time, so four in flight does not apply. The adversarial "
    "review still runs as two passes: hunt and write the candidates down, then refute each one "
    "by re-reading the code and running the smallest check, not by re-reading the hunt. The "
    "finder and the judge share a context here, so the report says so. Findings are fixed in "
    "this session, since it wrote the change.")

ONE_MODEL = (
    "Lean agent mode, LEAN_MODELS=one: every step runs on this session's model. Do not pass a "
    "model override to a subagent and do not ask the person to switch models. There is no "
    "escalation to a stronger model: a finding not fixed in one round gets one more round on "
    "the same model, then goes to the person. The per-model split in workflow-cost.py and the "
    "cheap-versus-expensive baseline do not apply.")

MIXED_IN_ONE = (
    "LEAN_AGENTS=one with LEAN_MODELS=mixed: a session cannot change its own model, so the "
    "cheap draft on {cheap} and the expensive escalation on {strong} happen only when the person "
    "switches with /model. Say when a step wants the other model, and carry on if they do not "
    "switch. The fast steps (search, gate, retro) run in the session itself, no switch needed.")

ROLES_CHANGED = (
    "Lean agent models: the cheap model is {cheap}, the strong model is {strong} and the fast model "
    "is {fast}. Start the drafter, the spec review and the critic's hunter and refuter with model "
    "{cheap}. Start the search, gate and retro agents (they read and report, never judge code) with "
    "model {fast}. Use {strong} only for a finding not fixed in one round or an unsure tier 2 answer.")

FAST_IS_STRONG = (
    "LEAN_MODEL_FAST is {strong}, the strong model, so the read-and-report agents (search, gate, "
    "retro) now cost the most.")

SAME_ROLES = (
    "LEAN_MODEL_CHEAP and LEAN_MODEL_STRONG are both {cheap}, so there is no cheaper drafter and "
    "no stronger model to escalate to: a finding not fixed in one round gets one more round, "
    "then goes to the person.")


def setting(env, name):
    """The value of NAME, and a warning when it is set to something unknown."""
    allowed = CHOICES[name]
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return allowed[0], None
    if raw in allowed:
        return raw, None
    return allowed[0], f"{name}={raw} is not one of {', '.join(allowed)}; using {allowed[0]}."


def role(env, name):
    """The model alias for the role NAME, and a warning when it is not an alias."""
    default = ROLES[name]
    raw = (env.get(name) or "").strip().lower()
    if not raw or raw in ALIASES:
        return raw or default, None
    return default, f"{name}={raw} is not one of {', '.join(ALIASES)}; using {default}."


def message(env):
    agents, bad_agents = setting(env, "LEAN_AGENTS")
    models, bad_models = setting(env, "LEAN_MODELS")
    parts = [w for w in (bad_agents, bad_models) if w]
    if agents == "one":
        parts.append(ONE_AGENT)
    if models == "one":
        parts.append(ONE_MODEL)
        return " ".join(parts)
    cheap, bad_cheap = role(env, "LEAN_MODEL_CHEAP")
    strong, bad_strong = role(env, "LEAN_MODEL_STRONG")
    fast, bad_fast = role(env, "LEAN_MODEL_FAST")
    parts += [w for w in (bad_cheap, bad_strong, bad_fast) if w]
    names = {"cheap": cheap, "strong": strong, "fast": fast}
    if cheap == strong:
        parts.append(SAME_ROLES.format(**names))
    elif agents == "one":
        parts.append(MIXED_IN_ONE.format(**names))
    elif (cheap, strong, fast) != tuple(ROLES[name] for name in ("LEAN_MODEL_CHEAP", "LEAN_MODEL_STRONG", "LEAN_MODEL_FAST")):
        parts.append(ROLES_CHANGED.format(**names))
    if fast == strong and cheap != strong and agents == "many":
        parts.append(FAST_IS_STRONG.format(**names))
    return " ".join(parts) or None


def main():
    hookkit.read_event()
    text = message(os.environ)
    if text:
        hookkit.note("SessionStart", text)
    return 0


def self_test():
    failures = 0

    def expect(name, env, has=(), lacks=()):
        nonlocal failures
        text = message(env) or ""
        for want in has:
            if want not in text:
                print(f"FAIL {name}: missing {want!r}")
                failures += 1
        for bad in lacks:
            if bad in text:
                print(f"FAIL {name}: should not say {bad!r}")
                failures += 1

    if message({}) is not None or message({"LEAN_AGENTS": "many", "LEAN_MODELS": "mixed"}) is not None:
        print("FAIL defaults should print nothing")
        failures += 1
    expect("one agent", {"LEAN_AGENTS": "one"},
           ("LEAN_AGENTS=one", "/model", "draft on sonnet", "escalation on opus"), ("LEAN_MODELS=one:",))
    expect("one model", {"LEAN_MODELS": "one"}, ("LEAN_MODELS=one",), ("LEAN_AGENTS=one", "/model"))
    expect("both", {"LEAN_AGENTS": "one", "LEAN_MODELS": "one"},
           ("LEAN_AGENTS=one:", "LEAN_MODELS=one:"), ("/model",))
    expect("case and spaces", {"LEAN_AGENTS": " One "}, ("LEAN_AGENTS=one:",))
    expect("unknown value", {"LEAN_AGENTS": "single"}, ("LEAN_AGENTS=single is not one of many, one",),
           ("LEAN_AGENTS=one:",))
    if message({"LEAN_MODEL_CHEAP": "sonnet", "LEAN_MODEL_STRONG": "opus"}) is not None:
        print("FAIL default roles should print nothing")
        failures += 1
    expect("roles changed", {"LEAN_MODEL_CHEAP": "Haiku", "LEAN_MODEL_STRONG": "fable"},
           ("cheap model is haiku", "strong model is fable", "with model haiku"), ("/model",))
    expect("roles in one session", {"LEAN_AGENTS": "one", "LEAN_MODEL_CHEAP": "haiku"},
           ("draft on haiku", "escalation on opus"), ("Lean agent models:",))
    expect("roles ignored with one model", {"LEAN_MODELS": "one", "LEAN_MODEL_CHEAP": "nope"},
           ("LEAN_MODELS=one:",), ("LEAN_MODEL_CHEAP", "haiku"))
    expect("unknown role", {"LEAN_MODEL_STRONG": "claude-opus-5-5"},
           ("LEAN_MODEL_STRONG=claude-opus-5-5 is not one of sonnet, opus, haiku, fable; using opus.",),
           ("Lean agent models:",))
    expect("same model in both roles", {"LEAN_MODEL_CHEAP": "opus"},
           ("are both opus", "goes to the person"), ("Lean agent models:",))
    if message({"LEAN_MODEL_FAST": "haiku"}) is not None:
        print("FAIL default fast model should print nothing")
        failures += 1
    expect("fast model changed", {"LEAN_MODEL_FAST": "sonnet"},
           ("fast model is sonnet", "search, gate and retro", "with model sonnet"), ("warn", "cost the most"))
    expect("fast equal to cheap is fine", {"LEAN_MODEL_FAST": "sonnet"}, (), ("are both", "cost the most"))
    expect("unknown fast model", {"LEAN_MODEL_FAST": "haiku-5"},
           ("LEAN_MODEL_FAST=haiku-5 is not one of sonnet, opus, haiku, fable; using haiku.",),
           ("Lean agent models:",))
    expect("fast ignored with one model", {"LEAN_MODELS": "one", "LEAN_MODEL_FAST": "fable"},
           ("LEAN_MODELS=one:",), ("LEAN_MODEL_FAST", "fable", "cost the most"))
    expect("fast equal to strong", {"LEAN_MODEL_FAST": "opus"},
           ("fast model is opus", "LEAN_MODEL_FAST is opus", "cost the most"))
    expect("every role the same model", {"LEAN_MODEL_CHEAP": "opus", "LEAN_MODEL_FAST": "opus"},
           ("are both opus",), ("cost the most",))
    expect("fast steps in one session", {"LEAN_AGENTS": "one"}, ("fast steps", "run in the session itself"))
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

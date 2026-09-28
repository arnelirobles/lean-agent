#!/usr/bin/env python3
"""Tell the session which working mode the method runs in.

SessionStart hook. The method assumes many agents (a drafter, a fresh critic, a
refuter, up to four changes in flight) and a mix of models (a cheap one drafts,
an expensive one takes what the critic cannot close). A strong enough model can
make one or both of those not worth it, so each is a setting. Set them in the
env block of Claude Code settings. The defaults print nothing.

  LEAN_AGENTS=many   default: subagents for drafting, hunting and refuting
  LEAN_AGENTS=one    everything in this session, no subagents or workflows
  LEAN_MODELS=mixed  default: cheap model drafts, expensive model escalates
  LEAN_MODELS=one    every step on the session's model, no switching
  mode.py --self-test
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402

CHOICES = {"LEAN_AGENTS": ("many", "one"), "LEAN_MODELS": ("mixed", "one")}

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
    "cheap draft and the expensive escalation happen only when the person switches with /model. "
    "Say when a step wants the other model, and carry on if they do not switch.")


def setting(env, name):
    """The value of NAME, and a warning when it is set to something unknown."""
    allowed = CHOICES[name]
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return allowed[0], None
    if raw in allowed:
        return raw, None
    return allowed[0], f"{name}={raw} is not one of {', '.join(allowed)}; using {allowed[0]}."


def message(env):
    agents, bad_agents = setting(env, "LEAN_AGENTS")
    models, bad_models = setting(env, "LEAN_MODELS")
    parts = [w for w in (bad_agents, bad_models) if w]
    if agents == "one":
        parts.append(ONE_AGENT)
    if models == "one":
        parts.append(ONE_MODEL)
    elif agents == "one":
        parts.append(MIXED_IN_ONE)
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
    expect("one agent", {"LEAN_AGENTS": "one"}, ("LEAN_AGENTS=one", "/model"), ("LEAN_MODELS=one:",))
    expect("one model", {"LEAN_MODELS": "one"}, ("LEAN_MODELS=one",), ("LEAN_AGENTS=one", "/model"))
    expect("both", {"LEAN_AGENTS": "one", "LEAN_MODELS": "one"},
           ("LEAN_AGENTS=one:", "LEAN_MODELS=one:"), ("/model",))
    expect("case and spaces", {"LEAN_AGENTS": " One "}, ("LEAN_AGENTS=one:",))
    expect("unknown value", {"LEAN_AGENTS": "single"}, ("LEAN_AGENTS=single is not one of many, one",),
           ("LEAN_AGENTS=one:",))
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)

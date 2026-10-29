#!/usr/bin/env python3
"""The Gradle arguments a test job may pass, shared by test-phase and the brain queue.

A job's args= may hold task names and a short list of options. Anything else is refused: an option that sets a
property, adds a build, a settings file, an init script or another Gradle home could hand a credential to the
build or run code the repo does not hold.

  gradle_args.py --self-test
"""
import re
import sys

# Options with no value.
FLAGS = {"--rerun", "--rerun-tasks", "--info", "-i", "--stacktrace", "-s", "--full-stacktrace", "-S", "--offline",
         "--continue", "--quiet", "-q", "--warn", "-w", "--build-cache", "--no-build-cache", "--parallel",
         "--no-parallel", "--fail-fast", "--refresh-dependencies", "--dry-run", "-m"}
# Options that take a value (the next word, or --name=value for a long one).
VALUES = {"--tests", "-x", "--exclude-task", "--max-workers", "--warning-mode"}
TASK = re.compile(r"^:?[A-Za-z0-9_.:][A-Za-z0-9_.:\-]*$")


def parse(args):
    """(lead, groups) for a list of words. lead holds the options before the first task; each group is a task
    followed by the options after it, so `jvmTest --tests Foo` keeps --tests on jvmTest. ValueError names
    the first word that is not allowed."""
    lead, groups, cur, i = [], [], None, 0
    cur = lead
    args = list(args)
    while i < len(args):
        t = args[i]
        if t.startswith("-"):
            name = t.split("=", 1)[0] if t.startswith("--") else t
            if name in FLAGS and name == t:
                cur.append(t)
            elif name in VALUES:
                if "=" in t and t.startswith("--"):
                    value = t.split("=", 1)[1]
                    cur.append(t)
                else:
                    i += 1
                    if i >= len(args):
                        raise ValueError(f"{t} needs a value")
                    value = args[i]
                    cur += [t, value]
                if not value or value.startswith("-"):
                    raise ValueError(f"{t} needs a value that does not start with -")
            else:
                raise ValueError(f"{t} is not allowed in a Gradle test run (allowed: tasks and "
                                 + " ".join(sorted(FLAGS | VALUES)) + ")")
        else:
            if not TASK.match(t):
                raise ValueError(f"{t!r} is not a Gradle task name")
            groups.append([t])
            cur = groups[-1]
        i += 1
    return lead, groups


def is_test_task(task):
    return task.rsplit(":", 1)[-1].lower().endswith("test")


def ios_test_tasks(args):
    """The iOS test tasks in a list of words: a test task whose name has ios in it, any case. Option values
    are not tasks. A list that does not parse has none."""
    try:
        _, groups = parse(args)
    except ValueError:
        return []
    return [g[0] for g in groups if is_test_task(g[0]) and "ios" in g[0].lower()]


def self_test():
    fails = []
    lead, groups = parse(["--info", "jvmTest", "--tests", "Foo", "-x", "lint", "check"])
    if lead != ["--info"] or groups != [["jvmTest", "--tests", "Foo", "-x", "lint"], ["check"]]:
        fails.append(f"parse keeps options with the task before them: {lead} {groups}")
    if parse(["jvmTest", "--tests=A.B"])[1] != [["jvmTest", "--tests=A.B"]]:
        fails.append("parse must take --tests=value")
    for bad in (["-PsigningKey=x"], ["--system-prop=org.gradle.project.k=v"], ["-Dgradle.user.home=/x"],
                ["--include-build", "x"], ["-c", "s.kts"], ["--settings-file", "s"], ["-b", "b"], ["--build-file=b"],
                ["--project-cache-dir", "x"], ["-I", "x"], ["--init-script", "x"], ["-g", "/t"], ["--gradle-user-home=/t"],
                ["--project-prop", "a=b"], ["-p", "x"], ["jvmTest", "--tests"], ["jvmTest", "--tests", "-x"], ["a=b"],
                ["--info=1"]):
        try:
            parse(bad)
            fails.append(f"parse must refuse {bad}")
        except ValueError:
            pass
    if ios_test_tasks(["jvmTest", "iosSimulatorArm64Test", ":shared:IOSX64Test"]) != ["iosSimulatorArm64Test", ":shared:IOSX64Test"]:
        fails.append("ios_test_tasks must find iOS test tasks in any case")
    if ios_test_tasks(["jvmTest", "--tests", "FooIosTest", "compileKotlinIosArm64", "-x", "iosTest"]):
        fails.append("ios_test_tasks must skip option values and non-test tasks")
    if ios_test_tasks(["-Pfoo", "iosTest"]):
        fails.append("ios_test_tasks must give none for words that do not parse")
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)

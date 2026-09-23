#!/usr/bin/env python3
"""The coding_task tool: a bounded capability, not a shell.

Why this file exists. The chat surface must not be able to shell out (auto_assistant.py:25-26).
This tool is the one capability the coding profile gets instead: a FIXED argv that runs the local
`deepseek` CLI. Everything asserted here is a property that turns "we pass a string to a command"
into something auditable:

  * list argv, never shell=True — so no task string can become a command;
  * the argv is exactly ["deepseek", "--cloud", "-p", task] — no operator-supplied flags;
  * `repo` must realpath INSIDE an allowlisted root, so ../../ cannot escape;
  * HOME is passed explicitly, because deepseek dies without it (it reads ~/.config/deepseek);
  * timeout, output cap, non-zero exit and a missing binary are four DISTINCT reported outcomes.

Usage:  python3 tests/test_coding_task_plugin.py
"""
import importlib.util, os, sys

PLUGIN = "/home/ohmz/ai-stack/hermes/plugins/coding_task/__init__.py"

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    spec = importlib.util.spec_from_file_location("coding_task_t", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = {}

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        # Reset first: a refusal must leave NO record of execution, and the "nothing was executed"
        # check reads this dict. (The brief's original left the previous call's argv here.)
        calls.clear()
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return Done()

    def refused_run(argv, **kwargs):
        # A spy that records NOTHING: if a refusal path ever reaches subprocess.run, the
        # "nothing was executed" check below sees a non-empty dict and fails. (The brief's
        # original read the shared `calls` dict, which still held the previous call's argv.)
        calls["executed"] = list(argv)

    real_run = mod.subprocess.run
    mod.subprocess.run = fake_run
    try:
        mod._handle_coding_task({"task": "add a docstring to foo.py"})
        check("argv is a list", isinstance(calls["argv"], list), repr(calls["argv"]))
        check("argv is exactly deepseek --cloud -p <task>",
              calls["argv"] == ["deepseek", "--cloud", "-p", "add a docstring to foo.py"],
              repr(calls["argv"]))
        check("no shell", calls["kwargs"].get("shell") in (None, False), repr(calls["kwargs"].get("shell")))

        # A task string containing shell metacharacters is DATA, not a command. This is the check
        # that makes the no-shell claim mean something.
        mod._handle_coding_task({"task": "x; rm -rf / # && $(whoami) `id`"})
        check("metacharacters pass through as one argv element",
              calls["argv"][-1] == "x; rm -rf / # && $(whoami) `id`", repr(calls["argv"]))
        check("...and still exactly four elements", len(calls["argv"]) == 4, repr(calls["argv"]))

        # HOME must be explicit: deepseek reads ~/.config/deepseek/router.json and ~/.claude/.
        env = calls["kwargs"].get("env") or {}
        check("HOME is in the tool's environment", env.get("HOME") == "/home/ohmz", repr(env))
        check("PATH is in the tool's environment", "PATH" in env, repr(env))
        check("the gateway's environment is not inherited wholesale",
              "HERMES_HOME" not in env, repr(sorted(env)))

        # The working directory: default root, then an allowlisted repo, then a refusal.
        check("defaults to the first allowlisted root",
              calls["kwargs"].get("cwd") == mod.CODING_TASK_ROOTS[0], repr(calls["kwargs"].get("cwd")))
        mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/ai-stack"})
        check("an allowlisted repo is used as cwd",
              calls["kwargs"].get("cwd") == "/home/ohmz/ai-stack", repr(calls["kwargs"].get("cwd")))
        calls.clear()
        mod.subprocess.run = refused_run
        out = mod._handle_coding_task({"task": "t", "repo": "/etc"})
        check("a repo outside the roots is refused", "outside" in out.lower(), repr(out))
        check("...and nothing was executed", "executed" not in calls, repr(calls))
        mod.subprocess.run = fake_run
        out = mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/ai-stack/../../etc"})
        check("traversal is resolved before the check, not after", "outside" in out.lower(), repr(out))

        # The denylist applies inside an allowed root.
        out = mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/.ssh"})
        check("a denylisted subtree is refused", "refus" in out.lower() or "deny" in out.lower(), repr(out))

        # An empty task is a reported error, not a subprocess with an empty prompt.
        out = mod._handle_coding_task({})
        check("a missing task is reported, not run", "task" in out.lower(), repr(out))

        # Four distinct outcomes, never a silent empty string.
        import subprocess as _sp

        def timeout_run(argv, **kwargs):
            raise _sp.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        mod.subprocess.run = timeout_run
        out = mod._handle_coding_task({"task": "t"})
        check("timeout is its own outcome", "timed out" in out.lower() or "timeout" in out.lower(), repr(out))

        def missing_run(argv, **kwargs):
            raise FileNotFoundError("deepseek")
        mod.subprocess.run = missing_run
        out = mod._handle_coding_task({"task": "t"})
        check("missing binary is its own outcome", "not found" in out.lower(), repr(out))

        def fail_run(argv, **kwargs):
            class R:
                returncode = 2
                stdout = ""
                stderr = "boom"
            return R()
        mod.subprocess.run = fail_run
        out = mod._handle_coding_task({"task": "t"})
        check("non-zero exit is its own outcome", "2" in out and "boom" in out, repr(out))

        # The output cap: a runaway build log must not fill the agent's context.
        def big_run(argv, **kwargs):
            class R:
                returncode = 0
                stdout = "x" * (mod.MAX_OUTPUT_BYTES * 3)
                stderr = ""
            return R()
        mod.subprocess.run = big_run
        out = mod._handle_coding_task({"task": "t"})
        check("stdout is capped", len(out) <= mod.MAX_OUTPUT_BYTES + 200, str(len(out)))
        check("the cap is announced, not silent", "truncat" in out.lower(), repr(out[:200]))
    finally:
        mod.subprocess.run = real_run

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""Coding policy and the launcher's alias table.

The policy answers which backend a coding turn uses, which model and window, and
why a turn failed. It is the one place "local by default, cloud only on purpose"
is written, so these checks are about REFUSAL as much as about values: an
unconfirmed cloud ask, an unknown backend string, and a cloud session whose
background traffic must stay off the GPU.

Standalone, like every suite here:  python3 tests/test_coding_policy.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "coding_policy", os.path.join(HERE, "..", "harness", "deepseek", "coding_policy.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"   {detail!r}"))


def main():
    print("--- the default is local ---")
    r = cp.resolve()
    check("no argument resolves to the local backend", r["backend"] == "local", r)
    check("...on the local coder", r["model"] == cp.LOCAL_MODEL, r)
    check("...with the 128k window", r["context_window"] == 131072, r)
    check("...and its reserve", r["output_reserve"] == 32768, r)
    check("DEFAULT_BACKEND is local", cp.DEFAULT_BACKEND == "local", cp.DEFAULT_BACKEND)

    print("\n--- cloud needs a confirmation ---")
    try:
        cp.resolve("cloud")
        check("an unconfirmed cloud ask is refused", False, "no exception")
    except cp.BackendRefused:
        check("an unconfirmed cloud ask is refused", True)
    r = cp.resolve("cloud", confirmed=True)
    check("a confirmed cloud ask resolves", r["backend"] == "cloud" and r["model"] == cp.CLOUD_MODEL, r)
    check("...and carries no local window",
          r["context_window"] is None and r["output_reserve"] is None, r)

    print("\n--- the local ask is never refused ---")
    check("explicit local needs no confirmation", cp.resolve("local")["backend"] == "local")

    print("\n--- unknown input fails loudly ---")
    for bad in ("api", "", "Cloud", "gpu"):
        try:
            cp.resolve(bad)
            check(f"{bad!r} is refused", False, "no exception")
        except cp.BackendRefused:
            check(f"{bad!r} is refused", True)

    print("\n--- the reason vocabulary is exactly the five ---")
    check("FailureReason is the pinned set",
          cp.FailureReason == {"gpu_busy", "model_missing", "harness_error",
                               "context_overflow", "cancelled"}, cp.FailureReason)
    check("a busy-GPU message maps", cp.reason_from("GPU is busy rendering") == "gpu_busy")
    check("a missing model maps", cp.reason_from("model not found") == "model_missing")
    check("an Ollama context error maps",
          cp.reason_from("exceeds the available context size") == "context_overflow")
    check("an unrelated string maps to nothing", cp.reason_from("hello") is None)

    print("\n--- the launcher binds the symmetric alias table ---")
    launcher = open(os.path.join(HERE, "..", "harness", "deepseek", "deepseek")).read()
    check("sonnet is the local coder in the cloud branch",
          'export ANTHROPIC_DEFAULT_SONNET_MODEL="$LOCAL_MODEL"' in launcher)
    check("haiku still follows the launch provider (cloud session stays cloud)",
          'export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLOUD_MODEL"' in launcher)
    check("opus is always the cloud",
          'export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLOUD_MODEL"' in launcher)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""The GPU thermal gate and the one-queue admission rule.

Two owner decisions (2026-10-04):
  1. Plain chat TAKES the GPU lock. It used to contend without one, which on a 24 GB card means
     Ollama evicting whichever tenant was resident — the render restarting cold, or a reply that
     arrives only after a cold reload, which is the timeout users actually see. Queueing makes the
     wait visible and keeps every model swap inside the lock.
  2. A hot card is waited out BEFORE the next model is loaded (pause 85 C / resume 75 C, measured
     against this card's Target 83 / Slowdown 95 / Shutdown 98).

The gate must FAIL OPEN: an unreadable temperature is never a reason to refuse a turn.

Standalone, like every suite here:  python3 tests/test_thermal_gate.py [pipe_path]
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "pipes", "live", "auto_assistant.py")

spec = importlib.util.spec_from_file_location("aa_thermal", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"   {detail!r}"))


def main():
    src = open(PIPE_PATH, encoding="utf-8").read()

    print("--- the thresholds are the measured ones, and overridable ---")
    check("pause is 85 C", mod.GPU_TEMP_PAUSE_C == 85.0, mod.GPU_TEMP_PAUSE_C)
    check("resume is 75 C", mod.GPU_TEMP_RESUME_C == 75.0, mod.GPU_TEMP_RESUME_C)
    check("resume is below pause (hysteresis, not a flip-flop)",
          mod.GPU_TEMP_RESUME_C < mod.GPU_TEMP_PAUSE_C)
    check("both are env-overridable",
          "AA_GPU_TEMP_PAUSE_C" in src and "AA_GPU_TEMP_RESUME_C" in src)

    print("\n--- an unreadable temperature fails OPEN ---")
    real = mod._gpu_temp_c
    mod._gpu_temp_c = lambda: None
    slept = []
    check("_thermal_wait returns immediately when unreadable",
          mod._thermal_wait(sleep=lambda s: slept.append(s)) is None and slept == [], slept)
    check("a None reading never blocks", slept == [], slept)

    print("\n--- a cool card does not wait ---")
    mod._gpu_temp_c = lambda: 62.0
    slept = []
    mod._thermal_wait(sleep=lambda s: slept.append(s))
    check("62 C proceeds with no sleep", slept == [], slept)

    print("\n--- a merely warm card (above resume, below pause) still proceeds ---")
    mod._gpu_temp_c = lambda: 80.0
    slept = []
    mod._thermal_wait(sleep=lambda s: slept.append(s))
    check("80 C is below the 85 C pause, so it does not wait", slept == [], slept)

    print("\n--- a hot card waits down to RESUME, not merely below PAUSE ---")
    readings = iter([92.0, 88.0, 80.0, 74.0])   # hot -> above pause -> warm -> at/below resume
    mod._gpu_temp_c = lambda: next(readings, 74.0)
    slept, seen = [], []
    final = mod._thermal_wait(on_wait=lambda t: seen.append(t), sleep=lambda s: slept.append(s))
    check("starts waiting only above pause and stops at/below resume", final == 74.0, final)
    check("...sleeping between reads", slept == [15, 15, 15], slept)
    check("...and reporting the temperatures it saw", seen == [92.0, 88.0, 80.0], seen)
    mod._gpu_temp_c = real

    print("\n--- the gate is wired into BOTH admission paths ---")
    check("the sync media lock runs the gate after acquiring",
          "_thermal_wait()   # hold the lock, let the card cool, THEN load" in src)
    check("the async path checks the temperature before loading",
          "await asyncio.to_thread(_gpu_temp_c)" in src
          and "cool before loading" in src.replace("—", "-").replace("…", "...")
          or "letting it cool before loading" in src)
    check("the gate reads nvidia-smi with a bounded timeout",
          'subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu"' in src
          and "timeout=5" in src)

    print("\n--- plain chat now takes the lock (the one-queue rule) ---")
    chat_tail = src.split('self._route_metric("chat:vision" if attached_img else "chat", 0, "fallthrough", text)')[1][:400]
    check("the chat fallthrough returns through _locked_stream",
          "_locked_stream(" in chat_tail, chat_tail[:140])
    check("...so a chat waits rather than evicting a render",
          "NOW TAKES THE LOCK" in src)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

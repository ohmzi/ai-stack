#!/usr/bin/env python3
"""The confirmation gate bounds wasted renders — and must never be why a render fails to happen.

Why this file exists. Media routing is default-deny and heavily measured, but the residual
false-positive rate is ~35% (UPGRADE_ROADMAP.md:864) and no regex drives that to zero — what is
left is genuinely ambiguous English. A wrong render is not a wrong answer: it evicts the 18 GB chat
tenant and holds the card for minutes.

The gate is a courtesy, not a safety control, so every ambiguous path must FAIL OPEN. The failure
this pins is the one that would be invisible in the UI and fatal everywhere else: no client to ask
means tests/eval/run_eval.py drives pipe() directly with no __event_call__, and a gate that waited
for an answer would hang all four media cases in --tier full.

Costs are keyed on measurement, not on the noun the user would use:
    Krea image        16.3 s (n=42)   cheap  -> only gated under "all"
    Qwen image EDIT  162.1 s (n=26)   expensive
    Wan video        186-386 s        expensive

Usage:  python3 tests/test_confirm_gate.py [pipe_path]
"""
import asyncio, importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_gate", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def ask(p, event_call, kind="video", expensive=True, mode=None):
    old = mod.CONFIRM_RENDERS
    if mode:
        mod.CONFIRM_RENDERS = mode
    try:
        return asyncio.run(p._confirm_render(event_call, kind, "detail", expensive))
    finally:
        mod.CONFIRM_RENDERS = old


def responder(value):
    async def _c(_event):
        return value
    return _c


def main():
    p = mod.Pipe()
    check("the pipe declares __event_call__ so OpenWebUI will pass it",
          "__event_call__" in mod.Pipe.pipe.__code__.co_varnames)

    print("--- fails open on every path that is not an explicit no ---")
    check("no client attached (eval harness / direct API) renders", ask(p, None) is True)

    async def boom(_event):
        raise RuntimeError("socket gone")
    check("an exception in the call renders", ask(p, boom) is True)
    check("a dead session ({'error': ...}) renders",
          ask(p, responder({"error": "Client session disconnected."})) is True)
    check("an unsupported client returning None renders", ask(p, responder(None)) is True)
    check("a client returning nonsense renders", ask(p, responder("yes please")) is True)

    print("--- and stops on an explicit no ---")
    check("False declines", ask(p, responder(False)) is False)
    check("{'confirmed': False} declines", ask(p, responder({"confirmed": False})) is False)
    check("True proceeds", ask(p, responder(True)) is True)

    print("--- cost, not the noun, decides what is gated ---")
    no = responder(False)
    check("video is gated under the default", ask(p, no, "video", True, "video") is False)
    check("an image EDIT is gated under the default (162 s median)",
          ask(p, no, "image edit", True, "video") is False)
    check("a fresh image is NOT gated under the default (16 s)",
          ask(p, no, "image", False, "video") is True)
    check("...but IS under 'all'", ask(p, no, "image", False, "all") is False)
    check("'never' disables the gate entirely", ask(p, no, "video", True, "never") is True)

    print("--- a decline explains itself rather than going silent ---")
    msg = mod.Pipe._declined("video")
    check("names what did not happen", "no video generated" in msg.lower(), msg)
    check("offers the alternative the user probably wanted", "ask" in msg.lower(), msg)

    print("--- the shape OpenWebUI's client actually renders ---")
    seen = {}

    async def capture(event):
        seen.update(event)
        return True
    ask(p, capture)
    check("event type is 'confirmation'", seen.get("type") == "confirmation", repr(seen)[:120])
    check("carries data.title and data.message (the two fields the client reads)",
          "title" in (seen.get("data") or {}) and "message" in (seen.get("data") or {}),
          repr(seen.get("data"))[:120])

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

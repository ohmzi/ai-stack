#!/usr/bin/env python3
"""The hermes cron GPU guard: due jobs must defer while ComfyUI is rendering.

Why this file exists. Hermes-agent's gateway ticks its cron scheduler every 60 s and runs due jobs
against hermes-genesis:agent (18.3 GB) via Ollama. ComfyUI needs essentially the whole 24 GB card
during a render, and the OpenWebUI pipe unloads every Ollama model before starting one — a cron job
firing mid-render would load 18 GB straight back into a card the render is counting on. The custom
provider at ~/.hermes/plugins/gpuguard/ closes that hole by composing the built-in ticker's
`can_dispatch` gate with a ComfyUI /queue probe.

This test loads the REAL plugin file and drives its probe against stubbed queue states, plus one
live probe against the real ComfyUI. Deterministic apart from that last check, which only asserts
the endpoint answers with the fields the guard reads.

Two properties matter and both are asserted structurally:
  * the provider subclasses InProcessCronScheduler — gateway/run.py passes its drain gate ONLY to
    instances of that class, so a from-scratch provider would silently lose drain handling;
  * a busy or pending queue defers, an empty queue allows, and an UNREACHABLE ComfyUI allows
    (a dead ComfyUI is not using the GPU — failing closed would strand every job on a comfy crash).

Known limitation, documented here on purpose: `hermes cron tick` run BY HAND bypasses the provider
and fires due jobs unguarded. The gateway path — the only path that runs unattended — is guarded.

Usage:  python3 tests/test_gpuguard.py
"""
import importlib.util, io, json, os, sys, urllib.request

PLUGIN = os.path.expanduser("~/.hermes/plugins/gpuguard/__init__.py")
HERMES_SRC = os.path.expanduser("~/.hermes/hermes-agent")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    if not os.path.exists(PLUGIN) or not os.path.isdir(HERMES_SRC):
        print("hermes-agent or the gpuguard plugin is not installed — nothing to test")
        return 0
    sys.path.insert(0, HERMES_SRC)
    spec = importlib.util.spec_from_file_location("gpuguard_t", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from cron.scheduler_provider import InProcessCronScheduler
    p = mod.GpuGuardCronScheduler()
    check("subclasses InProcessCronScheduler (gateway passes its drain gate only then)",
          isinstance(p, InProcessCronScheduler))
    check("provider name is gpuguard", p.name == "gpuguard")

    # Stub the probe endpoint by patching urlopen inside the plugin module.
    def fake_urlopen(payload):
        def _open(url, timeout=0):
            return io.BytesIO(json.dumps(payload).encode())
        return _open

    real = mod.urllib.request.urlopen
    try:
        for label, payload, want in [
            ("empty queue => dispatch", {"queue_running": [], "queue_pending": []}, True),
            ("render RUNNING => defer", {"queue_running": [["x", 1]], "queue_pending": []}, False),
            ("render PENDING => defer", {"queue_running": [], "queue_pending": [["x", 2]]}, False),
        ]:
            mod.urllib.request.urlopen = fake_urlopen(payload)
            p._last_probe_at = 0.0  # bust the cache between cases
            check(label, p._comfy_idle() == want)

        def boom(url, timeout=0):
            raise OSError("connection refused")
        mod.urllib.request.urlopen = boom
        p._last_probe_at = 0.0
        check("ComfyUI unreachable => dispatch (fail open)", p._comfy_idle() is True)

        # The composed gate: gateway drain must also be able to veto.
        mod.urllib.request.urlopen = fake_urlopen({"queue_running": [], "queue_pending": []})
        p._last_probe_at = 0.0
        vetoed = {"v": False}
        captured = {}

        class _Stop:  # substitute for threading.Event; start() is never reached in this test
            pass

        def fake_start(self, stop_event, *, adapters=None, loop=None, interval=60,
                       can_dispatch=None, profile_homes=None):
            captured["gate"] = can_dispatch
        orig = InProcessCronScheduler.start
        InProcessCronScheduler.start = fake_start
        try:
            p.start(_Stop(), can_dispatch=lambda: not vetoed["v"])
            gate = captured["gate"]
            check("composed gate passes when both allow", gate() is True)
            vetoed["v"] = True
            check("gateway drain veto wins even with GPU idle", gate() is False)
        finally:
            InProcessCronScheduler.start = orig
    finally:
        mod.urllib.request.urlopen = real

    # One live probe: the real endpoint must answer with the fields the guard reads.
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=5) as r:
            d = json.load(r)
        check("live ComfyUI /queue has queue_running + queue_pending",
              "queue_running" in d and "queue_pending" in d)
    except Exception as e:
        print(f"  [SKIP] live ComfyUI probe ({e}) — guard fails open in this state by design")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

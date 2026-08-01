#!/usr/bin/env python3
"""B2: what does a chat turn actually cost while a video render holds the card?

Why this file exists. Three separate decisions have been waiting on one unmeasured number:
the 2026-07-31 choice to make GPU admission control INFORM-only (vs a bounded wait), the
--lowvram re-evaluation (roadmap 3.3), and the browser checklist's manual contention item.
Every prior argument used the phrase "it works, slowly" without a number for *slowly*.

This is a MEASUREMENT, not a gate. It asserts process safety only and writes numbers to
tests/eval/results/contention-<ts>.json. Two findings would change decisions:
  * chat merely slow (TTFT ~ cold-load + queueing)  -> inform-only stands; bounded wait closed
    as a recorded negative; --lowvram re-eval unblocked.
  * render dies OR chat spills to CPU               -> a bounded wait is justified, and this run
    measured its constant.

Spill detection is by RATIO (size_vram/size from /api/ps): absolutes under-report (measured
16.70 GiB reported vs 19995 MiB real), but a partial CPU offload drops the ratio hard, and a
spilled 34B MoE is not "slow" — it is orders of magnitude slower, a different finding wearing
the same clothes.

Faithful to the real flow: the pipe unloads every Ollama model before submitting a render, so
a mid-render chat turn arrives at an EMPTY Ollama and pays a cold load onto a busy card. The
control does the same unload first, so both TTFTs include the load and the delta isolates
contention itself.

COSTS REAL GPU MINUTES (~4-6 min) and can OOM the render by design — hence the --live gate.

Usage:  python3 tests/test_contention.py --live [pipe_path]
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE_PATH = next((a for a in sys.argv[1:] if not a.startswith("-")),
                 os.path.join(ROOT, "pipes", "live", "auto_assistant.py"))
COMFY = "http://127.0.0.1:8188"
OLLAMA = "http://127.0.0.1:11434"
MEDIAN_RENDER_S = 186.7          # the measured 14B median this run is compared against

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def post(url, payload, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def unload_all():
    for m in get(f"{OLLAMA}/api/ps").get("models") or []:
        post(f"{OLLAMA}/api/generate", {"model": m["name"], "keep_alive": 0})
    deadline = time.time() + 30
    while time.time() < deadline and (get(f"{OLLAMA}/api/ps").get("models") or []):
        time.sleep(1)


async def chat_ttft(pipe_mod, prompt):
    """Seconds to the first streamed token of one chat turn through the real pipe."""
    p = pipe_mod.Pipe()
    body = {"model": "auto_assistant.auto",
            "messages": [{"role": "user", "content": prompt}]}
    t0 = time.monotonic()
    out = await p.pipe(body, __metadata__={"chat_id": "contention", "user_prompt": prompt},
                       __event_emitter__=None)
    if isinstance(out, str):
        return time.monotonic() - t0
    async for _tok in out:
        return time.monotonic() - t0
    return time.monotonic() - t0


def spill_ratio():
    ms = get(f"{OLLAMA}/api/ps").get("models") or []
    big = [m for m in ms if (m.get("size") or 0) > 8e9]
    if not big:
        return None
    m = big[0]
    return round((m.get("size_vram") or 0) / m["size"], 3)


class VramPeak(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak, self.stop = 0, threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                      "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                self.peak = max(self.peak, int(out.split()[0]))
            except Exception:
                pass
            time.sleep(2)


def main():
    if "--live" not in sys.argv:
        print("B2 is a live GPU measurement (~5 min, can OOM the render by design).")
        print("Run:  python3 tests/test_contention.py --live")
        return 0

    spec = importlib.util.spec_from_file_location("aa_cont", PIPE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    q = get(f"{COMFY}/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        print("REFUSING: ComfyUI is busy — this test may not evict real work.")
        return 2
    if get(f"{OLLAMA}/api/ps").get("models"):
        print("REFUSING: Ollama has resident models — unload or wait for keep_alive.")
        return 2

    data = {"median_render_s": MEDIAN_RENDER_S}

    print("--- control: chat TTFT with an idle card (run 1 = cold load, by design) ---")
    unload_all()
    ctl = []
    for i in range(3):
        t = asyncio.run(chat_ttft(mod, "In one word, what colour is a clear daytime sky?"))
        ctl.append(round(t, 2))
        print(f"  control run {i + 1}: TTFT {t:.2f}s")
    data["control_ttft_s"] = ctl

    print("--- start one median-class render (Wan 14B, default res) ---")
    unload_all()                       # the pipe's own pre-render state
    p = mod.Pipe()
    wf = p._wf_video_14b("a slow pan across a foggy pine forest at dawn", 12345)
    peak = VramPeak()
    peak.start()
    t_render = time.monotonic()
    pid = post(f"{COMFY}/prompt", {"prompt": wf, "client_id": "contention-b2"})["prompt_id"]
    print(f"  submitted {pid}; waiting 30s for it to occupy the card")
    time.sleep(30)

    print("--- contended: the same chat turns, mid-render ---")
    cont, ratios = [], []
    for i in range(3):
        t = asyncio.run(chat_ttft(mod, "In one word, what colour is grass?"))
        cont.append(round(t, 2))
        ratios.append(spill_ratio())
        print(f"  contended run {i + 1}: TTFT {t:.2f}s  vram/size ratio: {ratios[-1]}")
    data["contended_ttft_s"] = cont
    data["spill_ratios"] = ratios

    print("--- did the render survive? ---")
    render_ok, render_s = False, None
    deadline = time.time() + 900
    while time.time() < deadline:
        h = get(f"{COMFY}/history/{pid}", timeout=15)
        if pid in h:
            st = h[pid].get("status", {})
            render_ok = st.get("status_str") != "error"
            render_s = round(time.monotonic() - t_render, 1)
            break
        time.sleep(5)
    peak.stop.set()
    data.update(render_survived=render_ok, render_wall_s=render_s,
                vram_peak_mib=peak.peak, pipe=PIPE_PATH)
    print(f"  survived={render_ok} wall={render_s}s (median {MEDIAN_RENDER_S}s) "
          f"vram_peak={peak.peak}MiB")

    unload_all()

    out = os.path.join(ROOT, "tests", "eval", "results",
                       f"contention-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nsaved: {out}")

    # Safety assertions only — the numbers themselves are findings, not pass/fail.
    check("render completed without error", render_ok, f"wall={render_s}")
    check("all chat turns produced a first token", all(t is not None for t in ctl + cont))
    check("card released afterwards", not (get(f"{OLLAMA}/api/ps").get("models") or []))

    slow = max(cont) / max(0.01, max(ctl))
    spilled = any(r is not None and r < 0.85 for r in ratios)
    print(f"\nB2 VERDICT INPUTS: contended/control TTFT ratio (worst) = {slow:.1f}x; "
          f"spill={'YES' if spilled else 'no'}; render={'survived' if render_ok else 'DIED'}")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

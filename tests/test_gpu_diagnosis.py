#!/usr/bin/env python3
"""Tell a real out-of-memory apart from a container that has lost its GPU.

Why this file exists. On 2026-07-28 every image request failed and the pipe told the user its
"GPU allocator is wedged (common after long uptime)". It was not. At 08:48 that morning snapd
triggered a `systemctl daemon-reload`, and because Docker injects /dev/nvidia* through the NVIDIA
legacy hook — writing the device rule into the container's cgroup behind systemd's back — systemd
reapplied the scope's device policy from records that never mentioned the GPU. Every GPU container
on the box lost CUDA at once (NVIDIA/nvidia-docker#1730). ComfyUI kept answering /system_stats with
"23.9 GB free" from the CUDA context it had opened before the revocation, so the pipe's only test
("OOM but the card looks empty") matched the wrong diagnosis.

The discriminator is how long the job survived, taken from ComfyUI's own execution timestamps.
Measured on this host, from the container log of the actual incident:

    real OOM, 24 GB genuinely squeezed by a Wan 2.2 A14B load   13.49 s
    revoked device, same workflows, card reportedly empty        0.24 s / 0.27 s / 0.38 s

Nearly two orders of magnitude apart, and for a mechanical reason: a real OOM dies part-way through
streaming weights onto the card, a revoked device is refused at the first allocation before any VRAM
moves. Ambiguity must fall through to the wedged-allocator message — same remedy, weaker claim.

Usage:  python3 tests/test_gpu_diagnosis.py [pipe_path]
"""
import importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_gpu", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, got, want):
    results.append((label, got == want, f"got {got!r}, want {want!r}"))


def pipe_with_free(gib):
    """A Pipe whose card reports `gib` free, without touching the network."""
    p = mod.Pipe()
    p._vram_free_gib = lambda: gib
    return p


# ------------------------------------------------------------------ ComfyUI history -> exec seconds
def history(start_ms, end_ms):
    """Minimal /history entry shaped like ComfyUI's, with the two timestamps that matter."""
    return {"status": {"status_str": "error", "messages": [
        ["execution_start", {"timestamp": start_ms}],
        ["execution_error", {"timestamp": end_ms, "exception_type": "torch.OutOfMemoryError"}],
    ]}}


def main():
    p = pipe_with_free(22.0)

    print("--- exec-time extraction from ComfyUI history ---")
    for label, entry, want in [
        ("revoked device (0.27 s)", history(1_000_000, 1_000_270), 0.27),
        ("real OOM (13.49 s)", history(1_000_000, 1_013_490), 13.49),
        ("no timestamps at all", {"status": {"status_str": "error", "messages": []}}, None),
        ("start but no error stamp",
         {"status": {"status_str": "error", "messages": [["execution_start", {"timestamp": 5}]]}}, None),
        ("malformed message entries",
         {"status": {"status_str": "error", "messages": [["execution_start"], ["execution_error", None]]}}, None),
        ("empty entry", {}, None),
    ]:
        got = mod.Pipe._job_exec_secs(entry)
        check(f"exec_secs {label}", got, want)
        print(f"  [{'PASS' if got == want else 'FAIL'}] {label:32} -> {got}")

    # A revoked device needs BOTH signals. Either one alone is a different fault with different
    # advice, so each must be able to veto the diagnosis on its own.
    print("\n--- classification: revoked GPU vs everything else ---")
    CASES = [
        # (label,                                  exec_secs, free_gib, is_revoked)
        ("0.24 s, 22 GB free  (the real incident)",     0.24,     22.0,  True),
        ("0.38 s, 22 GB free",                          0.38,     22.0,  True),
        ("2.90 s, 22 GB free  (just inside)",           2.90,     22.0,  True),
        ("13.49 s, 22 GB free (real OOM, was wedged)", 13.49,     22.0,  False),
        ("3.10 s, 22 GB free  (just outside)",          3.10,     22.0,  False),
        ("0.25 s, 2 GB free   (card genuinely full)",   0.25,      2.0,  False),
        ("0.25 s, 8 GB free   (boundary, not >8)",      0.25,      8.0,  False),
        ("timestamps missing  (must not guess)",        None,     22.0,  False),
        ("0.25 s, /system_stats unreachable",           0.25,     None,  False),
    ]
    for label, secs, free, want in CASES:
        got = pipe_with_free(free)._gpu_revoked(secs)
        check(f"revoked? {label}", got, want)
        print(f"  [{'PASS' if got == want else 'FAIL'}] {label:42} -> revoked={got}")

    # The wording is the whole point of the fix: the operator must not be sent hunting for a ComfyUI
    # allocator bug that does not exist.
    print("\n--- the message the user actually sees ---")
    p = pipe_with_free(22.0)
    submitted = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_post(url, **kw):
        submitted.append(url)
        return FakeResp({"prompt_id": "pid-1"})

    def fake_get(url, **kw):
        return FakeResp({"pid-1": history(1_000_000, 1_000_240)})

    # Patching the module's `requests` is safe: this module object is private to the test process.
    mod.requests.post, mod.requests.get = fake_post, fake_get
    data, err, _ = p._submit_poll({}, "9", "Image", iters=3)

    check("revoked: returns no data", data, None)
    print(f"  err = {err}")
    for want in ("lost access to the GPU", "revoked device", "daemon-reload", "docker restart comfyui"):
        got = want in (err or "")
        check(f"message mentions {want!r}", got, True)
        print(f"  [{'PASS' if got else 'FAIL'}] mentions {want!r}")
    got = "allocator is wedged" not in (err or "")
    check("message does NOT blame the allocator", got, True)
    print(f"  [{'PASS' if got else 'FAIL'}] does not blame the allocator")

    # The doomed retry is the difference between failing in a second and failing in ~50 s, because
    # the retry path first waits out _free_vram (30 s) and _comfy_free (20 s) to no purpose.
    got = len([u for u in submitted if u.endswith("/prompt")])
    check("revoked: submitted once, no doomed retry", got, 1)
    print(f"  [{'PASS' if got == 1 else 'FAIL'}] submitted {got}x (no retry on a revoked device)")

    fails = sum(1 for _, ok, _ in results if not ok)
    for label, ok, detail in results:
        if not ok:
            print(f"  FAIL: {label} — {detail}")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

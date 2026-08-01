#!/usr/bin/env python3
"""The GPU lock must survive a client disconnect, and must not be why chat looks hung.

Why this file exists. `_locked_stream` acquired the lock OUTSIDE its `try:`:

    await asyncio.to_thread(_GEN_LOCK.acquire)     # <- cancellation here...
    try:
        ...
    finally:
        _GEN_LOCK.release()                        # <- ...never reaches this

asyncio.to_thread cannot interrupt a thread already blocked in `lock.acquire()`. If the client
disconnects while that thread is waiting, the awaiting coroutine is cancelled before the `try` is
entered — the worker then wins the lock with nobody left to release it, and every subsequent render
and coder turn blocks FOREVER. The only recovery was a redeploy, which re-execs the module and
mints a fresh lock object. The docstring meanwhile claimed "released in `finally` so a client
disconnect cannot leak it" — asserting exactly the property the code lacked.

The first check below is written to FAIL against the unfixed code. If it ever passes there, the
reading was wrong and the fix should be reconsidered rather than kept on faith.

Offline and deterministic — no GPU, no Ollama, no ComfyUI.

Usage:  python3 tests/test_admission.py [pipe_path]
"""
import asyncio, importlib.util, sys, threading, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_adm", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


async def _never():
    if False:
        yield ""
    await asyncio.sleep(60)


def main():
    p = mod.Pipe()

    print("--- a disconnect while waiting for the lock must not wedge the box ---")

    async def scenario():
        holder_done = threading.Event()

        def hold():
            with mod._GEN_LOCK:
                holder_done.wait(5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        time.sleep(0.3)                       # let the holder win the lock

        async def consume():
            async for _ in p._locked_stream(_never()):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.5)              # the stream is now blocked on acquire
        task.cancel()                         # the client disconnects
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        holder_done.set()                     # holder releases
        t.join(timeout=5)
        await asyncio.sleep(1.5)              # give the abandoned waiter time to take it
        got = mod._GEN_LOCK.acquire(timeout=2)
        if got:
            mod._GEN_LOCK.release()
        return got

    free = asyncio.run(scenario())
    check("the lock is free after a cancelled waiter (unfixed code leaks it here)", free,
          "lock still held — a cancelled acquire leaked it")

    print("--- the lock still does its job ---")
    if not free:
        # Everything below would block forever on the leaked lock. That is the bug, not a reason
        # to hang the suite — the unfixed code wedged this very test process before this guard.
        print("  [SKIP] lock is wedged; remaining checks would block forever (that IS the defect)")
    else:
        check("uncontended acquire works", mod._GEN_LOCK.acquire(timeout=1) and
              (mod._GEN_LOCK.release() or True))

        async def normal():
            async def two():
                yield "a"
                yield "b"
            return [c async for c in p._locked_stream(two())]

        check("a normal stream passes tokens through", asyncio.run(normal()) == ["a", "b"])
        check("...and releases afterwards", mod._GEN_LOCK.acquire(timeout=1) and
              (mod._GEN_LOCK.release() or True))

    print("--- contention reporting never blocks and never fires without a client ---")
    if hasattr(p, "_gpu_contended"):
        import contextlib, io, json as _json

        class _R:
            def __init__(self, payload): self._p = payload
            def json(self): return self._p
            def __enter__(self): return self
            def __exit__(self, *a): return False

        real_get = mod.requests.get
        try:
            mod.requests.get = lambda *a, **k: _R({"queue_running": [["x", 1]], "queue_pending": []})
            check("reports contention while a render is RUNNING", p._gpu_contended() is True)

            # Pending-only must stay silent: the user cannot act on it, and a strip they learn to
            # ignore is worth nothing when it matters.
            mod.requests.get = lambda *a, **k: _R({"queue_running": [], "queue_pending": [["x", 2]]})
            check("silent on a PENDING-only queue", p._gpu_contended() is False)

            mod.requests.get = lambda *a, **k: _R({"queue_running": [], "queue_pending": []})
            check("silent when the card is idle", p._gpu_contended() is False)

            def boom(*a, **k):
                raise OSError("probe down")
            mod.requests.get = boom
            check("a failed probe is silent, never an exception", p._gpu_contended() is False)
        finally:
            mod.requests.get = real_get
    else:
        print("  [SKIP] _gpu_contended not present yet")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

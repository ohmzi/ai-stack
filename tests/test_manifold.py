#!/usr/bin/env python3
"""Phase 2 — manifold entries (auto / knowledge / coder).

Verifies the three properties the architecture decision rests on:
  1. knowledge/coder are chat-only  — no media regex can reach a render from them.
  2. system messages survive on knowledge/coder — the 'auto' guard strips them, which is what
     silently discarded native memory / Adaptive Memory injection.
  3. the coder entry is serialized under the SAME _GEN_LOCK the render pipelines use.

The real Pipe class is exercised; only the HTTP layer is faked, so message assembly and model
selection under test are production code.
"""
import asyncio, importlib.util, json, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"

spec = importlib.util.spec_from_file_location("auto_assistant_manifold_test", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CAPTURED = []


class FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def content(self):
        async def gen():
            for tok in ("ok", " done"):
                yield json.dumps({"message": {"content": tok}}).encode()
        return gen()


class FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, **k):
        CAPTURED.append(json)
        return FakeResp()


mod.aiohttp.ClientSession = FakeSession


async def run_entry(model_id, messages, doc_poison=False):
    """Drive the real pipe() for a given manifold model id; return (text, captured_payload)."""
    CAPTURED.clear()
    p = mod.Pipe()

    async def _status(*a, **k):
        return None
    p._status = _status

    body = {"model": model_id, "messages": messages}
    out = await p.pipe(body, __metadata__={"chat_id": "t", "user_prompt": messages[-1]["content"]},
                       __event_emitter__=None)
    if hasattr(out, "__aiter__"):
        text = "".join([t async for t in out])
    else:
        text = str(out)
    return text, (CAPTURED[0] if CAPTURED else None)


IMAGE_REQ = [{"role": "user", "content": "make a picture of a cat"}]
WITH_SYS = [{"role": "system", "content": "MEMORY: the user's dog is called Rex."},
            {"role": "user", "content": "what is my dog called?"}]

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


async def main():
    print(f"Testing: {PIPE_PATH}\n")

    # --- 0. the manifold exposes exactly ONE entry ------------------------
    # knowledge/coder were removed: once 'auto' gained legacy FC, the memory filter and keep_system,
    # 'knowledge' was a strict subset of it, and 'coder' only forced a model that _is_code_request
    # now picks automatically. The ids below must still RESOLVE though, so chats saved against them
    # (and direct API callers) degrade gracefully instead of erroring.
    entries = mod.Pipe().pipes()
    check("pipes() exposes exactly one entry", len(entries) == 1, f"got {entries}")
    check("the entry is 'auto'", entries[0]["id"] == "auto", f"got {entries[0]}")

    # --- 1. entry parsing -------------------------------------------------
    p = mod.Pipe()
    for mid, want in [("auto_assistant.auto", "auto"),
                      ("auto_assistant.knowledge", "knowledge"),
                      ("auto_assistant.coder", "coder"),
                      ("knowledge", "knowledge"),
                      ("dolphin-venice:24b", "auto"),
                      ("", "auto"),
                      (None, "auto")]:
        got = p._entry({"model": mid})
        check(f"_entry({mid!r}) -> {want}", got == want, f"got {got!r}")
    check("_entry({}) -> auto", p._entry({}) == "auto")

    # --- 2. chat-only: a real image request must NOT render ---------------
    for entry in ("knowledge", "coder"):
        text, payload = await run_entry(f"auto_assistant.{entry}", IMAGE_REQ)
        check(f"{entry}: image request chats instead of rendering",
              text == "ok done" and payload is not None, f"text={text!r}")

    # 'auto' must still render it — proves the bypass is entry-scoped, not a global disable.
    p2 = mod.Pipe()

    async def _tracked(emitter, verb, coro):
        coro.close()
        return (verb, 0.0)

    async def _finish(emitter, result, verb, elapsed, detail):
        return f"MEDIA[{result}]"
    p2._tracked, p2._finish = _tracked, _finish
    got = await p2.pipe({"model": "auto_assistant.auto", "messages": IMAGE_REQ},
                        __metadata__={"chat_id": "t", "user_prompt": IMAGE_REQ[0]["content"]})
    check("auto: image request still renders", got == "MEDIA[Generating image]", f"got {got!r}")

    # --- 3. system message survival ---------------------------------------
    # All three entries now preserve system messages. 'auto' was changed deliberately so OpenWebUI's
    # native memory and system prompts reach it too — see AUTO_KEEP_SYSTEM. Routing is unaffected:
    # system messages are never part of the routing text (test_router.py covers that).
    for entry, want_kept in (("knowledge", True), ("coder", True), ("auto", True)):
        _t, payload = await run_entry(f"auto_assistant.{entry}", WITH_SYS)
        sys_msgs = [m for m in payload["messages"] if m["role"] == "system"]
        kept = any("Rex" in m["content"] for m in sys_msgs)
        check(f"{entry}: system message {'preserved' if want_kept else 'stripped'}",
              kept == want_kept, f"system msgs={[m['content'][:40] for m in sys_msgs]}")

    # --- 3b. system messages must be MERGED into exactly one ---------------
    # hermes-genesis:apex-compact fails the whole request with HTTP 400 ("Unable to generate parser
    # for this template") when sent more than one system message. keep_system=True naturally produces
    # two — our guard plus OpenWebUI's memory/context — so without merging, every turn carrying a
    # memory or a system convention breaks. Measured: 1 works, 2 is a hard 400.
    _t, payload = await run_entry("auto_assistant.auto", WITH_SYS)
    sys_msgs = [m for m in payload["messages"] if m["role"] == "system"]
    check("exactly ONE system message is sent", len(sys_msgs) == 1,
          f"sent {len(sys_msgs)}: {[m['content'][:30] for m in sys_msgs]}")
    check("...and the merge keeps the guard", "NEVER output JSON" in sys_msgs[0]["content"])
    check("...and keeps the caller's context", "Rex" in sys_msgs[0]["content"])
    check("...guard first, caller's context after",
          sys_msgs[0]["content"].index("NEVER output JSON") < sys_msgs[0]["content"].index("Rex"))

    # --- 4. model selection ------------------------------------------------
    _t, payload = await run_entry("auto_assistant.coder", WITH_SYS)
    check("coder: uses coder_model", payload["model"] == mod.Pipe().coder_model,
          f"got {payload['model']!r}")
    _t, payload = await run_entry("auto_assistant.knowledge", WITH_SYS)
    check("knowledge: uses chat_model", payload["model"] == mod.Pipe().chat_model,
          f"got {payload['model']!r}")

    # --- 5. guards are entry-specific -------------------------------------
    _t, payload = await run_entry("auto_assistant.coder", WITH_SYS)
    guard = payload["messages"][0]["content"]
    check("coder: guard does NOT forbid JSON", "NEVER output JSON" not in guard)
    check("coder: guard is the coding one", "programming assistant" in guard)
    _t, payload = await run_entry("auto_assistant.auto", WITH_SYS)
    check("auto: guard unchanged (still forbids JSON)",
          "NEVER output JSON" in payload["messages"][0]["content"])

    # --- 6. _GEN_LOCK is really held for coder, and released after --------
    check("lock free before", not mod._GEN_LOCK.locked())
    p3 = mod.Pipe()
    gen = p3._entry_chat_stream("coder", [{"role": "user", "content": "hi"}])
    await gen.__anext__()
    check("coder: _GEN_LOCK HELD during stream", mod._GEN_LOCK.locked())
    async for _ in gen:
        pass
    check("coder: _GEN_LOCK released after stream", not mod._GEN_LOCK.locked())

    # knowledge must NOT take the lock (it reuses already-warm small models)
    gen = p3._entry_chat_stream("knowledge", [{"role": "user", "content": "hi"}])
    await gen.__anext__()
    check("knowledge: _GEN_LOCK NOT held", not mod._GEN_LOCK.locked())
    async for _ in gen:
        pass

    # a client that disconnects mid-stream must not leak the lock
    gen = p3._entry_chat_stream("coder", [{"role": "user", "content": "hi"}])
    await gen.__anext__()
    await gen.aclose()
    check("coder: _GEN_LOCK released on early disconnect", not mod._GEN_LOCK.locked())

    fails = 0
    for name, ok, detail in results:
        fails += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok and detail:
            print(f"          {detail}")
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

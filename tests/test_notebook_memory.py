#!/usr/bin/env python3
"""Notebook mode remembers the notebook a chat settled on.

Why this file exists. Notebook mode resolves the notebook from the user's own words, and when the
words name none it asks. That is right for the first turn and wrong for every turn after it: once a
chat is clearly about "Islamic guidance", being asked which notebook you meant — again, and again —
reads as though the assistant forgot the conversation between one message and the next.

So a settled notebook is recorded per chat and reused. Two things make that safe, and both are
checked here because both fail SILENTLY:

  * **Contamination.** A memory read back in the wrong chat answers from the wrong book while the
    interface says otherwise. That is the one failure this mode must not have, so the store is keyed
    on the chat id AND the user handle, and there is deliberately no "most recent notebook"
    fallback — a missing chat id means NO memory, never someone else's.
  * **A notebook that has gone.** It can be deleted in Open Notebook between two turns. Answering
    from it is impossible and asserting it anyway would be a fabrication, so a remembered notebook
    is re-resolved against the live list every turn and falls back to asking when it is gone.

The other half is that this memory must OUTLIVE a deploy — the in-memory pending dict beside it
does not, and losing the memory that way is most of the annoyance. It is a file on the data volume,
so the second turn here runs on a FRESH Pipe instance, which is what a deploy produces.

Usage:  python3 tests/test_notebook_memory.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipes", "shared"))

# Read at import time by the pipe, so it must be set before the module loads.
_MEM = tempfile.NamedTemporaryFile(prefix="nbmem-", suffix=".json", delete=False)
_MEM.close()
os.environ["NOTEBOOK_MEMORY"] = _MEM.name

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "pipes", "live",
                                                               "auto_assistant.py")

import aiohttp as aiohttp_real  # noqa: E402

spec = importlib.util.spec_from_file_location("aa_nbmem", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
nbr = mod.nbr

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


NBS = [
    {"id": "notebook:isl", "name": "Islamic guidance", "description": "", "source_count": 1},
    {"id": "notebook:tec", "name": "technical", "description": "", "source_count": 2},
    {"id": "notebook:emp", "name": "empty one", "description": "", "source_count": 0},
]
ASK = []
ROWS = {"value": list(NBS)}
ANSWERED = "ANSWER FROM THE NOTEBOOK"   # what the stubbed Open Notebook returns


class _Content:
    def __init__(self, lines):
        self._it = iter(lines)

    def __aiter__(self):
        async def gen():
            for line in self._it:
                yield line
        return gen()


class _Resp:
    status = 200

    def __init__(self, lines):
        self.content = _Content(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "{}"


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **kw):
        ASK.append(kw.get("json") or {})
        body = json.dumps({"type": "final_answer", "content": "ANSWER FROM THE NOTEBOOK"})
        return _Resp([b"data: " + body.encode()])


class _Aiohttp:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, k):
        return getattr(self._real, k)

    def ClientSession(self, *a, **kw):
        return _Session()


def stack():
    p = mod.Pipe()
    ASK.clear()
    ROWS["value"] = list(NBS)
    mod.aiohttp = _Aiohttp(aiohttp_real)
    nbr.list_notebooks = lambda *a, **k: list(ROWS["value"])
    nbr.default_models = lambda *a, **k: ["model:chat", "model:chat", "model:chat"]
    nbr.probe_scope_support = lambda *a, **k: True
    nbr.list_source_titles = lambda *a, **k: []
    return p


def say(p, text, cid="c1", handle="ohmz"):
    """One full pipe() turn, in Notebook mode, as a given user."""
    p._chat_id = lambda *a, **k: cid
    p._nb_cache.clear()
    p._alert_username = staticmethod(lambda u: handle) if handle else mod.Pipe._alert_username

    async def emit(ev):
        pass

    async def go():
        res = await p.pipe({"messages": [{"role": "user", "content": text}],
                            "model": "auto_assistant.auto"},
                           __metadata__={"user_prompt": text, "chat_id": cid,
                                         "filter_ids": [mod.NOTEBOOK_MODE_ID]},
                           __event_emitter__=emit,
                           __user__={"email": f"{handle}@x.com"} if handle else None)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    return asyncio.run(go())


def reset():
    try:
        os.remove(_MEM.name)
    except FileNotFoundError:
        pass


def main():
    print("--- a settled notebook is reused instead of re-asked ---")
    reset()
    out = say(stack(), "check islamic guidance to answer this: what is zakat?")
    check("turn 1 answers from the named notebook", ANSWERED in out, repr(out[:160]))
    check("turn 1 asked Open Notebook about that notebook",
          ASK and ASK[-1].get("notebook_id") == "notebook:isl", repr(ASK[-1:]))

    # A FRESH Pipe, same chat: exactly what a deploy leaves behind.
    ASK.clear()
    out = say(stack(), "what does it say about charity?")
    check("turn 2 (fresh pipe) does not re-ask", "name the notebook" not in out.lower(), repr(out[:200]))
    check("turn 2 answers from the remembered notebook",
          ASK and ASK[-1].get("notebook_id") == "notebook:isl", repr(ASK[-1:]))
    check("turn 2 passes the user's own question through",
          ASK and "charity" in (ASK[-1].get("question") or ""), repr(ASK[-1:]))

    print("\n--- naming a different notebook wins, and replaces the memory ---")
    ASK.clear()
    out = say(stack(), "check technical to answer this: what is nginx?")
    check("explicit name is honoured", ASK and ASK[-1].get("notebook_id") == "notebook:tec",
          repr(ASK[-1:]))
    ASK.clear()
    say(stack(), "and what about caching?")
    check("the new notebook is what is remembered",
          ASK and ASK[-1].get("notebook_id") == "notebook:tec", repr(ASK[-1:]))

    # ANSWERED is the assertion that matters: the asking prompts all mention notebooks by name, so
    # a substring check on the word "notebook" passes for the wrong reason.
    print("\n--- contamination: another chat, and another user ---")
    ASK.clear()
    out = say(stack(), "what does it say about charity?", cid="c2")
    check("a DIFFERENT chat is not answered from c1's notebook", ANSWERED not in out, repr(out[:200]))
    check("it asks instead", "notebook" in out.lower(), repr(out[:200]))

    ASK.clear()
    out = say(stack(), "what does it say about charity?", cid="c1", handle="someoneelse")
    check("a DIFFERENT user on the same chat id is not answered", ANSWERED not in out, repr(out[:200]))

    print("\n--- a notebook deleted since must fall back to asking ---")
    ASK.clear()
    p = stack()
    ROWS["value"] = [n for n in NBS if n["id"] != "notebook:tec"]   # after stack(): it resets ROWS
    out = say(p, "and what about caching?")
    check("deleted notebook is not answered from", ANSWERED not in out, repr(out[:200]))
    check("the user is asked again instead", "notebook" in out.lower(), repr(out[:200]))
    check("no request was made against the notebook that is gone",
          not ASK or ASK[-1].get("notebook_id") != "notebook:tec", repr(ASK[-1:]))

    print("\n--- a notebook with no sources is never remembered ---")
    reset()
    ASK.clear()
    say(stack(), "check empty one to answer this: anything?")
    check("unanswerable notebook was not stored", mod.Pipe()._nb_recall("c1", "ohmz") is None,
          repr(mod.Pipe()._nb_recall("c1", "ohmz")))

    print("\n--- the store itself ---")
    reset()
    p = mod.Pipe()
    check("empty store answers nothing", p._nb_recall("cx", "ohmz") is None)
    p._nb_remember("cx", "ohmz", {"id": "n1", "name": "N", "source_count": 2})
    check("round trip", p._nb_recall("cx", "ohmz") == {"id": "n1", "name": "N"},
          repr(p._nb_recall("cx", "ohmz")))
    check("cross-chat read is refused", p._nb_recall("cy", "ohmz") is None)
    check("cross-user read is refused", p._nb_recall("cx", "other") is None)
    check("no chat id means no memory", p._nb_recall(None, "ohmz") is None
          and p._nb_recall("", "ohmz") is None)

    fails = sum(1 for r in results if not r)
    print(f"\n{len(results) - fails}/{len(results)} checks passed")
    os.unlink(_MEM.name) if os.path.exists(_MEM.name) else None
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

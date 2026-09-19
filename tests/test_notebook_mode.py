#!/usr/bin/env python3
"""Notebook mode, end to end through pipe().

Why this file exists. The feature's promise is narrow and absolute: when the Notebook control is
on, the turn is answered from the notebook the user named — and when anything about that is not
possible, the user is told, loudly. The failure this file exists to make impossible is the quiet
one: answering from the whole knowledge base (or from the chat model) while the interface says
"Islamic guidance".

So the checks below are mostly about REFUSAL, and about the fences between this mode and the
three that already exist:

  * with the control ON, every message goes to the notebook path — including ones the media and
    coder tiers would otherwise have swallowed, and including OpenWebUI's internal prompts, which
    must still be answered without touching Open Notebook at all;
  * a notebook with zero sources is refused BEFORE any request (measured: asking one returns a
    confident, fabricated answer citing a different notebook's source);
  * a server that ignores a notebook scope is refused rather than trusted (the released Open
    Notebook image does exactly this — see docs/NOTEBOOK_MODE.md);
  * the confirmation flow answers the ORIGINAL question once the notebook is settled, and never
    silently drops it;
  * with the control OFF, routing is exactly what it was — including for a message that names a
    notebook, which must reach the chat model and nothing else.

Fully offline: the resolver's HTTP layer, the chat model and every media path are stubbed, so a
turn that escapes to any of them is a loud failure rather than a real call.

Usage:  python3 tests/test_notebook_mode.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The pipe loads its sidecars from the container's data mount. Do the same here, from the repo,
# so `import notebook_resolver` inside the pipe resolves and `mod.nbr` is the real module — the
# checks below then patch THAT object, which is the one the pipe actually calls.
sys.path.insert(0, os.path.join(ROOT, "pipes", "shared"))

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
FILTER_PATH = "/home/ohmz/ai-stack/filters/notebook_mode.py"
OWUI_DB = "/volume1/docker/openwebui/config/webui.db"

spec = importlib.util.spec_from_file_location("aa_nb", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
nbr = mod.nbr

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


NBS = [
    {"id": "notebook:isl", "name": "Islamic guidance", "description": "", "source_count": 1},
    {"id": "notebook:tec", "name": "techincal",        "description": "", "source_count": 2},
    {"id": "notebook:his", "name": "historical",       "description": "", "source_count": 0},
    {"id": "notebook:fic", "name": "Fictional",        "description": "", "source_count": 2},
]

EVENTS = []    # every event the pipe emitted to the client
ASK = []       # every ask payload the pipe posted
CHAT = []      # every escape to the chat model
MEDIA = []     # every render attempt
METRICS = []
SRC_TITLES = {
    "notebook:isl": ["The Barakah Effect.pdf"],
    "notebook:tec": ["nginx in practice.pdf", "TCP/IP illustrated.pdf"],
}
PROBE = {"value": True}     # what the scope probe reports
NB_ROWS = {"value": list(NBS)}


class _Content:
    def __init__(self, lines, exc=None):
        self._it = iter(lines)
        self._exc = exc

    def __aiter__(self):
        async def gen():
            for line in self._it:
                yield line
            if self._exc is not None:
                raise self._exc
        return gen()


class _Resp:
    def __init__(self, lines, status=200, exc=None, body=b"{}"):
        self.status, self.content, self._body = status, _Content(lines, exc), body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body.decode()


class _Session:
    def __init__(self, state):
        self._s = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **kw):
        ASK.append(kw.get("json") or {})
        return _Resp(self._s["lines"], self._s["status"], self._s["exc"], self._s["body"])


class _Aiohttp:
    """Proxies the real aiohttp except for ClientSession, so ClientTimeout and the exception
    types the pipe catches keep working."""

    def __init__(self, real, state):
        self._real, self._s = real, state

    def __getattr__(self, k):
        return getattr(self._real, k)

    def ClientSession(self, *a, **kw):
        return _Session(self._s)


def sse(final="Zakat is 2.5% of qualifying wealth [source:abc123].",
        frames=("strategy", "answer"), error=None):
    out = []
    if "strategy" in frames:
        out.append(b'data: {"type": "strategy", "reasoning": "SECRET REASONING", '
                   b'"searches": [{"term": "zakat"}]}')
    if "answer" in frames:
        out.append(b'data: {"type": "answer", "content": "INTERMEDIATE ANSWER"}')
    if error:
        out.append(b"data: " + json.dumps({"type": "error", "message": error}).encode())
    else:
        out.append(b"data: " + json.dumps({"type": "final_answer", "content": final}).encode())
        out.append(b"data: " + json.dumps({"type": "complete", "final_answer": final}).encode())
    return out


def stack(status=200, lines=None, exc=None, body=b"{}", probe=True, models=None, cites=None):
    p = mod.Pipe()
    ASK.clear(); CHAT.clear(); MEDIA.clear(); METRICS.clear(); EVENTS.clear()
    PROBE["value"] = probe
    NB_ROWS["value"] = list(NBS)

    mod.aiohttp = _Aiohttp(aiohttp_real, {"lines": lines if lines is not None else sse(),
                                          "status": status, "exc": exc, "body": body})

    # The resolver's HTTP layer, replaced wholesale — nothing here may reach 127.0.0.1:5055.
    nbr.list_notebooks = lambda *a, **k: list(NB_ROWS["value"])
    nbr.default_models = lambda *a, **k: (models if models is not None
                                          else ["model:chat", "model:chat", "model:chat"])
    nbr.probe_scope_support = lambda *a, **k: PROBE["value"]
    nbr.resolve_citations = lambda *a, **k: (cites if cites is not None else [])
    nbr.list_source_titles = lambda base, auth, nb_id, **k: SRC_TITLES.get(nb_id, [])

    def chat(*a, **kw):
        CHAT.append(True)

        async def go():
            yield "[chat]"
        return go()

    def media(*a, **kw):
        MEDIA.append(True)

        async def go():
            return "![stub](data:,)"
        return go()

    p._achat_stream = chat
    for name in ("_gen_and_cache", "_gen_video_and_cache", "_gen_i2v_and_cache",
                 "_gen_multishot_and_cache"):
        setattr(p, name, media)
    p._metric = lambda **f: METRICS.append(f)
    return p


import aiohttp as aiohttp_real  # noqa: E402  (imported before stack() is first called)


def say(p, text, on=True, cid="c1", task=None, stamp=False):
    """One full pipe() turn. `on` rides in metadata.filter_ids, as the filter's toggle does."""
    p._chat_id = lambda *a, **k: cid
    p._nb_cache.clear()   # the stubs may have changed between turns
    md = {"user_prompt": text, "chat_id": cid}
    if on:
        md["filter_ids"] = [mod.NOTEBOOK_MODE_ID]
    if stamp:
        md["notebook_mode"] = True

    async def emit(ev):
        EVENTS.append(ev)

    async def go():
        res = await p.pipe({"messages": [{"role": "user", "content": text}],
                            "model": "auto_assistant.auto"},
                           __metadata__=md, __event_emitter__=emit, __task__=task)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    return asyncio.run(go())


def main():
    print("--- ON: the turn is claimed, whatever the message looks like ---")
    for t in ["draw a cat", "write me a python script to sort a list", "what's the weather"]:
        p = stack()
        out = say(p, t)
        check(f"ON: {t[:38]!r} reaches the notebook path, not media/chat",
              not CHAT and not MEDIA, f"chat={CHAT} media={MEDIA} out={out[:60]!r}")

    print("\n--- ON: the resolution actually decides the notebook ---")
    p = stack()
    say(p, "check islamic guidance to answer this question: what does zakat mean?")
    check("the resolved notebook id is what gets asked",
          bool(ASK) and ASK[-1].get("notebook_id") == "notebook:isl",
          str(ASK[-1].get("notebook_id") if ASK else None))
    check("the instruction is stripped, the question is sent",
          bool(ASK) and ASK[-1].get("question") == "what does zakat mean?",
          str(ASK[-1].get("question") if ASK else None))
    check("all three model ids are sent",
          bool(ASK) and all(ASK[-1].get(k) for k in
                            ("strategy_model", "answer_model", "final_answer_model")))

    p = stack()
    say(p, "check the technical book on how do i configure nginx")
    check("the typo'd name resolves to the right notebook",
          bool(ASK) and ASK[-1].get("notebook_id") == "notebook:tec",
          str(ASK[-1].get("notebook_id") if ASK else None))

    print("\n--- ON: the answer and its citations ---")
    p = stack(cites=[{"kind": "source", "id": "source:abc123", "title": "The Barakah Effect.pdf"}])
    out = say(p, "check islamic guidance on zakat")
    check("the final answer is returned", "Zakat is 2.5%" in out, out[:80])
    check("scaffolding never reaches the user",
          "SECRET REASONING" not in out and "INTERMEDIATE ANSWER" not in out, out[:80])
    check("markers become footnote numbers", "[1]" in out and "source:abc123" not in out, out[-60:])
    check("a Sources footer is appended", "**Sources**" in out and "The Barakah Effect.pdf" in out)

    p = stack(cites=[])
    out = say(p, "check islamic guidance on zakat")
    check("an unresolvable citation is dropped, not rendered as a dead link",
          "**Sources**" not in out, out[-60:])

    print("\n--- ON: refusals (the failure this feature must never have is a quiet one) ---")
    p = stack()
    out = say(p, "check my historical notes on rome")
    check("a zero-source notebook is refused", "no sources" in out, out[:90])
    check("...without asking Open Notebook at all", not ASK, f"ask={ASK}")

    p = stack(probe=False)
    out = say(p, "check islamic guidance on zakat")
    check("scope-unsupported is refused, and explains why",
          not ASK and "scope" in out.lower(), out[:110])
    check("...and does NOT fall through to the chat model", not CHAT, f"chat={CHAT}")

    p = stack(probe=True)
    nbr.list_notebooks = lambda *a, **k: (_ for _ in ()).throw(
        nbr.NotebookError("down"))
    out = say(p, "check islamic guidance on zakat")
    check("Open Notebook unreachable is refused", "not reachable" in out.lower() or "isn't" in out, out[:90])
    check("...not answered by the local model", not CHAT)

    p = stack(probe=True, models=None)
    nbr.default_models = lambda *a, **k: None
    out = say(p, "check islamic guidance on zakat")
    check("no configured model is refused with a pointer", "no chat model" in out.lower(), out[:90])
    check("...and no ask was attempted", not ASK)

    p = stack(status=401, body=b'{"detail":"nope"}')
    out = say(p, "check islamic guidance on zakat")
    check("a 401 names the thing to set", "OPEN_NOTEBOOK_PASSWORD" in out, out[:110])

    p = stack(status=500, body=b'{"detail":"boom"}')
    out = say(p, "check islamic guidance on zakat")
    check("a 500 surfaces the server's own detail", "boom" in out, out[:110])

    p = stack(lines=sse(error="no embedding model configured"))
    out = say(p, "check islamic guidance on zakat")
    check("an error frame surfaces its message", "no embedding model" in out, out[:110])

    print("\n--- ON: the confirmation flow ---")
    p = stack()
    out = say(p, "check the historical fiction notebook on what happened in rome")
    check("an ambiguous name asks instead of guessing", not ASK and "historical" in out and "Fictional" in out,
          out[:110])
    out2 = say(p, "the second one")
    check("the reply selects the second candidate",
          bool(ASK) and ASK[-1].get("notebook_id") == "notebook:fic",
          str(ASK[-1].get("notebook_id") if ASK else None))
    check("...and the ORIGINAL question is the one asked",
          bool(ASK) and "rome" in (ASK[-1].get("question") or ""),
          str(ASK[-1].get("question") if ASK else None))

    p = stack()
    say(p, "check the historical fiction notebook on what happened in rome")
    out = say(p, "cancel")
    check("cancel clears it", "nothing asked" in out.lower(), out[:60])
    out = say(p, "the second one")
    check("...and a later reply cannot resurrect the parked question",
          "closest" in out.lower() or "couldn't tell" in out.lower(), out[:80])

    p = stack()
    say(p, "check the historical fiction notebook on what happened in rome")
    out = say(p, "banana")
    check("an unresolvable reply re-asks and keeps the question",
          "still holding" in out.lower() and "rome" in out, out[:130])

    p = stack()
    say(p, "check the historical fiction notebook on what happened in rome", cid="c1")
    out = say(p, "the second one", cid="c2")
    check("pending state does not leak across chats",
          "closest" in out.lower() or "couldn't tell" in out.lower(), out[:80])

    p = stack()
    out = say(p, "check islamic guidance")
    check("naming a notebook with no question asks what to ask", "what would you like" in out.lower(), out[:80])
    out = say(p, "what does zakat mean?")
    check("...and the next message becomes the question",
          bool(ASK) and ASK[-1].get("question") == "what does zakat mean?",
          str(ASK[-1].get("question") if ASK else None))

    print("\n--- ON: the catalogue — what is in here, and how do I ask about it ---")
    p = stack()
    out = say(p, "what kind of books are there")
    check("a catalogue question lists the notebooks",
          "Islamic guidance" in out and "techincal" in out, out[:130])
    check("...with the real source titles", "The Barakah Effect.pdf" in out, out[:160])
    # "→ say:" is in the catalogue and in NOTHING else, so these cannot pass by accident on the
    # fallback message, which also happens to list the notebook names.
    check("...and shows a phrasing that works",
          "\u2192 say:" in out and "check islamic guidance to answer this question" in out, out[-160:])
    check("...and is not the \"couldn't tell\" fallback", "couldn't tell" not in out, out[:80])
    check("...and asks Open Notebook nothing at all", not ASK, f"ask={ASK}")
    check("...flagging the empty notebooks as unusable",
          "no sources yet" in out)

    p = stack()
    out = say(p, "list the books in islamic guidance")
    check("naming a notebook scopes the catalogue to it",
          "The Barakah Effect.pdf" in out and "nginx" not in out, out[:130])

    for t in ["what can i ask", "how do i ask a question", "help", "which notebook should i use"]:
        p = stack()
        out = say(p, t)
        check(f"catalogue: {t[:34]!r} is answered directly",
              "\u2192 say:" in out and "couldn't tell" not in out and not ASK, out[:90])

    # The regression that matters: naming a notebook while saying "book" must reach the notebook,
    # not the catalogue. "book" is in ordinary questions constantly.
    p = stack()
    say(p, "check islamic guidance on what the book says about charity")
    check("a named notebook + the word 'book' still asks the notebook",
          bool(ASK) and ASK[-1].get("notebook_id") == "notebook:isl" and "The Barakah" not in "",
          str(ASK[-1].get("question") if ASK else None))

    print("\n--- ON: follow-up chips offered with the catalogue ---")
    p = stack()
    say(p, "what kind of books are there")
    fps = [e for e in EVENTS if e.get("type") == "chat:message:follow_ups"]
    check("the catalogue offers follow-up chips", bool(fps), str(EVENTS)[:200])
    chips = (fps[0]["data"]["follow_ups"] if fps else [])
    check("...as several questions", len(chips) >= 2, str(chips))
    # The property that matters: a chip that does not name a notebook is a dead end — the user
    # clicks it and the mode says it cannot tell which notebook they meant.
    for c in chips:
        r = nbr.resolve(c, NBS)
        check(f"chip names a real notebook: {c[:46]!r}", r.kind == "answer",
              f"kind={r.kind} top={r.rows[0].name if r.rows else None}")
    check("...and never offers an empty notebook",
          not any(c.lower().find("historical") >= 0 or c.lower().find("fictional") >= 0
                  for c in chips), str(chips))

    p = stack()
    say(p, "help")
    check("chips come with the other catalogue phrasings too",
          any(e.get("type") == "chat:message:follow_ups" for e in EVENTS))

    print("\n--- ON: suggestions when nothing is named ---")
    p = stack()
    out = say(p, "whats the weather")
    check("nothing named offers the closest and asks", not ASK and "closest" in out.lower(), out[:100])

    print("\n--- ON: OpenWebUI's own prompts never reach Open Notebook ---")
    p = stack()
    say(p, "### Task: suggest a title for this chat", task="title_generation")
    check("the task guard still wins", not ASK, f"ask={ASK}")

    p = stack()
    out = say(p, "check islamic guidance on zakat", stamp=True, on=False)
    check("the filter's stamp alone enters the mode", bool(ASK), f"ask={len(ASK)}")

    print("\n--- OFF: routing is exactly what it was (regression fence) ---")
    for t, want in [("draw a cat", "media"), ("write me a python script to sort a list", "chat"),
                    ("what's the weather", "chat"),
                    ("check islamic guidance to answer this question", "chat")]:
        p = stack()
        say(p, t, on=False, stamp=False)
        got = "media" if MEDIA else "chat" if CHAT else "notebook" if ASK else "other"
        check(f"OFF: {t[:44]!r} still routes to {want}", got == want, f"got {got}")

    print("\n--- the id is pinned on both sides ---")
    src = open(FILTER_PATH).read()
    check("filter and pipe agree on NOTEBOOK_MODE_ID",
          f'NOTEBOOK_MODE_ID = "{mod.NOTEBOOK_MODE_ID}"' in src, mod.NOTEBOOK_MODE_ID)
    check("the filter is a toggle", "\ntoggle = True" in src)
    check("the frontmatter starts on line 1", src.startswith('"""\ntitle: Notebook'))
    ns = {}
    exec(compile(src, FILTER_PATH, "exec"), ns)
    check("the INSTANCE exposes toggle and icon (module level alone is invisible to OWUI)",
          hasattr(ns["Filter"](), "toggle") and getattr(ns["Filter"](), "toggle") is True)
    inlet = src.split("def inlet")[1].split("return body")[0]
    code = "\n".join(l for l in inlet.splitlines() if not l.strip().startswith("#"))
    check("inlet clears the other modes and never touches messages",
          "body[\"features\"]" in src and "messages" not in code,
          "a reference outside a comment means the signal leaked into the prompt")

    if os.path.exists(OWUI_DB):
        import sqlite3
        db = sqlite3.connect(f"file:{OWUI_DB}?mode=ro", uri=True)
        # filterIds live on the MODEL row (auto_assistant.auto), not on the Function row: the
        # filter can be installed, active and green across every other check while being attached
        # to nothing, which produces no control and no error anywhere.
        row = db.execute("select meta from model where id='auto_assistant.auto'").fetchone()
        if row:
            meta = json.loads(row[0] or "{}")
            ids = meta.get("filterIds") or []
            check("attached to the Assistant model", mod.NOTEBOOK_MODE_ID in ids, str(ids))
        else:
            check("auto_assistant.auto model row exists", False, "no row in webui.db")
        frow = db.execute("select is_active from function where id='notebook_mode'").fetchone()
        check("the filter row is installed and active", bool(frow and frow[0]), str(frow))
        db.close()

    fails = [r for r in results if not r]
    if fails:
        print("\nNotebook mode can answer from the wrong place. Fix before deploying.")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

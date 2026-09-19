#!/usr/bin/env python3
"""Follow-up chips generated from the conversation, in the idiom of the mode in play.

Why this file exists. Open WebUI renders a `chat:message:follow_ups` event as suggestion chips
under a reply. Its own generator would fill that slot, but on this stack it cannot: the configured
task model (`gemma3:1b`) has an inactive row, so the task falls through to this pipe, whose
`__task__` guard answers in prose where the generator expects JSON. The slot has therefore been
empty, and the pipe fills it instead — which is what makes the chips mode-aware, and is the whole
reason for generating them here rather than repairing the native path.

What is checked, and why each one fails silently in production:

  * **The event shape.** `FollowUps.svelte` takes `string[]` — unlike the landing-page pools, which
    are `{title, content}` objects. The wrong shape renders nothing at all, with no error anywhere.
  * **Parsing real output.** The format is one question per line, NOT the `{"follow_ups": [...]}`
    JSON Open WebUI's own generator asks for, and that is a measured decision: asked for exactly
    that JSON, gemma3:1b returned three concatenated objects with the first one malformed. The
    cases below are what it actually returns — numbering, bullets, a fence, a lead-in.
  * **The reply is in the prompt.** `msgs` does not contain the answer being generated, so a
    generator that forgets to append it suggests follow-ups to the PREVIOUS turn — plausible,
    wrong, and invisible.
  * **Failure is silent and harmless.** No model, a timeout, junk output: no chips, no exception
    into the reply. This decorates a reply that has already been delivered.

Usage:  python3 tests/test_follow_ups.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipes", "shared"))

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "pipes", "live",
                                                               "auto_assistant.py")
spec = importlib.util.spec_from_file_location("aa_fup", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []
EVENTS = []
SENT = []       # every prompt actually posted to the model
ASKED_MODELS = []
METRICS = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def pipe(reply="", status=200, raises=False):
    """A Pipe whose only model is the stubbed 1B. `reply` is its raw `response` text."""
    p = mod.Pipe()
    p._metric = lambda **f: METRICS.append(f)

    def post(url, **kw):
        body = kw.get("json") or {}
        SENT.append(body.get("prompt") or "")
        ASKED_MODELS.append(body.get("model"))
        if raises:
            raise RuntimeError("connection refused")
        return _Resp(status, {"response": reply})

    mod.requests.post = post
    return p


async def emit(ev):
    EVENTS.append(ev)


def run(coro):
    return asyncio.run(coro)


CONVO = [{"role": "user", "content": "how do I sort a list of dicts by key?"},
         {"role": "assistant", "content": "Use sorted(items, key=lambda d: d['k'])."}]
ANSWER = "You can also use operator.itemgetter for speed."


def main():
    print("--- the event the frontend actually renders ---")
    EVENTS.clear()
    p = pipe(reply="Add tests for the comparator\nWhat about a stable sort?")
    run(p._suggest_follow_ups(CONVO, ANSWER, emit, "code"))
    check("exactly one follow_ups event", len(EVENTS) == 1, repr(EVENTS))
    ev = EVENTS[0] if EVENTS else {}
    check("event type is chat:message:follow_ups",
          ev.get("type") == "chat:message:follow_ups", repr(ev.get("type")))
    ups = (ev.get("data") or {}).get("follow_ups")
    check("follow_ups is a list of plain strings (FollowUps.svelte takes string[])",
          isinstance(ups, list) and ups and all(isinstance(u, str) for u in ups), repr(ups))
    check("chips come back in order", ups == ["Add tests for the comparator",
                                              "What about a stable sort?"], repr(ups))

    print("\n--- the reply under discussion is in the prompt ---")
    check("the just-generated answer is included", ANSWER in SENT[-1], repr(SENT[-1][:200]))
    check("the conversation is included", "sort a list of dicts" in SENT[-1], repr(SENT[-1][:200]))
    check("the mode is described", "coding conversation" in SENT[-1], repr(SENT[-1][:200]))
    check("the model asked is the 1B", mod.FOLLOWUP_MODEL == mod.ROUTE_CLASSIFIER_MODEL,
          mod.FOLLOWUP_MODEL)

    print("\n--- modes put different words in the prompt ---")
    for mode, needle in (("notebook", "notebook of documents"), ("web", "web search"),
                         ("plain", "go deeper")):
        p = pipe(reply="one question here\ntwo question here")
        SENT.clear()
        run(p._suggest_follow_ups(CONVO, ANSWER, emit, mode))
        check(f"mode '{mode}' has its own instruction", needle in SENT[-1], repr(SENT[-1][:160]))

    print("\n--- parsing what a 1B actually returns ---")
    # Every case below was observed from gemma3:1b against a real conversation. The format is
    # one-question-per-line precisely because it could not hold a JSON contract: asked for
    # {"follow_ups": [...]} it returned three concatenated objects, the first malformed.
    cases = {
        "plain lines": "How do I handle duplicates?\nWhat if a list is empty?\nCan it be faster?",
        "numbered": "1. How do I handle duplicates?\n2. What if a list is empty?\n3. Can it be faster?",
        "bulleted": "- How do I handle duplicates?\n* What if a list is empty?\n• Can it be faster?",
        "a fence around them": "```\nHow do I handle duplicates?\nWhat if a list is empty?\nCan it be faster?\n```",
        "quoted": '"How do I handle duplicates?"\n"What if a list is empty?"',
    }
    for name, raw in cases.items():
        EVENTS.clear()
        run(pipe(reply=raw)._suggest_follow_ups(CONVO, ANSWER, emit, "plain"))
        ups = (EVENTS[0].get("data") or {}).get("follow_ups") if EVENTS else None
        want = ["How do I handle duplicates?", "What if a list is empty?"]
        check(f"{name} parses", ups[:2] == want, repr(ups))

    print("\n--- a lead-in is not a question (measured, in notebook and web chats) ---")
    for preamble in ("Okay, here are three short questions the user might ask next:",
                     "Sure! Here are some follow-ups:",
                     "Here are three questions:",
                     "Questions:",
                     "The following questions may help:"):
        EVENTS.clear()
        raw = f"{preamble}\nWhat is the nisab threshold?\nHow is zakat calculated?"
        run(pipe(reply=raw)._suggest_follow_ups(CONVO, ANSWER, emit, "notebook"))
        ups = (EVENTS[0].get("data") or {}).get("follow_ups") if EVENTS else None
        check(f"dropped: {preamble[:34]!r}",
              ups == ["What is the nisab threshold?", "How is zakat calculated?"], repr(ups))

    EVENTS.clear()
    p = pipe(reply="one question here\none question here\ntwo question here\nthree question here")
    run(p._suggest_follow_ups(CONVO, ANSWER, emit, "plain"))
    ups = (EVENTS[0].get("data") or {}).get("follow_ups")
    check("duplicates dropped and capped at 3", ups == ["one question here", "two question here",
                                                        "three question here"], repr(ups))

    EVENTS.clear()
    raw = "ok\nWhat really happens when the input is enormous and streaming?\n" + "x" * 200
    run(pipe(reply=raw)._suggest_follow_ups(CONVO, ANSWER, emit, "plain"))
    ups = (EVENTS[0].get("data") or {}).get("follow_ups")
    check("too-short and too-long lines are dropped",
          ups == ["What really happens when the input is enormous and streaming?"], repr(ups))

    print("\n--- every failure is silent, and costs only the chips ---")
    for name, kwargs in (("model absent / http error", {"status": 500}),
                         ("connection refused", {"raises": True}),
                         ("a refusal instead of questions", {"reply": "I cannot help with that."}),
                         ("empty reply", {"reply": ""}),
                         ("JSON instead of lines", {"reply": chr(123) + chr(34) + "follow_ups" + chr(34) + ": [" + chr(34) + "x" + chr(34) + "]" + chr(125)})):
        EVENTS.clear()
        METRICS.clear()
        p = pipe(**kwargs)
        try:
            run(p._suggest_follow_ups(CONVO, ANSWER, emit, "plain"))
            crashed = None
        except Exception as e:      # noqa: BLE001 — a crash here IS the failure under test
            crashed = e
        check(f"{name}: no exception escapes", crashed is None, repr(crashed))
        check(f"{name}: no chips emitted", EVENTS == [], repr(EVENTS))
        check(f"{name}: recorded in the metrics", any(m.get("job") == "follow_ups" for m in METRICS),
              repr(METRICS))

    print("\n--- when it is allowed to run at all ---")
    p = mod.Pipe()
    check("no emitter -> no suggester", p._suggester(CONVO, None, {}) is None)
    check("no messages -> no suggester", p._suggester([], emit, {}) is None)
    check("with both -> a suggester", callable(p._suggester(CONVO, emit, {})))

    print("\n--- the stream hands its reply over, and only on a clean finish ---")
    got = []

    async def drive(lines, status=200):
        """Run the REAL _achat_stream with the model stubbed, collecting what suggest() is given."""
        class _C:
            def __aiter__(self):
                async def gen():
                    for o in lines:
                        yield (json.dumps(o) + "\n").encode()
                return gen()

        class _R:
            def __init__(self):
                self.status = status
                self.content = _C()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def text(self):
                return "nope"

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _R()

        real = mod.aiohttp

        class _A:
            def __init__(self, r):
                self._r = r

            def __getattr__(self, k):
                return getattr(self._r, k)

            def ClientSession(self, *a, **kw):
                return _S()

        mod.aiohttp = _A(real)
        try:
            got.clear()
            p = mod.Pipe()
            async for _ in p._achat_stream([{"role": "user", "content": "hi"}], stream=True,
                                           suggest=lambda answer, model=None: got.append(
                                               (answer, model))):
                pass
        finally:
            mod.aiohttp = real

    run(drive([{"message": {"content": "Hel"}}, {"message": {"content": "lo"}},
               {"done": True, "eval_count": 2, "eval_duration": 10000000}]))
    check("suggest called once with the assembled reply",
          [a for a, _m in got] == ["Hello"], repr(got))
    check("...and with the tag that answered, so the warm model writes the chips",
          got and got[0][1] == mod.Pipe().chat_model, repr(got))

    run(drive([{"message": {"content": "Hel"}}, {"error": "model not found"}]))
    check("an errored stream never suggests", got == [], repr(got))

    run(drive([], status=500))
    check("an HTTP failure never suggests", got == [], repr(got))

    print("\n--- the WHOLE conversation goes in, not a window ---")
    long_convo = []
    for i in range(20):
        long_convo.append({"role": "user", "content": f"question number {i} about the pipeline"})
        long_convo.append({"role": "assistant", "content": f"answer number {i} about the pipeline"})
    SENT.clear()
    run(pipe(reply="a good question here\nanother good question")._suggest_follow_ups(
        long_convo, ANSWER, emit, "plain"))
    check("the very first turn is still in the prompt (no 6-message window)",
          "question number 0" in SENT[-1], "first turn missing")
    check("...and so is the last", "answer number 19" in SENT[-1], "last turn missing")
    check("every turn is present",
          all(f"question number {i}" in SENT[-1] for i in range(20)), "a turn was dropped")

    print("\n--- the log is formatted for a model, and trimmed to fit ---")
    p = mod.Pipe()
    log = p._followup_log([{"role": "system", "content": "SECRET GUARD"},
                           {"role": "user", "content": "hello there"},
                           {"role": "assistant", "content": "hi"}], "the answer")
    check("system messages are excluded", "SECRET GUARD" not in log, repr(log[:120]))
    check("roles are labelled", log.startswith("User: hello there"), repr(log[:120]))
    check("the new answer is appended", log.endswith("Assistant: the answer"), repr(log[-60:]))

    injected = ("### Task:\nRespond to the user query using the provided context\n<context>\n"
                + "web search result text " * 200 + "</context>what is the capital of France?")
    log = p._followup_log([{"role": "user", "content": injected}], "")
    check("injected web/RAG context is stripped from the log",
          "web search result text" not in log and "capital of France" in log, repr(log[:160]))

    # Oldest-first dropping, and the budget is the one _fit_ctx uses.
    huge = [{"role": "user", "content": f"OLD-TURN-{i} " + "x" * 40000} for i in range(12)]
    huge.append({"role": "user", "content": "NEWEST-TURN"})
    log = p._followup_log(huge, "")
    check("an oversized log drops the OLDEST turns, not the newest",
          "NEWEST-TURN" in log and "OLD-TURN-0" not in log, repr(log[:120]))
    check("the kept log fits the chat model's own window",
          len(log) / 3.2 <= mod.CTX_MAX, f"{len(log)/3.2:.0f} est. tokens")

    print("\n--- which model is asked ---")
    EVENTS.clear()
    SENT.clear()
    p = pipe(reply="a good question here\nanother good question")
    run(p._suggest_follow_ups(CONVO, ANSWER, emit, "code", "qwen38-coder:q4"))
    check("the answering tag is used when given", "qwen38-coder:q4" in ASKED_MODELS[-1],
          repr(ASKED_MODELS[-1:]))
    SENT.clear()
    run(pipe(reply="a good question here\nanother good question")._suggest_follow_ups(
        CONVO, ANSWER, emit, "notebook"))
    check("the 1B is the fallback when no model ran (notebook answers)",
          ASKED_MODELS[-1] == mod.FOLLOWUP_MODEL, repr(ASKED_MODELS[-1:]))

    print("\n--- a NOTEBOOK answer gets chips too (it returns before the chat path) ---")
    nbr = mod.nbr
    real_aiohttp = mod.aiohttp

    async def drive_nb(frames):
        """Run the real _nb_stream against a stubbed Open Notebook; report what it suggested."""
        seen = []

        async def record(msgs, answer, emitter, mode, model=None):
            seen.append({"q": msgs[-1]["content"] if msgs else None, "answer": answer,
                         "mode": mode, "model": model})

        class _C:
            def __aiter__(self):
                async def gen():
                    for f in frames:
                        yield b"data: " + json.dumps(f).encode()
                return gen()

        class _R:
            status = 200

            def __init__(self):
                self.content = _C()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def text(self):
                return "{}"

        class _S:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _R()

        class _A:
            def __getattr__(self, k):
                return getattr(real_aiohttp, k)

            def ClientSession(self, *a, **kw):
                return _S()

        mod.aiohttp = _A()
        nbr.resolve_citations = lambda *a, **k: []
        p = mod.Pipe()
        p._suggest_follow_ups = record
        try:
            async for _ in p._nb_stream("Islamic guidance", "notebook:isl", "what is zakat?",
                                        ["m", "m", "m"], emit):
                pass
            for _ in range(5):          # the kick is a task; let it run
                await asyncio.sleep(0)
        finally:
            mod.aiohttp = real_aiohttp
        return seen

    EVENTS.clear()
    seen = run(drive_nb([{"type": "final_answer", "content": "Zakat is 2.5%."}]))
    check("a notebook answer suggests follow-ups", len(seen) == 1, repr(seen))

    # The info button under a notebook answer. Open Notebook reports no token counts, so it
    # carries measured facts and says why there is no rate, rather than a fabricated one.
    stats = [e for e in EVENTS if e.get("type") == "chat:completion"]
    check("a notebook answer also emits generation stats", len(stats) == 1, repr(EVENTS))
    usage = (stats[0].get("data") or {}).get("usage") if stats else {}
    check("...naming the notebook and the duration",
          usage.get("answer") == "Islamic guidance" and str(usage.get("took", "")).endswith("s"),
          repr(usage))
    check("...and saying there is no tokens/s rather than inventing one",
          "no token" in str(usage.get("note", "")).lower() and "tokens/s" not in usage, repr(usage))
    if seen:
        check("...tagged as notebook mode", seen[0]["mode"] == "notebook", repr(seen[0]["mode"]))
        check("...carrying the question and the answer",
              seen[0]["q"] == "what is zakat?" and "2.5%" in seen[0]["answer"], repr(seen[0]))
        check("...and asking the 1B, since no Ollama model ran on this path",
              seen[0]["model"] is None, repr(seen[0]["model"]))

    seen = run(drive_nb([{"type": "error", "message": "boom"}]))
    check("a failed notebook answer suggests nothing", seen == [], repr(seen))

    seen = run(drive_nb([]))
    check("an empty notebook answer suggests nothing", seen == [], repr(seen))

    print("\n--- the mode is derived from the same signals routing used ---")
    p = mod.Pipe()
    check("notebook stamp wins", p._ui_mode({"filter_ids": [mod.NOTEBOOK_MODE_ID]}) == "notebook",
          p._ui_mode({"filter_ids": [mod.NOTEBOOK_MODE_ID]}))
    check("code interpreter feature",
          p._ui_mode({"features": {"code_interpreter": True}}) == "code")
    check("web search feature", p._ui_mode({"features": {"web_search": True}}) == "web")
    check("nothing on -> plain", p._ui_mode({}) == "plain")

    fails = sum(1 for r in results if not r)
    print(f"\n{len(results) - fails}/{len(results)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

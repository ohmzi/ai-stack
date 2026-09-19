#!/usr/bin/env python3
"""Generation stats under a reply — the `{"usage": ...}` dict `_achat_stream` yields.

Why this file exists. OpenWebUI renders an info button under a reply whenever `message.usage`
exists, and that button is the whole point of this change: it shows tokens/s on hover. The value
reaches it by a route that is easy to get subtly wrong, and every failure mode here is silent —
the reply still arrives, only the button is missing:

  * the terminal `done` line is the ONLY line carrying Ollama's timings, and it is also the line
    whose `message.content` is empty, so a loop that keeps only content drops it without a trace;
  * the dict must be YIELDED, not sent through the event emitter. An emitter event reaches the
    browser and nothing else — get_event_emitter has no DB-write branch for `chat:completion`, and
    the frontend no longer saves a completed chat either — so the figure would appear and then
    vanish on the next reload. Yielding makes process_line (functions.py) frame it as raw
    `data: {json}`, which is the shape middleware parses into `data['usage']`, attaches to the
    assistant message and persists;
  * that same yield is only safe while the caller is streaming. `get_message_content`
    (functions.py) serves a non-streaming caller with `''.join([str(s) async for s in res])`, so a
    dict yielded there is stringified straight into the reply text.

So the checks are: the dict appears exactly once when a timed stream completes; it carries a
correct rate; it does NOT appear when there are no timings, when the caller is not streaming, or
when the turn failed; and in no case does it alter the reply text.

Usage:  python3 tests/test_response_stats.py [pipe_path]
"""
import asyncio, importlib.util, json, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"

spec = importlib.util.spec_from_file_location("auto_assistant_stats_test", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

RESULTS = []
LINES = []          # NDJSON the fake Ollama streams back
STATUS = [200]      # HTTP status the fake Ollama returns


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


# Ollama reports durations in NANOseconds. 412 tokens over 9.82 s is 41.96 tok/s.
TIMED_DONE = {
    "done": True, "done_reason": "stop", "model": "test",
    "message": {"role": "assistant", "content": ""},
    "total_duration": 11400000000, "load_duration": 120000000,
    "prompt_eval_count": 1183, "prompt_eval_duration": 210000000,
    "eval_count": 412, "eval_duration": 9820000000,
}
BARE_DONE = {"done": True, "done_reason": "stop", "model": "test",
             "message": {"role": "assistant", "content": ""}}


class FakeResp:
    def __init__(self):
        self.status = STATUS[0]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "upstream said no"

    @property
    def content(self):
        async def gen():
            for obj in LINES:
                yield (json.dumps(obj) + "\n").encode()
        return gen()


class FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, **k):
        return FakeResp()


mod.aiohttp.ClientSession = FakeSession


def script(*objects, status=200):
    LINES[:] = list(objects)
    STATUS[0] = status


async def drive(stream, messages=None):
    """Run the REAL _achat_stream; return (text, [usage dicts], [every item])."""
    p = mod.Pipe()
    items = []
    async for item in p._achat_stream(messages or [{"role": "user", "content": "hi"}], stream=stream):
        items.append(item)
    text = "".join(i for i in items if isinstance(i, str))
    return text, [i for i in items if isinstance(i, dict)], items


HELLO = [{"message": {"role": "assistant", "content": "Hel"}},
         {"message": {"role": "assistant", "content": "lo"}}]


async def main():
    # --- 1. a timed stream yields the stats, exactly once, without touching the text ----
    script(*HELLO, TIMED_DONE)
    text, usages, items = await drive(stream=True)
    check("timed stream: reply text is unchanged", text == "Hello", repr(text))
    check("timed stream: exactly one usage dict", len(usages) == 1, f"got {len(usages)}")
    usage = usages[0].get("usage") if usages else None
    check("timed stream: yielded as {'usage': ...}", isinstance(usage, dict), repr(usages[:1]))
    if isinstance(usage, dict):
        check("timed stream: tokens/s is right", usage.get("tokens/s") == 41.96,
              repr(usage.get("tokens/s")))
        check("timed stream: prompt tokens/s is right", usage.get("prompt tokens/s") == 5633.33,
              repr(usage.get("prompt tokens/s")))
        check("timed stream: the headline rate comes first",
              list(usage)[:1] == ["tokens/s"], repr(list(usage)))
        check("timed stream: raw Ollama fields ride along",
              usage.get("eval_count") == 412 and usage.get("total_duration") == 11400000000,
              repr(usage))
        check("timed stream: usage dict is yielded last",
              items and isinstance(items[-1], dict), repr(items[-1:]))

    # --- 2. no timings must mean no button, not a zeroed one ---------------------------
    script(*HELLO, BARE_DONE)
    text, usages, items = await drive(stream=True)
    check("bare done: reply text is unchanged", text == "Hello", repr(text))
    check("bare done: nothing yielded", usages == [], repr(usages))

    # --- 3. the non-streaming guard ----------------------------------------------------
    script(*HELLO, TIMED_DONE)
    text, usages, items = await drive(stream=False)
    check("not streaming: nothing yielded", usages == [], repr(usages))
    check("not streaming: every item is a str (str(dict) would corrupt the reply)",
          all(isinstance(i, str) for i in items), repr([type(i).__name__ for i in items]))
    check("not streaming: text matches the streaming run", text == "Hello", repr(text))

    # --- 4. a failed turn reports the failure and no stats -----------------------------
    script({"message": {"role": "assistant", "content": "Hel"}}, {"error": "model not found"})
    text, usages, _ = await drive(stream=True)
    check("mid-stream error: nothing yielded", usages == [], repr(usages))
    check("mid-stream error: the user is told", "model not found" in text, repr(text))

    script(status=500)
    text, usages, _ = await drive(stream=True)
    check("HTTP 500: nothing yielded", usages == [], repr(usages))
    check("HTTP 500: the user is told", "Ollama HTTP 500" in text, repr(text))

    # --- 5. the dict survives the coder path's wrapper ---------------------------------
    # _locked_stream iterates the inner generator and re-yields; a dict that does not come out
    # the far side would leave the coder route — the fastest one, and the one most worth
    # measuring — with no stats at all.
    script(*HELLO, TIMED_DONE)
    p = mod.Pipe()
    seen = []
    async for item in p._locked_stream(p._achat_stream([{"role": "user", "content": "hi"}],
                                                       stream=True)):
        seen.append(item)
    check("coder path: usage survives _locked_stream",
          [i for i in seen if isinstance(i, dict)] != [], repr(seen))
    check("coder path: reply text is unchanged",
          "".join(i for i in seen if isinstance(i, str)) == "Hello", repr(seen))
    check("coder path: the GPU lock is released",
          not mod._GEN_LOCK.locked(), "lock still held")

    # --- 6. end to end through pipe(): the flag is threaded, not assumed ---------------
    async def run_pipe(stream):
        script(*HELLO, TIMED_DONE)
        p = mod.Pipe()
        p._is_code_request = lambda text: False       # keep the router offline and deterministic

        async def _status(*a, **k):
            return None
        p._status = _status
        out = await p.pipe({"model": "auto_assistant.auto", "stream": stream,
                            "messages": [{"role": "user", "content": "hey there"}]},
                           __metadata__={"chat_id": "t", "user_prompt": "hey there"},
                           __event_emitter__=None)
        items = [i async for i in out] if hasattr(out, "__aiter__") else [str(out)]
        return "".join(i for i in items if isinstance(i, str)), [i for i in items if isinstance(i, dict)]

    text, usages = await run_pipe(True)
    check("pipe(stream=True): stats reach the caller", len(usages) == 1, repr(usages))
    check("pipe(stream=True): reply text is unchanged", text == "Hello", repr(text))

    text, usages = await run_pipe(False)
    check("pipe(stream=False): nothing yielded", usages == [], repr(usages))
    check("pipe(stream=False): reply text is unchanged", text == "Hello", repr(text))

    # --- 7. the extractor itself --------------------------------------------------------
    f = mod.Pipe._usage_from_ollama
    check("extractor: no timing fields -> None", f({"done": True}) is None)
    check("extractor: zero durations -> no rate and no crash",
          f({"eval_count": 5, "eval_duration": 0, "total_duration": 1}) ==
          {"eval_count": 5, "total_duration": 1},
          repr(f({"eval_count": 5, "eval_duration": 0, "total_duration": 1})))
    check("extractor: a rate needs both halves",
          f({"eval_count": 50}) == {"eval_count": 50}, repr(f({"eval_count": 50})))

    fails = 0
    for name, ok, detail in RESULTS:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"   <- {detail}" if not ok and detail else ""))
        fails += 0 if ok else 1
    print(f"\n{len(RESULTS) - fails}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

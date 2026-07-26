#!/usr/bin/env python3
"""Live end-to-end QA: real Pipe, real Ollama, real responses, graded by a judge model.

Unlike test_router.py / test_manifold.py (which stub the model layer to test routing decisions),
this makes ACTUAL inference calls. It answers three questions the unit tests cannot:

  1. Does the single 🪄 Assistant entry pick the right model per turn, unaided?
  2. Is the response correct?
  3. Does keep_system=True really deliver system-message context to the model?

Model selection is observed by wrapping aiohttp at the transport layer, so what is recorded is the
payload the pipe genuinely sent to /api/chat — not what the code says it intends to send.

Every case runs on the one entry — model choice is the pipe's job, not the user's. Cases are ordered
to group same-model work together: OLLAMA_KEEP_ALIVE=60s on this host, so interleaving chat and coder
turns would pay a model load on nearly every case.

Usage:  python3 tests/qa_live.py [pipe_path]
"""
import asyncio, importlib.util, json, sys, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
OLLAMA = "http://localhost:11434"
JUDGE_MODEL = "gemma4:31b"          # loaded ONCE at the end to grade every captured response

spec = importlib.util.spec_from_file_location("aa_qa", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
aiohttp = mod.aiohttp

SENT = []           # payloads the pipe actually posted to Ollama


class SpySession(aiohttp.ClientSession):
    """Real session; records the JSON body of every POST before letting it through."""
    def post(self, url, **kw):
        if kw.get("json") is not None:
            SENT.append(kw["json"])
        return super().post(url, **kw)


mod.aiohttp.ClientSession = SpySession


# Expected models are read from the pipe, not hardcoded. Chat, code and vision were collapsed onto
# one tenant on 2026-07-26, so a literal tag here would assert nothing and would rot on the next swap.
_P = mod.Pipe()
CHAT_MODEL, CODER_MODEL = _P.chat_model, _P.coder_model

# (id, entry, messages, what a correct answer must contain/do)
CASES = [
    ("A1", "auto",
     [{"role": "user", "content": "What is the capital of Australia? Answer in one short sentence."}],
     CHAT_MODEL,
     "States that the capital of Australia is Canberra. Naming Sydney or Melbourne is WRONG."),

    ("A2", "auto",
     [{"role": "user", "content": "A train travels 60 km in 45 minutes. What is its average speed "
                                  "in km/h? Give the number."}],
     CHAT_MODEL,
     "Arrives at 80 km/h. Any other number is WRONG."),

    # The decisive test for the Phase 2 keep_system=True change: this fact exists nowhere in training
    # data, so a correct answer PROVES the system message reached the model.
    ("A3", "auto",
     [{"role": "system", "content": "Known facts about this user: their bicycle is a matte green "
                                    "Bianchi named Persimmon, bought in March 2024."},
      {"role": "user", "content": "What is my bicycle called and what colour is it?"}],
     CHAT_MODEL,
     "Says the bicycle is named Persimmon AND that it is (matte) green. Both required. Saying it "
     "does not know, or inventing a different name/colour, is WRONG."),

    # The 🪄 auto entry must pick the model itself — no manual entry switching.
    ("B1", "auto",
     [{"role": "user", "content": "What is the tallest mountain in Africa? One sentence."}],
     CHAT_MODEL,
     "States Mount Kilimanjaro. Anything else is WRONG."),

    ("B2", "auto",
     [{"role": "user", "content": "Write a Python function that reverses a string without using "
                                  "slicing. Code only."}],
     CODER_MODEL,
     "Contains a Python function that reverses a string and does NOT use [::-1] slicing. "
     "A loop, reversed(), or recursion are all acceptable."),

    ("C1", "auto",
     [{"role": "user", "content": "Write a Python function fib(n) returning the nth Fibonacci number "
                                  "iteratively, with fib(0)=0. Code only."}],
     CODER_MODEL,
     "Contains a Python function computing Fibonacci ITERATIVELY (a loop, not recursion), and the "
     "logic is correct for fib(0)=0, fib(1)=1, fib(10)=55."),

    ("C2", "auto",
     [{"role": "user", "content": "What is the bug in this Python code?\n"
                                  "def add_item(item, lst=[]):\n    lst.append(item)\n    return lst"}],
     CODER_MODEL,
     "Identifies the MUTABLE DEFAULT ARGUMENT problem — that the default list is created once and "
     "shared across calls, so it accumulates. Anything else is WRONG."),

    # A coder-routed turn must ALSO honour system-message conventions.
    ("C3", "auto",
     [{"role": "system", "content": "Project convention: all internal helper functions in this "
                                    "codebase must be prefixed with 'zz_'."},
      {"role": "user", "content": "Write a tiny internal helper that squares a number. Code only."}],
     CODER_MODEL,
     "The function name starts with the zz_ prefix (e.g. zz_square). Any other name is WRONG."),
]


async def drain(result):
    if hasattr(result, "__aiter__"):
        return "".join([tok async for tok in result])
    return str(result)


async def run_case(cid, entry, messages, expect_model, criterion):
    SENT.clear()
    p = mod.Pipe()
    body = {"model": f"auto_assistant.{entry}", "messages": messages}
    meta = {"chat_id": f"qa-{cid}", "user_prompt": messages[-1]["content"]}
    t0 = time.time()
    out = await p.pipe(body, __metadata__=meta, __event_emitter__=None)
    text = await drain(out)
    el = time.time() - t0
    used = SENT[0].get("model") if SENT else None
    sent_roles = [m["role"] for m in SENT[0]["messages"]] if SENT else []
    return {"id": cid, "entry": entry, "expect_model": expect_model, "used_model": used,
            "roles_sent": sent_roles, "elapsed": el, "criterion": criterion,
            "question": messages[-1]["content"], "answer": text.strip()}


async def judge(session, r):
    prompt = (
        "You are grading an AI assistant's answer. Be strict and literal.\n\n"
        f"QUESTION:\n{r['question']}\n\n"
        f"ANSWER:\n{r['answer'][:3000]}\n\n"
        f"CORRECTNESS CRITERION:\n{r['criterion']}\n\n"
        "Reply with EXACTLY one line of JSON and nothing else:\n"
        '{"verdict": "PASS" or "FAIL", "reason": "<one short sentence>"}'
    )
    async with session.post(f"{OLLAMA}/api/chat", json={
            "model": JUDGE_MODEL, "stream": False, "think": False,
            "messages": [{"role": "user", "content": prompt}]}) as resp:
        d = await resp.json()
    raw = (d.get("message") or {}).get("content", "")
    s, e = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[s:e + 1])
    except Exception:
        return {"verdict": "UNPARSEABLE", "reason": raw[:120]}


async def free_vram():
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{OLLAMA}/api/ps") as r:
            for m in (await r.json()).get("models", []):
                await s.post(f"{OLLAMA}/api/generate",
                             json={"model": m["name"], "keep_alive": 0})
    await asyncio.sleep(3)


async def main():
    print(f"Pipe under test: {PIPE_PATH}")
    print(f"Judge model:     {JUDGE_MODEL}\n")
    print("=" * 78)
    print("PART 1 — live responses (real inference)")
    print("=" * 78)

    results = []
    for cid, entry, msgs, expect, crit in CASES:
        print(f"  {cid} [{entry}] running...", flush=True)
        r = await run_case(cid, entry, msgs, expect, crit)
        results.append(r)
        ok = r["used_model"] == r["expect_model"]
        print(f"     model: {r['used_model']}")
        print(f"     {'OK  ' if ok else 'MISMATCH — expected ' + r['expect_model']}"
              f"   roles={r['roles_sent']}  {r['elapsed']:.1f}s")

    await free_vram()

    print("\n" + "=" * 78)
    print(f"PART 2 — correctness graded by {JUDGE_MODEL}")
    print("=" * 78)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for r in results:
            r["judgement"] = await judge(s, r)
            print(f"  {r['id']}: {r['judgement'].get('verdict')} — {r['judgement'].get('reason','')}")

    await free_vram()

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  {'case':6} {'entry':10} {'model routed':10} {'answer':8}")
    bad = 0
    for r in results:
        mok = r["used_model"] == r["expect_model"]
        aok = r["judgement"].get("verdict") == "PASS"
        bad += (not mok) + (not aok)
        print(f"  {r['id']:6} {r['entry']:10} {'PASS' if mok else 'FAIL':10} {'PASS' if aok else 'FAIL':8}")
    print(f"\n{'ALL PASS' if not bad else str(bad) + ' FAILURE(S)'}")
    with open("/tmp/qa_live_results.json", "w") as f:
        json.dump(results, f, indent=1)
    print("full transcripts: /tmp/qa_live_results.json")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
"""Live web-search path: real SearXNG -> OpenWebUI-shaped injection -> real Pipe.

Why this file exists. The eval suite covers routing, reasoning, coding, grounding, memory and media,
but nothing in it touches web search — the one capability whose failure is completely silent. When
search breaks the assistant does not error; it answers from training data instead, confidently and
without citations, and nobody notices until a date or a version number turns out to be stale.

Nothing here is mocked, deliberately. A stubbed search would only prove the pipe can read a
<source> block, which was never in doubt. What actually breaks in this stack is the search backend,
and it breaks in ways only a live query catches — `json` dropping out of `search.formats` in
settings.yml (upstream ships [html] only, and webapp.py then 404s every format=json request), or
`server.limiter` coming back on and returning 403/429 to server-to-server calls. Both are recorded
in compose/searxng/settings.yml as the two things that must not regress.

Three assertions per query, each catching a different layer:

  results     SearXNG answered with usable hits          -> the search backend itself
  grounded    the answer reflects those hits             -> the injection reached the model
  citations   the [id] markers survived into the answer  -> the pipe kept them (its system prompt
                                                            promises to, and the UI renders them as
                                                            source links; dropping them silently
                                                            turns a cited answer into a bare claim)

Queries are chosen so the expected token appears in any reasonable result set and does not rot:
authorship of a 1965 novel, and SearXNG's own description of itself.

Usage:  python3 tests/test_websearch.py [pipe_path]
"""
import asyncio, importlib.util, json, os, re, sys, urllib.parse, urllib.request

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
SEARXNG_URL = os.environ.get("SEARXNG_QUERY_URL", "http://localhost:8888/search")
TOP_N = 5  # OpenWebUI's web.search.result_count on this host

spec = importlib.util.spec_from_file_location("aa_web", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Mirrors OpenWebUI's RAG template for web search: numbered <source> blocks whose name is the URL,
# prepended to the user's message. The pipe must answer from these and keep the [id] markers.
SEARCH_TEMPLATE = ("### Task:\nRespond to the user query using the provided context, incorporating "
                   "inline citations in the format [id].\n\n<context>\n{src}\n</context>\n")

# (label, searxng query, question put to the assistant, regex the answer must contain)
CASES = [
    ("authorship", "Frank Herbert Dune novel author",
     "According to the search results, who wrote the novel Dune?", r"(?i)herbert"),
    ("self-describe", "SearXNG metasearch engine what is it",
     "Based on the search results, what is SearXNG in one sentence?",
     r"(?i)meta[- ]?search|search engine"),
]

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def live_search(query, n=TOP_N):
    """Query SearXNG's JSON API the way OpenWebUI's searxng provider does. Returns (hits, total)."""
    url = f"{SEARXNG_URL}?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
    with urllib.request.urlopen(url, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"SearXNG returned HTTP {r.status} — check search.formats includes json")
        data = json.load(r)
    hits = [h for h in data.get("results", [])
            if h.get("url") and (h.get("content") or h.get("title"))]
    return hits[:n], len(data.get("results", []))


async def ask(hits, question):
    """Put the question to the real Pipe with the search results injected as OpenWebUI would."""
    src = "\n".join(
        f'<source id="{i}" name="{h["url"]}">{(h.get("content") or h["title"])[:600]}</source>'
        for i, h in enumerate(hits, 1))
    p = mod.Pipe()
    out = await p.pipe(
        {"model": "auto_assistant.auto",
         "messages": [{"role": "user", "content": SEARCH_TEMPLATE.format(src=src) + question}]},
        # user_prompt is the CLEAN question: OpenWebUI captures it before injection, and routing
        # must not see a wall of search context (a result about drawing would route to the image
        # backend and start a GPU render).
        __metadata__={"chat_id": "websearch-test", "user_prompt": question},
        __event_emitter__=None)
    return out if isinstance(out, str) else "".join([t async for t in out])


async def main():
    print(f"searxng : {SEARXNG_URL}")
    print(f"pipe    : {PIPE_PATH}")

    for label, query, question, want in CASES:
        print(f"\n--- {label}: {query!r}")
        try:
            hits, total = live_search(query)
        except Exception as e:
            check(f"{label}: SearXNG reachable", False, str(e)[:160])
            check(f"{label}: answer grounded in results", False, "no search results")
            check(f"{label}: [id] citations preserved", False, "no search results")
            continue

        check(f"{label}: SearXNG returned >=3 usable results", len(hits) >= 3,
              f"got {len(hits)} usable of {total} raw")
        for i, h in enumerate(hits, 1):
            print(f"      [{i}] {h['url'][:76]}")
        if len(hits) < 3:
            check(f"{label}: answer grounded in results", False, "too few results to ground on")
            check(f"{label}: [id] citations preserved", False, "too few results to ground on")
            continue

        answer = await ask(hits, question)
        print(f"      answer: {answer.strip()[:220]}")
        check(f"{label}: answer grounded in results", bool(re.search(want, answer)),
              f"no /{want}/ in answer")
        check(f"{label}: [id] citations preserved", bool(re.search(r"\[\d+\]", answer)),
              "answer carried no [n] marker")

    fails = sum(1 for _, ok, _ in results if not ok)
    for label, ok, detail in results:
        if not ok:
            print(f"  FAIL: {label} — {detail}")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

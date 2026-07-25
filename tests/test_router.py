#!/usr/bin/env python3
"""Reproduce + verify the RAG router-poisoning bug against the real auto_assistant Pipe class.

Routing is exercised for real; only the generation/chat entry points are stubbed, so the
decision path under test is the untouched production code.
"""
import asyncio, importlib.util, sys, types

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"

spec = importlib.util.spec_from_file_location("auto_assistant_under_test", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# OpenWebUI's real DEFAULT_RAG_TEMPLATE (config.py:1045-1069), verbatim.
RAG_TEMPLATE = """### Task:
Respond to the user query using the provided context, incorporating inline citations in the format [id] **only when the <source> tag includes an explicit id attribute** (e.g., <source id="1">).

### Guidelines:
- If you don't know the answer, clearly state that.
- If uncertain, ask the user for clarification.
- Respond in the same language as the user's query.
- If the context is unreadable or of poor quality, inform the user and provide the best possible answer.
- If the answer isn't present in the context but you possess the knowledge, explain this to the user and provide the answer using your own understanding.
- **Only include inline citations using [id] (e.g., [1], [2]) when the <source> tag includes an id attribute.**
- Do not cite if the <source> tag does not contain an id attribute.
- Do not use XML tags in your response.
- Ensure citations are concise and directly related to the information provided.

### Example of Citation:
If the user asks about a specific topic and the information is found in a source with a provided id attribute, the response should include the citation like in the following example:
* "According to the study, the proposed method increases efficiency by 20% [1]."

### Output:
Provide a clear and direct response to the user's query, including inline citations in the format [id] only when the <source> tag with id attribute is present in the context.

<context>
{{CONTEXT}}
</context>
"""


def rag_augmented(query: str, doc_text: str) -> str:
    """Exactly what middleware does: render the template, then PREPEND it to the user turn
    (apply_source_context_to_messages -> add_or_update_user_message(append=False))."""
    ctx = f'<source id="1" name="brandbook.pdf">{doc_text}</source>'
    return RAG_TEMPLATE.replace("{{CONTEXT}}", ctx) + query


def make_pipe():
    p = mod.Pipe()

    async def _tracked(emitter, verb, coro):
        coro.close()          # never actually run a render
        return (verb, 0.0)

    async def _finish(emitter, result, verb, elapsed, detail):
        return f"MEDIA[{result}]"

    def _achat_stream(omsgs):
        return "CHAT"

    async def _status(*a, **k):
        return None

    p._tracked, p._finish, p._achat_stream, p._status = _tracked, _finish, _achat_stream, _status
    return p


async def route(query, doc=None, with_metadata=True, chat_id="testchat"):
    p = make_pipe()
    content = rag_augmented(query, doc) if doc else query
    body = {"messages": [{"role": "user", "content": content}]}
    meta = {"chat_id": chat_id}
    if with_metadata:
        meta["user_prompt"] = query      # middleware.py:2803 — captured BEFORE RAG injection
    return await p.pipe(body, __metadata__=meta, __event_emitter__=None)


# A document whose *text* trips the media regexes. This is ordinary prose in a real PDF.
POISON_DOC = (
    "Brand guidelines, section 4. The team will create a picture of the new logo and "
    "draw the mascot in flat colours. A short video explaining the animation principles "
    "accompanies this document. Photo of the founders appears on page 12."
)

CASES = [
    # (name, query, doc, with_metadata, expected)
    ("A  RAG doc + genuine question",        "summarise this document for me",  POISON_DOC, True,  "CHAT"),
    ("B  RAG doc + question about content",  "what does section 4 say?",        POISON_DOC, True,  "CHAT"),
    ("C  no RAG, real image request",        "make a picture of a cat",         None,       True,  "MEDIA[Generating image]"),
    ("D  no RAG, real video request",        "create a video of a dog running", None,       True,  "MEDIA[Generating video]"),
    ("E  RAG doc + real image request",      "make a picture of a cat",         POISON_DOC, True,  "MEDIA[Generating image]"),
    ("F  no metadata (direct API), chat",    "summarise this document for me",  None,       False, "CHAT"),
]


async def main():
    print(f"Testing: {PIPE_PATH}\n")
    fails = 0
    for name, q, doc, meta, expected in CASES:
        got = await route(q, doc, meta)
        ok = got == expected
        fails += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"          expected {expected!r}, got {got!r}")

    print("\n--- control: same cases with routing metadata REMOVED (simulates the pre-fix path) ---")
    for name, q, doc, _m, expected in CASES[:2]:
        got = await route(q, doc, with_metadata=False)
        print(f"  {name}: {got!r}   <- pre-fix behaviour ({'BUG REPRODUCED' if got != expected else 'no bug'})")

    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

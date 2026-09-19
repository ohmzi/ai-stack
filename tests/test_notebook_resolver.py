#!/usr/bin/env python3
"""Which notebook did the user mean? Deterministic, and measured rather than felt.

Why this file exists. Notebook mode's entire value is that it answers from the notebook the user
named. Everything else in the feature — the button, the streaming, the citations — is plumbing
around that one decision. If the decision is wrong the feature is not merely useless, it is worse
than useless: it answers confidently from the wrong document while the interface says otherwise.

So the decision is made by string matching, not by a language model, and this file pins it:

  * the scoring TABLE, verbatim, for real phrasings against the real notebooks on this box —
    including the user's own typo ("technical" vs the notebook `techincal`);
  * the gate that rejects the false positives, which is the part that actually earns its keep:
    "the technical debt in this codebase is high" scores 0.889 against `techincal` and must NOT
    be treated as naming it;
  * determinism, because "reply with the second one" has to mean the same thing every time;
  * the citation-id trap, where a wrong id fails SILENTLY (HTTP 200, title: null) rather than
    erroring, so a naive resolver ships a blank source list that looks fine.

Fully offline. Nothing here touches Open Notebook — `request` is replaced for the client tests.

Usage:  python3 tests/test_notebook_resolver.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipes", "shared"))

import notebook_resolver as nbr  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# The five notebooks that actually exist on this box, verbatim from GET /api/notebooks.
NBS = [
    {"id": "notebook:rld501etlkhv0gt7bule", "name": "historical",       "description": "", "source_count": 0},
    {"id": "notebook:rjm3mcaokcc2598n7w0m", "name": "techincal",        "description": "", "source_count": 0},
    {"id": "notebook:p99eh4wymwa2mcqkn48m", "name": "Self help",        "description": "", "source_count": 0},
    {"id": "notebook:wshrasohbx3rbvktuqpu", "name": "Fictional",        "description": "", "source_count": 0},
    {"id": "notebook:8oo0se6ztyyw4assjo7o", "name": "Islamic guidance", "description": "", "source_count": 1},
]

# MEASURED, not aspirational: every row below is what the scorer actually returns. The two SUGGEST
# rows are the important ones — they are ordinary sentences that merely look like notebook names.
TABLE = [
    # (message, kind, winner, score)
    ("check islamic guidance to answer this question",                        "answer",  "Islamic guidance", 1.00),
    ("check the technical book",                                              "answer",  "techincal",        0.89),
    ("check the techincal book",                                              "answer",  "techincal",        1.00),
    ("can you check the islamic guidance notebook and tell me about zakat",   "answer",  "Islamic guidance", 1.00),
    ("use the self help book to answer: how do i stop procrastinating",       "answer",  "Self help",        1.00),
    ("check my historical notes",                                             "answer",  "historical",       1.00),
    ("fictional please",                                                      "answer",  "Fictional",        1.00),
    ("islamic guidance: what does the quran say about patience",              "answer",  "Islamic guidance", 1.00),
    ("look in self help",                                                     "answer",  "Self help",        1.00),
    # Ambiguous on purpose: two notebooks are both genuinely named, winner's margin is under MARGIN.
    ("check the historical fiction notebook",                                 "confirm", "historical",       1.00),
    # The false positives the gate exists for.
    ("the technical debt in this codebase is high",                            "suggest", "techincal",        0.89),
    ("tell me about the history of rome",                                      "suggest", "historical",       0.71),
    ("whats the weather",                                                      "suggest", "Self help",        0.44),
    ("check the book",                                                         "suggest", "Self help",        0.44),
]


def test_table():
    print("\n  measured scoring table")
    for text, kind, winner, score in TABLE:
        r = nbr.resolve(text, NBS)
        top = r.rows[0]
        check(f"{kind:7} {winner:17} <- {text[:52]!r}",
              r.kind == kind and top.name == winner and abs(top.score - score) < 0.005,
              "" if (r.kind == kind and top.name == winner)
              else f"got {r.kind} {top.name}={top.score:.2f}")


def test_named_gate():
    print("\n  the named gate (what stops a topical sentence becoming a notebook request)")

    def named(text, name):
        r = nbr.resolve(text, NBS)
        return next(x.named for x in r.rows if x.name == name)

    check("verbatim name is named", named("check islamic guidance on x", "Islamic guidance"))
    check("multi-word name is self-evidencing", named("what about the self help side", "Self help"))
    check("cue nearby earns the gate", named("consult fictional about it", "Fictional"))
    check("name dominates a short message", named("fictional please", "Fictional"))
    check("topical sentence is NOT naming", not named("the technical debt in this codebase is high", "techincal"),
          "this is the case that motivated the gate")
    check("a preposition is not a cue", not named("the technical debt in this codebase is high", "techincal"),
          "'in' deliberately excluded from the cues")
    check("history question is NOT naming", not named("tell me about the history of rome", "historical"))


def test_typo():
    print("\n  the user's own typo")
    r = nbr.resolve("check the technical book", NBS)
    check("'technical' resolves to the notebook spelled 'techincal'",
          r.kind == "answer" and r.rows[0].name == "techincal", f"got {r.rows[0].name}={r.rows[0].score:.2f}")
    check("typo still clears HIGH (pinned against a fuzzy-backend change)",
          r.rows[0].score >= nbr.HIGH, f"score={r.rows[0].score:.3f} HIGH={nbr.HIGH}")


def test_strip():
    print("\n  question stripping (the text that reaches the answer engine)")
    cases = [
        ("use the self help book to answer: how do i stop procrastinating", "how do i stop procrastinating"),
        ("can you check the islamic guidance notebook and tell me about zakat", "tell me about zakat"),
        ("islamic guidance: what does the quran say about patience", "what does the quran say about patience"),
        ("check the techincal notebook, how do i configure nginx", "how do i configure nginx"),
        ("check islamic guidance to answer this question", ""),
        ("check my historical notes", ""),
        ("fictional please", ""),
    ]
    for text, want in cases:
        got = nbr.resolve(text, NBS).question
        check(f"{want!r:44} <- {text[:46]!r}", got == want, f"got {got!r}")
    # A spurious match must not mangle the message.
    got = nbr.resolve("tell me about the history of rome", NBS).question
    check("spurious match leaves the message intact", got == "tell me about the history of rome", f"got {got!r}")


def test_determinism():
    print("\n  determinism")
    sigs = set()
    for _ in range(100):
        r = nbr.resolve("check the historical fiction notebook", NBS)
        sigs.add((r.kind, tuple((x.name, round(x.score, 6)) for x in r.rows)))
    check("100 runs, one identical outcome", len(sigs) == 1, f"{len(sigs)} distinct")

    # Same name twice is a real tie; it must be stable and must NOT auto-answer.
    dupes = [dict(NBS[0]), dict(NBS[0], id="notebook:zzz")]
    r = nbr.resolve("check historical", dupes)
    check("identical names never auto-answer", r.kind != "answer", f"kind={r.kind}")
    orders = {tuple(x.id for x in nbr.resolve("check historical", dupes).rows) for _ in range(25)}
    check("tie order is stable", len(orders) == 1)


def test_markers():
    print("\n  citation markers")
    answer = ("Zakat is 2.5% [source:abc123] and the summary agrees [source_insight:def456]. "
              "See also [source:abc123] and [note:ghi789].")
    text, order = nbr.renumber(answer)
    check("markers become footnote numbers", "[1]" in text and "[2]" in text and "[3]" in text, text[:70])
    check("first appearance order", order == [("source", "abc123"), ("source_insight", "def456"), ("note", "ghi789")],
          str(order))
    check("repeat marker reuses its number", text.count("[1]") == 2)
    check("no raw markers survive", "source:" not in text)
    check("empty id is not a marker", nbr.renumber("[source:]")[1] == [])
    check("a bare id is not a marker (the small model emits these)",
          nbr.renumber("see [mlwsd9auwk6i4g1ktom2]")[1] == [])


def test_citation_resolution():
    print("\n  citation resolution — the silent-null trap")
    seen = []

    def fake_request(base, path, auth, *, method="GET", body=None, timeout=20):
        seen.append(path)
        if path.startswith("/api/sources/source:"):
            return 200, {"title": "The Barakah Effect.pdf"}
        if path.startswith("/api/insights/source_insight:"):
            return 200, {"id": path.rsplit("/", 1)[-1], "source_id": "source:mlwsd9auwk6i4g1ktom2"}
        if path.startswith("/api/sources/source:mlwsd9"):
            return 200, {"title": "The Barakah Effect.pdf"}
        if path.startswith("/api/notes/note:"):
            return 200, {"title": "My note"}
        return 404, {"detail": "not found"}

    real, nbr.request = nbr.request, fake_request
    try:
        out = nbr.resolve_citations("http://x", None, [("source", "abc"), ("note", "ghi")])
        check("source resolved to its title", any(r["title"] == "The Barakah Effect.pdf" for r in out), str(out))
        check("note resolved to its title", any(r["title"] == "My note" for r in out))
        check("EVERY call carries the record prefix",
              all(p.startswith(("/api/sources/source:", "/api/notes/note:", "/api/insights/source_insight:"))
                  for p in seen), str(seen))
        check("the bare-id form is never used (it 200s with title:null)",
              not any(p in ("/api/sources/abc", "/api/notes/ghi") for p in seen), str(seen))

        seen.clear()
        out = nbr.resolve_citations("http://x", None, [("source_insight", "def456")])
        check("insight follows source_id for a title",
              out and out[0]["title"] == "The Barakah Effect.pdf", str(out))
        check("insight is fetched, then its parent source",
              len(seen) == 2 and "source_insight:" in seen[0] and "source:" in seen[1], str(seen))
        check("insight keeps its own id in the report", out and out[0]["id"] == "source_insight:def456")

        footer = nbr.format_sources([{"kind": "source", "id": "source:abc", "title": "T"}])
        check("footer renders", "**Sources**" in footer and "1. T" in footer, footer.replace("\n", "|"))
        check("empty citation list renders nothing", nbr.format_sources([]) == "")
    finally:
        nbr.request = real


def test_sse_and_probe():
    print("\n  SSE parsing and the scope probe")
    check("data frame parses",
          nbr.parse_sse_line(b'data: {"type": "final_answer", "content": "hi"}') ==
          {"type": "final_answer", "content": "hi"})
    check("blank line ignored", nbr.parse_sse_line(b"") is None)
    check("comment ignored", nbr.parse_sse_line(b": keepalive") is None)
    check("[DONE] ignored", nbr.parse_sse_line(b"data: [DONE]") is None)
    check("malformed json ignored", nbr.parse_sse_line(b"data: {not json") is None)
    check("non-object json ignored", nbr.parse_sse_line(b"data: [1,2]") is None)

    calls = []

    def fake_request(base, path, auth, *, method="GET", body=None, timeout=20):
        calls.append((path, method, body))
        return fake_request.status, {}

    real, nbr.request = nbr.request, fake_request
    try:
        fake_request.status = 200
        check("200 means the scope was IGNORED", nbr.probe_scope_support("http://x", None) is False)
        check("the probe sends a notebook_id", (calls[-1][2] or {}).get("notebook_id") is not None,
              str(calls[-1][2]))
        fake_request.status = 404
        check("404 means the scope is honoured", nbr.probe_scope_support("http://x", None) is True)
        fake_request.status = 400
        check("400 (id parsed, rejected) also proves it is read",
              nbr.probe_scope_support("http://x", None) is True)
        fake_request.status = 500
        check("anything else is 'unknown'", nbr.probe_scope_support("http://x", None) is None)
    finally:
        nbr.request = real


def test_payloads():
    print("\n  request shaping")
    check("ask payload carries the scope",
          nbr.build_ask_payload("q", "notebook:e", ["a", "b", "c"])["notebook_id"] == "notebook:e")
    check("all three models are sent",
          set(nbr.build_ask_payload("q", "n", ["s", "a", "f"])) >=
          {"strategy_model", "answer_model", "final_answer_model"})
    check("auth header omitted when no password",
          "Authorization" not in nbr.ask_headers(None))
    check("auth header sent when a password is set",
          nbr.ask_headers("pw").get("Authorization") == "Bearer pw")
    check("normalization folds case, punctuation and accents",
          nbr.norm("Self-Help") == "self help" and nbr.norm("A.I.") == "a i"
          and nbr.norm("  Spaced   Out  ") == "spaced out" and nbr.norm("café") == "cafe")


def test_pick_candidate():
    print("\n  resolving a reply against a parked candidate list")
    cands = [{"id": "notebook:a", "name": "historical"},
             {"id": "notebook:d", "name": "Fictional"},
             {"id": "notebook:e", "name": "Islamic guidance"}]
    for text, want in [("the second one", 1), ("2", 1), ("#2", 1), ("fictional", 1),
                       ("historical", 0), ("islamic guidance", 2), ("the third", 2),
                       ("first", 0)]:
        got = nbr.pick_candidate(text, cands)
        check(f"{text!r} -> {want}", got == want, f"got {got}")

    # A reply that names nothing must NOT be read as a choice. These all returned an index before
    # the HIGH bar replaced the `named` gate — a short message made every single-word notebook
    # look "named" via the >=34% arm.
    for text in ["maybe", "never mind", "whats the weather", "", "not sure"]:
        got = nbr.pick_candidate(text, cands)
        check(f"{text!r} selects nothing", got is None, f"got {got}")
    check("an ordinal past the end selects nothing", nbr.pick_candidate("the ninth", cands) is None)
    check("empty candidate list is safe", nbr.pick_candidate("first", []) is None)


def test_catalog():
    print("\n  catalogue intent (what is in here, and how do I ask about it)")
    # The negatives are REAL messages from this box's Open Notebook session history — three
    # genuine content questions that all say "book". They are the reason every pattern requires an
    # inventory-shaped question around a collection noun rather than the noun alone.
    for text in ["what kind of categories or books there are",
                 "how can i address them to ask question specifically",
                 "what books do you have",
                 "what notebooks are there",
                 "list the notebooks",
                 "what can i ask",
                 "how do i ask a question",
                 "help",
                 "what categories are available",
                 "what's in the notebooks",
                 "which notebook should i use",
                 "list the books in islamic guidance",
                 "what kind of books are in islamic guidance"]:
        check(f"CATALOG <- {text[:56]!r}", nbr.is_catalog_request(text))

    for text in [
        "What does this book talk about in regards to living within your means or should I cheat "
        "people and make money and donate to feel good about it at the end",
        "What does book say about earning and if it's more important than sending time with family",
        "According to islamic books what should I do about providing helps to other",
        "Summarize chapter one",
        "what does the book say about charity",
        "what is in the book about charity",
        "how do i configure nginx",
        "what does the quran say about patience",
        "check islamic guidance on what books are mentioned",
        "tell me about the history of rome",
    ]:
        check(f"NOT catalogue <- {text[:52]!r}", not nbr.is_catalog_request(text))
    check("empty message is not a catalogue request", not nbr.is_catalog_request(""))


def test_catalog_render():
    print("\n  what the catalogue actually says")
    nbs = [{"id": "notebook:isl", "name": "Islamic guidance", "description": "", "source_count": 1},
           {"id": "notebook:tec", "name": "techincal", "description": "", "source_count": 0}]
    src = {"notebook:isl": ["The Barakah Effect.pdf"]}
    out = nbr.render_catalog(nbs, src)
    check("lists every notebook", "Islamic guidance" in out and "techincal" in out)
    check("names the real sources", "The Barakah Effect.pdf" in out)
    check("shows a copy-pasteable phrasing",
          "check islamic guidance to answer this question" in out, out[-200:])
    check("an empty notebook says so rather than looking usable",
          "no sources yet" in out)
    check("counts what has sources", "2 notebooks" in out and "1 of them with sources" in out)

    scoped = nbr.render_catalog(nbs, src, scoped=nbs[0])
    check("a scoped catalogue mentions only that notebook",
          "Islamic guidance" in scoped and "techincal" not in scoped, scoped)
    check("...and still shows the sources", "The Barakah Effect.pdf" in scoped)

    # A notebook we know has sources but could not list must not be described as empty.
    partial = nbr.render_catalog([nbs[0]], {})
    check("an unfetchable source list is not reported as empty",
          "no sources yet" not in partial and "unavailable" in partial, partial)
    check("an empty collection is handled", "no notebooks" in nbr.render_catalog([], {}))


def test_render():
    print("\n  what the user is shown when it cannot just answer")
    r = nbr.resolve("check the historical fiction notebook", NBS)
    text = nbr.render_confirm(r)
    check("confirm lists the candidates", "historical" in text and "Fictional" in text)
    check("confirm quotes the pending question when there is one", "ask it" in text)

    r = nbr.resolve("whats the weather", NBS)
    text = nbr.render_suggest(r)
    check("suggest offers the closest names", "closest" in text and "Self help" in text)
    check("suggest tells the user how to name one", "check" in text)

    check("no notebooks at all is handled", "any notebooks" in nbr.render_suggest(nbr.resolve("x", []), []))


def main():
    print(f"notebook resolver — fuzzy backend: {nbr._BACKEND}")
    test_table()
    test_named_gate()
    test_typo()
    test_strip()
    test_determinism()
    test_markers()
    test_citation_resolution()
    test_sse_and_probe()
    test_pick_candidate()
    test_catalog()
    test_catalog_render()
    test_payloads()
    test_render()

    fails = [r for r in results if not r[1]]
    if fails:
        print("\nThe resolver is the feature. Fix it before deploying.")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""Pick `rag.relevance_threshold` from measurement instead of argument.

Why this file exists. `rag.relevance_threshold` was `0`, and the gate that applies it is
`if self.r_score:` (`retrieval/utils.py:1722`) — a literal zero is falsy, so there was no relevance
filter on retrieved context at all. Something clearly wrong was reaching the model: for the search
behind `web-search-f631…`, 103 of 145 stored chunks are homepage navigation chrome, including 34
chunks of the Cambridge Dictionary entry for the word "top" — because the stock query-generation
template tells the task model to prefer "broad" queries, and a 3B model asked to be broad turned
"what's good on Netflix" into `top`.

**What this harness measured, which was not what was expected.** Scoring the real stored chunks
through the real reranker shows the junk is real but the reranker already keeps it out of the
top-5: in all four recorded searches the top-5 contains zero junk sites even at threshold 0,
because the good pages contribute more than five high-scoring chunks between them. Per-source best
scores are cleanly bimodal — 0.99/0.74 for the two Netflix articles against 0.0078 (cnn.com),
0.0069 (foxnews), 0.0005 (ctvnews), 0.0001 (cambridge dictionary), 0.0000 (topsports).

So the threshold is **insurance, not a fix**: it earns its place only when a search returns fewer
than five genuinely relevant chunks, where the context would otherwise be padded up to
`top_k_reranker` with whatever ranked next. That is a real failure mode worth guarding, but the
honest claim is narrow.

The value follows from the gap, not from a round number. Highest junk score observed: 0.0078.
Lowest score worth keeping: 0.0618 (crazygames.com for "play Run 3 online" — marginal but
legitimate). Anything in (0.008, 0.06) separates them; **0.05** takes it with margin at both ends.
0.15, the value originally proposed, is measurably too aggressive: it drops `pygame.org/docs` at
0.1011 from the Angry Birds search, which is a page you would want.

The real fix for the junk is upstream and this harness cannot see it: stop the bad query being
generated, so the chrome is never fetched, embedded, stored, or reranked at 0.26 s/doc.

The eval suite cannot cover any of this — `run_eval.py` imports the pipe and calls `Pipe.pipe()`
directly, while retrieval, reranking and thresholding all live in OpenWebUI middleware, and
`inject_rag` fabricates a `<source>` block rather than touching Chroma or Infinity. Hence a separate
harness. No LLM is involved, so it is deterministic and runs in seconds.

Reading the live database read-only rather than copying it is deliberate: OpenWebUI runs with WAL
enabled, so a `cp` of the main file would miss recent writes.

Also measured here, correcting the roadmap's "1.07 s for 20 docs" (that was short snippets): on real
stored chunks, which average 818 characters, reranking costs **0.26 s/doc and is linear — 5.2 s for
20**. `rag.top_k` is a latency dial, not a free recall knob.

Usage:  python3 tests/test_retrieval_quality.py
        python3 tests/test_retrieval_quality.py --threshold 0.15   # shows why 0.15 is too high
"""
import argparse, collections, json, os, sqlite3, sys, time
import urllib.request

CHROMA = os.environ.get(
    "CHROMA_DB", "/volume1/docker/openwebui/config/vector_db/chroma.sqlite3")
RERANK = os.environ.get("RERANK_URL", "http://localhost:7997/rerank")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
BATCH = 16          # Infinity handles 32 in one call; 16 keeps each request comfortably sub-4 s
LADDER = [0.0, 0.02, 0.05, 0.1, 0.15, 0.3, 0.5]

# Real searches this instance ran, recovered from the stored collections; `query` is reconstructed
# from the sources and titles. `junk` sources must never reach the context. `keep` are sources that
# must survive — including deliberately marginal ones, because the risk of a threshold is not that
# it fails to remove garbage but that it quietly removes something useful.
CASES = [
    {"collection": "web-search-f631", "query": "best new movies on Netflix July 2026",
     "junk": ["dictionary.cambridge.org", "foxnews.com", "cnn.com", "ctvnews.ca", "topsports.ca"],
     "keep": ["buzzfeed.com", "screenrant.com"]},
    {"collection": "web-search-3bcf", "query": "play Run 3 online free game",
     "junk": ["bestbuy.ca", "bestbuy.com", "wikipedia.org"],
     "keep": ["coolmathgames.com", "poki.com"]},
    {"collection": "web-search-c796",
     "query": "how to build an Angry Birds game in Python with pygame",
     "junk": ["sourceforge.net", "programiz.com", "alternativeto.net"],
     "keep": ["fuzzutechprojects.blogspot.com", "github.com", "pygame.org"]},
    # Control: a well-targeted search where nothing should be filtered out.
    {"collection": "web-search-9096", "query": "Canada GDP and employment economic report 2026",
     "junk": [],
     "keep": ["international.canada.ca", "statcan.gc.ca"]},
]

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def load_chunks(prefix, per_source):
    """Stored chunks for a collection, sampled up to `per_source` per source URL.

    Sampling per source rather than taking the first N keeps every site represented — the question
    is which SITES can reach the context, and a naive head() would return 24 chunks of one page."""
    db = sqlite3.connect(f"file:{CHROMA}?mode=ro", uri=True)
    rows = db.execute("""
        select em.id,
               max(case when em.key='chroma:document' then em.string_value end),
               max(case when em.key='source'          then em.string_value end)
        from collections c
             join segments s            on s.collection = c.id
             join embeddings e          on e.segment_id = s.id
             join embedding_metadata em on em.id = e.id
        where c.name like ?
        group by em.id""", (prefix + "%",)).fetchall()
    per, total = collections.defaultdict(list), 0
    for _id, doc, src in rows:
        if not doc or not src:
            continue
        total += 1
        if len(per[src]) < per_source:
            per[src].append(doc)
    return [(src, d) for src, docs in per.items() for d in docs], total, len(per)


def rerank(query, docs):
    """Relevance scores in input order. Batched only to keep each call short."""
    scores = [0.0] * len(docs)
    for start in range(0, len(docs), BATCH):
        chunk = docs[start:start + BATCH]
        payload = json.dumps({"model": RERANK_MODEL, "query": query,
                              "documents": chunk}).encode()
        req = urllib.request.Request(RERANK, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.load(r)
        for item in body["results"]:
            scores[start + item["index"]] = item["relevance_score"]
    return scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="the value proposed for rag.relevance_threshold")
    ap.add_argument("--top-n", type=int, default=5, help="rag.top_k_reranker")
    ap.add_argument("--per-source", type=int, default=3)
    a = ap.parse_args()

    if not os.path.exists(CHROMA):
        print(f"cannot find Chroma at {CHROMA} — set CHROMA_DB")
        return 2
    print(f"chroma    : {CHROMA}")
    print(f"reranker  : {RERANK} ({RERANK_MODEL})")
    print(f"proposed  : relevance_threshold={a.threshold}  top_k_reranker={a.top_n}\n")

    hi_junk, lo_keep, graded = 0.0, 1.0, 0
    for case in CASES:
        docs, stored, n_src = load_chunks(case["collection"], a.per_source)
        if not docs:
            check(f"{case['collection']}: chunks found", False, "collection empty or missing")
            continue
        print(f"--- {case['collection']}…  {case['query']!r}")
        print(f"    {stored} chunks stored across {n_src} sources; scoring {len(docs)} sampled")

        t0 = time.time()
        scores = rerank(case["query"], [d for _s, d in docs])
        secs = time.time() - t0
        print(f"    rerank {len(docs)} docs in {secs:.2f}s  ({secs / len(docs):.3f}s/doc)")

        ranked = sorted(zip(scores, [s for s, _d in docs]), key=lambda x: -x[0])
        best = {}
        for sc, src in ranked:
            best[src] = max(best.get(src, 0.0), sc)

        print("    best score per source:")
        for src, sc in sorted(best.items(), key=lambda x: -x[1]):
            if any(j in src for j in case["junk"]):
                tag, hi_junk = "  <- junk", max(hi_junk, sc)
            elif any(g in src for g in case["keep"]):
                tag = "  <- keep"
            else:
                tag = ""
            print(f"      {sc:.4f}  {src[:70]}{tag}")
        # A keeper's BEST chunk decides whether it can reach the context at all. A site's weakest
        # page (pygame.org/download at 0.0016 beside pygame.org/docs at 0.1011) says nothing about
        # whether the threshold would silence that site.
        for g in case["keep"]:
            hits = [sc for src, sc in best.items() if g in src]
            if hits:
                lo_keep = min(lo_keep, max(hits))

        print(f"    top-{a.top_n} by threshold:")
        for th in LADDER:
            kept = [(sc, src) for sc, src in ranked if sc >= th][:a.top_n]
            sites = sorted({s for _sc, s in kept})
            nj = sum(1 for s in sites if any(j in s for j in case["junk"]))
            mark = " <= proposed" if abs(th - a.threshold) < 1e-9 else ""
            print(f"      >={th:<5} kept {len(kept)}/{a.top_n}, {len(sites)} sites, {nj} junk{mark}")

        # Assertions at the proposed value. The filter applies to ALL candidates before the top-N
        # cut, matching retrieval/utils.py:1722-1723.
        kept = [(sc, src) for sc, src in ranked if sc >= a.threshold][:a.top_n]
        sites = {s for _sc, s in kept}
        for j in case["junk"]:
            survived = any(j in s for s in sites)
            check(f"{case['collection']}: {j} kept out", not survived,
                  "" if not survived else "reached the context")
        for g in case["keep"]:
            # The expensive failure: a threshold tuned to remove garbage that also silently removes
            # a page the user needed.
            present = next((s for s in best if g in s), None)
            survived = any(g in s for s in sites)
            check(f"{case['collection']}: {g} retained", survived or present is None,
                  "" if survived else f"dropped at {a.threshold} (best {best.get(present, 0):.4f})")
        check(f"{case['collection']}: context not emptied", bool(kept),
              "" if kept else f"nothing scored >= {a.threshold}")
        graded += 1
        print()

    print(f"separation across all cases: highest junk {hi_junk:.4f}  |  lowest keeper {lo_keep:.4f}")
    if hi_junk < lo_keep:
        inside = hi_junk < a.threshold < lo_keep
        print(f"  any threshold in ({hi_junk:.4f}, {lo_keep:.4f}) separates them; "
              f"{a.threshold} {'sits inside' if inside else 'DOES NOT sit inside'}")
    fails = sum(1 for _, ok, _ in results if not ok)
    for label, ok, detail in results:
        if not ok:
            print(f"  FAIL: {label} — {detail}")
    print(f"\n{len(results)} checks across {graded} real searches — "
          f"{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

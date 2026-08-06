#!/usr/bin/env python3
"""The background-monitor search layer, pinned offline. No network, no clock.

Why this file exists. Every rule here has a failure mode that produces no error message, which is
the only reason a test can be the difference between working and quietly not working:

  * **The query string must carry only q and format=json.** An unknown `engines=` or `categories=`
    value is SILENTLY IGNORED and falls back to SearXNG's full default roster of dozens of engines
    — the fastest way to CAPTCHA this IP, with a 200 and results to show for it. A bad
    `pageno`/`safesearch`/`time_range` is the opposite trap: a hard HTTP 400 the caller cannot tell
    from a real outage.
  * **Dead engines alone are NOT an outage.** With six engines, unresponsive_engines is non-empty on
    a large share of healthy searches. If that raised, the search would take the no-cooldown outage
    path and re-probe every five minutes — and probing at expiry re-triggers a CAPTCHA (measured),
    so one block would become permanent.
  * **results[] is grouped by engine, not sorted by score** (measured). Unsorted, a four-fetch
    audition budget can be spent entirely inside one engine's block.
  * **infoboxes[] entries carry url=None and title=""**. Code that treats them as result rows
    crashes on the first urlparse, and a crashed run has no LOG line at all.
  * **A snippet number must never be able to become a reported price.** It is a third party's
    rendering of the page at an unknown time. The guarantee is a type boundary, not a convention.
  * **The scoreboard must be reproducible.** Ordering is derived from counters, so a lost increment
    is a changed ordering; and on an empty board the order must be exactly ENGINE_ORDER, because
    that is what makes the whole thing safe to ship in halves.

Usage:  python3 tests/test_web_search.py
"""
import ast
import hashlib
import importlib.util
import io
import json
import random
import re
import sys
import tempfile
import urllib.parse

results = []

HERMES_SETTINGS = "/home/ohmz/ai-stack/compose/searxng-hermes/settings.yml"
CHAT_SETTINGS = "/home/ohmz/ai-stack/compose/searxng/settings.yml"
COMPOSE = "/home/ohmz/ai-stack/compose/docker-compose.yml"
# The chat instance's roster is the one OpenWebUI depends on, and a regression there is INVISIBLE:
# the model answers from training data and still looks grounded. This pin makes touching that file
# a deliberate act with a diff to review, rather than a side effect of working on monitors.
CHAT_SETTINGS_SHA256 = "968528585ec90fac3843011027b26c18362c430f6e808844ab0406c6b996e717"


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def row(url, score=1.0, engines=("bing",), positions=(1,), title="t", content=""):
    return {"url": url, "title": title, "content": content, "score": score,
            "engines": list(engines), "positions": list(positions)}


def main():
    ws = load("/home/ohmz/ai-stack/scripts/web_search.py", "ws")
    pw = load("/home/ohmz/ai-stack/scripts/price_watch.py", "pw")
    pw.STATE_DIR = tempfile.mkdtemp()

    calls = []

    def serve(payload):
        """Point ws at a canned SearXNG response and count the requests it makes."""
        def urlopen(url, timeout=0):
            calls.append(url)
            if isinstance(payload, Exception):
                raise payload
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return FakeResp(body.encode())
        ws.urllib.request.urlopen = urlopen

    print("--- one call, and the query string carries no knobs ---")
    del calls[:]
    serve({"results": [row("https://a.example.com/p")]})
    got = ws.search("google fitbit air price")
    check("a fan-out is ONE request, not one per engine", len(calls) == 1, calls)
    check("...and it returns the rows", len(got) == 1, got)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    check("q and format are sent", q.get("q") == ["google fitbit air price"]
          and q.get("format") == ["json"], q)
    check("...and NOTHING else — a silently-ignored engines= typo cannot happen",
          set(q) == {"q", "format"}, sorted(q))
    for banned in ("engines", "categories", "pageno", "safesearch", "time_range", "language"):
        check(f"...specifically no {banned}=", banned not in q)
    src = open("/home/ohmz/ai-stack/scripts/web_search.py").read()
    check("the default instance is the hermes one, not chat's",
          ws.SEARXNG.endswith(":8889"), ws.SEARXNG)
    default_line = [ln for ln in src.splitlines() if ln.startswith("SEARXNG =")][0]
    # A cron job's terminal has no guaranteed environment, so a :8888 default would silently mean
    # no fan-out AND monitors spending the engine budget chat depends on — with no error anywhere.
    check("...and its DEFAULT cannot be the chat instance",
          "8889" in default_line and "8888" not in default_line, default_line)

    print("--- an outage is zero results AND dead engines, never one of the two ---")
    serve({"results": [], "unresponsive_engines": [["google", "CAPTCHA"], ["brave", "429"]]})
    try:
        ws.search("x")
        check("zero results + dead engines raises RosterOutage", False)
    except ws.RosterOutage as e:
        check("zero results + dead engines raises RosterOutage", True)
        check("...and it IS a RuntimeError, so the caller's existing except-path still catches it",
              isinstance(e, RuntimeError))
        check("...naming the engines, because 'unresponsive' alone diagnoses nothing",
              "google" in str(e), str(e))
    serve({"results": [row("https://a.example.com/p")],
           "unresponsive_engines": [["google", "CAPTCHA"], ["brave", "429"], ["qwant", "x"],
                                   ["startpage", "y"]]})
    rows = ws.search("x")
    check("results + FOUR of six dead is a healthy search: it returns and does not raise",
          len(rows) == 1, rows)
    check("...and the dead list is recorded for the scoreboard denominator",
          ws.LAST_DEAD == ["google", "brave", "qwant", "startpage"], ws.LAST_DEAD)
    serve({"results": []})
    check("a genuinely empty search is [] — the fruitless path, not an outage", ws.search("x") == [])
    serve("this is not json")
    try:
        ws.search("x")
        check("a non-JSON body raises something that is NOT a RosterOutage", False)
    except ws.RosterOutage:
        check("a non-JSON body raises something that is NOT a RosterOutage", False)
    except Exception:
        check("a non-JSON body raises something that is NOT a RosterOutage", True)
    serve("[1, 2, 3]")
    try:
        ws.search("x")
        check("a JSON array body is rejected rather than treated as a result set", False)
    except ValueError:
        check("a JSON array body is rejected rather than treated as a result set", True)

    print("--- unresponsive_engines has had three shapes upstream; none of them may raise ---")
    for payload, want in [
        ({"unresponsive_engines": [["ddg", "CAPTCHA"]]}, ["ddg"]),
        ({"unresponsive_engines": [{"name": "bing", "reason": "x"}]}, ["bing"]),
        ({"unresponsive_engines": [{"engine": "qwant"}]}, ["qwant"]),
        ({"unresponsive_engines": ["mojeek"]}, ["mojeek"]),
        ({"unresponsive_engines": [[], None, 7]}, []),
        ({"unresponsive_engines": None}, []),
        ({}, []),
    ]:
        check(f"unresponsive({payload}) -> {want}", ws.unresponsive(payload) == want,
              ws.unresponsive(payload))
    check("a non-dict payload does not raise either", ws.unresponsive("junk") == [])

    print("--- infobox and junk rows are not result rows ---")
    junk = [None, "a string", 7, {}, {"url": None, "title": ""}, {"url": ""},
            {"url": "//x.example.com/y"}, {"url": "magnet:?xt=urn:btih:z"},
            {"url": "data:text/html,<p>x"}]
    check("every junk shape is rejected by _usable", not any(ws._usable(j) for j in junk))
    ranked = ws.rank(junk + [row("https://real.example.com/p")])
    check("...and rank() drops them all without crashing",
          [r["url"] for r in ranked] == ["https://real.example.com/p"], ranked)
    serve({"results": [], "infoboxes": [{"url": None, "title": ""}],
           "unresponsive_engines": [["google", "x"]]})
    try:
        ws.search("x")
        check("a payload of ONLY infoboxes plus dead engines is still an outage", False)
    except ws.RosterOutage:
        check("a payload of ONLY infoboxes plus dead engines is still an outage", True)

    print("--- ranking: score first, and the same input always gives the same order ---")
    grouped = [row("https://a.example.com/p", score=0.5, engines=("google",), positions=(1,)),
               row("https://b.example.com/p", score=3.0, engines=("mojeek",), positions=(4,))]
    check("a score-3.0 row from engine two beats a score-0.5 row from engine one",
          [r["url"] for r in ws.rank(grouped)] == ["https://b.example.com/p",
                                                   "https://a.example.com/p"])
    base = [row(f"https://h{i}.example.com/p", score=1.0) for i in range(9)]
    orders = []
    for _ in range(3):
        shuffled = base[:]
        random.shuffle(shuffled)
        orders.append([r["url"] for r in ws.rank(shuffled)])
    check("three shuffles of equal-scoring rows produce a byte-identical order",
          orders[0] == orders[1] == orders[2], orders)
    consensus = [row("https://one.example.com/p", score=2.0, engines=("google",)),
                 row("https://three.example.com/p", score=2.0,
                     engines=("google", "brave", "mojeek"))]
    check("a URL three engines agree on outranks one only google found",
          ws.rank(consensus)[0]["url"] == "https://three.example.com/p")
    check("a plausible-money snippet outranks a silent one at equal score",
          ws.rank([row("https://q.example.com/p", score=2.0),
                   row("https://p.example.com/p", score=2.0, content="only $139.99")],
                  below=150)[0]["url"] == "https://p.example.com/p")
    check("missing score/positions/engines keys do not crash and sort last-ish",
          len(ws.rank([{"url": "https://n.example.com/p"}, row("https://m.example.com/p")])) == 2)
    check("MERGE_CAP bounds the output", len(ws.rank(
        [row(f"https://cap{i}.example.com/p") for i in range(200)])) == ws.MERGE_CAP)

    print("--- dedupe folds tracking junk, never real parameters ---")
    dupes = [row("https://www.x.example.com/p?utm_source=a", score=1.0, engines=("google",),
                 positions=(2,)),
             row("http://x.example.com/p/", score=4.0, engines=("brave", "bing"), positions=(1,)),
             row("https://x.example.com/p", score=2.0, engines=("mojeek",), positions=(5,))]
    m = ws.rank(dupes)
    check("three spellings of one page merge to one row", len(m) == 1, m)
    check("...keeping a byte-identical url from the input",
          m[0]["url"] in [d["url"] for d in dupes], m[0]["url"])
    check("...the highest score", m[0]["score"] == 4.0, m[0]["score"])
    check("...the union of finders", m[0]["engines"] == ("bing", "brave", "google", "mojeek"),
          m[0]["engines"])
    check("...and the best position", min(m[0]["positions"]) == 1, m[0]["positions"])
    fares = [row("https://f.example.com/s?from=YYZ&to=YVR"),
             row("https://f.example.com/s?from=YVR&to=YYZ")]
    check("two fare URLs differing only in real params stay TWO pages — a fare IS its query string",
          len(ws.rank(fares)) == 2, ws.rank(fares))
    check("engines_of reads engines[], not engine",
          ws.engines_of({"engines": ["brave", "google"], "engine": "brave"})
          == ("brave", "google"))
    check("...falls back to engine when engines[] is absent",
          ws.engines_of({"engine": "mojeek"}) == ("mojeek",))
    check("...and to a placeholder when neither is present", ws.engines_of({}) == ("?",))

    print("--- snippets: a bounded int, and the parsed number never escapes ---")
    for content, below, want in [
        ("Only $139.99 today", 150, 1),
        ("$19.99 charging cable", 150, -1),
        ("no price here at all", 150, 0),
        ("from $1,299.00 for the bundle", 150, 0),
        ("rated 4.99 out of 5 by 1,299 reviews", 150, 0),
        (None, 150, 0),
        ("", 150, 0),
        ("$139.99", None, 0),
    ]:
        got = ws.snippet_prior(content, below=below)
        check(f"snippet_prior({content!r}, below={below}) == {want}", got == want, got)
    adversarial = ["$" * 40, "x" * 4000, "$0.00", "$999999999.99",
                   " ".join(f"${i}.99" for i in range(30)), "€1.00 £2.00", "\n$5.00\n"]
    check("every adversarial input still returns an int in {-1, 0, 1}",
          all(type(ws.snippet_prior(c, below=150)) is int
              and ws.snippet_prior(c, below=150) in (-1, 0, 1) for c in adversarial))
    body = src.split("def snippet_prior", 1)[1].split("\ndef ", 1)[0]
    # The structural half of the firewall: the money regex has exactly one use site — its
    # definition and one reference, both accounted for — and that site is inside snippet_prior,
    # whose return type is an int. rank() copies a snippet around; nothing else interprets one, so
    # there is no field on any row, meta dict or state file that a snippet number could ride on.
    check("only snippet_prior can parse money out of a snippet",
          src.count("MONEY_IN_TEXT") == 2 and "MONEY_IN_TEXT" in body,
          f"{src.count('MONEY_IN_TEXT')} references")
    readers = sorted(n.name for n in ast.parse(src).body
                     if isinstance(n, ast.FunctionDef) and "content" in ast.get_source_segment(src, n))
    check("...and only rank() and snippet_prior() touch a row's content at all",
          readers == ["rank", "snippet_prior"], readers)

    print("--- the scoreboard: an empty board IS the static roster ---")
    check("cold start returns exactly ENGINE_ORDER",
          ws.order({}, "fare") == {e: i for i, e in enumerate(ws.ENGINE_ORDER)}, ws.order({}, "fare"))
    b = {"kinds": {"fare": {"google": {"tries": 3, "wins": 3},
                            "brave": {"tries": 10, "wins": 0}}}}
    o = ws.order(b, "fare")
    check("3-for-3 (0.8) outranks unproven (0.5) outranks 0-for-10 (0.083)",
          o["google"] < o["mojeek"] < o["brave"], o)
    tied = {"kinds": {"fare": {"mojeek": {"tries": 4, "wins": 2}, "bing": {"tries": 4, "wins": 2}}}}
    t = ws.order(tied, "fare")
    check("an exact tie is broken by ENGINE_ORDER, so the ordering is reproducible",
          t["mojeek"] < t["bing"], t)

    print("--- tries are roster-minus-dead; wins credit every finder, once ---")
    b = {}
    ws.credit_tries(b, "fare", dead=["google", "startpage"])
    k = b["kinds"]["fare"]
    check("a dead engine gets no try", "google" not in k and "startpage" not in k, k)
    check("...and every live one does",
          all(k[e]["tries"] == 1 for e in ("brave", "qwant", "mojeek", "bing")), k)
    check("a silent engine still gets a denominator — no 1-for-1 leaderboard jumps",
          k["qwant"]["tries"] == 1 and k["qwant"]["wins"] == 0, k)
    ws.credit_win(b, "fare", ("brave", "bing"))
    check("both finders of the winning row are credited",
          k["brave"]["wins"] == 1 and k["bing"]["wins"] == 1, k)
    ws.credit_win(b, "fare", ("duckduckgo",))
    check("an off-roster winner cannot exceed a rate of 1.0",
          k["duckduckgo"]["wins"] <= k["duckduckgo"]["tries"], k)
    for junk in (None, "", "wat", "PRICE_DROP!!", "../../etc/passwd"):
        ws.credit_tries(b, ws.kind_key(junk))
    check("kind_key collapses unknown values, so the board cannot grow keys from a typo",
          set(b["kinds"]) <= {"fare", "default", "other"}, sorted(b["kinds"]))
    check("kind_key: None -> default, a known kind -> itself, anything else -> other",
          (ws.kind_key(None), ws.kind_key("fare"), ws.kind_key("FARE"), ws.kind_key("zz"))
          == ("default", "fare", "fare", "other"))

    print("--- decay bounds the file without reordering it ---")
    b2 = {"kinds": {"k": {"google": {"tries": 22, "wins": 18},
                          "bing": {"tries": 22, "wins": 4},
                          "mojeek": {"tries": 20, "wins": 10}},
                    "other": {"brave": {"tries": 3, "wins": 1}}}}
    before = ws.order(b2, "k")
    ws._decay(b2, "k")
    check("counters halve once the busiest engine passes SCORE_CAP",
          max(s["tries"] for s in b2["kinds"]["k"].values()) <= ws.SCORE_CAP, b2["kinds"]["k"])
    check("...the ordering is unchanged, which is what makes decay safe",
          ws.order(b2, "k") == before, (before, ws.order(b2, "k")))
    check("...wins <= tries still holds",
          all(s["wins"] <= s["tries"] for s in b2["kinds"]["k"].values()), b2["kinds"]["k"])
    check("...and another kind is untouched", b2["kinds"]["other"]["brave"]["tries"] == 3)

    print("--- board_update is read-modify-write, and degrades rather than failing a run ---")
    ws.board_update(pw, lambda bd: ws.credit_tries(bd, "fare"), now=11)
    ws.board_update(pw, lambda bd: ws.credit_tries(bd, "fare"), now=12)
    saved = pw.read_state(ws.BOARD_STATE)
    check("two sequential updates accumulate — not a read-once/write-at-end bug",
          saved["kinds"]["fare"]["google"]["tries"] == 2, saved)
    check("...and the write is stamped", saved["updated"] == 12 and saved["v"] == 1, saved)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def no_fcntl(name, *a, **kw):
        if name == "fcntl":
            raise ImportError("no fcntl on this host")
        return real_import(name, *a, **kw)
    if isinstance(__builtins__, dict):
        __builtins__["__import__"] = no_fcntl
    else:
        __builtins__.__import__ = no_fcntl
    try:
        ws.board_update(pw, lambda bd: ws.credit_tries(bd, "fare"), now=13)
        check("with no fcntl it proceeds unlocked: a lost counter beats a failed monitor run",
              pw.read_state(ws.BOARD_STATE)["kinds"]["fare"]["google"]["tries"] == 3)
    except Exception as e:
        check("with no fcntl it proceeds unlocked: a lost counter beats a failed monitor run",
              False, repr(e))
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = real_import
        else:
            __builtins__.__import__ = real_import

    print("--- the roster in code and the roster in the container cannot drift ---")
    cfg = open(HERMES_SETTINGS).read()
    keep = re.findall(r"^      - (\w+)", cfg, re.M)
    flip = re.findall(r"^  - name: (\w+)", cfg, re.M)
    check("the drift check found both directives", len(keep) >= 5 and len(flip) >= 5, (keep, flip))
    check("ENGINE_ORDER equals keep_only — 'tries = roster minus dead' is only honest if it does",
          sorted(ws.ENGINE_ORDER) == sorted(keep), (ws.ENGINE_ORDER, keep))
    check("...and every one is also in the engines: block, so a bad NAME fails loudly at start",
          sorted(keep) == sorted(flip), (keep, flip))
    check("google is on the hermes roster — the whole point of the second instance",
          "google" in keep)

    print("--- the chat instance is not touched by any of this ---")
    check("compose/searxng/settings.yml still matches its pinned sha256",
          hashlib.sha256(open(CHAT_SETTINGS, "rb").read()).hexdigest() == CHAT_SETTINGS_SHA256,
          "if this change was deliberate, re-pin it and review the diff")
    chat = open(CHAT_SETTINGS).read()
    for engine in ("google", "startpage", "brave", "qwant"):
        check(f"...and {engine} is still absent from the chat roster",
              not re.search(r"^      - " + engine + r"\b", chat, re.M))
    comp = open(COMPOSE).read()
    check("the hermes service binds loopback :8889", '"127.0.0.1:8889:8080"' in comp)
    digests = re.findall(r"image: searxng/searxng@(sha256:[0-9a-f]+)", comp)
    check("both instances run the SAME pinned digest — one JSON shape to test",
          len(digests) == 2 and digests[0] == digests[1], digests)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

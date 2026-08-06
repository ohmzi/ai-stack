#!/usr/bin/env python3
"""The web-search layer for BACKGROUND MONITORS. One fan-out query, merged and ranked.

Why this is a separate module from price_search.py. price_search answers one question — "which URL
should price_watch watch for this query?" — and this file answers a different one: "what did the web
say, in what order should we believe it, and which engine has earned that trust?". The scoreboard
here is GLOBAL state, which has no business inside a script whose every other line is per-monitor.
Everything in this file is either one HTTP call or a pure function over dicts, which is why its test
suite needs no filesystem and no clock.

Two instances, and the reason the split exists
----------------------------------------------
This module talks to the HERMES SearXNG instance (:8889, compose/searxng-hermes/settings.yml),
never to the chat instance (:8888, compose/searxng/settings.yml). Engine rate limits are per source
IP, so the rosters are kept almost disjoint: google, startpage, brave and qwant are hermes-only. A
monitor therefore cannot CAPTCHA an engine OpenWebUI chat depends on — which matters because a
degraded chat search fails silently, with the model answering from training data and still looking
grounded.

The fan-out multiplies engines PER QUERY, not queries PER ENGINE
---------------------------------------------------------------
SearXNG already fans out: one /search request dispatches one upstream request per enabled engine,
in parallel, then merges same-URL hits across engines and reports per-engine failures in
`unresponsive_engines`. So six engines cost ONE client call, and each engine still sees exactly one
query per monitor per SEARCH_COOLDOWN_S. Issuing six calls with `engines=google`, `engines=brave`,
... would produce the same six upstream queries plus five extra round trips, a hand-rolled merge,
and the `engines=` typo hazard below. There is no version of N calls that is better.

The query string carries no knobs
---------------------------------
Only `q` and `format=json` are ever sent. Every other parameter is a trap, in one of two ways:
`time_range`, `safesearch` and `pageno` return a hard HTTP 400 on a bad value — an outage the
caller cannot distinguish from a real one — while `engines` and `categories` are SILENTLY IGNORED
and fall back to SearXNG's full default roster of dozens of engines. That second failure mode is
the fastest way to CAPTCHA this IP and it produces no error anywhere. Every knob that could 400 or
silently fall back therefore lives in settings.yml, where a bad value fails once at bring-up under
human eyes instead of once per monitor run.

Measured constraints (docs/CAPABILITY_UPGRADE_PLAN.md:427-458)
--------------------------------------------------------------
~50-60 queries per engine per 10 min CAPTCHA'd google, duckduckgo, startpage and brave at once. A
CAPTCHA lasts ~3600 s; a plain rate-limit ~180 s; probing at expiry RE-TRIGGERS it. HTTP 200 is
returned even with four of six engines dead — `unresponsive_engines` is the only signal, and its
entries are [name, reason] ARRAYS, not objects. `results[]` is NOT sorted by score; it is grouped
by engine, which is what rank() below fixes.
"""
import json
import os
import re
import urllib.parse
import urllib.request

# The hermes instance. Same env override name the other consumers use, so a bisect or a --selftest
# can be pointed at :8888 without editing code.
SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8889")
SEARCH_TIMEOUT = 20
# Bound the ranking work. pick_url only ever FETCHES SEARCH_TOP_K of these, so a deeper list buys
# nothing; a six-engine fan-out on a popular query returns well over a hundred rows.
MERGE_CAP = 40

# The roster of compose/searxng-hermes/settings.yml, in the order to prefer on a cold board.
# A drift test asserts this tuple equals that file's roster in both of its directives — the
# "tries = roster minus dead" bookkeeping below is only honest if it does.
ENGINE_ORDER = ("google", "startpage", "brave", "qwant", "mojeek", "bing")

# The scoreboard lives in the monitor-state directory so it inherits price_watch's atomic write and
# its tests' tempdir isolation. The leading underscore keeps it out of the --state namespace the
# agent invents slugs in; price_search.main() also rejects a --state that starts with one.
BOARD_STATE = "_engine_scores"
BOARD_LOCK = BOARD_STATE + ".lock"
# When a kind's busiest engine passes this many tries, halve that kind's counters. Bounded file,
# recency-weighted, and no timestamps to reason about. Without it a long-lived board ossifies:
# an engine that won ten times in March would outrank one winning every week in August.
SCORE_CAP = 20

# The kinds price_search documents. A typo'd --kind must not grow a key on the board.
BOARD_KINDS = frozenset(("price_drop", "price_rise", "back_in_stock", "out_of_stock", "fare",
                         "inventory", "availability", "threshold", "change"))

# Dropped from the dedupe KEY only. A whitelist, not a blacklist: a fare page IS its query string
# (?from=YYZ&to=YVR&date=...), so two URLs differing in any parameter not named here are two
# different pages and must both survive.
JUNK_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
               "gclid", "gbraid", "wbraid", "msclkid", "fbclid", "srsltid", "mc_cid", "mc_eid",
               "ref", "ref_", "referrer", "tag", "_encoding", "th", "psc")

# A currency mark is required. Without it "rated 4.99 out of 5" and "1,299 reviews" are money.
MONEY_IN_TEXT = re.compile(r"[£$€]\s?([0-9][0-9,]*\.[0-9]{2})")

# The engines that failed the most recent search(), read by price_search immediately afterwards to
# credit the scoreboard's denominators. A module global rather than a second return value because
# search()'s one-argument, one-list shape IS the seam the test suite mocks, and widening it would
# cost the ~40 existing checks that drive the real function through a patched urlopen.
LAST_DEAD = []


class RosterOutage(RuntimeError):
    """Zero results AND dead engines: an outage wearing an empty search's clothes.

    Subclasses RuntimeError deliberately. price_search.run()'s except-branch and its suite both
    catch RuntimeError, so a CAPTCHA'd roster keeps taking exactly the path it takes today; the
    only thing the distinct type adds is that the caller can apply a backoff to THIS case without
    touching a transport failure, which must keep retrying.
    """


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _usable(r):
    """Is this a result ROW?

    infoboxes[] entries are not rows and must never be merged in: they carry url=None and title="",
    and code that treats them as rows crashes on the first urlparse. Same for answers[],
    corrections[] and suggestions[]. Non-http schemes are dropped as free insurance — nothing
    downstream can fetch a magnet: or a data: URL.
    """
    return (isinstance(r, dict) and isinstance(r.get("url"), str)
            and r["url"].startswith(("http://", "https://")))


def unresponsive(data):
    """-> [engine name]. Never raises.

    On this build the entries are [name, reason] ARRAYS, but that shape has drifted upstream before
    (it has been dicts, and bare strings), and this value decides whether a search is an outage or
    a fruitless hunt. Getting a TypeError here would turn a partial roster failure into a crashed
    monitor run, so every shape is accepted and anything unrecognisable is ignored.
    """
    out = []
    for e in (data.get("unresponsive_engines") or []) if isinstance(data, dict) else []:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict):
            n = e.get("name") or e.get("engine")
            if n:
                out.append(str(n))
        elif isinstance(e, (list, tuple)) and e:
            out.append(str(e[0]))
    return out


def engines_of(row):
    """-> tuple of engine names that found this URL.

    Read `engines`, not `engine`. SearXNG merges a URL found by several engines into ONE row and
    lists them all in engines[]; `engine` holds only the first. Attributing a consensus hit to that
    one name would systematically credit whichever engine SearXNG happened to list first, which is
    an artefact of its merge order rather than evidence about the engine.
    """
    e = row.get("engines") if isinstance(row, dict) else None
    if isinstance(e, (list, tuple)):
        names = tuple(sorted(str(x) for x in e if x))
        if names:
            return names
    one = row.get("engine") if isinstance(row, dict) else None
    return (str(one),) if one else ("?",)


def norm(url):
    """The dedupe KEY for a URL. Never the URL we fetch.

    SearXNG dedupes byte-identical URLs across engines; it does not fold http/https, a `www.`
    prefix, a trailing slash, a fragment, or tracking parameters. A six-engine fan-out therefore
    produces near-duplicates that would each burn one of only four audition fetches, and each land
    on a different `seen_hosts` entry.
    """
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port:
        host = f"{host}:{p.port}"
    path = re.sub(r"/+$", "", p.path) or "/"
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in JUNK_PARAMS]
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(sorted(keep)), ""))


def snippet_prior(content, below=None, above=None):
    """-> -1 | 0 | +1. A RANKING hint about relevance. NEVER a price.

    A snippet is a third party's rendering of the page at an unknown time: routinely a range
    ("from $199"), the wrong variant, a stale cache, or an adjacent sponsored listing. It is
    evidence about which result to AUDITION, never about what the item costs. This subsystem exists
    because a model once reported a price that appeared on neither variant of the page it claimed
    to have read, so the barrier has to be structural rather than a rule someone remembers at 2am.

    The float parsed here dies inside this function: the return type is an int in {-1, 0, 1} and
    nothing else in this module reads results[].content. A snippet number therefore has no path to
    price_search's meta["price"], to score_candidate(), to pw.emit(), or to any printed line —
    every reported number comes from price_watch's extraction of the FETCHED page.

      +1  a plausible value for this monitor's own target
      -1  EVERY value found is far below the target — the accessory smell, seen BEFORE a fetch is
          spent on it. The cheap mirror of price_search.score_candidate's same rule, which can only
          apply it after the page has been fetched.
       0  no money, no target to judge against, or out of band in either direction (a bundle, a
          different currency, a multi-item page) — unknown, which is not the same as bad
    """
    vals = [_num(m.replace(",", ""), 0.0) for m in MONEY_IN_TEXT.findall(content or "")]
    vals = [v for v in vals if v > 0]
    if not vals:
        return 0
    if below is not None:
        lo, hi = 0.2 * below, 3.0 * below
        if any(lo <= v <= hi for v in vals):
            return 1
        return -1 if all(v < lo for v in vals) else 0
    if above is not None:
        return 1 if any(0.33 * above <= v <= 5.0 * above for v in vals) else 0
    return 0


def search(query):
    """One fan-out query -> [result row]. Rows are usable-only, in SearXNG's own order.

    Ordering and merging are rank()'s job, not this function's: they need the caller's target and
    the scoreboard. This function is transport plus the two guards whose failure is silent.

    Raises RosterOutage when there are zero results AND dead engines, and lets any transport or
    JSON failure propagate. Returns [] for a genuinely empty search — the engines answered and
    nothing matched.

    The CONJUNCTION is load-bearing and gets more so with six engines. `unresponsive_engines` is
    non-empty on a large fraction of perfectly HEALTHY searches here: one CAPTCHA'd google beside
    thirty good rows from brave, mojeek and bing is normal weather, and the canary already treats a
    single dead engine that way. If dead-engines alone raised, that healthy search would take the
    outage path, which burns no cooldown — so an every-5m job would re-search every five minutes,
    and since probing at expiry re-triggers the block (measured), one CAPTCHA would become a
    permanent one. Requiring zero results too is what keeps a partial roster failure a non-event:
    results arrived, so the search is judged by whether a page survived scoring, exactly as before.
    """
    # Cleared first, so a transport or JSON failure cannot leave the PREVIOUS query's dead list
    # behind for the next reader to mistake for this one's.
    LAST_DEAD[:] = []
    url = f"{SEARXNG}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(url, timeout=SEARCH_TIMEOUT) as r:
        data = json.load(r)
    if not isinstance(data, dict):
        raise ValueError(f"search backend returned {type(data).__name__}, not an object")
    rows = [r for r in (data.get("results") or []) if _usable(r)]
    dead = unresponsive(data)
    LAST_DEAD[:] = dead
    if not rows and dead:
        raise RosterOutage(f"{len(dead)} search engines unresponsive: {','.join(dead[:6])}")
    return rows


def rank(rows, engine_rank=None, below=None, above=None):
    """Merge near-duplicate rows, then order them deterministically. -> [row], capped at MERGE_CAP.

    The sort key, most significant first:

      1. score, bucketed to 0.5. THE fix for the measured finding that results[] is grouped by
         engine rather than sorted by score: without it, a four-fetch audition budget can be spent
         entirely inside whichever engine's block happened to come first. Bucketing keeps SearXNG's
         cross-engine consensus as the lead signal without over-reading its float.
      2. the snippet prior — breaks SearXNG's ties with a plausible-money hint (never a price).
      3. how many engines found the URL. A page three engines agree on beats one only google found;
         in practice that is the canonical product page rather than a syndicated copy.
      4. the best position any engine gave it.
      5. engine_rank — the learned ordering. This is the ONLY thing the scoreboard influences, and
         it sits below every signal about the result itself: learned ordering may change who is
         auditioned, never who wins. The wrong-product gate stays a pure function of the page.
      6. the normalised URL, so the result is a total order.

    Keys 4-6 exist because the INCOMING order of results[] depends on which engines answered and in
    what order, which varies run to run. A tie-break on "original index" would therefore not be
    reproducible, and two runs of the same monitor could audition different pages.
    """
    engine_rank = engine_rank or {}
    merged = {}
    for r in rows:
        if not _usable(r):
            continue
        key = norm(r["url"])
        m = merged.get(key)
        if m is None:
            # Keep the FIRST-seen url byte-identical: norm() is a key, and fetching a URL we
            # rewrote would be fetching a page nobody offered us.
            merged[key] = m = {"url": r["url"], "title": r.get("title") or "",
                               "content": r.get("content") or "", "score": _num(r.get("score")),
                               "engines": [], "positions": []}
        else:
            m["score"] = max(m["score"], _num(r.get("score")))
            m["title"] = m["title"] or (r.get("title") or "")
            m["content"] = m["content"] or (r.get("content") or "")
        for e in engines_of(r):
            if e not in m["engines"]:
                m["engines"].append(e)
        pos = r.get("positions")
        if isinstance(pos, list):
            m["positions"] += [int(p) for p in pos if isinstance(p, int)]

    def key(m):
        best_engine = min((engine_rank.get(e, len(ENGINE_ORDER)) for e in m["engines"]),
                          default=len(ENGINE_ORDER))
        return (-int(m["score"] * 2) / 2.0,
                -snippet_prior(m["content"], below, above),
                -len(m["engines"]),
                min(m["positions"]) if m["positions"] else 99,
                best_engine,
                norm(m["url"]))

    out = sorted(merged.values(), key=key)
    for m in out:
        m["engines"] = tuple(sorted(m["engines"]))
    return out[:MERGE_CAP]


# --------------------------------------------------------------------------------------------
# The scoreboard: which engine has actually produced a usable page, per watch kind.
#
# Deterministic bookkeeping, not a model deciding. Every model-guessing tier in this stack's
# history has had to be measured and walked back, so what biases the ordering here is a pair of
# integers per engine and nothing else.
#
# It is deliberately GLOBAL and keyed by kind, not per monitor. A monitor searches roughly ONCE per
# lifetime — need_search() is false while its page is healthy — so per-monitor counters would hold
# one data point and could never say anything. "Best for this inquiry" varies by kind, though:
# google may win for fares, bing for retail, mojeek for obscure items, and that is learnable across
# monitors of the same kind.
# --------------------------------------------------------------------------------------------

def kind_key(kind):
    """The board key for a --kind value. Unknown values collapse to 'other'.

    A typo'd or invented --kind must not grow a key: the board is a bounded file, and a kind with
    one observation in it is noise that outranks a kind with twenty.
    """
    k = (kind or "").strip().lower()
    if not k:
        return "default"
    return k if k in BOARD_KINDS else "other"


def _slot(board, kind, engine):
    k = board.setdefault("kinds", {}).setdefault(kind, {})
    return k.setdefault(engine, {"tries": 0, "wins": 0})


def credit_tries(board, kind, dead=()):
    """Every roster engine that was NOT unresponsive took part in this search.

    Roster-minus-dead, not "every engine that returned a row". An engine that legitimately returns
    nothing for a query would otherwise carry no denominator, so the first time it happened to
    surface a winner it would show 1/1 = 100% and jump the board on one observation.
    """
    dead = {str(d) for d in dead}
    for e in ENGINE_ORDER:
        if e not in dead:
            _slot(board, kind, e)["tries"] += 1
    _decay(board, kind)
    return board


def credit_win(board, kind, engines):
    """The chosen URL yielded a real reading. Credit every engine that found it.

    All of them, not just the first: a URL three engines agree on is evidence about all three, and
    crediting only one would favour SearXNG's merge order rather than the engines.
    """
    for e in engines or ():
        s = _slot(board, kind, str(e))
        s["wins"] += 1
        # An engine can win without ever having been credited a try — SEARXNG_URL can be pointed at
        # the chat instance, whose roster is not ENGINE_ORDER. Keep wins <= tries so the rate below
        # can never exceed 1 and read as better than perfect.
        s["tries"] = max(s["tries"], s["wins"])
    _decay(board, kind)
    return board


def _decay(board, kind):
    """Halve a kind's counters once its busiest engine passes SCORE_CAP.

    Wins are scaled proportionally rather than floor-halved so the win RATE — the only thing
    order() reads — survives the operation; a floor on both numbers would let a rounding artefact
    reorder the board.
    """
    k = (board.get("kinds") or {}).get(kind) or {}
    if not k or max((s.get("tries", 0) for s in k.values()), default=0) <= SCORE_CAP:
        return
    for s in k.values():
        t, w = s.get("tries", 0), s.get("wins", 0)
        nt = t // 2
        s["wins"] = min(nt, int(round(w * nt / t))) if t else 0
        s["tries"] = nt


def order(board, kind):
    """-> {engine: rank}. Laplace-smoothed win rate, descending; ties by ENGINE_ORDER.

    Laplace ((wins+1)/(tries+2)) rather than a minimum-tries threshold, because it gets all three
    cases right with no special-casing: an unproven engine rates 0.5, three-for-three rates 0.8 and
    rises, nought-for-ten rates 0.083 and sinks BELOW unproven — which is correct, since ten
    failures is information. It also keeps a temporarily CAPTCHA'd engine (tries without wins)
    able to climb back rather than being written off.

    The property that makes this safe to ship in halves: on an EMPTY board every engine rates 0.5,
    so this returns exactly ENGINE_ORDER. Shipping without any bookkeeping is literally the
    cold-start behaviour of shipping with it.
    """
    k = (board.get("kinds") or {}).get(kind) or {}

    def rate(e):
        s = k.get(e) or {}
        return (s.get("wins", 0) + 1) / (s.get("tries", 0) + 2)

    def idx(e):
        return ENGINE_ORDER.index(e) if e in ENGINE_ORDER else len(ENGINE_ORDER)

    seq = sorted(set(ENGINE_ORDER) | set(k), key=lambda e: (-rate(e), idx(e), e))
    return {e: i for i, e in enumerate(seq)}


def board_update(pw, mutate, now=None):
    """read -> mutate -> write, under a file lock. -> the written board.

    The lock matters because ordering is derived from the counters: two monitors firing in the same
    minute would both read, both mutate and both write, and os.replace guarantees no corruption but
    the loser's increment vanishes — which can flip an order and contradict reproducibility.

    Degrades to unlocked exactly as the pipe's GPU lock does: no fcntl, an unwritable path, or any
    host where flock is unavailable proceeds anyway. A lost counter is a slightly worse ordering; a
    failed monitor run is a silently broken monitor, which is the thing this stack refuses to ship.
    """
    fh = None
    try:
        import fcntl
        os.makedirs(pw.STATE_DIR, exist_ok=True)
        fh = open(os.path.join(pw.STATE_DIR, BOARD_LOCK), "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        if fh is not None:
            fh.close()
        fh = None
    try:
        board = pw.read_state(BOARD_STATE) or {}
        board.setdefault("v", 1)
        mutate(board)
        if now is not None:
            board["updated"] = int(now)
        pw.write_state(BOARD_STATE, board)
        return board
    finally:
        if fh is not None:
            fh.close()

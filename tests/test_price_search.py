#!/usr/bin/env python3
"""Search-first price watching: query -> page via SearXNG, then price_watch, pinned offline.

Why this file exists. The live failure of 2026-08-05: "create alert to search online for Google
Fitbit Air when the price is under 150" gave the agent nothing it had a vetted recipe for —
price_watch.py is URL-in only — so it improvised and narrated a job it never created. price_search
closes that gap deterministically, and the rules worth pinning are the ones whose failure is
silent:

  * **One search per monitor lifetime, cooled down even when broken.** The SearXNG engine roster
    CAPTCHA'd at ~60 queries/10 min (measured); a monitor that searched every 5-minute run would
    cause the outage it depends on not having.
  * **The wrong product must lose, and no product must beat a wrong one.** "Fitbit Air Charging
    Cable $19.99" carries the product's name and a high-confidence price; against a $150 target it
    is still not the watch the user asked for.
  * **The FIRST LOG line is the one delivered.** The SEARCH info line must never be it.
  * **A search outage is not a missing product.** SearXNG being down alerts nobody and burns no
    cooldown; a genuinely fruitless hunt tells the user once, with advice.

Everything downstream of resolution (thresholds, dampening, failure streaks, recovery, protocol)
is price_watch's engine reused by import and pinned by ITS suite; here it is smoke-checked only.

Usage:  python3 tests/test_price_search.py
"""
import importlib.util
import io
import json
import re
import sys
import tempfile
import types
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Args:
    """Mirrors the real argparse namespace; the drift check below keeps it honest."""
    def __init__(self, **kw):
        self.query = "google fitbit air"
        self.state = "t"
        self.below = None
        self.above = None
        self.alert_to = "ohmz"
        self.kind = None
        self.label = None
        self.unit = "$"
        self.monitor = "test monitor"
        self.schedule = "every 6h"
        self.require_confidence = False
        self.prefer_domain = None
        self.mode = "price"
        self.engine_scores = False
        self.__dict__.update(kw)


_RANK = [1000.0]


def R(url, title="", score=None, engines=("bing",), content=""):
    """A SearXNG result row.

    `score` defaults to a DESCENDING counter so that every existing `[R(a), R(b)]` literal still
    means "a ranks above b" — Python evaluates list literals left to right. That matters because
    pick_url now sorts by score before auditioning, and rank()'s last tie-break is the normalised
    URL: with every fixture scoring 0, these lists would silently re-order ALPHABETICALLY, and
    checks about a page winning "from second place" would pass while testing nothing. Pass `score`
    explicitly to test the ranking itself.
    """
    _RANK[0] -= 1.0
    return {"url": url, "title": title, "content": content,
            "score": _RANK[0] if score is None else score,
            "engines": list(engines), "positions": [1]}


JSONLD = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"%s"}}</script>'
FIX_DEVICE = "<title>Google Fitbit Air Smartwatch</title>" + JSONLD % "199.99"
FIX_DEVICE_CHEAP = "<title>Google Fitbit Air Smartwatch</title>" + JSONLD % "139.99"
FIX_DEVICE_CHEAP2 = "<title>Google Fitbit Air Smartwatch Sale</title>" + JSONLD % "129.99"
FIX_CABLE = "<title>Fitbit Air Charging Cable</title>" + JSONLD % "19.99"
FIX_LOWCONF = "<title>Google Fitbit Air Smartwatch deal</title><p>only $149.00 today</p>"
FIX_FARE = ("<title>Toronto to Karachi flights from great airlines</title>"
            "<p>from $849.99 return</p>")

DEV = "https://shop-a.example.com/google-fitbit-air"
DEV2 = "https://shop-b.example.com/google-fitbit-air-sale"
DEV3 = "https://shop-c.example.com/google-fitbit-air"
DEV4 = "https://shop-d.example.com/google-fitbit-air"
CABLE = "https://shop-e.example.com/fitbit-air-cable"
LOWC = "https://shop-f.example.com/google-fitbit-air-deal"
AMZ = "https://www.amazon.ca/dp/B0FITBIT01"
WIKI = "https://en.wikipedia.org/wiki/Fitbit"
DEV6 = "https://shop-g.example.com/google-fitbit-air"
DEV6B = "https://shop-h.example.com/google-fitbit-air"
DEV7 = "https://shop-i.example.com/google-fitbit-air"
DEV8 = "https://shop-j.example.com/google-fitbit-air"
DEV9 = "https://shop-k.example.com/google-fitbit-air"
DEV10 = "https://shop-l.example.com/google-fitbit-air"
DEV11 = "https://shop-m.example.com/google-fitbit-air"
DEV12 = "https://shop-n.example.com/google-fitbit-air"
AMZ2 = "https://www.amazon.ca/dp/B0FITBIT02"
FARE_URL = "https://fares.example.com/toronto-karachi"
DEV13 = "https://shop-o.example.com/google-fitbit-air"
DEV14 = "https://shop-p.example.com/google-fitbit-air"
DEV15 = "https://shop-q.example.com/google-fitbit-air"
DEV16 = "https://shop-r.example.com/google-fitbit-air"
DEV17 = "https://shop-s.example.com/google-fitbit-air"
DEV18 = "https://shop-t.example.com/google-fitbit-air"


class SearchStub:
    def __init__(self):
        self.calls, self.queries, self.results = 0, [], []

    def __call__(self, q):
        self.calls += 1
        self.queries.append(q)
        if isinstance(self.results, Exception):
            raise self.results
        return list(self.results)


def main():
    ps = load("/home/ohmz/ai-stack/scripts/price_search.py", "ps")
    hd = load("/home/ohmz/ai-stack/scripts/hermes_delivery.py", "hd")
    ps.pw.STATE_DIR = tempfile.mkdtemp()

    clock = [1000.0]
    ps.time = types.SimpleNamespace(time=lambda: clock[0])

    def advance(s):
        clock[0] += s

    fetch_map, fetch_log = {}, []

    def fake_fetch(url):
        fetch_log.append(url)
        v = fetch_map[url]
        if isinstance(v, Exception):
            raise v
        return v
    ps.pw.fetch = fake_fetch

    real_search = ps.search
    search = SearchStub()
    ps.search = search

    def run(**kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ps.run(Args(**kw))
        return rc, buf.getvalue()

    def sstate(name):
        return json.load(open(f"{ps.pw.STATE_DIR}/{name}.search.json"))

    print("--- the fixture matches the real CLI, flag for flag ---")
    src = open("/home/ohmz/ai-stack/scripts/price_search.py").read()
    flags = {m.replace("-", "_") for m in re.findall(r'add_argument\("--([a-z-]+)"', src)}
    fixture = set(Args().__dict__) | {"selftest"}
    check("every CLI flag exists on the test fixture", not (flags - fixture),
          f"missing: {sorted(flags - fixture)}")
    check("the check found the flags at all", len(flags) >= 10, sorted(flags))
    pw_src = open("/home/ohmz/ai-stack/scripts/price_watch.py").read()
    pw_flags = {m.replace("-", "_") for m in re.findall(r'add_argument\("--([a-z-]+)"', pw_src)}
    check("every price_watch flag except url/selector/selftest is forwarded",
          not (pw_flags - {"url", "selector", "selftest"} - flags),
          f"missing: {sorted(pw_flags - {'url', 'selector', 'selftest'} - flags)}")
    ns = ps.pw_namespace(Args(), "https://x.example.com/")
    check("pw_namespace provides every attribute price_watch's CLI defines",
          not (pw_flags - {"selftest"} - set(vars(ns))),
          f"missing: {sorted(pw_flags - {'selftest'} - set(vars(ns)))}")

    print("--- the search query is composed, not reformulated ---")
    for q, kind, want in [
        ("google fitbit air", None, "google fitbit air price"),
        ("google fitbit air price", None, "google fitbit air price"),
        ("toronto to karachi", "fare", "toronto to karachi flight price"),
        ("flights to karachi", "fare", "flights to karachi price"),   # never "flight flight"
    ]:
        got = ps.compose_query(q, kind)
        check(f"{q!r} ({kind}) -> {want!r}", got == want, got)

    print("--- picking: confidence beats rank, junk hosts are skipped ---")
    fetch_map.update({LOWC: FIX_LOWCONF, DEV: FIX_DEVICE, CABLE: FIX_CABLE,
                      AMZ: FIX_DEVICE, DEV2: FIX_DEVICE_CHEAP2})
    pick = ps.pick_url([R(LOWC), R(DEV)], "google fitbit air", 150, None, (), ())
    check("a later high-confidence page beats a rank-1 low-confidence one",
          pick is not None and pick[0] == DEV, repr(pick))
    del fetch_log[:]
    pick = ps.pick_url([R(WIKI), R(DEV)], "google fitbit air", 150, None, (), ())
    check("wikipedia is never even fetched", WIKI not in fetch_log and pick[0] == DEV, fetch_log)

    print("--- the accessory trap: a $19.99 cable must not win a $150 watch ---")
    pick = ps.pick_url([R(CABLE), R(DEV)], "google fitbit air", 150, None, (), ())
    check("the device beats the higher-ranked cable", pick is not None and pick[0] == DEV,
          repr(pick))
    pick = ps.pick_url([R(CABLE)], "google fitbit air", 150, None, (), ())
    check("an accessory-ONLY result set picks nothing rather than the wrong thing",
          pick is None, repr(pick))
    pick = ps.pick_url([R(FARE_URL)], "google fitbit air", 150, None, (), ())
    check("a page that never mentions the product is discarded", pick is None, repr(pick))

    print("--- known retailers are preferred, and preference defeats rank ---")
    pick = ps.pick_url([R(DEV), R(AMZ)], "google fitbit air", 150, None, ("amazon.ca",), ())
    check("amazon.ca wins a tie from second place", pick is not None and pick[0] == AMZ,
          repr(pick))
    del fetch_log[:]
    pick = ps.pick_url([R(DEV), R(DEV2)], "google fitbit air", 150, None, (), ())
    check("with no preference list a perfect page stops the auditions",
          fetch_log == [DEV] and pick[0] == DEV, fetch_log)

    print("--- the audition budget is a fetch budget: errors count ---")
    dead = [f"https://dead-{i}.example.com/google-fitbit-air" for i in range(6)]
    fetch_map.update({u: OSError("boom") for u in dead})
    del fetch_log[:]
    pick = ps.pick_url([R(u) for u in dead], "google fitbit air", 150, None, (), ())
    check("no pick from six dead pages", pick is None)
    check(f"...and only {ps.SEARCH_TOP_K} were ever fetched",
          len(fetch_log) == ps.SEARCH_TOP_K, fetch_log)

    print("--- one search per run, none while the cached page is healthy ---")
    search.results = [R(DEV)]
    fetch_map[DEV] = FIX_DEVICE_CHEAP
    rc, out1 = run(state="t1", below=150)
    check("run resolves, reads, and fires", rc == 0 and "ALERT(ohmz):" in out1, out1)
    check("exactly one search", search.calls == 1)
    check("the pick is announced", f"SEARCH: picked {DEV}" in out1, out1)
    check("the price line is a normal price_watch LOG", "LOG: 139.99" in out1, out1)
    st = sstate("t1")
    check("the choice is cached", st["url"] == DEV and st["search_count"] == 1, st)
    check("...with its provenance", st["chosen_conf"] == "high", st)
    rc, out = run(state="t1", below=150)
    check("a healthy cache means NO search", search.calls == 1)
    check("...and no SEARCH line", "SEARCH:" not in out, out)
    check("dampening is inherited from price_watch",
          "ALERT(" not in out and "(unchanged)" in out, out)

    print("--- the delivered LOG line is the price, never the SEARCH note ---")
    body = "## Response\n" + out1
    logs = hd.LOG_RE.findall(body)
    check("exactly one LOG line on a search-and-read run", len(logs) == 1, repr(logs))
    check("...and it is the price reading", bool(logs) and logs[0].startswith("139.99"),
          repr(logs))
    alerts = hd.ALERT_RE.findall(body)
    check("the ALERT parses with the right recipient",
          len(alerts) == 1 and alerts[0][0] == "ohmz", repr(alerts))
    data = [json.loads(m) for m in hd.ALERT_DATA_RE.findall(body)]
    check("ALERT_DATA parses and carries the chosen link — 'send me the link'",
          len(data) == 1 and data[0]["url"] == DEV, repr(data))
    check("the item named itself from the chosen page",
          data[0].get("item") == "Google Fitbit Air Smartwatch", repr(data))
    check("output is markdown-safe (channel renders it)",
          "[" not in out1 and "]" not in out1, out1)

    print("--- a dead page heals itself: re-search, reject, re-alert ---")
    fetch_map[DEV] = urllib.error.HTTPError(DEV, 404, "gone", {}, None)
    advance(700)
    rc, out = run(state="t1", below=150)
    check("first failure is price_watch's business, not a search",
          "LOG: check failed" in out and search.calls == 1, out)
    search.results = [R(DEV), R(DEV2)]
    advance(700)
    rc, out = run(state="t1", below=150)
    check("the confirmed-dead page triggers a re-search", search.calls == 2)
    check("the dead URL is rejected, the new page picked",
          f"SEARCH: picked {DEV2}" in out, out)
    st = sstate("t1")
    check("...and remembered as rejected", DEV in st["rejected"], st)
    check("the new page alerts immediately — old dampening must not leak",
          "ALERT(ohmz):" in out and '"kind": "price_drop"' in out, out)

    print("--- the search cooldown holds even while the page is broken ---")
    search.results = [R(DEV3)]
    fetch_map[DEV3] = FIX_DEVICE
    advance(700)
    rc, out = run(state="t2", below=150)
    check("resolves without alerting (199.99 is over target)",
          search.calls == 3 and "ALERT(" not in out, out)
    fetch_map[DEV3] = urllib.error.HTTPError(DEV3, 404, "gone", {}, None)
    advance(100)
    run(state="t2", below=150)
    advance(100)
    rc, out = run(state="t2", below=150)
    check("a re-search is due but the cooldown blocks it",
          search.calls == 3 and "LOG: check failed" in out, out)

    print("--- a search outage is an outage, not a missing product ---")
    search.results = ConnectionError("no route to host")
    advance(700)
    rc, out = run(state="t2", below=150)
    check("with a cached page: an info note, and price_watch still reports",
          "SEARCH: backend unreachable" in out and "LOG: check failed" in out, out)
    check("...the delivered LOG is price_watch's",
          hd.LOG_RE.findall("## Response\n" + out)[0].startswith("check failed"), out)
    rc, out = run(state="t5", below=150)
    check("with no page yet: the outage IS the log line",
          rc == 0 and out.startswith("LOG: search backend unreachable"), out)
    check("...and nobody is alerted about a product", "ALERT" not in out, out)
    rc, out = run(state="t5", below=150)
    check("an outage burns no cooldown — the next run tries again",
          out.startswith("LOG: search backend unreachable"), out)
    check("...and no fruitless streak accrues",
          sstate("t5").get("fruitless_streak", 0) == 0, sstate("t5"))

    print("--- a fruitless hunt tells the user once, with advice ---")
    search.results = []
    advance(700)
    rc, out = run(state="t3", below=150)
    check("first miss logs and keeps quiet",
          "LOG: no product page found yet" in out and "(search 1 in a row)" in out
          and "ALERT" not in out, out)
    calls = search.calls
    rc, out = run(state="t3", below=150)
    check("the cooldown applies to fruitless hunts too", search.calls == calls, out)
    check("...while the run still logs honestly", "no product page found yet" in out, out)
    advance(700)
    rc, out = run(state="t3", below=150)
    data = [json.loads(m) for m in hd.ALERT_DATA_RE.findall("## Response\n" + out)]
    check("the second real miss alerts, once", len(data) == 1, out)
    check("...as a not_found problem with the query as the item",
          bool(data) and data[0]["kind"] == "not_found"
          and data[0]["item"] == "google fitbit air", repr(data))
    # From here the cooldown ESCALATES, and these checks say so explicitly rather than passing
    # because a search happened to be blocked. Streak 2 means 2 x SEARCH_COOLDOWN_S, so the 700 s
    # that used to buy a search no longer does — which is the point: once the user has been told,
    # an unresolvable monitor must stop spending 144 queries a day on every engine.
    calls = search.calls
    advance(700)
    rc, out = run(state="t3", below=150)
    check("past the not_found alert the cooldown escalates — 700 s no longer buys a search",
          search.calls == calls and "no product page found yet" in out, out)
    advance(700)          # 1400 s since the last query, past the escalated 1200 s
    rc, out = run(state="t3", below=150)
    check("the third miss is a REAL search and it is silent — one alert per outage",
          search.calls == calls + 1 and "ALERT" not in out, out)
    search.results = [R(DEV4)]
    fetch_map[DEV4] = FIX_DEVICE_CHEAP
    advance(1900)         # streak 3 => 1800 s
    rc, out = run(state="t3", below=150)
    check("a later success resolves and fires",
          f"SEARCH: picked {DEV4}" in out and "ALERT(ohmz):" in out, out)
    st = sstate("t3")
    check("...and clears the fruitless bookkeeping",
          st["fruitless_streak"] == 0 and st["not_found_alerted"] is False, st)

    print("--- a fare watch is REFUSED, because a readable fare number is not a fare ---")
    # Measured twice in production. Job 99cdcb68d1e1 found no page at all. Then job 52f821a8d3a2
    # resolved cheapflights.ca and texted "$358.72, under your $1,000.00 target" at HIGH confidence,
    # read from a JSON-LD offers ARRAY of 358.72/360.12/362.92/364.32 — a list of unrelated
    # itineraries, on a page titled "C$ 146+". Every guard in this file was satisfied and the
    # reading was still meaningless, which is why refusing beats auditioning.
    search.results = [R(FARE_URL)]
    fetch_map[FARE_URL] = FIX_FARE
    calls = search.calls
    advance(700)
    rc, out = run(state="t4", query="toronto to karachi", kind="fare", below=900)
    check("no search is spent at all", search.calls == calls, out)
    check("it exits 0 — a config limit is not an infrastructure error", rc == 0, rc)
    check("the LOG line says it cannot work, and why",
          len(hd.LOG_RE.findall("## Response\n" + out)) == 1
          and "cannot work" in out and "JavaScript" in out, out)
    data = [json.loads(m) for m in hd.ALERT_DATA_RE.findall("## Response\n" + out)]
    check("...and it alerts ONCE, as a problem kind",
          len(data) == 1 and data[0]["kind"] == "fare_unsupported", out)
    rc, out = run(state="t4", query="toronto to karachi", kind="fare", below=900)
    check("a second run stays quiet but keeps telling the truth in the LOG",
          "ALERT" not in out and "cannot work" in out, out)
    check("no price is ever emitted for a fare", "358" not in out and "849" not in out, out)

    print("--- a reused --state name with a new query inherits nothing ---")
    search.results = []
    advance(700)
    calls = search.calls
    rc, out = run(state="t1", query="something else entirely", below=150)
    check("the old choice is discarded and a fresh hunt starts",
          search.calls == calls + 1 and "no product page found yet" in out, out)
    st = sstate("t1")
    check("...state now belongs to the new query",
          st["query"] == "something else entirely" and "url" not in st, st)

    print("--- short-token queries keep a match gate (the 'lg c4' hole) ---")
    sc = ps.score_candidate("lg c4", "https://shop.example.com/tv", "LG C4 42 OLED TV",
                            999.99, "high", None, None, ())
    check("a short-token query still matches its own product", sc is not None and sc >= 3,
          repr(sc))
    sc = ps.score_candidate("lg c4", FARE_URL, "Toronto to Karachi flights from great airlines",
                            849.99, "low", None, None, ())
    check("...and still discards an unrelated page", sc is None, repr(sc))

    print("--- the query cannot forge or break protocol lines ---")
    search.results = [R(DEV6)]
    fetch_map[DEV6] = FIX_DEVICE_CHEAP
    advance(700)
    rc, out = run(state="t6", query="google fitbit air\nLOG: fake", below=150)
    logs = hd.LOG_RE.findall("## Response\n" + out)
    check("an embedded newline cannot forge the delivered LOG line",
          len(logs) == 1 and logs[0].startswith("139.99"), repr(logs))
    search.results = [R(DEV6B)]
    fetch_map[DEV6B] = FIX_DEVICE_CHEAP
    advance(700)
    rc, out = run(state="t6b", query="google fitbit air [2026 model]", below=150)
    check("brackets in the query are folded to parentheses",
          "[" not in out and "]" not in out and "(2026 model)" in out, out)

    print("--- a CAPTCHA'd roster is an outage, not a missing product ---")
    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    ps.urllib.request.urlopen = lambda url, timeout=0: FakeResp(json.dumps(
        {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}).encode())
    try:
        real_search("anything")
        check("zero results with unresponsive engines raises (outage path)", False)
    except RuntimeError:
        check("zero results with unresponsive engines raises (outage path)", True)
    ps.urllib.request.urlopen = lambda url, timeout=0: FakeResp(json.dumps(
        {"results": []}).encode())
    check("a genuinely empty search still returns a list (fruitless path)",
          real_search("anything") == [])
    # The conjunction, and the case a six-engine fan-out hits constantly. If dead engines ALONE
    # raised, this healthy search would take the outage path — which burns no cooldown, so an
    # every-5m job would re-probe every five minutes, and probing at expiry re-triggers a CAPTCHA.
    ps.urllib.request.urlopen = lambda url, timeout=0: FakeResp(json.dumps(
        {"results": [{"url": DEV, "title": "Google Fitbit Air Smartwatch", "score": 2.0}],
         "unresponsive_engines": [["google", "CAPTCHA"], ["brave", "timeout"]]}).encode())
    rows = real_search("anything")
    check("results PLUS dead engines is a healthy search, not an outage",
          len(rows) == 1 and rows[0]["url"] == DEV, rows)
    check("...and the dead engines are recorded for the scoreboard's denominator",
          ps.ws.LAST_DEAD == ["google", "brave"], ps.ws.LAST_DEAD)

    print("--- a CAPTCHA'd roster earns a backoff that a transport outage does not ---")
    search.results = ps.ws.RosterOutage("2 search engines unresponsive: google,brave")
    advance(2000)
    rc, out = run(state="t12", below=150)
    check("with no page yet, the roster outage is the log line",
          out.startswith("LOG: search backend unreachable") and "RosterOutage" in out, out)
    calls = search.calls
    rc, out = run(state="t12", below=150)
    check("...and unlike a transport error it does NOT retry on the very next run",
          search.calls == calls, out)
    advance(ps.ROSTER_BACKOFF_S + 1)
    search.results = []
    rc, out = run(state="t12", below=150)
    check("...but it does retry once the backoff expires", search.calls == calls + 1, out)

    print("--- a query rebind keeps the cooldown (the flapping-job hole) ---")
    search.results = [R(DEV7)]
    fetch_map[DEV7] = FIX_DEVICE_CHEAP
    advance(700)
    run(state="t7", below=150)
    calls = search.calls
    rc, out = run(state="t7", query="an entirely different gadget", below=150)
    check("an immediate rebind cannot search again", search.calls == calls, out)
    check("...and says the hunt is pending, not failing",
          "first search is pending" in out, out)
    advance(700)
    rc, out = run(state="t7", query="an entirely different gadget", below=150)
    check("the rebind searches once the cooldown allows", search.calls == calls + 1, out)

    print("--- a page that stops showing a price re-searches before it alarms ---")
    search.results = [R(DEV8)]
    fetch_map[DEV8] = FIX_DEVICE_CHEAP
    advance(700)
    run(state="t8", below=150)
    fetch_map[DEV8] = "<title>Google Fitbit Air Smartwatch</title><p>coming soon</p>"
    calls = search.calls
    advance(700)
    rc, out = run(state="t8", below=150)
    check("first empty run stays with the page",
          search.calls == calls and "no value found" in out, out)
    advance(700)
    run(state="t8", below=150)
    search.results = [R(DEV9)]
    fetch_map[DEV9] = FIX_DEVICE_CHEAP2
    advance(700)
    rc, out = run(state="t8", below=150)
    check("two empty runs trigger the re-search, one run before pw's no_value alert",
          search.calls == calls + 1 and f"SEARCH: picked {DEV9}" in out, out)
    check("...and the healed monitor alerts on the new page", "ALERT(ohmz):" in out, out)

    print("--- rejected URLs: skipped while alive, capped FIFO ---")
    fetch_map[DEV10] = FIX_DEVICE_CHEAP
    fetch_map[DEV11] = FIX_DEVICE_CHEAP2
    del fetch_log[:]
    pick = ps.pick_url([R(DEV10), R(DEV11)], "google fitbit air", 150, None, (), (DEV10,))
    check("a rejected URL is never fetched again, even while alive",
          DEV10 not in fetch_log and pick[0] == DEV11, fetch_log)
    old_rej = [f"https://old-{i}.example.com/x" for i in range(ps.REJECTED_CAP)]
    json.dump({"query": "google fitbit air", "url": DEV10, "last_search_ts": 0,
               "rejected": old_rej, "search_count": 1},
              open(f"{ps.pw.STATE_DIR}/t9.search.json", "w"))
    json.dump({"url": DEV10, "fail_kind": "gone", "fail_streak": 1},
              open(f"{ps.pw.STATE_DIR}/t9.json", "w"))
    search.results = [R(DEV11)]
    advance(700)
    rc, out = run(state="t9", below=150)
    rej = sstate("t9")["rejected"]
    check("the rejected list is capped FIFO — newest kept, oldest dropped",
          len(rej) == ps.REJECTED_CAP and DEV10 in rej and old_rej[0] not in rej, rej)

    print("--- production wiring: DEFAULT_PREFER and --require-confidence through run() ---")
    search.results = [R(DEV12), R(AMZ2)]
    fetch_map[DEV12] = FIX_DEVICE_CHEAP
    fetch_map[AMZ2] = FIX_DEVICE_CHEAP
    advance(700)
    rc, out = run(state="t10", below=150)
    check("a known retailer beats an equal earlier-ranked page via run()",
          f"SEARCH: picked {AMZ2}" in out, out)
    search.results = [R(LOWC)]
    advance(700)
    rc, out = run(state="t11", below=150, require_confidence=True)
    check("--require-confidence is forwarded: a low-confidence firing is suppressed",
          "SUPPRESSED" in out and "ALERT(" not in out, out)

    print("--- a snippet can RANK a result but can never BE the price ---")
    search.results = [R(DEV13, content="was $1.00 today only, lowest ever")]
    fetch_map[DEV13] = FIX_DEVICE_CHEAP          # the page itself says 139.99
    advance(2000)
    rc, out = run(state="t13", below=150)
    check("the reported price is the fetched page's, not the snippet's",
          "LOG: 139.99" in out and "1.00" not in out, out)
    check("...and the snippet number reaches no state file either",
          "1.00" not in json.dumps(sstate("t13")), sstate("t13"))
    check("snippet_prior returns a bounded int, never a value",
          all(isinstance(ps.ws.snippet_prior(c, below=150), int)
              and ps.ws.snippet_prior(c, below=150) in (-1, 0, 1)
              for c in ("was $1.00", "$139.99 now", "rated 4.99 out of 5", None, "$" * 40)))

    print("--- the scoreboard: tries are the roster, wins are proven readings ---")

    def board():
        return ps.pw.read_state(ps.ws.BOARD_STATE)

    search.results = [R(DEV14, engines=("google", "brave"))]
    fetch_map[DEV14] = FIX_DEVICE_CHEAP
    advance(2000)
    rc, out = run(state="t14", below=150, kind="price_drop")
    b = board()["kinds"]["price_drop"]
    check("every engine on the roster is credited a try",
          all(b.get(e, {}).get("tries", 0) >= 1 for e in ps.ws.ENGINE_ORDER), b)
    check("BOTH finders of the winning row are credited the win",
          b["google"]["wins"] == 1 and b["brave"]["wins"] == 1, b)
    check("...and an engine that did not find it is not", b["mojeek"]["wins"] == 0, b)
    check("the SEARCH line names the finders (sorted, so the line is reproducible)",
          "brave+google" in out, out)
    check("...and the state remembers them",
          sstate("t14")["chosen_engines"] == ["brave", "google"], sstate("t14"))
    check("the pending marker is settled and cleared", "pending_win" not in sstate("t14"))
    rc, out = run(state="t14", below=150, kind="price_drop")
    check("a later healthy run does NOT credit a second win for the same pick",
          board()["kinds"]["price_drop"]["google"]["wins"] == 1, board())

    print("--- a pick is not a win until price_watch has read the page ---")
    first = [True]

    def once(url):
        fetch_log.append(url)
        if first[0]:
            first[0] = False
            return FIX_DEVICE_CHEAP
        raise OSError("boom")
    ps.pw.fetch = once
    search.results = [R(DEV15, engines=("mojeek",))]
    advance(2000)
    rc, out = run(state="t15", below=150, kind="price_rise")
    st = sstate("t15")
    check("the audition succeeded but the real read failed, so the win stays PENDING",
          st.get("pending_win", {}).get("engines") == ["mojeek"]
          and board()["kinds"]["price_rise"]["mojeek"]["wins"] == 0, st)
    ps.pw.fetch = fake_fetch
    fetch_map[DEV15] = FIX_DEVICE_CHEAP
    rc, out = run(state="t15", below=150, kind="price_rise")
    check("...and the next healthy run settles it",
          board()["kinds"]["price_rise"]["mojeek"]["wins"] == 1
          and "pending_win" not in sstate("t15"), sstate("t15"))

    print("--- the learned ordering decides who is AUDITIONED, never who wins ---")
    ps.pw.write_state(ps.ws.BOARD_STATE, {"v": 1, "kinds": {"inventory": {
        "brave": {"tries": 6, "wins": 5}, "google": {"tries": 6, "wins": 0}}}})
    fetch_map.update({DEV16: FIX_DEVICE_CHEAP, DEV17: FIX_DEVICE_CHEAP})
    order = ps.ws.order(board(), "inventory")
    del fetch_log[:]
    pick = ps.pick_url([R(DEV16, score=5.0, engines=("google",)),
                        R(DEV17, score=5.0, engines=("brave",))],
                       "google fitbit air", 150, None, (), (), engine_rank=order)
    check("with everything else equal, the engine with the better record is auditioned first",
          fetch_log[:1] == [DEV17] and pick[0] == DEV17, fetch_log)
    check("...but score_candidate never sees an engine or a snippet",
          "engine" not in ps.score_candidate.__code__.co_varnames
          and "content" not in ps.score_candidate.__code__.co_varnames)

    print("--- infobox-shaped rows never become a candidate ---")
    search.results = [{"url": None, "title": "", "content": ""},
                      {"title": "Fitbit", "content": "an infobox row carries no url"},
                      {"url": "magnet:?xt=urn:btih:x"}, R(DEV18)]
    fetch_map[DEV18] = FIX_DEVICE_CHEAP
    advance(2000)
    rc, out = run(state="t18", below=150)
    check("a payload carrying infobox and junk rows still resolves the real one",
          f"SEARCH: picked {DEV18}" in out, out)
    check("...and the audit counted the rows it was given",
          sstate("t18")["last_search"]["merged"] == 1, sstate("t18"))

    print("--- the audit block distinguishes 'no results' from 'nothing extractable' ---")
    # This is the diagnostic that answers the question the live fare failure could not: its state
    # file incremented the same fruitless_streak whether the engines returned nothing or four
    # fetched pages yielded no price. Those two want opposite fixes — more engines, or better
    # extraction — so the verdict has to survive into the state file.
    # The stub replaces ps.search outright, so it never reaches ws.search and never touches
    # ws.LAST_DEAD. Reset it here rather than inherit whatever the real-search checks above left.
    ps.ws.LAST_DEAD[:] = []
    search.results = []
    advance(2000)
    rc, out = run(state="t20", below=150)
    check("an empty result set is 'no_results' — more engines might help",
          sstate("t20")["last_search"] == {"rows": 0, "merged": 0, "fetched": 0,
                                          "dead": [], "reason": "no_results"}, sstate("t20"))
    search.results = [R(CABLE), R(FARE_URL)]
    advance(2000)
    rc, out = run(state="t21", below=150)
    ls = sstate("t21")["last_search"]
    check("pages found but none readable is 'no_candidate' — the roster was never the problem",
          ls["reason"] == "no_candidate" and ls["rows"] == 2 and ls["fetched"] >= 1, ls)
    search.results = [R(DEV13)]
    advance(2000)
    rc, out = run(state="t22", below=150)
    check("a successful resolve is 'picked'",
          sstate("t22")["last_search"]["reason"] == "picked", sstate("t22"))

    print("--- the auditions have a wall clock, and running out of it is not a fruitless hunt ---")
    slow = [f"https://slow-{i}.example.com/google-fitbit-air" for i in range(4)]

    def slow_fetch(url):
        fetch_log.append(url)
        advance(30)
        raise OSError("timed out")
    ps.pw.fetch = slow_fetch
    del fetch_log[:]
    audit = {}
    ps.pick_url([R(u) for u in slow], "google fitbit air", 150, None, (), (),
                audit=audit, started=clock[0])
    check("the deadline stops the auditions before the fetch budget does",
          len(fetch_log) == 2 and audit["reason"] == "deadline", (fetch_log, audit))
    search.results = [R(u) for u in slow]
    advance(4000)
    rc, out = run(state="t19", below=150)
    check("a deadline abort says so in the delivered LOG line",
          len(hd.LOG_RE.findall("## Response\n" + out)) == 1 and "ran out of time" in out, out)
    check("...and accrues NO fruitless streak: nothing was proven about the query",
          sstate("t19").get("fruitless_streak", 0) == 0, sstate("t19"))
    check("...but it does burn the cooldown, because the query WAS spent",
          sstate("t19")["last_search_ts"] > 0
          and sstate("t19")["last_search"]["reason"] == "deadline", sstate("t19"))
    ps.pw.fetch = fake_fetch

    print("--- the shared scoreboard is not reachable as a monitor name ---")
    old_argv = sys.argv
    sys.argv = ["price_search.py", "--query", "x", "--state", "_engine_scores"]
    try:
        with redirect_stderr(io.StringIO()):
            ps.main()
        check("a --state that would overwrite the scoreboard is refused", False)
    except SystemExit as e:
        check("a --state that would overwrite the scoreboard is refused", e.code != 0, repr(e))
    finally:
        sys.argv = old_argv

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

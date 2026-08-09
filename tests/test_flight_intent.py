#!/usr/bin/env python3
"""Flight intent and slot extraction: recognise a flight ask, and never invent an itinerary.

WHY THIS FILE EXISTS. Measured against this pipe on 2026-08-07, SEVEN OF EIGHT realistic flight
phrasings reached no background task at all. _is_bg_task_request needs an imperative verb AND
independent evidence of recurrence, and "in September" is not recurrence, so "find me a cheap flight
to Tokyo in March" fell through to the chat model — which has no fare data and answers with a number
it made up. _guess_kind already returned "fare" for all eight and nothing consumed it, because the
branch that would was never reached. The eighth phrasing DID match, and built a
price_search --kind fare job that refuses itself on its first run.

Both outcomes are wrong, and this path takes them instead. It follows the same DEFAULT-DENY
discipline test_media_intent.py enforces for renders and test_bgtask_intent.py for jobs: mentioning a
flight is not asking for one. The deny arms run FIRST, because "my flight was delayed" carries every
positive token a real request does.

The second half pins the slot parser. Nothing here may be supplied by a model: a guessed airport or
date produces a search for a trip the user never asked about, and unlike a wrong chat answer they may
act on it. The parser's job is to be right or to ASK.

Usage:  python3 tests/test_flight_intent.py [pipe_path]
"""
import asyncio
import datetime
import importlib.util
import sys
import time

PIPE = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_flight", PIPE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = mod.Pipe
p = P.__new__(P)
p._flight_draft = {}
p._route_metric = lambda *a, **k: None

TODAY = datetime.date(2026, 8, 7)          # injected, always. See the note at the bottom.
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- must route

YES = [
    "watch flights YYZ to YVR in September",
    "find me a cheap flight to Tokyo in March",
    "track Toronto to Vancouver flights under $600",
    "watch the flight price Toronto to Vancouver",
    "alert me when fares to karachi drop",
    "watch round trips to lisbon",
    "set up a fare alert for toronto to vancouver",
    "track the flight price to karachi for the next 10 days",
    "monitor flights from YYZ to LHR for december",
    "keep an eye on airfare to osaka next spring",
    "i want to fly from toronto to vancouver on sep 3 and back sep 10",
    "cheapest flight from montreal to lisbon in october",
    "notify me if flights to tokyo go under 900",
    "look for flights yyz-yvr labour day weekend",
    "text me when a YYZ to NRT round trip drops below 1200",
    "book a one way to calgary next friday",
    "price out flights to karachi for december 20 returning january 5",
    "search flights from pearson to vancouver first week of september",
    "how much is a flight to reykjavik in feb",
    "compare fares yyz to yvr for the long weekend",
    "i need a plane ticket to lahore in november",
    "find me the cheapest nonstop from montreal to london in march",
    "please track airfare from YUL to CDG for the next two months",
    "check flights to bogota, leaving dec 20 back jan 5",
    "email me when a round trip to mexico city dips under 500",
    "look up flights to st johns for the long weekend",
    "watch for cheap fares to japan next spring",
    "monitor a one-way from toronto to calgary on sep 12",
    "set up an alert on flights from yyz to yvr, depart sep 3 return sep 10, under 600",
    "i wanna fly to porto in may, find me something under 900",
]

# Each negative names the deny arm it exercises, so an edit that loses an arm fails loudly rather
# than passing by accident.
NO = [
    ("my flight was delayed by four hours", "PAST"),
    ("what airline flies to Osaka?", "META"),
    ("how do flight prices work?", "META"),
    ("I watched a documentary about flight", "PAST"),
    ("my flight lands at 6am, can you remind me to check in", "PAST"),
    ("i flew to vancouver last summer and it was great", "PAST"),
    ("why are flights so expensive right now", "META"),
    ("is it cheaper to book flights on a tuesday", "META"),
    ("the wright brothers made the first powered flight", "FIGURATIVE"),
    ("i took a flight of stairs to the third floor", "FIGURATIVE"),
    ("flight simulator 2024 is on sale", "FIGURATIVE"),
    ("our flight attendant was lovely", "FIGURATIVE"),
    ("the flight recorder was recovered", "FIGURATIVE"),
    ("cancel the flight watch", "META/manage"),
    ("stop watching flights to vancouver", "META/manage"),
    ("what's the flight path over my house", "FIGURATIVE"),
    ("she has a flight of fancy about moving abroad", "FIGURATIVE"),
    ("in-flight entertainment has gotten worse", "FIGURATIVE"),
    ("the bus fare went up again", "FIGURATIVE/transit"),
    ("how did you fare on the exam", "FIGURATIVE/verb"),
    ("should i book flights now or wait", "META"),
    ("which airlines fly to osaka", "META"),
    ("what is the best month to fly to japan", "META"),
    ("i just booked my flight to lisbon", "PAST"),
    ("my flight got cancelled, what are my rights", "PAST"),
    ("flight school in ottawa costs how much", "FIGURATIVE"),
    ("i missed my flight and had to sleep at the airport", "PAST"),
    ("the transit fare in montreal is going up", "FIGURATIVE/transit"),
    ("track the price of the rtx 5090 on newegg", "no flight noun"),
    ("watch this product page daily for restocks", "no flight noun"),
    ("list my background tasks", "no flight noun"),
    ("keep an eye on the kids tonight", "no flight noun"),
    ("the price of eggs is crazy right now", "no flight noun"),
    ("write a python script to scrape flight prices", "CODE_HINT gate"),
    # The risk 'one way' and 'nonstop' introduce as flight nouns: both are ordinary English. They
    # are safe only because a route AND a request frame are still required.
    ("one way or another we'll get there", "one-way idiom"),
    ("she talked nonstop about her trip to lisbon", "nonstop idiom, and it names a place"),
    ("it's a one way street to the airport", "one-way idiom with a place"),
    ("there's only one way to do this properly", "one-way idiom"),
]

# A flight ask does not have to name a flight. Every one of these is a request frame plus two
# airports and NO flight noun, which is the band that used to fall straight through to the
# scheduler. The live miss is first: it created job ba2a91e18def on 2026-08-08 as a fare watch with
# no dates in it. YES already carried the same sentence one word away ("track Toronto to Vancouver
# flights under $600") — the word was "flights".
#
# These are decided DETERMINISTICALLY, and that is a measurement, not a preference. The band was
# first routed to the gemma3:1b tier on the theory that a bare route is ambiguous; measured against
# it the same day, that scored 7 of 11 and answered OTHER for the live message itself, because the
# classifier's own prompt puts "any other kind of price watching" under OTHER. A reworded prompt
# reached 9 of 11 and began leaking a moving quote. So the ambiguity is NAMED instead — two
# resolvable airports, a request frame, and no other-mode word — and the checks below assert the
# classifier is never consulted for any of them.
ROUTE_NO_NOUN = [
    "track price from Toronto to Vancouver and text me if the price is under 1000, "
    "check every 15 mins next 2 hours",
    "watch the price toronto to vancouver, alert me under 900",
    "monitor prices from montreal to lisbon in october",
    "find me something from toronto to karachi in december",
    "check prices toronto to calgary next friday",
]

# Route-shaped and still never worth a classifier call. Each names the guard that stops it, and the
# guards are different on purpose: two of these have a perfectly good route and are rejected on the
# frame, which is the cheap check that keeps a ~0.3 ms airport scan off every message in every chat.
ROUTE_NEVER_ASKED = [
    ("the drive from toronto to vancouver takes four days", "no request frame"),
    ("how far is toronto from vancouver", "no request frame"),
    ("track price from toronto to toronto", "same airport both ends"),
    ("track the price of the rtx 5090 on newegg", "no route"),
    ("the price of eggs is crazy right now", "no route, no frame"),
    ("cancel the toronto to vancouver watch", "deny arm runs first"),
    # A route travelled some other way. These are the whole reason the band was thought to need a
    # model: each is a request frame plus two real airports, and none of them is about flying.
    ("how much does it cost to ship a package from toronto to vancouver", "other mode: ship"),
    ("find me a moving company from toronto to vancouver", "other mode: moving"),
    ("check the train prices from toronto to montreal", "other mode: train"),
    ("check driving costs from toronto to vancouver", "other mode: driving"),
    ("find a hotel from toronto to vancouver", "other mode: hotel"),
    ("compare cruise prices from vancouver to tokyo", "other mode: cruise"),
]


def main():
    print("--- a flight ask reaches the flight path (was 0 of 8; see the docstring) ---")
    for t in YES:
        tier, rule = p._is_flight_request(t)
        check(f"routes: {t[:60]!r}", tier is not None, f"got {(tier, rule)}")

    print("\n--- mentioning a flight is not asking for one (DEFAULT DENY) ---")
    for t, arm in NO:
        tier, rule = p._is_flight_request(t)
        check(f"[{arm}] {t[:52]!r}", (tier, rule) == (None, None), f"LEAKED as {(tier, rule)}")

    print("\n--- the deny arms are individually load-bearing ---")
    check("a figurative arm exists and fires", p._flight_deny("a flight of stairs"))
    check("a past-tense arm exists and fires", p._flight_deny("my flight was delayed"))
    check("a meta arm exists and fires", p._flight_deny("how do flight prices work"))
    check("deny beats a positive frame",
          p._is_flight_request("find me my flight that was delayed") == (None, None))

    print("\n--- the classifier tier is fail-closed and not consulted by the strong tier ---")
    p._classify_flight = lambda t: (_ for _ in ()).throw(AssertionError("must not be consulted"))
    check("a STRONG match never calls the classifier",
          p._is_flight_request("find me a cheap flight to Tokyo in March")[0] == 1)
    p._classify_flight = lambda t: False
    check("the HINT band declining means no route", p._is_flight_request("flights to tokyo")[0] is None)
    p._classify_flight = lambda t: True
    check("the HINT band accepting routes at tier 3",
          p._is_flight_request("flights to tokyo") == (3, "flight_classifier"))
    check("developer vocabulary is suppressed before the classifier",
          p._is_flight_request("build me a flight search api endpoint") == (None, None))
    p._classify_flight = lambda t: False

    print("\n--- a route with no flight noun routes WITHOUT a model (job ba2a91e18def) ---")
    # Fail-loud stub: this band must never reach the 1B, because the 1B was measured wrong about it.
    p._classify_flight = lambda t: (_ for _ in ()).throw(AssertionError("must not be consulted"))
    for t in ROUTE_NO_NOUN:
        try:
            got = p._is_flight_request(t)
            check(f"tier 2, deterministic: {t[:52]!r}", got == (2, "flight_route"), f"got {got}")
        except AssertionError:
            check(f"tier 2, deterministic: {t[:52]!r}", False, "consulted the classifier")
    for t, guard in ROUTE_NEVER_ASKED:
        try:
            got = p._is_flight_request(t)
            check(f"[{guard}] {t[:44]!r}", got == (None, None), f"LEAKED as {got}")
        except AssertionError:
            check(f"[{guard}] {t[:44]!r}", False, "reached the classifier; the guard is gone")
    check("_fl_route needs BOTH ends", not p._fl_route("track the price to vancouver"))
    check("_fl_route rejects a same-airport pair", not p._fl_route("from toronto to toronto"))
    check("_fl_route reads a real route", p._fl_route("from montreal to lisbon"))
    # The other-mode arm must subtract from a route, not from everything: a message that names a
    # flight AND a train is still a flight ask, and is claimed by the noun band above it.
    check("an other-mode word does not veto the noun band",
          p._is_flight_request("find flights to tokyo in march, or should i take the train")[0] == 1)
    p._classify_flight = lambda t: False

    print("\n--- the other routers are untouched ---")
    bg = importlib.util.spec_from_file_location("aa_bg2", PIPE)
    bgm = importlib.util.module_from_spec(bg)
    bg.loader.exec_module(bgm)
    q = bgm.Pipe.__new__(bgm.Pipe)
    check("_guess_kind still says 'fare' for every routed phrasing",
          all(bgm.Pipe._guess_kind(t) == "fare" for t in YES if "flight" in t or "fare" in t))
    check("a product price watch still reaches the bg-task path, not flight",
          q._is_bg_task_request("monitor the price of the RTX 5090 on newegg for 2 weeks")
          and p._is_flight_request("monitor the price of the RTX 5090 on newegg for 2 weeks")
          == (None, None))
    check("a render request is not a flight request",
          p._is_flight_request("make a picture of a plane taking off from YYZ") == (None, None))

    print("\n--- places: positional, because fragment matching got this wrong twice ---")
    for text, want_o, want_d in [
        ("fly from toronto to vancouver on sep 3", "YTO", "YVR"),
        ("toronto to karachi dec 20", "YTO", "KHI"),          # bare A to B, no "from"
        ("montreal to porto in may", "YUL", "OPO"),
        ("flights from YYZ to YVR in March", "YYZ", "YVR"),
        ("yyz-yvr labour day weekend", "YYZ", "YVR"),
        ("one way to calgary next friday", None, "YYC"),
    ]:
        o, d, _u = p._fl_places(text)
        check(f"{text[:44]!r} -> {want_o}/{want_d}",
              (o[0] if o else None) == want_o and (d[0] if d else None) == want_d,
              f"got {o}/{d}")
    o, d, u = p._fl_places("flights from ottawa to narnia in may")
    check("an unresolvable place is reported, not silently dropped", u == ["narnia"], u)
    check("...and the resolvable half is still kept", o[0] == "YOW" and d is None)

    print("\n--- dates: exact, range and month, with the year inferred forward ---")
    for text, kind, val in [
        ("depart 2026-09-15", "exact", "2026-09-15"),
        ("on sep 15", "exact", "2026-09-15"),
        ("on 15 sep", "exact", "2026-09-15"),
        ("september 15th", "exact", "2026-09-15"),
        ("next friday", "exact", "2026-08-14"),
        ("in 3 weeks", "exact", "2026-08-28"),
        ("in march", "month", "2027-03"),               # March 2026 is past -> next occurrence
        ("in september", "month", "2026-09"),           # still ahead this year
        ("first week of september", "range", "2026-09"),
        ("late march", "range", "2027-03"),
    ]:
        got = p._fl_find_dates(text, TODAY)
        ok = got and got[0]["kind"] == kind and (
            got[0].get("date") == val or got[0].get("month") == val)
        check(f"{text!r} -> {kind} {val}", ok, f"got {got}")

    print("\n--- the two date bugs a rollover and an ordering would hide ---")
    got = p._fl_find_dates("dec 20 returning jan 5", TODAY)
    check("Dec 20 -> Jan 5 rolls the return into the NEXT year",
          [d["date"] for d in got] == ["2026-12-20", "2027-01-05"], got)
    got = p._fl_find_dates("first week of september", TODAY)
    check("'first week of september' is ONE range, not a range plus a bare month",
          len(got) == 1 and got[0]["kind"] == "range", got)

    print("\n--- what must be ASKED rather than guessed ---")
    s = p._flight_slots("flights to lisbon sometime in the fall", today=TODAY)
    check("a season is not a date", "dates" in p._flight_missing(s))
    check("...and it is flagged so the reply can explain why", s.get("season_only") is True)
    s = p._flight_slots("flights to lisbon for the long weekend", today=TODAY)
    check("a named holiday is not a date either", "dates" in p._flight_missing(s))
    s = p._flight_slots("watch flights to vancouver", today=TODAY)
    check("no origin and no dates -> both asked", p._flight_missing(s) == ["origin", "dates"])

    print("\n--- one-way, trip length, target ---")
    s = p._flight_slots("one way to calgary next friday", today=TODAY)
    check("one-way sets the flag and carries no return", s.get("one_way") and "ret" not in s)
    s = p._flight_slots("montreal to porto in may for a week", today=TODAY)
    check("'for a week' is 7 days", s.get("trip_days") == 7)
    check("a bare month defaults the return to the same window", s.get("ret_defaulted") is True)
    check("...which is STATED, not silent", s["ret"]["month"] == s["depart"]["month"])
    s = p._flight_slots("flights to tokyo in march under $900", today=TODAY)
    check("a target is parsed", s.get("target") == 900.0)
    s = p._flight_slots("flights to tokyo in march", today=TODAY)
    check("...and is optional — its absence never blocks", "target" not in p._flight_missing(s))

    print("\n--- slots merge across turns, which is what makes the form work ---")
    a = p._flight_slots("watch flights to vancouver", today=TODAY)
    b = p._flight_slots("from toronto in march", prev=a, today=TODAY)
    check("turn 2 fills what turn 1 left open", p._flight_missing(b) == [], b)
    check("...without losing turn 1's destination", b["dest"][0] == "YVR")

    # REPORTED LIVE, 2026-08-09. The form asked "when?", said "a month like March is fine", and then
    # could not read "leaving October and returning nov" — _FL_BAREMON required a PREPOSITION, so a
    # departure/return cue parsed to nothing. _flight_turn returned None, the turn fell through to
    # the background-task path, and the agent replied about flying to Astana and searching
    # Amazon.ca. Both halves are pinned here: the cue must parse, and a reply the parser cannot read
    # must never reach a model that will invent an itinerary from it.
    print("\n--- a month named by a departure/return CUE, not a preposition ---")
    for text, want in [
        ("leaving October and returning nov", ["2026-10", "2026-11"]),
        ("depart october return november", ["2026-10", "2026-11"]),
        ("out in october back in november", ["2026-10", "2026-11"]),
        ("going october, home november", ["2026-10", "2026-11"]),
        ("leaving in October and returning in nov", ["2026-10", "2026-11"]),
    ]:
        got = p._fl_find_dates(text, TODAY)
        check(f"{text[:44]!r} -> {want}",
              [d.get("month") for d in got] == want and all(d["kind"] == "month" for d in got), got)
    check("a cue does not swallow an exact date",
          [d.get("date") for d in p._fl_find_dates("leaving oct 15 returning nov 3", TODAY)]
          == ["2026-10-15", "2026-11-03"])
    check("a cue does not beat a part-month",
          p._fl_find_dates("leaving early october", TODAY)[0]["kind"] == "range")

    print("\n--- a BARE month answers the form, and only the form ---")
    for text in ("October", "october and november", "may", "march"):
        check(f"{text!r} stays unparsed in free text", p._fl_find_dates(text, TODAY) == [],
              p._fl_find_dates(text, TODAY))
    check("...but 'October' is an answer once the form is open",
          [d.get("month") for d in p._fl_find_dates("October", TODAY, bare=True)] == ["2026-10"])
    check("...and two bare months are depart then return, in document order",
          [d.get("month") for d in p._fl_find_dates("october and november", TODAY, bare=True)]
          == ["2026-10", "2026-11"])
    check("the modal 'may' is why bare is off by default",
          p._fl_find_dates("may i ask what this costs", TODAY) == [])

    print("\n--- the reported two-turn exchange, end to end (hermetic engine) ---")
    # Every turn that completes the form now calls FlightClaw. The stub returns canned text in
    # the SAME shapes the live service emits (pinned against a live capture, 2026-08-09) — these
    # tests must never touch the network, and a fabricated engine reply here is fine because what
    # is under test is the pipe's handling, not the engine.
    DATES_TEXT = ("YYZ -> YVR cheapest dates (ECONOMY, CAD):\n"
                  "  2026-10-02 -> 2026-11-02: C$323\n"
                  "  2026-10-15 -> 2026-11-15: C$338\n"
                  "\n2 date(s) found. Cheapest: C$323")
    DEC_TEXT = ("YYZ -> YVR cheapest dates (ECONOMY, CAD):\n"
                "  2026-12-03 -> 2027-01-03: C$401\n\n1 date(s) found. Cheapest: C$401")
    # The RULERLESS shape — the MCP tool's real output (captured live 2026-08-09). The CLI
    # prints ===== rulers; the renderer must take both, and the ruler-shape is pinned separately.
    OPTIONS_TEXT = ("\nYYZ -> YVR on 2026-10-02 (CAD):\n\n"
                    "Option 1: C$686 total\n  Outbound: C$338 | 5h 10m | 0 stop(s)\n"
                    "  F8 607: YYZ 19:55 -> YVR 22:05\n  Book: https://g.example/book1\n\n"
                    "Option 2: C$690 total\n  Outbound: C$340 | 5h 10m | 0 stop(s)\n"
                    "  Book: https://g.example/book2\n")
    TRACK_TEXT = ("Tracking YYZ-YVR-2026-10-02-RT-2026-11-02: C$338 (F8)\n"
                  "Target price: C$1,000\n\n1 new route(s) tracked.")

    def rig(q, dates=DATES_TEXT, options=OPTIONS_TEXT, track=TRACK_TEXT, fail=None,
            calls=None, jobs=None):
        """A pipe whose engine and scheduler are fixtures. Records every call."""
        calls = calls if calls is not None else []
        jobs = jobs if jobs is not None else []

        async def fc(tool, arguments, timeout=0):
            calls.append((tool, dict(arguments)))
            if fail:
                raise RuntimeError(fail)
            return {"search_dates": dates, "search_flights": options,
                    "track_flight": track}.get(tool, "")

        def api(method, path, body=None, timeout=10):
            if method == "POST" and path == "/api/jobs":
                jobs.append(body)
                return 200, {"job": {"id": "fcjob1234567", "next_run_at": None}}, None
            return 200, {"jobs": []}, None

        q._fc_call = fc
        q._hermes_api = api
        q._stamp_owner = lambda *a, **k: 1
        q._contact = lambda h: {}
        q._save_phone = lambda h, e: True
        q._alert_setup_block = lambda h: "\n\n[alert-setup]"
        q._route_metric = lambda *a, **k: None
        return q, calls, jobs

    def drain(gen):
        async def go():
            return "".join([c async for c in gen])
        return asyncio.run(go())

    def form_turn(reply, prev, answered=False, **rigkw):
        """Turn 2: a live draft, then the user's answer. Hermetic."""
        q = P.__new__(P)
        q._flight_draft = {"c": {"t": time.time(), "turns": 1, "slots": dict(prev),
                                 **({"answered": True} if answered else {})}}
        q, calls, jobs = rig(q, **rigkw)
        out = q._flight_turn("c", reply, [], resume=True)
        return (None if out is None else drain(out)), calls, jobs, q

    OPEN = {"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"], "target": 1000.0}
    out, calls, jobs, _ = form_turn("leaving October and returning nov", OPEN)
    check("the reply is ANSWERED, not passed on", out is not None, "returned None")
    check("...the month window resolved to CONCRETE dates via search_dates",
          calls and calls[0][0] == "search_dates"
          and calls[0][1]["from_date"] == "2026-10-01" and calls[0][1]["to_date"] == "2026-10-31",
          calls[:1])
    check("...with the trip length taken from the window gap",
          calls[0][1].get("trip_duration") == 31, calls[:1])
    check("...and the chosen dates are NAMED as chosen",
          "2026-10-02" in out and "chosen dates, not typed ones" in out, out[:600])
    check("...real fares are shown for the pick",
          calls[1][0] == "search_flights" and "Option 1: C$686 total" in out, out[:900])
    check("...as links, not raw 500-char URLs",
          "[Book option 1](https://g.example/book1)" in out, out[-600:])
    check("no watch verbs -> nothing scheduled, and the offer says how",
          not jobs and "track it" in out, out[-300:])
    out, calls, jobs, _ = form_turn("October", OPEN)
    check("a one-word month answers too", out is not None and "2026-10-02" in out,
          (out or "")[:300])

    print("\n--- a date attempt the parser cannot read re-asks; it never escapes ---")
    for reply in ("sometime around the 3rd quarter", "5ish weeks out",
                  "the week after next month maybe"):
        out, _c, _j, _ = form_turn(reply, OPEN)
        check(f"unreadable dates re-ask: {reply[:38]!r}",
              out is not None and "couldn't read" in out, (out or "RETURNED NONE")[:160])
    check("...and the re-ask quotes what it could not read",
          "3rd quarter" in (form_turn("sometime around the 3rd quarter", OPEN)[0] or ""))
    check("...and lists forms that do work",
          "Oct 15" in (form_turn("5ish weeks out", OPEN)[0] or ""))
    # The wildcard that made this necessary: 'next month' used to resolve to a next-Monday date and
    # complete the form on a departure the user never named.
    check("'next month' names no day of the week",
          p._fl_find_dates("next month", TODAY) == [], p._fl_find_dates("next month", TODAY))
    check("...nor does 'this month'", p._fl_find_dates("this month", TODAY) == [])
    for dow, want in (("next monday", "2026-08-10"), ("next mon", "2026-08-10"),
                      ("next thursday", "2026-08-13"), ("next tues", "2026-08-11"),
                      ("next wednesday", "2026-08-12"), ("next sun", "2026-08-09")):
        got = p._fl_find_dates(dow, TODAY)
        check(f"...and a real day still resolves: {dow!r} -> {want}",
              got and got[0].get("date") == want, got)
    # The other direction: moving on must stay cheap. No month, no digit, no duration word.
    for reply in ("what's the weather like", "who won the game", "thanks"):
        check(f"a topic change still routes normally: {reply!r}",
              form_turn(reply, OPEN)[0] is None)
    check("abandoning still wins over everything",
          "dropped" in (form_turn("never mind", OPEN)[0] or ""))

    print("\n--- ONE TURN: a tracking ask searches, tracks and schedules together ---")
    # The canonical message, the one that started all of this. With FlightClaw live it now does
    # everything at once: resolve the window, show real fares, create the tracking entry AND the
    # cron job on the USER'S cadence — and the phone typed inline is saved before the first alert
    # could need it.
    TURN1 = ("track price from Toronto to Vancouver and text me if the price is under 1000, "
             "leaving oct and returning nov send me the link on email and notify me on text at "
             "5145579764, check every 15 mins next 2 hours.")
    S1 = p._flight_slots(TURN1, today=TODAY)
    check("tracking intent is a slot", S1.get("wants_watch") is True, S1)
    check("the inline phone number is a slot, normalised",
          S1.get("phone") == "+15145579764", S1.get("phone"))
    check("...and 'under 1000' did not read as a phone number",
          p._flight_slots("watch flights to tokyo under 1000", today=TODAY).get("phone") is None)

    q = P.__new__(P); q._flight_draft = {}
    q, calls, jobs = rig(q)
    saved_phones = []
    q._save_phone = lambda h, e: (saved_phones.append((h, e)), True)[1]
    out = drain(q._flight_turn("c", TURN1, [], resume=False, handle="tester"))
    check("one turn: fares are shown", "Option 1: C$686 total" in out, out[:800])
    check("one turn: the watch is created", "Watching it" in out and "fcjob1234567" in out,
          out[-900:])
    check("...tracking was created with the user's ceiling",
          any(t == "track_flight" and a.get("target_price") == 1000.0 for t, a in calls), calls)
    check("...on the RESOLVED dates, not the window",
          any(t == "track_flight" and a.get("date") == "2026-10-02"
              and a.get("return_date") == "2026-11-02" for t, a in calls), calls)
    check("...the job runs on the cadence the user typed",
          jobs and jobs[0]["schedule"] == "every 15m" and jobs[0]["repeat"] == 8, jobs)
    check("...its command is the vetted flightclaw_watch with the route id",
          "flightclaw_watch.py" in jobs[0]["prompt"]
          and "YYZ-YVR-2026-10-02-RT-2026-11-02" in jobs[0]["prompt"], jobs[0]["prompt"])
    check("...which parses clean and passes the pipe's own job-shape checks",
          P._job_stray_args(jobs[0]["prompt"])[0] == []
          and [d[0] for d in P._job_defects({"prompt": jobs[0]["prompt"], "deliver": "local"})]
          == [], jobs[0]["prompt"])
    check("...the inline phone was saved for the alerts",
          saved_phones == [("tester", "+15145579764")], saved_phones)
    check("...and the confirmation shows the alert channels", "[alert-setup]" in out)
    check("...and the baseline price FlightClaw just recorded",
          "C$338" in out, out[-900:])

    print("\n--- 'track it' after a search-only answer creates the same watch ---")
    S_EXACT = p._flight_slots("find flights from toronto to vancouver oct 15 returning nov 12 "
                              "under 1000", today=TODAY)
    check("lookup verbs alone do not set wants_watch", not S_EXACT.get("wants_watch"), S_EXACT)
    for reply in ("track it", "yes", "set the alert", "yes please", "schedule it anyway"):
        out, calls, jobs, _ = form_turn(reply, S_EXACT, answered=True)
        check(f"watch created on: {reply!r}",
              out is not None and "Watching it" in out and len(jobs) == 1,
              (out or "FELL THROUGH")[:200])
    out, calls, jobs, _ = form_turn("what's the weather", S_EXACT, answered=True)
    check("moving on after an answer still routes normally", out is None)
    out, calls, jobs, _ = form_turn("never mind", S_EXACT, answered=True)
    check("abandoning after an answer still wins", "dropped" in (out or ""))

    print("\n--- the engine failing is said plainly; nothing is scheduled on a guess ---")
    q = P.__new__(P); q._flight_draft = {}
    q, calls, jobs = rig(q, fail="connection refused")
    out = drain(q._flight_turn("c", TURN1, [], resume=False, handle="tester"))
    check("engine down: honest, with the manual link",
          "couldn't reach live fares" in out and "google.com/travel/flights" in out, out)
    check("...and NO job was created", jobs == [], jobs)
    check("...and no number was invented", "C$" not in out.split("](")[0], out)
    check("...the draft survives un-answered so a retry re-searches",
          q._flight_draft["c"].get("answered") is not True)
    q = P.__new__(P); q._flight_draft = {}
    q, calls, jobs = rig(q, dates="No prices found for YYZ -> YVR between X and Y")
    out = drain(q._flight_turn("c", TURN1, [], resume=False, handle="tester"))
    check("an empty grid is honest too, and schedules nothing",
          "Nothing was scheduled" in out and jobs == [], out[:400])

    print("\n--- the deterministic pieces the one-turn flow is built from ---")
    check("cadence with its own bound: the user's words win",
          P._fl_schedule("every 15 mins next 2 hours", horizon_days=60) == ("every 15m", 8, False))
    check("an UNBOUNDED cadence runs until departure",
          P._fl_schedule("every 6 hours", horizon_days=30) == ("every 6h", 120, False))
    check("no cadence at all: daily until departure, and SAYS it defaulted",
          P._fl_schedule(None, horizon_days=30) == ("every 1d", 30, True))
    check("...capped so 15-minute checks on a far trip cannot be five thousand runs",
          P._fl_schedule("every 15 mins", horizon_days=120)[1] == 360)
    check("the horizon for an exact date is days until it",
          P._fl_horizon_days({"depart": {"kind": "exact", "date": "2026-09-08"}},
                             today=TODAY) == 32)
    check("...for a window, days until the window ENDS",
          P._fl_horizon_days({"depart": {"kind": "month", "from": "2026-10-01",
                                         "to": "2026-10-31"}}, today=TODAY) == 85)
    check("...clamped for a passed date", P._fl_horizon_days(
        {"depart": {"kind": "exact", "date": "2020-01-01"}}, today=TODAY) == 1)
    rid = P._fc_route_id({"origin": ["YYZ", "x"], "dest": ["YVR", "y"],
                          "depart": {"kind": "exact", "date": "2026-10-02"},
                          "ret": {"kind": "exact", "date": "2026-11-02"}})
    # "toronto" resolves to the METRO code YTO on purpose (any Toronto airport) — and fli's
    # Airport enum rejects metro codes. Measured 2026-08-09: 15 of 129 codes in _IATA are metro.
    # The translation lives at the engine boundary so the table's semantics survive.
    check("metro codes are translated at the engine boundary",
          P._fc_code("YTO") == "YYZ" and P._fc_code("NYC") == "JFK"
          and P._fc_code("LON") == "LHR", "")
    check("...and real airport codes pass through untouched",
          P._fc_code("YYZ") == "YYZ" and P._fc_code("KHI") == "KHI")
    check("...so a 'toronto to vancouver' route id is an AIRPORT pair",
          P._fc_route_id({"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"],
                          "depart": {"kind": "exact", "date": "2026-10-02"},
                          "ret": {"kind": "exact", "date": "2026-11-02"}})
          == "YYZ-YVR-2026-10-02-RT-2026-11-02")
    tor_plan = p._fc_search_plan({"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"],
                                  "depart": {"kind": "exact", "date": "2026-10-15"}})
    check("...and the search goes out with the airport code too",
          tor_plan[1]["origin"] == "YYZ", tor_plan)
    check("the route id matches FlightClaw's own formula",
          rid == "YYZ-YVR-2026-10-02-RT-2026-11-02", rid)
    check("...one-way has no RT tail",
          P._fc_route_id({"origin": ["YYZ", "x"], "dest": ["YYC", "y"], "one_way": True,
                          "depart": {"kind": "exact", "date": "2026-09-12"}})
          == "YYZ-YYC-2026-09-12")
    plan = P._fc_search_plan(P.__new__(P), {"origin": ["YYZ", "x"], "dest": ["YVR", "y"],
                                            "depart": {"kind": "exact", "date": "2026-10-15"},
                                            "ret": {"kind": "exact", "date": "2026-11-12"}}) \
        if False else p._fc_search_plan({"origin": ["YYZ", "x"], "dest": ["YVR", "y"],
                                         "depart": {"kind": "exact", "date": "2026-10-15"},
                                         "ret": {"kind": "exact", "date": "2026-11-12"}})
    ruled = ("\nYYZ -> YVR (CAD)\n\n" + "=" * 60 + "\nOption 1: C$646 total\n"
             "  Book: https://g/b1\n" + "=" * 60 + "\nOption 2: C$650\n  Book: https://g/b2\n")
    r_out = p._fc_render_options(ruled)
    check("the CLI's ruler shape renders identically",
          "Option 1: C$646 total" in r_out and "[Book option 1](https://g/b1)" in r_out, r_out)
    check("exact dates -> search_flights with both dates",
          plan[0] == "search_flights" and plan[1]["return_date"] == "2026-11-12", plan)
    cmd = p._fc_watch_cmd("YYZ-YVR-2026-10-02-RT-2026-11-02", "fc-test", "tester",
                          "YYZ→YVR fare watch under $1,000", "every 15m", 1000.0)
    check("the watch command quotes every spaced value",
          P._job_stray_args(cmd)[0] == [], P._job_stray_args(cmd))
    trip = p._job_trip({"prompt": cmd})
    check("the job listing renders the itinerary from the route id",
          "YYZ → YVR" in trip and "2 Oct 2026" in trip and "2 Nov 2026" in trip, trip)

    print("\n--- ...while a correction still re-answers and moving on routes normally ---")
    for reply in ("actually make it december", "no, make it december",
                  "how about december instead"):
        out, calls, _j, _ = form_turn(reply, S_EXACT, answered=True, dates=DEC_TEXT)
        check(f"a correction re-answers with a fresh search: {reply!r}",
              out is not None and calls and "2026-12-03" in out, (out or "None")[:300])

    print("\n--- the Google Flights link is built from the slots, never typed by a model ---")
    u = p._gflights_url(b)
    check("it carries both codes", "YTO" in u and "YVR" in u)
    check("a month ask becomes a month query", "March%202027" in u or "March+2027" in u, u)
    s = p._flight_slots("from toronto to vancouver sep 3 back sep 10", today=TODAY)
    check("an exact ask carries both dates", "2026-09-03" in p._gflights_url(s)
          and "2026-09-10" in p._gflights_url(s))
    s = p._flight_slots("one way from toronto to calgary on sep 12", today=TODAY)
    check("a one-way says so and carries no return date",
          "One%20way" in p._gflights_url(s) and "through" not in p._gflights_url(s))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


# The clock is injected on every date assertion rather than read from the system, for the reason
# TRACKING_ENHANCEMENT.md:104 gives: "An injected clock finds what production hides." The stock flap
# cooldown defaulted its timestamp to 0 and swallowed the very first firing — invisible on a real
# epoch, which is not a property to depend on. Here the equivalent trap is the year-inference rule:
# "in march" means 2027 in August 2026 and 2026 in January 2026, and a test that used today's real
# date would pass in one half of the year and fail in the other.
sys.exit(main())

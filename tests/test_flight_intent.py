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

    print("\n--- the reported two-turn exchange, end to end ---")

    def drain(gen):
        async def go():
            return "".join([c async for c in gen])
        return asyncio.run(go())

    def form_turn(reply, prev):
        """Turn 2: a live draft, then the user's answer."""
        q = P.__new__(P)
        q._flight_draft = {"c": {"t": time.time(), "turns": 1, "slots": dict(prev)}}
        q._route_metric = lambda *a, **k: None
        out = q._flight_turn("c", reply, [], resume=True)
        return None if out is None else drain(out)

    OPEN = {"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"], "target": 1000.0}
    out = form_turn("leaving October and returning nov", OPEN)
    check("the reply is ANSWERED, not passed on", out is not None, "returned None")
    check("...with October as the departure", out and "October 2026" in out, (out or "")[:220])
    check("...and November as the return", out and "November 2026" in out, (out or "")[:220])
    check("...and a link built from those months",
          out and "October%202026" in out, (out or "")[:400])
    out = form_turn("October", OPEN)
    check("a one-word month answers too", out is not None and "October 2026" in out,
          (out or "")[:160])

    print("\n--- a date attempt the parser cannot read re-asks; it never escapes ---")
    for reply in ("sometime around the 3rd quarter", "5ish weeks out",
                  "the week after next month maybe"):
        out = form_turn(reply, OPEN)
        check(f"unreadable dates re-ask: {reply[:38]!r}",
              out is not None and "couldn't read" in out, (out or "RETURNED NONE")[:160])
    check("...and the re-ask quotes what it could not read",
          "3rd quarter" in (form_turn("sometime around the 3rd quarter", OPEN) or ""))
    check("...and lists forms that do work",
          "Oct 15" in (form_turn("5ish weeks out", OPEN) or ""))
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
        check(f"a topic change still routes normally: {reply!r}", form_turn(reply, OPEN) is None,
              (form_turn(reply, OPEN) or "")[:120])
    check("abandoning still wins over everything",
          "dropped" in (form_turn("never mind", OPEN) or ""))

    # REPORTED LIVE, 2026-08-09. The answer ends by saying Google Flights can set a price alert in
    # one click. The user replied "yes so set alert" — the draft had already been dropped, so that
    # matched nothing, became a background followup, and the agent built job e6df1739a275: every
    # 1440m for 7 days (against "every 15 mins next 2 hours"), no --depart/--return, and a command
    # argparse rejects outright. Three failures, all downstream of this one gap.
    print("\n--- 'yes, set it up' after an answer is claimed, never handed to the scheduler ---")

    def answered(reply, slots):
        q = P.__new__(P)
        q._flight_draft = {"c": {"t": time.time(), "turns": 0, "slots": dict(slots),
                                 "answered": True}}
        q._route_metric = lambda *a, **k: None
        out = q._flight_turn("c", reply, [], resume=True)
        return (None if out is None else drain(out)), q

    TURN1 = ("track price from Toronto to Vancouver and text me if the price is under 1000, "
             "leaving oct and returning nov, check every 15 mins next 2 hours.")
    S = p._flight_slots(TURN1, today=TODAY)
    check("the cadence the user asked for is captured, not dropped",
          S.get("cadence") == "every 15 mins next 2 hours", S.get("cadence"))
    check("...and the dates come out of the same sentence",
          S["depart"]["month"] == "2026-10" and S["ret"]["month"] == "2026-11", S.get("depart"))

    # The answer must LEAVE the draft behind, or the affirmative has nothing to match.
    q = P.__new__(P); q._flight_draft = {}; q._route_metric = lambda *a, **k: None
    drain(q._flight_turn("c", TURN1, [], resume=False))
    check("a completed answer keeps its draft, marked answered",
          q._flight_draft.get("c", {}).get("answered") is True, q._flight_draft)

    for reply in ("yes so set alert", "yes", "ok do it", "set the alert", "please set it up",
                  "track it", "sure", "yes please"):
        out, _ = answered(reply, S)
        check(f"claimed, not scheduled: {reply!r}",
              out is not None and "Still nothing scheduled" in out, (out or "FELL THROUGH")[:120])
    out, after = answered("yes so set alert", S)
    check("...it hands over the real alert, on the confirmed itinerary",
          "Track prices" in out and "google.com/travel/flights" in out, out[:200])
    check("...says the cadence back rather than ignoring it",
          "every 15 mins next 2 hours" in out, out[-400:])
    check("...names why it will not schedule one itself",
          "won't for a fare" in out and "can't read one on every single run" in out, out[-600:])
    check("...promises two steps and lists exactly two",
          out.count("\n1. ") == 1 and out.count("\n2. ") == 1 and "\n3. " not in out, out)
    check("...and the list is closed, so the next paragraph is not swallowed into item 2",
          "\n\nThat alert is Google's" in out, out[out.find("2. "):][:260])
    check("...and drops the draft so it cannot answer twice", "c" not in after._flight_draft)

    # REPORTED 2026-08-09: "I've noted your $1,000 target" read as though something had been stored
    # and would be acted on, in a reply whose whole point is that nothing was. And the reply never
    # actually ASKED whether the user wanted the alert -- it trailed off into "want different
    # dates?", so "yes so set alert" was the user answering a question that had not been put.
    print("\n--- the answer states what is NOT running, and asks a real question ---")
    q = P.__new__(P); q._flight_draft = {}; q._route_metric = lambda *a, **k: None
    ans = drain(q._flight_turn("c", TURN1, [], resume=False))
    check("it says plainly that nothing is scheduled",
          "Nothing is scheduled" in ans and "no alert, no watch, no job" in ans.lower(), ans[:400])
    check("'I've noted' is gone — it implied storage that never happened",
          "noted your" not in ans.lower(), ans[:400])
    check("...and the target is described as unsaved, not noted",
          "not saved anywhere" in ans and "nothing is comparing against it" in ans, ans[:600])
    check("the cadence is echoed as NOT running, rather than silently dropped",
          "every 15 mins next 2 hours" in ans and "no check is running" in ans, ans[:600])
    check("it asks a direct question about the alert",
          "Do you still want a price alert set up?" in ans, ans[-500:])
    check("...and says what answering yes will get them",
          "Say **yes**" in ans and "two steps" in ans, ans[-500:])
    check("the itinerary link is still handed over unprompted",
          "google.com/travel/flights" in ans)

    print("\n--- ...while a correction still re-answers and moving on still routes normally ---")
    for reply, want in [("actually make it december", "December"),
                        ("no, make it december", "December"),
                        ("how about december instead", "December")]:
        out, _ = answered(reply, S)
        check(f"a correction re-answers: {reply!r}", out and want in out, (out or "None")[:140])
    for reply in ("what's the weather", "may i ask something else", "thanks"):
        out, _ = answered(reply, S)
        check(f"moving on routes normally: {reply!r}", out is None, (out or "")[:120])
    out, _ = answered("never mind", S)
    check("abandoning still wins after an answer too", out and "dropped" in out)

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

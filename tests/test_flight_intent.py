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
import datetime
import importlib.util
import sys

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

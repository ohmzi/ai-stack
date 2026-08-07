#!/usr/bin/env python3
"""Watch a flight fare for ONE itinerary, reading only pages built for that itinerary.

WHY THIS CAN WORK WHEN price_search.py --kind fare CANNOT.

price_search refuses fares, and it is right to. It takes a URL from a search engine and hopes: on
2026-08-07 it resolved a cheapflights.ca ROUTE LANDING PAGE with no dates in the URL at all, read
358.72 out of a JSON-LD array of 97 unrelated itineraries, and texted "under your $1,000.00 target"
at high confidence. The state file is still on this box: item "C$ 146+ Cheap Flights from Toronto to
Vancouver", price 358.72, alerted_price 358.72. Nothing was misread. The number simply was not the
price of anything the user asked about, and no confidence score can notice that.

The difference here is structural, not a better regex: THE URL IS BUILT, NOT FOUND. Origin,
destination and dates go into the request, so a fare on the response is about that itinerary by
construction. Two consequences worth stating out loud:

  * This file issues ZERO search queries. The whole SearXNG rate-limit surface -- ~50-60 queries in
    10 minutes CAPTCHA'ing the roster, the 144-queries-a-day unresolved monitor, ROSTER_BACKOFF_S,
    probing-at-expiry re-triggering a block -- is removed from the fare path by construction. It is
    also why the live :8889 roster being broken does not affect this file.
  * Nothing is queried unless a HUMAN marked it shippable in flight_sites.json after measuring it
    with flight_probe.py. Zero shippable sites is not an error, it is a config limit, and it says so
    once and exits 0.

MONTH MODE GIVES THE STRUCTURAL GUARANTEE UP, SO IT REPLACES IT.

"Flights to Tokyo in March" is not vague -- it is what these sites sell. But a whole-month results
page legitimately lists dozens of itineraries, which is the same page shape that produced $358.72.
So binding moves from the URL to the ELEMENT: a month-mode fare is valid only if the fare and its own
departure and return dates come out of one itinerary element together, both inside the requested
window. A bare minimum with no dates attached is rejected.

And because the fare then belongs to dates the user never named, THOSE DATES ARE MANDATORY OUTPUT.
A text saying "March is $412" is one the reader cannot act on; they would have to re-search the whole
month by hand, and if they guessed a different week they would conclude the alert was wrong.

Usage:
  python3 scripts/flight_watch.py --origin YYZ --dest YVR --depart 2026-09-15 --return 2026-09-22 \
      --below 600 --state yyz-yvr-sep --alert-to ohmz --monitor 'YYZ-YVR fare' --schedule 'every 6h'
  python3 scripts/flight_watch.py --origin YYZ --dest YVR --depart-month 2027-03 --trip-days 7 \
      --below 900 --state yyz-yvr-mar --alert-to ohmz
  python3 scripts/flight_watch.py --print-urls --origin YYZ --dest YVR --depart-month 2027-03
  python3 scripts/flight_watch.py --selftest
"""
import argparse
import calendar as _cal
import importlib.util
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(modname):
    path = os.path.join(HERE, modname + ".py")
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pw = _load("price_watch")
# The measurement helpers live in flight_probe and are IMPORTED, not copied. The probe decides
# whether a site is usable using exactly the money pattern, teaser cues and date-binding window this
# file then reads fares with; two copies would drift, and a site measured usable under one set of
# rules and read under another is a wrong alert with a clean audit trail.
fp = _load("flight_probe")

REGISTRY = os.path.join(HERE, "flight_sites.json")

FARE_COOLDOWN_S = 6 * 3600   # closes TRACKING_ENHANCEMENT.md "Still open" item 3 for the fare path
AGREE_PCT = 12.0             # two independently-read fares within this agree. Exact equality would
                             # never fire: taxes and carrier mixes genuinely differ across OTAs.
MAX_SITES = 3
RUN_DEADLINE_S = 150         # the hermes terminal tool times out at 180 s
EMPTY_ALERT_AFTER = pw.EMPTY_ALERT_AFTER
UNREADABLE_GIVE_UP = 24      # runs after which the wording escalates once and the browser tier rests

# What choosing the dates this way is worth as evidence. A fare priced on dates the WATCHER picked is
# weaker than one the SITE picked as the month's cheapest, and rung 4 is weakest of all: it prices
# 1st-of-month to last-of-month, which is one arbitrary pair and emphatically not the cheapest in the
# month. If Mar 1->Mar 31 is $412 while Mar 8->Mar 15 is $280, texting "$412 for March" would make
# the reader book the worse fare believing it was the best. So it logs and never texts alone.
STRONG_BASES = ("exact", "native_month", "explicit_range")
CEILING = {"exact": "high", "native_month": "high", "explicit_range": "high",
           "calendar_cheapest": "medium", "assumed_month_bounds": "medium"}
# Rung 4 must never be able to say "cheapest". Not a style rule -- a different branch, so the claim
# cannot be made by accident.
CHEAPEST_CLAIMABLE = ("native_month", "explicit_range", "calendar_cheapest")


class NoShippableSites(Exception):
    pass


# ---------------------------------------------------------------- date specs and the ladder

def month_bounds(ym):
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, 1), date(y, m, _cal.monthrange(y, m)[1])


def clamp_to_future(d, today):
    """A month already partly elapsed narrows to its remaining days; a fully past one is an error."""
    return max(d, today)


def spec_window(spec, today):
    """(from_date, to_date) the fare's departure must fall inside."""
    if spec["kind"] == "exact":
        d = datetime.strptime(spec["date"], "%Y-%m-%d").date()
        return d, d
    if spec["kind"] == "month":
        lo, hi = month_bounds(spec["month"])
        return clamp_to_future(lo, today), hi
    lo = datetime.strptime(spec["from"], "%Y-%m-%d").date()
    hi = datetime.strptime(spec["to"], "%Y-%m-%d").date()
    return clamp_to_future(lo, today), hi


def resolve_rung(site, depart_spec, ret_spec, trip_days, today):
    """(mode, date_basis, depart, ret) for this site, or None when it cannot express the ask.

    The four-rung ladder. Which rung a site takes is a MEASURED capability (date_flex), never a
    guess: a month query sent to a site that cannot parse it returns a page about nothing, and a
    number read off that page is the 2026-08-07 bug again.
    """
    if depart_spec["kind"] == "exact":
        d = depart_spec["date"]
        r = ret_spec["date"] if ret_spec and ret_spec["kind"] == "exact" else None
        return ("exact", "exact", d, r)

    flex = site.get("date_flex")
    dlo, dhi = spec_window(depart_spec, today)
    rlo, rhi = spec_window(ret_spec, today) if ret_spec else (None, None)

    if flex == "whole_month" and site.get("month_url_template"):
        return ("month", "native_month", dlo.isoformat(),
                rlo.isoformat() if rlo else None)
    if flex == "range" and site.get("range_url_template"):
        # An unbounded range is meaningless: a whole month x whole month round trip is ~900 date
        # pairs and most sites will not quote it. Without a stay length this rung is SKIPPED rather
        # than sent, and the site falls through to the rungs below.
        if ret_spec and not trip_days:
            pass
        else:
            return ("range", "explicit_range", dlo.isoformat(),
                    (dlo + timedelta(days=trip_days)).isoformat() if (ret_spec and trip_days)
                    else None)
    if flex == "calendar" and site.get("calendar"):
        return ("calendar", "calendar_cheapest", dlo.isoformat(),
                (dlo + timedelta(days=trip_days)).isoformat() if (ret_spec and trip_days)
                else (rlo.isoformat() if rlo else None))
    if flex == "none":
        # Rung 4. One arbitrary pair, logged, never texting alone.
        return ("exact", "assumed_month_bounds", dlo.isoformat(),
                rhi.isoformat() if rhi else None)
    return None      # unknown / n/a: do not query


# ---------------------------------------------------------------- reading a fare off a page

_MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_abbr) if m}


def parse_found_date(s, win_from, win_to):
    """A date string lifted from a page -> a date inside the window, or None.

    Pages render dates without years far more often than with them, so the year is INFERRED from the
    window rather than guessed: if the window spans a year boundary both candidates are tried and the
    one inside the window wins. A date that lands outside the window is discarded rather than
    coerced -- it is a neighbouring-month suggestion the site volunteered, not the fare asked for.
    """
    s = (s or "").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return d if win_from <= d <= win_to else None
    m = re.search(r"([A-Za-z]{3,})\.?\s+(\d{1,2})\b", s) or \
        re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\b", s)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    mon, day = (a, b) if a[:1].isalpha() else (b, a)
    mi = _MONTHS.get(mon[:3].lower())
    if not mi:
        return None
    for y in {win_from.year, win_to.year}:
        try:
            d = date(y, mi, int(day))
        except ValueError:
            continue
        if win_from <= d <= win_to:
            return d
    return None


ATTRIB_WINDOW = 250    # a date further than this from a number is not that number's date


def bind_dates_nearest(html, cands, want=2):
    """[(value, [datestr, ...])] — each fare's OWN dates, in document order.

    Deliberately stricter than flight_probe.fares_bound_to_dates, and the two are not
    interchangeable. The probe asks a capability question -- "is any date co-located with any fare on
    this page?" -- and a generous +/-400 char window is right for it. This asks an ATTRIBUTION
    question: "which dates belong to THIS fare?" A generous window answers that one wrong.

    Found by its own test. On

        <div>Mon, Mar 8 - Sun, Mar 15</div><span>C$480.00</span>
        <div>Wed, Mar 10 - Wed, Mar 17</div><span>C$412.00</span>

    every date is within 400 characters of every price, so taking the first date in the window gave
    the C$412 fare the NEIGHBOURING card's dates and would have told the user to book Mar 8-15 at a
    price only available Mar 10-17. That is the same failure as reporting $358.72 off a 97-price
    array -- a real number attached to the wrong itinerary -- so nearness is the tie-breaker and
    distance is bounded.

    Nearness alone is not enough either, and that was the SECOND wrong version. Sorting the four
    dates above purely by distance handed the C$412 fare "Wed, Mar 17" first, because its own return
    date sits 12 characters away while its departure sits 26 away -- so the fare was reported as
    departing on its own return date. Distance picks the right CARD; only document order says which
    of that card's dates is the departure. A fare card states its dates immediately BEFORE its price,
    so this takes the closest-preceding run and then restores document order.
    """
    html = html or ""
    marks = [(m.start(), m.group(0)) for m in DATE_NEAR_RE.finditer(html)]
    out = []
    for v, off in cands:
        def _pick(seq):
            near = sorted(seq, key=lambda p: p[0])[:want]
            return [t for _d, _s, t in sorted(near, key=lambda p: p[1])]
        before = [(off - (s + len(t)), s, t) for s, t in marks
                  if s < off and 0 <= off - (s + len(t)) <= ATTRIB_WINDOW]
        if before:
            out.append((v, _pick(before)))
            continue
        # Some layouts put the price first. Same logic, other direction.
        after = [(s - off, s, t) for s, t in marks if s >= off and s - off <= ATTRIB_WINDOW]
        out.append((v, _pick(after)))
    return out


# One definition, borrowed from the probe so the two cannot disagree about what a date looks like.
DATE_NEAR_RE = fp.DATE_NEAR


def read_fare(html, site, mode, date_basis, dwin, rwin, currency):
    """(reading, why_not). A reading is {value, currency, depart_found, ret_found, date_basis}.

    The six validation gates. In exact mode gates 1-2 are satisfied by the URL; in month/range/
    calendar mode they are replaced by the tuple rule, which is the whole reason month mode is safe.
    """
    if not html:
        return None, "no page"
    wall, sigs, _narrow = fp.wall_check(html)
    if wall:
        return None, f"robot wall ({', '.join(sigs[:2])})"

    cands = fp.fare_candidates(html)
    if not cands:
        return None, "no fare-shaped number on the page"

    # Gate 5: teaser rejection.
    tez = fp.teaser_flagged(html, cands)
    if tez >= len(cands):
        return None, "every number follows a 'from'/'as low as' cue"

    if date_basis in ("exact", "assumed_month_bounds"):
        # The itinerary is in the URL, so gate 1 holds structurally. Gate 4 (the listing guard) still
        # applies: a page offering many prices has no price of its own.
        vals = sorted({v for v, _o in cands})
        if len(vals) >= pw.LISTING_MIN_PRICES and not site.get("list_is_itinerary"):
            return None, (f"the page offers {len(vals)} different fares, so none of them is the "
                          f"fare for one itinerary")
        return ({"value": vals[0], "currency": currency,
                 "depart_found": dwin[0].isoformat(),
                 "ret_found": rwin[0].isoformat() if rwin and rwin[0] else None,
                 "date_basis": date_basis}, None)

    # Month / range / calendar: THE TUPLE RULE. A fare is valid only with its own dates attached,
    # and "its own" means nearest, bounded — see bind_dates_nearest.
    pairs = bind_dates_nearest(html, cands)
    bound = sum(1 for _v, ds in pairs if ds)
    if bound == 0:
        return None, (f"{len({v for v, _ in cands})} fares and none carries its own dates — a "
                      f"minimum here would be an arbitrary element of a list")
    best = None
    for value, datestrs in pairs:
        if not datestrs:
            continue
        # Index-aware, and it has to be: the departure and the return are two DIFFERENT entries in
        # this list. Re-scanning from the start for the return re-picked the departure itself
        # (Mar 10 satisfies ">= Mar 10"), reporting a round trip that returns the day it leaves.
        dep = dep_i = None
        for i, s in enumerate(datestrs):
            d = parse_found_date(s, dwin[0], dwin[1])
            if d:
                dep, dep_i = d, i
                break
        if not dep:
            continue                       # its dates are outside the requested window
        ret = None
        if rwin and rwin[0]:
            for s in datestrs[dep_i + 1:]:
                d = parse_found_date(s, rwin[0], rwin[1])
                if d and d >= dep:
                    ret = d
                    break
            if not ret:
                continue                   # a round trip whose return we cannot pin is not a pair
        if best is None or value < best["value"]:
            best = {"value": value, "currency": currency,
                    "depart_found": dep.isoformat(),
                    "ret_found": ret.isoformat() if ret else None,
                    "date_basis": date_basis}
    if not best:
        return None, "fares carry dates, but none inside the requested window"
    return best, None


# ---------------------------------------------------------------- confidence and quorum

def independent(readings):
    """One reading per OWNER. Two agreeing Booking Holdings properties are one source: they share a
    fare engine, so their agreement is a fact about that engine, not corroboration."""
    seen, out = set(), []
    for r in sorted(readings, key=lambda r: r["value"]):
        o = r.get("owner") or r["site"]
        if o in seen:
            continue
        seen.add(o)
        out.append(r)
    return out


def decide(readings, allow_single=False):
    """(best, confidence, agreeing) over validated readings.

    high  -> quorum: >=2 owner-independent readings agreeing within AGREE_PCT, and the reported
             reading's own date_basis is strong. Only `high` may text.
    medium-> exactly one source, or the best reading rests on dates the watcher chose.
    """
    if not readings:
        return None, None, []
    indep = independent(readings)
    best = indep[0]
    ceiling = CEILING.get(best["date_basis"], "medium")
    agreeing = [r for r in indep
                if abs(r["value"] - best["value"]) <= best["value"] * AGREE_PCT / 100.0]
    quorum = len(agreeing) >= 2 and best["date_basis"] in STRONG_BASES
    if quorum or (allow_single and best["date_basis"] in STRONG_BASES):
        conf = "high" if ceiling == "high" else "medium"
    else:
        conf = "medium"
    return best, conf, agreeing


def itinerary_label(origin, dest, dep, ret):
    """Short, ASCII, and DOT-FREE. alert_transports.URL_RE strips bare-domain-shaped tokens from an
    SMS, so 'Mar. 8' would be silently eaten by the carrier gateway while 'Mar 8' survives."""
    def d(s):
        if not s:
            return ""
        dt = datetime.strptime(s, "%Y-%m-%d").date()
        return f"{dt.strftime('%b')} {dt.day}"
    a = d(dep)
    b = d(ret)
    if a and b:
        # Same month -> "Mar 8-15"; different -> "Mar 28-Apr 4".
        am, bm = a.split()[0], b.split()[0]
        tail = b.split()[1] if am == bm else b
        return f"{origin}-{dest} {a}-{tail}"
    return f"{origin}-{dest} {a}".strip()


# ---------------------------------------------------------------- the run

def load_registry(path):
    return json.load(open(path))


def shippable(reg, only=None):
    out = []
    for s in reg["sites"]:
        if s.get("verdict") not in ("usable", "usable_with_browser"):
            continue
        if only and s["domain"] not in only:
            continue
        out.append(s)
    return out


def build_for(site, mode, origin, dest, dep, ret, adults):
    key = {"exact": ("one_way_template" if not ret else "url_template"),
           "month": "month_url_template", "range": "range_url_template",
           "calendar": ("one_way_template" if not ret else "url_template")}[mode]
    tpl = site.get(key)
    if not tpl:
        return None
    try:
        return tpl.format(**fp._slots(origin, dest, dep, ret, adults))
    except (KeyError, IndexError):
        return None


def run(a, now=None, today=None, fetch_plain=None, fetch_browser=None):
    """One check. Seams for the clock and both fetchers so the suite runs offline with an injected
    clock -- TRACKING_ENHANCEMENT.md:104: 'An injected clock finds what production hides.'"""
    now = time.time() if now is None else now
    today = today or date.today()
    fetch_plain = fetch_plain or (lambda u: fp.fetch_plain(u)[0])
    fetch_browser = fetch_browser or (lambda u, sel=None: fp.fetch_browser(u, sel)[0])

    reg = load_registry(a.registry)
    only = {h.strip() for h in a.sites.split(",")} if a.sites else None
    sites = shippable(reg, only)

    depart_spec = ({"kind": "exact", "date": a.depart} if a.depart else
                   {"kind": "month", "month": a.depart_month} if a.depart_month else
                   {"kind": "range", "from": a.depart_range.split(":")[0],
                    "to": a.depart_range.split(":")[1]} if a.depart_range else None)
    if a.one_way:
        ret_spec = None
    elif a.ret:
        ret_spec = {"kind": "exact", "date": a.ret}
    elif a.return_month:
        ret_spec = {"kind": "month", "month": a.return_month}
    elif a.depart_month:
        # A same-month round trip is overwhelmingly the common case. Defaulted rather than asked, and
        # the pipe states it in the confirmation so one word corrects it.
        ret_spec = {"kind": "month", "month": a.depart_month}
    else:
        ret_spec = None

    if a.print_urls:
        for s in reg["sites"]:
            r = resolve_rung(s, depart_spec, ret_spec, a.trip_days, today)
            if not r:
                print(f"  {s['domain']:24s} — cannot express this ask (date_flex="
                      f"{s.get('date_flex')})")
                continue
            mode, basis, dep, ret = r
            print(f"  {s['domain']:24s} {basis:22s} {build_for(s, mode, a.origin, a.dest, dep, ret, a.adults)}")
        return 0

    state = pw.read_state(a.state)
    itin = (f"{a.origin}-{a.dest}/{a.depart or a.depart_month or a.depart_range}/"
            f"{a.ret or a.return_month or ('ow' if a.one_way else '')}/{a.adults}/{a.cabin}")
    if state.get("itin") and state["itin"] != itin:
        # A reused --state name with a different itinerary is a different monitor and inherits
        # nothing. price_watch.py binds state to its URL for the same reason; this box already has
        # five orphaned fare state files with the obvious names.
        state = {}
    state["itin"] = itin
    state["mode"] = "fare"

    if not sites:
        # A config limit, not an infrastructure error: exit 0, say so ONCE, spend no fetches.
        if not state.get("unsupported_alerted"):
            state["unsupported_alerted"] = True
            pw.emit({"to": a.alert_to, "item": itinerary_label(a.origin, a.dest, a.depart, a.ret),
                     "url": None, "unit": a.unit, "monitor": a.monitor, "schedule": a.schedule,
                     "kind": "fare_unsupported"})
        print("LOG: this monitor cannot run yet — no flight site in scripts/flight_sites.json is "
              "cleared to read a real fare. Measure them with flight_probe.py and set ship/verdict.")
        pw.write_state(a.state, state)
        return 0

    dwin = spec_window(depart_spec, today)
    rwin = spec_window(ret_spec, today) if ret_spec else None

    readings, notes, t0 = [], [], time.monotonic()
    for s in sites[:a.max_sites]:
        if time.monotonic() - t0 > RUN_DEADLINE_S:
            notes.append("ran out of time")
            break
        r = resolve_rung(s, depart_spec, ret_spec, a.trip_days, today)
        if not r:
            notes.append(f"{s['domain']} cannot express this ask")
            continue
        mode, basis, dep, ret = r
        url = build_for(s, mode, a.origin, a.dest, dep, ret, a.adults)
        if not url:
            notes.append(f"{s['domain']} has no template for {mode}")
            continue
        want_browser = s.get("needs_browser") and a.browser != "never"
        try:
            html = (fetch_browser(url, (s.get("extract") or {}).get("wait_selector"))
                    if want_browser else fetch_plain(url))
        except Exception as e:
            notes.append(f"{s['domain']} {pw.classify_error(e)}")
            continue
        reading, why = read_fare(html, s, mode, basis, dwin, rwin, s.get("currency"))
        if not reading:
            notes.append(f"{s['domain']} {why}")
            continue
        reading.update(site=s["domain"], owner=s.get("owner"), url=url,
                       book_url=build_for(s, "exact", a.origin, a.dest,
                                          reading["depart_found"], reading["ret_found"], a.adults))
        readings.append(reading)

    best, conf, agreeing = decide(readings, allow_single=a.allow_single_source)

    if not best:
        n = state.get("empty_streak", 0) + 1
        state["empty_streak"] = n
        print(f"LOG: no fare read for {itinerary_label(a.origin, a.dest, a.depart, a.ret)} "
              f"(run {n} in a row) — {'; '.join(notes[:6]) or 'no site answered'}")
        if n == EMPTY_ALERT_AFTER and not state.get("unreadable_alerted"):
            state["unreadable_alerted"] = True
            pw.emit({"to": a.alert_to, "item": itinerary_label(a.origin, a.dest, a.depart, a.ret),
                     "url": None, "unit": a.unit, "monitor": a.monitor, "schedule": a.schedule,
                     "kind": "fare_unreadable", "sites_tried": [s["domain"] for s in sites[:a.max_sites]]})
        pw.write_state(a.state, state)
        return 0

    state["empty_streak"] = 0
    state["unreadable_alerted"] = False
    label = itinerary_label(a.origin, a.dest, best["depart_found"], best["ret_found"])
    cheapest = (" , cheapest in the window" if best["date_basis"] in CHEAPEST_CLAIMABLE else "")
    src = f"{best['site']}"
    print(f"LOG: {label} is {a.unit}{best['value']:.2f} ({src}, {best['date_basis']}, "
          f"{conf} confidence{cheapest})"
          + (f"; " + "; ".join(f"{r['site']} {a.unit}{r['value']:.2f}" for r in agreeing[1:])
             if len(agreeing) > 1 else "")
          + (f"; {'; '.join(notes[:3])}" if notes else ""))

    hit = a.below is not None and best["value"] <= a.below
    if not hit:
        pw.write_state(a.state, state)
        return 0

    prev = state.get("alerted")
    same_value = prev and abs(prev["value"] - best["value"]) < 0.005
    moved = prev and (prev.get("depart_found") != best["depart_found"]
                      or prev.get("ret_found") != best["ret_found"])
    if same_value and moved:
        # The month's floor is unchanged but the cheapest week drifted. Worth saying, not worth a
        # second text: keying suppression on the tuple alone would re-text every time it wobbled.
        print(f"LOG: same {a.unit}{best['value']:.2f}, now on {best['depart_found']}"
              f"{'..' + best['ret_found'] if best['ret_found'] else ''} — already alerted, staying quiet")
        pw.write_state(a.state, state)
        return 0
    if same_value:
        pw.write_state(a.state, state)
        return 0
    if conf != "high" and a.require_confidence:
        why = ("a single source cannot text a fare" if len(agreeing) < 2
               else f"dates chosen by {best['date_basis']} cannot text alone")
        print(f"LOG: holding the alert — {why}. Raise it with --allow-single-source if that is wanted.")
        pw.write_state(a.state, state)
        return 0
    last = state.get("fare_alerted_ts")
    if last is not None and (now - last) < FARE_COOLDOWN_S:
        left = int(FARE_COOLDOWN_S - (now - last))
        print(f"LOG: within the {FARE_COOLDOWN_S // 3600}h alert cooldown ({left}s to go) — quiet")
        pw.write_state(a.state, state)
        return 0

    state["alerted"] = {"value": best["value"], "depart_found": best["depart_found"],
                        "ret_found": best["ret_found"]}
    state["fare_alerted_ts"] = now
    pw.emit({"to": a.alert_to, "item": label, "value": best["value"], "target": a.below,
             "unit": a.unit, "url": best.get("book_url"), "searched_url": best.get("url"),
             "monitor": a.monitor, "schedule": a.schedule, "kind": "fare",
             "itinerary": label, "source": best["site"], "date_basis": best["date_basis"],
             "depart_found": best["depart_found"], "ret_found": best["ret_found"],
             "confidence": conf,
             "sources": [{"site": r["site"], "value": r["value"],
                          "depart_found": r["depart_found"], "ret_found": r["ret_found"],
                          "date_basis": r["date_basis"]} for r in independent(readings)]})
    pw.write_state(a.state, state)
    if a.result_out:
        try:
            os.makedirs(os.path.dirname(a.result_out), exist_ok=True)
            with open(a.result_out, "w") as f:
                json.dump({"ts": now, "itinerary": label, "value": best["value"],
                           "unit": a.unit, "source": best["site"],
                           "depart_found": best["depart_found"], "ret_found": best["ret_found"],
                           "date_basis": best["date_basis"], "confidence": conf,
                           "book_url": best.get("book_url"),
                           "sources": [{"site": r["site"], "value": r["value"]}
                                       for r in independent(readings)],
                           "notes": notes}, f)
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------- selftest

def selftest():
    checks = []

    def ck(label, cond):
        checks.append(bool(cond))
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")

    T = date(2026, 8, 7)

    print("--- date specs and windows ---")
    ck("an exact date is its own window",
       spec_window({"kind": "exact", "date": "2026-09-15"}, T)
       == (date(2026, 9, 15), date(2026, 9, 15)))
    ck("a month spans the whole month",
       spec_window({"kind": "month", "month": "2027-03"}, T)
       == (date(2027, 3, 1), date(2027, 3, 31)))
    ck("a partly-elapsed month narrows to its remaining days",
       spec_window({"kind": "month", "month": "2026-08"}, T)[0] == T)

    print("--- the ladder picks a rung from the MEASURED capability ---")
    sky = {"date_flex": "whole_month", "month_url_template": "m/{depart_ym}/{ret_ym}"}
    cal = {"date_flex": "calendar", "calendar": {"cell": ".c"}, "url_template": "u"}
    non = {"date_flex": "none", "url_template": "u"}
    unk = {"date_flex": "unknown", "url_template": "u"}
    rng = {"date_flex": "range", "range_url_template": "r"}
    dm = {"kind": "month", "month": "2027-03"}
    ck("whole_month -> native_month", resolve_rung(sky, dm, dm, 7, T)[1] == "native_month")
    ck("calendar -> calendar_cheapest", resolve_rung(cal, dm, dm, 7, T)[1] == "calendar_cheapest")
    ck("none -> assumed_month_bounds, 1st out and last back",
       resolve_rung(non, dm, dm, None, T)[1:] == ("assumed_month_bounds", "2027-03-01", "2027-03-31"))
    ck("unknown is NOT queried", resolve_rung(unk, dm, dm, 7, T) is None)
    ck("range with a trip length -> explicit_range",
       resolve_rung(rng, dm, dm, 7, T)[1] == "explicit_range")
    ck("range WITHOUT a trip length is skipped, not sent unbounded",
       resolve_rung(rng, dm, dm, None, T) is None)
    ck("an exact ask ignores date_flex entirely",
       resolve_rung(unk, {"kind": "exact", "date": "2026-09-15"},
                    {"kind": "exact", "date": "2026-09-22"}, None, T)
       == ("exact", "exact", "2026-09-15", "2026-09-22"))

    print("--- parse_found_date infers the year from the window and discards outsiders ---")
    lo, hi = date(2027, 3, 1), date(2027, 3, 31)
    ck("'Mar 8' inside the window resolves", parse_found_date("Mar 8", lo, hi) == date(2027, 3, 8))
    ck("'Mon, Mar 8' resolves", parse_found_date("Mon, Mar 8", lo, hi) == date(2027, 3, 8))
    ck("'8 Mar' resolves", parse_found_date("8 Mar", lo, hi) == date(2027, 3, 8))
    ck("an ISO date resolves", parse_found_date("2027-03-08", lo, hi) == date(2027, 3, 8))
    ck("'Apr 8' is OUTSIDE the window and discarded",
       parse_found_date("Apr 8", lo, hi) is None)
    ck("a window spanning new year resolves the right side",
       parse_found_date("Jan 5", date(2026, 12, 20), date(2027, 1, 10)) == date(2027, 1, 5))
    ck("garbage is None", parse_found_date("sometime", lo, hi) is None)

    print("--- exact mode: the listing guard still applies ---")
    site = {"currency": "CAD"}
    many = " ".join(f"<span>C${100 + i}.00</span>" for i in range(8))
    r, why = read_fare(many, site, "exact", "exact",
                       (date(2026, 9, 15), date(2026, 9, 15)),
                       (date(2026, 9, 22), date(2026, 9, 22)), "CAD")
    ck("8 distinct fares on an exact page -> no reading", r is None)
    ck("...and the reason names the count", "8 different fares" in (why or ""))
    r, why = read_fare("<span>C$412.00</span> incl tax <span>C$412.00</span>", site, "exact",
                       "exact", (date(2026, 9, 15), date(2026, 9, 15)),
                       (date(2026, 9, 22), date(2026, 9, 22)), "CAD")
    ck("one fare repeated -> a reading", r and r["value"] == 412.0)
    ck("...carrying the requested dates", r["depart_found"] == "2026-09-15")

    print("--- month mode: THE TUPLE RULE ---")
    bare = " ".join(f"<span>C${300 + i}.00</span>" for i in range(9))
    r, why = read_fare(bare, site, "month", "native_month", (lo, hi), (lo, hi), "CAD")
    ck("9 month fares, none carrying dates -> NO reading", r is None)
    ck("...and the reason says a minimum would be arbitrary", "arbitrary" in (why or ""))
    good = ('<div>Mon, Mar 8 - Sun, Mar 15</div><span>C$480.00</span>'
            '<div>Wed, Mar 10 - Wed, Mar 17</div><span>C$412.00</span>')
    r, why = read_fare(good, site, "month", "native_month", (lo, hi), (lo, hi), "CAD")
    ck("fares beside their dates -> a reading", r is not None)
    ck("...and it is the MINIMUM", r and r["value"] == 412.0)
    ck("...carrying the dates it was found on",
       r and r["depart_found"] == "2027-03-10" and r["ret_found"] == "2027-03-17")
    # Both of these were live bugs in this function, caught by the check above. Pinned separately so
    # a future refactor that reintroduces either fails loudly on the mechanism, not on a symptom.
    ck("each card gets ITS OWN dates, not the neighbouring card's",
       m_bind := bind_dates_nearest(good, fp.fare_candidates(good)))
    ck("...480 -> Mar 8/Mar 15", m_bind[0][1] == ["Mon, Mar 8", "Sun, Mar 15"])
    ck("...412 -> Mar 10/Mar 17 (nearest card), in DOCUMENT order not distance order",
       m_bind[1][1] == ["Wed, Mar 10", "Wed, Mar 17"])
    ck("a round trip's return is a DIFFERENT date from its departure",
       r and r["ret_found"] != r["depart_found"] and r["ret_found"] == "2027-03-17")

    out = ('<div>Mon, Jun 8 - Sun, Jun 15</div><span>C$412.00</span>')
    r, why = read_fare(out, site, "month", "native_month", (lo, hi), (lo, hi), "CAD")
    ck("a fare dated outside the window is discarded", r is None)
    ck("...with a reason saying so", "outside" in (why or "") or "window" in (why or ""))

    print("--- teaser rejection ---")
    r, why = read_fare("flights from C$199.00", site, "exact", "exact",
                       (date(2026, 9, 15), date(2026, 9, 15)), None, "CAD")
    ck("a page whose only number is a 'from' teaser -> no reading", r is None)
    ck("...and no number leaks into the reason", "199" not in (why or ""))

    print("--- quorum: owners, not hosts ---")
    A = {"value": 412.0, "site": "a.com", "owner": "booking", "date_basis": "native_month",
         "depart_found": "2027-03-10", "ret_found": "2027-03-17"}
    B = dict(A, site="b.com")                       # same owner
    C = dict(A, site="c.com", owner="google", value=430.0)
    best, conf, agree = decide([A, B])
    ck("two same-owner readings are ONE source -> medium", conf == "medium")
    ck("...and only one of them is counted", len(agree) == 1)
    best, conf, agree = decide([A, C])
    ck("two owner-independent agreeing readings -> high", conf == "high")
    ck("...reporting the minimum", best["value"] == 412.0)
    far = dict(C, value=900.0)
    best, conf, agree = decide([A, far])
    ck("readings 100%+ apart do not agree -> medium", conf == "medium")

    print("--- date_basis caps what may text ---")
    weak = dict(A, date_basis="assumed_month_bounds")
    weak2 = dict(weak, site="d.com", owner="expedia")
    best, conf, agree = decide([weak, weak2])
    ck("two agreeing rung-4 readings still cannot reach high", conf == "medium")
    ck("rung 4 cannot claim 'cheapest'", "assumed_month_bounds" not in CHEAPEST_CLAIMABLE)
    ck("calendar_cheapest CAN claim cheapest", "calendar_cheapest" in CHEAPEST_CLAIMABLE)
    ck("a lone strong reading reaches high only with --allow-single-source",
       decide([A])[1] == "medium" and decide([A], allow_single=True)[1] == "high")

    print("--- the SMS label is short, ASCII and DOT-FREE ---")
    lab = itinerary_label("YYZ", "YVR", "2027-03-08", "2027-03-15")
    ck(f"same-month label is compact ({lab!r})", lab == "YYZ-YVR Mar 8-15")
    ck("no dot anywhere (URL_RE would eat a dotted token)", "." not in lab)
    ck("cross-month label names both months",
       itinerary_label("YYZ", "YVR", "2027-03-28", "2027-04-04") == "YYZ-YVR Mar 28-Apr 4")
    ck("one-way label has no range",
       itinerary_label("YYZ", "YVR", "2027-03-08", None) == "YYZ-YVR Mar 8")
    at = _load("alert_transports")
    tpl = _load("alert_templates")
    body = at.sms_body(f"Hi ohmz, Ohmz AI here! {lab} is $412.00, under your $900.00 target. "
                       f"Dates and link in email.")
    ck(f"a full flex SMS fits 140 ASCII (len {len(body)})", len(body) <= 140)
    ck("...and the found dates survive the URL stripper", "Mar 8-15" in body)

    print("--- registry gating: nothing is queried until a human ships a site ---")
    reg = load_registry(REGISTRY)
    ck("zero shippable sites right now", shippable(reg) == [])

    n = len(checks)
    bad = checks.count(False)
    print(f"\n{n} checks — {'ALL PASS' if not bad else f'{bad} FAILURE(S)'}")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--origin")
    ap.add_argument("--dest")
    ap.add_argument("--depart")
    ap.add_argument("--depart-month", dest="depart_month")
    ap.add_argument("--depart-range", dest="depart_range")
    ap.add_argument("--return", dest="ret")
    ap.add_argument("--return-month", dest="return_month")
    ap.add_argument("--one-way", dest="one_way", action="store_true")
    ap.add_argument("--trip-days", dest="trip_days", type=int)
    ap.add_argument("--trip-flex", dest="trip_flex", type=int, default=3)
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--cabin", default="economy")
    ap.add_argument("--below", type=float)
    ap.add_argument("--state")
    ap.add_argument("--alert-to", dest="alert_to", default="ohmz")
    ap.add_argument("--unit", default="$")
    ap.add_argument("--monitor", default=None)
    ap.add_argument("--schedule", default=None)
    ap.add_argument("--sites", default=None)
    ap.add_argument("--max-sites", dest="max_sites", type=int, default=MAX_SITES)
    ap.add_argument("--browser", choices=("auto", "never", "always"), default="auto")
    # Default ON, the inverse of price_watch. The dangerous mode has to be typed.
    ap.add_argument("--require-confidence", dest="require_confidence",
                    action="store_true", default=True)
    ap.add_argument("--allow-single-source", dest="allow_single_source", action="store_true")
    ap.add_argument("--result-out", dest="result_out", default=None)
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--print-urls", dest="print_urls", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not (a.origin and a.dest and (a.depart or a.depart_month or a.depart_range)):
        print("need --origin, --dest and one of --depart / --depart-month / --depart-range",
              file=sys.stderr)
        return 2
    if not a.print_urls and not a.state:
        print("need --state", file=sys.stderr)
        return 2
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

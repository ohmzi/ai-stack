#!/usr/bin/env python3
"""What an alert SAYS — one place, driven by structured data rather than string-building at source.

Why this exists. Alerts used to be whatever sentence the emitting job happened to print, wrapped in
a subject line built by slicing that sentence. It produced texts like

    amazon B0DP6D3TRB price: 46.99, under your 50.00 target (high confidence) Link in email.

which names a job id and a source ID, and never names the thing the user is actually watching. The
person reading it at 7am wants to know WHAT got cheap, not which cron entry fired.

So a job now emits a payload describing what happened, and rendering lives here:

    {"to": "ohmz", "kind": "price_drop", "item": "Zakkart Cat Scratching Board",
     "value": 46.99, "prev": 49.99, "target": 50.0, "unit": "$", "url": "https://...",
     "confidence": "high", "monitor": "amazon B0DP6D3TRB price", "schedule": "every 6h"}

Three surfaces come out of it — an SMS (140 ASCII characters, no web addresses), an email subject,
and an HTML email with a plain-text alternative. They are deliberately consistent: a user holding
the text next to the email should see one event described twice, not two events.

Every field is OPTIONAL. A payload with only `kind` and `to` still renders something honest, because
the failure mode being designed against is an alert that cannot be sent at all. Nothing here invents
a value it was not given: `item` comes from the page's own <title>, never from a model.

Kinds are open-ended — an unknown kind falls back to a generic renderer rather than raising, so a
future job type reaches the user before anyone updates this file.
"""
import datetime as _dt
import html as _html
import re

# ---------------------------------------------------------------- small helpers

_SITE_SUFFIX = re.compile(
    r"\s*[|–—-]\s*(?:amazon(?:\.\w+)*|walmart|best ?buy|ebay|etsy|newegg|costco|"
    r"books to scrape.*|sandbox|home ?depot|target|aliexpress|shop|store)\b.*$", re.I)


def item_label(title, limit=52):
    """A page <title> reduced to something a person would call the thing.

    Retail titles are keyword soup — "Zakkart 2-Pack Cat Scratching Board, 65cm Tall Cardboard L
    Shape Vertical Cat Scratchers for Indoor Cats, Cat Scratch Pad..." — written for search, not for
    a lock screen. The useful name is almost always the first clause, so cut at the first comma or
    separator and drop the site name. Deterministic: no model, no guessing, and if the result is
    empty the caller falls back to the monitor name.
    """
    if not title:
        return None
    t = re.sub(r"\s+", " ", str(title)).strip()
    t = _SITE_SUFFIX.sub("", t)
    t = re.split(r"\s*[|]\s*", t)[0]
    head = re.split(r",", t)[0].strip()
    # Only accept the comma-cut if it left something substantial; some titles are one long clause.
    if len(head) < 12:
        head = t
    if len(head) > limit:
        cut = head[:limit].rsplit(" ", 1)[0]
        head = (cut or head[:limit]).rstrip(" ,-") + "…"
    return head.strip() or None


def money(value, unit=None):
    """A number formatted the way its unit is actually written.

    Not everything watched is money: a CPU threshold rendered "91.00 %" reads like a machine wrote
    it. Currency keeps two decimals because $46.9 is wrong; a percentage or a bare count does not.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    u = (unit or "").strip()
    plain = f"{f:,.0f}" if f == int(f) else f"{f:,.2f}".rstrip("0").rstrip(".")
    if u == "%":
        return f"{plain}%"
    if u in ("$", "£", "€", "¥"):
        return f"{u}{f:,.2f}"
    if len(u) == 3 and u.isalpha():          # CAD, USD, GBP
        return f"{f:,.2f} {u}"
    return f"{plain} {u}" if u else plain


def _thing(p):
    """What to call the subject of this alert, or None when nothing usable was supplied.

    Returning None rather than a stock phrase matters: the SMS builds "the listing you're tracking
    - {thing} -", and a fallback phrase there produced "the listing you're tracking - the page
    you're tracking -". When there is no name, the clause is dropped instead.
    """
    return p.get("item") or p.get("monitor") or None


def _noun(p):
    """The word for what kind of thing this is, used in prose.

    "listing" is wrong for half of what a person can ask to be watched, and a message that calls a
    flight a listing reads like it was written by something that did not understand the request.
    Anything genuinely generic falls back to "task you assigned me", which is true of everything.
    """
    return {"fare": "fare", "inventory": "stock level", "availability": "availability",
            "back_in_stock": "item", "out_of_stock": "item", "price_drop": "listing",
            "price_rise": "listing", "unreachable": "page", "blocked": "page",
            "no_value": "page", "not_found": "item", "fare_unsupported": "fare",
            "fare_unreadable": "fare", "fare_needs_itinerary": "fare watch",
            "recovered": "page"}.get(p.get("kind"), "task you assigned me")


def _phrase(noun):
    """The noun as a sentence subject.

    "task you assigned me" already contains its own clause, so the generic wrapper produced "the
    task you assigned me you're tracking". A noun that already says what the user did to it does
    not get told again.
    """
    return f"the {noun}" if "you" in noun else f"the {noun} you're tracking"


def _conf_short(p):
    return "" if p.get("confidence") in (None, "high") else " (unconfirmed)"


def _conf_long(p):
    c = p.get("confidence")
    if c in (None, "high"):
        return None
    note = p.get("confidence_note")
    return f"Unconfirmed reading{' - ' + note if note else ''}."


# ---------------------------------------------------------------- per-kind copy
#
# Each entry returns (headline, sentence). `headline` is the short label at the top of the email and
# the lead of the subject; `sentence` is the human line used in the SMS and the email body.

def _price_drop(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    prev = money(p.get("prev"), p.get("unit"))
    if not v:
        return "Price drop", ("dropped under your " + t + " target" if t else "hit your target")
    s = f"is {v}"
    if t:
        s += f", under your {t} target"
    if prev and prev != v:
        s += f" (was {prev})"
    return "Price drop", s


def _price_rise(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    prev = money(p.get("prev"), p.get("unit"))
    if not v:
        return "Price rise", ("rose past your " + t + " threshold" if t else "passed your threshold")
    s = f"is {v}"
    if t:
        s += f", over your {t} threshold"
    if prev and prev != v:
        s += f" (was {prev})"
    return "Price rise", s


def _back_in_stock(p):
    v = p.get("value")
    s = "is available again"
    # "Only 3 left" is the whole reason someone set a restock watch on a hard-to-get item, so when
    # the page said a number, the text says it too.
    if p.get("state") == "limited" and v is not None:
        s += f" - only {v} left"
    return "Back in stock", s


def _out_of_stock(p):
    # "has just gone out of stock" claims a TRANSITION, and firing is a predicate on the current
    # reading: a first-ever reading of a page that was already sold out would be asserting something
    # about timing that nobody observed. Say it only when a previous reading was available.
    if p.get("prev_state") in ("in_stock", "limited"):
        return "Out of stock", "has just gone out of stock"
    return "Out of stock", "is out of stock"


def _fare(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    if not v:
        return "Fare drop", ("dropped under your " + t + " target" if t else "hit your target")
    s = f"is {v}"
    if t:
        s += f", under your {t} target"
    return "Fare drop", s + _how_dates(p)


def _how_dates(p):
    """The clause that says how a flex-mode fare's dates were chosen, or "".

    A month watch reports a fare on dates the user never named, so leaving this out would make a
    chosen-by-fallback itinerary read exactly like one they picked. Rung 4 takes its own branch and
    the word "cheapest" is not reachable from it: it priced 1st-of-month out and last-of-month back,
    which is one arbitrary pair, and calling that the cheapest would make the reader book it
    believing nothing better existed. (If Mar 1->Mar 31 is $412 while Mar 8->Mar 15 is $280, that
    single word is the whole difference between a useful alert and an expensive one.)
    """
    basis = p.get("date_basis")
    where = p.get("window")
    if basis in ("native_month", "explicit_range", "calendar_cheapest"):
        return f", cheapest in {where}" if where else ", the cheapest dates I found"
    if basis == "assumed_month_bounds":
        return ", on the only dates that site would quote"
    return ""


def _inventory(p):
    # money() with an empty unit, not the raw value: a count arrives as a number and "3.0 left" is
    # what a bare float looks like once it has been through JSON.
    v, prev, t = p.get("value"), p.get("prev"), p.get("target")
    n = money(v, "")
    if v is None:
        s = "changed"
    elif prev is None:
        s = f"has {n} left"
    elif v < prev:
        s = f"is down to {n} left"
    else:
        s = f"is back up to {n} left"
    if prev is not None and v is not None:
        s += f" (was {money(prev, '')})"
    if t is not None:
        s += f", at or below your {money(t, '')} target" if v is not None and v <= t \
            else f", against your {money(t, '')} target"
    return "Stock level", s


def _availability(p):
    v = p.get("value")
    return "Now available", ("has availability" if v is None else f"has {money(v, '')} available")


def _threshold(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    op = p.get("op") or "past"
    s = f"is {v}" if v else "met your condition"
    if t:
        s += f", {op} your {t} threshold"
    return "Threshold met", s


def _change(p):
    return "Changed", "has changed since the last check"


def _unreachable(p):
    err = p.get("error")
    s = "can't be reached any more"
    return "Can't reach it", s + (f" ({err})" if err else "")


def _blocked(p):
    return "Blocked by the site", "is refusing automated checks"


def _no_value(p):
    return "Can't read it any more", "still loads, but no longer shows a readable value"


def _fare_unsupported(p):
    # Short on purpose: the SMS has 140 ASCII characters and the reason does not fit in them. The
    # full explanation is the ADVICE entry, which only the email carries.
    #
    # NARROWED when flight_watch.py shipped. It used to mean "a fare cannot be watched", which is no
    # longer true -- one can be read from a URL built out of an itinerary. It now means the narrower
    # and still-real thing: no flight site has been MEASURED and cleared to read one yet. Hence
    # "yet": the difference between a capability that does not exist and one that is not switched on
    # is the difference between the user giving up and the user asking again next week.
    return "Can't watch a fare yet", "can't be watched yet - no flight site is cleared to read one"


def _fare_unreadable(p):
    # Distinct from _fare_unsupported, and the distinction is the user's next action. "Unsupported"
    # means nothing on this host is cleared to read a fare, so there is nothing to wait for.
    # "Unreadable" means the sites ARE cleared and did not answer this time -- a rotated bot
    # challenge or a markup change -- so the monitor keeps running and may well heal itself.
    return "Can't read the fare", "couldn't be read on any flight site I checked"


def _fare_needs_itinerary(p):
    return ("Needs dates",
            "needs a departure and destination airport and travel dates before it can be watched")


def _not_found(p):
    return "Can't find it", "couldn't be found by an online search yet"


def _recovered(p):
    v = money(p.get("value"), p.get("unit"))
    return "Back to normal", (f"is readable again, now {v}" if v else "is reachable again")


KINDS = {
    "price_drop": _price_drop, "price_rise": _price_rise,
    "back_in_stock": _back_in_stock, "out_of_stock": _out_of_stock,
    "fare": _fare, "inventory": _inventory, "availability": _availability,
    "threshold": _threshold, "change": _change,
    "unreachable": _unreachable, "blocked": _blocked, "no_value": _no_value,
    "not_found": _not_found, "fare_unsupported": _fare_unsupported, "recovered": _recovered,
    "fare_unreadable": _fare_unreadable, "fare_needs_itinerary": _fare_needs_itinerary,
}
# Kinds that report a PROBLEM with the monitor rather than a result from it. They read differently
# (something needs your attention, rather than something you asked for happened) and they carry an
# instruction, because an error the user cannot act on is just noise.
PROBLEM_KINDS = {"unreachable", "blocked", "no_value", "not_found", "fare_unsupported",
                 "fare_unreadable", "fare_needs_itinerary"}

ADVICE = {
    "unreachable": "Double-check the link still opens in a browser. If the page moved, ask me to "
                   "set the monitor up again with the new address.",
    "blocked": "The site is blocking automated checks, so this monitor can't keep working. Ask me "
               "to watch a different page for it.",
    "no_value": "The page layout has probably changed. Ask me to set this monitor up again and "
                "I'll re-read it.",
    "fare_unsupported": "I can watch a flight fare, but only from a flight site that has been "
                        "tested and cleared to read one, and right now none has been. A fare only "
                        "exists for one route on one set of dates, so a price read off a general "
                        "search result is a 'from' teaser or an unrelated trip - and a price you "
                        "can't book is worse than no alert at all. Nothing is wrong with your "
                        "monitor; the reading side just isn't switched on yet. In the meantime you "
                        "can set a price alert on Google Flights directly.",
    "not_found": "I searched the web but couldn't find a page for this item. Ask me to set the "
                 "monitor up again with a direct link, or a better description of what to "
                 "look for.",
    "fare_unreadable": "The flight sites I check didn't show me a readable fare for these dates "
                       "this time - usually a site changing its layout or challenging automated "
                       "visits. The monitor is still running and will pick the fare up again if it "
                       "comes back, so there's nothing you need to do. If it stays quiet for a day "
                       "or two, ask me and I'll look at which sites are failing.",
    "fare_needs_itinerary": "A fare only exists for one route on one set of dates, so I can't "
                            "watch 'flights to Vancouver' on its own. Tell me where you're leaving "
                            "from, where you're going, and roughly when - a month is enough, I'll "
                            "find the cheapest dates in it - and I'll set the watch up properly.",
}


def describe(payload):
    """(headline, sentence) for any payload, including kinds this file has never heard of."""
    fn = KINDS.get(payload.get("kind"))
    if fn:
        return fn(payload)
    return "Update", "met the condition you set"


# ---------------------------------------------------------------- the three surfaces

# render_sms must measure the string the HANDSET will get, not a prettier one. alert_transports
# folds to ASCII on the way out ("…" becomes "..."), so measuring here with typographic characters
# under-counts and lets the transport's own truncation fire — which cut the pointer to the email,
# the one part that must always survive. Fold first, then count.
_SMS_ASCII = {"\u2026": "...", "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
              "\u201c": '"', "\u201d": '"', "\u00a0": " "}


def _ascii(text):
    for bad, good in _SMS_ASCII.items():
        text = (text or "").replace(bad, good)
    return text.encode("ascii", "ignore").decode("ascii")


def render_sms(payload, limit=140):
    """One ASCII segment, no web addresses, greeting the user by name.

    Degrades in a fixed order when it will not fit: the item name is shortened first, then the
    greeting is dropped, and the pointer to the email is never dropped — it is the only thing
    telling a first-time user where the link went. The caller (alert_transports.sms_body) still
    performs the final address-strip and ASCII fold; this only has to be sensible.
    """
    who = _ascii((payload.get("to") or "").strip())
    headline, sentence = describe(payload)
    sentence = _ascii(sentence)
    thing, noun = _ascii(_thing(payload) or "") or None, _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    pointer = "Details in email." if not payload.get("url") else "Link in email."

    # The previous value belongs in the email, not the buzz — it cost 13 characters and pushed the
    # item name, which is the whole point of the message, into truncation.
    sentence = re.sub(r"\s*\(was [^)]*\)", "", sentence)

    # Who is texting? The message arrives from a mail-to-SMS gateway, so the handset shows an
    # email address the user has no reason to recognise. Naming the assistant answers the first
    # question a text from an unknown sender raises, which is worth its characters — and it is
    # configured, not hard-coded, so renaming the assistant never means editing this file.
    assistant = _ascii((payload.get("assistant") or "").strip())

    def build(name, greet=True, ident=True):
        lead = f"Hi {who}, " if (greet and who) else ""
        if ident and assistant:
            lead += f"{assistant} here! "
        head = "Heads up - " if problem else ""
        conf = "" if problem else _conf_short(payload)
        # With a greeting and an identity in front, "the listing you're tracking" is ceremony the
        # 140 characters cannot afford — the user knows why they are being texted. The item name
        # carries the meaning on its own.
        subject = name if name else _phrase(noun)
        core = f"{head}{subject} {sentence}{conf}."
        if lead.endswith("! ") or not lead:
            core = core[:1].upper() + core[1:]
        return f"{lead}{core} {pointer}"

    name = thing or ""
    out = build(name)
    if len(out) <= limit:
        return out
    # 1. shorten the item name
    over = len(out) - limit
    if len(name) - over > 16:
        return build(name[:len(name) - over - 3].rstrip(" ,-") + "...")
    name = name[:22].rstrip(" ,-") + "..." if len(name) > 22 else name
    # 2. drop the personal greeting before the identity: on a text from an address the user does
    #    not recognise, "who is this" matters more than "hello by name".
    for kw in ({"greet": False}, {"greet": False, "ident": False}):
        out = build(name, **kw)
        if len(out) <= limit:
            return out
    # 3. last resort: keep the pointer, trim the middle
    return out[:limit - len(pointer) - 2].rstrip() + " " + pointer


def render_subject(payload, limit=120):
    headline, sentence = describe(payload)
    thing = _thing(payload)
    if not thing:
        subj = f"{headline}: your monitor {sentence}"
    elif payload.get("kind") in PROBLEM_KINDS:
        subj = f"{headline}: {thing}"
    else:
        # The lead clause only — "is $46.99", not "is $46.99, under your $50.00 target (was
        # $49.99)". The target and the previous price are already in the body one tap away, and
        # every clause here costs characters of the item name in a crowded inbox line. Commas in
        # this module's sentences only ever introduce subordinate clauses (the item name, which
        # can contain real commas, is `thing`), so cutting at the first one is safe.
        short = re.sub(r"\s*\(was [^)]*\)", "", sentence)
        short = re.split(r",\s", short)[0]
        subj = f"{headline}: {thing} {short}"
    subj = re.sub(r"\s{2,}", " ", subj).strip()
    return subj[:limit - 1] + "…" if len(subj) > limit else subj


def _pretty_day(iso):
    """'2027-03-08' -> 'Mon 8 Mar 2027'. Unparseable input is returned as given rather than dropped."""
    try:
        d = _dt.datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return iso
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}"


# One map, used by the plain text and the HTML alike, so the two surfaces cannot drift into
# describing the same date_basis with different words.
_DATE_BASIS_HOW = {
    "native_month": "using its own whole-month search",
    "explicit_range": "over the date range I asked for",
    "calendar_cheapest": "read off its price calendar",
    "assumed_month_bounds": "the only dates it would quote - NOT the cheapest in the month",
    "exact": "for the dates you gave me",
}


def _nights(dep, ret):
    """Nights between two ISO dates, or None when they don't parse or aren't positive."""
    try:
        n = (_dt.datetime.strptime(ret, "%Y-%m-%d")
             - _dt.datetime.strptime(dep, "%Y-%m-%d")).days
        return n if n > 0 else None
    except Exception:
        return None


def _dates_lines(payload):
    """The dates a flex-mode fare was actually found on. MANDATORY when they exist.

    Without this the email says "March is $412" and the reader has to re-search the whole month to
    find which week it was -- and if they guess a different week and see a higher number, they
    conclude the alert was wrong. The dates are the deliverable, not a detail.
    """
    dep = payload.get("depart_found")
    if not dep:
        return []
    ret = payload.get("ret_found")
    line = f"  dates found: {_pretty_day(dep)}"
    if ret:
        line += f"  ->  {_pretty_day(ret)}"
        n = _nights(dep, ret)
        if n:
            line += f"   ({n} night{'s' if n != 1 else ''})"
    out = [line]
    src = payload.get("source")
    if src:
        how = _DATE_BASIS_HOW.get(payload.get("date_basis"), "")
        out.append(f"  found on: {src}" + (f", {how}" if how else ""))
    return out


def _sources_lines(payload):
    """What every other site said. This is what makes two sites finding different weeks legible
    rather than looking like a contradiction, and it is where a weakest-rung reading is labelled
    instead of being allowed to imply it found a cheapest."""
    srcs = payload.get("sources") or []
    if len(srcs) < 2:
        return []
    out = ["", "  also checked:"]
    for s in srcs[1:]:
        v = money(s.get("value"), payload.get("unit")) or "-"
        when = ""
        if s.get("depart_found"):
            when = f"   {_pretty_day(s['depart_found'])}"
            if s.get("ret_found"):
                when += f" -> {_pretty_day(s['ret_found'])}"
        tag = (" (only dates this site would quote)"
               if s.get("date_basis") == "assumed_month_bounds" else "")
        out.append(f"    {s.get('site', '?'):22s} {v}{when}{tag}")
    return out


def _link_lines(payload):
    """Up to two links, labelled. Never reached by the SMS - render_sms strips URLs because carrier
    gateways silently drop texts containing them.

    Two because they answer different questions. "Book these exact dates" is built from the site's
    ordinary exact-date template using the dates that were FOUND, so one click reproduces the deal
    instead of dropping the reader back into a month search to hunt for it. "What I searched" is the
    URL the watcher actually read, so the finding is auditable -- the reader can see what it saw.
    """
    url, searched = payload.get("url"), payload.get("searched_url")
    if not url and not searched:
        return []
    if url and searched and url != searched:
        return ["", f"  book these exact dates:  {url}", f"  what I searched:         {searched}"]
    return ["", f"  {url or searched}"]


def render_plain(payload):
    """Plain-text alternative. Same information, same order as the HTML."""
    who = (payload.get("to") or "").strip()
    headline, sentence = describe(payload)
    thing, noun = _thing(payload), _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    assistant = (payload.get("assistant") or "").strip()
    hello = f"Hi {who}," if who else "Hi,"
    lines = [f"{hello} {assistant} here!" if assistant else hello, ""]
    lines.append(f"{_phrase(noun).capitalize()} {sentence}." if not problem
                 else f"One of your monitors needs a look - the {noun} {sentence}.")
    lines[-1] = re.sub(r"\s{2,}", " ", lines[-1])
    if thing:
        lines += ["", f"  {thing}"]
    v = money(payload.get("value"), payload.get("unit"))
    if v:
        lines.append(f"  {v}")
    # The page's own words, when it was an availability that was read rather than a number. This is
    # the "never report a state you didn't read" evidence: a doubtful reading can be checked against
    # the page without anyone having to trust the wording above it.
    if payload.get("state_text"):
        lines.append(f"  the page says: {payload['state_text']}")
    lines += _dates_lines(payload)
    cl = _conf_long(payload)
    if cl:
        lines.append(f"  {cl}")
    lines += _sources_lines(payload)
    lines += _link_lines(payload)
    advice = ADVICE.get(payload.get("kind"))
    if advice:
        lines += ["", advice]
    lines += [""] + _footer_lines(payload)
    return "\n".join(lines)


def _footer_lines(payload):
    bits = []
    sched = payload.get("schedule")
    if sched:
        bits.append(f"Checked {sched}")
    if payload.get("texted_to"):
        bits.append(f"a text went to {payload['texted_to']}")
    tail = ". ".join(x[0].upper() + x[1:] for x in [", ".join(bits)] if x)
    out = [tail] if tail else []
    out.append("Reply in the assistant to change or cancel this monitor.")
    return out


# ---------------------------------------------------------------- the html email's brand
#
# The OhmzAI brand language, as branding/ohmz.css defines it and home.ohmz.cloud typesets it:
# warm near-black surfaces, ONE amber accent spent on the action, depth from flat panels and 1px
# hairlines — no gradients, no shadows, never pure #fff or #000. The email is dark in both of the
# reader's themes because the brand is dark-first and email offers no way to follow a client theme;
# a dark email also survives dark-mode clients, which force-invert light ones.
#
# Values are copied from branding/ohmz.css, the same snapshot ohmz-cloud's tokens.css carries.
# Every text/surface pair below was measured against WCAG AA (4.5:1 for normal text):
# fg on panel 14.09, secondary on panel 9.60, amber on panel 6.49, on-amber on amber 6.46,
# secondary on raise 9.04. --ohmz-muted (#8b857e) measures 4.50 on the panel — exactly on the
# line — so, like the site's semantic layer, this file does not use it at all.
_OHMZ = {
    "canvas": "#1a1917", "panel": "#211f1d", "raise": "#262421",
    "line": "#3a3733", "line_soft": "#302d2a",
    "fg": "#f0edea", "secondary": "#cbc5be",
    "amber": "#e0913f", "on_amber": "#241f18",
}
# The real fonts cannot ride along: an email with no external assets (a strict client blocks them
# anyway) cannot load a webfont, so the stacks lead with the brand face for readers who have it
# installed and fall back exactly the way tokens.css does.
_FONT = ("'Space Grotesk',ui-sans-serif,system-ui,-apple-system,'Segoe UI',"
         "Helvetica,Arial,sans-serif")
_MONO = "'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace"


def _lockup_html(assistant):
    """The wordmark row, or "" when no assistant name is configured.

    The brand lockup sets the stem in the text colour and the last word in amber — OhmzAI does
    "Ohmz"+"AI", the site does "Ohmz"+".cloud" — so this rebuilds that grammar from the CONFIGURED
    assistant name rather than hard-coding a brand string a rename would orphan. The Ω is the text
    character, not the favicon SVG: Gmail strips <svg>, and the mark is drawn from the Space
    Grotesk Ω outline anyway, so the glyph in the same stack is the honest email-safe rendering.
    """
    e = _html.escape
    name = (assistant or "").strip()
    if not name:
        return ""
    parts = name.split()
    stem, suffix = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (name, "")
    text = e(stem) + (f'<span style="color:{_OHMZ["amber"]};font-weight:600;">&nbsp;{e(suffix)}'
                      f'</span>' if suffix else "")
    return (f'<div style="padding-bottom:16px;border-bottom:1px solid {_OHMZ["line_soft"]};">'
            f'<span style="color:{_OHMZ["amber"]};font-weight:600;font-size:19px;">&Omega;</span>'
            f'&nbsp;<span style="font-size:16px;font-weight:500;letter-spacing:-0.02em;'
            f'color:{_OHMZ["fg"]};">{text}</span></div>')


def _details_html(payload):
    """The dates a fare was found on and what the other sites said, as a raised mono panel.

    The plain text has always carried these (_dates_lines calls the dates "the deliverable, not a
    detail"), but the HTML used to drop them — the surface most people read omitted the one thing
    that lets them book the deal. Set in the mono face because it is the site's treatment for
    metadata runs, and on the raised surface because that is how the brand does depth.
    """
    e = _html.escape
    C = _OHMZ
    rows = []
    dep = payload.get("depart_found")
    if dep:
        line = e(_pretty_day(dep))
        ret = payload.get("ret_found")
        if ret:
            line += f" &rarr; {e(_pretty_day(ret))}"
            n = _nights(dep, ret)
            if n:
                line += (f' <span style="color:{C["secondary"]};">({n} night'
                         f'{"s" if n != 1 else ""})</span>')
        rows.append(f'<div style="font-size:14px;color:{C["fg"]};">{line}</div>')
        src = payload.get("source")
        if src:
            how = _DATE_BASIS_HOW.get(payload.get("date_basis"), "")
            rows.append(f'<div style="margin-top:4px;font-size:12px;color:{C["secondary"]};">'
                        f'found on {e(src)}{", " + e(how) if how else ""}</div>')
    srcs = payload.get("sources") or []
    if len(srcs) >= 2:
        rows.append(f'<div style="margin-top:12px;font-size:10px;font-weight:500;'
                    f'letter-spacing:.12em;text-transform:uppercase;color:{C["secondary"]};">'
                    f'also checked</div>')
        for s in srcs[1:]:
            v = money(s.get("value"), payload.get("unit")) or "-"
            when = ""
            if s.get("depart_found"):
                when = f'&nbsp;&nbsp;{e(_pretty_day(s["depart_found"]))}'
                if s.get("ret_found"):
                    when += f' &rarr; {e(_pretty_day(s["ret_found"]))}'
            tag = (" (only dates this site would quote)"
                   if s.get("date_basis") == "assumed_month_bounds" else "")
            rows.append(f'<div style="margin-top:4px;font-size:12px;color:{C["secondary"]};">'
                        f'{e(s.get("site") or "?")}&nbsp;&nbsp;{e(v)}{when}{e(tag)}</div>')
    if not rows:
        return ""
    return (f'<div style="margin-top:18px;padding:14px 16px;background:{C["raise"]};'
            f'border:1px solid {C["line"]};border-radius:10px;font-family:{_MONO};">'
            + "".join(rows) + "</div>")


def render_html(payload):
    """A self-contained HTML email in the OhmzAI brand language: inline styles only, no external
    assets, mobile-friendly.

    Inline styles and a table shell because email clients strip <style> blocks and do not implement
    flexbox. The layout is home.ohmz.cloud's card translated to what email can hold: canvas behind
    a hairline-bordered panel, the mono uppercase label as the kind eyebrow, the value set like a
    heading (600, tight tracking, text colour), and amber spent once — on the button. That single
    accent replaced the old green/problem-amber pair on purpose: the brand has one accent, and the
    headline and advice copy already say whether this is good news or a problem.
    """
    e = _html.escape
    C = _OHMZ
    who = (payload.get("to") or "").strip()
    headline, sentence = describe(payload)
    thing, noun = _thing(payload), _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    v = money(payload.get("value"), payload.get("unit"))
    prev = money(payload.get("prev"), payload.get("unit"))
    target = money(payload.get("target"), payload.get("unit"))

    big = ""
    if v:
        delta = ""
        try:
            if payload.get("prev") is not None and float(payload["prev"]) != float(payload["value"]):
                arrow = "▼" if float(payload["value"]) < float(payload["prev"]) else "▲"
                delta = (f'<span style="font-size:14px;color:{C["secondary"]};font-weight:400;'
                         f'letter-spacing:0;">&nbsp;&nbsp;{arrow} was {e(prev)}</span>')
        except (TypeError, ValueError):
            pass
        sub = f"under your {e(target)} target" if (target and payload.get("kind") in
                                                  ("price_drop", "fare")) else ""
        if target and payload.get("kind") == "price_rise":
            sub = f"over your {e(target)} threshold"
        if target and payload.get("kind") == "inventory":
            sub = f"at or below your {e(target)} left"
        big = (f'<div style="margin:18px 0 4px;font-size:34px;line-height:1.15;font-weight:600;'
               f'letter-spacing:-0.02em;color:{C["fg"]};">{e(v)}{delta}</div>'
               + (f'<div style="font-size:14px;color:{C["secondary"]};">{sub}</div>' if sub else ""))

    # e() because a phrase lifted off a page is untrusted input, exactly like a page title.
    said = (f'<div style="margin:6px 0 0;font-size:14px;color:{C["secondary"]};">the page says: '
            f'{e(payload["state_text"])}</div>' if payload.get("state_text") else "")

    cl = _conf_long(payload)
    # The raised surface and a hairline, not a warning-yellow block: the house style has no second
    # colour to spend, and the words "Unconfirmed reading" carry the caveat on their own.
    conf_html = (f'<div style="margin-top:14px;padding:10px 14px;background:{C["raise"]};'
                 f'border:1px solid {C["line"]};border-radius:10px;font-size:13px;'
                 f'line-height:1.6;color:{C["secondary"]};">{e(cl)}</div>' if cl else "")

    btn = ""
    if payload.get("url"):
        btn = (f'<div style="margin:24px 0 6px;"><a href="{e(payload["url"])}" '
               f'style="display:inline-block;background:{C["amber"]};color:{C["on_amber"]};'
               f'text-decoration:none;padding:10px 18px;border-radius:10px;font-size:15px;'
               f'font-weight:500;">View the {e(noun)} &rarr;</a></div>')

    advice = ADVICE.get(payload.get("kind"))
    advice_html = (f'<div style="margin-top:16px;font-size:14px;line-height:1.6;'
                   f'color:{C["secondary"]};">{e(advice)}</div>' if advice else "")

    assistant = (payload.get("assistant") or "").strip()
    lead = ("One of your monitors needs a look." if problem
            else f"{e(_phrase(noun)).capitalize()} just hit your condition.")
    # _footer_lines writes LINES; joined into one run of text they need the periods the line
    # breaks were providing ("Checked every 6h Reply in the assistant..." is not a sentence).
    footer = " ".join(x if x.endswith(".") else x + "." for x in _footer_lines(payload))

    # No background on the wrapper, deliberately: the brand canvas belongs to the site, and a
    # full-bleed near-black made the email claim the reader's whole viewport. The dark panel
    # floats on whatever the mail client shows behind it instead.
    return f"""<div style="margin:0;padding:0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:{C["panel"]};border:1px solid {C["line_soft"]};border-radius:12px;padding:30px 28px;font-family:{_FONT};color:{C["fg"]};">
<tr><td>
{_lockup_html(assistant)}
<div style="margin-top:20px;font-family:{_MONO};font-size:11px;font-weight:500;letter-spacing:.12em;text-transform:uppercase;color:{C["amber"]};">{e(headline)}</div>
<div style="margin-top:12px;font-size:15px;line-height:1.6;color:{C["secondary"]};">Hi {e(who) or 'there'}{f", {e(assistant)} here!" if assistant else ""} {lead}</div>
{f'<div style="margin-top:14px;font-size:19px;font-weight:600;letter-spacing:-0.02em;line-height:1.35;color:{C["fg"]};">{e(thing)}</div>' if thing else ''}
{big}{said}{_details_html(payload)}{conf_html}{btn}{advice_html}
<div style="margin-top:28px;padding-top:16px;border-top:1px solid {C["line_soft"]};font-size:12px;line-height:1.6;color:{C["secondary"]};">{e(footer)}</div>
</td></tr></table>
</td></tr></table></div>"""

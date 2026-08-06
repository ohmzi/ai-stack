#!/usr/bin/env python3
"""Deterministic price extraction for background monitors. No model at run time.

Why this exists. Asking the agent to scrape a page per run failed in every way an LLM can fail: it
crashed on its own regex, it wrote an ALERT into a variable it never printed, and — on a 1.5 MB
Amazon page — it abandoned the output protocol entirely and emitted marketing prose containing an
invented promo code and a price it had never read. A price check is arithmetic on a fetched string;
it does not need a 34B model, and giving it one adds a hallucination surface for nothing.

So extraction lives here: fetched, parsed, compared and formatted by tested code. A job's prompt
becomes "run this script", and the run's output is the script's stdout.

Extraction is CONFIDENCE-RANKED and honest about ambiguity:

  high    schema.org/JSON-LD `price`, `og:price:amount`, `itemprop="price"` — the page stating its
          own price in a machine-readable field
  medium  a site-specific container known to hold the buy-box price
  low     a bare currency match in visible text — reported, but flagged

A `--selector` regex pins the exact element when a site needs it. When the best candidate is only
low-confidence, the LOG line says so rather than presenting a guess as a measurement; with
--require-confidence it refuses to alert at all.

**Amazon specifically**: the page comes in variants, and which one you get is not under your
control. `corePrice_feature_div` ships EMPTY on the common variant (the buy box is written in by
JavaScript), leaving only the "New (N) from $X" marketplace offer — a real number, but not the buy
box, so it is labelled `amazon-offer-listing` at LOW confidence. On other fetches of the exact same
URL, Amazon serves a variant that DOES embed `"price"` and `"priceAmount"` as JSON, which reads
high-confidence. Measured on this host, both variants agreed on the value, and the model that
originally "read" this page had reported a third number that appears nowhere in either.

Hence `best_candidates()`: retry a few times and take a high-confidence variant if one shows up,
otherwise report the offer-listing price WITH its caveat. Never guess which one you got.

Usage:
  price_watch.py --url URL --state NAME --below 50 --alert-to ohmz
  price_watch.py --url URL --state NAME --below 50 --alert-to ohmz --selector 'id="x".{0,200}?\\$([\\d.]+)'
  price_watch.py --selftest
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

STATE_DIR = os.path.expanduser("~/.hermes/monitor-state")
TIMEOUT = 30
# Plain urllib, no spoofed UA. Counter-intuitive and verified repeatedly: amazon.ca serves the full
# page to a bare Python-urllib request and a 3.8 KB robot wall to a fake Chrome UA. Do not "fix"
# this by adding a browser User-Agent — that is the one thing guaranteed to break it.
HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-CA,en;q=0.9"}

MONEY = r"([0-9][0-9,]*\.[0-9]{2})"


class Blocked(Exception):
    """The site served a robot wall instead of the page. Not a transient failure — retrying on the
    same schedule will keep hitting it, so it needs its own message and its own advice."""


class Gone(Exception):
    """The URL looks permanently broken (4xx, or a host that no longer resolves)."""


def classify_error(e):
    """('gone'|'blocked'|'transient', short human reason).

    A dead amazon.ca product returns HTTP **500**, not 404 — measured — so status code alone cannot
    decide this. 4xx and an unresolvable host are treated as permanent; everything else (5xx,
    timeouts, resets) is transient and gets more patience before the user is told anything.
    """
    if isinstance(e, Blocked):
        return "blocked", "the site is blocking automated checks"
    if isinstance(e, urllib.error.HTTPError):
        if 400 <= e.code < 500:
            return "gone", f"HTTP {e.code}"
        return "transient", f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = str(getattr(e, "reason", e))
        if "not known" in reason or "Name or service" in reason or "No address" in reason:
            return "gone", "the address no longer resolves"
        return "transient", reason[:60]
    return "transient", f"{type(e).__name__}: {str(e)[:50]}"


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def page_title(html):
    m = TITLE_RE.search(html or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    html = raw.decode("utf-8", "replace")
    if len(html) < 20000 and re.search(r"captcha|not a robot|automated access", html, re.I):
        raise Blocked("the site served a robot wall instead of the page")
    return html


def candidates(html):
    """[(price, source_label, confidence)] — every plausible price, best first."""
    out = []

    def add(vals, label, conf):
        for v in vals:
            try:
                out.append((float(v.replace(",", "")), label, conf))
            except ValueError:
                pass

    add(re.findall(r'"price"\s*:\s*"?' + MONEY + r'"?', html), "json-ld/price", "high")
    add(re.findall(r'"priceAmount"\s*:\s*' + MONEY, html), "priceAmount", "high")
    add(re.findall(r'property="(?:og:|product:)price:amount"\s+content="' + MONEY + r'"', html),
        "og:price", "high")
    add(re.findall(r'itemprop="price"[^>]*content="' + MONEY + r'"', html), "itemprop", "high")
    # Site-specific: books.toscrape puts the price in a known class.
    add(re.findall(r'class="price_color">\s*[£$€]\s*' + MONEY, html), "price_color", "high")
    # Amazon: on the variant that omits the JSON price, the marketplace offer is all that is
    # statically readable. It is a real price but NOT the buy box, so it can only ever be `low`.
    if "a-offscreen" in html:
        m = re.search(r'New\s*\(\d+\)\s*from.{0,400}?a-offscreen"[^>]*>\s*[£$€]?\s*' + MONEY,
                      html, re.S)
        if m:
            add([m.group(1)], "amazon-offer-listing", "low")
        else:
            add(re.findall(r'a-offscreen"[^>]*>\s*[£$€]?\s*' + MONEY, html)[:1],
                "amazon-a-offscreen", "low")
    if not out:   # last resort: visible text
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        add(re.findall(r"[£$€]\s?" + MONEY, text)[:1], "visible-text", "low")

    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda c: rank[c[2]])
    return out


# Both extractors put confidence LAST in their tuples, which is the one structural rule that lets
# best_candidates() and its early exit be shared between a price (a float) and an availability
# state (an enum). candidates() above keeps its own local copy of this: it is the most-bitten code
# in the file and is deliberately left untouched.
CONF_RANK = {"high": 0, "medium": 1, "low": 2}

# The availability vocabulary, and it is CLOSED. Keys are schema.org ItemAvailability values plus
# the product-feed spellings, normalised to letters only, so "InStock", "in stock", "IN_STOCK" and
# "https://schema.org/InStock" are all recognised as the same claim.
AVAIL_TOKENS = {
    "instock": "in_stock", "instoreonly": "in_stock", "onlineonly": "in_stock",
    "limitedavailability": "limited",
    "outofstock": "out_of_stock", "soldout": "out_of_stock", "discontinued": "out_of_stock",
    "oos": "out_of_stock",
    "preorder": "preorder", "presale": "preorder",
    "backorder": "backorder", "backordered": "backorder",
}
AVAILABLE = ("in_stock", "limited")
UNAVAILABLE = ("out_of_stock",)
# A pre-order button is not a restock, and firing back_in_stock on PreOrder is exactly the alert
# that teaches someone to ignore the channel. These fire nothing by default — but they are LOGGED
# on every run, so the page's own claim is never hidden from the user.
PENDING = ("preorder", "backorder")


def _avail_token(raw):
    """A schema.org or feed availability word -> our state, or None.

    None, not a guess. "InStock" and "in stock" are the same claim, but a token this table has
    never seen is not a claim at all: returning None routes the page to the "loaded but no value
    found" path, which tells the user, instead of inventing an availability it never read.
    """
    return AVAIL_TOKENS.get(re.sub(r"[^a-z]", "", (raw or "").lower()))


# Tier 1 (high): the page states its own availability in a machine-readable field. Every attribute
# order gets its own BOUNDED pattern rather than one clever pattern with `.*?` — unbounded, across
# a 1.5 MB Amazon page, that matches the availability field of a recommendation carousel. `[^>]`
# keeps each match inside a single tag.
_SCHEMA = r"(?:https?://(?:www\.)?schema\.org/)?"
SCHEMA_AVAIL = re.compile(r'"availability"\s*:\s*"' + _SCHEMA + r'([A-Za-z_ ]{2,30})"', re.I)
SCHEMA_AVAIL_ID = re.compile(r'"availability"\s*:\s*\{[^{}]{0,160}?schema\.org/([A-Za-z]{2,30})',
                             re.I)
ITEMPROP_AVAIL = re.compile(r'itemprop="availability"[^>]{0,200}?(?:href|content)="' + _SCHEMA
                            + r'([A-Za-z_ ]{2,30})"', re.I)
ITEMPROP_AVAIL_REV = re.compile(r'(?:href|content)="' + _SCHEMA + r'([A-Za-z_ ]{2,30})"'
                                r'[^>]{0,200}?itemprop="availability"', re.I)
META_AVAIL = re.compile(r'property="(?:og:|product:)availability"[^>]{0,120}?'
                        r'content="([^"]{2,40})"', re.I)
META_AVAIL_REV = re.compile(r'content="([^"]{2,40})"[^>]{0,120}?'
                            r'property="(?:og:|product:)availability"', re.I)

# Tier 2 (medium): a site-specific container known to hold the availability box. This is the tier
# the docstring at the top of this file has always promised and never emitted.
AMAZON_AVAIL = re.compile(r'id="availability".{0,600}?>\s*([^<>{}]{3,120}?)\s*<', re.S | re.I)
SHOPIFY_AVAIL = re.compile(r'"available"\s*:\s*(true|false)', re.I)
# The gate is what makes the bare key above a KNOWN container rather than a hopeful grep:
# "available":true appears in unrelated JSON on plenty of non-Shopify pages.
SHOPIFY_TELL = re.compile(r"cdn\.shopify\.com|Shopify\.theme|shopify-section", re.I)
STOCK_STATUS = re.compile(r'"(?:stockStatus|inventoryStatus|availabilityStatus)"\s*:\s*'
                          r'"([A-Za-z_ -]{2,30})"', re.I)

# Tier 3 (low): visible text. Deliberately NOT included: bare "unavailable" and "not available".
# "Not available in your size" and "not available for pickup" sit on plenty of in-stock pages, and
# a phrase that broad turns the negatives-win rule below into a false out-of-stock generator.
TEXT_UNAVAILABLE = re.compile(
    r"sold\s*out|out\s*of\s*stock|currently\s+unavailable|temporarily\s+(?:out|unavailable)|"
    r"no\s+longer\s+available|notify\s+me\s+when\s+(?:it(?:'s)?\s+)?(?:back|available)|"
    r"email\s+me\s+when\s+(?:it(?:'s)?\s+)?available|join\s+the\s+wait\s*list", re.I)
TEXT_COUNT = re.compile(r"only\s+(\d{1,4})\s+left|(\d{1,4})\s+left\s+in\s+stock|"
                        r"(\d{1,4})\s+(?:units?\s+)?(?:left|remaining|in\s+stock|available)", re.I)
TEXT_AVAILABLE = re.compile(
    r"add\s+to\s+(?:cart|bag|basket)|buy\s+(?:it\s+)?now|in\s+stock|available\s+now|"
    r"pick\s*up\s+today|ships?\s+(?:today|tomorrow|within)", re.I)
TEXT_PENDING = re.compile(r"pre-?order|back-?order|coming\s+soon", re.I)


def _evidence(raw):
    """The page's own words, made safe to print and to put in a JSON payload.

    A phrase lifted off a page is untrusted input: it reaches stdout inside an ALERT_DATA line the
    channel renders, so fold whitespace (an embedded newline would forge a protocol line) and swap
    the markdown-link brackets, the same contract the rest of this file's output keeps.
    """
    return (re.sub(r"\s+", " ", str(raw or "")).strip()
            .replace("[", "(").replace("]", ")"))[:60]


def _classify_phrase(text):
    """A human phrase off a page -> (state, count, why). (None, None, None) if it says no such thing.

    Resolution order is negative -> count -> positive -> pending, and it is asymmetric on purpose.
    "Add to Cart" ships on nearly every retail page — disabled, inside a carousel, under "customers
    also bought" — so it is weak evidence about THIS item. "Currently unavailable" is almost never
    decoration.
    """
    t = re.sub(r"\s+", " ", text or "")
    if TEXT_UNAVAILABLE.search(t):
        return "out_of_stock", None, "unavailable"
    m = TEXT_COUNT.search(t)
    if m:
        n = int(next(g for g in m.groups() if g))
        return ("out_of_stock" if n == 0 else "limited"), n, "count"
    if TEXT_AVAILABLE.search(t):
        return "in_stock", None, "available"
    if TEXT_PENDING.search(t):
        return "preorder", None, "preorder"
    return None, None, None


def stock_candidates(html, want_count=False):
    """[(state, count, evidence, source_label, confidence)] — readable availability, best first.

    `evidence` is the phrase the page itself used, folded and capped: the "never report a state you
    didn't read" field, which the email shows so a doubtful reading can be checked against the page.
    Confidence stays LAST so best_candidates() can rank prices and states with the same code.

    `state` is one of in_stock | limited | out_of_stock | preorder | backorder. `count` is an int
    when the page stated one ("Only 3 left"), otherwise None, and it is reported at the confidence
    of the tier it was read from — never promoted.

    A page that states its availability in none of these ways yields NOTHING, and that is the
    important half of this function. The caller then takes the existing "loaded but no value found"
    path, which after EMPTY_ALERT_AFTER runs says so out loud. An unreadable page is never reported
    as out of stock: Amazon writes its buy box with JavaScript and may well write this box the same
    way, and "I could not read it" is a different statement from "it is gone".

    Conflicting signals resolve in DOCUMENT order within a tier, exactly as candidates() resolves
    competing prices. A page describing several products (a carousel, a bundle) conventionally puts
    the main one first; the bounded patterns above are what keep a carousel from being read at all
    in the common case.
    """
    out = []

    def add(raw, label, conf, count=None):
        st = _avail_token(raw)
        if st:
            out.append((st, count, _evidence(raw), label, conf))

    for rx, label in ((SCHEMA_AVAIL, "json-ld/availability"),
                      (SCHEMA_AVAIL_ID, "json-ld/availability"),
                      (ITEMPROP_AVAIL, "itemprop-availability"),
                      (ITEMPROP_AVAIL_REV, "itemprop-availability"),
                      (META_AVAIL, "og:availability"),
                      (META_AVAIL_REV, "og:availability")):
        for raw in rx.findall(html or ""):
            add(raw, label, "high")

    m = AMAZON_AVAIL.search(html or "")
    if m:
        # medium, not high: this IS Amazon's real availability container, and it is also the one
        # Amazon sometimes ships empty and rotates — the same honesty amazon-offer-listing gets.
        st, n, _ = _classify_phrase(m.group(1))
        if st:
            out.append((st, n, _evidence(m.group(1)), "amazon-availability", "medium"))
    if SHOPIFY_TELL.search(html or ""):
        m = SHOPIFY_AVAIL.search(html)
        if m:
            out.append(("in_stock" if m.group(1).lower() == "true" else "out_of_stock",
                        None, _evidence(f'"available": {m.group(1)}'),
                        "shopify-available", "medium"))
    for raw in STOCK_STATUS.findall(html or ""):
        add(raw, "stock-status-json", "medium")

    # Last resort, and gated exactly as candidates() gates its own visible-text tier so the
    # expensive strip never runs on a page that answered properly. want_count runs it anyway,
    # because "only 3 left" is often the ONLY place a count appears.
    if not out or want_count:
        text = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        st, n, why = _classify_phrase(text)
        if st:
            rx = {"unavailable": TEXT_UNAVAILABLE, "count": TEXT_COUNT,
                  "available": TEXT_AVAILABLE, "preorder": TEXT_PENDING}[why]
            hit = rx.search(re.sub(r"\s+", " ", text))
            out.append((st, n, _evidence(hit.group(0) if hit else why),
                        "visible-text-" + why, "low"))

    out.sort(key=lambda c: CONF_RANK[c[-1]])
    return out


# What a source ID means to a person. "amazon-offer-listing" is precise and tells the user nothing;
# they will never read this file to find out what it refers to. The phrase has to survive being
# read once, on a lock screen, at 7am — so it says which number on the page was read, in the words
# the page itself uses.
CONFIDENCE_PHRASE = {
    "json-ld/price":        "high confidence",
    "priceAmount":          "high confidence",
    "og:price":             "high confidence",
    "itemprop":             "high confidence",
    "price_color":          "high confidence",
    "selector":             "high confidence",
    "amazon-offer-listing": "unconfirmed - read from the offer listing, not the main price",
    "amazon-a-offscreen":   "unconfirmed - read from a secondary price on the page",
    "visible-text":         "unconfirmed - read from page text, not a price field",
    # Availability sources. Same contract: `high` reads exactly "high confidence", and everything
    # else opens with "unconfirmed" and says " - read from <where>", because that tail becomes the
    # confidence_note on the SMS and the email.
    "json-ld/availability":     "high confidence",
    "itemprop-availability":    "high confidence",
    "og:availability":          "high confidence",
    "amazon-availability":      "unconfirmed - read from the site's own availability box, which "
                                "this page sometimes ships empty",
    "shopify-available":        "unconfirmed - read from the store's product data, not a "
                                "schema.org availability field",
    "stock-status-json":        "unconfirmed - read from a stock-status field, not a schema.org one",
    "visible-text-unavailable": "unconfirmed - read from page text, not an availability field",
    "visible-text-count":       "unconfirmed - read from page text, not an availability field",
    "visible-text-available":   "unconfirmed - read from page text, not an availability field",
    "visible-text-preorder":    "unconfirmed - read from page text, not an availability field",
}

# How an availability state reads to a person. "limited" says "in stock" and lets the count carry
# the scarcity, because "limited availability, 3 left" is the page's jargon plus a number.
STOCK_PHRASE = {"in_stock": "in stock", "limited": "in stock", "out_of_stock": "out of stock",
                "preorder": "pre-order", "backorder": "on back-order"}


def _stock_words(state, count):
    w = STOCK_PHRASE.get(state, str(state))
    return f"{w}, {count} left" if count is not None else w


def read_state(name):
    try:
        return json.load(open(os.path.join(STATE_DIR, f"{name}.json")))
    except Exception:
        return {}


def write_state(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, f"{name}.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def best_candidates(url, attempts=3, mode="price", want_count=False):
    """Fetch up to `attempts` times, stopping as soon as a high-confidence reading appears.

    Amazon rotates page variants: the SAME url and User-Agent returns a machine-readable
    `"price": 46.99` on some fetches and a JS-only buy box on others (measured — 1 of 6 fetches
    carried it, with no pattern in size or UA). One fetch that happens to land on the poor variant
    would report `low` on a page that could have answered properly. Retrying costs a second and
    turns an intermittent high-confidence source into a usually-available one.

    `mode` picks the extractor: a price (a float) or an availability state (an enum). Both put
    confidence LAST in their tuples, which is what lets this loop and its early exit be shared —
    a `medium` stock reading therefore costs the same three fetches an Amazon `low` price does.

    Returns (candidates, tries, title). The last error is raised only if EVERY attempt failed.
    """
    extract = (candidates if mode == "price"
               else lambda html: stock_candidates(html, want_count=want_count))
    err, best, title = None, [], None
    for i in range(attempts):
        try:
            html = fetch(url)
        except Exception as e:
            err = e
            if classify_error(e)[0] in ("gone", "blocked"):
                raise      # permanent: retrying the same dead URL three times proves nothing
            continue
        title = title or page_title(html)
        c = extract(html)
        if c and c[0][-1] == "high":
            return c, i + 1, title
        best = c
    if err is not None and not best:
        raise err
    return best, attempts, title


# How many consecutive failures before the user hears about it. A single failed check is a blip;
# telling someone at 3am that a page timed out once is how an alerting system gets muted. A URL
# that looks permanently dead earns less patience than one that merely timed out.
FAIL_ALERT_AFTER = {"gone": 2, "blocked": 2, "transient": 3}
# A page that loads but yields no price will NEVER fire again while looking perfectly healthy.
# That is the quiet failure worth naming.
EMPTY_ALERT_AFTER = 3

# Stock suppression, layer 1: how many MATCHING READINGS before a stock reading is acted on, keyed
# by how the page said it. The ladder is the one this file already uses for failures above — a
# machine-readable field is a measurement and acts at once, a site container gets the "looks real"
# patience of 2, and body text gets the "merely flaky" patience of 3. It counts matching readings
# rather than clock ticks: an unreadable run returns before the buffer, so it is evidence neither
# for nor against. Evidence improving can only ever SHORTEN the wait.
STOCK_CONFIRM = {"high": 1, "medium": 2, "low": 3}
# Layer 2: one stock alert per monitor per six hours. Six because it is this stack's own default
# check period for a watch, so it is one alert per natural period. A page that flips in and out of
# stock every five minutes runs 72 times inside one window; without this it sends 72 texts and the
# user mutes the channel, which is the product failure. It is a CEILING, not a heartbeat — a restock
# that simply persists stays quiet forever through the repeat dampener instead.
FLAP_COOLDOWN_S = 6 * 3600

# The four kinds that describe an availability, as opposed to a price.
STOCK_KINDS = ("back_in_stock", "out_of_stock", "inventory", "availability")


def _inventory_pred(st, n, below, above, prev_state, prev_count):
    if below is not None:
        return n is not None and n < below          # literally "only N left"
    if above is not None:
        return n is not None and n > above          # restocked past N
    # No threshold: the user's words were "tell me when the number changes", so this is the one
    # genuinely transition-shaped predicate here, and it is theirs rather than invented.
    return prev_count is not None and n != prev_count


# Firing in stock mode is a PREDICATE ON THE CURRENT READING, not a state machine. Brief rule 8 and
# this file's own suite ("FIRST run alerts — no 'wait for a baseline' rule") forbid inventing a
# transition requirement the user did not ask for: "tell me when it is in stock" is a state
# predicate exactly like "below 50", so a first confirmed reading fires. --kind SELECTS the
# predicate here, which is the fix for --kind having been applied after a numeric comparison.
STOCK_PREDICATES = {
    "back_in_stock": lambda st, n, below, above, ps, pn: st in AVAILABLE,
    "availability":  lambda st, n, below, above, ps, pn: st in AVAILABLE,
    "out_of_stock":  lambda st, n, below, above, ps, pn: st in UNAVAILABLE,
    "inventory":     _inventory_pred,
    "change":        lambda st, n, below, above, ps, pn: ps is not None and (st, n) != (ps, pn),
}


def emit(payload):
    """Print both alert forms: a plain sentence, then its structured payload.

    Both, deliberately. The structured line is what produces a well-written text and a laid-out
    email, but a watcher that predates it would understand nothing and the alert would vanish —
    the exact silent-loss failure this whole subsystem keeps being bitten by. The plain line
    guarantees delivery; the watcher suppresses it whenever it also understood the structured one.
    """
    import json as _json
    try:
        sentence = _tpl().render_sms(payload, limit=10_000)
    except Exception:
        sentence = f"{payload.get('kind', 'alert')} on {payload.get('url') or 'your monitor'}"
    print(f"ALERT({payload['to']}): {sentence}")
    print(f"ALERT_DATA: {_json.dumps(payload, sort_keys=True)}")


def run(a):
    mode = getattr(a, "mode", None) or "price"
    kind = a.kind
    # A stock --kind under price mode is the advertised-but-unimplemented bug this mode fixes: the
    # numeric comparison below used to decide first and --kind merely relabelled it, so
    # "--kind back_in_stock --below 50" fired on a PRICE and a watch with no threshold never fired
    # at all. Promote it and say so, rather than silently doing the wrong thing.
    promoted = mode == "price" and kind in STOCK_KINDS
    if promoted:
        mode = "stock"
    # --unit defaults to None, not "$", so it can be resolved per mode in one place: an inventory
    # reading of 3 rendered through money(3, "$") is a 34px "$3.00" in the email for "3 left".
    unit = a.unit if a.unit is not None else ("" if mode == "stock" else "$")
    state = read_state(a.state)

    # Bind the state to the URL it was recorded for.
    #
    # --state is a short name the agent picks, and it reuses them: this box already has two
    # different watches that were both called "tipping-the-velvet-price". A reused name inherits
    # the previous watch's `alerted_price`, and the repeat dampener below then reads the NEW
    # monitor's very first reading as "same as last time" and stays silent. The user is never told
    # — which is the single outcome this entire path exists to prevent, and it would look like a
    # working monitor the whole time.
    #
    # A different URL under the same name is a different watch, so its dampening means nothing.
    # State with no url predates this check: adopt it rather than resetting a live monitor.
    if state.get("url") and state["url"] != a.url:
        state = {}
    # And to the MODE it was recorded for, which the url binding cannot catch: same URL, same
    # --state name, different mode is "actually, forget the price, just tell me when it's back".
    # Without this the new stock watch inherits alerted_price and price, and in the other direction
    # a leftover stock_state is read by nothing while alerted_price silently dampens the first
    # price reading. State with no `mode` key predates this check: adopt it as a price watch rather
    # than resetting a live monitor, exactly as the url binding does.
    if state.get("mode", "price") != mode:
        state = {"url": a.url}
    state["url"], state["mode"] = a.url, mode

    label = a.label or state.get("item")
    base = {"to": a.alert_to, "item": label, "url": a.url, "unit": unit,
            "monitor": a.monitor, "schedule": a.schedule}

    # A count usually lives ONLY in body text ("Only 3 left"), so an inventory or thresholded watch
    # has to let the low tier run even when a machine-readable field already answered.
    want_count = mode == "stock" and (kind == "inventory" or a.below is not None
                                      or a.above is not None)
    try:
        cands, tries, title = best_candidates(a.url, mode=mode, want_count=want_count)
    except Exception as e:
        kind, why = classify_error(e)
        n = state.get("fail_streak", 0) + 1 if state.get("fail_kind") == kind else 1
        state["fail_streak"], state["fail_kind"] = n, kind
        state.pop("empty_streak", None)
        print(f"LOG: check failed ({why}) — attempt {n} in a row — {a.url.split('/')[2]}")
        # One alert per outage, on the run that confirms it. Silence afterwards until it recovers:
        # a broken URL that texts every 6 hours for a week trains the user to ignore the channel.
        if n == FAIL_ALERT_AFTER.get(kind, 3) and not state.get("fail_alerted"):
            state["fail_alerted"] = True
            emit(dict(base, kind="blocked" if kind == "blocked" else "unreachable", error=why))
        write_state(a.state, state)
        return 0

    if title and not a.label:
        label = _tpl().item_label(title) or label
        state["item"] = label
        base["item"] = label

    if not cands:
        n = state.get("empty_streak", 0) + 1
        state["empty_streak"] = n
        state.pop("fail_streak", None)
        print(f"LOG: page loaded but no value found (run {n} in a row) — {a.url.split('/')[2]}")
        if n == EMPTY_ALERT_AFTER and not state.get("empty_alerted"):
            state["empty_alerted"] = True
            emit(dict(base, kind="no_value"))
        write_state(a.state, state)
        return 0

    recovered = bool(state.get("fail_alerted") or state.get("empty_alerted"))
    for k in ("fail_streak", "fail_kind", "fail_alerted", "empty_streak", "empty_alerted"):
        state.pop(k, None)

    # Everything below DECIDES first and prints once. The old order printed the reading, then
    # printed the suppression reason as a SECOND `LOG:` line — and hermes_delivery reads only the
    # FIRST match, so that explanation has never once reached a human. Folding it into the one
    # delivered line is what makes all four suppressions legible, and it also moves `suppressed`
    # off the head of the branch chain, so a run that both recovers AND is suppressed now says so.
    notes = []
    held = False

    if mode == "stock":
        st, count, evidence, source, conf = cands[0]
        if kind == "inventory":
            # The thing being watched is the number, so prefer the best-confidence reading that
            # actually carries one — and report its real (often low) confidence as its own.
            st, count, evidence, source, conf = next((c for c in cands if c[1] is not None),
                                                     cands[0])
        prev_value, prev_state = state.get("stock_count"), state.get("stock_state")
        alerted = (state.get("alerted_stock"), state.get("alerted_count"))

        pending = (state.get("pending_state"), state.get("pending_count"))
        runs = state.get("pending_runs", 0) + 1 if pending == (st, count) else 1
        state["pending_state"], state["pending_count"], state["pending_runs"] = st, count, runs
        need = STOCK_CONFIRM.get(conf, 3)
        confirmed = runs >= need

        words = _stock_words(st, count)
        delta = "" if prev_state is None else (
            " (unchanged)" if (st, count) == (prev_state, prev_value)
            else f" (was {_stock_words(prev_state, prev_value)})")
        reading = f"{words}{delta}"
        value, prev = count, prev_value
        if not confirmed:
            notes.append(f"{runs} of {need} matching runs needed before acting")
        if st in PENDING:
            notes.append("a pre-order is not a restock, so nothing fires")

        if not kind:
            kind = "inventory" if (a.below is not None or a.above is not None) and \
                count is not None else "back_in_stock"
        pred = STOCK_PREDICATES.get(kind, STOCK_PREDICATES["change"])
        if kind != "inventory" and (a.below is not None or a.above is not None):
            # Honouring it silently is exactly how the old bug stayed invisible.
            notes.append("the number you gave is a price threshold, ignored on a stock watch")
        fires = bool(confirmed and st not in PENDING
                     and pred(st, count, a.below, a.above, prev_state, prev_value))
        if kind == "inventory" and a.below is not None:
            payload_target = a.below
        elif kind == "inventory" and a.above is not None:
            payload_target = a.above
        else:
            payload_target = None
        repeat = alerted != (None, None) and (st, count) == alerted
        if confirmed:
            state["stock_state"], state["stock_count"] = st, count
    else:
        price, source, conf = cands[0]
        evidence, value = None, price
        prev = alerted = state.get("price")
        prev, alerted = state.get("price"), state.get("alerted_price")
        delta = "" if prev is None else (" (unchanged)" if abs(price - prev) < 0.005
                                         else f" (was {prev:.2f})")
        reading = f"{price:.2f}{delta}"
        fires, kind, payload_target = False, None, None
        if a.below is not None and price < a.below:
            fires, kind, payload_target = True, "price_drop", a.below
        elif a.above is not None and price > a.above:
            fires, kind, payload_target = True, "price_rise", a.above
        if fires and a.kind:
            kind = a.kind
        # Dampening: never repeat an identical alert. The only suppression allowed here — the
        # condition itself is evaluated literally, including on the very first run.
        repeat = alerted is not None and abs(price - alerted) < 0.005
        state["price"] = price

    # Confidence is printed on EVERY run, including good ones. If it only appeared on doubtful
    # readings, its absence would have to be interpreted — and an omission would be
    # indistinguishable from a bug that stopped emitting it.
    phrase = CONFIDENCE_PHRASE.get(source) or f"{conf} confidence"
    conf_word = "high" if conf == "high" else "unconfirmed"
    note = None if conf == "high" else phrase.split(" - ", 1)[-1]
    payload = dict(base, value=value, prev=prev, confidence=conf_word, confidence_note=note)
    if payload_target is not None:
        payload["target"] = payload_target
    if mode == "stock":
        payload.update(state=st, prev_state=prev_state, state_text=evidence)

    suppressed = fires and conf != "high" and a.require_confidence
    if suppressed:
        notes.append(f"alert SUPPRESSED: only an unconfirmed reading ({source}) was readable, "
                     f"refusing to alert on a guess")
    elif fires and repeat:
        notes.append("already alerted on this exact reading")
    elif fires and mode == "stock" and state.get("stock_alerted_ts") is not None:
        # Only a monitor that HAS alerted can be holding one back. Defaulting the timestamp to 0
        # would make `now - 0` look like a recent alert and swallow the very first firing — true
        # only because a real epoch is large, which is not a property to depend on.
        since = time.time() - state["stock_alerted_ts"]
        held = since < FLAP_COOLDOWN_S
        if held:
            left = int(FLAP_COOLDOWN_S - since)
            notes.append(f"alert held: one stock alert per 6h, {left // 3600}h "
                         f"{left % 3600 // 60}m to go")
    if promoted:
        notes.append(f"read as a stock watch, not a price one (--kind {kind})")

    print("LOG: " + " — ".join([f"{reading} ({phrase})"] + notes + [a.url.split("/")[2]]))

    if fires and not repeat and not suppressed and not held:
        emit(dict(payload, kind=kind))
        if mode == "stock":
            state["alerted_stock"], state["alerted_count"] = st, count
            state["stock_alerted_ts"] = time.time()
        else:
            state["alerted_price"] = price
    elif recovered:
        # The monitor was broken and is not any more — say so, or the user is left wondering
        # whether it ever came back. Only when no real alert went out in the same run: two texts
        # seconds apart saying the same monitor works and also hit its target is noise, and the
        # target message already proves it works.
        emit(dict(payload, kind="recovered"))
    write_state(a.state, state)
    return 0


def _tpl():
    """alert_templates, imported lazily so a missing module degrades to the raw page title."""
    import importlib.util
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "alert_templates.py")
    spec = importlib.util.spec_from_file_location("alert_templates", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def selftest():
    ok = True
    cases = [
        ("books.toscrape (static, machine-readable)",
         "http://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html", "high"),
        # Amazon rotates variants: some fetches carry a machine-readable price, some do not.
        # Either outcome is correct behaviour, so this case asserts only that a price is found and
        # that whatever confidence is reported is the truth about that fetch.
        ("amazon.ca (variant-dependent: high when the JSON price is served, else the offer listing)",
         "https://www.amazon.ca/dp/B0DP6D3TRB", None),
    ]
    for label, url, expect_conf in cases:
        try:
            c, tries, _title = best_candidates(url)
            if c:
                p, src, conf = c[0]
                good = conf == expect_conf if expect_conf else True
                mark = "OK " if good else "?? "
                exp = f"expected {expect_conf}" if expect_conf else "any confidence, reported honestly"
                print(f"  {mark} {label}\n      -> {p:.2f} via {src} ({conf}) in {tries} fetch(es); {exp}")
                ok &= good
            else:
                print(f"  FAIL {label} -> no candidates"); ok = False
        except Exception as e:
            print(f"  FAIL {label} -> {e}"); ok = False
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--state", help="state file name under ~/.hermes/monitor-state/")
    ap.add_argument("--below", type=float)
    ap.add_argument("--above", type=float)
    ap.add_argument("--alert-to", default="ohmz")
    ap.add_argument("--selector", help="regex with one capture group, overrides all strategies")
    ap.add_argument("--kind", help="price_drop | price_rise | back_in_stock | fare | inventory | "
                                   "availability | threshold | change (picks the message wording)")
    ap.add_argument("--label", help="what to call the item; omit to read the page <title>")
    ap.add_argument("--mode", choices=("price", "stock"), default="price",
                    help="what to read off the page: a price, or an availability state. "
                         "--mode stock is REQUIRED for a stock/availability watch — without it "
                         "the run compares a PRICE against a threshold and a back-in-stock watch "
                         "never fires")
    # None, not "$": resolved per mode in run(), because a stock reading has no currency and
    # money(3, "$") would render "3 left" as a 34px "$3.00" in the email.
    ap.add_argument("--unit", default=None, help="currency symbol or code for display")
    ap.add_argument("--monitor", help="the monitor's name, used when no item label is available")
    ap.add_argument("--schedule", help="how often this runs, e.g. 'every 6h' (shown in the email)")
    ap.add_argument("--require-confidence", action="store_true",
                    help="refuse to alert on a low-confidence price")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.url and a.state):
        ap.error("--url and --state are required")
    if a.selector:
        global candidates
        pat = a.selector
        candidates = lambda h: [(float(m.replace(",", "")), "selector", "high")
                                for m in re.findall(pat, h, re.S)[:1]]
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

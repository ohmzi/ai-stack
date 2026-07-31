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
}


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


def best_candidates(url, attempts=3):
    """Fetch up to `attempts` times, stopping as soon as a high-confidence price appears.

    Amazon rotates page variants: the SAME url and User-Agent returns a machine-readable
    `"price": 46.99` on some fetches and a JS-only buy box on others (measured — 1 of 6 fetches
    carried it, with no pattern in size or UA). One fetch that happens to land on the poor variant
    would report `low` on a page that could have answered properly. Retrying costs a second and
    turns an intermittent high-confidence source into a usually-available one.

    Returns (candidates, tries, title). The last error is raised only if EVERY attempt failed.
    """
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
        c = candidates(html)
        if c and c[0][2] == "high":
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
    state["url"] = a.url

    label = a.label or state.get("item")
    base = {"to": a.alert_to, "item": label, "url": a.url, "unit": a.unit,
            "monitor": a.monitor, "schedule": a.schedule}

    try:
        cands, tries, title = best_candidates(a.url)
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

    price, source, conf = cands[0]
    recovered = bool(state.get("fail_alerted") or state.get("empty_alerted"))
    for k in ("fail_streak", "fail_kind", "fail_alerted", "empty_streak", "empty_alerted"):
        state.pop(k, None)

    prev, alerted = state.get("price"), state.get("alerted_price")
    delta = "" if prev is None else (" (unchanged)" if abs(price - prev) < 0.005
                                     else f" (was {prev:.2f})")
    # Confidence is printed on EVERY run, including good ones. If it only appeared on doubtful
    # readings, its absence would have to be interpreted — and an omission would be
    # indistinguishable from a bug that stopped emitting it.
    phrase = CONFIDENCE_PHRASE.get(source) or f"{conf} confidence"
    print(f"LOG: {price:.2f}{delta} ({phrase}) — {a.url.split('/')[2]}")

    conf_word = "high" if conf == "high" else "unconfirmed"
    note = None if conf == "high" else phrase.split(" - ", 1)[-1]
    payload = dict(base, value=price, prev=prev, confidence=conf_word, confidence_note=note)

    fires, kind = False, None
    if a.below is not None and price < a.below:
        fires, kind, payload["target"] = True, "price_drop", a.below
    elif a.above is not None and price > a.above:
        fires, kind, payload["target"] = True, "price_rise", a.above
    if fires and a.kind:
        kind = a.kind
    # Dampening: never repeat an identical alert. The only suppression allowed — the condition
    # itself is evaluated literally, including on the very first run.
    repeat = alerted is not None and abs(price - alerted) < 0.005
    suppressed = fires and conf != "high" and a.require_confidence
    if suppressed:
        print(f"LOG: alert SUPPRESSED — only an unconfirmed price ({source}) was readable; "
              f"refusing to alert on a guess")
    elif fires and not repeat:
        emit(dict(payload, kind=kind))
        state["alerted_price"] = price
    elif recovered:
        # The monitor was broken and is not any more — say so, or the user is left wondering
        # whether it ever came back. Only when no real alert went out in the same run: two texts
        # seconds apart saying the same monitor works and also hit its target is noise, and the
        # target message already proves it works.
        emit(dict(payload, kind="recovered"))
    state["price"] = price
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
    ap.add_argument("--unit", default="$", help="currency symbol or code for display")
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

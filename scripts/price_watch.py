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
import urllib.request

STATE_DIR = os.path.expanduser("~/.hermes/monitor-state")
TIMEOUT = 30
# Plain urllib, no spoofed UA. Counter-intuitive and verified repeatedly: amazon.ca serves the full
# page to a bare Python-urllib request and a 3.8 KB robot wall to a fake Chrome UA. Do not "fix"
# this by adding a browser User-Agent — that is the one thing guaranteed to break it.
HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-CA,en;q=0.9"}

MONEY = r"([0-9][0-9,]*\.[0-9]{2})"


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    html = raw.decode("utf-8", "replace")
    if len(html) < 20000 and re.search(r"captcha|not a robot|automated access", html, re.I):
        raise RuntimeError("blocked: the site served a robot wall instead of the page")
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

    Returns (candidates, tries). The last error is raised only if EVERY attempt failed.
    """
    err, best = None, []
    for i in range(attempts):
        try:
            c = candidates(fetch(url))
        except Exception as e:
            err = e
            continue
        if c and c[0][2] == "high":
            return c, i + 1
        best = c
    if err is not None and not best:
        raise err
    return best, attempts


def run(a):
    try:
        cands, tries = best_candidates(a.url)
    except Exception as e:
        print(f"LOG: fetch failed for {a.url} — {type(e).__name__}: {str(e)[:120]}")
        return 0        # exit 0: a reported failure is a successful RUN, not a crash
    if not cands:
        print(f"LOG: no price found on {a.url} (page fetched over {tries} attempt(s))")
        return 0

    price, source, conf = cands[0]
    state = read_state(a.state)
    prev, alerted = state.get("price"), state.get("alerted_price")
    delta = "" if prev is None else (" (unchanged)" if abs(price - prev) < 0.005
                                     else f" (was {prev:.2f})")
    # Parentheses, not square brackets: the LOG line is posted into an OpenWebUI channel and
    # rendered as markdown, where [...] is link syntax. Keep the caveat out of that grammar.
    #
    # (Record correction: the first two live runs showed no caveat and this was briefly blamed on
    # hermes stripping the text. It was not — those runs genuinely read `high` confidence, because
    # the cron venv is Python 3.11 and its urllib User-Agent draws the page variant that embeds the
    # JSON price, while the 3.12 shell here draws the one that does not. No output was ever
    # altered in transit. Left here because "delivery mangled it" was the wrong suspect twice.)
    # Confidence is printed on EVERY run, including good ones. If it only appeared on doubtful
    # readings, its absence would have to be interpreted — and an omission would be
    # indistinguishable from a bug that stopped emitting it. Always-present means a missing
    # confidence tag is itself the signal that something is wrong.
    note = f" ({CONFIDENCE_PHRASE.get(source) or conf + ' confidence'})"

    fires = a.below is not None and price < a.below
    # Dampening: never repeat an identical alert. This is the only suppression allowed — the
    # condition itself is evaluated literally, including on the very first run.
    repeat = alerted is not None and abs(price - alerted) < 0.005

    print(f"LOG: {price:.2f}{delta}{note} — {a.url.split('/')[2]}")
    if fires and not repeat:
        if conf == "low" and a.require_confidence:
            print(f"LOG: alert SUPPRESSED — only a {conf}-confidence price ({source}) was "
                  f"readable; refusing to alert on a guess")
        else:
            print(f"ALERT({a.alert_to}): {price:.2f}, under your {a.below:.2f} target"
                  f"{note} — {a.url}")
            state["alerted_price"] = price
    state["price"] = price
    write_state(a.state, state)
    return 0


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
            c, tries = best_candidates(url)
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

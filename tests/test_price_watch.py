#!/usr/bin/env python3
"""Deterministic price extraction: every rule pinned offline against fixture HTML.

Why this file exists. The thing this replaces was a 34B model writing a scraper at run time. It
failed three distinct ways in production — a crash in its own regex, an ALERT assigned to a variable
it never printed, and finally a run that abandoned the output protocol and emitted an INVENTED promo
code beside a price it had never read. Extraction is now code, and code gets pinned.

The rules that matter, each chosen because getting it wrong is silent:

  * **Confidence is not decoration.** A machine-readable `price` field and a bare `$` in body text
    are not the same claim. Ranking them, and SAYING which one was used, is the difference between
    a measurement and a guess wearing a measurement's clothes.
  * **Amazon's buy box is unreadable from static HTML** — `corePrice_feature_div` ships empty and is
    filled by JavaScript. The only statically-present price is the "New (N) from $X" marketplace
    offer. That must be labelled `low`, so an alert built on it can never claim to be the buy-box
    price. If a future Amazon change makes the buy box static, this test fails — correctly.
  * **First run alerts.** Suppressing "no previous value to compare" would silently swallow the
    very first target hit, which is the one the user is waiting for.
  * **Repeat alerts are dampened, changed ones are not.** A price sitting under target must not text
    every tick; a price that moves and is still under target must.
  * **A fetch failure is a reported outcome, not a crash** — exit 0 with a LOG line, so the watcher
    posts "couldn't reach the site" instead of the job dying silently.

Usage:  python3 tests/test_price_watch.py
"""
import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load():
    spec = importlib.util.spec_from_file_location(
        "pw", "/home/ohmz/ai-stack/scripts/price_watch.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Args:
    def __init__(self, **kw):
        self.url = "https://shop.example.com/item"
        self.state = "t"
        self.below = None
        self.above = None
        self.alert_to = "ohmz"
        self.selector = None
        self.require_confidence = False
        self.__dict__.update(kw)


# Real shapes, trimmed. The Amazon one reproduces what amazon.ca actually serves: an EMPTY
# corePrice_feature_div plus a populated "New (N) from" offer block.
FIX_JSONLD = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"39.99"}}</script>'
FIX_OG = '<meta property="og:price:amount" content="24.50" />'
FIX_BOOKS = '<p class="price_color">£51.77</p>'
FIX_AMAZON = ('<div id="corePrice_feature_div" class="celwidget"></div>'
              '<div id="olp"><span>New (4) from</span> <span class="a-price">'
              '<span class="a-offscreen">$46.99</span></span></div>'
              '<div class="carousel"><span class="a-offscreen">$129.00</span></div>')
FIX_TEXT_ONLY = '<html><body><p>Yours for only $12.34 today</p></body></html>'


def main():
    pw = load()
    tmp = tempfile.mkdtemp()
    pw.STATE_DIR = tmp

    print("--- machine-readable fields rank HIGH ---")
    for label, html, want, src in [
        ("JSON-LD offers.price", FIX_JSONLD, 39.99, "json-ld/price"),
        ("og:price:amount meta", FIX_OG, 24.50, "og:price"),
        ("itemprop=price", '<span itemprop="price" content="7.25">', 7.25, "itemprop"),
        ("books.toscrape price_color", FIX_BOOKS, 51.77, "price_color"),
    ]:
        c = pw.candidates(html)
        ok = c and abs(c[0][0] - want) < 0.005 and c[0][1] == src and c[0][2] == "high"
        check(f"{label} -> {want} / high", bool(ok), repr(c[:1]))

    print("--- Amazon: buy box is JS-rendered, so ONLY the offer listing is readable (low) ---")
    c = pw.candidates(FIX_AMAZON)
    check("picks the 'New (N) from' price, not the carousel",
          c and abs(c[0][0] - 46.99) < 0.005, repr(c[:1]))
    check("labelled amazon-offer-listing", c and c[0][1] == "amazon-offer-listing", repr(c[:1]))
    check("confidence is LOW — it is not the buy-box price", c and c[0][2] == "low", repr(c[:1]))
    check("empty corePrice_feature_div yields no high-confidence price",
          not any(x[2] == "high" for x in c), repr(c))

    print("--- bare body text is a last resort, and says so ---")
    c = pw.candidates(FIX_TEXT_ONLY)
    check("finds 12.34", c and abs(c[0][0] - 12.34) < 0.005, repr(c[:1]))
    check("flagged low / visible-text", c and c[0][2] == "low" and c[0][1] == "visible-text", repr(c[:1]))
    check("a page with no price at all -> no candidates", pw.candidates("<p>sold out</p>") == [])

    print("--- ranking: a high-confidence field always beats a low one on the same page ---")
    c = pw.candidates(FIX_AMAZON + FIX_JSONLD)
    check("json-ld wins over a-offscreen", c[0][1] == "json-ld/price" and c[0][2] == "high", repr(c[:1]))

    print("--- thresholds, first run, and dampening ---")
    pw.fetch = lambda url: FIX_JSONLD          # 39.99

    def run(**kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pw.run(Args(**kw))
        return buf.getvalue()

    out = run(state="s1", below=50)
    check("FIRST run alerts (no 'wait for a baseline' rule)", "ALERT(ohmz):" in out, out)
    check("LOG line always present", out.startswith("LOG: 39.99"), out)
    out = run(state="s1", below=50)
    check("identical repeat is dampened", "ALERT(" not in out, out)
    check("but still logs, marked unchanged", "(unchanged)" in out, out)

    pw.fetch = lambda url: '<meta property="og:price:amount" content="35.00" />'
    out = run(state="s1", below=50)
    check("a CHANGED price under target alerts again", "ALERT(ohmz):" in out, out)
    check("delta shows the previous value", "(was 39.99)" in out, out)

    out = run(state="s2", below=10)
    check("above the target -> no alert", "ALERT(" not in out, out)
    check("...but the reading is still logged", "LOG: 35.00" in out, out)

    print("--- low confidence: reported by default, refused under --require-confidence ---")
    pw.fetch = lambda url: FIX_AMAZON
    out = run(state="s3", below=50)
    check("alerts, but the message carries the caveat",
          "ALERT(ohmz):" in out and "unconfirmed" in out, out)

    print("--- confidence is stated on GOOD runs too, never merely omitted ---")
    # If the tag only appeared on doubtful readings, a run that stopped emitting it would be
    # indistinguishable from a clean measurement. Always-present makes its absence a signal.
    pw.fetch = lambda url: FIX_JSONLD
    good = run(state="s8", below=50)
    check("a high-confidence LOG says so explicitly", "(high confidence)" in good, good)
    check("...and so does its ALERT line",
          "(high confidence)" in [l for l in good.splitlines() if l.startswith("ALERT")][0], good)
    pw.fetch = lambda url: FIX_AMAZON   # restore: the checks below need the low-confidence page
    out = run(state="s4", below=50, require_confidence=True)
    check("--require-confidence suppresses it", "ALERT(" not in out, out)
    check("...loudly, with the reason", "SUPPRESSED" in out and "guess" in out, out)

    print("--- a fetch failure is a reported outcome, not a crash ---")
    def boom(url):
        raise OSError("Name or service not known")
    pw.fetch = boom
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pw.run(Args(state="s5", below=50))
    check("exit code 0 (the RUN succeeded; the fetch didn't)", rc == 0)
    check("LOG explains the failure", buf.getvalue().startswith("LOG: fetch failed"), buf.getvalue())
    check("no ALERT invented from a failed fetch", "ALERT(" not in buf.getvalue())

    print("--- the caveat is markdown-safe (LOG lines are rendered in an OpenWebUI channel) ---")
    # LOG lines are posted to a channel that renders markdown — the live post came back as
    # "46.99 — [www.amazon.ca](https://www.amazon.ca)" after auto-linking. A caveat written with
    # square brackets sits inside markdown link grammar, so it is written with parentheses instead.
    # The caveat existing AT ALL is the point: a low-confidence price that arrives looking like a
    # measurement is the exact failure this whole script was built to stop.
    pw.fetch = lambda url: FIX_AMAZON
    out = run(state="s7", below=50)
    check("no markdown link grammar in the output", "[" not in out and "]" not in out, out)
    check("the caveat is still there, in parentheses",
          "(unconfirmed - read from the offer listing" in out, out)
    check("and it survives into the ALERT line too",
          "unconfirmed" in [l for l in out.splitlines() if l.startswith("ALERT")][0], out)

    print("--- the source ID is translated into something a person can read ---")
    # "amazon-offer-listing" is precise and tells the user nothing; they will never open this file
    # to find out what it refers to. The caveat has to survive being read once, on a lock screen.
    for src in ("amazon-offer-listing", "amazon-a-offscreen", "visible-text"):
        phrase = pw.CONFIDENCE_PHRASE[src]
        check(f"{src!r} has a plain-English phrase", phrase.startswith("unconfirmed"), phrase)
        check(f"...that says which number was read ({src})", " - read from" in phrase, phrase)
    for src in ("json-ld/price", "priceAmount", "og:price", "itemprop", "price_color", "selector"):
        check(f"{src!r} reads as high confidence",
              pw.CONFIDENCE_PHRASE[src] == "high confidence")
    check("every source the extractor can emit has a phrase",
          all(s in pw.CONFIDENCE_PHRASE
              for s in ("json-ld/price", "priceAmount", "og:price", "itemprop", "price_color",
                        "amazon-offer-listing", "amazon-a-offscreen", "visible-text", "selector")))

    print("--- alert text is parseable by the delivery watcher ---")
    spec = importlib.util.spec_from_file_location(
        "hd", "/home/ohmz/ai-stack/scripts/hermes_delivery.py")
    hd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hd)
    pw.fetch = lambda url: FIX_JSONLD
    out = run(state="s6", below=50)
    alerts = hd.ALERT_RE.findall("## Response\n" + out)
    check("watcher's ALERT_RE matches what price_watch emits", len(alerts) == 1, repr(alerts))
    check("recipient parsed as the handle", alerts and alerts[0][0] == "ohmz", repr(alerts))
    logs = hd.LOG_RE.findall("## Response\n" + out)
    check("watcher's LOG_RE matches too", len(logs) == 1, repr(logs))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

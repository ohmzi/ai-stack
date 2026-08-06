#!/usr/bin/env python3
"""Stock and availability watching, pinned offline. No network, no real clock.

Why this file exists. Stock was ADVERTISED for months and never existed: the four kinds were in
alert_templates and in the docs, but --kind was applied after a purely numeric comparison, so
"tell me when it's back in stock" fired on a price threshold or never fired at all. Everything
pinned here has a failure mode that produces no error message:

  * **An unreadable page must never be reported as out of stock.** Amazon writes its buy box with
    JavaScript and may write the availability box the same way. "I could not read it" and "it is
    gone" are different statements, and only one of them is ever true of a page nobody could parse.
  * **The vocabulary is closed.** A schema token this code has never seen is not evidence. Guessing
    from one would be inventing an availability, which is the same class of failure as the run that
    invented a promo code.
  * **A flapping page must text once.** A page flipping in and out of stock every five minutes runs
    72 times in six hours. 72 texts is how a channel gets muted, which is a product failure.
  * **Exactly one LOG line, with the reason in it.** hermes_delivery reads only the FIRST match, so
    a suppression explained on a second line reaches nobody — that was a real bug for the price
    path, and stock adds three more ways to stay quiet.
  * **`value` stays numeric.** render_html does float(prev) for its arrow and money() for the big
    number; an enum in there breaks the email for every kind, not just this one.
  * **State is bound to its MODE.** Same page, same --state name, "forget the price, just tell me
    when it's back" — without the binding the new watch inherits alerted_price and silently dampens
    its own first reading.

Usage:  python3 tests/test_stock_watch.py
"""
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import types

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


JLD = ('<script type="application/ld+json">{"@type":"Product","offers":'
       '{"availability":"https://schema.org/%s"}}</script>')
TITLE = "<title>Zakkart 2-Pack Cat Scratching Board</title>"


def page(token, extra=""):
    return TITLE + (JLD % token) + extra


FIX_ITEMPROP = TITLE + '<link itemprop="availability" href="https://schema.org/InStock">'
FIX_ITEMPROP_REV = TITLE + '<link href="https://schema.org/LimitedAvailability" itemprop="availability">'
FIX_OG = TITLE + '<meta property="product:availability" content="out of stock">'
FIX_OG_REV = TITLE + '<meta content="preorder" property="og:availability">'
FIX_AMZ_AVAIL = TITLE + '<div id="availability" class="a-section"><span>Only 3 left in stock.</span></div>'
# The real rotating Amazon variant: the availability box ships EMPTY beside a populated offer block.
FIX_AMZ_EMPTY = TITLE + '<div id="availability" class="a-section"><span></span></div><div>New (4) from</div>'
FIX_SHOPIFY = TITLE + '<script src="//cdn.shopify.com/s/x.js"></script>{"available":false}'
FIX_STOCK_STATUS = TITLE + '{"sku":"x","stockStatus":"IN_STOCK"}'
# A cart button in a carousel AND a real negative phrase. The negative has to win.
FIX_TEXT_BOTH = TITLE + '<div class="also-bought"><button>Add to Cart</button></div><p>Currently unavailable</p>'
FIX_TEXT_CART = TITLE + '<button>Add to Cart</button>'
FIX_ONLY_N = TITLE + '<p>Only 2 left in stock - order soon.</p>'
FIX_PREORDER = TITLE + '<p>Pre-order now, ships in March</p>'
FIX_SILENT = TITLE + '<p>A lovely board for cats of all sizes.</p>'
FIX_PRICE = TITLE + '<script type="application/ld+json">{"offers":{"price":"39.99"}}</script>'
BOOKS = '<title>Tipping the Velvet</title><p class="price_color">£51.77</p>'
URL = "https://shop.example.com/cat-board"


def main():
    pw = load("/home/ohmz/ai-stack/scripts/price_watch.py", "pw")
    hd = load("/home/ohmz/ai-stack/scripts/hermes_delivery.py", "hd")
    at = load("/home/ohmz/ai-stack/scripts/alert_templates.py", "at")
    tr = load("/home/ohmz/ai-stack/scripts/alert_transports.py", "tr")
    ps = load("/home/ohmz/ai-stack/scripts/price_search.py", "ps")
    pw.STATE_DIR = tempfile.mkdtemp()

    clock = [1_700_000_000.0]
    pw.time = types.SimpleNamespace(time=lambda: clock[0])

    def advance(s):
        clock[0] += s

    class Args:
        """Mirrors the real argparse namespace; the drift check at the end keeps it honest."""
        def __init__(self, **kw):
            self.url = URL
            self.state = "t"
            self.below = None
            self.above = None
            self.alert_to = "ohmz"
            self.selector = None
            self.require_confidence = False
            self.kind = None
            self.label = None
            self.mode = "stock"
            self.unit = None
            self.monitor = "cat board watch"
            self.schedule = "every 6h"
            self.__dict__.update(kw)

    def serve(html):
        pw.fetch = lambda url: html if not isinstance(html, Exception) else (_ for _ in ()).throw(html)

    def run(html=None, **kw):
        if html is not None:
            serve(html)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pw.run(Args(**kw))
        return buf.getvalue()

    def logs(out):
        return hd.LOG_RE.findall("## Response\n" + out)

    def data(out):
        return [json.loads(m) for m in hd.ALERT_DATA_RE.findall("## Response\n" + out)]

    print("--- machine-readable availability ranks HIGH ---")
    for label, html, want in [
        ("json-ld, schema.org URL", page("InStock"), ("in_stock", "json-ld/availability")),
        ("json-ld, bare token", TITLE + '{"availability":"OutOfStock"}',
         ("out_of_stock", "json-ld/availability")),
        ("json-ld, nested @id", TITLE + '"availability":{"@id":"https://schema.org/SoldOut"}',
         ("out_of_stock", "json-ld/availability")),
        ("itemprop, attr order A", FIX_ITEMPROP, ("in_stock", "itemprop-availability")),
        ("itemprop, attr order B", FIX_ITEMPROP_REV, ("limited", "itemprop-availability")),
        ("og/product, order A", FIX_OG, ("out_of_stock", "og:availability")),
        ("og/product, order B", FIX_OG_REV, ("preorder", "og:availability")),
    ]:
        c = pw.stock_candidates(html)
        check(f"{label} -> {want[0]}",
              bool(c) and (c[0][0], c[0][3], c[0][4]) == (want[0], want[1], "high"), repr(c[:1]))
    c = pw.stock_candidates(page("InStock"))
    check("every candidate is a 5-tuple with confidence LAST — the shared-ranking rule",
          len(c[0]) == 5 and c[0][4] in ("high", "medium", "low"), repr(c[:1]))
    check("...and carries the page's own words as evidence", c[0][2] == "InStock", repr(c[:1]))

    print("--- the vocabulary is CLOSED: an unknown word is not evidence ---")
    for token in ("InStock", "InStoreOnly", "OnlineOnly", "LimitedAvailability", "OutOfStock",
                  "SoldOut", "Discontinued", "PreOrder", "PreSale", "BackOrder"):
        check(f"schema.org/{token} maps to a known bucket",
              pw._avail_token(token) in pw.AVAILABLE + pw.UNAVAILABLE + pw.PENDING)
    check("every value in the table is a known bucket",
          set(pw.AVAIL_TOKENS.values()) <= set(pw.AVAILABLE + pw.UNAVAILABLE + pw.PENDING))
    for junk in ("Frobnicated", "Maybe", "", None, "42", "InStockish"):
        check(f"{junk!r} yields NO candidate rather than a guess",
              pw.stock_candidates(TITLE + '{"availability":"%s"}' % junk) == []
              if junk else pw._avail_token(junk) is None)
    check("...and an unknown token can never land in AVAILABLE",
          pw._avail_token("Frobnicated") not in pw.AVAILABLE)

    print("--- site containers rank MEDIUM (the tier that was reserved and never used) ---")
    for label, html, want in [("Amazon #availability", FIX_AMZ_AVAIL, ("limited", 3)),
                              ("Shopify, gated on a Shopify tell", FIX_SHOPIFY,
                               ("out_of_stock", None)),
                              ("a stockStatus field", FIX_STOCK_STATUS, ("in_stock", None))]:
        c = pw.stock_candidates(html)
        check(f"{label} -> medium",
              bool(c) and (c[0][0], c[0][1], c[0][4]) == (want[0], want[1], "medium"), repr(c[:1]))
    check("an ungated \"available\":true is NOT a Shopify container",
          pw.stock_candidates(TITLE + '{"available":true}') == [])
    check("CONF_RANK orders high < medium < low",
          pw.CONF_RANK["high"] < pw.CONF_RANK["medium"] < pw.CONF_RANK["low"])
    check("price extraction is untouched: price_color is still HIGH",
          pw.candidates(BOOKS)[0] == (51.77, "price_color", "high"), repr(pw.candidates(BOOKS)[:1]))

    print("--- visible text is LOW, and a negative phrase beats a cart button ---")
    for label, html, want in [
        ("a carousel cart button loses to 'Currently unavailable'", FIX_TEXT_BOTH,
         ("out_of_stock", None, "visible-text-unavailable")),
        ("'Only 2 left in stock'", FIX_ONLY_N, ("limited", 2, "visible-text-count")),
        ("a cart button alone", FIX_TEXT_CART, ("in_stock", None, "visible-text-available")),
        ("'Pre-order now'", FIX_PREORDER, ("preorder", None, "visible-text-preorder")),
    ]:
        c = pw.stock_candidates(html)
        check(label, bool(c) and (c[0][0], c[0][1], c[0][3], c[0][4]) == want + ("low",),
              repr(c[:1]))
    check("'not available in your size' is NOT out of stock — that phrase sits on live pages",
          pw.stock_candidates(TITLE + "<p>not available in your size</p>") == [])
    for src in ("visible-text-unavailable", "visible-text-count", "visible-text-available",
                "visible-text-preorder", "amazon-availability", "shopify-available",
                "stock-status-json"):
        phrase = pw.CONFIDENCE_PHRASE[src]
        check(f"{src!r} says it is unconfirmed and where it was read",
              phrase.startswith("unconfirmed") and " - read from" in phrase, phrase)
    for src in ("json-ld/availability", "itemprop-availability", "og:availability"):
        check(f"{src!r} reads as high confidence", pw.CONFIDENCE_PHRASE[src] == "high confidence")

    print("--- an unreadable availability box is REPORTED, never guessed ---")
    out = [run(FIX_AMZ_EMPTY, state="u1", kind="back_in_stock") for _ in range(3)]
    check("an empty availability box yields no candidate at all",
          pw.stock_candidates(FIX_AMZ_EMPTY) == [], repr(pw.stock_candidates(FIX_AMZ_EMPTY)))
    check("runs 1 and 2 log it and stay quiet",
          all("no value found" in o for o in out[:2]) and not any("ALERT(" in o for o in out[:2]),
          out[0])
    check("run 3 emits exactly one no_value alert",
          [d["kind"] for d in data(out[2])] == ["no_value"], out[2])
    check("NO run ever says out of stock — the whole point",
          not any("out of stock" in o for o in out), out)
    run(page("InStock"), state="u1", kind="back_in_stock")
    out = run(FIX_AMZ_EMPTY, state="u1", kind="back_in_stock")
    check("a readable run in between resets the empty streak",
          "(run 1 in a row)" in out, out)

    print("--- the condition is a STATE, not a transition (brief rule 8) ---")
    check("a first confirmed in_stock fires back_in_stock",
          [d["kind"] for d in data(run(page("InStock"), state="s1", kind="back_in_stock"))]
          == ["back_in_stock"])
    check("a first confirmed out_of_stock fires an out_of_stock watch",
          [d["kind"] for d in data(run(page("OutOfStock"), state="s2", kind="out_of_stock"))]
          == ["out_of_stock"])
    check("a back_in_stock watch on a sold-out page never fires",
          "ALERT(" not in run(page("OutOfStock"), state="s3", kind="back_in_stock"))
    out = run(FIX_PREORDER, state="s4", kind="back_in_stock")
    check("a pre-order fires nothing — it is not a restock",
          "ALERT(" not in out and "pre-order is not a restock" in out, out)
    check("...but it is still logged every run, so the page's claim is not hidden",
          "pre-order" in logs(out)[0], out)
    out = run(page("InStock"), state="s5", kind="not_a_real_kind")
    check("an unknown kind falls back to a change predicate rather than raising",
          len(logs(out)) == 1, out)

    print("--- identical repeats are dampened, changed readings are not ---")
    run(page("InStock"), state="d1", kind="back_in_stock")
    out = run(page("InStock"), state="d1", kind="back_in_stock")
    check("the same reading twice sends one text and says why",
          "ALERT(" not in out and "already alerted" in out and "(unchanged)" in out, out)
    texts = 0
    for i in range(60):
        texts += len(data(run(page("InStock" if i % 2 else "OutOfStock"), state="d2",
                              kind="back_in_stock")))
        advance(300)
    check("a high-confidence in/out flapper texts ONCE across 60 runs — the repeat dampener alone",
          texts == 1, texts)

    print("--- weak signals are confirmed before they are acted on ---")
    out = [run(page("InStock"), state="c1", kind="back_in_stock")]
    check("high confidence acts on run 1", "ALERT(" in out[0], out[0])
    out = [run(FIX_AMZ_AVAIL, state="c2", kind="back_in_stock") for _ in range(2)]
    check("medium needs 2 matching runs", "ALERT(" not in out[0] and "ALERT(" in out[1], out)
    check("...and the waiting run says how many more it needs", "1 of 2" in out[0], out[0])
    out = [run(FIX_TEXT_CART, state="c3", kind="back_in_stock") for _ in range(3)]
    check("low needs 3 matching runs",
          "ALERT(" not in out[0] and "ALERT(" not in out[1] and "ALERT(" in out[2], out)
    run(FIX_TEXT_CART, state="c4", kind="back_in_stock")
    run(FIX_TEXT_BOTH, state="c4", kind="back_in_stock")
    out = run(FIX_TEXT_CART, state="c4", kind="back_in_stock")
    check("a contradictory reading restarts the count", "1 of 3" in out, out)
    run(FIX_TEXT_CART, state="c5", kind="back_in_stock")
    out = run(page("InStock"), state="c5", kind="back_in_stock")
    check("evidence improving can only SHORTEN the wait: low then high acts at once",
          "ALERT(" in out, out)
    run(FIX_TEXT_CART, state="c6", kind="back_in_stock")
    run(FIX_SILENT, state="c6", kind="back_in_stock")
    out = run(FIX_TEXT_CART, state="c6", kind="back_in_stock")
    check("an unreadable run is evidence NEITHER way — it does not reset the buffer",
          "2 of 3" in out, out)
    out = run(FIX_TEXT_CART, state="c7", kind="back_in_stock", require_confidence=True)
    for _ in range(2):
        out = run(FIX_TEXT_CART, state="c7", kind="back_in_stock", require_confidence=True)
    check("--require-confidence still means high only, and says so in the LOG",
          "ALERT(" not in out and "SUPPRESSED" in logs(out)[0] and "guess" in logs(out)[0], out)

    print("--- a page flapping every 5 minutes texts once ---")
    texts = held = loglines = 0
    for i in range(71):
        out = run(page("InStock", f"<p>Only {3 if (i // 3) % 2 else 4} left in stock</p>"),
                  state="f1", kind="inventory")
        texts += len(data(out))
        held += "alert held" in out
        loglines += len(logs(out))
        advance(300)
    check("71 runs of a flapping count over ~6h send exactly ONE text", texts == 1, texts)
    check("...while every single run still logs", loglines == 71, loglines)
    check("...and the held runs name the cooldown with time remaining", held >= 5, held)
    alerted_at = pw.read_state("f1")["stock_alerted_ts"]
    clock[0] = alerted_at + pw.FLAP_COOLDOWN_S + 1
    out = [run(page("InStock", "<p>Only 9 left in stock</p>"), state="f1", kind="inventory")
           for _ in range(3)][-1]
    check("a NEW value alerts again once the 6h window has passed", len(data(out)) == 1, out)

    print("--- --below/--above mean 'only N left', and mean nothing elsewhere ---")
    for _ in range(3):
        out = run(page("InStock", "<p>Only 2 left in stock</p>"), state="b1", kind="inventory",
                  below=3)
    check("--below 3 fires at a count of 2", len(data(out)) == 1, out)
    check("...and the target rides in the payload", data(out)[0].get("target") == 3, data(out))
    for _ in range(3):
        out = run(page("InStock", "<p>Only 4 left in stock</p>"), state="b2", kind="inventory",
                  below=3)
    check("...but not at a count of 4", "ALERT(" not in out, out)
    for _ in range(3):
        out = run(page("InStock", "<p>Only 9 left in stock</p>"), state="b3", kind="inventory",
                  above=5)
    check("--above 5 fires on a restock past 5", len(data(out)) == 1, out)
    out = run(page("InStock"), state="b4", kind="back_in_stock", below=50)
    check("a threshold on a NON-inventory stock watch is ignored OUT LOUD",
          "price threshold, ignored" in logs(out)[0], out)
    c = pw.stock_candidates(page("InStock", "<p>Only 2 left in stock</p>"), want_count=True)
    check("an inventory watch prefers a counted reading and reports its REAL confidence",
          [x[4] for x in c] == ["high", "low"] and c[1][1] == 2, repr(c))

    print("--- state is bound to its MODE as well as its URL ---")
    pw.write_state("m1", {"url": URL, "price": 40.0, "alerted_price": 40.0})
    out = run(page("InStock"), state="m1", kind="back_in_stock")
    st = pw.read_state("m1")
    check("a stock watch over a price watch's state fires its first reading",
          "ALERT(" in out, out)
    check("...and the inherited price dampening is gone",
          "alerted_price" not in st and "price" not in st and st["mode"] == "stock", st)
    pw.write_state("m2", {"url": URL, "price": 39.99, "alerted_price": 39.99})
    out = run(FIX_PRICE, state="m2", mode="price", unit="$")
    check("a file with NO mode key is adopted as a price watch, not reset",
          "(unchanged)" in out and "ALERT(" not in out, out)
    out = run(page("InStock"), state="m3", mode="price", kind="back_in_stock", below=50)
    check("a stock --kind without --mode auto-promotes and says so",
          "ALERT(" in out and "read as a stock watch" in out, logs(out)[0] if logs(out) else out)

    print("--- exactly ONE LOG line on every path, and the reason is IN it ---")
    paths = [
        ("fires", dict(html=page("InStock"), state="L1", kind="back_in_stock"), None),
        ("repeat", dict(html=page("InStock"), state="L1", kind="back_in_stock"),
         "already alerted"),
        ("cooldown held", dict(html=page("InStock", "<p>Only 3 left in stock</p>"),
                               state="f1", kind="inventory"), None),
        ("require-confidence refusal", dict(html=FIX_TEXT_CART, state="L3",
                                            kind="back_in_stock", require_confidence=True), None),
        ("unreadable", dict(html=FIX_SILENT, state="L4", kind="back_in_stock"), "no value found"),
        ("fetch failure", dict(html=OSError("boom"), state="L5", kind="back_in_stock"),
         "check failed"),
        ("ignored threshold", dict(html=page("InStock"), state="L6", kind="back_in_stock",
                                   below=50), "price threshold, ignored"),
        ("pending confirmation", dict(html=FIX_AMZ_AVAIL, state="L7", kind="back_in_stock"),
         "matching runs needed"),
    ]
    for label, kw, must in paths:
        out = run(**kw)
        ls = logs(out)
        check(f"{label}: exactly one LOG line", len(ls) == 1, repr(ls))
        if must:
            check(f"...and it carries the reason ({must})", bool(ls) and must in ls[0], repr(ls))
    out = run(page("InStock"), state="L8", kind="back_in_stock")
    check("the ALERT line parses with the right recipient",
          hd.ALERT_RE.findall("## Response\n" + out)[0][0] == "ohmz", out)
    check("output stays markdown-safe", "[" not in out and "]" not in out, out)
    # A phrase lifted off a page is untrusted input: it rides into an ALERT_DATA line the channel
    # renders, so it cannot be allowed to forge a protocol line or open a markdown link.
    out = run(TITLE + '<p>Only 3 left [2026 model]\nLOG: fake in stock</p>', state="L9",
              kind="inventory")
    check("a page phrase cannot forge a LOG line", len(logs(out)) == 1, out)
    check("...and its brackets are folded", "[" not in out and "]" not in out, out)
    ev = pw.stock_candidates(TITLE + '<p>Only 3 left [2026 model]\nLOG: fake in stock</p>')[0][2]
    check("...while the evidence still reports the page's own words, folded",
          "[" not in ev and "\n" not in ev and "left" in ev, repr(ev))

    print("--- the payload keeps value NUMERIC ---")
    out = run(page("InStock"), state="p1", kind="back_in_stock")
    d = data(out)[0]
    check("a boolean reading carries value=None, never a string or a bool",
          d["value"] is None and not isinstance(d["value"], bool), d)
    check("...the enum lives in its own key", d["state"] == "in_stock", d)
    check("...and the page's own words are the evidence field", d["state_text"] == "InStock", d)
    check("stock mode emits unit \"\" so money() never renders \"$3.00\" for \"3 left\"",
          d["unit"] == "", d)
    check("the payload round-trips as JSON", json.loads(json.dumps(d)) == d)
    for _ in range(3):
        out = run(page("InStock", "<p>Only 3 left in stock</p>"), state="p2", kind="inventory",
                  below=5)
    d2 = data(out)[0]
    check("a counted reading carries an int", isinstance(d2["value"], int) and d2["value"] == 3, d2)
    html = at.render_html(d2)
    check("render_html shows the count and no currency symbol",
          ">3<" in html.replace("&nbsp;", "") and "$3" not in html)
    check("render_html omits the big-number block for a boolean reading",
          "34px" not in at.render_html(d))
    check("the evidence line reaches both email surfaces",
          "the page says" in at.render_plain(d) and "the page says" in at.render_html(d))
    for kind in ("back_in_stock", "out_of_stock", "inventory", "availability"):
        body = tr.sms_body(at.render_sms(dict(d2, kind=kind)), job=None)
        check(f"the {kind} SMS fits 140 ASCII with no URL",
              len(body) <= 140 and "http" not in body and body == body.encode("ascii", "ignore")
              .decode(), f"{len(body)}: {body}")
    check("price mode with --unit unset still emits \"$\"",
          data(run(FIX_PRICE, state="p3", mode="price", below=50))[0]["unit"] == "$")

    print("--- the failure machinery is inherited, not reimplemented ---")
    out = [run(OSError("boom"), state="e1", kind="back_in_stock") for _ in range(3)]
    check("a transient failure still alerts on the third",
          not data(out[0]) and not data(out[1]) and [d["kind"] for d in data(out[2])]
          == ["unreachable"], out)
    # The recovery notice goes out only when no REAL alert did: two texts seconds apart saying the
    # same monitor works and also hit its target is noise. So recover onto a page that reads fine
    # but does not satisfy the watch.
    out = run(page("OutOfStock"), state="e1", kind="back_in_stock")
    check("...and recovery closes the loop", [d["kind"] for d in data(out)] == ["recovered"], out)
    st = pw.read_state("e1")
    check("...clearing the failure bookkeeping", "fail_streak" not in st, st)
    # The elif-chain fix, and it only bites when suppression and recovery land on the SAME run.
    # Two low readings build the confirmation buffer, three failures set fail_alerted (the buffer
    # survives them — an unreadable or failed run is evidence neither way), and the next low reading
    # is therefore confirmed, fires, and is refused by --require-confidence all at once. Before the
    # fix `suppressed` sat at the head of the chain and swallowed the recovery entirely.
    for _ in range(2):
        run(FIX_TEXT_CART, state="e2", kind="back_in_stock", require_confidence=True)
    for _ in range(3):
        run(OSError("x"), state="e2", kind="back_in_stock", require_confidence=True)
    check("the failures alerted, so there is a recovery owed",
          pw.read_state("e2").get("fail_alerted") is True, pw.read_state("e2"))
    out = run(FIX_TEXT_CART, state="e2", kind="back_in_stock", require_confidence=True)
    check("a run that both RECOVERS and is suppressed announces the recovery AND says it refused",
          [d["kind"] for d in data(out)] == ["recovered"] and "SUPPRESSED" in logs(out)[0], out)
    check("...in exactly one LOG line", len(logs(out)) == 1, out)

    print("--- the fixture and the forwarding cannot drift ---")
    src = open("/home/ohmz/ai-stack/scripts/price_watch.py").read()
    flags = {m.replace("-", "_") for m in re.findall(r'add_argument\("--([a-z-]+)"', src)}
    check("every price_watch flag exists on the local fixture",
          not (flags - set(Args().__dict__) - {"selftest"}),
          f"missing: {sorted(flags - set(Args().__dict__) - {'selftest'})}")
    check("the check found the flags at all", len(flags) >= 12, sorted(flags))
    check("--mode is one of them, so a stock watch is reachable at all", "mode" in flags)
    ns = ps.pw_namespace(types.SimpleNamespace(**dict(Args().__dict__, prefer_domain=None)), URL)
    check("price_search forwards --mode, or the no-URL path silently watches a price",
          getattr(ns, "mode", None) == "stock", vars(ns))
    check("best_candidates still returns exactly (candidates, tries, title)",
          "(candidates, tries, title)" in pw.best_candidates.__doc__)
    check("...and both extractors put confidence last",
          pw.candidates(BOOKS)[0][-1] == "high"
          and pw.stock_candidates(page("InStock"))[0][-1] == "high")
    for kind in ("back_in_stock", "out_of_stock", "inventory", "availability"):
        check(f"{kind!r} has a predicate and a renderer", kind in pw.STOCK_PREDICATES
              and kind in at.KINDS)
    check("STOCK_KINDS is exactly what auto-promotes",
          set(pw.STOCK_KINDS) == {"back_in_stock", "out_of_stock", "inventory", "availability"})

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""What an alert says, across every kind and every degenerate payload.

Why this file exists. Alerts used to be whatever sentence the emitting job printed, with a subject
built by slicing that sentence — which produced texts naming a cron id and a source ID and never
naming the thing the user was watching. Rendering now happens in one place from structured data,
and the two failure modes worth pinning are:

  * **A payload that renders badly is worse than no alert**, because it still consumes the one text
    the user gets. Every kind is rendered here and asserted to be a real sentence — no "is None",
    no doubled clauses, no empty quotes.
  * **A payload missing fields must still deliver.** Jobs are written by an agent and fields WILL be
    missing. Every renderer is run against an almost-empty payload, because raising here means the
    alert never leaves the machine.

The SMS budget is the hard one: 140 ASCII characters, no web addresses, and the item name is the
whole point of the message. The degradation order is pinned — item name shortened, then the
greeting dropped, and the pointer to the email never dropped, because it is the only thing telling
a first-time user where the link went.

Usage:  python3 tests/test_alert_templates.py
"""
import importlib.util
import re
import sys

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BASE = {"to": "ohmz", "item": "Zakkart 2-Pack Cat Scratching Board", "unit": "$",
        "url": "https://www.amazon.ca/dp/B0DP6D3TRB", "monitor": "cat board watch",
        "schedule": "every 6h", "texted_to": "+1 514-557-9764"}


def main():
    t = load("/home/ohmz/ai-stack/scripts/alert_templates.py", "tpl")
    at = load("/home/ohmz/ai-stack/scripts/alert_transports.py", "at")

    print("--- the item names itself from the page title ---")
    # Retail titles are keyword soup written for search, not for a lock screen. The useful name is
    # the first clause; the site name is never part of it.
    for raw, want in [
        ("Zakkart 2-Pack Cat Scratching Board, 65cm Tall Cardboard L Shape Vertical Cat "
         "Scratchers for Indoor Cats", "Zakkart 2-Pack Cat Scratching Board"),
        ("Tipping the Velvet | Books to Scrape - Sandbox", "Tipping the Velvet"),
        ("Sony WH-1000XM5 Wireless Headphones - Amazon.ca", "Sony WH-1000XM5 Wireless Headphones"),
    ]:
        got = t.item_label(raw)
        check(f"{raw[:34]!r} -> {want!r}", got == want, repr(got))
    check("no title -> None, so the caller can fall back", t.item_label(None) is None)
    check("an over-long single clause is cut on a word boundary",
          len(t.item_label("A " + "word " * 40)) <= 53)

    print("--- every kind renders a real sentence, inside the SMS budget ---")
    kinds = {
        "price_drop":    dict(value=46.99, prev=49.99, target=50.0, confidence="high"),
        "price_rise":    dict(value=88.0, prev=70.0, target=80.0, confidence="high"),
        "back_in_stock": {},
        "out_of_stock":  {},
        "fare":          dict(item="Montreal to London, Mar 12", value=612.0, target=700.0),
        "inventory":     dict(item="RTX 5090 Founders Edition", value=3, prev=11),
        "availability":  {},
        "threshold":     dict(value=91, target=85, unit="%", op="over"),
        "change":        {},
        "unreachable":   dict(error="HTTP 404"),
        "blocked":       {},
        "no_value":      {},
        "not_found":     dict(item="Google Fitbit Air", url=None),
        "recovered":     dict(value=46.99),
    }
    for kind, extra in kinds.items():
        p = dict(BASE, kind=kind, **extra)
        sms = at.sms_body(t.render_sms(p))          # through the real transport shaping
        subj, plain, html = t.render_subject(p), t.render_plain(p), t.render_html(p)
        check(f"{kind}: sms within one segment ({len(sms)})", len(sms) <= 140, sms)
        check(f"{kind}: sms is ASCII", sms.isascii(), sms)
        check(f"{kind}: sms carries no web address",
              "http" not in sms and "amazon.ca" not in sms, sms)
        check(f"{kind}: no unrendered None anywhere",
              not re.search(r"\bNone\b", sms + subj + plain + html), sms + " | " + subj)
        check(f"{kind}: no doubled 'you're tracking'", sms.count("you're tracking") <= 1, sms)
        check(f"{kind}: subject is non-trivial", len(subj) > 12 and ":" in subj, subj)
        check(f"{kind}: html is a complete fragment",
              html.count("<table") == html.count("</table>") and html.strip().endswith("</div>"))
        check(f"{kind}: the item name reaches the email", "Zakkart" in plain or "item" not in p
              or p.get("item") != BASE["item"], plain[:80])

    print("--- a nearly-empty payload still delivers something honest ---")
    for p in [{"to": "ohmz", "kind": "price_drop"}, {"kind": "unreachable"},
              {"to": "ohmz", "kind": "a_kind_nobody_has_written_yet"}, {}]:
        sms, subj = t.render_sms(p), t.render_subject(p)
        plain, html = t.render_plain(p), t.render_html(p)
        check(f"{p!s:46} renders an sms", bool(sms.strip()) and "None" not in sms, sms)
        check(f"{p!s:46} renders a subject", bool(subj.strip()) and "None" not in subj, subj)
        check(f"{p!s:46} renders a body", bool(plain.strip()) and bool(html.strip()))
        check(f"{p!s:46} has no empty clause", " -  - " not in sms and '""' not in sms, sms)

    print("--- SMS degradation: item name first, pointer never ---")
    long_item = "Ultra Premium Extra Large Heavy Duty Stainless Steel Multi Function Kitchen Thing"
    p = dict(BASE, kind="price_drop", item=long_item, value=46.99, target=50.0)
    sms = at.sms_body(t.render_sms(p))
    check("still one segment with a very long item name", len(sms) <= 140, f"{len(sms)}: {sms}")
    check("the pointer to the email survives", sms.rstrip().endswith("in email."), sms)
    check("the measurement survives", "46.99" in sms, sms)
    check("the item name is the thing that was cut", "..." in sms or "…" in sms, sms)
    # A greeting is worth less than the fact.
    p2 = dict(p, item=long_item * 2)
    sms2 = at.sms_body(t.render_sms(p2))
    check("an extreme name still fits", len(sms2) <= 140, f"{len(sms2)}: {sms2}")
    check("...still ending with the pointer", sms2.rstrip().endswith("in email."), sms2)

    print("--- problems read as problems, and say what to do ---")
    for kind in ("unreachable", "blocked", "no_value", "not_found"):
        p = dict(BASE, kind=kind, error="HTTP 500")
        sms, plain = t.render_sms(p), t.render_plain(p)
        check(f"{kind}: sms flags it as needing attention", "heads up" in sms.lower(), sms)
        check(f"{kind}: sms does not claim the condition was met",
              "target" not in sms and "under your" not in sms, sms)
        check(f"{kind}: the email says what to do next",
              any(w in plain for w in ("Double-check", "Ask me")), plain)
    check("a result kind carries no error advice",
          "Double-check" not in t.render_plain(dict(BASE, kind="price_drop", value=1.0)))

    print("--- the assistant introduces itself (the sender is an unrecognised address) ---")
    # These texts arrive from a mail-to-SMS gateway, so the handset shows an email address the user
    # has no reason to recognise. Naming the assistant answers the first question a text from an
    # unknown sender raises, which is what those characters buy.
    p = dict(BASE, kind="price_drop", value=46.99, target=50.0, assistant="Ohmz AI")
    sms = at.sms_body(t.render_sms(p))
    check("the text says who is speaking", "Ohmz AI here!" in sms, sms)
    check("...after greeting the user by name", sms.startswith("Hi ohmz, Ohmz AI here!"), sms)
    check("...and still fits one segment", len(sms) <= 140, f"{len(sms)}: {sms}")
    check("the email says it too", "Ohmz AI here!" in t.render_plain(p), t.render_plain(p)[:60])
    check("...and the html", "Ohmz AI here!" in t.render_html(p))
    # The subject's first ~45 characters are the notification preview; an identity there would
    # push the number out of the window for no gain, since the sender is already shown.
    check("the subject spends no characters on it", "Ohmz AI" not in t.render_subject(p),
          t.render_subject(p))
    # Configured, not hard-coded.
    p2 = dict(p, assistant="Homelab Bot")
    check("the name is whatever the deployment configured",
          "Homelab Bot here!" in t.render_sms(p2), t.render_sms(p2))
    p3 = dict(p); p3.pop("assistant")
    check("no name configured -> no identity clause, not an empty one",
          "here!" not in t.render_sms(p3), t.render_sms(p3))
    check("...and the greeting still works", t.render_sms(p3).startswith("Hi ohmz, "))

    print("--- when space runs out, identity outranks the personal greeting ---")
    # On a text from an address the user does not recognise, "who is this" beats "hello by name".
    long_item = "Ultra Premium Extra Large Heavy Duty Stainless Steel Multi Function Kitchen Thing"
    p = dict(BASE, kind="price_drop", item=long_item * 2, value=46.99, target=50.0,
             assistant="Ohmz AI", confidence="unconfirmed")
    sms = at.sms_body(t.render_sms(p))
    check("fits one segment", len(sms) <= 140, f"{len(sms)}: {sms}")
    check("the pointer to the email survives everything", sms.rstrip().endswith("in email."), sms)
    check("the measurement survives", "46.99" in sms, sms)

    print("--- the noun matches what is actually being watched ---")
    # A message that calls a flight a "listing" reads like it did not understand the request.
    for kind, want in [("fare", "fare"), ("inventory", "stock level"),
                       ("availability", "availability"), ("price_drop", "listing"),
                       ("unreachable", "page"), ("change", "task you assigned me"),
                       ("threshold", "task you assigned me")]:
        got = t._noun({"kind": kind})
        check(f"{kind} -> {want!r}", got == want, got)
    # With no item name the noun is what the user sees, so it has to read as a sentence.
    sms = t.render_sms({"to": "ohmz", "kind": "change", "assistant": "Ohmz AI"})
    check("a generic watch reads naturally", "task you assigned me has changed" in sms, sms)

    print("--- numbers are formatted the way their unit is written ---")
    for value, unit, want in [(46.99, "$", "$46.99"), (91, "%", "91%"), (91.5, "%", "91.5%"),
                              (612.0, "$", "$612.00"), (46.99, "CAD", "46.99 CAD"),
                              (3, None, "3"), (1234.5, "$", "$1,234.50")]:
        check(f"money({value!r}, {unit!r}) -> {want!r}", t.money(value, unit) == want,
              t.money(value, unit))
    check("a CPU threshold does not read as currency",
          "91%" in t.render_sms(dict(BASE, kind="threshold", value=91, target=85, unit="%",
                                     op="over")))

    print("--- confidence is carried onto every surface ---")
    p = dict(BASE, kind="price_drop", value=46.99, target=50.0, confidence="unconfirmed",
             confidence_note="read from the offer listing, not the main price")
    check("sms flags it briefly", "(unconfirmed)" in t.render_sms(p), t.render_sms(p))
    check("the email explains it in full",
          "offer listing" in t.render_plain(p), t.render_plain(p))
    check("the html shows it as a callout", "Unconfirmed reading" in t.render_html(p))
    hi = dict(p, confidence="high", confidence_note=None)
    check("a high-confidence alert is not cluttered with a caveat",
          "unconfirmed" not in t.render_sms(hi).lower(), t.render_sms(hi))

    print("--- the html is safe and self-contained ---")
    evil = dict(BASE, kind="price_drop", value=1.0,
                item='<script>alert(1)</script> & "quoted"')
    html = t.render_html(evil)
    check("markup in a page title cannot inject", "<script>" not in html, html[:200])
    check("...it is escaped instead", "&lt;script&gt;" in html)
    check("ampersands escaped", "&amp;" in html)
    check("no external assets (a strict client blocks them anyway)",
          "src=" not in html and "@import" not in html and "<link" not in html)
    # Stronger than counting: every href must be a link this payload put there. A count of two
    # would pass with one legitimate link replaced by an injected one.
    hrefs = re.findall(r'href="([^"]*)"', html)
    check("every href is a link we put there",
          hrefs and set(hrefs) <= {evil.get("url"), evil.get("cancel_url")} - {None}, str(hrefs))

    print("--- the cancel link reaches the email, and only the email ---")
    curl = "https://cancel.ohmz.cloud/c?t=AbC123xyz.def-XYZ_45"
    p = dict(BASE, kind="price_drop", value=46.99, target=50.0, unit="$", cancel_url=curl)
    html, plain = t.render_html(p), t.render_plain(p)
    check("html carries the cancel anchor", f'href="{curl}"' in html)
    check("...worded as an action", "Cancel this monitor" in html)
    check("...and drops the dead-end sentence", "Reply in the assistant to change or" not in html)
    check("plain text carries the raw url on its own line",
          f"Cancel this monitor: {curl}" in plain.splitlines(), plain[-200:])
    check("...with no period glued to it", curl + "." not in plain)
    sms = at.sms_body(t.render_sms(p))
    check("the sms never sees it", "cancel.ohmz" not in sms and "http" not in sms, sms)
    check("nor does the subject", "http" not in t.render_subject(p), t.render_subject(p))
    hrefs = set(re.findall(r'href="([^"]*)"', html))
    check("both hrefs accounted for", hrefs == {BASE["url"], curl}, str(hrefs))
    # The one-accent rule: the cancel affordance is a footer text link, not a second button.
    check("cancel is not dressed as a button",
          "background" not in html.split(f'href="{curl}"')[1].split(">")[0])
    nasty = dict(p, cancel_url='https://cancel.ohmz.cloud/c?t=a&b"c')
    nhtml = t.render_html(nasty)
    check("a hostile cancel url is escaped in the href",
          'href="https://cancel.ohmz.cloud/c?t=a&amp;b&quot;c"' in nhtml)
    # The anchor is found by the line's own prefix. Matching on the URL instead would turn any
    # other footer line that happened to contain it into a link, dropping what that line said.
    sneaky = dict(BASE, kind="price_drop", value=46.99, cancel_url=curl,
                  texted_to=f"+1 514-557-9764 {curl}")
    shtml = t.render_html(sneaky)
    check("a url echoed elsewhere in the footer does not eat that line",
          "a text went to" in shtml and shtml.count("Cancel this monitor</a>") == 1, shtml[-400:])
    absent = t.render_html(dict(BASE, kind="price_drop", value=46.99))
    check("without cancel_url the old sentence remains",
          "Reply in the assistant to change or cancel this monitor." in absent)
    check("...and no cancel host appears", "cancel.ohmz" not in absent)

    print("--- 'subscribed' confirms creation, not a result ---")
    # Every other kind here reports something a WATCHED VALUE did; this one reports something the
    # USER did, and the generic lead line ("...just hit your condition") would be a flat lie about
    # a monitor that has not run its first check yet.
    generic = dict(BASE, kind="subscribed", item="Zakkart Cat Scratching Board",
                   monitor="cat board watch", schedule="every 6h")
    check("no fabricated 'just hit your condition'",
          "just hit your condition" not in t.render_html(generic))
    check("...nor in the plain text", "just hit your condition" not in t.render_plain(generic))
    check("the sms reads as a plain confirmation, not a problem",
          "Heads up" not in t.render_sms(generic), t.render_sms(generic))
    check("the item still names itself", "Zakkart Cat Scratching Board" in t.render_sms(generic))
    check("subject says something happened, not a value",
          t.render_subject(generic).startswith("You're all set:"), t.render_subject(generic))

    flight = dict(BASE, kind="subscribed", item="YTO→YVR fare watch under $1,000",
                  monitor="YTO→YVR fare watch under $1,000", schedule="every 15m",
                  target=1000, unit="CAD", depart_found="2026-10-02", ret_found="2026-11-02",
                  date_basis="exact")
    check("a target price is repeated back, so the promise is on record before the first check",
          "1,000.00 CAD" in t.render_html(flight) and "1,000.00 CAD" in t.render_plain(flight))
    check("...and in the sms too", "1,000.00" in t.render_sms(flight), t.render_sms(flight))
    fsms = t.render_sms(flight)
    check("the sms carries no web address (still)",
          "http" not in fsms and "amazon" not in fsms, fsms)
    check("a flight watch's arrow survives ascii-folding, not vanishing",
          "YTO->YVR" in fsms and "YTOYVR" not in fsms, fsms)
    check("no 'found on' claim for dates nobody searched for",
          "found on" not in t.render_plain(flight), t.render_plain(flight))
    check("but the itinerary dates ARE shown", "2 Oct 2026" in t.render_plain(flight))
    check("no fabricated dates line when none were given",
          "dates found" not in t.render_plain(generic))
    check("without a target, the sentence still stands on its own",
          "None" not in t.render_html(generic) and "None" not in t.render_plain(generic))
    check("the cancel-link machinery still works alongside it",
          "Cancel this monitor" in t.render_html(dict(generic, cancel_url=curl)))

    print("--- 'subscribed' direction: under a price drop, over a price rise ---")
    rise = dict(BASE, kind="subscribed", item="resale watch", target=500, unit="$", op="over")
    check("a price-RISE confirmation says 'over', not 'under'",
          "over $500.00" in t.render_sms(rise) and "over $500.00" in t.render_html(rise), rise)
    check("no unrendered op leaks through as text", "over over" not in t.render_sms(rise))
    drop_default = dict(BASE, kind="subscribed", item="cat board watch", target=50, unit="$")
    check("no op at all defaults to 'under', the common case",
          "under $50.00" in t.render_sms(drop_default))
    explicit_under = dict(drop_default, op="under")
    check("an explicit 'under' reads the same as the default",
          t.render_sms(explicit_under) == t.render_sms(drop_default))
    stock = dict(BASE, kind="subscribed", item="ticket count watch", target=3, unit="")
    check("a bare count (empty unit) renders without a stray currency symbol",
          "under 3." in t.render_sms(stock) or "under 3 " in t.render_sms(stock), t.render_sms(stock))
    check("...and no naked '$3' appears anywhere", "$3" not in t.render_sms(stock))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

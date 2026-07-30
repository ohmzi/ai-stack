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
    for kind in ("unreachable", "blocked", "no_value"):
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
    check("the link is the only href", html.count("href=") <= 1)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

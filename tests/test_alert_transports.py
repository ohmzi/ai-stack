#!/usr/bin/env python3
"""SMS + email alert transports: everything that can be checked without sending a real message.

Why this file exists. ntfy died on an unverifiable last hop, and the lesson generalizes: the parts
of a notification path that CAN be pinned deterministically should be, so that when a live test
fails there is exactly one unknown left. Everything here runs offline with the network stubbed —
what remains for live testing is only "does the carrier/mailbox show it".

Pinned behaviours, each chosen because getting it wrong fails silently in production:

  * **E.164 normalization.** Twilio rejects non-E.164 numbers with a 400 that a naive caller logs
    and forgets. A number that cannot be normalized must be REFUSED locally, not sent.
  * **Partial success is success.** SMS delivered + email failed means the user was alerted;
    returning False would make the watcher re-send the SMS every minute to fix an email problem.
  * **Unconfigured is not an error.** No config file, or a handle with no phone, degrades to the
    channels that do work — never an exception into the delivery tick.
  * **The Twilio request shape**, asserted against the documented API (form-encoded From/To/Body,
    basic auth, /Accounts/<SID>/Messages.json), because a typo there is a 404 in production and a
    passing unit test everywhere else.
  * **Address resolution precedence** — contacts file overrides OpenWebUI, since a user may want
    alerts at a different address than their login.

Usage:  python3 tests/test_alert_transports.py
"""
import importlib.util
import json
import os
import sys
import tempfile

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load():
    spec = importlib.util.spec_from_file_location(
        "at", "/home/ohmz/ai-stack/scripts/alert_transports.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    at = load()

    print("--- E.164 normalization (Twilio accepts nothing else) ---")
    for raw, want in [
        ("+15551234567", "+15551234567"),
        ("5551234567", "+15551234567"),
        ("(555) 123-4567", "+15551234567"),
        ("555-123-4567", "+15551234567"),
        ("15551234567", "+15551234567"),
        ("+44 7700 900123", "+447700900123"),
        ("123", None),
        ("not a phone", None),
        ("", None),
        (None, None),
    ]:
        got = at.normalize_phone(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"got {got!r}")

    print("--- env parser strips inline comments (a comment leaked into a gateway address, live) ---")
    with tempfile.TemporaryDirectory() as td:
        cf = os.path.join(td, "e.env")
        open(cf, "w").write("SMS_GATEWAY=msg.telus.com   # Telus/Public\nSMTP_PASS=abcd efgh ijkl mnop\n")
        at.CONF = cf
        c = at.load_conf()
        check("gateway value has no comment", c["SMS_GATEWAY"] == "msg.telus.com", repr(c.get("SMS_GATEWAY")))
        check("app-password spaces preserved (no ' #' in it)", c["SMTP_PASS"] == "abcd efgh ijkl mnop",
              repr(c.get("SMTP_PASS")))

    print("--- carrier email-to-SMS gateway address forming ---")
    for e164, want in [
        ("+15145579764", "5145579764"),
        ("15145579764", "5145579764"),
        ("5145579764", "5145579764"),
        ("+447700900123", None),   # non-NANP -> refused
        ("", None),
    ]:
        check(f"national_number({e164!r}) -> {want!r}", at.national_number(e164) == want)
    check("carrier_sms_address builds <num>@gateway",
          at.carrier_sms_address("+15145579764", "msg.telus.com") == "5145579764@msg.telus.com")
    check("carrier_sms_address None when unformable",
          at.carrier_sms_address("+447700900123", "msg.telus.com") is None)

    print("--- send_sms dispatches to the gateway (an email), not Twilio ---")
    sent = {}
    at.send_email = lambda to, subj, body, conf: sent.update(to=to, subj=subj, body=body) or True
    ref = at.send_sms("+15145579764", "target met at 51.77", {"SMS_GATEWAY": "msg.telus.com"})
    check("gateway send returns gateway:<addr>", ref == "gateway:5145579764@msg.telus.com", repr(ref))
    check("emailed the carrier address", sent.get("to") == "5145579764@msg.telus.com", repr(sent))
    check("empty subject for gateway", sent.get("subj") == "", repr(sent.get("subj")))
    check("body carried through", "51.77" in (sent.get("body") or ""), repr(sent.get("body")))

    print("--- SMS bodies carry no links (the gateway silently eats them) ---")
    # Measured 2026-07-30: two price alerts containing an amazon.ca URL were accepted by Gmail's
    # SMTP server and never arrived; a link-free test sent minutes later arrived at once. The
    # gateway gives no bounce and no error code, so this failure is undetectable downstream — the
    # only defence is not to send a link. Email always carries the full text, link intact.
    at3 = load()
    body = at3.sms_body("46.99 is below your 50.00 target — https://www.amazon.ca/dp/B0DP6D3TRB")
    check("the URL is gone entirely", "amazon" not in body and "http" not in body, repr(body))
    check("the alert still says the thing that matters", "46.99" in body and "50.00" in body, repr(body))
    check("and points at where the link went", body.endswith("Link in email."), repr(body))
    # TESTC is why hosts are removed rather than kept: a BARE DOMAIN was filtered exactly like a
    # full URL, so an earlier version that shortened links to their hostname would have been
    # dropped identically.
    check("a bare domain is stripped too (TESTC never arrived)",
          "amazon.ca" not in at3.sms_body("check www.amazon.ca now"),
          repr(at3.sms_body("check www.amazon.ca now")))
    check("...including one with no www.",
          "amazon.ca" not in at3.sms_body("price at amazon.ca dropped"))
    check("a message with no link is untouched",
          at3.sms_body("CPU at 91 percent") == "CPU at 91 percent")
    check("no '(link in email)' is bolted onto a link-free message",
          "link in email" not in at3.sms_body("CPU at 91 percent"))
    # Decimals, version numbers and the confidence caveat must not read as hostnames.
    for keep in ("46.99 below 50.00", "python 3.11 vs 3.12",
                 "46.99 (confidence: low, source: amazon-offer-listing)"):
        check(f"{keep[:34]!r} survives intact", at3.sms_body(keep) == keep, repr(at3.sms_body(keep)))
    check("over-long body is truncated to one segment",
          len(at3.sms_body("x" * 400)) <= 140)
    check("truncation is marked, not silent", at3.sms_body("x" * 400).endswith("..."))

    print("--- the text names the monitor, and degrades in a fixed order ---")
    long_msg = "46.99 is below your 50.00 target (LOW confidence: amazon-offer-listing) — https://www.amazon.ca/dp/B0DP6D3TRB"
    b = at3.sms_body(long_msg, "amazon B0DP6D3TRB price")
    check("leads with the monitor name — WHICH one fired is the first question",
          b.startswith("amazon B0DP6D3TRB price:"), b)
    check("still under one segment", len(b) <= 140, str(len(b)))
    check("keeps the pointer to the email", b.endswith("Link in email."), b)
    check("no link survived", "amazon.ca" not in b and "http" not in b, b)
    # The pointer is the only thing telling a first-time user where the link went, so it outranks
    # both the monitor name and the measurement when space runs out.
    huge = at3.sms_body(long_msg, "x" * 90)
    check("an over-long monitor name is shortened, not the pointer",
          huge.endswith("Link in email.") and len(huge) <= 140, f"{len(huge)}: {huge}")
    check("...and the shortening is visible", ".." in huge, huge)
    nolink = at3.sms_body("CPU at 91% against your 85% threshold", "cpu watch")
    check("no pointer when there was no link to strip",
          "in email" not in nolink and nolink.startswith("cpu watch:"), nolink)
    check("a job-less alert still renders",
          at3.sms_body("something fired") == "something fired")

    print("--- link-stripping must not eat ordinary dotted words ---")
    # An earlier pattern treated any "word.word" with a 2+ letter tail as a hostname, so the token
    # naming what broke was exactly the token deleted — then a "(link in email)" was bolted on for
    # a link that never existed. On a box running ollama, openwebui and docker this is the common
    # case, not an edge case. The tail must be a real public suffix.
    for keep in ["ollama.service died, GPU stuck at 100%",
                 "openwebui container down, check webui.db",
                 "node.js worker crashed",
                 "hermes gateway.pid stale, 3 jobs queued",
                 "restore from webui.db.bak-channels",
                 "config.yaml changed, cron.provider reset"]:
        got = at3.sms_body(keep, None)
        check(f"survives intact: {keep[:34]!r}", got == keep, repr(got))
        check(f"...and claims no link: {keep[:22]!r}", "in email" not in got, repr(got))
    for strip in ["see https://x.io/a", "check www.amazon.ca", "price at amazon.ca dropped",
                  "open foo.co.uk/page"]:
        got = at3.sms_body(strip, None)
        check(f"still stripped: {strip[:30]!r}", "." not in got.replace("Link in email.", ""),
              repr(got))
    # A half-eaten address is worse than none: "reply to omariqbal97@" looks usable and is not.
    got = at3.sms_body("reply to omariqbal97@gmail.com", None)
    check("an email address is removed whole, not just its domain", "@" not in got, repr(got))

    print("--- everything sent is plain ASCII (gateways predate UTF-8) ---")
    for raw in ["46.99 \u2014 below \u2018target\u2019", "price \u20ac12.34\u2026", "caf\u00e9 monitor"]:
        out = at3.sms_body(raw, "m")
        check(f"{raw[:24]!r} folded to ASCII", out.isascii(), repr(out))
    check("em dash becomes a hyphen", "-" in at3.sms_body("a \u2014 b", None))
    check("euro sign becomes EUR", "EUR" in at3.sms_body("\u20ac12.34", None))

    print("--- the email subject mirrors the text, so both name one event ---")
    subj = at3.alert_subject(long_msg, "amazon B0DP6D3TRB price")
    check("monitor name lands first", subj.startswith("amazon B0DP6D3TRB price:"), subj)
    check("the number lands inside the ~45-char notification window",
          "46.99" in subj[:52], subj[:52])
    check("no link in the subject either", "http" not in subj and "amazon.ca" not in subj, subj)
    check("subject is ASCII", subj.isascii(), subj)
    check("an empty message still yields a usable subject",
          at3.alert_subject("", "cpu watch") == "Alert: cpu watch")
    check("no job, no message -> a sane constant",
          at3.alert_subject("", None) == "Assistant alert")

    print("--- the email body carries what the text had to drop ---")
    body = at3.alert_email_body(long_msg, "amazon B0DP6D3TRB price", "6c6f16be874c",
                                "2026-07-30T15:24:55", "+15145579764")
    check("the link is present in full", "https://www.amazon.ca/dp/B0DP6D3TRB" in body, body)
    check("names the monitor", "amazon B0DP6D3TRB price" in body)
    check("carries the job id for debugging", "6c6f16be874c" in body)
    check("carries the time it fired", "2026-07-30T15:24:55" in body)
    check("says a text was also sent, and to where", "+15145579764" in body)
    check("explains why the text had no link", "silently drop" in body)

    print("--- the fan-out sends the SMS form to sms and the full text to email ---")
    at3.load_conf = lambda: {"SMS_GATEWAY": "msg.telus.com", "SMTP_HOST": "h",
                             "SMTP_USER": "u", "SMTP_PASS": "p"}
    at3.resolve = lambda h, c=None, k=None: ("to@test", "+15145579764")
    # Stub the DNS probe with its real (status, detail) shape — "to@test" is not a real domain and
    # this section is about fan-out, not deliverability.
    at3.mail_domain_status = lambda addr, timeout=6: ("ok", "stubbed")
    seen = {}
    at3.send_sms = lambda phone, msg, conf: seen.__setitem__("sms", msg) or "gateway:x"
    at3.send_email = lambda to, subj, body, conf: seen.__setitem__("email", body) or True
    at3.send_alert("ohmz", "46.99 below target — https://www.amazon.ca/dp/B0DP6D3TRB")
    check("sms leg got the link-free form",
          "amazon" not in seen["sms"] and "46.99" in seen["sms"], repr(seen.get("sms")))
    check("email leg kept the full URL", "https://www.amazon.ca/dp/B0DP6D3TRB" in seen["email"],
          repr(seen.get("email")))

    print("--- SMTP acceptance is not deliverability (ohmz.com has no MX) ---")
    # The relay accepting a message says nothing about whether a mailbox exists. Gmail accepted
    # every alert addressed to ohmz@ohmz.com, then bounced asynchronously to the sending account
    # where nothing is watching, and the ledger recorded "email sent" each time. Same silent-loss
    # shape as the SMS gateway eating links — and it matters more now that texts drop the URL and
    # say "(link in email)".
    at4 = load()
    check("a domain with real MX is ok", at4.mail_domain_status("x@gmail.com")[0] == "ok")
    check("no MX + no A is dead",
          at4.mail_domain_status("x@nonexistent-zzz-domain-9987.com")[0] == "dead")
    check("an address with no domain is dead", at4.mail_domain_status("bogus")[0] == "dead")
    check("empty address is dead, not a crash", at4.mail_domain_status("")[0] == "dead")
    st, detail = at4.mail_domain_status("ohmz@ohmz.com")
    check("no MX but an A record is flagged 'implicit', not silently trusted",
          st == "implicit", f"{st} / {detail}")
    check("...and the note explains why that is not deliverable", "implicit MX" in detail, detail)

    print("--- the ledger records the body that was actually sent ---")
    # It recorded the ORIGINAL alert text, so a text mangled in transit by link-stripping looked
    # flawless in the record — the one place an operator would look to find out what went wrong.
    at5 = load()
    at5.load_conf = lambda: {"SMS_GATEWAY": "msg.telus.com", "ALERT_CHANNELS": "sms"}
    at5.resolve = lambda h, c=None, k=None: (None, "+15145579764")
    at5.send_sms = lambda phone, body, conf: "gateway:x"
    ok, notes = at5.send_alert("ohmz", "46.99 under 50.00 — https://www.amazon.ca/dp/B0",
                               job="amazon price")
    check("delivered", ok is True, repr(notes))
    check("the note carries the exact body sent", any("body=" in n for n in notes), repr(notes))
    sent_body = [n for n in notes if "body=" in n][0].split("body=", 1)[1]
    check("...and that body is the stripped form, not the original",
          "amazon.ca" not in sent_body and "46.99" in sent_body, sent_body)
    check("...showing the monitor name that was prepended", "amazon price" in sent_body, sent_body)

    print("--- a dead domain is refused and recorded, never counted as delivered ---")
    at4.load_conf = lambda: {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASS": "p",
                             "ALERT_CHANNELS": "email"}
    at4.resolve = lambda h, c=None, k=None: ("x@nonexistent-zzz-domain-9987.com", None)
    sent = []
    at4.send_email = lambda *a, **k: sent.append(a) or True
    ok, notes = at4.send_alert("ohmz", "target met")
    check("no send is even attempted", sent == [], repr(sent))
    check("not reported as delivered", ok is False, repr(notes))
    check("the reason is in the notes for the ledger",
          any("NOT SENT" in n and "cannot be delivered" in n for n in notes), repr(notes))

    at4.resolve = lambda h, c=None, k=None: ("ohmz@ohmz.com", None)
    ok, notes = at4.send_alert("ohmz", "target met")
    check("an implicit-MX domain IS still attempted", ok is True, repr(notes))
    check("...but the note marks it unverifiable", any("UNVERIFIABLE" in n for n in notes), repr(notes))

    print("--- unconfigured degrades, never raises ---")
    at.CONF = "/nonexistent/alert_transports.env"
    ok, notes = at.send_alert("ohmz", "hello")
    check("no config -> (False, explanatory note)", ok is False and notes and "no transport" in notes[0],
          repr(notes))

    print("--- resolution: contacts override OpenWebUI, phone is opt-in ---")
    with tempfile.TemporaryDirectory() as td:
        at.CONTACTS = os.path.join(td, "c.json")
        json.dump({"alice": {"phone": "555-000-1111", "email": "alt@x.com"},
                   "bob": {"phone": "bogus"}}, open(at.CONTACTS, "w"))
        e, p = at.resolve("alice", {}, at.load_contacts())
        check("contacts email wins", e == "alt@x.com", repr(e))
        check("contacts phone normalized", p == "+15550001111", repr(p))
        e, p = at.resolve("bob", {}, at.load_contacts())
        check("unnormalizable phone -> None (refused locally)", p is None, repr(p))
        e, p = at.resolve("nobody", {}, at.load_contacts())
        check("unknown handle -> no contacts", (e, p) == (None, None), repr((e, p)))

    print("--- fan-out: partial success counts as delivered ---")
    conf = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM": "+15550000000",
            "SMTP_HOST": "smtp.test", "SMTP_USER": "u@test", "SMTP_PASS": "p"}
    at.load_conf = lambda: conf
    at.resolve = lambda h, c=None, k=None: ("to@test", "+15551234567")
    at.mail_domain_status = lambda addr, timeout=6: ("ok", "stubbed")

    at.send_sms = lambda *a, **k: "SM123"
    at.send_email = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mailbox full"))
    ok, notes = at.send_alert("alice", "target met")
    check("sms ok + email fail -> delivered", ok is True, repr(notes))
    check("failure is reported, not hidden", any("email FAILED" in n for n in notes), repr(notes))

    at.send_sms = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("twilio 401"))
    at.send_email = lambda *a, **k: True
    ok, notes = at.send_alert("alice", "target met")
    check("sms fail + email ok -> delivered", ok is True, repr(notes))

    at.send_email = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    ok, _ = at.send_alert("alice", "target met")
    check("both fail -> NOT delivered (watcher folds it into the channel)", ok is False)

    print("--- channel selection is honoured ---")
    conf["ALERT_CHANNELS"] = "email"
    sent = {"sms": 0}
    at.send_sms = lambda *a, **k: sent.__setitem__("sms", sent["sms"] + 1) or "SM"
    at.send_email = lambda *a, **k: True
    ok, notes = at.send_alert("alice", "x")
    check("ALERT_CHANNELS=email skips sms entirely", sent["sms"] == 0 and ok is True, repr(notes))

    print("--- Twilio request shape matches the documented API ---")
    at2 = load()
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"sid": "SM999"}'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        captured["auth"] = req.headers.get("Authorization", "")
        captured["ctype"] = req.headers.get("Content-type", "")
        return FakeResp()

    at2.urllib.request.urlopen = fake_urlopen
    sid = at2.send_sms("+15551234567", "hi there", conf)
    check("returns the message sid", sid == "SM999", repr(sid))
    check("URL is /Accounts/<SID>/Messages.json",
          captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json",
          captured.get("url", ""))
    check("basic auth header", captured["auth"].startswith("Basic "), captured.get("auth", ""))
    check("form-encoded", "application/x-www-form-urlencoded" in captured["ctype"].lower(),
          captured.get("ctype", ""))
    for field in ("From=%2B15550000000", "To=%2B15551234567", "Body=hi+there"):
        check(f"body carries {field}", field in captured["data"], captured.get("data", ""))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

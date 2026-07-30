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
    check("http link reduced to its bare host",
          at3.sms_body("46.99 below target — https://www.amazon.ca/dp/B0DP6D3TRB")
          == "46.99 below target — www.amazon.ca")
    check("https + path + query all stripped",
          "?" not in at3.sms_body("see https://x.com/a/b?c=1&d=2 now"))
    check("a message with no link is untouched",
          at3.sms_body("CPU at 91 percent") == "CPU at 91 percent")
    check("over-long body is truncated to one segment",
          len(at3.sms_body("x" * 400)) <= 140)
    check("truncation is marked, not silent", at3.sms_body("x" * 400).endswith("\u2026"))

    print("--- the fan-out sends the SMS form to sms and the full text to email ---")
    at3.load_conf = lambda: {"SMS_GATEWAY": "msg.telus.com", "SMTP_HOST": "h",
                             "SMTP_USER": "u", "SMTP_PASS": "p"}
    at3.resolve = lambda h, c=None, k=None: ("to@test", "+15145579764")
    seen = {}
    at3.send_sms = lambda phone, msg, conf: seen.__setitem__("sms", msg) or "gateway:x"
    at3.send_email = lambda to, subj, body, conf: seen.__setitem__("email", body) or True
    at3.send_alert("ohmz", "46.99 below target — https://www.amazon.ca/dp/B0DP6D3TRB")
    check("sms leg got the link-free form", seen["sms"].endswith("www.amazon.ca"), repr(seen.get("sms")))
    check("email leg kept the full URL", "https://www.amazon.ca/dp/B0DP6D3TRB" in seen["email"],
          repr(seen.get("email")))

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

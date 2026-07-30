#!/usr/bin/env python3
"""Personal alert transports: Twilio SMS and SMTP email. The seam ntfy used to fill.

Why this is a separate module. `send_alert()` in hermes_delivery.py is the one function a transport
must implement; keeping the transports here means they are testable without the watcher, and
swapping or adding one never touches delivery logic. Stdlib only — Twilio is a form-encoded POST,
email is smtplib.

Design, and the reasons:

* **Two channels, different jobs.** SMS is the buzz (a real text, real ringtone, no app, works on a
  dead-battery-mode phone); email is the record (full text, searchable, free). Both fire by default
  because a fired condition is worth both; ALERT_CHANNELS narrows it.
* **Partial success is success.** If SMS lands and email fails, the user was alerted — return True
  so the watcher marks it delivered, and log which leg failed. Returning False would re-alert by SMS
  on the next tick to fix an email problem.
* **Addresses come from where they actually live.** Email is authoritative in OpenWebUI's user table
  and needs no configuration. Phone numbers do not exist there, so they are opt-in per handle in
  ~/.hermes/alert_contacts.json — a user with no phone entry silently gets email only, which is the
  right default for a household where not everyone wants texts.
* **E.164 or nothing.** Twilio rejects anything else, so numbers are normalized (digits kept, a
  default country code applied to bare 10-digit US numbers) and a number that cannot be normalized
  is skipped with a log line rather than sent and silently dropped by the API.
* **No retries here.** The watcher already retries per leg on the next minute's tick; retrying
  inside a transport would double-send on a slow-but-successful request.

Configuration (~/.hermes/alert_transports.env, 0600, never in git):

    ALERT_CHANNELS=sms,email          # optional; default both
    ALERT_DEFAULT_COUNTRY=+1          # optional; for bare 10-digit numbers
    TWILIO_ACCOUNT_SID=ACxxxxxxxx
    TWILIO_AUTH_TOKEN=xxxxxxxx
    TWILIO_FROM=+15551234567
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587                     # 587 = STARTTLS, 465 = implicit TLS
    SMTP_USER=you@gmail.com
    SMTP_PASS=app-password            # an APP password, not the account password
    SMTP_FROM=you@gmail.com           # optional; defaults to SMTP_USER

Contacts (~/.hermes/alert_contacts.json): {"ohmz": {"phone": "+15551234567"}}
Email is looked up from OpenWebUI automatically; an "email" key here overrides it.

Usage:  python3 scripts/alert_transports.py --status
        python3 scripts/alert_transports.py --test <handle> "message"
"""
import argparse
import base64
import json
import os
import re
import smtplib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

CONF = os.path.expanduser("~/.hermes/alert_transports.env")
CONTACTS = os.path.expanduser("~/.hermes/alert_contacts.json")
OWUI_DB = os.environ.get("OWUI_DB", "/volume1/docker/openwebui/config/webui.db")
TIMEOUT = 20


def load_conf():
    """KEY=VALUE lines; blank lines and # comments ignored. Missing file => {} (unconfigured)."""
    conf = {}
    if os.path.exists(CONF):
        for line in open(CONF):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def load_contacts():
    try:
        return json.load(open(CONTACTS))
    except Exception:
        return {}


def owui_email(handle):
    """The OpenWebUI email whose local part matches this handle. Read-only, WAL-safe."""
    try:
        db = sqlite3.connect(f"file:{OWUI_DB}?mode=ro", uri=True)
        rows = db.execute("select u.email from user u join auth a on a.id=u.id "
                          "where a.active=1").fetchall()
        db.close()
    except Exception:
        return None
    for (email,) in rows:
        local = re.sub(r"[^a-z0-9_-]", "", (email or "").split("@")[0].lower())
        if local == handle:
            return email
    return None


def normalize_phone(raw, default_country="+1"):
    """-> E.164, or None if it cannot be made valid. Twilio silently rejects other shapes."""
    if not raw:
        return None
    s = re.sub(r"[^\d+]", "", str(raw))
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        return "+" + digits if 8 <= len(digits) <= 15 else None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:                      # bare national number
        return f"{default_country}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def resolve(handle, conf=None, contacts=None):
    """(email, phone_e164) for a handle. Either may be None."""
    conf = conf if conf is not None else load_conf()
    contacts = contacts if contacts is not None else load_contacts()
    entry = contacts.get(handle, {}) or {}
    email = entry.get("email") or owui_email(handle)
    phone = normalize_phone(entry.get("phone"),
                            conf.get("ALERT_DEFAULT_COUNTRY", "+1"))
    return email, phone


def send_sms(to_e164, body, conf):
    """Twilio REST. Raises on failure so the caller can log the reason."""
    sid, token, frm = (conf.get("TWILIO_ACCOUNT_SID"), conf.get("TWILIO_AUTH_TOKEN"),
                       conf.get("TWILIO_FROM"))
    if not (sid and token and frm):
        raise RuntimeError("twilio not configured")
    data = urllib.parse.urlencode({"From": frm, "To": to_e164, "Body": body[:1500]}).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data, headers={"Authorization": f"Basic {auth}",
                            "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r).get("sid")
    except urllib.error.HTTPError as e:
        # Twilio puts the actionable reason in the body; a bare "400" is useless in a log.
        raise RuntimeError(f"twilio {e.code}: {e.read()[:200].decode(errors='replace')}") from None


def send_email(to_addr, subject, body, conf):
    host, user, pw = conf.get("SMTP_HOST"), conf.get("SMTP_USER"), conf.get("SMTP_PASS")
    if not (host and user and pw):
        raise RuntimeError("smtp not configured")
    port = int(conf.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"] = conf.get("SMTP_FROM") or user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
    return True


def send_alert(handle, message, subject="Alert from your assistant"):
    """Fan out one alert. Returns (ok, [notes]) — ok is True if ANY channel delivered."""
    conf = load_conf()
    if not conf:
        return False, ["no transport configured (~/.hermes/alert_transports.env missing)"]
    channels = [c.strip() for c in conf.get("ALERT_CHANNELS", "sms,email").split(",") if c.strip()]
    email, phone = resolve(handle, conf)
    ok, notes = False, []

    if "sms" in channels:
        if not phone:
            notes.append(f"sms skipped: no phone for {handle!r} in alert_contacts.json")
        else:
            try:
                notes.append(f"sms sent to {phone} ({send_sms(phone, message, conf)})")
                ok = True
            except Exception as e:
                notes.append(f"sms FAILED: {e}")

    if "email" in channels:
        if not email:
            notes.append(f"email skipped: no address for {handle!r}")
        else:
            try:
                send_email(email, subject, message, conf)
                notes.append(f"email sent to {email}")
                ok = True
            except Exception as e:
                notes.append(f"email FAILED: {e}")

    return ok, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", nargs=2, metavar=("HANDLE", "MESSAGE"))
    a = ap.parse_args()
    conf = load_conf()

    if a.status or not a.test:
        print(f"config   : {CONF} {'FOUND' if conf else 'MISSING'}")
        print(f"channels : {conf.get('ALERT_CHANNELS', 'sms,email (default)')}")
        print(f"twilio   : {'configured' if conf.get('TWILIO_ACCOUNT_SID') else 'NOT configured'}"
              f"  from={conf.get('TWILIO_FROM', '-')}")
        print(f"smtp     : {'configured' if conf.get('SMTP_HOST') else 'NOT configured'}"
              f"  host={conf.get('SMTP_HOST', '-')} user={conf.get('SMTP_USER', '-')}")
        print(f"contacts : {CONTACTS} {'FOUND' if os.path.exists(CONTACTS) else 'MISSING'}")
        for h in sorted(set(load_contacts()) | {"ohmz", "ohmz2"}):
            e, p = resolve(h, conf)
            print(f"   {h:10} email={e or '-':24} phone={p or '-'}")
        return 0

    ok, notes = send_alert(a.test[0], a.test[1])
    for n in notes:
        print(" ", n)
    print("RESULT:", "delivered" if ok else "NOT delivered")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

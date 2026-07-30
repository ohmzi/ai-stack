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
# Contacts live in an `alerts/` subdirectory of the OpenWebUI config tree because it is the ONLY
# path both sides can reach. The subdirectory is owned by the host user rather than root: the
# config dir itself is root:root 755, and an atomic tmp+rename needs write permission on the
# DIRECTORY, not just the file — so publishing the profile from the (unprivileged) delivery timer
# silently failed until the files got their own directory.
#
# Contacts live in the OpenWebUI config directory because it is the ONLY path both sides can reach:
# the auto_assistant pipe runs INSIDE the OpenWebUI container (bind-mounted at /app/backend/data, so
# it cannot see ~/.hermes), while these transports run on the host as the user. The pipe needs write
# access to save a phone number the user supplies in chat, and the transports need read access to
# send to it. Same file, two mount points. ~/.hermes stays a read fallback for older installs.
CONTACTS = os.environ.get("ALERT_CONTACTS",
                          "/volume1/docker/openwebui/config/alerts/contacts.json")
LEGACY_CONTACTS = os.path.expanduser("~/.hermes/alert_contacts.json")
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
                # Strip a trailing " # inline comment" (whitespace-preceded, standard .env rule).
                # Values here are domains / creds that never contain " #", so this is safe and it
                # stops a comment leaking into a gateway address (observed live: a text went to
                # "5145579764@msg.telus.com   # Telus/..." and vanished).
                v = re.split(r"\s+#", v, 1)[0]
                conf[k.strip()] = v.strip()
    return conf


def load_contacts():
    """Shared file wins; ~/.hermes is a fallback so an existing install keeps working un-migrated."""
    for path in (CONTACTS, LEGACY_CONTACTS):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def save_contact(handle, phone=None, email=None):
    """Add or update one handle. Returns (ok, note).

    Writes the SHARED file, since a number supplied in chat arrives from inside the container and
    must be visible to the host-side transports. The number is normalized before it is stored, so an
    unusable one is rejected at the point the user typed it rather than silently at send time.
    """
    if phone is not None:
        norm = normalize_phone(phone)
        if not norm:
            return False, f"{phone!r} is not a usable phone number"
        phone = norm
    contacts = load_contacts()
    entry = dict(contacts.get(handle) or {})
    if phone:
        entry["phone"] = phone
    if email:
        entry["email"] = email
    contacts[handle] = entry
    try:
        os.makedirs(os.path.dirname(CONTACTS), exist_ok=True)
        tmp = CONTACTS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(contacts, f, indent=2)
        os.replace(tmp, CONTACTS)
    except Exception as e:
        return False, f"could not write {CONTACTS}: {e}"
    return True, f"saved {handle}: " + ", ".join(k for k in ("phone", "email") if entry.get(k))


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


def national_number(e164):
    """E.164 -> local 10-digit (North America). Carrier email-to-SMS gateways want the bare
    number, no country code: +15145579764 -> 5145579764. None if not a 10/11-digit NANP number."""
    digits = re.sub(r"\D", "", e164 or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def carrier_sms_address(phone_e164, gateway):
    """<local-number>@<gateway>, e.g. 5145579764@msg.telus.com. None if the number can't be formed."""
    nn = national_number(phone_e164)
    return f"{nn}@{gateway}" if (nn and gateway) else None


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
    """One text. Two methods: 'gateway' emails the carrier's email-to-SMS bridge (free, rides the
    SMTP transport); 'twilio' hits the REST API. Default is gateway when SMS_GATEWAY is set."""
    gateway = conf.get("SMS_GATEWAY")
    method = conf.get("SMS_METHOD", "gateway" if gateway else "twilio")

    if method == "gateway":
        addr = carrier_sms_address(to_e164, gateway)
        if not addr:
            raise RuntimeError(f"cannot form carrier address for {to_e164!r} / gateway {gateway!r}")
        # Carrier gateways turn the email into a text: empty subject (some prepend it), short body.
        send_email(addr, "", body[:300], conf)
        return f"gateway:{addr}"

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
        raise RuntimeError(f"twilio {e.code}: {e.read()[:200].decode(errors='replace')}") from None


def mail_domain_status(addr, timeout=6):
    """('ok'|'implicit'|'dead'|'unknown', detail) for an address's domain.

    SMTP acceptance by the RELAY says nothing about deliverability. Gmail accepts a message for
    ohmz@ohmz.com, then discovers the domain has no MX, then bounces asynchronously to the sending
    mailbox — where nothing in this system is watching. The ledger says "email sent" forever.

    This is the same silent-loss shape as the SMS gateway eating links, and it now matters more:
    texts deliberately drop URLs and say "(link in email)", so an undeliverable email leg leaves the
    user with a pointer to nothing.

    'implicit' means no MX but an A record exists. RFC 5321 says mail then goes to that host, which
    for a parked domain is a web server that speaks no SMTP — technically valid, practically dead.
    Worth flagging loudly, not worth refusing outright.

    Fails OPEN ('unknown') if no resolver is available: a missing `dig` must never stop an alert.
    """
    import shutil, subprocess
    domain = (addr or "").rsplit("@", 1)[-1].strip().lower()
    if not domain or "." not in domain:
        return "dead", f"{addr!r} has no usable domain"
    dig = shutil.which("dig")
    if not dig:
        return "unknown", "no resolver available to check"
    def q(rr):
        """Records, or None if the QUERY ITSELF failed — those are not the same answer.

        dig writes resolver errors to stderr, leaves stdout EMPTY and exits non-zero (9 for "no
        reply from server"). Reading stdout alone turns "I could not ask" into "there is no such
        record", which would classify a live domain as `dead` and cancel the email leg during a
        DNS blip — fail-CLOSED, the exact opposite of the intent stated above.
        """
        try:
            r = subprocess.run([dig, "+short", "+time=3", "+tries=1", rr, domain],
                               capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                return None
            return [x for x in r.stdout.split("\n") if x.strip()]
        except Exception:
            return None
    mx = q("MX")
    if mx is None:
        return "unknown", "resolver error"
    if mx:
        return "ok", f"{len(mx)} MX record(s)"
    a = q("A")
    if a is None:
        return "unknown", "resolver error on A lookup"
    if a:
        return "implicit", (f"no MX; mail would fall back to A record {a[0]} (RFC 5321 implicit MX) "
                            f"— for a parked domain that host speaks no SMTP and mail is lost")
    return "dead", "domain has no MX and no A record — mail cannot be delivered"


def send_email(to_addr, subject, body, conf, html=None):
    host, user, pw = conf.get("SMTP_HOST"), conf.get("SMTP_USER"), conf.get("SMTP_PASS")
    if not (host and user and pw):
        raise RuntimeError("smtp not configured")
    port = int(conf.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"] = conf.get("SMTP_FROM") or user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    # multipart/alternative: the plain part is what a carrier gateway and a text-only client read,
    # the HTML part is what a normal mail client shows. Plain is set FIRST so it stays the
    # fallback rather than the payload.
    if html:
        msg.add_alternative(html, subtype="html")
    # send_message returns {recipient: (code, reason)} for anyone the server REFUSED. An empty
    # dict is the only proof of acceptance we can get; treat a refusal as a failure so the retry
    # queue sees it rather than logging a success the server never granted.
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
            s.login(user, pw)
            refused = s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls()
            s.login(user, pw)
            refused = s.send_message(msg)
    if refused:
        raise RuntimeError(f"server refused recipient(s): {refused}")
    return True


# What a spam filter reads as a web address: full URLs, www.* hosts, and bare hostnames — the last
# form was measured to be dropped exactly like a full link.
#
# The tail MUST be a real public suffix, not merely "2+ letters". A length rule matched every
# dotted token in ordinary prose and deleted the substance of the alert:
#     "ollama.service died, GPU stuck at 100%"   -> "died, GPU stuck at 100%"
#     "openwebui down, check webui.db"           -> "openwebui down, check"
#     "node.js worker crashed"                   -> "worker crashed"
# The word naming what broke is precisely the word a hostname pattern eats. So the tail is an
# allowlist: service/db/js/py/log/conf are not TLDs and survive.
_TLD = (r"(?:com|net|org|edu|gov|mil|int|info|biz|name|pro|mobi|asia|io|co|ai|app|dev|page|site|"
        r"online|shop|store|cloud|tech|blog|news|live|life|world|today|space|website|link|click|"
        r"media|video|studio|design|art|music|games?|fun|xyz|top|icu|vip|cc|ws|me|tv|ly|to|sh|gg|"
        r"fm|am|nu|bz|ca|uk|us|de|fr|jp|cn|au|nz|in|br|mx|es|it|nl|se|no|fi|dk|pl|ru|ch|at|be|pt|"
        r"gr|ie|il|za|kr|sg|hk|tw|th|my|ph|id|vn|tr|ua|cz|hu|ro|bg|hr|rs|sk|si|lt|lv|ee|is|lu|ar|"
        r"cl|pe|eu)")
# Emails are matched FIRST and removed WHOLE. Letting the hostname branch reach one deleted only
# the domain and left "reply to omariqbal97@" — a plausible-looking address that is unusable, which
# is worse than an obvious gap.
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
URL_RE = re.compile(
    rf"{_EMAIL}|https?://\S+|\bwww\.[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?"
    rf"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.{_TLD}\b(?:/\S*)?",
    re.I)


def sms_body(message, job=None, limit=140):
    """The SMS form of an alert: names the monitor, no web addresses, one ASCII segment.

    Carrier email-to-SMS gateways silently drop messages containing links. The message is accepted
    by SMTP, never bounces, and never arrives — so this cannot be detected downstream or fixed by
    retrying. It has to be avoided.

    Measured on this host 2026-07-30, three otherwise-identical texts sent seconds apart:

        "TESTA plain no link 46.99 below 50.00"              -> arrived
        "TESTB ... https://www.amazon.ca/dp/B0DP6D3TRB"      -> never arrived
        "TESTC ... www.amazon.ca"                            -> never arrived

    TESTC is why addresses are removed rather than shortened to their host: a BARE DOMAIN is
    filtered exactly like a full URL. An earlier version of this function replaced links with their
    hostname and would have been dropped just the same.

    The full text, links intact, always goes out by email — that is what the email leg is for, and
    the SMS says so.
    """
    body = URL_RE.sub("", message)
    body = re.sub(r"[\s\u2014-]+$", "", re.sub(r"\s{2,}", " ", body)).strip(" -\u2014")
    stripped = body != message.strip()
    body = ascii_fold(body)
    # The pointer is the one part that must always survive: it is the only thing telling a
    # first-time user where the link went. Only added when something was actually removed —
    # promising a link in an email that has none is its own small lie.
    tail = " Link in email." if stripped else ""
    # A caveat has to survive being read once, on a lock screen. The full explanation of WHY a
    # reading is unconfirmed is worth 60 characters in an email and is worth the flag alone in a
    # text — spending half the segment on it pushes the measurement toward truncation.
    body = re.sub(r"\((unconfirmed|high confidence|low confidence)[^)]*\)", r"(\1)", body)
    # The monitor name is user-supplied ("amazon.ca watch") and is prepended AFTER stripping, so it
    # could smuggle a domain into a text that then vanishes. Strip it on the same rule.
    name = ascii_fold(URL_RE.sub("", job or "")).strip(" :-")
    name = re.sub(r"\s{2,}", " ", name)

    def assemble(n):
        return (f"{n}: {body}{tail}" if n else f"{body}{tail}")

    out = assemble(name)
    if len(out) <= limit:
        return out
    # Degrade in a fixed order, worst-affordable-loss first. The monitor name is shortened before
    # the measurement, the measurement before the pointer, and the pointer never at all.
    room = limit - len(assemble(""))
    if name and room > 8:
        return assemble(name[:room - 4] + "..")
    keep = limit - len(tail) - 3
    return f"{body[:max(keep, 0)]}...{tail}"


# Typographic characters an LLM and this codebase both emit freely. A carrier gateway is a 1990s
# mail-to-SMS bridge with no promise of UTF-8; a mangled em dash reads as garbage on the handset,
# and the link-stripping work is wasted if the text arrives looking broken anyway. ASCII is the only
# thing every gateway is guaranteed to carry, so fold rather than gamble.
ASCII_FOLD = {"\u2014": "-", "\u2013": "-", "\u2026": "...", "\u2018": "'", "\u2019": "'",
              "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u00b7": "-", "\u2022": "-",
              "\u20ac": "EUR", "\u00a3": "GBP", "\u00a5": "JPY", "\u2192": "->"}


def ascii_fold(text):
    for bad, good in ASCII_FOLD.items():
        text = text.replace(bad, good)
    # Anything still non-ASCII (emoji in a job name, an accented product title) is dropped rather
    # than sent as bytes the gateway may refuse outright.
    return text.encode("ascii", "ignore").decode("ascii").strip()


PROFILE = os.environ.get("ALERT_PROFILE",
                         "/volume1/docker/openwebui/config/alerts/profile.json")


def alert_plan(handle):
    """How an alert to `handle` would actually be delivered, as display facts. No secrets.

    Exists so the assistant can TELL the user their alert setup at the moment they schedule a job,
    instead of them finding out at 3am that nothing was configured. Every field here is something a
    user can act on: a wrong number, a dead mailbox, an unrecognised sender.
    """
    conf = load_conf()
    email, phone = resolve(handle, conf)
    channels = [c.strip() for c in conf.get("ALERT_CHANNELS", "sms,email").split(",") if c.strip()]
    gateway = conf.get("SMS_GATEWAY")
    method = conf.get("SMS_METHOD", "gateway" if gateway else "twilio")
    sender = conf.get("SMTP_FROM") or conf.get("SMTP_USER")
    plan = {
        "handle": handle,
        "channels": channels,
        "sms_enabled": "sms" in channels,
        "email_enabled": "email" in channels,
        "phone": phone,
        "sms_method": method,
        "sms_via": carrier_sms_address(phone, gateway) if (method == "gateway" and phone) else None,
        "sms_from": sender if method == "gateway" else conf.get("TWILIO_FROM"),
        "email": email,
        "email_from": sender,
        "configured": bool(conf),
    }
    plan["email_status"] = mail_domain_status(email)[0] if email else None
    return plan


def publish_profile(path=None):
    """Write the non-secret display facts where the OpenWebUI pipe can read them.

    The pipe runs in a container and cannot see ~/.hermes/alert_transports.env, but it needs the
    sender address and channel list to show an honest confirmation. Refreshed by the delivery
    watcher every tick, so it can never drift from the live config. Contains NO credentials.
    """
    path = path or PROFILE
    conf = load_conf()
    gateway = conf.get("SMS_GATEWAY")
    method = conf.get("SMS_METHOD", "gateway" if gateway else "twilio")
    data = {
        "configured": bool(conf),
        "channels": [c.strip() for c in conf.get("ALERT_CHANNELS", "sms,email").split(",")
                     if c.strip()],
        "sms_method": method,
        "sms_gateway": gateway,
        "sms_from": (conf.get("SMTP_FROM") or conf.get("SMTP_USER")) if method == "gateway"
                    else conf.get("TWILIO_FROM"),
        "email_from": conf.get("SMTP_FROM") or conf.get("SMTP_USER"),
        "sms_char_limit": 140,
        "sms_strips_links": True,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def alert_subject(message, job=None):
    """Email subject: monitor, then the measurement.

    "Alert from your assistant" told the user nothing — every alert looked identical in a
    notification shade. A phone shows roughly the first 45 characters, so both the monitor name and
    the number must land inside that window.

    Deliberately the same shape as the SMS, so a user holding the text next to the email can see
    at a glance that the two describe one event rather than two.
    """
    head = ascii_fold(URL_RE.sub("", message)).strip(" -")
    head = re.sub(r"\s{2,}", " ", head)
    if not head:
        return f"Alert: {job}" if job else "Assistant alert"
    if len(head) > 110:
        head = head[:107] + "..."
    return f"{ascii_fold(job).strip()}: {head}" if job else head


def alert_email_body(message, job=None, job_id=None, when=None, phone=None):
    """The full record. Everything the text had to drop, plus what an operator needs at 2am."""
    lines = [message, ""]
    facts = []
    if job:
        facts.append(f"  Monitor : {job}")
    if job_id:
        facts.append(f"  Job ID  : {job_id}")
    if when:
        facts.append(f"  Fired   : {when}")
    if facts:
        lines += ["What fired", *facts, ""]
    if phone:
        lines.append(f"A text was also sent to {phone}. Texts cannot carry links — carrier "
                     f"gateways silently drop messages containing a web address — so any link for "
                     f"this alert is in this email only.")
        lines.append("")
    lines.append("You are receiving this because a background task you scheduled met its alert "
                 "condition. Reply in the assistant to change or cancel it.")
    return "\n".join(lines)


def _templates():
    """alert_templates, imported lazily and guarded — a broken template module must degrade to the
    plain sentence, not stop the alert. Delivery outranks presentation."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import alert_templates
        return alert_templates
    except Exception:
        return None


def send_alert(handle, message, subject=None, job=None, job_id=None, when=None, payload=None):
    """Fan out one alert. Returns (ok, [notes]) — ok is True if ANY channel delivered.

    With a `payload` the three surfaces are rendered from structured data by alert_templates, which
    is what makes a text read "Hi ohmz, the listing you're tracking - Zakkart Cat Scratching Board
    - is $46.99, under your $50.00 target" instead of a sliced-up log line. Without one, the plain
    `message` is used exactly as before, so jobs that emit only a sentence keep working.
    """
    conf = load_conf()
    if not conf:
        return False, ["no transport configured (~/.hermes/alert_transports.env missing)"]
    channels = [c.strip() for c in conf.get("ALERT_CHANNELS", "sms,email").split(",") if c.strip()]
    email, phone = resolve(handle, conf)
    ok, notes = False, []
    tpl = _templates() if payload else None
    if tpl and phone:
        # So the email can say where the text went without the job having to know.
        payload = dict(payload, texted_to=phone)

    if "sms" in channels:
        if not phone:
            notes.append(f"sms skipped: no phone for {handle!r} in alert_contacts.json")
        else:
            try:
                # The ledger recorded the ORIGINAL alert text, so a text that arrived mangled by
                # link-stripping looked flawless in the record. Log the body that actually went
                # out — it is the only copy of what the handset received.
                body = sms_body(tpl.render_sms(payload) if tpl else message,
                                None if tpl else job)
                ref = send_sms(phone, body, conf)
                notes.append(f"sms sent to {phone} ({ref}) body={body!r}")
                ok = True
            except Exception as e:
                notes.append(f"sms FAILED: {e}")

    if "email" in channels:
        if not email:
            notes.append(f"email skipped: no address for {handle!r}")
        else:
            status, detail = mail_domain_status(email)
            if status == "dead":
                # Do not spend an "ok" on a mailbox that provably cannot receive. Recording the
                # refusal is the whole point: the alternative is a ledger full of green "sent"
                # lines for mail that evaporated.
                notes.append(f"email NOT SENT to {email}: {detail}")
            else:
                try:
                    if tpl:
                        send_email(email, subject or tpl.render_subject(payload),
                                   tpl.render_plain(payload), conf,
                                   html=tpl.render_html(payload))
                    else:
                        send_email(email, subject or alert_subject(message, job),
                                   alert_email_body(message, job, job_id, when, phone), conf)
                    if status == "implicit":
                        notes.append(f"email sent to {email} — ⚠️ UNVERIFIABLE: {detail}")
                    else:
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
        _method = conf.get("SMS_METHOD", "gateway" if conf.get("SMS_GATEWAY") else "twilio")
        if _method == "gateway":
            print(f"sms      : carrier gateway  ->  <number>@{conf.get('SMS_GATEWAY', '(unset!)')}")
        else:
            print(f"sms      : twilio  {'configured' if conf.get('TWILIO_ACCOUNT_SID') else 'NOT configured'}"
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

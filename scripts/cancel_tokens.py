#!/usr/bin/env python3
"""The manage token an alert email carries: proof of the right to manage ONE monitor.

Why this exists. The email footer used to end at "Reply in the assistant to change or cancel this
monitor" — a dead end for someone reading the alert on a phone at 7am. The link that replaces it
must authorize a cancel (or a pause) with no login behind it, which makes the token itself the
whole security model. So the scheme lives in one small file that both sides import: the mail
sender mints (alert_transports), the public-facing service verifies (cancel_service), and neither
has to agree with the other about anything except what is written here.

Stateless on purpose. A token is `b64url(body).b64url(hmac)` where body is

    1|{job_id}|{handle}|{issued_epoch}

so verification needs only the shared secret — no token store, no writer, no cleanup job, no way
for a lost file to lock everyone out. What bounds a token's life instead:

  - the job: cancel is idempotent and a deleted job 404s, so a replayed token is a no-op;
  - the clock: max_age_s (default 30 days) — a link surfacing months later in a forwarded
    mailbox must be an expired capability, not a live one;
  - the secret: rotating CANCEL_SECRET kills every outstanding link at once, deliberately.

The version byte in front is the rotation path for the FORMAT (a future `2|...` can carry more
fields without breaking outstanding `1|` tokens during a grace window). 160 bits of HMAC-SHA256
survive truncation with a margin nobody will brute-force through a rate-limited endpoint.

Field shapes are enforced at mint time with the same regexes the rest of the stack uses, and
re-checked at verify time AFTER the digest comparison — nothing here branches on
attacker-controlled content until the signature has already proven it was minted by us.
"""
import base64
import hmac
import hashlib
import re
import time

VERSION = "1"
SIG_BYTES = 20                      # 160-bit truncated HMAC-SHA256
DEFAULT_MAX_AGE_S = 30 * 86400      # 30 days; see module docstring for why
SKEW_S = 300                        # tolerate this much clock disagreement, no more

# The stack's own shapes: job ids are hermes cron ids, handles are alert recipients.
_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_HANDLE_RE = re.compile(r"^[a-z0-9_-]+$")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sig(body, secret):
    return hmac.new(secret.encode(), body, hashlib.sha256).digest()[:SIG_BYTES]


def mint(job_id, handle, secret, now=None):
    """A token for this job and owner, or None when the inputs are not mintable.

    None rather than raising: the caller is the alert path, and a malformed handle must degrade
    to "email without a cancel link", never to an alert that failed to send.
    """
    if not (secret and job_id and handle):
        return None
    if not (_JOB_ID_RE.match(str(job_id)) and _HANDLE_RE.match(str(handle))):
        return None
    issued = int(now if now is not None else time.time())
    body = f"{VERSION}|{job_id}|{handle}|{issued}".encode()
    return f"{_b64(body)}.{_b64(_sig(body, secret))}"


def verify(token, secret, now=None, max_age_s=DEFAULT_MAX_AGE_S):
    """(job_id, handle, "ok") for a valid token, else (None, None, "bad"|"expired").

    "bad" and "expired" are distinct because the page they produce is different: an expired link
    gets "this link has expired, ask the assistant instead", a forged one gets nothing more
    specific than not-found. Everything structurally wrong is "bad" — the caller must not be
    able to distinguish a tampered signature from a garbled body from the response.
    """
    if not (token and secret):
        return None, None, "bad"
    try:
        body_b64, sig_b64 = str(token).split(".", 1)
        body, sig = _unb64(body_b64), _unb64(sig_b64)
    except Exception:
        return None, None, "bad"
    # Signature first, fields after: no parsing decisions on unauthenticated bytes.
    if not hmac.compare_digest(sig, _sig(body, secret)):
        return None, None, "bad"
    try:
        version, job_id, handle, issued = body.decode().split("|")
        issued = int(issued)
    except Exception:
        return None, None, "bad"
    if version != VERSION:
        return None, None, "bad"
    if not (_JOB_ID_RE.match(job_id) and _HANDLE_RE.match(handle)):
        return None, None, "bad"
    ts = now if now is not None else time.time()
    # A token from the future is as wrong as an old one: nothing legitimate mints ahead of the
    # clock, so past SKEW_S it can only be a minting oracle being probed or a badly broken host.
    if issued > ts + SKEW_S:
        return None, None, "bad"
    if ts - issued > max_age_s:
        return None, None, "expired"
    return job_id, handle, "ok"

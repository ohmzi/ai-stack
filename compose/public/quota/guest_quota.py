#!/usr/bin/env python3
"""Per-IP message quota for the public, no-account Ohmz AI. See docs/PUBLIC_INSTANCE.md.

WHY THIS EXISTS
---------------
The gate already carries `limit_req` on /api/chat/completions, but that is a RATE limit — 20
requests a minute, refilled every minute, forever. It stops a burst; it does nothing about someone
patiently holding a free GPU all day. This is the missing half: a hard ceiling on how many messages
one visitor gets per rolling window, after which they are asked (nicely) to make an account.

HOW IT'S WIRED
--------------
nginx does not proxy chat messages to this service — it ASKS it, via `auth_request`, and proxies to
OpenWebUI itself only if the answer is yes:

    POST /api/chat/completions ─► auth_request /_quota ─► this service, /check
                                        204 ─► proxied to open-webui-public as normal
                                        403 ─► error_page ─► this service, /limited

So the hot path (a guest under quota) costs one tiny subrequest and is otherwise untouched: nginx
still does all the streaming, and a bug in this file cannot corrupt a chat response — it can only
wrongly allow or wrongly deny.

/limited answers with a valid OpenAI-shaped SSE stream, which is what makes the limit notice render
as an ordinary assistant reply — markdown, signup link and all — instead of the red error toast a
bare 403 would produce. The frontend parser this has to satisfy is
`src/lib/apis/streaming/index.ts`: it reads `choices[0].delta.content` off each `data:` line and
stops at `data: [DONE]`.

KEYED ON IP, NOT ON THE GUEST COOKIE
------------------------------------
The ohmzgid cookie identifies a session, and clearing it is one click — a cookie quota would be
decorative. IP is the only identifier a casual abuser doesn't trivially control. Two consequences,
both accepted deliberately:
  - Visitors sharing a NAT (an office, a phone carrier) share a quota. For a personal demo that is
    the right trade; the alternative punishes nobody.
  - Anyone with a pool of addresses can still cycle them. This is a speed bump for casual abuse,
    not a defense against a determined attacker — Cloudflare's own WAF/rate limiting is the layer
    for that, and it sits in front of this.

IPs are never stored. The key is an HMAC of the address under a secret generated on first run and
kept on the data volume, so the table is a set of opaque digests: enough to count against, useless
as a record of who visited.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("GUEST_QUOTA_BIND", "0.0.0.0")
PORT = int(os.environ.get("GUEST_QUOTA_PORT", "9099"))
DB_PATH = os.environ.get("GUEST_QUOTA_DB", "/data/guest_quota.db")
SECRET_PATH = os.environ.get("GUEST_QUOTA_SECRET_FILE", "/data/quota_secret")
LIMIT = int(os.environ.get("GUEST_QUOTA_LIMIT", "20"))
WINDOW_S = int(float(os.environ.get("GUEST_QUOTA_WINDOW_HOURS", "24")) * 3600)
SIGNUP_URL = os.environ.get("GUEST_QUOTA_SIGNUP_URL", "https://ai.ohmz.cloud")
# The header nginx puts the real client address in ($client_ip in guest-gate.conf, which is
# CF-Connecting-IP when the request came through Cloudflare and $remote_addr otherwise).
IP_HEADER = os.environ.get("GUEST_QUOTA_IP_HEADER", "X-Ohmz-Client-IP")

_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute(
            "CREATE TABLE IF NOT EXISTS quota ("
            "  key TEXT PRIMARY KEY,"
            "  used INTEGER NOT NULL,"
            "  window_start INTEGER NOT NULL"
            ")"
        )
        _db.commit()
    return _db


def secret():
    """Generated once, on the data volume. Rotating it resets every quota — which is the intended
    emergency lever, not a bug: delete the file to forgive everyone at once."""
    try:
        with open(SECRET_PATH, "rb") as f:
            val = f.read().strip()
            if val:
                return val
    except FileNotFoundError:
        pass
    val = secrets.token_hex(32).encode()
    os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
    old = os.umask(0o077)
    try:
        with open(SECRET_PATH, "wb") as f:
            f.write(val)
    finally:
        os.umask(old)
    return val


_SECRET = None


def key_for(ip):
    global _SECRET
    if _SECRET is None:
        _SECRET = secret()
    return hmac.new(_SECRET, ip.encode(), hashlib.sha256).hexdigest()


def state(ip, consume):
    """Return (allowed, used, reset_in_seconds) for this address.

    consume=True is the /check path: it counts the message when it allows one. consume=False is the
    /limited path, which only needs to read the window back to say when it lifts — and must not
    charge a visitor for being told they've run out.
    """
    k = key_for(ip)
    now = int(time.time())
    with _lock:
        conn = db()
        row = conn.execute(
            "SELECT used, window_start FROM quota WHERE key = ?", (k,)
        ).fetchone()

        if row is None or now - row[1] >= WINDOW_S:
            used, window_start = 0, now
        else:
            used, window_start = row

        allowed = used < LIMIT
        if allowed and consume:
            used += 1
            conn.execute(
                "INSERT INTO quota (key, used, window_start) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET used = excluded.used, "
                "window_start = excluded.window_start",
                (k, used, window_start),
            )
            conn.commit()

        return allowed, used, max(0, window_start + WINDOW_S - now)


def human_duration(seconds):
    if seconds <= 90:
        return "in under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


def limit_message(reset_in):
    return (
        f"### You've reached the demo limit\n\n"
        f"That's all {LIMIT} messages for this session — thanks for giving Ohmz AI a real try.\n\n"
        f"The demo opens back up **{human_duration(reset_in)}**. "
        f"If you'd rather keep going now, [create an account]({SIGNUP_URL}) — it's free, and it "
        f"lifts the message cap entirely.\n\n"
        f"An account also unlocks what this demo deliberately leaves out: image and video "
        f"generation, web search, file uploads, and chats that are still here when you come back."
    )


def sse(content):
    """One content chunk, one stop chunk, then [DONE] — the minimum
    src/lib/apis/streaming/index.ts accepts as a complete assistant turn."""
    created = int(time.time())
    base = {"id": "guest-quota", "object": "chat.completion.chunk",
            "created": created, "model": "guest-quota"}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default per-request stderr line
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def client_ip(self):
        # Falls back to the socket peer, which inside the compose network is always the gate — i.e.
        # one shared bucket. That is the safe direction to fail: it throttles rather than exempts.
        return self.headers.get(IP_HEADER) or self.client_address[0]

    def route(self):
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            return self._send(200, b"ok\n")

        if path == "/check":
            allowed, used, reset_in = state(self.client_ip(), consume=True)
            headers = {
                "X-Quota-Limit": str(LIMIT),
                "X-Quota-Used": str(used),
                "X-Quota-Reset-In": str(reset_in),
            }
            # 204/403 are the only two codes nginx's auth_request treats as a verdict; anything
            # else it reports as a 500 and the request fails closed.
            return self._send(204 if allowed else 403, b"", extra=headers)

        if path == "/limited":
            _allowed, _used, reset_in = state(self.client_ip(), consume=False)
            return self._send(200, sse(limit_message(reset_in)),
                              ctype="text/event-stream; charset=utf-8")

        if path == "/stats":
            # Owner-facing, reachable only from inside the compose network — no nginx route points
            # here. Opaque keys only; see the module docstring.
            with _lock:
                rows = db().execute(
                    "SELECT key, used, window_start FROM quota ORDER BY used DESC LIMIT 50"
                ).fetchall()
            now = int(time.time())
            payload = {
                "limit": LIMIT,
                "window_hours": WINDOW_S / 3600,
                "active": [
                    {"key": k[:12], "used": u, "reset_in": max(0, ws + WINDOW_S - now)}
                    for k, u, ws in rows
                    if now - ws < WINDOW_S
                ],
            }
            return self._send(200, json.dumps(payload, indent=2).encode(),
                              ctype="application/json")

        return self._send(404, b"not found\n")

    def do_GET(self):
        self.route()

    # auth_request always issues a GET, but /check is also useful to POST by hand when testing.
    def do_POST(self):
        self.route()


def main():
    print(f"[guest-quota] {LIMIT} messages / {WINDOW_S / 3600:g}h per IP, db={DB_PATH}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

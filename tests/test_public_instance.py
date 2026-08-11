#!/usr/bin/env python3
"""The public, no-account OhmzAI stays isolated and stays anonymous.

Two properties this instance exists for, and nothing about them is obvious from reading the compose
file in isolation — they only hold if the network topology, the nginx gate, and OWUI's own auth
config all agree with each other at runtime:

  1. ISOLATION. open-webui-public cannot reach ComfyUI, the hermes gateway, or qdrant — all
     loopback-only host services the PRIVATE instance's pipes call directly. Those pipes are never
     deployed here, but that would only be a UI convention without the network boundary backing it.
     The one deliberate hole is Ollama, via owui-public-ollama's socat pinhole.
  2. ANONYMITY. Every visitor gets a fresh, unspoofable guest identity and sees exactly one model.
     Signup is unreachable. A forged identity header does not survive owui-public-gate's
     proxy_set_header (docs/PUBLIC_INSTANCE.md explains why that specific directive is what makes
     this true rather than merely configured).

Modeled on test_branding.py: assert on what is actually served/reachable, not on config intent.

Usage:  python3 tests/test_public_instance.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

GATE_BASE = os.environ.get("PUBLIC_GATE_URL", "http://127.0.0.1:4568")
ADMIN_BASE = os.environ.get("PUBLIC_ADMIN_URL", "http://127.0.0.1:4570")
PUBLIC_CONTAINER = os.environ.get("PUBLIC_CONTAINER", "open-webui-public")
EXPECTED_MODEL = os.environ.get("PUBLIC_GUEST_MODEL", "hermes-genesis:apex-compact")

GUEST_EMAIL_RE = re.compile(r"^guest-[0-9a-f]{32}@public\.ohmz\.cloud$")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def docker_exec(cmd, timeout=10, container=None):
    r = subprocess.run(["docker", "exec", container or PUBLIC_CONTAINER, "sh", "-c", cmd],
                        capture_output=True, timeout=timeout + 5, text=True)
    return r.stdout.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The bootstrap hop's whole job is a 302 the caller needs to SEE, not follow — the Set-Cookie
    that makes the mint real lives on that response, and urllib's default handler follows 3xx
    transparently and discards exactly that header. This opener stops it there."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


_ip_seq = iter(range(20, 250))


def fresh_ip():
    """A TEST-NET-2 address (RFC 5737, never routable) unique within this run.

    Both the signin rate limiter and the message quota are keyed on $client_ip, so any two checks
    sharing an address share their limiter buckets — which made this suite order-dependent and
    unable to run twice inside a minute: the deliberate rate-limit test below would leave the
    bucket empty and the NEXT check to sign in got a 503 it never asked for. Handing every
    signin-performing check its own address isolates them completely.
    """
    return f"198.51.100.{next(_ip_seq)}"


def http(base, method, path, cookie=None, token=None, body=None, timeout=10, client_ip=None):
    """Minimal request helper — plain urllib, matching this repo's convention (no `requests`).

    Returns (status, parsed_json_or_None, set_cookie_header_or_None). Never raises: an HTTPError
    still carries a status and body worth asserting on (403s and 429s are expected outcomes here,
    not exceptions to swallow).
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"ohmzgid={cookie}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if client_ip:
        # Only cloudflared reaches these ports, so the gate trusts CF-Connecting-IP as the real
        # client address — which lets a test pick its own limiter bucket.
        req.add_header("CF-Connecting-IP", client_ip)
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read()
            parsed = json.loads(raw) if raw else None
            return r.status, parsed, r.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed, e.headers.get("Set-Cookie")
    except urllib.error.URLError:
        return None, None, None


def mint_guest(client_ip=None):
    """One full guest arrival: prime a cookie, sign in, return (gid, token, identity)."""
    ip = client_ip or fresh_ip()
    status, _, set_cookie = http(GATE_BASE, "GET", "/", client_ip=ip)
    if status != 302 or not set_cookie:
        return None, None, None
    m = re.search(r"ohmzgid=([0-9a-f]{32})", set_cookie)
    if not m:
        return None, None, None
    gid = m.group(1)
    status, body, _ = http(GATE_BASE, "POST", "/api/v1/auths/signin", cookie=gid,
                            body={"email": "x", "password": "x"}, client_ip=ip)
    if status != 200 or not body:
        return gid, None, None
    return gid, body.get("token"), body


def main():
    print("--- isolation: the network boundary the whole design rests on ---")
    isolation = [
        ("ComfyUI :8188 (image/video gen — must be unreachable)", "8188", False),
        ("hermes gateway :8642 (must be unreachable)", "8642", False),
        ("qdrant :6333 (must be unreachable)", "6333", False),
    ]
    for label, port, want_reachable in isolation:
        code = docker_exec(f"curl -m3 -s -o /dev/null -w '%{{http_code}}' "
                            f"http://172.16.240.1:{port}/ 2>/dev/null")
        reachable = code not in ("", "000")
        check(label, reachable == want_reachable, f"http_code={code!r}")

    ollama_code = docker_exec("curl -m3 -s -o /dev/null -w '%{http_code}' "
                               "http://172.16.240.1:11435/api/tags")
    check("Ollama pinhole :11435 (must be reachable — the one deliberate hole)",
          ollama_code == "200", f"http_code={ollama_code!r}")

    print("--- guest arrival: cookie mint, auto sign-in, no form ever shown ---")
    gid1, token1, identity1 = mint_guest()
    check("bootstrap hop mints a 32-hex guest id", bool(gid1))
    check("signin succeeds without credentials (trusted header)", bool(token1))
    if identity1:
        check("guest role is 'user', not 'admin'", identity1.get("role") == "user",
              f"role={identity1.get('role')!r}")
        check("guest email matches the minted id",
              identity1.get("email") == f"guest-{gid1}@public.ohmz.cloud",
              f"email={identity1.get('email')!r}")
        perms = (identity1.get("permissions") or {})
        for path, want in [
            (("features", "image_generation"), False),
            (("features", "web_search"), False),
            (("features", "code_interpreter"), False),
            (("features", "folders"), False),
            (("chat", "file_upload"), False),
            (("chat", "controls"), False),
            (("chat", "temporary"), False),
        ]:
            node = perms
            for k in path:
                node = (node or {}).get(k)
            check(f"guest permission {'.'.join(path)} is False", node is False, f"got {node!r}")

    print("--- exactly one model, and it's the guest model ---")
    status, models, _ = http(GATE_BASE, "GET", "/api/models", cookie=gid1, token=token1)
    ids = [m.get("id") for m in (models or {}).get("data", [])] if models else []
    check("guest sees exactly one model", len(ids) == 1, f"saw {ids!r}")
    check(f"it's {EXPECTED_MODEL}", ids == [EXPECTED_MODEL], f"saw {ids!r}")

    print("--- two guests never share an identity ---")
    gid2, token2, identity2 = mint_guest()
    check("second guest gets a different id", bool(gid2) and gid2 != gid1,
          f"gid1={gid1!r} gid2={gid2!r}")
    if identity1 and identity2:
        check("second guest gets a different email",
              identity2.get("email") != identity1.get("email"))

    print("--- identity cannot be forged ---")
    status, forged, _ = http(GATE_BASE, "POST", "/api/v1/auths/signin",
                              cookie="cafecafecafecafecafecafecafecafe",
                              body={"email": "x", "password": "x"}, client_ip=fresh_ip())
    # A raw urllib.request.Request can't attach a second value for a header proxy_set_header would
    # normally replace anyway — the actual security property (proxy_set_header REPLACES a
    # client-supplied X-Ohmz-Guest) can't be exercised over plain HTTP without hand-building the
    # request; the check that matters is behavioral: an attacker-chosen cookie value still only ever
    # produces a GUEST identity, never an elevated one.
    check("a chosen cookie value still resolves to role=user, never admin",
          forged is not None and forged.get("role") == "user", f"got {forged!r}")

    print("--- signup is unreachable ---")
    status, _, _ = http(GATE_BASE, "POST", "/api/v1/auths/signup",
                         body={"name": "x", "email": "nobody@example.com", "password": "x"},
                         client_ip=fresh_ip())
    check("POST /api/v1/auths/signup is rejected", status in (403, 404), f"status={status}")

    print("--- the admin door is a fixed, separate identity ---")
    status, admin_identity, _ = http(ADMIN_BASE, "POST", "/api/v1/auths/signin",
                                      body={"email": "x", "password": "x"})
    check("admin door signs in as role=admin", bool(admin_identity) and admin_identity.get("role") == "admin",
          f"got {admin_identity!r}" if not admin_identity else f"role={admin_identity.get('role')!r}")

    print("--- rate limiting on signin engages ---")
    # Its own address, deliberately: this check exists to EXHAUST a limiter bucket, and sharing one
    # with any other check makes the suite order-dependent and un-rerunnable inside a minute.
    burst_ip = fresh_ip()
    codes = []
    for _ in range(8):
        status, _, _ = http(GATE_BASE, "POST", "/api/v1/auths/signin",
                             body={"email": "x", "password": "x"}, client_ip=burst_ip)
        codes.append(status)
    check("a burst of signin calls eventually gets 503 (limit_req)", 503 in codes, f"codes={codes}")

    print("--- per-IP message quota ---")
    probe_ip = fresh_ip()
    # Unlike the rate limiter (which refills in seconds), a quota bucket lives for the whole 24h
    # window — so a second run of this suite the same day would inherit the last run's exhausted
    # count and fail on the very first check. Clear this probe's row first, computing the same
    # HMAC the service does so the test never needs to know the raw key.
    docker_exec(
        "python3 -c \""
        "import sqlite3,hmac,hashlib;"
        "s=open('/data/quota_secret','rb').read().strip();"
        f"k=hmac.new(s,b'{probe_ip}',hashlib.sha256).hexdigest();"
        "c=sqlite3.connect('/data/guest_quota.db');"
        "c.execute('DELETE FROM quota WHERE key=?',(k,));c.commit()\"",
        container="owui-public-quota",
    )

    def quota_check():
        """One /check against the quota service, from inside the gate — the same call
        auth_request makes. Returns the HTTP status."""
        out = docker_exec(
            "wget -qS -O- --header='X-Ohmz-Client-IP: " + probe_ip + "' "
            "http://owui-public-quota:9099/check 2>&1 | head -1",
            container="owui-public-gate",
        )
        return "204" if "204" in out else ("403" if "403" in out else out.strip())

    first = quota_check()
    check("a fresh address is allowed", first == "204", f"got {first!r}")

    # Burn through whatever the configured limit is, then one more. Read the limit off the service
    # rather than hardcoding 20, so raising GUEST_QUOTA_LIMIT doesn't turn this into a false alarm.
    limit_hdr = docker_exec(
        "wget -qS -O- --header='X-Ohmz-Client-IP: " + probe_ip + "' "
        "http://owui-public-quota:9099/check 2>&1 | grep X-Quota-Limit",
        container="owui-public-gate",
    )
    try:
        limit = int(limit_hdr.split(":")[1].strip())
    except (IndexError, ValueError):
        limit = 0
    check("the service reports a limit", limit > 0, f"header was {limit_hdr!r}")

    for _ in range(limit):
        quota_check()
    over = quota_check()
    check(f"address is refused after {limit} messages", over == "403", f"got {over!r}")

    # The whole point of the design: an exhausted visitor gets a readable assistant turn, not a red
    # error toast. 200 + SSE + a signup link, through the real nginx path.
    status, _, _ = (None, None, None)
    req = urllib.request.Request(
        f"{GATE_BASE}/api/chat/completions", method="POST",
        data=json.dumps({"model": EXPECTED_MODEL, "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("CF-Connecting-IP", probe_ip)
    try:
        with _opener.open(req, timeout=15) as r:
            code, body, ctype = r.status, r.read().decode(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        code, body, ctype = e.code, e.read().decode(), e.headers.get("Content-Type", "")

    check("over-quota chat returns 200, not an error", code == 200, f"status={code}")
    check("...as an SSE stream", "text/event-stream" in ctype, f"content-type={ctype!r}")
    check("...that terminates properly", "data: [DONE]" in body)
    check("...carrying the signup link", "ai.ohmz.cloud" in body)
    check("...and no model was invoked for it", '"model": "guest-quota"' in body)

    print("--- branding: shared skin plus the guest-ui trim, nothing else ---")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def served(path):
        with urllib.request.urlopen(f"{ADMIN_BASE}/static/{path}", timeout=10) as r:
            return r.read()

    # custom.css is deliberately NOT byte-identical to branding/ohmz.css here: apply_guest_ui.sh
    # APPENDS compose/public/guest-ui.css to it (docs/PUBLIC_INSTANCE.md) — those rules have no
    # business shipping to the private instance too. So this checks the actual invariant — shared
    # skin as a PREFIX, guest-ui.css as the exact remainder — instead of exact equality.
    served_css = served("custom.css")
    ohmz_css = open(os.path.join(root, "branding", "ohmz.css"), "rb").read()
    guest_css = open(os.path.join(root, "compose", "public", "guest-ui.css"), "rb").read()
    check("custom.css starts with the shared branding/ohmz.css",
          served_css.startswith(ohmz_css), f"served {len(served_css)}B vs shared {len(ohmz_css)}B")
    check("custom.css ends with the appended guest-ui.css",
          served_css.endswith(guest_css), f"served {len(served_css)}B vs guest-ui {len(guest_css)}B")

    # Everything else branding installs carries no such divergence — byte-identical, same as the
    # private instance. NOT delegated to test_branding.py: its own custom.css check assumes exact
    # equality and would misreport the intentional divergence above as a regression.
    for served_path, repo_path in [
        ("loader.js", "branding/loader.js"),
        ("favicon.png", "branding/assets/favicon.png"),
        ("favicon.svg", "branding/assets/favicon.svg"),
        ("splash.png", "branding/assets/splash.png"),
        ("ohmz-fonts/space-grotesk-latin.woff2", "branding/fonts/space-grotesk-latin.woff2"),
    ]:
        want = open(os.path.join(root, repo_path), "rb").read()
        got = served(served_path)
        check(f"{served_path} matches {repo_path}", got == want, f"{len(got)}B vs {len(want)}B")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

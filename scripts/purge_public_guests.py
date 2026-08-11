#!/usr/bin/env python3
"""Reap idle guest accounts from the public OhmzAI instance.

Why this exists. Guest sessions on the public instance (docs/PUBLIC_INSTANCE.md) are fresh every
visit by design — closing the browser drops the ohmzgid cookie, so a returning visitor gets a brand
new identity rather than picking up an old one. That is a UX choice with a direct cost: every visit
mints a NEW user row (and its chats), so the public instance's database grows strictly with traffic,
never with distinct visitors. Nothing else prunes it.

Authenticates through the admin door (owui-public-gate's :81, docs/PUBLIC_INSTANCE.md) — loopback
only, no password, no Cloudflare route — the same way any other script would administer an instance
whose login form is permanently disabled (ENABLE_LOGIN_FORM=false).

Deletion goes through OWUI's own DELETE /api/v1/users/{id}, not direct sqlite surgery: that endpoint
is what actually cascades a user's chats and sessions (Auths.delete_auth_by_id), and it independently
refuses to touch the primary admin or the caller's own account — a second, structural guard against
ever deleting the owner by mistake, on top of the email-shape filter below.

Usage:  python3 scripts/purge_public_guests.py                 # dry run, default 24h cutoff
        python3 scripts/purge_public_guests.py --yes            # actually delete
        python3 scripts/purge_public_guests.py --max-age-hours 6 --yes
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

ADMIN_BASE = "http://127.0.0.1:4570"
PAGE_SIZE = 30  # matches PAGE_ITEM_COUNT in open_webui/routers/users.py
TIMEOUT = 10

# The exact shape owui-public-gate's guest-gate.conf mints: guest-<32 hex>@public.ohmz.cloud.
# Deliberately narrow — anything that doesn't match this exactly is left alone, on the theory that
# a purge script's failure mode should be "misses a guest" (annoying) never "deletes something that
# wasn't a guest" (destructive).
_GUEST_EMAIL_RE = re.compile(r"^guest-[0-9a-f]{32}@public\.ohmz\.cloud$")


def _request(method, path, token=None, body=None):
    url = f"{ADMIN_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def admin_token():
    # The admin door supplies the identity itself (owui-public-gate hardcodes X-Ohmz-Guest to the
    # owner's email on :81) — the email/password body below is required by SigninForm's schema but
    # never actually consulted on the trusted-header path.
    try:
        resp = _request("POST", "/api/v1/auths/signin", body={"email": "x", "password": "x"})
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach the admin door at {ADMIN_BASE}: {e}\n"
                 f"is the owui-public stack up? (compose/public/up.sh)")
    if resp.get("role") != "admin":
        sys.exit(f"admin door signed in as role={resp.get('role')!r}, not admin — "
                 f"check compose/public/nginx/_admin_proxy.snippet's X-Ohmz-Guest value "
                 f"matches the bootstrapped owner account (docs/PUBLIC_INSTANCE.md)")
    return resp["token"]


def all_guest_users(token):
    """Page through every user and yield the ones matching the guest email shape."""
    page = 1
    seen = 0
    total = None
    while total is None or seen < total:
        resp = _request(
            "GET",
            f"/api/v1/users/?page={page}&order_by=last_active_at&direction=asc",
            token=token,
        )
        users = resp["users"]
        total = resp["total"]
        if not users:
            break
        for u in users:
            seen += 1
            if _GUEST_EMAIL_RE.match(u["email"]):
                yield u
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                     help="delete guest accounts idle longer than this (default: 24)")
    ap.add_argument("--yes", action="store_true",
                     help="actually delete — without this, only prints what would be deleted")
    args = ap.parse_args()

    token = admin_token()
    cutoff = time.time() - args.max_age_hours * 3600

    stale = [u for u in all_guest_users(token) if u["last_active_at"] < cutoff]

    if not stale:
        print(f"no guest accounts idle > {args.max_age_hours}h")
        return

    for u in stale:
        idle_h = (time.time() - u["last_active_at"]) / 3600
        tag = "DELETE" if args.yes else "would delete"
        print(f"{tag}: {u['email']}  (idle {idle_h:.1f}h, id={u['id']})")
        if args.yes:
            try:
                _request("DELETE", f"/api/v1/users/{u['id']}", token=token)
            except urllib.error.HTTPError as e:
                print(f"  failed: {e.code} {e.reason}", file=sys.stderr)

    print(f"\n{len(stale)} guest account(s) {'deleted' if args.yes else 'found'}"
          f"{'' if args.yes else ' (pass --yes to delete)'}")


if __name__ == "__main__":
    main()

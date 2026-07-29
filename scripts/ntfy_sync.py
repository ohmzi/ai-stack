#!/usr/bin/env python3
"""Mirror OpenWebUI accounts into ntfy — same username, same password, zero plaintext.

Why this exists. Every OpenWebUI user should be able to receive phone alerts from their own
background jobs with the credentials they already have. OpenWebUI stores passwords as bcrypt
hashes, so the plaintext is unrecoverable by design — but ntfy verifies bcrypt too, so the sync
mirrors the HASH itself. Nobody's password ever exists in plaintext anywhere in this pipeline,
including here.

Mechanism: ntfy v2.26 supports declarative provisioning (NTFY_AUTH_USERS / NTFY_AUTH_ACCESS /
NTFY_AUTH_TOKENS), applied at server start and reconciled against the auth database — provisioned
entries are updated and removed to match the config. So the sync never touches ntfy's database:
it reads OpenWebUI's user table (read-only URI, WAL-safe), renders compose/ntfy/provision.env,
and recreates the ntfy container ONLY when the rendered file actually changed. A password change
in OpenWebUI propagates on the next timer tick (systemd user timer, every 2 minutes); an
unchanged world is a no-op that touches nothing.

What each OpenWebUI user gets:
  * an ntfy account with the same username (derived from their email's local part — see
    derive_username, which the auto_assistant pipe duplicates and tests assert stays identical)
    and the same password (mirrored hash);
  * READ-ONLY access to exactly one topic, alerts-<username>. Users subscribe; only the service
    account publishes.

The service account (hermes-bot) is provisioned with WRITE-ONLY access to alerts-* and a static
token. The token and the base URL live in ~/.hermes/ntfy_alert (line 1: base URL, line 2: token),
which is what cron jobs read to deliver pushes. hermes-bot's password is a random throwaway,
hashed once and cached in compose/ntfy/.bot_secret — it exists only because ntfy users need one.

Excluded from provisioning: OpenWebUI users with role 'pending' (not yet approved) and
deactivated accounts. Removed OpenWebUI users disappear from the env file and ntfy reconciles
them away on the next restart.

Usage:  python3 scripts/ntfy_sync.py [--dry-run]
"""
import argparse
import os
import re
import secrets
import sqlite3
import subprocess
import sys

OWUI_DB = os.environ.get("OWUI_DB", "/volume1/docker/openwebui/config/webui.db")
STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROVISION_ENV = os.path.join(STACK, "compose", "ntfy", "provision.env")
BOT_SECRET = os.path.join(STACK, "compose", "ntfy", ".bot_secret")
ALERT_FILE = os.path.expanduser("~/.hermes/ntfy_alert")
NTFY_BASE_LOCAL = "http://127.0.0.1:8300"
BOT = "hermes-bot"
RESERVED = {BOT, "everyone", "anonymous", "*"}


def derive_username(email, name=""):
    """OpenWebUI identity -> ntfy username. MUST stay in lockstep with the copy in
    pipes/live/auto_assistant.py (_ntfy_username) — tests/test_ntfy_sync.py asserts equality."""
    local = (email or "").split("@")[0] or (name or "")
    uname = re.sub(r"[^a-z0-9_-]", "", local.lower())
    return uname or "user"


def owui_users():
    """Active, approved users with their bcrypt hashes, oldest first (stable collision order)."""
    db = sqlite3.connect(f"file:{OWUI_DB}?mode=ro", uri=True)
    rows = db.execute(
        """select u.id, u.name, u.email, u.role, u.created_at, a.password
           from user u join auth a on a.id = u.id
           where a.active = 1 and u.role in ('user', 'admin')
           order by u.created_at""").fetchall()
    db.close()
    return rows


def assign_usernames(rows):
    """Unique usernames; collisions get -2, -3 … by account age, so an existing user's
    username never changes when a newer clashing account appears."""
    taken, out = set(RESERVED), []
    for _id, name, email, _role, _created, pw_hash in rows:
        base = derive_username(email, name)
        uname, n = base, 2
        while uname in taken:
            uname, n = f"{base}-{n}", n + 1
        taken.add(uname)
        out.append((uname, pw_hash))
    return out


def bot_credentials():
    """Static hash + token for the service account, generated once and cached."""
    if os.path.exists(BOT_SECRET):
        with open(BOT_SECRET) as f:
            pw_hash, token = f.read().split()
        return pw_hash, token
    try:
        import bcrypt
        pw_hash = bcrypt.hashpw(secrets.token_urlsafe(24).encode(), bcrypt.gensalt()).decode()
    except ImportError:
        # The ntfy binary can hash for us; the throwaway plaintext never leaves this pipe.
        pw = secrets.token_urlsafe(24)
        pw_hash = subprocess.run(
            ["docker", "exec", "-i", "ntfy", "ntfy", "user", "hash"],
            input=pw, capture_output=True, text=True, check=True).stdout.strip()
    token = "tk_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789")
                            for _ in range(29))
    with open(BOT_SECRET, "w") as f:
        f.write(f"{pw_hash} {token}\n")
    os.chmod(BOT_SECRET, 0o600)
    return pw_hash, token


def render(users, bot_hash, bot_token):
    """The provision.env content. Values are comma-joined lists; bcrypt hashes contain '$' and
    ':' never appears in them, so the user:hash:role triplet parses unambiguously."""
    auth_users = [f"{u}:{h}:user" for u, h in users] + [f"{BOT}:{bot_hash}:user"]
    auth_access = [f"{u}:alerts-{u}:read-only" for u, _ in users] + [f"{BOT}:alerts-*:write-only"]
    auth_tokens = [f"{BOT}:{bot_token}"]
    # docker compose INTERPOLATES $ inside env_file values (observed live: it ate the third $
    # of a bcrypt hash and ntfy crash-looped on "hashedSecret too short"). $$ escapes to a
    # literal $ — applied to every value, since bcrypt hashes are $-delimited by definition.
    def esc(v):
        return v.replace("$", "$$")
    return ("# GENERATED by scripts/ntfy_sync.py — do not edit; mirrors OpenWebUI accounts.\n"
            f"NTFY_AUTH_USERS={esc(','.join(auth_users))}\n"
            f"NTFY_AUTH_ACCESS={esc(','.join(auth_access))}\n"
            f"NTFY_AUTH_TOKENS={esc(','.join(auth_tokens))}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    users = assign_usernames(owui_users())
    bot_hash, bot_token = bot_credentials()
    content = render(users, bot_hash, bot_token)

    old = open(PROVISION_ENV).read() if os.path.exists(PROVISION_ENV) else None
    if a.dry_run:
        print(f"would provision {len(users)} user(s) + {BOT}; "
              f"{'UNCHANGED' if content == old else 'CHANGED'}")
        for u, _ in users:
            print(f"  {u:24} topic alerts-{u} (read-only)")
        return 0

    # Keep the job-delivery file current regardless of change state (idempotent).
    with open(ALERT_FILE, "w") as f:
        f.write(f"{NTFY_BASE_LOCAL}\n{bot_token}\n")
    os.chmod(ALERT_FILE, 0o600)

    if content == old:
        print(f"in sync: {len(users)} user(s), no restart")
        return 0

    tmp = PROVISION_ENV + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PROVISION_ENV)

    # Recreate ntfy so provisioning reconciles. Compose only recreates on actual config change,
    # which is exactly the semantics wanted here.
    r = subprocess.run(["docker", "compose", "up", "-d", "ntfy"],
                       cwd=os.path.join(STACK, "compose"), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"compose failed: {r.stderr[-300:]}", file=sys.stderr)
        return 1
    print(f"provisioned {len(users)} user(s) + {BOT}; ntfy reconciled")
    return 0


if __name__ == "__main__":
    sys.exit(main())

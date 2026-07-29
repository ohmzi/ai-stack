#!/usr/bin/env python3
"""The OpenWebUI -> ntfy account mirror: provisioning, escaping, and the drift that must not happen.

Why this file exists. Multi-user phone alerts rest on three load-bearing claims, each of which
failed silently in a different way during development or would if it regressed:

1. **Username derivation is duplicated** — scripts/ntfy_sync.py provisions accounts and topic ACLs
   from one copy, pipes/live/auto_assistant.py addresses per-user topics and writes onboarding
   instructions from another. If they drift, jobs push to a topic nobody is subscribed to and the
   instructions name an account that does not exist. Both failures are invisible: no error anywhere,
   just alerts that never arrive. This test runs both implementations over the same inputs.

2. **docker compose interpolates $ inside env_file values.** Observed live: the third $ of a bcrypt
   hash was eaten and ntfy crash-looped on "hashedSecret too short". render() must escape every $
   as $$ — asserted by round-tripping the rendered file through compose's unescaping rule.

3. **Collision suffixes must be stable by account age** — alice@a.com and alice@b.com become alice
   and alice-2 in creation order, and a NEW clashing signup must never rename an EXISTING user
   (that would strand their phone subscription).

No docker, no live databases — pure functions over fixtures, deterministic.

Usage:  python3 tests/test_ntfy_sync.py
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
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    sync = load("/home/ohmz/ai-stack/scripts/ntfy_sync.py", "sync_t")
    pipe_mod = load("/home/ohmz/ai-stack/pipes/live/auto_assistant.py", "pipe_t")
    Pipe = pipe_mod.Pipe

    print("--- the two username derivations are the same function ---")
    CASES = [
        ("ohmz@ohmz.com", "ohmz"),
        ("Omar.Iqbal97@gmail.com", "Omar"),
        ("weird+tag@example.com", ""),
        ("UPPER_case-Name@x.io", ""),
        ("@nodomain", "Fallback Name"),
        ("", ""),
        ("dots.and.spaces @x", "y"),
    ]
    for email, name in CASES:
        a = sync.derive_username(email, name)
        b = Pipe._ntfy_username({"email": email, "name": name})
        check(f"{email!r}/{name!r} -> {a!r}", a == b, f"pipe said {b!r}")

    print("--- derived names are valid ntfy usernames ---")
    for email, name in CASES:
        u = sync.derive_username(email, name)
        check(f"{u!r} matches [a-z0-9_-]+", bool(re.fullmatch(r"[a-z0-9_-]+", u)))

    print("--- collision handling: stable, age-ordered, reserved names avoided ---")
    rows = [  # (id, name, email, role, created_at, hash) in creation order
        ("1", "Alice A", "alice@a.com", "admin", 100, "$2b$12$aaa"),
        ("2", "Alice B", "alice@b.com", "user", 200, "$2b$12$bbb"),
        ("3", "Bot Wannabe", "hermes-bot@x.com", "user", 300, "$2b$12$ccc"),
    ]
    assigned = sync.assign_usernames(rows)
    names = [u for u, _ in assigned]
    check("older alice keeps the bare name", names[0] == "alice", str(names))
    check("newer alice gets -2", names[1] == "alice-2", str(names))
    check("'hermes-bot' is reserved -> suffixed", names[2] == "hermes-bot-2", str(names))
    assigned2 = sync.assign_usernames(
        rows + [("4", "Alice C", "alice@c.com", "user", 400, "$2b$12$ddd")])
    check("existing names unchanged when a new clash appears",
          [u for u, _ in assigned2][:3] == names, str(assigned2))

    print("--- render(): compose's $-interpolation is escaped, and round-trips ---")
    users = [("alice", "$2b$12$abc/DEF.ghi"), ("bob", "$2a$10$xyz")]
    env = sync.render(users, "$2a$10$bothash", "tk_abcdefghijklmnopqrstuvwxyz123")
    check("no bare $ survives in values",
          all("$" not in line.replace("$$", "")
              for line in env.splitlines() if not line.startswith("#")), env)
    unescaped = env.replace("$$", "$")
    check("round-trip restores the exact hash", "alice:$2b$12$abc/DEF.ghi:user" in unescaped)
    check("bot gets write-only on the wildcard", "hermes-bot:alerts-*:write-only" in unescaped)
    check("users get read-only on their own topic", "alice:alerts-alice:read-only" in unescaped)
    check("token line present", "hermes-bot:tk_abcdefghijklmnopqrstuvwxyz123" in unescaped)

    print("--- onboarding footer names the same topic the ACL grants ---")
    foot = Pipe._ntfy_onboarding("alice")
    check("footer names alerts-alice", "`alerts-alice`" in foot)
    check("footer names the public server", "notify.ohmz.cloud" in foot)
    check("footer says OpenWebUI password", "OpenWebUI password" in foot)
    check("footer warns: username, not email", "NOT your email" in foot)
    check("footer warns: set the server first", "wrong server" in foot)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

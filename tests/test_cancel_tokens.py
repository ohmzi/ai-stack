#!/usr/bin/env python3
"""The manage token: forgery, expiry, and the shapes it refuses to mint.

Why this file exists. The cancel link has no login behind it — the token IS the authorization, so
the properties pinned here are the security model, not details of it:

  * **Only we can mint.** Any bit flipped anywhere in the token, any wrong secret, any structural
    mangling must verify as "bad". There is exactly one way to a valid token: our secret.
  * **Old capabilities die.** A token found in a forwarded mailbox months later must be "expired",
    and one from the future (a probe, or a broken clock) must be "bad".
  * **Malformed input degrades, never raises.** mint() runs on the alert path, where an exception
    means an alert that never sends; verify() runs on a public endpoint, where an exception is a
    crash an attacker can drive.

Usage:  python3 tests/test_cancel_tokens.py
"""
import importlib.util
import sys

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
JOB, HANDLE, NOW = "ae57d3973b9f", "ohmz", 1_800_000_000


def main():
    ct = load("/home/ohmz/ai-stack/scripts/cancel_tokens.py", "ct")

    print("--- round trip ---")
    tok = ct.mint(JOB, HANDLE, SECRET, now=NOW)
    check("mints a token", bool(tok), repr(tok))
    jid, who, why = ct.verify(tok, SECRET, now=NOW + 3600)
    check("verifies to the same job", jid == JOB, f"{jid} {why}")
    check("...and the same handle", who == HANDLE, who)
    check("...as ok", why == "ok", why)
    check("token is URL-safe", all(c.isalnum() or c in "-_." for c in tok), tok)
    check("token fits a query string", len(tok) < 120, str(len(tok)))

    print("--- expiry ---")
    ok29 = ct.verify(tok, SECRET, now=NOW + 29 * 86400)
    ok31 = ct.verify(tok, SECRET, now=NOW + 31 * 86400)
    check("29 days old still verifies", ok29[2] == "ok", ok29[2])
    check("31 days old is expired, not bad", ok31 == (None, None, "expired"), str(ok31))
    check("expiry override is honoured",
          ct.verify(tok, SECRET, now=NOW + 2 * 86400, max_age_s=86400)[2] == "expired")
    check("a token from the future is bad, not expired",
          ct.verify(tok, SECRET, now=NOW - 3600) == (None, None, "bad"))
    check("small clock skew is tolerated", ct.verify(tok, SECRET, now=NOW - 100)[2] == "ok")

    print("--- forgery: every mangling verifies as bad ---")
    body_b64, sig_b64 = tok.split(".")
    manglings = {
        "flipped char in body": ("A" if body_b64[3] != "A" else "B").join(
            [body_b64[:3], body_b64[4:]]) + "." + sig_b64,
        "flipped char in sig": body_b64 + "." + ("A" if sig_b64[3] != "A" else "B").join(
            [sig_b64[:3], sig_b64[4:]]),
        "truncated": tok[:-4],
        "missing dot": tok.replace(".", ""),
        "empty": "",
        "just a dot": ".",
        "non-b64 garbage": "not!!a**token.%%%",
        "doubled token": tok + "." + tok,
    }
    for label, bad in manglings.items():
        got = ct.verify(bad, SECRET, now=NOW)
        check(label, got == (None, None, "bad"), str(got))
    check("wrong secret", ct.verify(tok, "another-secret", now=NOW) == (None, None, "bad"))
    check("None token", ct.verify(None, SECRET, now=NOW) == (None, None, "bad"))
    check("empty secret", ct.verify(tok, "", now=NOW) == (None, None, "bad"))

    print("--- a forged body with a valid-format signature still dies ---")
    # Re-sign a different job id with the WRONG secret: structure is perfect, signature is not.
    forged = ct.mint(JOB, HANDLE, "attacker-secret", now=NOW)
    check("attacker-minted token fails", ct.verify(forged, SECRET, now=NOW)[2] == "bad")

    print("--- mint refuses shapes the stack would never produce ---")
    for label, jid, who in [
        ("short job id", "abc123", HANDLE),
        ("uppercase job id", "AE57D3973B9F", HANDLE),
        ("path-shaped job id", "../etc/passwd", HANDLE),
        ("handle with a space", JOB, "oh mz"),
        ("handle with a slash", JOB, "oh/mz"),
        ("empty handle", JOB, ""),
        ("empty job id", "", HANDLE),
    ]:
        check(label + " -> None", ct.mint(jid, who, SECRET, now=NOW) is None)
    check("no secret -> None", ct.mint(JOB, HANDLE, "", now=NOW) is None)

    print("--- version pinning ---")
    # A hand-built v2 body signed with the real secret must not verify under v1 rules.
    body = f"2|{JOB}|{HANDLE}|{NOW}".encode()
    v2 = f"{ct._b64(body)}.{ct._b64(ct._sig(body, SECRET))}"
    check("future version is bad (until code learns it)",
          ct.verify(v2, SECRET, now=NOW) == (None, None, "bad"))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return fails


if __name__ == "__main__":
    sys.exit(main())

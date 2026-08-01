#!/usr/bin/env python3
"""The skin must actually be SERVED — and must survive a container restart.

Why this file exists. On 2026-08-01 a plain `docker restart open-webui` silently reverted the
skin, and nothing noticed for hours because every check anyone would think to run still passed:

    GET /static/custom.css  ->  HTTP 200        (but ZERO BYTES)
    GET /static/loader.js   ->  HTTP 200        (but ZERO BYTES)
    index.html              ->  still fingerprinted ?v=69bf3ed939

A half-state: the shell asking for branded URLs that resolve to empty files. Status codes cannot
see it. Only length can.

The cause was structural, not a typo. config.py:96-115 runs at import — on EVERY container start —
and it unlinks every top-level file in STATIC_DIR, then copies /app/build/static/**/* over it.
apply.sh had been writing the served dir only, because an earlier reading of the code concluded
/app/build/static was "a leftover served to nobody". It is in fact the source of truth at startup.
apply.sh now writes both, so the rebuild reproduces the brand instead of undoing it.

These checks assert BYTES SERVED against the repo files — the assertion that would have caught it.

Usage:  python3 tests/test_branding.py [--restart]
        --restart also bounces the container and re-checks (the regression itself; ~20 s).
"""
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("OWUI_URL", "http://127.0.0.1:4567")
CONTAINER = os.environ.get("OWUI_CONTAINER", "open-webui")

# served path -> repo file it must be byte-identical to
SERVED = {
    "custom.css": "branding/ohmz.css",
    "loader.js": "branding/loader.js",
    "favicon.png": "branding/assets/favicon.png",
    "favicon.svg": "branding/assets/favicon.svg",
    "splash.png": "branding/assets/splash.png",
    "ohmz-fonts/space-grotesk-latin.woff2": "branding/fonts/space-grotesk-latin.woff2",
}

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def fetch(path):
    try:
        with urllib.request.urlopen(f"{BASE}/static/{path}", timeout=10) as r:
            return r.read()
    except Exception:
        return None


def verify(stage):
    for served, repo in SERVED.items():
        want = open(os.path.join(ROOT, repo), "rb").read()
        got = fetch(served)
        n = len(got) if got is not None else -1
        # Length first, then bytes: a 0-byte 200 is the actual failure mode, and saying so
        # explicitly is more useful than "content differs".
        check(f"{stage}: {served} is served with content ({n} bytes)", n > 0, f"got {n}")
        check(f"{stage}: {served} matches {repo}", got == want,
              f"{n} bytes served vs {len(want)} in repo")


def main():
    if fetch("custom.css") is None:
        print(f"OpenWebUI not reachable at {BASE} — nothing to check")
        return 0

    print("--- served assets match the repo ---")
    verify("live")

    print("--- the shell asks for what is actually there ---")
    try:
        with urllib.request.urlopen(BASE, timeout=10) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        html = ""
        check("index.html reachable", False, str(e))
    if html:
        import re
        stamped = re.findall(r"/static/([A-Za-z0-9._-]+)\?v=([A-Za-z0-9]+)", html)
        check("index.html carries fingerprints", bool(stamped), "none found")
        # Every fingerprinted URL must resolve to something non-empty. This is the exact
        # cross-check that was missing: fingerprints present + assets empty = the half-state.
        empty = [n for n, _v in stamped if not (fetch(n) or b"")]
        check("no fingerprinted asset is empty", not empty, f"empty: {empty}")

    if "--restart" in sys.argv:
        print(f"--- restarting {CONTAINER} (the regression) ---")
        subprocess.run(["docker", "restart", CONTAINER], capture_output=True, timeout=180)
        for _ in range(60):
            if fetch("custom.css") is not None:
                break
            subprocess.run(["sleep", "2"])
        verify("after restart")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

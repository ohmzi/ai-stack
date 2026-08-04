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

The second half checks the SHELL, which is a different failure surface with the same shape:
correct-looking files that no browser is actually reading.

    <link rel="manifest" href="/manifest.json">    the BACKEND route, not our static manifest.
                                                   It returns {"name": "Open WebUI"}, which is
                                                   what named every Android/iOS home-screen
                                                   shortcut. loader.js cannot reach it — a browser
                                                   fetches a manifest itself, not via window.fetch.
    ?v=<stamp>                                     computed from three files, so changing the MARK
                                                   left every icon URL identical and neither
                                                   Cloudflare nor Chrome's favicon store ever
                                                   refetched. A new logo simply did not appear.

Both were invisible to a bytes-served check: the files were right, nothing asked for them.

Usage:  python3 tests/test_branding.py [--restart]
        --restart also bounces the container and re-checks (the regression itself; ~20 s).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("OWUI_URL", "http://127.0.0.1:4567")
CONTAINER = os.environ.get("OWUI_CONTAINER", "open-webui")

BRAND = "OhmzAI"

# served path -> repo file it must be byte-identical to
SERVED = {
    "custom.css": "branding/ohmz.css",
    "loader.js": "branding/loader.js",
    "favicon.png": "branding/assets/favicon.png",
    "favicon.svg": "branding/assets/favicon.svg",
    "splash.png": "branding/assets/splash.png",
    "ohmz-fonts/space-grotesk-latin.woff2": "branding/fonts/space-grotesk-latin.woff2",
}

# site.webmanifest is NOT in SERVED: apply.sh stamps the icon URLs inside it, so the served copy
# is deliberately not byte-identical to the repo. It is checked as parsed JSON instead, which is
# the more useful assertion anyway — what a browser does with it, not what it weighs.

# Exactly the files apply.sh hashes into the stamp, in the same order. Duplicated on purpose:
# this list IS the invariant. The stamp used to cover three files while eleven carried it, which
# is what let a changed mark ship a URL no cache had any reason to refetch. If the two ever drift
# apart again, this pins it.
STAMPED = ["branding/ohmz.css", "branding/loader.js"] + [
    "branding/assets/" + a for a in (
        "favicon.svg", "favicon.png", "favicon-dark.png", "favicon-96x96.png", "favicon.ico",
        "apple-touch-icon.png", "logo.png", "splash.png", "splash-dark.png",
        "web-app-manifest-192x192.png", "web-app-manifest-512x512.png", "site.webmanifest",
    )
]

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def fetch(path):
    return fetch_abs(f"/static/{path}")[0]


def fetch_abs(path):
    """Fetch an absolute site path. Returns (bytes, content-type) or (None, None)."""
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
            return r.read(), r.headers.get("content-type", "")
    except Exception:
        return None, None


CHUNKS = "/app/build/_app/immutable/chunks"


def docker_sh(cmd):
    r = subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", cmd],
                       capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def png_size(b):
    """(w, h) from a PNG's IHDR — 8-byte signature, 4 length, 4 type, then two big-endian u32.

    Worth checking because the stock manifest declared its 512x512 logo as 500x500, and Chrome
    silently DROPS a manifest icon whose declared size does not match the file. An icon that is
    present, correct and ignored looks exactly like one that works.
    """
    if not b or len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")


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


def verify_shell(stage):
    html, _ = fetch_abs("/")
    if html is None:
        check(f"{stage}: index.html reachable", False)
        return
    html = html.decode("utf-8", "replace")

    stamped = re.findall(r"/static/([A-Za-z0-9._-]+)\?v=([A-Za-z0-9]+)", html)
    check(f"{stage}: index.html carries fingerprints", bool(stamped), "none found")
    # Every fingerprinted URL must resolve to something non-empty. This is the exact
    # cross-check that was missing: fingerprints present + assets empty = the half-state.
    empty = [n for n, _v in stamped if not (fetch(n) or b"")]
    check(f"{stage}: no fingerprinted asset is empty", not empty, f"empty: {empty}")

    # The stamp must be the hash of everything that carries it — see STAMPED. A stamp that
    # does not move when an asset does is a stamp that ships nothing.
    h = hashlib.sha256()
    for rel in STAMPED:
        h.update(open(os.path.join(ROOT, rel), "rb").read())
    want_stamp = h.hexdigest()[:10]
    got = {v for _n, v in stamped}
    check(f"{stage}: stamp covers every fingerprinted file", got == {want_stamp},
          f"shell carries {sorted(got)}, repo hashes to {want_stamp} — re-run branding/apply.sh")

    # --- the shell's own branding, none of which /static can reach ---
    title = re.search(r"<title>([^<]*)</title>", html)
    check(f"{stage}: <title> is {BRAND}", bool(title) and title.group(1) == BRAND,
          f"got {title.group(1)!r}" if title else "no <title>")

    ios = re.search(r'<meta name="apple-mobile-web-app-title" content="([^"]*)"', html)
    check(f"{stage}: iOS home-screen name is {BRAND}", bool(ios) and ios.group(1) == BRAND,
          f"got {ios.group(1)!r}" if ios else "tag absent — iOS falls back to the document title")

    # The meta tag alone is not enough: the inline anti-FOUC script setAttribute()s theme-color
    # from its own hardcoded table a frame later, so a stock value surviving anywhere in the
    # shell means the branded one is overwritten on load.
    check(f"{stage}: no stock theme-colour left in the shell", "#171717" not in html,
          "'#171717' still present — the anti-FOUC script will overwrite the meta tag")

    # --- the manifest: what actually names a home-screen shortcut ---
    link = re.search(r'<link rel="manifest" href="([^"]*)"', html)
    href = link.group(1) if link else ""
    check(f"{stage}: shell does not point at the backend /manifest.json",
          href.split("?")[0] == "/static/site.webmanifest",
          f"points at {href!r} — that route is built from WEBUI_NAME and says 'Open WebUI'")
    if not href:
        return

    raw, ctype = fetch_abs(href)
    check(f"{stage}: manifest is served", bool(raw), "not reachable")
    check(f"{stage}: manifest content-type is a manifest type",
          "manifest+json" in (ctype or "") or "json" in (ctype or ""), f"got {ctype!r}")
    if not raw:
        return
    try:
        man = json.loads(raw)
    except Exception as e:
        check(f"{stage}: manifest parses as JSON", False, str(e))
        return
    check(f"{stage}: manifest parses as JSON", True)
    check(f"{stage}: manifest name is {BRAND}", man.get("name") == BRAND, f"got {man.get('name')!r}")
    check(f"{stage}: manifest short_name is {BRAND}", man.get("short_name") == BRAND,
          f"got {man.get('short_name')!r}")

    icons = man.get("icons") or []
    check(f"{stage}: manifest declares icons", bool(icons))
    for icon in icons:
        src = icon.get("src", "")
        name = urllib.parse.urlparse(src).path.rsplit("/", 1)[-1]
        blob, _ = fetch_abs(src)
        check(f"{stage}: manifest icon {name} is served with content",
              bool(blob), f"{src} -> {'missing' if blob is None else '0 bytes'}")
        # Declared size must match the file, or Chrome drops the icon without a word.
        size, declared = png_size(blob), icon.get("sizes", "")
        if size and declared:
            check(f"{stage}: manifest icon {name} really is {declared}",
                  declared == f"{size[0]}x{size[1]}", f"declared {declared}, file is {size[0]}x{size[1]}")

    # The site root, for everything that ignores <link> tags and just asks for /favicon.*.
    ico, ctype = fetch_abs("/favicon.ico")
    check(f"{stage}: /favicon.ico is an icon, not the SPA shell",
          bool(ico) and "html" not in (ctype or ""),
          f"got {ctype!r} — the catch-all is answering, so consumers of it get HTML")


def verify_copy(stage):
    """The UI copy that still said WebUI.

    These are i18n KEYS compiled into the frontend, resolved through a dynamically imported
    chunk — so they are reachable neither by loader.js (import() does not go through
    window.fetch) nor by anything under /static. branding/i18n_brand.py fills in the en-US
    values, which is the lever i18next already provides: English ships as "" and falls back to
    the key, so a non-empty value is what renders.

    Checked against the SERVED chunk, and located the same way the app locates it — through the
    locale registry, never by filename, because Vite content-hashes those on every build.
    """
    listing = docker_sh(f"grep -l 'locales/en-US/translation.json' {CHUNKS}/*.js || true")
    name = None
    for path in listing.split():
        if path.endswith(".map"):
            continue
        m = re.search(r'locales/en-US/translation\.json".{0,160}?import\("\./([^"]+\.js)"',
                      docker_sh(f"cat {path}"))
        if m:
            name = m.group(1)
            break
    check(f"{stage}: the en-US locale chunk is findable", bool(name),
          "no chunk maps ./locales/en-US/translation.json — i18n registration changed shape")
    if not name:
        return

    body, _ = fetch_abs(f"/_app/immutable/chunks/{name}")
    check(f"{stage}: the en-US locale chunk is served", bool(body), f"{name} not reachable")
    if not body:
        return
    body = body.decode("utf-8", "replace")

    # Every key naming the app must carry a value. An empty one means i18next falls back to the
    # key and the stock wording renders — the exact state this was written to end.
    empty = re.findall(r'"((?:[^"\\]|\\.)*WebUI(?:[^"\\]|\\.)*)":""', body)
    empty += re.findall(r"'((?:[^'\\]|\\.)*WebUI(?:[^'\\]|\\.)*)':\"\"", body)
    check(f"{stage}: no 'WebUI' string is left to fall back to its key", not empty,
          f"{len(empty)} unbranded: {[k[:45] for k in empty[:4]]} — run branding/apply.sh")

    # The page that prompted this, spelled out: a rule that silently stopped matching would
    # still pass the count check above if the keys vanished too.
    for want in ("Contact Admin for OhmzAI Access",
                 "To access OhmzAI, please reach out to the administrator"):
        check(f"{stage}: pending page says {want[:34]!r}...", want in body)

    # Each article rule, by the phrase it produces. A bare substitution would leave "To access
    # the OhmzAI" and "your OhmzAI." here, so these are what proves those rules still fire.
    #
    # Deliberately NOT a blanket "the OhmzAI never appears": "maintained by the OhmzAI team" is
    # correct English, and no cheap pattern separates it from "the OhmzAI, please" without
    # guessing at parts of speech. Assert the outputs, not the absence of a shape.
    for rule, want in (('"the WebUI"', "Please serve OhmzAI from the backend"),
                       ('"your WebUI"', "Enter the public URL of your OhmzAI instance")):
        check(f"{stage}: the {rule} article rule still fires", want in body,
              f"expected {want!r} — a bare substitution would read 'the OhmzAI' here")


def main():
    if fetch("custom.css") is None:
        print(f"OpenWebUI not reachable at {BASE} — nothing to check")
        return 0

    print("--- served assets match the repo ---")
    verify("live")

    print("--- the shell asks for what is actually there ---")
    verify_shell("live")

    print("--- the UI copy no longer says WebUI ---")
    verify_copy("live")

    if "--restart" in sys.argv:
        print(f"--- restarting {CONTAINER} (the regression) ---")
        subprocess.run(["docker", "restart", CONTAINER], capture_output=True, timeout=180)
        for _ in range(60):
            if fetch("custom.css") is not None:
                break
            subprocess.run(["sleep", "2"])
        verify("after restart")
        verify_shell("after restart")
        verify_copy("after restart")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

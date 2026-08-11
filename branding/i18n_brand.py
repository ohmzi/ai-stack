#!/usr/bin/env python3
"""Rebrand every user-facing "WebUI" string to Ohmz AI.

    python3 branding/i18n_brand.py            # install
    python3 branding/i18n_brand.py --revert   # put the stock wording back
    python3 branding/i18n_brand.py --check    # report only, change nothing

Called by branding/apply.sh, which is the thing you normally run.

WHY THIS AND NOT loader.js
--------------------------
loader.js rebrands the app name by rewriting GET /api/config at the fetch
boundary. That does not reach these strings, and no amount of caching work would
have: they are i18n keys compiled into the frontend, and i18next pulls its
resources with a dynamic import() — which does not go through window.fetch, so
the wrapper never sees them.

WHY NOT A FORK REBUILD
----------------------
compose/openwebui/fork/ exists for frontend changes, and it is the right tool
when behaviour changes. This is product copy: a replacement either matched or it
did not, and the assertions below make that loud. Against that, a rebuild is
npm ci + npm run build and a container recreate for a text change.

The lever it uses is the one i18next already provides. en-US/translation.json
ships every value as "" — English is the fallback, so what you see on screen is
the KEY. Give a key a non-empty value and that value is what renders, with no
source patched and no key broken for any other locale.

HOW IT FINDS THE FILE
---------------------
Not by name. Vite content-hashes chunk filenames, so DwGFF-zt.js is true of
exactly one build. It follows the locale map instead — the registry the app
itself uses:

    "./locales/en-US/translation.json": () => import("./DwGFF-zt.js")

so a rebuild that renames every chunk is found rather than silently missed.

WHAT IT REWRITES
----------------
Every key containing "WebUI", by rule rather than by a list, so a string added
upstream is picked up instead of quietly keeping stock wording. The article
rules exist because a bare substitution produces broken English: "To access the
WebUI" would become "To access the Ohmz AI".

Ordered, and the order is load-bearing — "the WebUI" must run before
"Open WebUI", or "maintained by the Open WebUI team" loses its article too.

Scope was the owner's call (2026-08-04): every occurrence, including the ones
that name the upstream project — version strings, Community links, the funding
notice. Open WebUI's licence permits removing its branding at 50 users or fewer;
this is a single-user instance.

CACHING
-------
/_app/immutable/ sounds like it would be a problem and is not: this build sends
no cache-control for it, only etag + last-modified, and Cloudflare reports
REVALIDATED. Editing a chunk changes its etag, so both layers pick it up. That
is worth re-checking if upstream ever starts sending immutable — at which point
the chunk would have to be renamed rather than edited.

Like everything else in branding/, this writes inside the image: re-run after
`docker rm` or an image pull. A plain restart is safe — /app/build/_app is not
touched by the startup rebuild that config.py does to STATIC_DIR.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

CONTAINER = os.environ.get("OWUI_CONTAINER", "open-webui")
CHUNKS = "/app/build/_app/immutable/chunks"

# Applied in order. The first two exist so the result reads as English; the last
# two are the actual rebrand. See the module docstring on why order matters.
RULES = [
    ("The WebUI", "Ohmz AI"),
    ("the WebUI", "Ohmz AI"),
    ("your WebUI", "your Ohmz AI instance"),
    ("Open WebUI", "Ohmz AI"),
    ("WebUI", "Ohmz AI"),
]

# A key is double-quoted unless it contains a double quote, in which case the
# minifier switches to single quotes. Matched with ANY value, not just an empty
# one — matching only `:""` would mean the script could not see its own work,
# so a re-run would report that nothing matched and --revert would find nothing
# to undo.
#
# The value matcher is written once and concatenated in, rather than inlined
# into both patterns: spelled out inside a double-quoted raw string it needs
# \" and \\\\, and getting that wrong is silent — the pattern simply stops
# matching the three keys whose value carries escaped quotes, and the script
# reports a smaller count rather than an error.
_JS_STR = r'"(?:[^"\\]|\\.)*"'
KEY_PATTERNS = [
    ('"', re.compile(r'"((?:[^"\\]|\\.)*WebUI(?:[^"\\]|\\.)*)":(' + _JS_STR + ")")),
    ("'", re.compile(r"'((?:[^'\\]|\\.)*WebUI(?:[^'\\]|\\.)*)':(" + _JS_STR + ")")),
]


def decode_key(raw, quote):
    """The literal text of a key, from the source between its quotes."""
    if quote == '"':
        return json.loads(f'"{raw}"')
    # Single-quoted only ever because the key contains a double quote, which JSON
    # needs escaped; the apostrophe escape goes the other way.
    return json.loads('"' + raw.replace("\\'", "'").replace('"', '\\"') + '"')


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def dexec(*args, binary=False):
    r = subprocess.run(["docker", "exec", CONTAINER, *args],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        die(f"docker exec {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def brand(text):
    for old, new in RULES:
        text = text.replace(old, new)
    return text


def find_en_us_chunk():
    """The chunk en-US resolves to, read out of the app's own locale registry."""
    listing = dexec("sh", "-c", f"grep -l 'locales/en-US/translation.json' {CHUNKS}/*.js || true")
    names = set()
    for path in listing.split():
        if path.endswith(".map"):
            continue
        body = dexec("cat", path)
        m = re.search(r'locales/en-US/translation\.json".{0,160}?import\("\./([^"]+\.js)"', body)
        if m:
            names.add(m.group(1))
    if not names:
        die("no chunk maps ./locales/en-US/translation.json to an import — "
            "upstream changed how i18n resources are registered")
    if len(names) > 1:
        die(f"the locale registry names more than one en-US chunk: {sorted(names)}")
    return f"{CHUNKS}/{names.pop()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true", help="restore the stock wording")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    path = find_en_us_chunk()
    body = dexec("cat", path)

    changed, already = [], []

    def make_rewriter(quote):
        def rewrite(m):
            key, value = decode_key(m.group(1), quote), m.group(2)
            current = json.loads(m.group(2))
            # brand() from the CURRENT value when there is one, so an upstream en-US
            # wording is rebranded rather than discarded — and because brand() leaves
            # already-branded text alone (nothing left to match), that is also what
            # makes a re-run a no-op instead of a second pass.
            want = "" if args.revert else brand(current or key)
            if current == want:
                already.append(key)
                return m.group(0)
            changed.append((key, current, want))
            # The value is re-emitted with json.dumps so quotes inside it are escaped —
            # 'WebUI will make requests to "{{url}}"' has them, and an unescaped one
            # would end the string literal and take the whole chunk down with it.
            return m.group(0).replace(f":{value}", f":{json.dumps(want)}", 1)
        return rewrite

    patched, matched = body, 0
    for quote, pat in KEY_PATTERNS:
        matched += len(pat.findall(patched))
        patched = pat.sub(make_rewriter(quote), patched)

    if not matched:
        die(f"no 'WebUI' keys matched in {path} — the chunk shape changed. This is not "
            "'nothing to do': every key is still there, so the pattern is what broke.")
    # Every occurrence must be accounted for, or something is being silently skipped.
    occurrences = body.count("WebUI")
    if matched != occurrences:
        die(f"matched {matched} of {occurrences} 'WebUI' occurrences in {path} — "
            "some are in a form the patterns do not cover")

    verb = "revert" if args.revert else "brand"
    if args.check:
        print(f"{path}\n  would {verb}: {len(changed)}   already done: {len(already)}")
        for key, current, want in changed:
            print(f"    {(current or key)[:78]!r}\n      -> {want[:78]!r}")
        return 0

    if changed:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(patched)
            tmp = fh.name
        try:
            subprocess.run(["docker", "cp", tmp, f"{CONTAINER}:{path}"], check=True,
                           capture_output=True, timeout=120)
            dexec("chmod", "a+r", path)
        finally:
            os.unlink(tmp)

    print(f"i18n: {verb}ed {len(changed)} string(s), {len(already)} already done "
          f"({os.path.basename(path)})")
    return 0


sys.exit(main())

#!/usr/bin/env bash
#
# Install the OhmzAI skin into the running Open WebUI container.
#
#   ./branding/apply.sh            # install
#   ./branding/apply.sh --revert   # put the stock assets back
#
# Why a script and not a bind mount: this container's only bind mount is
# /app/backend/data — the served assets live INSIDE the image, so `docker rm`
# or an image pull wipes them. Re-run after either. Idempotent.
#
# TWO directories, and BOTH are mandatory. This was learned the hard way on
# 2026-08-01, when a plain `docker restart` silently reverted the skin:
#
#   /app/backend/open_webui/static   STATIC_DIR — what /static actually serves
#                                    (main.py:2567 mounts it, env.py:240 resolves it)
#   /app/build/static                the SOURCE it is rebuilt from on every start
#
# config.py:96-115 runs at import, i.e. on EVERY container start: it unlinks
# every top-level FILE in STATIC_DIR, then copies /app/build/static/**/* over
# it. Directories survive (which is why ohmz-fonts/ did) but every branded file
# does not. An earlier revision of this header called /app/build/static "a
# leftover served to nobody" — that was wrong, and acting on it is what broke
# the skin: the branding was written to the served dir only, and the next
# restart copied stock right back over it.
#
# So we write both. Writing STATIC_DIR makes it live immediately without a
# restart; writing /app/build/static makes the startup rebuild reproduce the
# brand instead of undoing it. Failure mode if you skip the second one is
# nasty: index.html keeps its ?v= fingerprints while the files they point at
# come back 200 OK and ZERO BYTES — invisible unless you check served length.
#
# Stock files are copied to .stock-backup on first run only, so a later re-run
# never overwrites the pristine originals with branded ones.
#
# THE SHELL, NOT JUST THE ASSETS. index.html ships four things this script has
# to rewrite, because no amount of correct files under /static can reach them:
#
#   <link rel="manifest" href="/manifest.json">   the BACKEND route, which
#     returns {"name": "Open WebUI"} built from WEBUI_NAME. It is the only
#     manifest a browser reads, so Android and iOS name a home-screen shortcut
#     "Open WebUI" no matter what /static/site.webmanifest says. loader.js
#     cannot help: the browser fetches a manifest itself, not through
#     window.fetch, so the wrapper never sees it. Repointed at the branded
#     static manifest, which was previously installed and referenced by nothing.
#   <title>Open WebUI</title>                     what anything reading the raw
#     HTML sees — bookmarks, link previews, iOS's add-to-home-screen prefill.
#     loader.js only fixes the title once JS has run.
#   <meta name="theme-color" content="#171717">   stock neutral; paints the
#     Android status bar and Chrome-mobile's tab strip.
#   apple-mobile-web-app-title                    absent; it is what iOS labels
#     a home-screen icon with.

set -euo pipefail

CONTAINER="${OWUI_CONTAINER:-open-webui}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATIC=/app/backend/open_webui/static
# The source STATIC_DIR is rebuilt from on every container start — see the header.
BUILD_STATIC=/app/build/static
# Brand webfonts go in their own directory: $STATIC/fonts already holds the
# Noto family the PDF exporter needs, and must not be disturbed.
FONTS="$STATIC/ohmz-fonts"
BACKUP="$STATIC/.stock-backup"
# The SPA shell. Served from /app/build (unlike /static, which comes from the
# backend directory above) — this is the one file in /app/build that matters.
INDEX=/app/build/index.html
# Files in /app/build are also served from the site ROOT, which is where every
# scraper, feed reader and OS shortcut-maker probes for an icon when it ignores
# the <link> tags. Stock ships favicon.png here (the "OI" mark) and no .ico at
# all, so /favicon.ico falls through to the SPA catch-all and returns HTML.
BUILD_ROOT=/app/build

BRAND_NAME=OhmzAI
BRAND_THEME='#1a1917'
# The values to put back on --revert. Hardcoded rather than restored from a
# backup for the same reason the fingerprints are stripped rather than restored:
# deterministic, and it cannot resurrect a half-branded shell.
STOCK_NAME='Open WebUI'
STOCK_THEME='#171717'
STOCK_MANIFEST=/manifest.json

# Everything build_assets.py emits, plus the manifest.
ASSETS=(
  favicon.svg favicon.png favicon-dark.png favicon-96x96.png favicon.ico
  apple-touch-icon.png logo.png splash.png splash-dark.png
  web-app-manifest-192x192.png web-app-manifest-512x512.png site.webmanifest
)

die() { echo "error: $*" >&2; exit 1; }

docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || die "container '$CONTAINER' not found (set OWUI_CONTAINER to override)"
[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] \
  || die "container '$CONTAINER' is not running"

# Back up the stock assets exactly once. Deliberately all-or-nothing rather
# than per-file top-up: on a re-run the live files are already branded, so
# "copy anything missing from the backup" would enshrine a branded file as the
# stock one and destroy the only way back.
if ! docker exec "$CONTAINER" test -d "$BACKUP"; then
  echo "backing up stock assets -> $BACKUP"
  docker exec "$CONTAINER" mkdir -p "$BACKUP"
  docker exec "$CONTAINER" sh -c \
    "cd $STATIC && for f in ${ASSETS[*]}; do [ -f \"\$f\" ] && cp -p \"\$f\" $BACKUP/ || true; done"
fi

# The site-root favicon.png is a different file from $STATIC/favicon.png, and it
# is stashed under a name that is NOT in ASSETS so the restore loop below —
# which walks ASSETS — can never mistake it for a /static asset.
#
# Guarded on its own rather than folded into the block above, because that block
# fires once ever, on the existence of $BACKUP. Anything added to the backup set
# later would therefore never be captured on an instance that already has one —
# which is exactly what happened when this file was added. The guard here is
# per-file and self-healing, and the checksum test enforces the same rule the
# all-or-nothing block above was written for: a branded file must never be
# enshrined as the stock one.
if ! docker exec "$CONTAINER" test -f "$BACKUP/build-root-favicon.png"; then
  live=$(docker exec "$CONTAINER" sh -c \
    "sha256sum $BUILD_ROOT/favicon.png 2>/dev/null | cut -d' ' -f1" || true)
  mine=$(sha256sum "$HERE/assets/favicon.png" | cut -d' ' -f1)
  if [ -n "$live" ] && [ "$live" != "$mine" ]; then
    echo "backing up stock site-root favicon"
    docker exec "$CONTAINER" cp -p "$BUILD_ROOT/favicon.png" "$BACKUP/build-root-favicon.png"
  fi
fi

if [ "${1:-}" = "--revert" ]; then
  docker exec "$CONTAINER" test -d "$BACKUP" || die "no backup to revert to"
  # custom.css and loader.js ship empty — they exist purely as customisation
  # hooks — so truncating them IS the stock state, no backup copy needed.
  # Both dirs, or the next start would copy the branded build dir back over the
  # reverted served one — the same trap in reverse.
  #
  # Restore by walking ASSETS rather than `cp -rp $BACKUP/.`: the backup also
  # holds the site-root favicon, which is not a /static asset and must not be
  # dropped into either static dir.
  docker exec "$CONTAINER" sh -c \
    "cd $BACKUP && for f in ${ASSETS[*]}; do [ -f \"\$f\" ] && cp -p \"\$f\" $STATIC/ && cp -p \"\$f\" $BUILD_STATIC/ || true; done \
     && : > $STATIC/custom.css && : > $STATIC/loader.js \
     && : > $BUILD_STATIC/custom.css && : > $BUILD_STATIC/loader.js \
     && rm -rf $FONTS $BUILD_STATIC/ohmz-fonts"
  # The site-root icons: favicon.png overwrote a stock file, so it is restored;
  # favicon.ico did not exist before this script, so it is simply removed.
  # Say so if the stock copy is missing rather than skipping in silence — that
  # leaves a branded file behind, and a quiet revert is how you end up debugging
  # a "reverted" instance that isn't.
  if docker exec "$CONTAINER" test -f "$BACKUP/build-root-favicon.png"; then
    docker exec "$CONTAINER" cp -p "$BACKUP/build-root-favicon.png" "$BUILD_ROOT/favicon.png"
  else
    echo "warning: no stock copy of $BUILD_ROOT/favicon.png — it stays branded." >&2
    echo "         recover it with: docker run --rm --entrypoint cat <image> $BUILD_ROOT/favicon.png" >&2
  fi
  docker exec "$CONTAINER" rm -f "$BUILD_ROOT/favicon.ico"
  # Put the shell back. Strip the fingerprints rather than restoring index.html
  # from a backup: deterministic, and it cannot resurrect a half-branded shell.
  # Same reasoning for writing the stock strings back literally.
  docker exec "$CONTAINER" sh -c \
    "sed -i -E 's#(/static/[A-Za-z0-9._-]+)\?v=[A-Za-z0-9]+#\1#g' $INDEX \
     && sed -i -E 's@<meta name=\"apple-mobile-web-app-title\"[^>]*>@@g' $INDEX \
     && sed -i -E 's@(<meta name=\"theme-color\" content=)\"[^\"]*\"@\1\"$STOCK_THEME\"@' $INDEX \
     && sed -i -E \"s@'$BRAND_THEME'@'$STOCK_THEME'@g\" $INDEX \
     && sed -i -E 's@(<link rel=\"manifest\" href=)\"[^\"]*\"@\1\"$STOCK_MANIFEST\"@' $INDEX \
     && sed -i -E 's@<title>[^<]*</title>@<title>$STOCK_NAME</title>@' $INDEX"
  echo "reverted to stock assets. Hard-refresh the browser (ctrl-shift-r)."
  exit 0
fi

[ -f "$HERE/ohmz.css" ] || die "missing $HERE/ohmz.css"
[ -f "$HERE/loader.js" ] || die "missing $HERE/loader.js"
[ -d "$HERE/assets" ] || die "missing $HERE/assets (run: python3 branding/build_assets.py)"

# Every file goes to BOTH the served dir (live now) and the build dir (so the
# next container start rebuilds the brand instead of stock). See the header.
install_both() {                       # install_both <local-file> <basename>
  docker cp "$1" "$CONTAINER:$STATIC/$2"
  docker cp "$1" "$CONTAINER:$BUILD_STATIC/$2"
}

echo "installing theme"
install_both "$HERE/ohmz.css" custom.css

echo "installing app-name override"
install_both "$HERE/loader.js" loader.js

echo "installing fonts"
docker exec "$CONTAINER" mkdir -p "$FONTS" "$BUILD_STATIC/ohmz-fonts"
for f in "$HERE"/fonts/*.woff2; do
  docker cp "$f" "$CONTAINER:$FONTS/$(basename "$f")"
  docker cp "$f" "$CONTAINER:$BUILD_STATIC/ohmz-fonts/$(basename "$f")"
done

echo "installing brand assets"
for a in "${ASSETS[@]}"; do
  [ -f "$HERE/assets/$a" ] || die "missing asset $a"
  install_both "$HERE/assets/$a" "$a"
done

# The site root, for anything that ignores the <link> tags and just asks for
# /favicon.*. favicon.png replaces the stock "OI" mark; favicon.ico is new —
# without it that path falls through to the SPA catch-all and answers 200 with
# text/html, which every consumer of it then fails to decode.
echo "installing site-root icons"
docker cp "$HERE/assets/favicon.png" "$CONTAINER:$BUILD_ROOT/favicon.png"
docker cp "$HERE/assets/favicon.ico" "$CONTAINER:$BUILD_ROOT/favicon.ico"

# The served files must be readable by the app's uid.
docker exec "$CONTAINER" sh -c \
  "chmod -R a+r $STATIC $BUILD_STATIC && chmod a+rx $FONTS \
   && chmod a+r $BUILD_ROOT/favicon.png $BUILD_ROOT/favicon.ico"

# Fingerprint the asset URLs in index.html.
#
# Without this the skin does not actually ship. This instance sits behind
# Cloudflare, which caches /static/* for four hours (max-age=14400,
# cf-cache-status: HIT) and hands browsers a stale copy long after the files
# on disk have changed — `no-store` on the client does not help, because it
# only bypasses the *browser* cache and still hits the edge. The failure is
# invisible from the server: the file is correct, the edge just isn't serving
# it yet.
#
# index.html is cf-cache-status: DYNAMIC — never cached — so a fingerprint
# embedded there reaches every browser on the next page load and busts both
# layers at once. It also means no hard-refresh is ever needed.
#
# Idempotent: any existing ?v= is replaced, not appended to.
#
# The stamp hashes EXACTLY the set of files whose URLs carry it. It used to
# hash three (ohmz.css, loader.js, favicon.svg), which meant changing the mark
# left the stamp — and therefore every icon URL — untouched: Cloudflare served
# its four-hour-old copy under the unchanged key, and Chrome, whose favicon
# database is keyed by icon URL and expires on the order of days, never
# refetched at all. A revamped logo would simply not appear, on a shell that
# looked correctly fingerprinted and files that were correct on disk. Hashing
# the whole installed set is what makes a new asset actually ship.
STAMP=$(cat "$HERE/ohmz.css" "$HERE/loader.js" \
  "${ASSETS[@]/#/$HERE/assets/}" | sha256sum | cut -c1-10)

# Built from ASSETS so the two can never drift. The dots are escaped, which is
# what keeps the alternation unambiguous — no entry is a prefix of another once
# its separator must match literally (splash\.png cannot match splash-dark.png).
FINGERPRINTED='custom\.css|loader\.js'
for a in "${ASSETS[@]}"; do
  FINGERPRINTED="$FINGERPRINTED|${a//./\\.}"
done

# index.html and the manifest are the two documents that name asset URLs, and
# neither is edge-cached (DYNAMIC), so a stamp written into them reaches every
# browser on the next load and busts the browser and Cloudflare together.
echo "fingerprinting index.html + site.webmanifest (v=$STAMP)"
docker exec "$CONTAINER" sh -c \
  "sed -i -E 's#/static/($FINGERPRINTED)(\?v=[A-Za-z0-9]+)?#/static/\1?v=$STAMP#g' \
     $INDEX $STATIC/site.webmanifest $BUILD_STATIC/site.webmanifest"

# The shell's own branding — see the header. Every one of these is idempotent:
# each rewrites a value in place, and the apple-mobile-web-app-title tag is
# removed before it is re-added so a re-run cannot stack duplicates. It is
# deleted as a TAG and not as a LINE, because after the first run it shares a
# line with the theme-color meta that re-inserts it.
#
# The last sed is not a duplicate of the theme-color one. The meta tag only
# holds until the inline anti-FOUC script runs, roughly a frame later: that
# script setAttribute()s theme-color from its own hardcoded table, so the tag
# alone would be overwritten with stock #171717 on every load. It is matched
# single-quoted, which is exactly and only how the script spells it — the meta
# tag uses double quotes — so this cannot touch the markup. Only the DARK entry
# is rebranded; light (#ffffff), oled-dark (#000000) and her (#983724) are
# deliberately left alone, since #1a1917 is the dark canvas specifically
# (ohmz.css: --color-gray-900 / --color-black).
echo "branding the shell (manifest link, title, theme-colour, iOS name)"
docker exec "$CONTAINER" sh -c \
  "sed -i -E 's@(<link rel=\"manifest\" href=)\"[^\"]*\"@\1\"/static/site.webmanifest?v=$STAMP\"@' $INDEX \
   && sed -i -E 's@<title>[^<]*</title>@<title>$BRAND_NAME</title>@' $INDEX \
   && sed -i -E 's@<meta name=\"apple-mobile-web-app-title\"[^>]*>@@g' $INDEX \
   && sed -i -E 's@<meta name=\"theme-color\" content=\"[^\"]*\" />@<meta name=\"theme-color\" content=\"$BRAND_THEME\" /><meta name=\"apple-mobile-web-app-title\" content=\"$BRAND_NAME\" />@' $INDEX \
   && sed -i -E \"s@'$STOCK_THEME'@'$BRAND_THEME'@g\" $INDEX"

cat <<DONE

OhmzAI skin installed (v=$STAMP).

The assets are fingerprinted, so no refresh is needed to pick them up. The
exception is the HTML that carries the fingerprints: Open WebUI serves / with
no cache-control, so a browser that loaded the app earlier may hold it under
heuristic freshness (~10% of its age) and keep requesting the old URLs. One
hard refresh (ctrl-shift-r / cmd-shift-r) clears that, once. After it, the
shell's Last-Modified is recent, so browsers revalidate it almost every load
and later skin changes propagate on their own.

The app name is handled by loader.js, which rewrites the "name" field of
GET /api/config before the front-end reads it — so the sign-in heading, the
sidebar and the document title all say OhmzAI. Setting WEBUI_NAME instead
would need the container recreated, and env.py:842-844 would render it as
"OhmzAI (Open WebUI)" regardless.

The name on a phone home-screen shortcut is a SEPARATE path, and loader.js
cannot reach it: a browser fetches the web app manifest itself rather than
through window.fetch. The shell now points at /static/site.webmanifest instead
of the backend's /manifest.json, which is what was naming those shortcuts
"Open WebUI". An already-added shortcut keeps the name and icon it was created
with — the OS copied both at the time. Remove it and add it again.

No restart is needed: these files are read per request. A restart is now also
SAFE — the assets are written to /app/build/static as well, so config.py's
startup rebuild reproduces the brand instead of reverting it. Before that fix
(2026-08-01) a plain "docker restart" silently served 0-byte custom.css and
loader.js while index.html still asked for the fingerprinted URLs.

Re-run after "docker rm" or an image pull, which wipe both directories.

(NB: this heredoc is unquoted so the version stamp expands — never use
backticks in it, they run as command substitution. Not hypothetical: the first
draft of this very message executed "docker restart" and "docker rm".)
DONE

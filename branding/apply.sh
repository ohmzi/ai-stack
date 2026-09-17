#!/usr/bin/env bash
#
# Install the Ohmz AI skin into the running Open WebUI container.
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
#
# THE SITE ROOT, SEPARATELY. Two further files go to $BUILD_ROOT itself rather than into either
# static dir: robots.txt and .well-known/assetlinks.json. They are what Android reads when this is
# installed as an app, and neither has an iOS counterpart — which is why the same site can install
# cleanly from Safari and fail from Chrome on Android. They live in site-root/, mirroring the paths
# they serve at. Neither is fingerprinted, and neither CAN be: the verifier fetches exact paths, so
# a change needs a Cloudflare edge purge rather than a new ?v=. README.md, "The Android install
# path", has the full account.

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
# The compiled SPA itself. Unlike index.html it is not a document the browser
# re-reads and re-resolves — it is ~980 chunks that hardcode their own asset
# URLs, so a stamp written into index.html does not reach the marks the SPA
# draws. Explains the "tab is right, sidebar and sign-in are still stock"
# symptom; see the fingerprinting step below.
BUILD_APP=/app/build/_app/immutable

# Quoted because the name contains a space — unquoted, `BRAND_NAME=Ohmz AI` is
# parsed as "run the command AI with BRAND_NAME=Ohmz in its environment", which
# is exactly how this failed the moment the brand gained a space.
BRAND_NAME='Ohmz AI'
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
  # Same reasoning as the shell above: write the stock content back literally rather than restore
  # it from a backup. It is two lines, it is deterministic, and it cannot resurrect a half-reverted
  # state. (Stock is exactly "User-agent: *" / "Disallow: /".)
  docker exec "$CONTAINER" sh -c \
    "printf 'User-agent: *\nDisallow: /\n' > $BUILD_ROOT/robots.txt"
  # .well-known/assetlinks.json did not exist before this script, so it is removed, not restored.
  docker exec "$CONTAINER" rm -rf "$BUILD_ROOT/.well-known"
  # Put the shell back. Strip the fingerprints rather than restoring index.html
  # from a backup: deterministic, and it cannot resurrect a half-branded shell.
  # Same reasoning for writing the stock strings back literally.
  docker exec "$CONTAINER" sh -c \
    "sed -i -E 's#(/static/[A-Za-z0-9._-]+)\?v=[A-Za-z0-9]+#\1#g' $INDEX \
     && sed -i -E 's@<meta name=\"apple-mobile-web-app-title\"[^>]*>@@g' $INDEX \
     && sed -i -E 's@(<meta name=\"theme-color\" content=)\"[^\"]*\"@\1\"$STOCK_THEME\"@' $INDEX \
     && sed -i -E \"s@'$BRAND_THEME'@'$STOCK_THEME'@g\" $INDEX \
     && sed -i -E 's@(<link rel=\"manifest\" href=)\"[^\"]*\"@\1\"$STOCK_MANIFEST\"@' $INDEX \
     && sed -i -E \"s@localStorage.theme = 'dark'@localStorage.theme = 'system'@\" $INDEX \
     && sed -i -E 's@<title>[^<]*</title>@<title>$STOCK_NAME</title>@' $INDEX"
  # The UI copy lives in a frontend chunk, not in either static dir — its own
  # script, which puts the empty en-US values back.
  OWUI_CONTAINER="$CONTAINER" python3 "$HERE/i18n_brand.py" --revert
  echo "reverted to stock assets. Hard-refresh the browser (ctrl-shift-r)."
  exit 0
fi

[ -f "$HERE/ohmz.css" ] || die "missing $HERE/ohmz.css"
[ -f "$HERE/loader.js" ] || die "missing $HERE/loader.js"
[ -d "$HERE/assets" ] || die "missing $HERE/assets (run: python3 branding/build_assets.py)"
[ -f "$HERE/site-root/robots.txt" ] || die "missing $HERE/site-root/robots.txt"
[ -f "$HERE/site-root/.well-known/assetlinks.json" ] \
  || die "missing $HERE/site-root/.well-known/assetlinks.json"

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

# ...and two more site-root files, there for the same reason favicon.ico is: a path that is not a
# file falls through SPAStaticFiles, which answers any non-.js miss with index.html and a 200. So
# /.well-known/assetlinks.json returned ELEVEN KILOBYTES OF HTML with a content-type of text/html
# — a soft 404 saying "200 OK" in the one place that must not lie, because it is where Android
# looks to check that this site owns an installed app. (Found 2026-09-17, chasing an Android PWA
# that would not load while the same site was fine in Chrome and on iOS.)
#
# robots.txt keeps the instance disallowed — it is private — but unblocks /.well-known/, because
# the verifier reads robots.txt FIRST and will not fetch a disallowed path. Under the stock
# two-liner the file was never even requested.
#
# assetlinks.json declares an EMPTY association list, deliberately. The package name and signing
# fingerprint of a Chrome-minted WebAPK are generated by Google at mint time and cannot be known
# here, so "no app is associated" is the only truthful thing to say — and it is valid JSON with a
# real 200, rather than HTML pretending to be one.
#
# Neither file is fingerprinted: they are not cacheable assets, and neither is served from
# /static, so the stamp does not reach them.
echo "installing site-root robots.txt + .well-known"
docker exec "$CONTAINER" mkdir -p "$BUILD_ROOT/.well-known"
docker cp "$HERE/site-root/robots.txt" "$CONTAINER:$BUILD_ROOT/robots.txt"
docker cp "$HERE/site-root/.well-known/assetlinks.json" \
          "$CONTAINER:$BUILD_ROOT/.well-known/assetlinks.json"

# The served files must be readable by the app's uid.
docker exec "$CONTAINER" sh -c \
  "chmod -R a+r $STATIC $BUILD_STATIC && chmod a+rx $FONTS \
   && chmod a+r $BUILD_ROOT/favicon.png $BUILD_ROOT/favicon.ico $BUILD_ROOT/robots.txt \
   && chmod -R a+r $BUILD_ROOT/.well-known"

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

# ...and the compiled chunks, which the line above does NOT reach. Added
# 2026-09-17, after exactly the symptom that omission produces: the browser tab
# showed the new mark while the SIDEBAR and the SIGN-IN page kept the stock one.
#
# Nothing about the SPA reads its icons from index.html. It hardcodes
# '/static/favicon.png' into the bundle — 96 references: the sidebar mark, the
# sign-in mark, the default avatar, the notification toast. Without the stamp
# those URLs are byte-identical to the ones the browser cached back when the
# file was stock, so Chrome served its own copy (its favicon/asset cache is
# keyed by URL) and Cloudflare served its edge copy, and a hard refresh did not
# help, because there was nothing for the refresh to notice.
#
# SvelteKit names chunks by content hash, so this edits a build artifact in
# place and leaves its filename alone. Safe HERE and only here: nothing verifies
# the hash (index.html carries no integrity= attribute — checked), the chunks
# resolve each other by names this does not touch, and the same in-place edit is
# already made to index.html eight lines up. The cost is that the on-disk hash
# is no longer the content's hash, so treat /app/build/_app as apply.sh's output
# rather than as the build's — which is already true of index.html.
echo "fingerprinting the compiled chunks (v=$STAMP)"
CHUNKS=$(docker exec "$CONTAINER" grep -rlE "/static/($FINGERPRINTED)" "$BUILD_APP" \
           --include='*.js' 2>/dev/null || true)
if [ -n "$CHUNKS" ]; then
  printf '%s\n' "$CHUNKS" | docker exec -i "$CONTAINER" \
    xargs -r sed -i -E "s#/static/($FINGERPRINTED)(\?v=[A-Za-z0-9]+)?#/static/\1?v=$STAMP#g"
  echo "  $(printf '%s\n' "$CHUNKS" | grep -c .) chunk file(s) re-stamped"
else
  echo "  none — no chunk names a branded asset (unexpected; check FINGERPRINTED)"
fi

# Root-relative URLs are a SECOND namespace, and the /static/-anchored pattern
# above is blind to it. apply.sh populates both: favicon.png and favicon.ico are
# installed at the site ROOT as well as under /static, because the root is where
# scrapers and OS shortcut-makers probe (see BUILD_ROOT above).
#
# The SPA uses the ROOT one as its image-error FALLBACK — 38 references compile
# down to `on:error={(e) => (e.currentTarget.src = '/favicon.png')}` — so any
# avatar with no custom profile image lands on it. Those URLs never changed, so
# the browser's cached stock copy was served forever, while the file on disk and
# its /static/ twin were both already correct. Reported 2026-09-17 as "the icon
# in the middle above the chatbox is still old on mobile": that is the empty-chat
# hero's model avatar (Placeholder.svelte), and the giveaway was that the sidebar
# right beside it had already updated.
#
# The `(^|[^/A-Za-z0-9_.-])` guard is load-bearing: without it the alternation
# also fires on the tail of the /static/favicon.png?v=… URLs the step above just
# wrote, producing a doubled query string.
ROOT_ASSETS='favicon\.png|favicon\.ico'
ROOT_SED="s#(^|[^/A-Za-z0-9_.-])/($ROOT_ASSETS)(\?v=[A-Za-z0-9]+)?#\1/\2?v=$STAMP#g"
ROOT_CHUNKS=$(docker exec "$CONTAINER" grep -rlE "/($ROOT_ASSETS)" "$BUILD_APP" \
                --include='*.js' 2>/dev/null || true)
echo "fingerprinting root-relative fallbacks (v=$STAMP)"
docker exec "$CONTAINER" sh -c \
  "sed -i -E '$ROOT_SED' $INDEX $STATIC/site.webmanifest $BUILD_STATIC/site.webmanifest"
if [ -n "$ROOT_CHUNKS" ]; then
  printf '%s\n' "$ROOT_CHUNKS" | docker exec -i "$CONTAINER" xargs -r sed -i -E "$ROOT_SED"
  echo "  $(printf '%s\n' "$ROOT_CHUNKS" | grep -c .) chunk file(s) re-stamped"
fi

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
# Dark is also the DEFAULT, not just a supported theme. Upstream's pre-paint
# script seeds `localStorage.theme = 'system'` when nothing is stored, which
# hands a first-time visitor's impression to whatever their OS happens to be
# set to. These apps are dark-first and ohmz.cloud/Homarr now agree on that, so
# an unset preference resolves to dark here too.
#
# Only the SEED is changed. Every branch that reads the value is untouched, and
# 'dark' already falls through to the script's final else (which adds .dark and
# #1a1917), so no new branch is needed. A user who picks a theme has it written
# to localStorage by the app and keeps it — this only decides what happens
# before anyone has chosen.
echo "branding the shell (manifest link, title, theme-colour, iOS name, default theme)"
docker exec "$CONTAINER" sh -c \
  "sed -i -E 's@(<link rel=\"manifest\" href=)\"[^\"]*\"@\1\"/static/site.webmanifest?v=$STAMP\"@' $INDEX \
   && sed -i -E 's@<title>[^<]*</title>@<title>$BRAND_NAME</title>@' $INDEX \
   && sed -i -E 's@<meta name=\"apple-mobile-web-app-title\"[^>]*>@@g' $INDEX \
   && sed -i -E 's@<meta name=\"theme-color\" content=\"[^\"]*\" />@<meta name=\"theme-color\" content=\"$BRAND_THEME\" /><meta name=\"apple-mobile-web-app-title\" content=\"$BRAND_NAME\" />@' $INDEX \
   && sed -i -E \"s@localStorage.theme = 'system'@localStorage.theme = 'dark'@\" $INDEX \
   && sed -i -E \"s@'$STOCK_THEME'@'$BRAND_THEME'@g\" $INDEX"

# The UI copy that still says WebUI — the pending-activation page, WebUI
# Settings, the webhook URL hints. These are i18n keys compiled into the
# frontend and resolved through a dynamically imported chunk, so neither
# loader.js nor anything under /static can reach them. See i18n_brand.py; it
# fails loudly rather than quietly leaving stock wording in place.
echo "branding the UI copy"
OWUI_CONTAINER="$CONTAINER" python3 "$HERE/i18n_brand.py"

cat <<DONE

Ohmz AI skin installed (v=$STAMP).

The assets are fingerprinted, so no refresh is needed to pick them up. The
exception is the HTML that carries the fingerprints, and as of 2026-09-17 that
is fixed at the server rather than left to chance: SPAStaticFiles now serves the
shell with "Cache-Control: no-cache" (compose/openwebui/fork/gen/05_shell_cache.py),
so browsers revalidate it every load for the price of a 304. Before that they
fell back to heuristic freshness (~10% of the document's age) and could hold a
stale shell for days — which is not just stale branding: a shell whose chunk
hashes no longer exist leaves the app stuck on the splash screen until site data
is cleared. One hard refresh (ctrl-shift-r / cmd-shift-r) is still needed ONCE,
on any browser holding a pre-fix copy, because a header can only be learned from
a request the browser has not yet decided to make.

Run this after every rebuild of the fork image, not only after a static change:
the image supplies main.py, and this script supplies the assets.

The app name is handled by loader.js, which rewrites the "name" field of
GET /api/config before the front-end reads it — so the sign-in heading, the
sidebar and the document title all say Ohmz AI. Setting WEBUI_NAME instead
would need the container recreated, and env.py:842-844 would render it as
"Ohmz AI (Open WebUI)" regardless.

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

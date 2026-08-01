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

if [ "${1:-}" = "--revert" ]; then
  docker exec "$CONTAINER" test -d "$BACKUP" || die "no backup to revert to"
  # custom.css and loader.js ship empty — they exist purely as customisation
  # hooks — so truncating them IS the stock state, no backup copy needed.
  # Both dirs, or the next start would copy the branded build dir back over the
  # reverted served one — the same trap in reverse.
  docker exec "$CONTAINER" sh -c \
    "cp -rp $BACKUP/. $STATIC/ && cp -rp $BACKUP/. $BUILD_STATIC/ \
     && : > $STATIC/custom.css && : > $STATIC/loader.js \
     && : > $BUILD_STATIC/custom.css && : > $BUILD_STATIC/loader.js \
     && rm -rf $FONTS $BUILD_STATIC/ohmz-fonts"
  # Strip the fingerprints rather than restoring index.html from a backup:
  # deterministic, and it cannot resurrect a half-branded shell.
  docker exec "$CONTAINER" sh -c \
    "sed -i -E 's#(/static/[A-Za-z0-9._-]+)\?v=[A-Za-z0-9]+#\1#g' $INDEX"
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

# The served files must be readable by the app's uid.
docker exec "$CONTAINER" sh -c "chmod -R a+r $STATIC $BUILD_STATIC && chmod a+rx $FONTS"

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
STAMP=$(cat "$HERE/ohmz.css" "$HERE/loader.js" "$HERE/assets/favicon.svg" \
  | sha256sum | cut -c1-10)
FINGERPRINTED='custom\.css|loader\.js|favicon\.png|favicon\.svg|favicon\.ico|favicon-96x96\.png|favicon-dark\.png|apple-touch-icon\.png|splash\.png|splash-dark\.png'

echo "fingerprinting index.html (v=$STAMP)"
docker exec "$CONTAINER" sh -c \
  "sed -i -E 's#/static/($FINGERPRINTED)(\?v=[A-Za-z0-9]+)?#/static/\1?v=$STAMP#g' $INDEX"

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

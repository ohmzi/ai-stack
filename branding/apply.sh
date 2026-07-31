#!/usr/bin/env bash
#
# Install the OhmzAI skin into the running Open WebUI container.
#
#   ./branding/apply.sh            # install
#   ./branding/apply.sh --revert   # put the stock assets back
#
# Why a script and not a bind mount: this container's only bind mount is
# /app/backend/data — the served assets live INSIDE the image. Anything written
# here survives `docker restart` but is wiped by `docker rm` or an image pull,
# so re-run it after either. It is idempotent; running it twice is a no-op.
#
# Note the path. main.py:2567 mounts /static from STATIC_DIR, which env.py:240
# resolves to /app/backend/open_webui/static — NOT /app/build/static. The
# latter also exists, is a leftover of the front-end build, and is served to
# nobody; writing branding there looks like it worked and changes nothing.
#
# Stock files are copied to .stock-backup on first run only, so a later re-run
# never overwrites the pristine originals with branded ones.

set -euo pipefail

CONTAINER="${OWUI_CONTAINER:-open-webui}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATIC=/app/backend/open_webui/static
# Brand webfonts go in their own directory: $STATIC/fonts already holds the
# Noto family the PDF exporter needs, and must not be disturbed.
FONTS="$STATIC/ohmz-fonts"
BACKUP="$STATIC/.stock-backup"

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
  docker exec "$CONTAINER" sh -c \
    "cp -rp $BACKUP/. $STATIC/ && : > $STATIC/custom.css && : > $STATIC/loader.js && rm -rf $FONTS"
  echo "reverted to stock assets. Hard-refresh the browser (ctrl-shift-r)."
  exit 0
fi

[ -f "$HERE/ohmz.css" ] || die "missing $HERE/ohmz.css"
[ -f "$HERE/loader.js" ] || die "missing $HERE/loader.js"
[ -d "$HERE/assets" ] || die "missing $HERE/assets (run: python3 branding/build_assets.py)"

echo "installing theme"
docker cp "$HERE/ohmz.css" "$CONTAINER:$STATIC/custom.css"

echo "installing app-name override"
docker cp "$HERE/loader.js" "$CONTAINER:$STATIC/loader.js"

echo "installing fonts"
docker exec "$CONTAINER" mkdir -p "$FONTS"
for f in "$HERE"/fonts/*.woff2; do
  docker cp "$f" "$CONTAINER:$FONTS/$(basename "$f")"
done

echo "installing brand assets"
for a in "${ASSETS[@]}"; do
  [ -f "$HERE/assets/$a" ] || die "missing asset $a"
  docker cp "$HERE/assets/$a" "$CONTAINER:$STATIC/$a"
done

# The served files must be readable by the app's uid.
docker exec "$CONTAINER" sh -c "chmod -R a+r $STATIC && chmod a+rx $FONTS"

cat <<'DONE'

OhmzAI skin installed. Hard-refresh the browser (ctrl-shift-r) to clear the
cached custom.css, loader.js and favicons.

The app name is handled by loader.js, which rewrites the "name" field of
GET /api/config before the front-end reads it — so the sign-in heading, the
sidebar and the document title all say OhmzAI. Setting WEBUI_NAME instead
would need the container recreated, and env.py:842-844 would render it as
"OhmzAI (Open WebUI)" regardless.

No restart is needed or wanted: these files are read per request, and
WEBUI_SECRET_KEY is unset on this container, so a restart signs everyone out.
DONE

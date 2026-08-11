#!/usr/bin/env bash
#
# Append guest-ui.css to the public instance's already-branded custom.css.
#
#   OWUI_CONTAINER=open-webui-public ./branding/apply.sh   # first — installs the shared skin
#   ./compose/public/apply_guest_ui.sh                     # then this
#
# Why a separate script instead of folding these rules into branding/ohmz.css: that file is
# INSTALLED VERBATIM onto both instances by branding/apply.sh (docker cp, whole-file overwrite) —
# rules specific to the guest experience have no business shipping to the private instance's chat
# header too. This appends instead of overwriting, and only ever targets open-webui-public.
#
# Same two-directory requirement as branding/apply.sh, same reason: /app/backend/open_webui/static
# is what's actually served, /app/build/static is what config.py rebuilds STATIC_DIR from on every
# container start, and only writing the first one means a restart silently reverts this.
#
# Idempotent: re-run after any branding/apply.sh pass on this container, since that overwrites
# custom.css wholesale and would otherwise wipe these rules out from under it.
set -euo pipefail

CONTAINER="${OWUI_CONTAINER:-open-webui-public}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATIC=/app/backend/open_webui/static
BUILD_STATIC=/app/build/static
CSS="$HERE/guest-ui.css"

die() { echo "error: $*" >&2; exit 1; }

[ -f "$CSS" ] || die "missing $CSS"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "container '$CONTAINER' not found"
[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] || die "container '$CONTAINER' is not running"

# Guarded on the marker comment so re-running this without an intervening apply.sh pass doesn't
# pile up duplicate (harmless but messy) copies of the same rules.
MARKER="Guest UI trim"
for dir in "$STATIC" "$BUILD_STATIC"; do
  if docker exec "$CONTAINER" sh -c "grep -q '$MARKER' $dir/custom.css 2>/dev/null"; then
    echo "$dir/custom.css already has it, skipping"
    continue
  fi
  docker exec -i "$CONTAINER" sh -c "cat >> $dir/custom.css" < "$CSS"
done

echo "guest-ui.css appended to $CONTAINER's custom.css (both static dirs). No refresh needed —"
echo "custom.css is served with no cache-control and re-read per request."

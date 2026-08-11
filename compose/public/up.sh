#!/usr/bin/env bash
#
# (Re)create the public OWUI stack. Mirrors compose/openwebui/run.sh's secret handling: generate
# WEBUI_SECRET_KEY once, on first run, then hand off to compose.
#
# Usage:  ./up.sh                after any docker-compose.yml or nginx/guest-gate.conf edit
#         ./up.sh --force-recreate   after bootstrap step 7 (docs/PUBLIC_INSTANCE.md), to pick up
#                                    the env vars that flip login form / signup back off
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRET_ENV=/volume1/docker/openwebui-public/secret.env

if [ ! -f "$SECRET_ENV" ]; then
  echo "generating $SECRET_ENV (WEBUI_SECRET_KEY)"
  mkdir -p "$(dirname "$SECRET_ENV")"
  umask 177
  printf 'WEBUI_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > "$SECRET_ENV"
  umask 022
fi

# GUEST_GROUP_ID is read by docker-compose.yml as DEFAULT_GROUP_ID. Absent before bootstrap step 3
# creates the Guests group (docs/PUBLIC_INSTANCE.md) — compose tolerates the unset var (defaults to
# empty), OWUI just leaves new guests without a default group until this is filled in.
docker compose -p owui-public -f "$HERE/docker-compose.yml" up -d "$@"

echo -n "waiting for open-webui-public"
until docker exec open-webui-public curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1; do
  echo -n .
  sleep 3
done
echo " up"
echo "next: OWUI_CONTAINER=open-webui-public ./branding/apply.sh && ./compose/public/apply_guest_ui.sh"

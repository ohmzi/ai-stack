#!/usr/bin/env bash
#
# (Re)create the open-webui container. Idempotent: safe to re-run after an image pull.
#
# This file exists because until 2026-08-01 the container had been created BY HAND and its recipe
# lived nowhere but `docker inspect` on the running instance — a machine rebuild or an accidental
# `docker rm` meant reconstructing it from memory. Everything below reproduces the verified live
# config plus four deliberate additions, each annotated.
#
# The GPU is attached with `--device nvidia.com/gpu=all` (CDI), NOT `--gpus all`: the legacy path
# injects device cgroup rules that a `systemctl daemon-reload` silently strips, after which the
# container loses the GPU until recreated. ComfyUI hit exactly that and migrated; see
# compose/comfyui/run.sh and docs/TROUBLESHOOTING.md.
#
# Sessions: WEBUI_SECRET_KEY comes from secret.env (0600, NEVER committed). Before this, no key
# was set, OpenWebUI generated one per boot inside the container's writable layer, and every
# restart signed everyone out. One final logout happens when THIS change lands; none after.
#
# Deliberately NOT set (owner decisions, 2026-08-01):
#   ENABLE_SIGNUP=false  — owner keeps public signup open on ai.ohmz.cloud.
#   HOST=127.0.0.1       — owner keeps OWUI reachable on the LAN (0.0.0.0:4567).
#   WEBUI_NAME           — env.py appends " (Open WebUI)" to any custom value; the branding
#                          loader.js rewrites /api/config suffix-free instead.
#
# After any recreate: run branding/apply.sh (the static dir lives inside the image, so a fresh
# container is stock), then python3 tests/test_deployed.py.
set -euo pipefail

SECRET_ENV=/volume1/docker/openwebui/secret.env
if [ ! -f "$SECRET_ENV" ]; then
  echo "generating $SECRET_ENV (WEBUI_SECRET_KEY)"
  umask 177
  printf 'WEBUI_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > "$SECRET_ENV"
  umask 022
fi

docker rm -f open-webui 2>/dev/null || true

docker run -d --name open-webui \
  --network host \
  --restart unless-stopped \
  --device nvidia.com/gpu=all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -v /volume1/docker/openwebui/config:/app/backend/data \
  --env-file "$SECRET_ENV" \
  -e PORT=4567 \
  -e OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  -e ENABLE_VERSION_UPDATE_CHECK=false \
  -e ANONYMIZED_TELEMETRY=false \
  -e SCARF_NO_ANALYTICS=true \
  -e DO_NOT_TRACK=true \
  -e OFFLINE_MODE=true \
  -e HF_HUB_OFFLINE=1 \
  `# UVICORN_WORKERS pinned where the image would default it: the pipe's in-process lock half` \
  `# arbitrates threads within ONE worker; more workers would each mint their own lock.` \
  -e UVICORN_WORKERS=1 \
  `# The DB row web.loader.engine="safe_web" was WRITE-ONLY before this: the runtime reads the` \
  `# env var once at import (openwebui-config-snapshot.md), so the admin setting was decorative.` \
  -e WEB_LOADER_ENGINE=safe_web \
  `# Whisper: cut hallucinated transcription on silence, stop auto-detect guessing the language.` \
  -e WHISPER_VAD_FILTER=true \
  -e WHISPER_LANGUAGE=en \
  ghcr.io/open-webui/open-webui:cuda

echo -n "waiting for open-webui"
until curl -sf http://127.0.0.1:4567/health >/dev/null 2>&1; do echo -n .; sleep 3; done
echo " up"
echo "next: ./branding/apply.sh && python3 tests/test_deployed.py"

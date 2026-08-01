#!/bin/bash
# Create the comfyui container. Idempotent: removes any existing one first.
#
# The GPU is attached with `--device nvidia.com/gpu=all` (CDI), NOT `--gpus all` (the legacy
# NVIDIA hook). The legacy hook writes the device rule into the container's cgroup behind systemd's
# back, so the next `systemctl daemon-reload` — snapd triggers one during a routine apt-daily cycle
# — strips the GPU from every container on the box. CDI puts the devices in the OCI spec instead,
# where runc records them as real systemd DeviceAllow entries that survive a reload.
#
# Prerequisites, both one-time and already applied on this host:
#   sudo nvidia-ctk system create-dev-char-symlinks --create-all
#   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# plus /lib/udev/rules.d/71-nvidia-dev-char.rules to recreate the symlinks after a driver reload.
#
# See docs/TROUBLESHOOTING.md for the full incident writeup.
set -euo pipefail

IMAGE="${COMFYUI_IMAGE:-comfyui-local:tier2-gcc}"
DATA="${COMFYUI_DATA:-/volume1/docker/comfyui}"

docker rm -f comfyui 2>/dev/null || true
docker run -d --name comfyui --restart unless-stopped \
  --device nvidia.com/gpu=all \
  -e TORCHINDUCTOR_CACHE_DIR=/app/inductor_cache \
  -p 127.0.0.1:8188:8188 \
  `# Loopback deliberately: ComfyUI has NO auth, and /prompt-/interrupt on the LAN means any` \
  `# device (or wifi guest) can queue renders or kill yours. ufw does not help — docker's` \
  `# published ports bypass it. Every consumer (auto_assistant, photoreal, gpuguard, tests)` \
  `# already talks to 127.0.0.1. Do NOT touch --listen below: that is the bind INSIDE the` \
  `# bridge namespace, where 0.0.0.0 is required for the docker-proxy to reach it at all.` \
  -v "$DATA/input:/app/input" \
  -v "$DATA/models:/app/models" \
  -v "$DATA/output:/app/output" \
  -v "$DATA/custom_nodes:/app/custom_nodes" \
  -v "$DATA/inductor_cache:/app/inductor_cache" \
  "$IMAGE" \
  python main.py --listen 0.0.0.0 --port 8188 \
    --disable-smart-memory --lowvram --fast fp16_accumulation

echo -n "waiting for comfyui"
until curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; do echo -n .; sleep 3; done
echo " up"
docker exec comfyui nvidia-smi -L

# Troubleshooting

## Every image and video fails, but the GPU looks idle

**Symptom.** Any generation request comes back within a second or two as an out-of-memory error.
`nvidia-smi` on the host shows the card nearly empty, ComfyUI's `/system_stats` agrees, and the
container log carries a `torch.OutOfMemoryError: Allocation on device` whose PyTorch memory summary
is *all zeros* — no allocations, no `cudaMalloc` retries, no OOM counter.

That combination is not a memory shortage. **The container has lost access to the GPU.**

### What actually happens

Docker injects `/dev/nvidia*` through the NVIDIA *legacy* hook (`--gpus all` on a container whose
image carries the `com.nvidia.volumes.needed` label). The hook runs after `runc` has created the
container and writes the device rule straight into the container's cgroup — behind systemd's back.
`runc` never puts the GPU in the OCI spec, so the systemd scope's own record of the container never
mentions it:

```bash
systemctl show docker-$(docker inspect comfyui --format '{{.Id}}').scope -p DeviceAllow
```

lists `/dev/char/1:3`, `5:0`, `10:200` … and nothing in major 195 (`nvidia0`, `nvidiactl`) or
511 (`nvidia-uvm`).

Any `systemctl daemon-reload` makes systemd reapply that scope's device policy from its own records
— which never mentioned the GPU — and every GPU container on the box loses CUDA at the same instant.
This is [NVIDIA/nvidia-docker#1730](https://github.com/NVIDIA/nvidia-docker/issues/1730).

Nothing has to be upgraded for this to fire. On 2026-07-28 it was **snapd**, mid-morning, during a
routine `apt-daily` cycle:

```
08:48:24 systemd[1]: Reloading requested from client PID 2553912 ('systemctl') (unit snapd.service)
08:48:24 systemd[1]: Reloading...
```

The `/dev/nvidia*` device *nodes* stay visible inside the container, which is what makes this
confusing — only opening them is blocked. `nvidia-smi` in the container reports
`Failed to initialize NVML: Unknown Error`, and a fresh Python process reports
`No CUDA GPUs are available`, while the already-running ComfyUI keeps answering `/system_stats`
with plausible free-VRAM numbers from the CUDA context it opened *before* the revocation.

### Confirm it in ten seconds

```bash
docker exec comfyui nvidia-smi -L
```

`GPU 0: NVIDIA GeForce RTX 3090 (UUID: …)` — fine. `Failed to initialize NVML: Unknown Error` — the
device has been revoked. Check the other GPU containers too (`immich_machine_learning`,
`ebook2audiobook-…-gpu-1`); they go down together, because the trigger is host-wide.

### Fix

```bash
docker restart comfyui
```

Restarting re-runs the hook against a freshly created cgroup, and the GPU comes back. Restarting
the *process* inside the container does not help, and neither does ComfyUI's `/free` endpoint —
only re-creating the container's cgroup does.

Every GPU container needs its own restart:

```bash
docker restart comfyui immich_machine_learning ebook2audiobook-ebook2audiobook-gpu-1
```

### Preventing the recurrence

`docker restart` is a cure, not a fix — the next `daemon-reload` breaks it again. Two ways out,
neither free:

1. **CDI mode** (NVIDIA's recommended path). Generate a CDI spec and start the container with
   `--runtime=nvidia --device nvidia.com/gpu=all` instead of `--gpus all`. The devices then go into
   the OCI spec, `runc` emits real `DeviceAllow` entries for them, and a reload leaves them alone.
   Costs: one `nvidia-ctk cdi generate`, plus re-creating each GPU container.
2. **Switch Docker's cgroup driver to `cgroupfs`** (`exec-opts` in `/etc/docker/daemon.json`).
   Containers stop being systemd scopes, so a reload cannot touch them. Immune stack-wide and needs
   no per-container work, but it restarts every container on the box once and moves the host off
   the upstream-recommended driver.

### How the assistant reports it

`_gpu_revoked()` in `pipes/live/auto_assistant.py` separates this from a real squeeze using ComfyUI's
own execution timestamps. A genuine OOM dies part-way through streaming weights onto the card
(13.49 s, measured); a revoked device is refused at the first allocation, before any VRAM moves
(0.24–0.38 s, measured). Both signals — sub-3-second death *and* a card reporting >8 GB free — must
agree, and anything ambiguous falls through to the older "allocator is wedged" wording, whose advice
is the same restart. A revoked device also skips the automatic retry, which cannot succeed and
costs ~50 s of pointless VRAM-unload waiting.

Covered by `tests/test_gpu_diagnosis.py`.

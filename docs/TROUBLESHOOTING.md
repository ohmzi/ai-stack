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
   `--device nvidia.com/gpu=all` instead of `--gpus all`. The devices then go into the OCI spec,
   `runc` emits real `DeviceAllow` entries for them, and a reload leaves them alone. Costs: one
   `nvidia-ctk cdi generate`, plus re-creating each GPU container.
2. **Switch Docker's cgroup driver to `cgroupfs`** (`exec-opts` in `/etc/docker/daemon.json`).
   Containers stop being systemd scopes, so a reload cannot touch them. Immune stack-wide and needs
   no per-container work, but it restarts every container on the box once and moves the host off
   the upstream-recommended driver.

#### What was done here — comfyui is on CDI as of 2026-07-28

Docker 28.2.2 already reports `CDISpecDirs: [/etc/cdi /var/run/cdi]`, so CDI needed no daemon
change and no `--runtime=nvidia`; `--device` is enough.

```bash
sudo nvidia-ctk system create-dev-char-symlinks --create-all   # runc maps devices via /dev/char/MAJ:MIN
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

The `/dev/char` symlinks are the part that makes `DeviceAllow` work at all — without them `runc`
cannot name the devices to systemd. They do not survive a driver reload on their own, so
`/lib/udev/rules.d/71-nvidia-dev-char.rules` recreates them when the `nvidia` PCI driver binds.

The container is then created with `--device nvidia.com/gpu=all` and **no** `--gpus all`. Verified:
the scope now carries `DeviceAllow=/dev/char/195:0`, `195:254`, `195:255`, `511:0`, `511:1`, and
three consecutive `systemctl daemon-reload`s leave `nvidia-smi -L` and a live 2 GiB CUDA allocation
working.

**`immich_machine_learning` and `ebook2audiobook-…-gpu-1` are still on the legacy hook** and will
still lose the GPU on the next reload. They only need a `docker restart` to recover, so converting
them is optional.

#### The trap: re-creating the container loses hand-installed packages

Re-creating comfyui broke *video* while leaving images working — `RuntimeError in KSamplerAdvanced —
Failed to find C compiler`. SageAttention (`attention_sage` in ComfyUI-KJNodes, which the Wan
workflows use) JIT-compiles Triton kernels on first use, and Triton shells out to a C compiler to
build its CUDA driver shim. The base image has none: `gcc` had been `apt install`-ed by hand inside
the *running* container, so it lived only in that container's writable layer and vanished the moment
the container was replaced.

`docker diff` on the old container is what surfaced it — 27 added packages, the entire gcc/g++
toolchain and nothing else:

```bash
docker diff <container> | grep '^A /usr/bin/.*gcc'
```

That state was a landmine regardless of CDI: *any* recreate, image rebuild or `compose up
--force-recreate` would have silently broken video the same way. It is now a real image layer
(`comfyui-local:tier2-gcc`, `FROM comfyui-local:tier2` + `apt-get install -y gcc g++`), so it
survives a recreate. Images, edits and video all re-verified green on it afterwards.

Worth knowing: the image had also lost its `comfyui-local:tier2` tag at some point and was
referenced only by digest, one `docker image prune` away from being unrecoverable. It has been
re-tagged.

### How the assistant reports it

`_gpu_revoked()` in `pipes/live/auto_assistant.py` separates this from a real squeeze using ComfyUI's
own execution timestamps. A genuine OOM dies part-way through streaming weights onto the card
(13.49 s, measured); a revoked device is refused at the first allocation, before any VRAM moves
(0.24–0.38 s, measured). Both signals — sub-3-second death *and* a card reporting >8 GB free — must
agree, and anything ambiguous falls through to the older "allocator is wedged" wording, whose advice
is the same restart. A revoked device also skips the automatic retry, which cannot succeed and
costs ~50 s of pointless VRAM-unload waiting.

Covered by `tests/test_gpu_diagnosis.py`.

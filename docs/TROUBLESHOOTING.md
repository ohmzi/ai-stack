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

## A follow-up edit changed the wrong picture (2026-08-02)

**Symptom.** Generate an image, then ask for a change — "make this picture realistic" — and
what comes back is a *different scene entirely*. The recorded case: a cat leaping from a
burning building became a photo of a father and son. In Photoreal, "make this picture
animated" returned an unrelated person.

The edit instruction was fine. **The reference image was not.**

### What actually happens

Open WebUI asks the model for a chat title, tags, follow-up suggestions and a web-search
decision after every turn. Those prompts start `### Task:` — and they were reaching the media
pipes as ordinary requests, matching the image regexes, and running **real GPU renders**.
Root cause in [MODELS.md](MODELS.md#-a-task-model-that-isnt-visible-silently-becomes-the-chat-model-2026-08-02):
a task model absent from the visible registry is silently replaced by the chat's own model.

Each junk render then overwrote that chat's "last image", because the task calls carry the
real `chat_id`. The next genuine follow-up edited the junk. Three things had to fail together
for it to be invisible: the junk render, a history scan that could not see generated images
(Open WebUI 0.10 stores pipe replies in `message.output`, leaving `content` empty), and a
vision-QA step that verified the *rewritten instruction* against only the produced image — so
an edit that swapped the subjects passed with `qa_rounds=0`.

### Confirm it in ten seconds

```bash
python3 -c "
import json
p='/volume1/docker/openwebui/config/media_metrics.jsonl'
n=sum(1 for l in open(p) if l.strip() and '### Task' in json.dumps(d:=json.loads(l)) and d.get('ts','')>'2026-08-03')
print(n)"
```

`0` is the healthy answer, and `0` is what it printed when run on 2026-08-08. Anything above it means
a task prompt has reached a renderer *since the guard shipped*. `job=image` or `job=edit` rows whose
`request` field is task boilerplate are the junk renders.

> **Corrected 2026-08-08.** This section used to run
> `grep -c '### Task' /volume1/docker/openwebui/config/media_metrics.jsonl` and call **any** non-zero
> count a regression. That check can never come back clean. `media_metrics.jsonl` is append-only and
> still holds the incident's own rows: measured today, `grep -c` returns **40** — 21 `job=image` and
> 19 `job=edit`, every one stamped between 2026-07-31T15:16:53Z and 2026-08-02T17:30:23Z, which is
> before the guard existed. So the old wording published a permanent false alarm, and an operator
> following it would have read the historical record as a live regression. The command above asks the
> same question of rows written after the fix, and the date is the only difference.

### Fix

Already fixed in the pipes (Assistant 0.6.0, Image 1.4.0, Photoreal 0.6.0): each declares
`__task__` in `pipe()` and answers task prompts as text, the last image is persisted per chat
under `/app/backend/data/media_recent/`, and edit QA judges both images against the original
ask. If the symptom returns, check in this order:

1. the count above — anything but `0` and the task guard is gone (redeploy:
   `python3 scripts/deploy_pipe.py --all`);
2. `python3 tests/test_continuation.py` — 49 checks, no GPU, pins the whole contract;
3. `ls /volume1/docker/openwebui/config/media_recent/` — empty after images were generated
   means the `media_session` sidecar did not deploy (`tests/test_deployed.py` checks it).

Design and the research behind the prompt changes: [IMAGE_CONTINUATION.md](IMAGE_CONTINUATION.md).

## A fix that is committed, tested and green — and not running (2026-07-28, again 2026-08-08)

The worst-behaved failure in this repo, because every instrument you would reach for says you are
fine. The code is written. The tests pass. `git status` is clean. The server serves the previous
build, and will keep doing so indefinitely.

### What actually happens

Open WebUI does not import pipes from disk. Each Function's source is **a row in `webui.db`**, read
per request. Editing `pipes/auto_assistant.py` changes a file nothing loads. There is no error,
because nothing failed — the deploy step simply did not happen.

It has bitten twice, the second time *through* the check built to catch the first:

- **2026-07-28.** A fix was written, tested, reviewed and committed while the running instance went
  on serving the old build. `tests/test_deployed.py` was written as the backstop.
- **2026-08-08.** The backstop had the same hole one level down. It compared each `webui.db` row
  against `pipes/live/*.py` — the gitignored deployed copy — for all five pipes, but compared
  `pipes/live/` back to the *tracked* source for only **one** of them. `pipes/auto_assistant.py`,
  the pipe under heaviest development, ran **696 lines ahead** of its live copy through an entire
  committed feature while the suite printed ALL PASS, because the row faithfully matched a stale
  copy and nothing checked the copy.

There is a second, quieter version of the same trap: `tests/test_job_shape.py` and
`tests/test_hermes_delegation.py` take a pipe path and **default to `pipes/live/`**. Run bare, they
test the deployed code. On 2026-08-08 that meant a suite for a feature that was not deployed yet
failed with an `IndexError` — while the same suite passed against the tracked source. A suite that
passes and a suite that fails can both be reporting on the wrong file.

### Confirm it in ten seconds

```bash
python3 tests/test_deployed.py                              # both links, all five pipes
diff pipes/auto_assistant.py pipes/live/auto_assistant.py   # silence = deployed
```

### Fix

```bash
python3 scripts/deploy_pipe.py auto_assistant --dry-run
python3 scripts/deploy_pipe.py auto_assistant
```

Two things about the deploy that will stop you if you are not expecting them. It writes through
`sudo -n sqlite3` and preflights inside the container with `docker exec`, so it needs real
privileges — under a sandbox or `NoNewPrivileges` it fails with *"sudo: The 'no new privileges' flag
is set"* and nothing is written. And it **refuses** a file whose prose contains a literal
`from utils`, `from apps`, `from main` or `from config`: Open WebUI runs `replace_imports()` as a
naive whole-file `str.replace` over comments and docstrings too, so such a line is silently rewritten
on load and surfaces later as drift no diff of your own changes explains.

The pairs are now derived from the same `SOURCES` map the row check uses (`twin_pairs()`), so adding
a pipe cannot half-cover it — there is no second list to forget.

The same failure told from the test suite's side, with the reasoning for checking both links:
[QA_TEST_PLAN §1.5](QA_TEST_PLAN.md#15-the-deploy-check-and-why-a-green-suite-was-not-enough).

## The skin is back to stock, and every asset still returns 200 (2026-08-01)

**Symptom.** The UI renders in stock colours and the browser tab says "Open WebUI", while nothing you
would think to check disagrees: `GET /static/custom.css` and `GET /static/loader.js` answer **200 OK
with zero bytes**, and `index.html` still asks for the fingerprinted `?v=` URLs. A status code cannot
see this failure. Only the served *length* can.

### What actually happens

The branded assets live **inside the image** — the container's only bind mount is
`/app/backend/data` — so a `docker rm`, an image pull or a fork rebuild wipes them. On top of that,
`config.py:96-115` runs at import, i.e. on *every* container start: it unlinks every top-level **file**
in `STATIC_DIR` (`/app/backend/open_webui/static`, which is what `/static` serves) and copies
`/app/build/static/**/*` over it. Directories survive — which is why `ohmz-fonts/` did — and every
branded file does not.

On 2026-08-01 that made a plain `docker restart` revert the skin, because `branding/apply.sh` was
writing the served directory only: an earlier reading of the code had called `/app/build/static` "a
leftover served to nobody", when it is the source the startup rebuild copies from.

**That is the 2026-08-01 behaviour, not today's.** `apply.sh` now writes both directories, so the
rebuild reproduces the brand instead of undoing it, and `compose/openwebui/run.sh:32-34` records that
a plain restart no longer needs it. What still does is anything that replaces the image or the
container: a **recreate**, an **image pull**, and a **fork rebuild** — the assets are not in the DB
and not in a mount, so they go with it.

### Confirm it in ten seconds

```bash
python3 tests/test_branding.py             # asserts BYTES SERVED against the repo files
python3 tests/test_branding.py --restart   # also bounces the container and re-checks (~20 s)
```

That suite was written for this incident and asserts the one thing status codes miss. It also covers
the second failure surface with the same shape — correct files that nothing reads: the
`<link rel="manifest">` that pointed at the *backend* route (which returns `{"name": "Open WebUI"}`
and is what named every Android/iOS home-screen shortcut), the raw `<title>`, and the `?v=` stamp,
which is computed from the branding files so a changed mark actually changes the URLs.

### Fix

```bash
./branding/apply.sh            # idempotent; --revert puts the stock assets back
```

Then hard-refresh once (ctrl-shift-r). A browser that cached the old shell keeps asking for the old
URLs on heuristic freshness; after that one refresh the shell's `Last-Modified` is recent enough that
later skin changes propagate on their own.

After an upstream bump of the frontend fork, `apply.sh` is step 3 of four
(`compose/openwebui/fork/Dockerfile:28-34`): move `OWUI_REV` and the base digest together, rebuild —
a failing `git apply` is the signal to re-derive the patch, not to force past it — re-run
`branding/apply.sh`, then `python3 tests/test_deployed.py` and confirm the three mode buttons still
switch each other off.

The recreate/image-pull rule is already stated in the three places you are most likely to reach
first, so it is not restated here: [`compose/openwebui/run.sh`](../compose/openwebui/run.sh) prints it
as the next step after creating the container, README's branding section, and step 4 of a restore in
[BACKUPS.md](BACKUPS.md).

## The second SearXNG: a config file that changed owner, and an `up -d` that hits chat (2026-08-07)

**Symptom.** Two, from the same container. The first is fixed and recorded here because the cause is
not guessable: on 2026-08-07, writing `compose/searxng-hermes/settings.yml` failed with *Permission
denied* on a checkout you own, and `ls -l` showed the file as `nobody:nogroup`. The second still bites
— a roster change you edited, restarted and re-tested does not show up at `/config`.

### What actually happens

There are two SearXNG instances, and their rosters are kept deliberately near-disjoint. `searxng` on
`127.0.0.1:8888` serves OpenWebUI chat; `searxng-hermes` on `127.0.0.1:8889` serves background
monitors only, and `scripts/web_search.py` hard-defaults to `:8889` because it must never reach the
chat instance. Engine rate limits are per source IP, so a monitor that CAPTCHAs an engine chat depends
on degrades chat search **silently** — the model answers from training data and still looks grounded.
Which engines the hermes instance is actually *running* is a measured question, not a declared one:
the live roster, and how far it diverged from the file, are in
[TRACKING_ENHANCEMENT.md](TRACKING_ENHANCEMENT.md#still-open).

The container entrypoint chowns everything in `/etc/searxng` to `nobody:nogroup`. On 2026-08-07 the
hermes config was mounted as a directory, so that took ownership of `compose/searxng-hermes/` **and
the `settings.yml` inside it** — editing needed sudo, and every restart took it back. The mount is now
the single file, read-only (`compose/docker-compose.yml:101`), which leaves `/etc/searxng`
container-local and writable, so the entrypoint is satisfied and generates its own `limiter.toml`
there while the config stays ours. Measured 2026-08-08: `compose/searxng-hermes/settings.yml` is
`ohmz:ohmz` again, and the directory is still `nobody:nogroup` mode 775 — a leftover of that day that
nothing re-takes, and writable anyway because `ohmz` is in `nogroup`.

### Fix, and the rule

```bash
docker compose -f compose/docker-compose.yml up -d searxng-hermes
```

**Name the service.** A bare `up -d` would recreate the chat container too, and "chat search was not
touched" is the property this whole split exists to guarantee. A `settings.yml` edit is re-read on
`up -d` (or `restart`); a change to the *mount*, or to `docker-compose.yml` itself, needs the
container **recreated** rather than restarted — the `--force-recreate` on 2026-08-07 is what proved
the file had been reloaded and the roster was still wrong for some other reason.

Nothing in that settings file fails loudly: a wrong roster starts clean, exit 0, empty logs. So verify
against the live instance, not the file:

```bash
python3 scripts/flight_probe.py --stack      # Phase 0 only; zero site traffic
```

`probe_stack()` does the three-way check by itself — `web_search.ENGINE_ORDER` vs `settings.yml`'s
`keep_only` vs the live `127.0.0.1:8889/config` — and reports `missing_from_live` and
`unexpected_in_live` as separate fields, because they are different bugs: an addition that never
landed versus a removal that never landed. `tests/test_web_search.py` catches neither; it pins the two
declarations to each other and neither to the live instance.

## LAN exposure — the deliberate list (2026-08-01)

Docker-published ports **bypass ufw**, so the firewall is not the control here; the bind address
is. Reviewed once, deliberately, rather than closed ad hoc:

| Listener | Decision |
|---|---|
| ComfyUI `8188` | **Closed** — was `0.0.0.0` with no auth and a `/interrupt` that kills whoever is rendering. Now `127.0.0.1` (`compose/comfyui/run.sh`). Do not touch the in-container `--listen 0.0.0.0`: that is the bridge-namespace bind the docker-proxy needs. |
| OpenWebUI `4567` | **Kept on `0.0.0.0`** — owner decision, LAN devices open it directly. Public signup is also deliberately left **open**. Recorded in `compose/openwebui/run.sh`. |
| host `redis-server *:6379` | **Left as-is, flagged.** Not a container (`/usr/bin/redis-server`, pid on the host, no systemd unit found). It answers from the LAN IP with `-DENIED … protected mode`, so it refuses commands without a password — and every established client is `127.0.0.1`. Binding it to loopback would therefore break nothing observed, but it is not part of this stack, so it is the owner's call. |
| `hermes_api_key` | **Fixed** — was `0644` (any local user could read the key that authenticates to the agent gateway). Now `0640 root:ohmz` — world-read removed, owner and the uid-0 container both still read it. (0600 also works for the container but locks the owner out of host-side debugging for no gain.) |
| cloudflared token | **Flagged, not moved.** Visible in `ps aux`, so any local user can read the tunnel credential. Fixing means reconfiguring a working tunnel to use a credentials file — worth doing, but not worth breaking remote access unattended. |

The `~/.hermes/.env` (which holds the real gateway secret) was already `0600`.

# OpenWebUI local media Function pipes

A set of [OpenWebUI](https://github.com/open-webui/open-webui) **Functions** (pipe models)
that turn a local **Ollama + ComfyUI** stack into an auto-routing assistant plus standalone
image/video generators. Install them from *Workspace → Functions*, or with `scripts/deploy_pipe.py`
— see [Standing it up](#standing-it-up).

**Nothing on this page needs a forked OpenWebUI.** Every pipe and filter below is a database row
loaded by the stock backend, which is what makes them portable to any instance. Worth stating
plainly because **this host does run a fork** — the image is `ai-stack/open-webui:task-mode`, and
`compose/openwebui/fork/` rebuilds the *frontend* so the chat input carries three mutually-exclusive
mode buttons (Internet / Code / Task) and the sidebar gets a Background-tasks shortcut. The two facts
are unrelated: the fork changes the browser, the pipes run on the untouched Python backend, and
setting `compose/openwebui/run.sh` back to the digest-pinned upstream image leaves everything here
working. The tag, the build, the upstream-bump rule and the rollback are in
[The frontend fork](#the-frontend-fork) below; how the patch is regenerated is in
[../compose/openwebui/fork/gen/README.md](../compose/openwebui/fork/gen/README.md).

## Pipes
The Function column is the OpenWebUI function id — what `scripts/deploy_pipe.py` takes, and not
always the repo filename.

| Function (OpenWebUI id) | Model in the UI | What it does |
|---|---|---|
| `auto_assistant` | Ω Assistant | One model that routes by intent: chat (with vision when an image is in play), automatic coder routing, a RedCraft image (create or edit), a Wan 2.2 video (text-to-video, image-to-video, or multi-shot), and standing background jobs through the local hermes agent. Manages GPU VRAM around every job. |
| `image_krea` | *(hidden)* | RedCraft (Krea 2 base) text-to-image + Qwen-Image-Edit 2509 instruction editing, optional LoRA, local prompt-enhance, vision-QA correction. Hidden from the picker 2026-08-02; the function stays active. |
| `photoreal` | Photoreal | Lustify SDXL uncensored text-to-image; edits — including text-only follow-ups on the previous picture — go through Qwen-Image-Edit via the shared `identity_edit` module, with an SDXL img2img fallback. |
| `animate_scail` | Animate | SCAIL-2 (Wan 2.1 14B GGUF) motion transfer: attach a full-body character image and name one of three built-in motions — dance, wave or walk. See [SCAIL_ANIMATE.md](SCAIL_ANIMATE.md). |
| `flux_image` | *(disabled)* | FLUX.1-dev text-to-image pipe. Kept deployed for rollback; not selectable in the UI. |

Two **filters** are installed alongside them — see the README for both. `adaptive_memory` is the
vendored per-user memory filter. `task_mode` backs the **Task** button: while it is on, the turn goes
to the hermes background-task agent instead of being guessed at from the user's wording, and
Internet / Code are stood down server-side for that turn.

`tests/test_deployed.py` byte-compares **both** filters against their repo files. The asymmetry is
only in the deploy path: `task_mode` is in `scripts/deploy_pipe.py`'s `FILTERS` map and is deployed
by `--all`, while `adaptive_memory` is vendored, never edited here, is not in that map, and stays
pasted in by hand from *Workspace → Functions*.

## Shared modules (sidecars)
OpenWebUI execs each Function standalone, so a pipe cannot import a repo-relative module. The
shared code is therefore copied onto OpenWebUI's data mount and reached with a guarded
`sys.path` insert; every importer degrades gracefully when the copy is missing.

| Repo source | Deployed to | Used by |
|---|---|---|
| `pipes/shared/identity_edit.py` | `/app/backend/data/identity_edit.py` | Qwen-Image-Edit graph building, edit tiers, seed parsing |
| `pipes/shared/media_session.py` | `/app/backend/data/media_session.py` | Background-task guard, persistent per-chat last-image store, reference recovery, the prompt contracts |

`scripts/deploy_pipe.py` copies both as part of deploying (`SIDECARS`), and
`tests/test_deployed.py` fails if a deployed copy drifts from the repo.

## Requirements
- OpenWebUI — this stack runs the **local fork image** `ai-stack/open-webui:task-mode`, built from
  [`../compose/openwebui/fork/`](../compose/openwebui/fork/), not the official image; the Python
  backend and CUDA layers inside it are byte-identical digest-pinned upstream. With Ollama
  (`localhost:11434`) and ComfyUI (`localhost:8188`) reachable — the two endpoints
  `compose/openwebui/run.sh` (`OLLAMA_BASE_URL=http://127.0.0.1:11434`) and
  `compose/comfyui/run.sh` (`-p 127.0.0.1:8188:8188`) actually use.

  > **This bullet read "OpenWebUI (official image)" until 2026-08-08.** The two endpoints were right;
  > the image was not. Followed literally it means running stock, which comes up with no
  > Internet / Code / Task buttons in the chat input and no Background-tasks shortcut in the sidebar,
  > and nothing fails to say so — see [The frontend fork](#the-frontend-fork). Building the fork also
  > adds prerequisites this list never carried: Docker with network access, because the build clones
  > open-webui at `OWUI_REV` and runs `npm ci && npm run build` inside `node:22-alpine`.
- Background jobs — the "standing background jobs" row in the pipes table needs a whole component
  this list did not name until 2026-08-08: the `hermes-gateway` systemd **user** service on
  `127.0.0.1:8642`, a second Ollama tag `hermes-genesis:agent` at `num_ctx 65536`, the gateway's API
  key staged at `/volume1/docker/openwebui/config/hermes_api_key` so the pipe can read it in-container
  (as `/app/backend/data/hermes_api_key`), `hermes/plugins/gpuguard/` symlinked to
  `~/.hermes/plugins/gpuguard/` as the cron provider, and `scripts/hermes_delivery.py` on a
  one-minute user timer, which is what performs delivery. Miss them and the advertised capability
  cannot work at all; an empty or absent key file makes the pipe report the feature unavailable
  rather than 401ing, which is the only signal you get. Install steps: [HERMES_AGENT.md](HERMES_AGENT.md).
- Ollama: the model roles (chat, vision, task, router, embeddings) and their measured VRAM are in
  [MODELS.md](MODELS.md) — the single source of truth. Do not restate tags here; that is how this
  list went stale.
- ComfyUI with the referenced checkpoints/GGUFs (RedCraft on a Krea 2 base, Wan 2.2 A14B,
  Qwen-Image-Edit 2509, Lustify SDXL, SCAIL-2, …).

## Standing it up
Until 2026-08-08 this page's entire setup content was the Requirements list above plus "install them
from *Workspace → Functions*". Followed as written it leaves no image built, no container created, no
support services running, no skin applied and no Function rows written — the model picker shows none
of the models in the table above, because those models *are* the rows step 5 writes. Every step below
is transcribed from the code that performs it; README's Requirements block carries the same first
three commands, and this is the ordered form with the traps attached.

1. **Build the fork image** — `docker build -t ai-stack/open-webui:task-mode compose/openwebui/fork/`.
   Details and the bump rules: [The frontend fork](#the-frontend-fork).
2. **Create the container** — `compose/openwebui/run.sh`. Idempotent, safe to re-run after an image
   pull; it `docker rm -f`s the old one first. It exists because until 2026-08-01 the container had
   been created by hand and its recipe "lived nowhere but `docker inspect` on the running instance",
   so a machine rebuild or an accidental `docker rm` meant reconstructing it from memory. Three of its
   choices are load-bearing and annotated in the file: the GPU is attached with
   `--device nvidia.com/gpu=all` (CDI) because the legacy `--gpus all` path injects cgroup rules a
   `systemctl daemon-reload` silently strips; `UVICORN_WORKERS=1` is pinned so the pipe's in-process
   generation lock is the only one; and `WEBUI_SECRET_KEY` is read from
   `/volume1/docker/openwebui/secret.env` so restarts stop signing everyone out.
3. **Bring up the support services** — `docker compose -f compose/docker-compose.yml up -d`. Five of
   them, each on a deliberately chosen host port because OpenWebUI runs `--network host` and reaches
   them at `localhost:<port>`: tika `:9998` (document extraction), searxng `:8888` (chat search),
   searxng-hermes `:8889` (background monitors only), kokoro `:8081` (TTS), infinity-rerank `:7997`
   (`BAAI/bge-reranker-v2-m3` on CPU, in the live request path, not optional polish). Afterwards
   **name the service** whenever you touch only one —
   `docker compose -f compose/docker-compose.yml up -d searxng-hermes`. A bare `up -d` recreates the
   chat container too, and "chat search was not touched" is the property the two-instance split
   exists to guarantee.
4. **Apply the skin** — `./branding/apply.sh`. Both served static directories live *inside* the image
   and the container's only bind mount is `/app/backend/data`, so a `docker rm`, a fork rebuild or an
   image pull wipes the skin; re-run after any of the three. A plain `docker restart` no longer needs
   it.
5. **Deploy the Functions** — `python3 scripts/deploy_pipe.py --all`. That covers the mapped ids
   only: the five pipes, the `task_mode` filter, and both sidecars. It does **not** cover
   `adaptive_memory`, which is not in the `FILTERS` map and is pasted in by hand. Step 6 will not
   catch that for you: it byte-compares `adaptive_memory` once the row exists, but when the function
   was never installed it prints `has no installed function called 'adaptive_memory' — not deployed`
   and does **not** fail, deliberately, because a repo file with no installed row is usually a
   retirement. A skipped paste stays green.
6. **Prove it landed** — `python3 tests/test_deployed.py` (33 checks). It compares the database row
   against the repo file for every mapped id, the tracked source against the gitignored `pipes/live/`
   copy for the five pipes, and both sidecar copies against `pipes/shared/`.

## The frontend fork
Operator-critical, and until 2026-08-08 written down nowhere but code comments.

| | |
|---|---|
| Image tag | `ai-stack/open-webui:task-mode` — what `compose/openwebui/run.sh` ends its `docker run` with |
| Build | `docker build -t ai-stack/open-webui:task-mode compose/openwebui/fork/`, then `compose/openwebui/run.sh` (which already points at that tag) |
| What is rebuilt | The frontend only. `compose/openwebui/fork/Dockerfile` clones open-webui at `OWUI_REV`, `git apply --verbose`s `task-mode.patch`, runs `npm ci && npm run build` in `node:22-alpine`, then COPYs `/src/build` onto a base pinned **by digest** rather than by the `cuda` tag. Backend, CUDA layers and every dependency stay byte-identical upstream — the patch cannot reach them. |
| What it adds | Three mutually-exclusive mode buttons in the chat input (Internet / Code / Task), and a sidebar Background-tasks shortcut that resolves the delivery channel by name (`background-tasks`). |
| Rollback to stock | Put the digest from that Dockerfile's `FROM` line into `run.sh` instead. The fork adds nothing the backend depends on, so nothing else has to change and every pipe and filter keeps working. |

After an upstream bump, in this order (`compose/openwebui/fork/Dockerfile` is the source for all four):

1. Move `OWUI_REV` and the base digest **together** — they must describe the same build, or the
   frontend and the backend disagree.
2. Rebuild. `git apply` runs with no fuzz and no 3-way, so it fails loudly on conflict; that failure
   is the signal to re-derive the patch, not something to force past. Regeneration procedure:
   [../compose/openwebui/fork/gen/README.md](../compose/openwebui/fork/gen/README.md).
3. Re-run `branding/apply.sh` — the static assets live inside the image, as always.
4. `python3 tests/test_deployed.py`, then confirm **by hand** that the three buttons still switch
   each other off.

**Nothing tests the running image.** `tests/test_deployed.py` compares Function rows and the two
sidecar copies against the repo; it makes no assertion about the image the container was created
from. So an upstream `docker pull`, or a `docker run` that names the stock image, silently reverts the
three mode buttons and the Background-tasks shortcut while every suite stays green — the operator's
first symptom is a chat input with an Integrations dropdown where the mode buttons used to be. Step 4
above is the only check that exists.

## Public instance

Everything above stands up the **private** instance — the one with pipes, filters and the
Internet/Code/Task fork. It has a separate, disposable sibling at `aipublic.ohmz.cloud`: no pipes,
no login, one model, on its own network with no route to ComfyUI or the hermes gateway. Different
compose project (`compose/public/`), different bootstrap order, different everything except the
branding step. Not part of this page's stand-up order on purpose — standing it up requires the
private instance's Ollama already running, but nothing here depends on the public instance existing.
Full procedure: [PUBLIC_INSTANCE.md](PUBLIC_INSTANCE.md).

## Highlights
- **Intent routing** with question/small-talk guards — a question about an image isn't turned into an edit.
- **Conversation continuity** — a follow-up ("make this picture realistic") edits the picture already on the table, recovered from history or the persistent per-chat store; OpenWebUI's own background-task prompts are answered as text and never render. See [IMAGE_CONTINUATION.md](IMAGE_CONTINUATION.md).
- **Wan 2.2 A14B** video incl. multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification of every generation against the request, with corrections — edits are judged on both the original and the result, against the user's own wording.
- **VRAM choreography** — a generation lock, real VRAM-release polling, idle-gated ComfyUI unloads.
- **Native status line** — a live elapsed-time ticker collapsing to `Generated in 1m 05s · RedCraft · 1024×1024 · 8 steps`.

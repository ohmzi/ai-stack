# OpenWebUI local media Function pipes

A set of [OpenWebUI](https://github.com/open-webui/open-webui) **Functions** (pipe models)
that turn a local **Ollama + ComfyUI** stack into an auto-routing assistant plus standalone
image/video generators. Install them from *Workspace → Functions* — they run inside the stock
OpenWebUI image, no fork or source patch required.

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

`adaptive_memory` is also installed, as a **filter** rather than a pipe — see the README.

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
- OpenWebUI (official image) with Ollama (`localhost:11434`) and ComfyUI (`localhost:8188`) reachable.
- Ollama: the model roles (chat, vision, task, router, embeddings) and their measured VRAM are in
  [MODELS.md](MODELS.md) — the single source of truth. Do not restate tags here; that is how this
  list went stale.
- ComfyUI with the referenced checkpoints/GGUFs (RedCraft on a Krea 2 base, Wan 2.2 A14B,
  Qwen-Image-Edit 2509, Lustify SDXL, SCAIL-2, …).

## Highlights
- **Intent routing** with question/small-talk guards — a question about an image isn't turned into an edit.
- **Conversation continuity** — a follow-up ("make this picture realistic") edits the picture already on the table, recovered from history or the persistent per-chat store; OpenWebUI's own background-task prompts are answered as text and never render. See [IMAGE_CONTINUATION.md](IMAGE_CONTINUATION.md).
- **Wan 2.2 A14B** video incl. multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification of every generation against the request, with corrections — edits are judged on both the original and the result, against the user's own wording.
- **VRAM choreography** — a generation lock, real VRAM-release polling, idle-gated ComfyUI unloads.
- **Native status line** — a live elapsed-time ticker collapsing to `Generated in 1m 05s · RedCraft · 1024×1024 · 8 steps`.

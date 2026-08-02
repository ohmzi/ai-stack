# ai-stack

A self-hosted [Open WebUI](https://github.com/open-webui/open-webui) deployment
that does more than chat: it routes between local chat/image/video models,
watches things on a schedule and texts you when they happen, and is measured by
a repeatable eval suite instead of vibes.

Everything runs on the stock Open WebUI image — the pipes and filters install
from *Workspace → Functions*, so there is no fork and no source patch.

```
pipes/       Open WebUI Function pipes (the models you pick in the UI)
filters/     Open WebUI filters
scripts/     the alerting side — monitors, delivery, transports
hermes/      hermes-agent plugins, symlinked into ~/.hermes
compose/     support services (Tika, SearXNG, Kokoro, reranker)
branding/    the OhmzAI skin
tests/       unit tests, live QA harnesses and the eval suite
docs/        runbooks, setup, model notes, QA plan
```

## Pipes

The Function column is the OpenWebUI function id, which is what `scripts/deploy_pipe.py` takes
and what the DB rows are keyed on — it is not always the repo filename.

| Function (OpenWebUI id) | Model in the UI | What it does |
|---|---|---|
| `auto_assistant` | Ω Assistant | One entry that routes by intent: chat (with vision when an image is in play), automatic coder routing, a RedCraft image (create, or edit via Qwen-Image-Edit), a Wan 2.2 video (text-to-video, image-to-video, or a multi-shot sequence), and standing background jobs through the local hermes agent. Vision-QA on stills and video frames, a confirmation step before a video render, and VRAM choreography around every job. |
| `image_krea` | *(hidden)* | RedCraft (Krea 2 base) text-to-image + Qwen-Image-Edit 2509 instruction editing, optional trained LoRA, local prompt-enhance and vision-grounded edit-rewrite, two-round vision-QA correction. Hidden from the picker on 2026-08-02 — the Assistant covers the same two jobs. Restore by setting `image_krea.krea2` active in the `model` table. |
| `uncensored` (`pipes/photoreal.py`) | Photoreal | Lustify SDXL uncensored text-to-image. Edits — including text-only follow-ups on the previous picture — run through Qwen-Image-Edit via the shared `identity_edit` module so the subject stays the same person, with an SDXL img2img fallback when Qwen is unavailable. |
| `animate_scail` | Animate | SCAIL-2 (Wan 2.1 14B GGUF) motion transfer: attach a full-body character image and name one of three built-in motions — dance, wave or walk. Your text picks the motion; it is not a prompt. ~2 s clip, ~3 min. |
| `flux_image` | *(disabled)* | FLUX.1-dev text-to-image. Kept deployed and byte-current for rollback; not selectable in the UI. |

`auto_assistant`'s "animate this image" is Wan 2.2 **I2V** — text-directed motion out of the
still. That is a different engine from the Animate pipe, which transfers a preset motion and
ignores your wording.

Highlights:

- **Intent routing** with question/small-talk guards — a question *about* an
  image isn't turned into an edit — and default-deny on media intent.
- **Conversation continuity** — "make this picture realistic" keeps editing
  *that* picture. A persistent per-chat reference store survives deploys and
  model switches, and OpenWebUI's own background-task prompts can never reach a
  renderer. [docs/IMAGE_CONTINUATION.md](docs/IMAGE_CONTINUATION.md).
- **Wan 2.2 A14B** video including multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification against the request on the Assistant and Image
  pipes, with a corrected retry. Edits are judged on *both* images — original and
  result — against the user's original wording, so a rewrite that drifted off the
  subject fails instead of passing. (Photoreal does not verify — it trades that
  for seed control and per-job metrics.)
- **VRAM choreography** — a generation lock, real VRAM-release polling,
  idle-gated ComfyUI unloads, and recovery from a wedged allocator on OOM.
- **Native status line** — a live elapsed ticker collapsing to
  `Generated in 1m 05s · RedCraft · 1024×1024 · 8 steps`.

## Filters

| Function | What it does |
|---|---|
| `adaptive_memory` | Vendored Adaptive Memory v4.4.1 (`1818TusculumSt/owui-adaptive-memory`) — extracts, dedupes and embeds per-user memories, then prepends them to the last user message. Attached to `Ω Assistant` specifically rather than globally. Provenance and the license caveat: [docs/CAPABILITY_UPGRADE_PLAN.md](docs/CAPABILITY_UPGRADE_PLAN.md). |

### The picker is curated on purpose

**The model dropdown shows exactly three entries — `Ω Assistant`, `Animate`, `Photoreal` — and
that is the intended state, not drift.** Everything else is hidden behind an inactive `model`
row: `image_krea`, `flux_image`, and every raw Ollama tag (`hermes-genesis:apex-compact`,
`hermes-genesis:agent`, `bge-m3`, `gemma3:1b`, `gemma4:e2b`).

The reasoning: the Assistant already routes to image, edit and video, so a picker full of
near-duplicates is a choice the user shouldn't have to make. `Photoreal` and `Animate` stay
because neither is reachable from the Assistant — one is a different checkpoint, the other a
different engine.

Hiding costs nothing here because **nothing reaches those models through OpenWebUI**: the pipes
call Ollama on `http://localhost:11434` themselves and retrieval goes through
`rag.embedding_engine`. Two consequences worth knowing before you change any of it:

- A hidden model is also **uncallable** through OpenWebUI — the picker and the dispatcher read
  the same map. There is no hidden-but-dispatchable state.
- That is why **no task model is in effect** — see
  [openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md). Being the task model and
  being hidden are mutually exclusive.

`Animate` also carries a `model` row for a second reason: a manifold entry with no row inherits
`models.default_metadata`, which switches *every* capability on. Its row turns off web search,
the code interpreter, image generation, terminal and the builtin tools — that pipe reads one
attached image plus a motion keyword and ignores tool output entirely.

`tests/test_deployed.py` pins this: it fails if the tables above stop matching the running
instance.

## Standing jobs and alerts

"Monitor this price for 2 weeks" becomes a real cron job. The agent side is
documented in [docs/HERMES_AGENT.md](docs/HERMES_AGENT.md); delivery lives in
`scripts/`.

The design point worth knowing: **delivery is not left to the model.** Jobs
write `LOG`/`ALERT` lines and a watcher delivers them, so a model that goes
quiet cannot silently drop an alert. Alerts are verified, retried 3× at 5-minute
spacing, and recorded per attempt. Job creation is checked against the
scheduler, not the agent's word. Price checks are arithmetic done in Python, not
asked of a 34B model. Monitors report their own failures — once, after
confirming them.

Transport is SMTP plus carrier email-to-SMS gateways. Two failures that cost
real time and are now pinned by tests: texts containing links are silently
dropped by carriers, and an alert address whose domain has no MX record fails
without saying so.

## Models

One 24 GB RTX 3090, and as of the 2026-07-26 consolidation almost everything
runs on a single tenant:

| Slot | Model | Role |
|---|---|---|
| Everything | `hermes-genesis:apex-compact` | Chat, code, vision, prompt helpers |
| Task model | `gemma3:1b` | Titles, tags, RAG queries — also what the pipes answer task prompts with directly |
| Router | `gemma3:1b` | Chat-vs-code classifier inside the pipe |
| Embeddings | `bge-m3` | RAG, 1024-dim / 8192-token |

None of these appear in the model picker. Every one has an inactive `model` row, which is
OpenWebUI's hide switch — the picker is curated down to the three pipes above. That is safe
here because the pipes reach Ollama over `http://localhost:11434` directly and retrieval goes
through `rag.embedding_engine`, so neither path consults the model registry. It is *not* free
in general: hiding a model also makes it uncallable through OpenWebUI
([openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md) has the mechanism, and the
task-model consequence).

Image generation is **RedCraft** (`redcraft23INT8INT4FP8_30Krea2.safetensors`, a Krea 2
base) at the creator's own settings — 8 steps, cfg 1.0, `er_sde`/simple. Krea 2 Turbo is
still on disk beside it; reverting is a one-line change in both image pipes.

Every VRAM number in [docs/MODELS.md](docs/MODELS.md) is a measured `nvidia-smi`
delta. **Do not budget from `/api/ps`** — it omits the multimodal projector and
the CUDA context, and understated real usage by up to 2.8 GB. Several figures in
older docs were wrong because of exactly that.

## Support services

`compose/docker-compose.yml` — Tika (document extraction), SearXNG (web search),
Kokoro (TTS), and an Infinity cross-encoder reranker. Port choices are deliberate
and explained inline; the upstream defaults collide with other services on this
host.

The reranker is not optional polish: with hybrid search on and no reranker, Open
WebUI re-embeds the fused candidates and re-sorts by plain cosine, discarding the
BM25 half at the final ranking step.

```bash
docker compose -f compose/docker-compose.yml up -d
```

## Branding

[`branding/`](branding/) holds the OhmzAI skin — warm-dark shell, one amber
accent, Ω mark — applied to the running container:

```bash
./branding/apply.sh
```

It re-themes through Tailwind v4 token overrides rather than a selector chase, so
it survives upstream class-name churn. The served static directory lives inside
the image, so re-run it after any `docker rm` or image pull. Details and the
`WEBUI_NAME` caveat: [branding/README.md](branding/README.md).

## Tests and evals

```bash
python3 -m pytest tests/
```

`tests/` covers routing, media intent, GPU diagnosis, alert setup, templating,
transports and delivery. Beyond unit tests:

- `tests/eval/` — a repeatable evaluation suite with objective graders and a
  checked-in baseline. The judge honours `cases.json`; self-grading was removed.
- `tests/qa_live.py`, `tests/media_metrics.py` — live orchestration QA and media
  measurement.
- `tests/test_continuation.py` — the follow-up-after-an-image contract: task
  detection, reference recovery across every message shape Open WebUI sends, the
  persistent store, and the routing guards in all three image pipes. No GPU.
- `tests/bench_models.py` — head-to-head model benchmarking.
- `tests/test_deployed.py` — fails when the running Open WebUI instance has code
  that differs from this repo, including the shared sidecar modules.

Methodology and the current baseline: [docs/QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md).

## Requirements

- Open WebUI (official image) with Ollama (`localhost:11434`) and ComfyUI
  (`localhost:8188`) reachable.
- ComfyUI with the referenced checkpoints/GGUFs (RedCraft on a Krea 2 base,
  Wan 2.2 A14B, Qwen-Image-Edit 2509, Lustify SDXL, SCAIL-2, …).

## Docs

| | |
|---|---|
| [STACK_SETUP.md](docs/STACK_SETUP.md) | How the stack is put together |
| [HERMES_AGENT.md](docs/HERMES_AGENT.md) | Standing jobs: what runs, why, how to undo it |
| [MODELS.md](docs/MODELS.md) | Model roles, measured VRAM, the consolidation |
| [QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md) | Methodology and baselines |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Failures seen in practice |
| [IMAGE_CONTINUATION.md](docs/IMAGE_CONTINUATION.md) | Why a follow-up edits *that* picture: the task guard, the reference store, the prompt contracts |
| [openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md) | Sanitized workspace config, and which config rows the runtime actually reads |
| [KREA_LORA_GUIDE.md](docs/KREA_LORA_GUIDE.md) · [SCAIL_ANIMATE.md](docs/SCAIL_ANIMATE.md) | Media pipe guides |
| [UPGRADE_ROADMAP.md](docs/UPGRADE_ROADMAP.md) · [CAPABILITY_UPGRADE_PLAN.md](docs/CAPABILITY_UPGRADE_PLAN.md) · [VIDEO_QUALITY_ROADMAP.md](docs/VIDEO_QUALITY_ROADMAP.md) | Where this is going |

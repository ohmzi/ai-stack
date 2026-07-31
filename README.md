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
compose/     support services (Tika, SearXNG, Kokoro, reranker)
branding/    the OhmzAI skin
tests/       unit tests, live QA harnesses and the eval suite
docs/        runbooks, setup, model notes, QA plan
```

## Pipes

| Function | Model in the UI | What it does |
|---|---|---|
| `auto_assistant` | 🪄 Assistant | One entry that decides: chat (with vision), a Krea 2 image (create or edit), a Wan 2.2 video, or animate a still. Routes by intent and manages VRAM around every job. |
| `image_krea` | Image | Krea 2 Turbo text-to-image + Qwen-Image-Edit instruction editing, local prompt-enhance, vision-QA correction. |
| `photoreal` | Photoreal | Lustify SDXL uncensored image pipe. |
| `animate_scail` | Animate | SCAIL motion-transfer: drive a still with a motion clip. |
| `flux_image` | — | FLUX.1-dev text-to-image. |

Highlights:

- **Intent routing** with question/small-talk guards — a question *about* an
  image isn't turned into an edit — and default-deny on media intent.
- **Wan 2.2 A14B** video including multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification of every generation against the request.
- **VRAM choreography** — a generation lock, real VRAM-release polling,
  idle-gated ComfyUI unloads, and recovery from a wedged allocator on OOM.
- **Native status line** — a live elapsed ticker collapsing to
  `Generated in 1m 05s · Krea 2 · 1024×1024 · 8 steps`.

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
| Task model | `gemma4:e2b` | Titles, tags, RAG queries, QA judge (thinking off) |
| Router | `gemma3:1b` | Chat-vs-code classifier inside the pipe |
| Embeddings | `bge-m3` | RAG, 1024-dim / 8192-token |

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
- `tests/bench_models.py` — head-to-head model benchmarking.
- `tests/test_deployed.py` — fails when the running Open WebUI instance has code
  that differs from this repo.

Methodology and the current baseline: [docs/QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md).

## Requirements

- Open WebUI (official image) with Ollama (`localhost:11434`) and ComfyUI
  (`localhost:8188`) reachable.
- ComfyUI with the referenced checkpoints/GGUFs (Krea 2 Turbo, Wan 2.2 A14B,
  Qwen-Image-Edit, Lustify SDXL, …).

## Docs

| | |
|---|---|
| [STACK_SETUP.md](docs/STACK_SETUP.md) | How the stack is put together |
| [HERMES_AGENT.md](docs/HERMES_AGENT.md) | Standing jobs: what runs, why, how to undo it |
| [MODELS.md](docs/MODELS.md) | Model roles, measured VRAM, the consolidation |
| [QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md) | Methodology and baselines |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Failures seen in practice |
| [openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md) | Sanitized workspace config, and which config rows the runtime actually reads |
| [KREA_LORA_GUIDE.md](docs/KREA_LORA_GUIDE.md) · [SCAIL_ANIMATE.md](docs/SCAIL_ANIMATE.md) | Media pipe guides |
| [UPGRADE_ROADMAP.md](docs/UPGRADE_ROADMAP.md) · [CAPABILITY_UPGRADE_PLAN.md](docs/CAPABILITY_UPGRADE_PLAN.md) · [VIDEO_QUALITY_ROADMAP.md](docs/VIDEO_QUALITY_ROADMAP.md) | Where this is going |

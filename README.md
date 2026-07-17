# OpenWebUI local media Function pipes

A set of [OpenWebUI](https://github.com/open-webui/open-webui) **Functions** (pipe models)
that turn a local **Ollama + ComfyUI** stack into an auto-routing assistant plus standalone
image/video generators. Install them from *Workspace → Functions* — they run inside the stock
OpenWebUI image, no fork or source patch required.

## Pipes
| Function | Model | What it does |
|---|---|---|
| `auto_assistant` | 🪄 Assistant | One model that decides: chat (with vision), a Krea 2 image (create or edit), a Wan 2.2 video, or animate a still. Routes by intent and manages GPU VRAM around every job. |
| `image_krea` | Image | Krea 2 Turbo text-to-image + Qwen-Image-Edit instruction editing, local prompt-enhance, vision-QA correction. |
| `flux_image` | — | FLUX.1-dev text-to-image pipe. |
| `animate_scail` | Animate | SCAIL motion-transfer: drive a still with a motion clip. |

## Requirements
- OpenWebUI (official image) with Ollama (`localhost:11434`) and ComfyUI (`localhost:8188`) reachable.
- Ollama: a chat model (e.g. `dolphin-venice:24b`) + a vision model (`gemma4:31b`) for image QA.
- ComfyUI with the referenced checkpoints/GGUFs (Krea 2 Turbo, Wan 2.2 A14B, Qwen-Image-Edit, …).

## Highlights
- **Intent routing** with question/small-talk guards — a question about an image isn't turned into an edit.
- **Wan 2.2 A14B** video incl. multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification of every generation against the request, with corrections.
- **VRAM choreography** — a generation lock, real VRAM-release polling, idle-gated ComfyUI unloads.
- **Native status line** — a live elapsed-time ticker collapsing to `Generated in 1m 05s · Krea 2 · 1024×1024 · 8 steps`.

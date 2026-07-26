# Model roles on the local stack

Single 24 GB RTX 3090. **Every number here is measured** (`nvidia-smi` delta on a clean idle
baseline), not estimated — see the warning about `/api/ps` below.

| Slot | Model | Real VRAM | tok/s | Role |
|---|---|---|---|---|
| **Chat + pipe helpers** | `dolphin-venice:24b` (dense 24B) | **16584 MiB** | 49.9 | Text chat, and every prompt helper the pipes run — enhance, edit-rewrite, shot-planning, prompt-merge. Uncensored (the Photoreal pipe's enhancer needs a model that won't refuse). |
| **Code + vision** | `hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` (MoE, ~3 B active) | **18372 MiB** | **119.6** gen / 123.7 vision | Coding turns, **and all vision work** — image Q&A and the `_verify_image` QA check. |
| **Task model** | `gemma4:e2b` | **3307 MiB** | 166.6 | Chat titles, tags, RAG query generation. Thinking is OFF (see below). ⚠️ see "the phantom". |
| **Router classifier** | `gemma3:1b` | **1313 MiB** | 235.4 | The HINT-tier chat-vs-code classifier in the pipe. Co-resides with the coder. |
| _retired_ | `gemma4:31b` | 21772 MiB | 33.4 | **No longer used.** Kept on disk for rollback only. |

Idle baseline is **1023–1070 MiB**, and it is not what the old version of this doc claimed: the
desktop is only ~186 MiB (Xorg + gnome-shell + TeamViewer + nautilus). The rest is
**ComfyUI 444 MiB + the Open WebUI CUDA embedder 360 MiB**.

> ### ⚠️ Never budget from `/api/ps`
> `size_vram` omits the multimodal projector and the ~305 MiB CUDA context. It understates real VRAM
> by **1189 MiB** on the coder, **1451 MiB** on `gemma4:e2b` and **2828 MiB** on `gemma4:31b`. Use
> `nvidia-smi` deltas. Several figures in older docs were wrong because of this.
>
> Working formula, validated to within 16 MiB on all five models:
> `nvidia-smi delta = model_buffer + KV_cache + compute_buffer + mmproj + ~305 MiB CUDA context`

## Vision runs on the coder (2026-07-26)

`gemma4:31b` was retired. Qwen3.6-35B-A3B is multimodal — it carries a 1134 MiB `mmproj` and declares
`vision` — and it reads an image at **123.7 tok/s vs gemma4:31b's 33.4**, at 18372 MiB instead of
21772 MiB. Verified end-to-end through the live pipe: correct description of a test image in 9.2 s,
with `images[]` confirmed on the wire.

The bigger win is churn, not speed. Ollama *predicted* 25.5 GiB for `gemma4:31b` at 32k context —
more than the card — so it evicted **every** other model unconditionally on every image turn. Vision
now lands on a tenant that is often already resident.

Rollback: set `self.vision_model` back to `"gemma4:31b"` in `pipes/auto_assistant.py`. The tag is
still on disk; do not `ollama rm` it until a real render-QA pass is green.

## Co-residency — measured, and order matters

Only **two models fit at once** at the current `OLLAMA_CONTEXT_LENGTH=32768`. Any 3-way is impossible.

| Combination | Result |
|---|---|
| coder → `gemma3:1b` | ✅ co-resident, 20715 MiB |
| `gemma3:1b` → coder | ❌ the 1b gets evicted |
| coder ↔ `gemma4:e2b` | ❌ evicts in **both** orders |
| coder ↔ `dolphin` | ❌ evicts in **both** orders |
| any 3 models | ❌ impossible at 32k |

At `num_ctx=8192` a genuine triple works (`e2b → 1b → dolphin`, 20161 MiB), so
`OLLAMA_MAX_LOADED_MODELS=3` is **fiction at 32768 and real at 8192**.

### ⚠️ The `gemma4:e2b` phantom

Ollama's scheduler *predicts* **7.5 GiB** for `gemma4:e2b`, which really uses 3.23 GiB — a **4.3 GiB
phantom reservation**. It therefore needs ~9.4 GiB free to load and cannot co-reside with the coder
in either order. This is the single largest VRAM inefficiency on the box.

The old rationale in this doc — *"the task model is tiny on purpose so a title/tag generation never
evicts the resident chat model"* — is **falsified by measurement**. It evicts the coder on every new
chat. `gemma3:1b` (1313 MiB, no phantom) does co-reside. Reverting the task model is a pending
decision; the cost of not doing it is ~6 s of warm reload on the first coder message after a new chat.

## Context length is pure cost

Throughput is **identical** at 4k and 32k (dolphin 49.5–49.9, coder 123.4–124.9). The 32k default
buys nothing and costs VRAM:

| Model | cost of 32k vs 8k |
|---|---|
| `dolphin-venice:24b` | **1837 MiB** |
| `gemma4:31b` (retired) | 1207 MiB |
| coder | 327 MiB |
| `gemma4:e2b` | 124 MiB |

Most of that lever belongs to dolphin. ⚠️ Do **not** drop the global to 8192 while `--context-shift`
is on: oversized RAG and web-search turns would degrade silently instead of erroring.

## KV cache: keep `q8_0`

| KV type | dolphin total / tok/s | verdict |
|---|---|---|
| f16 | 18982 MiB / 50.5 | ❌ +2398 MiB for +0.6 tok/s, and it pushes `gemma4:31b` to CPU spill (−48%) |
| **q8_0** | **16584 MiB / 49.9** | ✅ current |
| q4_0 | 15304 MiB / 49.8 | ❌ saves 1280 MiB but measurably degrades long-context recall |

q4_0 was tested, not assumed: a 30-question exact-match battery went **22/30 → 20/30**, and mean
first-token logprob on planted facts fell from −0.528 to −0.818 nats, with the penalty *widening*
with depth (−0.545 at 22.7k tokens). Quantized KV costs no throughput on Ampere with flash attention
on — but it costs accuracy. Cutting context buys more, for free.

## Task model: `think: false`

`gemma4:e2b` is a thinking model and OpenWebUI's task calls don't pass `think:false`, so by default
it "thought" through every chat title (~1.4 s, ~871 hidden reasoning chars) and could return empty
output on a tight token budget. There is no Modelfile switch — `PARAMETER think false` is rejected.

The fix is an OpenWebUI model param, since 0.10.2 forwards `think` straight through
(`utils/payload.py` → `ollama_root_params`, and `ModelParams` uses `extra='allow'`):

```
model row `gemma4:e2b` → advanced params → { "think": false }
```

Verified: titles now generate in **~0.04 s** warm with zero thinking.

## Considered, not adopted

- **A 30–35B MoE for the *task* slot** — rejected. MoE saves compute, not memory; ~18 GB resident
  would evict the chat model on every title. Wrong slot.
- **Replacing dolphin with a smaller DENSE chat model so it fits beside the coder** —
  arithmetically impossible. The ceiling for a coder co-tenant is 24115 − 18372 − 1023 = 4720 MiB
  physical, and Ollama's floor cuts that to ~3.3 GiB, i.e. a 4B-class model. Even a 12B Q4
  (~8.1 GiB) fails: 18044 + 8140 > 24115.
- **`gemma4:e2b` as the vision model** — superseded; vision went to the coder instead, which costs
  zero extra GB and zero downloads.
- **q4_0 KV cache** — measured and rejected above.

## Open: replacing dolphin with an uncensored MoE

The highest-value remaining swap, and this doc has flagged it before. Measured on this box, MoE vs
dense at comparable residency: **coder 119.6 tok/s at 18372 MiB vs dolphin 49.9 tok/s at 16584 MiB**
— a 30–35B-A3B-class uncensored chat model would be ~2.4× faster than dolphin for +1.8 GiB.

The blocker is not VRAM, it is the *uncensored* requirement: the Photoreal pipe's prompt enhancer
needs a model that won't refuse. Abliterated builds measurably degrade quality, so this is a real
trade, not a free upgrade.

A cheaper variant worth trying first: point `chat_model` at the **coder** as well. Chat, code and
vision would then be one tenant — 18372 MiB, zero eviction churn, 2.4× faster chat — but the
Photoreal helper would still need an uncensored model, so dolphin could not be deleted outright.

## Rollback

- Vision → `gemma4:31b`: edit `self.vision_model` in `pipes/auto_assistant.py`.
- Task model → `gemma3:1b`: set `task.model.default` / `task.model.external`, restart OpenWebUI.

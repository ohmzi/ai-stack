# Model roles on the local stack

Single 24 GB RTX 3090. **Every number here is measured** (`nvidia-smi` delta on a clean idle
baseline), not estimated — see the warning about `/api/ps` below.

**As of 2026-08-01 the box runs FIVE models, one of which does almost everything.**
(Was four until `hermes-genesis:agent` — the 65536-ctx tag hermes-agent needs — joined the
roster on 2026-07-29. Same weights as `apex-compact`, ~0 extra disk, but a SEPARATE runner:
Ollama keys runners by model+options, so the two cannot co-reside on this card.)

| Slot | Model | Real VRAM | tok/s | Role |
|---|---|---|---|---|
| **Everything** | `hermes-genesis:apex-compact` (MoE, ~3 B active of 34.7 B) | **18285 MiB** | **135.3** | Chat, code, vision, and the uncensored prompt helpers — across `auto_assistant`, `photoreal` and `image_krea`. |
| **Task model** | `gemma3:1b` | **1313 MiB** | 235.4 | Chat titles, tags, RAG query generation. **Reverted from `gemma4:e2b` on 2026-08-01** — see "the phantom": e2b was measured EVICTING the 16.70 GiB tenant on every title generation. `gemma3:1b` co-resides (21298/24576 measured). |
| **QA judge** | `gemma4:e2b` | 3307 MiB | 166.6 | Still the eval judge (cross-family control). Kept on disk; no longer in the request path. ⚠️ see "the phantom". |
| **Router classifier** | `gemma3:1b` | **1313 MiB** | 235.4 | The HINT-tier chat-vs-code classifier in the pipe. Co-resides with the main tenant. |
| **Embeddings** | `bge-m3:latest` | ~941 MiB, transient | — | RAG embeddings via the Ollama engine. 1024-dim, 8192-token window. |
| **Background agent** | `hermes-genesis:agent` | ~17 GB (**not measured here** — see note) | — | The tag `hermes-agent` runs cron jobs on. Same weights as `apex-compact` via `ollama create` + `PARAMETER num_ctx 65536`, so ~0 extra disk — but Ollama keys runners by model+options, making it a **separate ~17 GB runner** that cannot co-reside with the 32768-ctx chat tenant. A tick firing mid-conversation evicts chat and the next turn pays a cold reload, **measured at 22.7 s**. That is why the pipe releases the chat tenant before handing off, and why the GPU guard exists. Full account: [HERMES_AGENT.md](HERMES_AGENT.md). |

> **The agent row is the one number on this page that is not an `nvidia-smi` delta.** It was missing
> from this table entirely until 2026-08-08 — the paragraph above counted it toward "FIVE models"
> while the table listed only the other four distinct tags, so the doc contradicted itself for a
> week. The `~17 GB` is carried over from `HERMES_AGENT.md` and the pipe's own comment rather than
> re-measured, and it is flagged instead of quietly formatted like the measured rows, because
> "every number here is measured" is the claim this file opens with.

**Deleted 2026-07-26** — ~55 GB reclaimed, disk 277 → 332 GB free:
`hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` (18 GB) · `dolphin-venice:24b` (14 GB) ·
`gemma4:31b` (19 GB) · `gemma4:e2b-it-qat` (4.3 GB) · `qwen3-embedding:0.6b` · `embeddinggemma:300m`
(the last three were never referenced by anything).

Idle baseline is **~630 MiB** since the 2026-07-26 embeddings move: the desktop is only ~186 MiB
(Xorg + gnome-shell + TeamViewer + nautilus — not the "880–1050 MiB desktop overhead" the older docs
claimed, corrected in `UPGRADE_ROADMAP.md` §4 “The VRAM budget after Wave 1 + 2.1”), and the rest is **ComfyUI idle 444 MiB**. Open WebUI
now holds **zero** VRAM: pointing retrieval at the Ollama engine (`rag.embedding_engine = "ollama"` —
the Embeddings row above) stops OWUI constructing a local SentenceTransformer at all, which freed the
360 MiB CUDA embedder it used to hold permanently. Measured at 664 MiB immediately after the switch
(`UPGRADE_ROADMAP.md` §1.5, the SHIPPED Embeddings row — “baseline 1029→664 MiB”) and decomposed as
186 + 444 in `UPGRADE_ROADMAP.md` §4 (“Baseline changes from 1023–1070 MiB to **~630 MiB**”).

> **Cited by section and quoted phrase, not line number — deliberately, since 2026-08-08.** The three
> line numbers this paragraph used (`:955`, `:953-955`, and `:621` further down) all broke the same
> day they were written, because `UPGRADE_ROADMAP.md` was being edited above them and every insertion
> shifted the target. `:955` landed on unrelated VQA prose. A pointer that silently moves is worse
> than no pointer: it makes a measured number read as unsourced.

> **Superseded 2026-08-08.** Until today this paragraph read "Idle baseline is **1023–1070 MiB** …
> The rest is **ComfyUI 444 MiB + the Open WebUI CUDA embedder 360 MiB**". That is the
> pre-2026-07-26 figure, and it stayed in the present tense for the fortnight after the embedder was
> removed, contradicting this doc's own Embeddings row. It is recorded here because it remains the
> correct baseline for any measurement taken before 2026-07-26. What the staleness cost: the
> co-tenant ceiling under "Considered, not adopted" was subtracting 1023 MiB, so anyone sizing a
> second tenant off this page under-counted free VRAM by ~393 MiB.

> ### ⚠️ Never budget from `/api/ps`
> `size_vram` omits the multimodal projector and the ~305 MiB CUDA context. It understates real VRAM
> by **1189 MiB** on the coder, **1451 MiB** on `gemma4:e2b` and **2828 MiB** on `gemma4:31b`. Use
> `nvidia-smi` deltas. Several figures in older docs were wrong because of this.
>
> Working formula, validated to within 16 MiB on all five models:
> `nvidia-smi delta = model_buffer + KV_cache + compute_buffer + mmproj + ~305 MiB CUDA context`

## ⚠️ This model accepts only ONE system message

Its embedded chat template cannot parse more than one. Send two and Ollama fails the whole request:

```
HTTP 400 — "Unable to generate parser for this template.
            Automatic parser generation failed: ... While executing CallExpression at line 85"
```

Measured: **1 system message works, 2 is a hard 400.** Stock Qwen3.6 handled 2 fine, so this is
specific to this build.

That is exactly the shape `keep_system=True` produces — the pipe's own guard plus OpenWebUI's
memory/RAG context — so **every turn carrying a memory or a system convention returned an error
string** until it was fixed. `_achat_stream` now collapses all system messages into one, guard first,
which is the portable shape most chat templates expect anyway. Four assertions in `test_manifold.py`
lock it in.

Worth noting how this was caught: `bench_models.py` missed it completely, because it only ever sends
a single user message. `qa_live.py` caught it because cases A3 and C3 deliberately test
system-message delivery. A benchmark that only exercises the happy path will not find this class of
defect.

## Vision runs on the coder (2026-07-26)

`gemma4:31b` was retired. Qwen3.6-35B-A3B is multimodal — it carries a 1134 MiB `mmproj` and declares
`vision` — and it reads an image at **123.7 tok/s vs gemma4:31b's 33.4**, at 18372 MiB instead of
21772 MiB. Verified end-to-end through the live pipe: correct description of a test image in 9.2 s,
with `images[]` confirmed on the wire.

The bigger win is churn, not speed. Ollama *predicted* 25.5 GiB for `gemma4:31b` at 32k context —
more than the card — so it evicted **every** other model unconditionally on every image turn. Vision
now lands on a tenant that is often already resident.

⚠️ `gemma4:31b` has since been **deleted**. Rolling vision back to it now requires
`ollama pull gemma4:31b` (19 GB) first — see Rollback at the foot of this doc.

## Co-residency — measured, and order matters

Only **two models fit at once** at the current `OLLAMA_CONTEXT_LENGTH=32768`. Any 3-way is impossible.

The table below was measured before the 2026-07-26 consolidation and names models that are now
deleted. It is kept because the *shape* still governs the box: the main tenant co-resides with
`gemma3:1b` and with nothing larger. In practice there is now only one large model, so eviction
churn between big tenants no longer arises at all.

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

> **Acted on 2026-08-01.** Measured directly: loading `gemma4:e2b` while the tenant was
> resident left ONLY e2b in `/api/ps` — it evicted 16.70 GiB to load 1.81. `gemma3:1b` co-resides
> (both present, 21298/24576 MiB). Task model reverted. The known counter-argument, recorded so it
> is not lost: a 1b model is weaker at RAG query reformulation
> (`openwebui-improvement-plan.md:255`). That cost is UNMEASURED; the eviction cost is not. If
> retrieval recall ever feels weak, measure the two on real queries rather than swapping back.

Ollama's scheduler *predicts* **7.5 GiB** for `gemma4:e2b`, which really uses 3.23 GiB — a **4.3 GiB
phantom reservation**. It therefore needs ~9.4 GiB free to load and cannot co-reside with the coder
in either order. This is the single largest VRAM inefficiency on the box.

The old rationale in this doc — *"the task model is tiny on purpose so a title/tag generation never
evicts the resident chat model"* — is **falsified by measurement**. (Recorded before the 2026-08-01
revert — see the blockquote above. While `gemma4:e2b` held the task slot it evicted the coder on
every new chat.) `gemma3:1b` (1313 MiB, no phantom) does co-reside. Leaving `gemma4:e2b` in the task
slot cost ~6 s of warm reload on the first coder message after each new chat; the 2026-08-01 revert
is what removed that.

## Context length is pure cost

Throughput is **identical** at 4k and 32k (dolphin 49.5–49.9, coder 123.4–124.9). The 32k default
buys nothing and costs VRAM:

| Model | cost of 32k vs 8k |
|---|---|
| `dolphin-venice:24b` | **1837 MiB** |
| `gemma4:31b` (retired) | 1207 MiB |
| coder | 327 MiB |
| `gemma4:e2b` | 124 MiB |

Most of that lever belonged to dolphin, which is now deleted — so on the current single-tenant setup
the 32k default costs only ~327 MiB. ⚠️ Do **not** drop the global to 8192 while `--context-shift`
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

*(Historical — the live task model is `gemma3:1b`, which does not think. Kept because the
mechanism still applies to any thinking model put in this slot.)*

`gemma4:e2b` is a thinking model and OpenWebUI's task calls don't pass `think:false`, so by default
it "thought" through every chat title (~1.4 s, ~871 hidden reasoning chars) and could return empty
output on a tight token budget. There is no Modelfile switch — `PARAMETER think false` is rejected.

The fix is an OpenWebUI model param, since 0.10.2 forwards `think` straight through
(`utils/payload.py` → `ollama_root_params`, and `ModelParams` uses `extra='allow'`):

```
model row `gemma4:e2b` → advanced params → { "think": false }
```

Verified: titles now generate in **~0.04 s** warm with zero thinking.

## ⚠️ A task model that isn't *visible* silently becomes the chat model (2026-08-02)

`task.model.default` / `task.model.external` are not honoured unconditionally.
`get_task_model_id` (`utils/task.py:16-27`) only uses the configured id **if that id is
present in the loaded model registry**; otherwise it falls back to the chat's current model —
which, for a chat on a pipe, is *the pipe*. And a raw Ollama tag with no `model` row is
admin-only in 0.10.2, so `gemma3:1b` was invisible and every title/tag/follow-up/search-query
prompt was executed by the media pipes.

That is not merely wasteful, though it was that too — `media_metrics.jsonl` showed 14–174 s
**GPU renders** of `### Task:` boilerplate. Those renders also overwrote each chat's
last-image memory, which is what made a follow-up edit act on the wrong picture. Full
incident: [IMAGE_CONTINUATION.md](IMAGE_CONTINUATION.md).

The pipes now defend themselves: each declares `__task__` in `pipe()` and answers task
prompts as plain text on `gemma3:1b` (OpenWebUI pops `metadata` before the pipe sees the
body, so the kwarg is the only usable marker). Giving `gemma3:1b` a real `model` row plus an
access grant would additionally fix it server-side; the in-pipe guard makes that optional.

## ComfyUI checkpoints (the media side)

Not Ollama tenants, but they compete for the same 24 GB, so they belong in the same ledger.

| Job | File | Notes |
|---|---|---|
| Text-to-image | `krea2/redcraft23INT8INT4FP8_30Krea2.safetensors` | **RedCraft** (Krea 2 base, INT8/INT4/FP8-scaled), 12.2 GB. Creator's spec: `ER_SDE`/Euler, simple, **cfg 1.0, 8–12 steps** — the pipes run 8. No trigger words. Swapped in 2026-08-02. |
| Text-to-image (previous) | `krea2/krea2_turbo_fp8_scaled.safetensors` | Krea 2 Turbo. **Kept on disk** for rollback: change `self.unet` in `image_krea.py` and the `unet_name` in `auto_assistant._build_t2i_wf`. |
| Instruction editing | `Qwen-Image-Edit-2509-Q4_K_M.gguf` | + `Qwen-Image-Edit-2509-Lightning-4steps` LoRA for the fast tiers. Shared CLIP/VAE with the t2i path. |
| Uncensored t2i | `lustifySDXL.safetensors` | Photoreal only. |

Raising steps toward 10–12 is the sanctioned quality lever for RedCraft; **cfg stays 1.0**,
where a negative prompt is mathematically inert (measured — byte-identical output with and
without one). Anything that depends on a negative must run a cfg > 1 tier.

## Considered, not adopted

- **A 30–35B MoE for the *task* slot** — rejected. MoE saves compute, not memory; ~18 GB resident
  would evict the chat model on every title. Wrong slot.
- **Replacing dolphin with a smaller DENSE chat model so it fits beside the coder** —
  arithmetically impossible. The ceiling for a coder co-tenant is 24115 − 18372 − 630 = 5113 MiB
  physical (recomputed 2026-08-08 on the ~630 MiB baseline; it read 24115 − 18372 − 1023 = 4720 MiB
  while the stale 1023 MiB figure stood, and the conclusion does not move), and Ollama's per-model
  free-memory floor cuts the usable budget to ~3.3 GiB (`UPGRADE_ROADMAP.md` §2 “Wave 2 — needs a decision or a download”, “co-tenant under ~3.3 GiB”), i.e. a 4B-class
  model. Even a 12B Q4 (~8.1 GiB) fails: 18044 + 8140 > 24115.
- **`gemma4:e2b` as the vision model** — superseded; vision went to the coder instead, which costs
  zero extra GB and zero downloads.
- **q4_0 KV cache** — measured and rejected above.

## Resolved: dolphin replaced (2026-07-26)

This doc flagged the swap for weeks; it is now done. `hermes-genesis:apex-compact` is an uncensored
Qwen3.6-35B-A3B derivative, so it satisfies the Photoreal enhancer requirement **and** the coding
role at once — 135.3 tok/s at 18285 MiB, versus dolphin's 49.9 at 16584.

The blocker was never VRAM, it was the *uncensored* requirement. Measured refusal rates on five
prompt-enhancer-style requests settle it:

| Model | Complied |
|---|---|
| stock `Qwen3.6-35B-A3B` | **3/5** — refused 2 |
| `dolphin-venice:24b` | 5/5 |
| **`hermes-genesis:apex-compact`** | **5/5** |

That gap is the entire reason dolphin survived this long, and closing it is what let the box go from
three large tenants to one.

> **SUPERSEDED — kept for the measurements, not the recommendation.** Everything below
> this line was written before the consolidation and argues for a swap that has since
> happened: chat/vision/coder all point at `hermes-genesis:apex-compact` today
> (`self.chat_model` / `self.vision_model` / `self.coder_model` in `pipes/auto_assistant.py`,
> currently lines 456/461/466). **Citation corrected 2026-08-08** — it read "pipe lines
> 330/335/340", which by now lands in the job-ownership block (`TASK_OWNERS_FILE`,
> `OWNER_PRUNE_S`, `OWUI_DB`); the symbols are the durable reference, the line numbers are not.
> Read it as a record of how the decision was reached.

**Now measured, 2026-07-26.** The refusal gap is real: on five prompt-enhancer-style requests the
coder refused **2 of 5**, while `dolphin` and an uncensored Qwen3.6 both complied 5/5. So `dolphin`
cannot simply be deleted — something uncensored has to take its place.

The recommended candidate is **`HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive`**
(1.93M downloads, Apache-2.0, abliterated directly from official Qwen3.6, mmproj bundled, pullable at
standard quant tags). Full evaluation, including why a superficially similar "Hermes" repo was
rejected on provenance, is in `UPGRADE_ROADMAP.md` §0.

A cheaper variant worth trying first: point `chat_model` at the **coder** as well. Chat, code and
vision would then be one tenant — 18372 MiB, zero eviction churn, 2.4× faster chat — but the
Photoreal helper would still need an uncensored model, so dolphin could not be deleted outright.

## Rollback

The 2026-07-26 consolidation deleted the models it replaced, so reverting means re-pulling. Costs are
download size, not just config.

| To undo | Steps |
|---|---|
| **The whole consolidation** | `ollama pull hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` (17.7 GB) and `ollama pull dolphin-venice:24b` (14 GB), then revert `chat_model`/`vision_model`/`coder_model` in `pipes/auto_assistant.py`, `self.text_model` in `pipes/photoreal.py` (**function id** `photoreal`, shown as "Photoreal" — renamed from the function id `uncensored` on 2026-08-02, commit 35d629f), and both in `image_krea.py`.

> **Corrected 2026-08-08.** A note here briefly claimed a pre-rename `uncensored.py` file "no longer exists". No such file ever existed: `git log --diff-filter=A` shows the pipe was added as `pipes/photoreal.py` in 504d102 and has never been renamed. What changed in 35d629f was the **OpenWebUI function id** — the key on the `function` DB row — not a filename. The distinction matters because `scripts/deploy_pipe.py` takes the function id, and the two have drifted apart before. Git history has the exact prior values. |
| **Vision only** | `ollama pull gemma4:31b` (19 GB), set `self.vision_model`. |
| **Task model → `gemma4:e2b`** | set `task.model.default` / `task.model.external` back, restart OpenWebUI. No download — it is still installed, and still carries `{"think": false}`. Note the visibility trap above: an id the registry cannot see is ignored in favour of the chat's model. |
| **Image checkpoint → Krea 2 Turbo** | `self.unet` in `pipes/image_krea.py` and `unet_name` in `auto_assistant._build_t2i_wf`, then `python3 scripts/deploy_pipe.py --all`. The file was never deleted. |
| **Re-create the main model** | The source GGUFs are kept at `/home/ohmz/models/hermes-genesis/` (18.3 GB) precisely so this does not need a re-download: `ollama create hermes-genesis:apex-compact -f Modelfile`. Worth keeping — the upstream repo's `:latest` tag resolves to **V3**, not the V5 build in use here, so a re-download would not reproduce it. |

DB backups taken along the way: `webui.db.bak-genesis` (before the swap), `webui.db.bak-embedder`,
`webui.db.bak-onepipe`, `webui.db.bak-autofull`.

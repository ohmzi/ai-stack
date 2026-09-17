# Model roles on the local stack

Single 24 GB RTX 3090. **Every number here is measured** (`nvidia-smi` delta on a clean idle
baseline), not estimated — see the warning about `/api/ps` below.

**As of 2026-08-17 the box runs SIX models.** The coder role split off the shared tenant onto its
own tag — see "The coder split (2026-08-17)" below for the full account.

| Slot | Model | Real VRAM | tok/s | Role |
|---|---|---|---|---|
| **Chat + vision** | `hermes-genesis:apex-compact` (MoE, ~3 B active of 34.7 B) | **18285 MiB** | **135.3** | Chat and vision, and the uncensored prompt helpers — across `auto_assistant`, `photoreal` and `image_krea`. No longer the coder — see below. |
| **Coder** | `qwen38-coder:q4` (Qwen3.8-27B dense Q4_K_M, official Ollama build, mmproj dropped) | **16881 MiB** @ 32K ctx | not benchmarked | The coder route in `auto_assistant.py` (`self.coder_model`), and the sole model OpenCode talks to. Cannot co-reside with the chat tenant — see below. |
| **Coder — Claude Code** | `qwen38-coder:q4-128k` (same weights as `qwen38-coder:q4`) | **21857 MiB** @ 128K ctx, 100% GPU ⚠️ (**a resident total, not a delta** — see below) | not benchmarked | The tag the `deepseek` harness serves to Claude Code (its local `sonnet` alias). Same weights as the coder via `ollama create` `FROM qwen38-coder:q4`, with only `PARAMETER num_ctx 131072` overridden — so it shares the 32768 tag's template and sampling parameters, and adds ~0 disk — but Ollama keys runners by model+options, making it a **second ~21 GB runner** that cannot co-reside with the 32768-ctx coder tenant or the chat tenant on one 24 GB card. **That consequence is reasoned, not measured under contention.** Full account: "The Claude Code 128K tag (2026-09-17)" below. |
| **Task model** | `gemma3:1b` | **1313 MiB** | 235.4 | Chat titles, tags, RAG query generation. **Reverted from `gemma4:e2b` on 2026-08-01** — see "the phantom": e2b was measured EVICTING the 16.70 GiB tenant on every title generation. `gemma3:1b` co-resides (21298/24576 measured). |
| **QA judge** | `gemma4:e2b` | 3307 MiB | 166.6 | Still the eval judge (cross-family control). Kept on disk; no longer in the request path. ⚠️ see "the phantom". |
| **Router classifier** | `gemma3:1b` | **1313 MiB** | 235.4 | The HINT-tier chat-vs-code classifier in the pipe. Co-resides with BOTH the chat tenant and the coder — measured 18957 MiB with the coder, 2026-08-17. |
| **Embeddings** | `bge-m3:latest` | ~941 MiB, transient (664 MiB observed resident) | — | RAG embeddings via the Ollama engine. 1024-dim, 8192-token window. Also OpenCode's `local_code_index` embedder since 2026-08-17 (was LM Studio's nomic-embed, 768-dim, dead backend). |
| **Background agent** | `hermes-genesis:agent` | ~17 GB (**not measured here** — see note) | — | The tag `hermes-agent` runs cron jobs on. Same weights as `apex-compact` via `ollama create` + `PARAMETER num_ctx 65536`, so ~0 extra disk — but Ollama keys runners by model+options, making it a **separate ~17 GB runner** that cannot co-reside with the 32768-ctx chat tenant. A tick firing mid-conversation evicts chat and the next turn pays a cold reload, **measured at 22.7 s**. That is why the pipe releases the chat tenant before handing off, and why the GPU guard exists. Full account: [HERMES_AGENT.md](HERMES_AGENT.md). |

> **The agent row is one of two numbers on this page that are not an `nvidia-smi` delta** — the
> other is the `qwen38-coder:q4-128k` row, a resident total rather than a delta (see "The Claude Code
> 128K tag" below). It was missing
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

## The coder split (2026-08-17)

The 2026-07-26 consolidation put chat/code/vision on one tenant with a documented caveat: the
coding benchmark scored every candidate 27/27, showing *no detectable regression*, not equal
quality — and the upstream author states the build is tuned for uncensored roleplay, not code.
Qwen3.8-27B (weights 2026-08-14, Apache 2.0) shipped an official Ollama Q4_K_M build with native
tool-calling and thinking, so the coder role split off onto its own tag. Chat/vision are
unaffected — `hermes-genesis:apex-compact` still holds them, and still passes the Photoreal
uncensored regression guard 5/5 (re-verified 2026-08-17).

**Prerequisite: Ollama upgraded 0.32.1 → 0.32.14.** Qwen3.8 support landed in 0.32.12; 0.32.14
additionally fixes non-leading system messages, which the coder route's guard + `keep_system`
combination needs. The `multi-model.conf` drop-in and CDI GPU access on all three containers were
confirmed intact after the required `daemon-reload`. One measured side effect: this version range
also changed the `repeat_penalty` default from 1.1 to 1.0 for models that don't set it explicitly
— `hermes-genesis`'s Modelfile is one of those (no `PARAMETER` overrides, by design), and a direct
A/B (repeat_penalty 1.0 vs 1.1) confirmed the classifier drift below is NOT explained by this;
the effective sampling shift on hermes-genesis itself was not otherwise probed.

**The tag:** `qwen38-coder:q4`, built from the official `qwen3.8:27b-q4_K_M` with a
single-`FROM` Modelfile (`/home/ohmz/models/qwen38-coder/Modelfile`) that drops the ~884 MiB vision
projector — the coder route never receives images (`not attached_img` at the dispatch site) and
neither does OpenCode — and pins `num_ctx 32768` plus Qwen's published non-thinking sampling
(temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.0 / repeat_penalty 1.0).

**Real VRAM, measured 2026-08-17:** 16881 MiB @ 32K context (baseline 763 → 17644 MiB with the
coder alone resident) — well under the ~19288 MiB a uniform-attention estimate predicted. Ollama
reports this model's architecture as `qwen35`: a **hybrid SSM/attention** design (65 layers, only
every 4th does full attention; the rest are fixed-state Mamba-style layers), which needs far less
KV cache than a dense transformer of the same size. `gemma3:1b` co-resides comfortably: 18957 MiB
total with both loaded, against the 24115 MiB usable ceiling — about 5.2 GB of headroom, more than
double what the pre-measurement estimate assumed.

**The runner-key trap is real, not hypothetical — reproduced directly.** Ollama keys a running
model by tag *and* options. OpenCode sends no `num_ctx` and inherits the server's
`OLLAMA_CONTEXT_LENGTH=32768`; the pipe's `_fit_ctx` sizes per-turn and would ask for 16384 on most
messages. A live test confirmed a request at `num_ctx=16384` evicted a resident 32768-context
runner (**and `gemma3:1b` alongside it**) rather than reusing it. `auto_assistant.py`'s coder
branch now sends a fixed `CODER_CTX = 32768` for exactly this reason — matching what OpenCode
inherits is not optional.

**The swap cost, measured 2026-08-17:** hermes→coder ~6.2 s, coder→hermes ~12.4 s (both faster
than the 22.7 s baseline the `hermes-genesis:agent` row above cites — plausibly because the box's
disk cache was warm from the model having just been pulled). They cannot co-reside
(18285 + 16881 ≫ 22172, the co-resident ceiling), so every chat↔code alternation is a full swap,
serialized under the existing `_locked_stream`/`_GEN_LOCK` so a swap can never land mid-render. The
pipe now emits a "Loading the coding model…" status before the lock wait, since `_locked_stream`'s
own ticker only starts once the lock is held — during the load itself the user previously saw
nothing. `OLLAMA_KEEP_ALIVE` was left at `60s`; raising it would pin ~17 GB for the whole window
and starve ComfyUI, so this is a real ongoing tradeoff, not a solved problem.

**⚠️ A measured side effect on the HINT-tier classifier, unrelated to any of the above changes.**
`gemma3:1b`'s response to one specific ambiguous test phrase ("my python keeps dying on me") now
comes back CODE where `tests/test_autoroute.py --live` expects CHAT for at least one of its two
identical calls. Confirmed NOT caused by the coder-route code changes (the classifier prompt/model/
call path in `_classify_code` was not touched) and NOT the `repeat_penalty` default (tested both
1.0 and 1.1 directly — identical CODE verdict either way). No pre-upgrade baseline exists to prove
the Ollama version bump caused this rather than it having always been borderline, but the
72-Ollama-release gap (0.32.1→0.32.14) is the only thing that changed in this path. **This now
costs more than it used to**: a HINT-tier misclassification to CODER used to be free (same
resident tag); it now triggers a real ~6-12 s swap. Worth watching via `route_metrics`
(`job:route`, `rule_id:code_classifier`) rather than acting on a single test phrase — per
`docs/ROUTING_ROADMAP.md`'s "earn a heuristic with data first" policy, retuning the classifier
prompt needs its own broader validation, not a one-line reaction to this.

**OpenCode**, previously configured against two dead backends (LM Studio never running; Lucebox's
model directory gone), now points at this same Ollama tenant — one backend for the whole box. Its
`local_code_index` MCP embeddings moved from LM Studio's dead nomic-embed endpoint to `bge-m3` via
Ollama (also confirming the `LMSTUDIO_EMBEDDING_AUTOLOAD=0` guard actually prevents the 300 s hang
the old fallback path risked — `bge-m3` was observed loaded and serving, not stuck). Full config in
`~/.config/opencode/opencode.json` / `profile.env`; `validate-profile-sync.sh` passes.

## The Claude Code 128K tag (2026-09-17)

The `deepseek` harness exists to run Claude Code against a backend selectable mid-session, because
Claude Code reads `ANTHROPIC_BASE_URL` once at launch and has no documented way to switch backends
inside one session. Its local alias is this tag.

**Why a second tag.** The stock `qwen38-coder:q4` pins `num_ctx 32768`, which is fine for OpenCode —
it sends a lean prompt — and unusable as a Claude Code backend: Claude Code's baseline request alone
(system prompt plus every tool schema) measures roughly 25K tokens, so a 32K window leaves almost
nothing for the actual conversation. The first real turn overflows, and Ollama answers:

```
400 request (33142 tokens) exceeds the available context size (32768 tokens)
```

**The tag:** `qwen38-coder:q4-128k`, built with
`ollama create qwen38-coder:q4-128k -f models/qwen38-coder-128k.Modelfile`
(`/home/ohmz/ai-stack/harness/deepseek/models/qwen38-coder-128k.Modelfile`). It is
`FROM qwen38-coder:q4` with a single override, `PARAMETER num_ctx 131072`, so it inherits that tag's
template and its pinned non-thinking sampling profile (temp 0.7 / top_p 0.8 / top_k 20 /
`repeat_penalty` 1.0, and `presence_penalty` 1.0 — which the base tag deliberately sets *below*
Qwen's published 1.5, for the reasons in its own Modelfile comment). The two tags answer with
identical style and differ only in how much they can hold — same weights, so **~0 extra disk**.

**Real VRAM, measured 2026-09-17:** 21857 MiB resident (`nvidia-smi`), `ollama ps` reporting 21 GB,
100% GPU, CONTEXT 131072. The full window fits in VRAM with no CPU offload. The reason it fits is the
q8_0 KV cache (`OLLAMA_KV_CACHE_TYPE=q8_0`): it works out to roughly **40 KB/token**, not the
~128 KB an fp16 estimate predicts. 128K is therefore the comfortable ceiling on a 24 GB card; 256K
would not fit and would start spilling.

**The window advertised is smaller than the tag's.** The router reserves `output_reserve` 32768 —
Claude Code asks for `max_tokens=32000`, and the backend window covers prompt *and* completion — so
what Claude Code is told it has is 98304, not 131072. Advertising the raw window leaves nothing for
the reply and the request fails 400.

**⚠️ The 21857 MiB above is a resident total, not a delta, and is not comparable to the coder row.**
Every other measured figure in the table is an `nvidia-smi` delta against an idle baseline — the 32K
coder row's 16881 MiB is 17644 − 763 — whereas this one is the card's absolute `memory.used` with the
tag loaded. Subtracting an idle baseline from it would be guesswork, because this file carries two
different baselines: the **~630 MiB** headline (186 MiB desktop + 444 MiB ComfyUI) and the **763 MiB**
used for the 2026-08-17 measurement. Re-measure the baseline before deriving a co-tenant budget from
this row. The row is flagged rather than quietly reformatted for the same reason as the note below.

**⚠️ The co-residency cost is reasoned, not measured under contention.** Ollama keys runners by
model+options, so a 131072-ctx tag of the same weights is a **second ~21 GB runner** that cannot
co-reside with the 32768-ctx coder tenant or with the chat tenant on one 24 GB card. That follows
from the 21857 MiB footprint measured above plus the model+options rule this file already documents
for `hermes-genesis:agent`. It has **not** been measured under contention, and is flagged rather
than formatted like the measured rows, because "every number here is measured" is the claim this
file opens with.

**One bookkeeping consequence:** the header above counts tags, not weights, so this tag makes
**seven** where the 2026-08-17 header still says six. The weights are unchanged; the resident runner
is not.

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
| **The coder split (2026-08-17)** | `ollama rm qwen38-coder:q4 qwen3.8:27b-q4_K_M` (frees ~18 GB); set `self.coder_model = "hermes-genesis:apex-compact"` in `pipes/auto_assistant.py` and `python3 scripts/deploy_pipe.py auto_assistant`, or `--rollback` for the whole pipe. Restore `opencode.json` from `~/.config/opencode/opencode.json.bak-pre-qwen38`. Ollama itself can go back to 0.32.1 with `OLLAMA_VERSION=0.32.1 curl -fsSL https://ollama.com/install.sh \| sh`, though nothing downstream requires it once the model is removed. |

| **Ollama 0.34.1 (2026-09-17)** | Both halves are still on disk. `sudo systemctl stop ollama && sudo rm -rf /usr/local/lib/ollama && sudo mv /usr/local/lib/ollama.0.32.14 /usr/local/lib/ollama && sudo mv /usr/local/bin/ollama.0.32.14 /usr/local/bin/ollama && sudo systemctl start ollama`. Delete the two backups once 0.34.1 has settled — together they are 2.1 GB on a 92%-full disk. |

**Upgrading Ollama here: use the release tarball, not `install.sh`.** The install
script rewrites `/etc/systemd/system/ollama.service`, and this box's unit carries a
hand-maintained `Environment="PATH=…"` line the script does not reproduce. The
drop-in (`ollama.service.d/multi-model.conf` — `MAX_LOADED_MODELS`,
`FLASH_ATTENTION`, `KV_CACHE_TYPE`, `CONTEXT_LENGTH`, `KEEP_ALIVE`) is a separate
file and survives either way. So: stop the service, move the binary and
`/usr/local/lib/ollama` aside, unpack the tarball's `bin/` and `lib/` over
`/usr/local`, start. The binary and the lib tree MUST move together — 0.34.1 ships
`libggml-base.so.0.23.0` where 0.32.14 had `0.20.0`, and a mismatched pair fails at
load rather than at startup.

Done 2026-09-17, 0.32.14 → **0.34.1**, with nothing loaded at the time (idle GPU),
so no in-flight generation was interrupted. 0.34.1 selects **`cuda_v13`** on this
box (driver 13.0; the tarball ships `cuda_v12` and `cuda_v13` both) — verified in
the startup line as `library=CUDA … libdirs=ollama,cuda_v13`. All seven models
loaded and generated afterwards, `hermes-genesis:apex-compact` included. Nothing in
the 0.33/0.34 notes invalidates an existing GGUF: the changes are a faster
`/api/tags` on large libraries, deprecated `typical_p` (existing GGUF keep support),
and MLX/`ollama create` tooling — the last only affects *creating* from safetensors,
which is not how anything here was built.

The `gemma3:1b` classifier phrase this file already flags above behaves the same
after the bump — `tests/test_autoroute.py --live` still reports CODER for "my python
keeps dying on me". That run also reports a second failure, "HINT does consult the
classifier", which is a **test artifact rather than a regression**: `CLASSIFIER_CALLS`
is appended only inside `if not LIVE:` (`test_autoroute.py:86-90`), so under `--live`
that counter is always empty and the check cannot pass. Neither failure is new.

DB backups taken along the way: `webui.db.bak-genesis` (before the swap), `webui.db.bak-embedder`,
`webui.db.bak-onepipe`, `webui.db.bak-autofull`.

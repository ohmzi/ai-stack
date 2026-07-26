# Upgrade roadmap — enhanced

> ## ✅ SHIPPED 2026-07-26 (Wave 1, first pass)
>
> | Item | Result |
> |---|---|
> | **Vision → the coder**, retire `gemma4:31b` | 123.7 vs 33.4 tok/s, 18372 vs 21772 MiB. Verified end-to-end (9.2 s, `images[]` on the wire). Tag kept on disk for rollback. |
> | **Chat → the coder** | Chat + code + vision are now ONE tenant. 119.6 vs 49.9 tok/s. `dolphin` stays for `photoreal.py`/`uncensored.py`, which hold their own reference. |
> | **Default-deny media intent** | Was a live bug: **8 of 10 ordinary sentences started a GPU render.** Now needs an explicit verb, an imperative, or `/img`&nbsp;/&nbsp;`/vid`. New suite: 40 checks, 20 of them negative. |
> | **Embeddings → `bge-m3` via Ollama** | 384→**1024 dim**, 256→8192 tokens, fixing 13% silent chunk truncation. **Freed 360 MiB**: Open WebUI now holds *zero* VRAM (baseline 1029→664 MiB). |
> | **Reranker: Infinity CPU + `bge-reranker-v2-m3`** | 0 VRAM, **1.07 s for 20 docs**, correct Cohere schema. Not a nicety — see below. |
> | `top_k_reranker` 3 → 5 | Precision now earned by a cross-encoder rather than assumed. |
>
> **Why the reranker was not optional.** Reading `retrieval/utils.py:1702-1735`: with
> `rag.reranking_model` empty, `RerankCompressor` takes the RRF-fused hybrid candidates, **re-embeds
> them with the ordinary embedding model, re-sorts by plain cosine and truncates**. The BM25 half of
> hybrid search was being discarded at the final ranking step. Phase 7's "settings only, defer the
> reranker" decision was based on a wrong reading of the code and is hereby corrected.
>
> **Correction to this document:** the Infinity image is `michaelf34/infinity`, not `michaelfeil/…`
> as written below. Tag `0.0.77-cpu`, 0.75 GB.
>
> Still open from Wave 1: the confirmation gate before renders, task-model revert to `gemma3:1b`,
> and the `keep_alive:0` audit.


_Created 2026-07-26. **Supersedes the "what's left" / open-decision sections of
`CAPABILITY_UPGRADE_PLAN.md`.** That document remains the historical record of Phases 0–9 and the
decision ledger; this one is the forward-looking work list._

Every number below is measured on this box unless it carries a source URL. Where two measurement
methods disagree, `nvidia-smi` deltas win — `/api/ps size_vram` was proven to understate real VRAM by
1189 MiB (coder), 1451 MiB (`gemma4:e2b`) and 2828 MiB (`gemma4:31b`), because it excludes the mmproj
vision projector and the ~305 MiB CUDA context. Several research passes quoted `/api/ps` figures
(dolphin "17.06 GB", e2b "1.95 GB"); those are not comparable to anything else here and are ignored.

---

## 0. Model consolidation evaluation — 2026-07-26

_Question asked: can ONE model replace the coder, `dolphin-venice:24b`, and "the Hermes agent"?_

**Answer in one line: two of those three, yes — but not with the model that was proposed, and "Hermes
Agent" is not a model at all.**

### 0.1 What was actually tested

`tests/bench_models.py` (committed, re-runnable). Objective grading wherever possible: coding is
**executed** against hidden tests, refusals are pattern-matched, facts are exact-matched, and only
false-premise probes are judged — by a *third* model, so nothing grades its own family. Models load
one at a time; the box cannot hold two.

| Metric | `Qwen3.6-35B-A3B` *(incumbent coder)* | `hermes-genesis:apex-compact` *(candidate)* | `dolphin-venice:24b` |
|---|---|---|---|
| VRAM (nvidia-smi delta) | 18369 MiB | **18285 MiB** | 16581 MiB |
| gen tok/s | 130.8 | **135.3** | 52.9 |
| **Coding** — 9 tasks × 3 runs, executed | 27/27 | 27/27 | 27/27 |
| **Uncensored** — enhancer-style prompts | **3/5** ❌ | **5/5** ✅ | 5/5 ✅ |
| General exact-match | 4/5 | 4/5 | 4/5 |
| Critical thinking (judged) | 2/2 | 2/2 | 1/2 |
| Vision | PASS | PASS | **no support** (HTTP 400) |

> ⚠️ **The coding row does not discriminate.** All three scored 27/27, *including* on the harder tier
> added specifically to separate them (touching-interval merges, subtractive Roman parsing, stride
> arithmetic, in-place mutation). This shows **no detectable regression** — it does *not* show equal
> quality. Do not cite it as evidence that the candidate codes as well as the incumbent.

The one dimension that *did* separate them is refusals, which is the dimension that matters for
retiring `dolphin`: the incumbent coder refused 2 of 5 prompt-enhancer requests. That is exactly why
`dolphin` is still installed.

### 0.2 Why the tested candidate is NOT recommended

The benchmark says adopt it. The provenance says do not. Provenance wins, because the benchmark
cannot see the risk.

| Claim | Evidence |
|---|---|
| **Not a Hermes fine-tune** | The "Hermes" content is ~2k blocks grafted from two FFN expert tensors of a **Qwen3.5** LoRA — a *different model generation* — into Qwen3.6 weights. |
| **The GGUF will not name itself** | `general.basename` = `KL0.0764` (a KL-divergence number), `general.finetune` = `3Ref`, and **no `base_model` key at all**. Verified locally via `/api/show`. Legitimate derivatives declare their parent. |
| **The author says not to use it for this** | *"V5 is useful for uncensored local roleplay. For coding, 27B Genesis is a lot better."* and *"I don't train or finetune models, I repair purity of signal in them on Google Colab Free on a Tesla T4."* |
| **`ollama pull` gives you the wrong model** | No tag selects V5. `:latest` resolves to a 17,327,724,672-byte layer = **V3**, not V5 (17,392,736,384). `APEX-Compact` and `Q4_K_M` both return HTTP 400. This evaluation only tested V5 because the GGUF was downloaded directly. |

One claim *did* check out: **`APEX-Compact` is a legitimate standard quant** — Ollama's parser reports
`general.file_type=15` (Q4_K_M), and a tensor-table dump shows only ordinary k-quants. The marketing
name hides nothing.

### 0.3 The better-provenanced alternative

**`HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive`** — verified via the HF API:

- **1,927,138 downloads** (26× the tested candidate), 3,111 likes, Apache-2.0, ungated
- Abliterated **directly from official `Qwen/Qwen3.6-35B-A3B`** — no cross-generation graft
- imatrix quants, mmproj bundled, and **cleanly `ollama pull`-able** at standard tags
  (IQ4_XS / IQ4_NL / Q4_K_M / IQ3_M all resolve with model + projector layers)

It is the upstream artifact the tested candidate was built on top of, minus the dubious part.

**Recommended next step:** pull it, run the same bench, adopt whichever wins. Same benefit, sound
provenance, and one command instead of a manual GGUF download plus Modelfile.

### 0.4 "Hermes Agent" — resolved

**It is [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)** (MIT, 220,767★,
42k forks, pushed 2026-07-26). Not a model — a Python agent *runtime*: CLI, Telegram/Discord/Slack/
WhatsApp/Signal gateways, cron scheduler, subagent spawning, skill-learning, FTS5 session memory.
It also ships an MCP server, which is why several plausible readings of "hermes agent" collapse onto
this one repo. **There is no Open WebUI component called Hermes** — a code search across `open-webui`
returns only docs referencing this project.

Open WebUI documents it **first-party** (`connect-an-agent/hermes-agent.mdx`), one of three agents
they officially support.

**But adopting it conflicts with this stack's architecture.** OWUI connects to it as a plain OpenAI
connection (`http://localhost:8642/v1`), which means:

- a second model row in a picker deliberately collapsed to one, and
- a path that **bypasses `auto_assistant.auto` entirely** — no media routing, no memory, no
  `_GEN_LOCK`. Every Hermes turn would load an 18.4 GB tenant **outside the lock**, which is precisely
  the "can load a coder mid-render" failure that Phase 2 rejected shape A over.

It can run fully local (provider `custom`, aliases `ollama`/`local`/`vllm`/`llamacpp`), and
`OFFLINE_MODE=true` does not block it. Port 8642 is free.

**Verdict: not rejected, but not a drop-in.** If wanted, evaluate it standalone in a terminal first.
Do not wire it in as a second OWUI connection without deciding to reverse the single-entry collapse.

**Two integration facts already measured**, should it ever be pointed at local Ollama:

| Parameter on `/v1/chat/completions` | Tokens to answer "OK" |
|---|---|
| *(none)* | 171 — and **empty content** at `max_tokens:16` |
| `"think": false` | 144 — **silently ignored** on this endpoint |
| `"chat_template_kwargs": {"enable_thinking": false}` | 201 — **silently ignored** |
| **`"reasoning_effort": "none"`** | **2** |

The thinking model burns ~150 tokens on every trivial call through the OpenAI-compatible endpoint and
returns **empty** on tight budgets — which would break an agent runtime making many small calls.
Note this is a **different knob** from the native `/api/chat` `think: false` the pipe already uses.

### 0.5 Free capability already owned: OpenWebUI Skills

OWUI 0.10.2 ships a `skill` table, a `/api/v1/skills` router, and injection at
`utils/middleware.py:2509-2556`. The gate is `use_builtin_tools`, which is **False whenever
`function_calling == 'legacy'`** (`:2517-2521`). At `:2535`:

```python
if skill.id in mentioned_skill_ids or not use_builtin_tools:
```

→ in **legacy mode the full skill body is injected unconditionally**, where native mode only gets a
lazy manifest the model must call `view_skill` to expand. **The "handicap" of legacy FC is the better
branch here.** It lands in the *system* message, so it structurally cannot poison the router, which
reads `metadata['user_prompt']`.

Caveat: full-body injection every turn consumes context. One or two skills, not a library.

### 0.6 Current state

`hermes-genesis:apex-compact` is **installed but not wired in** — nothing in the pipe points at it.
All incumbents are untouched. Rollback is `ollama rm hermes-genesis:apex-compact` plus deleting
`/home/ohmz/models/hermes-genesis/` (18.3 GB).

---

## 1. Where the stack actually stands

The code is **ahead of the docs, not behind**. Phases 3 and 4 are fully applied in the live config
while the plan still calls them partial; the Phase 3 "host-networking trap" table is wrong on 3 of 5
rows (`mineru` and `paddleocr_vl` were already defused to the discard port `:9`); two commits
(`2db5e25`, `5fba13b`) landed after the last doc update and are recorded nowhere, one of which fixed a
live-fatal bug where OWUI's code-interpreter prompt routed **every** message to the 18 GB coder. All
three unit suites pass against the current file (15/15 router, 24/24 manifold, 39/39 autoroute) and
the smoke eval is 10/11 trajectory + 4/4 outcome with a real Krea image and a real Wan video rendered.

What is genuinely weak is not the plumbing, it is three things. **First, the model roster is one
model too big and two models too many**: the Qwen3.6-35B-A3B "coder" was verified this week to be a
working vision-language model (126.8 tok/s on a real image, `capabilities: [tools, thinking,
completion, vision]`, live 446M-param CLIP projector), which makes the 21772 MiB `gemma4:31b` vision
tenant slower, worse and redundant — and makes most of the VRAM contortions in the old plan
unnecessary. **Second, the router's front door is untested and leaky**: 7 of 20 ordinary
conversational sentences started a GPU render, and 10 of 20 triggered a blocking classifier call,
none of which any test suite covers. **Third, several subsystems are configured but have never been
exercised** — zero knowledge bases, zero documents ever ingested, zero memories ever stored, the mic
never pressed. Building more retrieval or voice infrastructure on top of that is building for a user
who does not exist yet.

| Area | Status | Corrected against the live system |
|---|---|---|
| Phase 0 — driver | ✅ done | 580.173.02, all 15 pkgs held. Plan line 94 ("prevention still outstanding") is stale — contradicted 27 lines later and by `apt-mark showhold` |
| Phase 1 — router poisoning | ✅ done, extended | 15/15 cases (A–O), not the 6/6 the plan claims. All md5s quoted in the plan are stale (live: `167e6c10…`, 113586 B; DB == disk == `pipes/live/`) |
| Phase 2 — architecture | ✅ superseded | One entry, `🪄 Assistant`. `_entry()` still resolves retired `knowledge`/`coder` leaves |
| Phase 2b — coder autorouting | ✅ done | `AUTO_ROUTE_CODER=True`, classifier `gemma3:1b`, 39/39 |
| Phase 3 — documents/OCR | ✅ **done** (doc says partial) | `content_extraction_engine="tika"`, `tika_server_url=http://localhost:9998`, Tika 3.3.0 + Tesseract 5.5.0 healthy. **Never exercised**: 0 knowledge bases, 0 documents (all 25 files are jpg/png/webm) |
| Phase 4 — web search | ✅ **done + proven in production** | SearXNG live, 5-engine roster pinned by `5fba13b`. Real `type=web_search` sources and `[1]/[3][5]` markers in `chat_message` rows |
| Phase 5 — TTS | ✅ done | Kokoro CPU on :8081, healthy |
| Phase 5 — STT | ◐ open, **but reframed** | `USE_CUDA_DOCKER=true` → whisper on GPU (407 MiB when used). Offline cache is seeded (1.7 GB, loads in 0.24 s). Mic has never been pressed once |
| Phase 6 — memory | ◐ installed, **never fired** | Adaptive Memory v4.4.1 (401 KB, **no license file**) attached to `auto`. `memory` table = **0 rows** |
| Phase 7 — retrieval | ◐ settings done, quality broken | `top_k=20`, hybrid on. **New finding: with `reranking_model=""` OWUI's `RerankCompressor` discards the RRF fusion entirely** and re-sorts by plain MiniLM cosine (`retrieval/utils.py:1702-1713`) |
| Phase 8 — coder | ✅ done, premise obsolete | Wired and reachable. The coder↔`gemma4:e2b` eviction that dominated the plan is dissolved by retiring `e2b` as task model |
| Phase 9 — QA | ◐ automated done | Full tier not re-run since `2db5e25`/`5fba13b`. 6 of 13 browser items never exercised |
| Media (VIDEO_QUALITY_ROADMAP) | ✅ most accurate doc | Tier 1 all six live; Tier 2 items 7/8/13 live; 9–12, 14 not started, exactly as its header says |
| `MODELS.md` | ❌ least accurate | Still describes a three-model stack; omits the Qwen3.6 coder entirely; quotes `gemma4:e2b` at ~1.9 GB (real: 3307 MiB) |

**The single architectural fact that reprices everything below:** the coder is a VLM. Collapsing
chat + vision + code onto it gives one 18372 MiB tenant plus `gemma3:1b` at 1313 MiB, ~20.3 GB
resident with **zero eviction churn**, and retires `gemma4:31b` (19.87 GB disk) and eventually
`dolphin-venice:24b` (14.33 GB disk).

---

## 2. What is left from the original plan

| Original item | Real status today | Verdict |
|---|---|---|
| **Decision 9 — whisper to CPU** | Unapplied and **impossible as specified** (`audio.py` reads the global `DEVICE_TYPE` from `USE_CUDA_DOCKER`; no `WHISPER_DEVICE` override) | **OBSOLETE AS WRITTEN.** Its real goal was the 360 MiB embedder. `rag.embedding_engine="ollama"` frees that 360 MiB with no container recreate and no side effect on whisper (`routers/retrieval.py:145` only builds a local SentenceTransformer when the engine is `""`). Take that instead. The whisper-device question then only matters if you start using the mic — see Wave 2 |
| **Coder ↔ task-model eviction** | Unmitigated; `task.model.default = task.model.external = gemma4:e2b` | **STILL WORTH DOING, cheaply.** Revert to `gemma3:1b`. `gemma4:e2b` occupies 3307 MiB but Ollama reserves 7.5 GiB plus a 1970 MiB floor — 9.4 GiB effective for a 3.2 GiB model, caused by the 7.16 GB MatFormer container on disk. It is the only model here that co-resides with **nothing**. `gemma3:1b` (1313 MiB) co-resides with the coder at 20715/24576 |
| **Phase 7 reranker (deferred, not cancelled)** | Nothing installed | **STILL VALID BUT GATED.** The new `RerankCompressor` finding makes it more important than the plan thought — hybrid search currently buys recall and zero ranking. But there are **zero persistent knowledge bases** and 585 embeddings across 3 transient `web-search-*` collections. Ingest documents first, then buy the 0.3 GB reranker. Wave 3 |
| **Internal tool loop / native FC (Phase 2 shape B)** | Never built | **KILL FOR NOW.** Three research passes recommend it as if it were a config change. It is not: `function_calling: "legacy"` is what currently delivers web search with working citations, model knowledge, folder files and the code-interpreter XML path, and `pipe()` never declares `__tools__` and drops `body['tools']` on the floor (`auto_assistant.py:1486-1488`). Flipping the mode is an immediate, verified regression in exchange for tools you would still have to build a loop for. See §5 |
| **Phase 3 — PaddleOCR-VL + FastAPI shim** | Skipped by decision 4, never revisited | **CANCEL.** Tika + Tesseract handled the real `.doc`/`.msg`/scanned-PDF cases. Revisit only if a real document fails |
| **Browser checklist items 1–13** | 6 never exercised (KB ingest, citations-from-KB, memory round-trip, mic, lock contention, `ollama ps` observation) | **REWRITE.** Item 1 ("confirm three entries exist") is obsolete. Items 4/5/7/8 are blocked on there being a document or a memory to test. Item 11 (lock contention) is worth scripting — Wave 2 |
| **Full eval tier re-run** | Newest full run predates `2db5e25` and `5fba13b` | **DO IT** as the Wave 1 exit gate — media cases V01–V04 are currently unverified against live code |
| **`gemma4:e2b` audio capability** | Declared, never tested | **CLOSE IT.** Ollama 0.32.1 rejects audio on `/api/chat` (must go to `/v1/chat/completions` as `input_audio`), and community reports of Gemma-4 audio describe looping and dropped text. Not a viable STT path |
| **Doc hygiene** | 5 docs carry stale claims | **FIX IN WAVE 1** — cheap, and stale docs are what caused two agents to re-derive the same wrong VRAM numbers |

---

## 3. The enhanced roadmap

Each wave is independently shippable. Do not start a wave before the previous one's verification
passes — several Wave 2 items assume the Wave 1 model roster.

### Wave 1 — ships today, low risk, no downloads

---

#### 1.1 Route vision to the coder; retire `gemma4:31b`

**What.** `auto_assistant.py:74` — `self.vision_model = "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS"`.
Add `"think": False` to the `_verify_image` call at `:723` (the coder is a thinking model; the QA
verdict must not arrive wrapped in reasoning). Same one-line change in `image_krea.py:60` for
consistency. Then `ollama rm gemma4:31b`.

**Why.** Measured: coder 126.8 tok/s on a real vision turn at 18372 MiB, versus `gemma4:31b` 33.4
tok/s at 21772 MiB — **3.8× faster image QA, 3.4 GB less resident**. Qwen's own card shows it winning
nearly every vision benchmark against Gemma4-31B, including the two that matter for image QA:
RealWorldQA 85.3 vs 72.3 and HallusionBench 69.8 vs 67.4
(<https://huggingface.co/Qwen/Qwen3.6-35B-A3B>). It also deletes the tenant that every VRAM argument
in the old plan was bending around — `gemma4:31b` needed 25.5 GiB by Ollama's own prediction at 32k,
i.e. more than the card, so it evicted **everything** unconditionally.

**Cost.** −19.87 GB disk. −3.4 GB peak VRAM. −1 model load per image turn.
**Effort.** Two lines + a QA pass. Under an hour.
**Risk.** `_VERIFY_SYS` was tuned against Gemma's response style. The strict-QA verdict format may
drift. Low but real.
**Verify.** Run three real Krea renders with `IMG_VERIFY` on and confirm the verdict parses; run one
deliberately-wrong render (ask for "two people", render one) and confirm it still fails the check.
`curl localhost:11434/api/ps` during a vision turn must show exactly one large tenant.
**Rollback.** Revert the two lines. Do **not** `ollama rm gemma4:31b` until the QA pass is green.

---

#### 1.2 Media predicates default-deny + a confirmation gate + regression tests

**What.** Three changes, shipped together:
- Delete the bare-noun fallbacks at `auto_assistant.py:354` (`\b(video|animation|footage|moving image|make it move)\b`) and `:405-409` (`\b(draw|sketch|paint|illustrate)\b`, `<noun> of`). Require an imperative generation verb within ~25 chars of a media noun — the pattern already exists at `:344` and `:396`. Add `/img` and `/vid` prefixes as the escape hatch.
- Before any media branch commits, `await __event_call__({"type": "confirmation", ...})` with the parsed prompt and estimated cost; abort to chat on decline. `functions.py:255` already injects `__event_call__` and the shipped frontend already handles `confirmation`. **Must default to proceed when `__event_call__` is `None`** or direct-API renders break.
- Add the 20 conversational negatives and the 4 `_CODE_STRONG` false positives to `tests/test_router.py` as a permanent gate, before touching the predicates.

**Why.** Measured against the real `Pipe` class: **7 of 20** ordinary sentences started a GPU render
("I watched a great video about sourdough yesterday" → Wan; "my kid loves to draw" → Krea), each
costing 1–5 GPU-minutes with `_GEN_LOCK` held. **10 of 20** hit `_CODE_HINT` on everyday English
("go", "merge", "branch", "commit", "array", "pipeline", "deploy", "java"), each a blocking
`gemma3:1b` round trip on the critical path. `_CODE_STRONG` routes straight to the 18 GB coder with no
classifier check on "fix the makefile my dad left in the shed" and "Error: my flight was cancelled".
Nothing in the 32-case eval or the 13 router tests covers any of this — every media case is an
explicit imperative and every negative avoids media vocabulary entirely.

**Cost.** 0 VRAM, 0 latency (regex change is strictly cheaper). ~50 lines total.
**Effort.** Half a day including tests.
**Risk.** Loses terse phrasings like "a cat picture please" unless the prefix is used. The
confirmation adds one click to legitimate renders — gate it to fire only when the route came from a
low-confidence predicate if that becomes annoying.
**Verify.** `python3 tests/test_router.py` green including the 24 new negatives; all 8 imperative
media cases in `tests/eval/cases.json` still route correctly; manually type each of the 7 known false
positives and confirm none renders.
**Rollback.** Single commit revert; the tests come with it.

---

#### 1.3 Fix the 162 s image-edit path with the LoRA already on disk

**What.** `Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors` (849 MB) is sitting unused in
`models/loras/` — `image_krea.py:69,439` uses it; `auto_assistant.py`'s `_build_edit_wf` deliberately
does not (see the comment at `:938-941`). Wire it in **tiered**: Lightning 4–8 steps at cfg 1 by
default, full 20–24 steps at cfg 4–6 retained behind `_edit_boost` / style conversion.

**Why.** Measured from 117 real ComfyUI jobs: Qwen-Image-Edit-2509 median **162.1 s** (n=26), against
Krea 2 T2I at 16.3 s (n=42) and a whole Wan A14B video at 186.7 s (n=48). The edit path is the worst
quality-per-minute operation on the box, and 24 steps @ cfg 6 is 48 model evaluations of a 20B model.
Dropping to 4 steps @ cfg 1 is a 12× cut in sampling work — expect ~30 s end-to-end. It also shortens
the `IMG_VERIFY` correction loop, which can re-run the edit twice more.

**Cost.** 0 download, 0 VRAM change.
**Effort.** ~20 lines + an A/B on edit quality.
**Risk.** Real: the code comment records that the 4-step LoRA was **rejected on quality grounds** —
Lightning edits blend new elements less convincingly into scene lighting and grain. This needs
re-judging on real edits, not blind enabling. The tiered design is the mitigation.
**Verify.** Run the same three edit prompts through both paths with a fixed seed; if Lightning output
is unacceptable on any, keep it behind an opt-in keyword instead of making it the default.
**Rollback.** One constant.

---

#### 1.4 Task-model cleanup

**What.** Three independent changes:
- `task.model.default` and `task.model.external` → `gemma3:1b` (its hidden `is_active=0` row still exists). Reverses commit `619a85c`.
- `ollama rm gemma4:e2b-it-qat` (4.34 GB, wired into nothing).
- Audit the seven `keep_alive: 0` sites (`auto_assistant.py:382, 612, 725, 745, 908, 932, 1261`). The ones that immediately precede a ComfyUI render must keep it. The ones that do not (style enrichment, prompt merge on the chat path) get `"keep_alive": "120s"`.

**Why.** `gemma4:e2b` is the single biggest VRAM inefficiency measured: 3307 MiB resident, but Ollama
predicts 7.5 GiB (from the 7.16 GB MatFormer container on disk, not from KV — forcing `num_ctx=8192`
only moves it to 6.9 GiB) and demands a further 1970 MiB free. That is a **9.4 GiB effective
reservation for a 3.2 GiB model**, and it is why it co-resides with nothing in any order at any
context length. `gemma3:1b` at 1313 MiB co-resides with the coder (20715/24576, re-probe load
durations 0.23 s / 0.35 s proving neither reloaded). The `keep_alive` audit removes 12–19 s of pure
reload latency per turn — dolphin/coder warm reload is 6.3 s and a turn can currently pay it three
times for the same tenant. `e2b-it-qat` measured 51 tok/s vs `e2b`'s 111 tok/s at the same VRAM.

**Cost.** −4.34 GB disk, −1994 MiB when the task model runs. 0 new cost.
**Effort.** One afternoon (the `keep_alive` audit is the slow part — seven call sites individually).
**Risk.** `MODELS.md` records that `gemma4:e2b` produces measurably better titles/tags. Accepted.
`gemma3:1b` does **not** declare the `tools` capability — if legacy tool selection is ever enabled
(`tool_server.connections` is empty today), `e2b` must come back for that one call. Keep the tag.
The `keep_alive` change must not touch pre-render helpers or a 14–18 GB tenant could still be resident
when Krea/Wan allocates; `_free_vram()` is the existing safety net and stays.
**Verify.** Open a new chat, `curl localhost:11434/api/ps` after the title generates — expect
`gemma3:1b`, not `gemma4:e2b`, and the coder still resident. Time a coder turn immediately after.
**Rollback.** Config revert (two keys) + one commit.

---

#### 1.5 Retrieval: move embeddings to Ollama + `bge-m3`, and take the free config wins

**What.**
- `rag.embedding_engine = "ollama"`, `rag.embedding_model = "bge-m3:latest"` (`rag.ollama.base_url` is already `http://127.0.0.1:11434`; the model is **already pulled**, 1.16 GB).
- `rag.embedding_batch_size` 1 → 32.
- `rag.enable_hybrid_search_enriched_texts` false → true.
- `rag.chunk_min_size_target` 0 → ~400.
- Re-index. The 3 transient `web-search-*` collections regenerate themselves.

**Why.** `all-MiniLM-L6-v2` is a 2021 22.7M-param model, 384-dim, **trained at 128 tokens and
truncating at 256**. Measured against the live Chroma store: **13.0% of the 585 stored chunks
(76/585) exceed that limit and are silently truncated**, p90 = 264 tokens, max 403 — with
`chunk_size=1000`. bge-m3 gives 8192 tokens, 1024 dims, MIT, and is the one strong candidate that
needs **no query/document prefixes**, which matters because the Ollama engine physically cannot send
them (`retrieval/utils.py:997-1010` puts the prefix in a JSON field Ollama does not read). Measured
here: 927 MiB, 85.3 chunks/s, 90 ms per query, 6.0 s cold. Switching the engine also stops OWUI
constructing a local SentenceTransformer at all, **freeing the 360 MiB** it holds permanently on the
GPU — cheaper than Decision 9's container recreate and with no whisper side effect.
`embedding_batch_size=1` is one GPU forward pass per chunk. And critically: **there are zero
persistent knowledge bases right now, so the normally-painful re-index is free. This window does not
recur.**

**Cost.** −360 MiB permanent. +927 MiB transient (unloads on `OLLAMA_KEEP_ALIVE=60s`), or 0 with
`num_gpu: 0` at 3.4 chunks/s if you prefer CPU. 0 disk.
**Effort.** Config only, ~30 min.
**Risk.** Vector dimension changes 384 → 1024, so **every collection must be rebuilt** — back up
`data/vector_db` and `webui.db` first. Do not hand-delete anything inside the vector store. bge-m3
also occupies one of the 3 `OLLAMA_MAX_LOADED_MODELS` slots while warm; with the coder resident that
means churn on the first RAG query after idle.
`enable_hybrid_search_enriched_texts=true` disables the native-hybrid fast path — irrelevant on
Chroma, which has none, but do not enable it if you ever migrate to pgvector.
**Verify.** `nvidia-smi --query-compute-apps` no longer lists a 360 MiB open-webui python process.
Do one web search and confirm citations still render. Re-run the tokenizer check: no chunk should
exceed bge-m3's window.
**Rollback.** Revert the four config keys and re-index again (still cheap).

---

#### 1.6 Make the VRAM guards honest

**What.**
- `_comfy_idle()` / `_comfy_free()` (`auto_assistant.py:668-675, 677-684, 697-700`) currently `return True` on exception — a hung ComfyUI is indistinguishable from an idle one. Make them fail **closed**, paired with a short retry rather than an immediate hard refusal.
- `_free_vram()` returns a bool its own docstring says exists "so callers can surface GPU busy rather than blindly OOM". **Not one of its 10 call sites** (`:871, 1002, 1022, 1037, 1058, 1308, 1333, 1406, 1418, 1594`) checks it. Honour it.
- `photoreal.py:177-178` issues an unconditional global ComfyUI `/interrupt` on its own ~15-minute timeout, which by its own comment most likely kills an in-flight `auto_assistant` Wan render. Guard it: only interrupt if the tracked `prompt_id` is the one in `queue_running`.
- Pin the two silent load-bearing externals: set `UVICORN_WORKERS=1` explicitly in the open-webui container env (today it relies on `start.sh`'s default; >1 silently gives every worker its own `_GEN_LOCK`), and document ComfyUI's `--disable-smart-memory` as VRAM-correctness-critical, not a tuning flag.
- Same container recreate: `WEBUI_SECRET_KEY` set explicitly, `WHISPER_VAD_FILTER=true`, `WHISPER_LANGUAGE=en`.

**Why.** A safety mechanism whose return value everyone ignores is worse than no safety mechanism,
because it reads as covered. The photoreal `/interrupt` is a **live bug**, not a hypothetical.
`WHISPER_VAD_FILTER` defaults to `False`, so whisper is currently free to hallucinate text over
silence — the classic "Thank you for watching" failure — the moment the mic is ever used.

**Cost.** 0 VRAM, 0 disk. One container recreate (bundle all env changes into it).
**Effort.** Half a day.
**Risk.** Fail-closed on a flaky `/system_stats` will refuse work that would have succeeded — hence
the retry. `WHISPER_LANGUAGE=en` mistranscribes non-English; skip that half if you dictate in more
than one language.
**Verify.** `docker stop comfyui`, then ask for an image — expect a clean "GPU busy / ComfyUI is
down" instead of a 19 GB model load. `docker inspect open-webui` shows all four env vars.
**Rollback.** Revert commit; container recreate restores prior env.

---

#### 1.7 Free config wins (one sitting, ~30 minutes total)

| Change | Why |
|---|---|
| `evaluation.arena.enable = false` | "Arena Model" is live in the picker right now and dispatches to a **uniformly random** pick among all 6 non-arena entries — including the raw 18.63 GB Qwen tag and the media-rendering pipes |
| Insert a model row for the raw Qwen coder tag with `is_active=0` | It is the only large tag still directly selectable. The router reaches it automatically |
| `user.permissions.chat.multiple_models = false` | Parallel dispatch to two entries bypasses `_GEN_LOCK` entirely; dolphin + coder does not fit in 24576 MiB |
| `is_active=0` on `image_krea` and `animate_scail` | Both are live, both render to the same GPU with no lock, neither has a model row (so no filters, no legacy injection). Keeping `photoreal` (the `uncensored` entry) — it is actually used |
| Settings → Audio → **Allow Voice Interruption in Call** | Already shipped and defaults to `false`; with it off the mic analyser is deliberately deafened while the assistant speaks. One checkbox = barge-in |
| Create a Note with your standing context, attach it to 🪄 Assistant as knowledge | Notes are injected **full-content, unchunked** (`retrieval/utils.py:1361-1379`) through the legacy `:2377` gate. A persistent, UI-editable context doc you can actually read — unlike Adaptive Memory's inference. Keep it under ~1–2k tokens |
| Create a Folder per project (system prompt + file set) | Folder system prompts apply unconditionally, folder files RAG-inject on the legacy path. 0 folders exist |
| Press the code-interpreter toggle | Already enabled on pyodide and **not blocked by legacy FC** — legacy uses the `<code_interpreter>` XML path (`middleware.py:2466-2484` → `:3910` → `:5043`). The `_CODE_STRONG` hazard from its injected prompt is already stripped by `_INJECTED_MARKERS`; do not remove that marker |
| Use the thumbs; open the admin analytics endpoints | `feedback` table = 0 rows, so the leaderboard has nothing to rank over 58 chats / 254 messages |

**Verify.** Model picker shows exactly the entries you expect. `docker exec` the DB and confirm
`function` rows for `image_krea`/`animate_scail` are `is_active=0`.
**Rollback.** All single config values; note the previous value before changing each.

---

#### 1.8 Doc hygiene + the exit gate

**What.** Fix `CAPABILITY_UPGRADE_PLAN.md` line 94 (stale), its Phase 3 trap table (3 of 5 rows
wrong), all quoted `function.content` md5s, Phase 6's "scoped to knowledge/coder" (now `auto`), and
Phase 6's "native memory remains dead on auto" (reversed by `AUTO_KEEP_SYSTEM=True`). Rewrite
`MODELS.md` for the post-Wave-1 roster. Tick the five unchecked boxes in
`openwebui-improvement-plan.md` Phase 5 (the work is done; only the checkboxes are wrong). Delete
`pipes/live/video.py` (6971 B, no DB counterpart). Add a `known_issue` marker to RE02 in
`tests/eval/cases.json` so a reader of a results file does not see an unexplained red.

**Exit gate for Wave 1:** back up `webui.db`, then run `python3 tests/eval/run_eval.py --tier full`
and compare against `tests/eval/baseline.json` (31/32 trajectory, 23/24 outcome; known fails R05,
RE02, flaky CO01). This tier has not run since two commits landed, so media cases V01–V04 are
currently unverified against live code.

---

### Wave 2 — needs a decision or a download

---

#### 2.1 Retire `dolphin-venice:24b`: chat → the coder, uncensored work → a small Heretic helper

**What.** `auto_assistant.py:73` → the coder tag. Then add a **separate** small attribute used only by
the prompt-rewrite helpers that must not refuse (`:373, 603, 744, 907, 931, 1260`) and by
`photoreal.py:24` — e.g. a Heretic-decensored Gemma-4-E4B GGUF at ~5 GB. Then `ollama rm
dolphin-venice:24b`.

**Why.** Measured at similar residency: coder **119.6–124.9 tok/s at 18372 MiB** versus dolphin
**49.9 tok/s at 16584 MiB** — 2.4× on every chat turn *and* every prompt-enhance/shot-plan helper.
dolphin's HF card dates to 2025-06-12 (a Mistral Small 3.1 derivative) and there is no newer Dolphin
in that class. Going *smaller* instead buys nothing structural: chat+coder co-residency needs a
co-tenant under ~3.3 GiB (a 4B-class Q4), so even a 12B (~8.1 GiB projected) leaves the card unable to
hold both. And the alternative of an abliterated 35B for the chat slot is worse: the only available
builds are Q3_K (17.17 GB), a materially worse KLD tier than the current UD-IQ4_XS, and abliteration
measurably degrades coding/agentic quality (<https://github.com/p-e-w/heretic> — Heretic is now the
SOTA method, 0.16 KL divergence vs 0.45/1.04 for hand-made abliterations at equal refusal
suppression). Scoping decensoring to a 5 GB helper on the render path keeps damaged weights nowhere
near the coder.

**Cost.** −14.33 GB disk (dolphin), +~5 GB (helper). Chat VRAM unchanged (reuses the already-loaded
coder). Helper is transient — the render path calls `_free_vram()` immediately after anyway.
**Effort.** One attribute + repointing 6 call sites + `photoreal.py`. A day with A/B.
**Risk.** Two real ones. (a) **General chat becomes censored** relative to dolphin — see Open
Question 1. (b) A 4B writes weaker prompt expansions than a 24B, and Photoreal output quality is
exactly what you would be trading. A/B on a set of known prompts before deleting dolphin.
**Verify.** Run the Photoreal enhancer on 5 known-difficult prompts and diff the expansions; run the
smoke eval; time a chat turn (expect ~2.4× the tok/s).
**Rollback.** Two attributes; keep the dolphin tag until the A/B is settled.

---

#### 2.2 Per-request `num_ctx`, not a global context cut

**What.** `auto_assistant.py:1486-1488` currently posts `{"model", "messages", "stream", "think"}`
with **no options at all**, so every chat turn allocates the full 32768-token KV from
`OLLAMA_CONTEXT_LENGTH`. Compute `num_ctx` per turn from actual message length, rounded up to
16384/32768, and pass it in `options`. **Do not** set `OLLAMA_CONTEXT_LENGTH=8192` globally.

**Why.** Measured: 32k vs 8k costs 327 MiB on the coder, 100 MiB on `gemma3:1b` — and **zero**
throughput at every context length (coder 123.4–124.9 tok/s flat across 4k/8k/16k/32k). Note the
honest revision: with dolphin retired this lever collapses from 1837 MiB to 327 MiB of value, because
1837 of it was dolphin's 85.0 KiB/token full-attention KV. The coder only keeps KV on 10 of 40 layers
(10.6 KiB/token). **A global 8192 is actively dangerous**: `--context-shift` is on, so oversized RAG
and web-search turns would be silently truncated rather than erroring.

**Cost.** ~327 MiB recovered on short turns. ~10 lines.
**Effort.** Two hours.
**Risk.** Under-sizing a turn truncates it silently. Floor at 16384 and only ever round up.
**Verify.** `journalctl -u ollama` shows `llama_kv_cache: size = …` varying per request.
**Rollback.** Delete the options dict.

---

#### 2.3 One VRAM arbiter, and admission control for chat

**What.** Replace the module-level `threading.Lock` with an `fcntl.flock` on a fixed path (~15 lines,
in `auto_assistant.py` only — do **not** duplicate 40 lines into four files that will drift; two of
the siblings are being deactivated in 1.7 and `photoreal` is the only remaining one worth a second
copy). Add a timeout and a queue-position status event. Then add **admission control** — not full
serialization — to the unlocked chat return at `:1811`: if `_vram_free_gib()` is below the model's
footprint, emit "GPU busy rendering, queued" and wait, rather than issuing the load.

**Why.** `_GEN_LOCK` is process-local, module-local, unfair, untimed, and **silently replaced whenever
the function is redeployed** — OWUI re-reads `function.content` from SQLite on every request and
re-`exec`s the module on any diff (`plugin.py:371-392`), producing a new lock object and a new `Pipe()`
instance mid-render. A path-based lock survives that and releases on process death. Plain chat and
vision take **no lock at all** today, and with the Wave 1 changes the chat tenant is the same 18372
MiB coder — the largest unarbitrated load in the system.
Full serialization is explicitly rejected: it would make the box feel single-task.

**Cost.** 0 VRAM. Slight added latency per acquire.
**Effort.** Half a day.
**Risk.** A crashed holder must not wedge the lock — `flock` releases on process death, which is
exactly why it beats a socket mutex.
**Verify.** Script the contention test `QA_TEST_PLAN.md:278` leaves manual: two concurrent `pipe()`
calls, one video + one chat, asserting no OOM and no interleaved ComfyUI submission. Gate it behind a
flag — it costs ~5 GPU-minutes and can OOM the box by design.
**Rollback.** Revert commit; the module-level lock returns.

---

#### 2.4 Structured output on the pipe's own helper calls

**What.** Pass Ollama's `format` JSON schema on internal helper calls — the code classifier becomes
`{"type":"object","properties":{"route":{"enum":["CODE","CHAT"]}},"required":["route"]}`.

**Why.** Measured: on the coder, adding a schema dropped legacy tool-selection latency from **2.25 s
to 0.55 s** at identical accuracy (16/16), because the grammar suppresses preamble tokens. It also
deletes the "classifier returned junk → degrade to chat" branch. Note the honest negative from the
same measurement: schemas did **not** improve tool-selection accuracy at all (21/24 → 21/24) —
across 72 runs on three models there were **zero** malformed-JSON failures. Grammars fix syntax, and
syntax was never the problem here.

**Cost.** 0 VRAM. A few lines per site.
**Effort.** Two hours.
**Risk.** Over-constraining degrades generative helpers by forcing early closure. Apply to
classification/extraction only; leave the enhancer and shot planner alone.
**Verify.** `tests/test_autoroute.py` still 39/39; time the classifier before and after.
**Rollback.** Remove the `format` key.

---

#### 2.5 Media: refresh the Wan distill LoRAs, wire a Krea aesthetic LoRA, retest `torch.compile`

**What.**
- Replace the 250928 T2V Lightning pair (3.7 GB) with `lightx2v/Wan2.2-Distill-Loras` → `wan2.2_t2v_A14b_{high,low}_noise_lora_rank64_lightx2v_4step_1217.safetensors` (0.61 GB each, three months newer, 6× smaller). Re-tune `V_HIGH_LORA` (0.8) and the cfg-3 high-stage recipe afterwards — those values were fitted to the old pair.
- Set `IMG_T2I_LORA` / `IMG_T2I_TRIGGER` / `IMG_T2I_LORA_STRENGTH` (`auto_assistant.py:34-35`, currently empty) to a Krea 2 aesthetic or realism LoRA. Plumbing already exists and has never been used. Verify the exact HF repo id before pulling.
- Flip `V_COMPILE` back on for a controlled test with `TORCHINDUCTOR_CACHE_DIR` on a volume. Both recorded blockers have changed: ComfyUI-GGUF now logs `Allowing full torch compile`, and 0.28.0 ships `TorchCompileModelWanVideoV2`.

**Why.** The LoRA swap is newer, smaller and free. The aesthetic LoRA is pure quality at identical
render time through plumbing you already built. `torch.compile` was estimated at +15–30% on the Wan
experts and was the missing third of the "166 s → ~80–100 s" stack.

**Cost.** ~1.2 GB net LoRA download, ~0.5 GB for the aesthetic LoRA, 0 for compile.
**Effort.** An afternoon each.
**Risk.** `torch.compile` recompiles on every resolution change, which is bad for a pipe that switches
between 480p/720p/121-frame on keywords — compile only the default path. `V_COMPILE` is already the
kill switch. Aesthetic LoRAs bias *every* image; do a strength sweep.
**Verify.** Fixed-seed A/B against the current 186.7 s median, three renders each.
**Rollback.** Constants.

---

#### 2.6 Automations — daily only, and only after the confirmation gate exists

**What.** Workspace → Automations. One or two daily/hourly RRULE prompts against 🪄 Assistant.

**Why.** The scheduler is already running in the OWUI event loop (`main.py:353`) with **zero**
automations defined — the largest paid-for-and-unused feature. `execute_automation` calls
`app.state.CHAT_COMPLETION_HANDLER`, i.e. the full pipeline: your router, memory filter and legacy
RAG all work unchanged.

**Cost.** 0 new VRAM; one model load per run.
**Effort.** Minutes (UI).
**Risk.** Every run is **unarbitrated GPU traffic**. An automation firing mid-render contends for the
lock; one that routes to the coder stalls behind it. Daily or hourly, never minutely. Runs also
accumulate chat rows.
**Verify.** Run Now, then open the created chat.
**Rollback.** Delete or toggle off.

---

#### 2.7 STT — **only if you start using the mic**

**What.** Two options, both deferred until the mic is actually pressed (browser checklist item 10 has
never been done):
- **Preferred:** NVIDIA Parakeet TDT 0.6B v3 int8 on CPU via `ghcr.io/achetronic/parakeet` on port 5002 or 8082 (3000/8000/8080 are all occupied here), then `audio.stt.engine=openai` pointed at it — the same pattern Kokoro already uses. 6.34% avg WER vs large-v3-turbo's 7.75%, ~2 GB system RAM, **0 VRAM**, and it makes `DEVICE_TYPE` irrelevant to STT entirely (<https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3>, <https://github.com/achetronic/parakeet>).
- **Fallback, 30 seconds:** `audio.stt.whisper_model` → `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, which is **already cached** in the container (1.6 GB). Costs +1399 MiB VRAM.

**Why.** Current model is `base`. Parakeet is more accurate *and* frees the 407 MiB whisper takes when
used. But building either before the mic has ever been pressed is building for a hypothetical user.

**Risk.** Parakeet covers 25 European languages only — no Arabic, Hindi, Chinese, Japanese. Its
encoder rejects audio over 400 s unless `-long-audio` is enabled.
**Verify.** Dictate three sentences including a proper noun and a number.
**Rollback.** One config key.

---

### Wave 3 — ambitious, experimental, or gated on a precondition

---

#### 3.1 A reranker — **after there are documents to rerank**

**What.** Pre-seed `Alibaba-NLP/gte-reranker-modernbert-base` into
`/app/backend/data/cache/embedding/models`, set `rag.reranking_model` accordingly and
`SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS='{"torch_dtype":"float16"}'`, then raise `top_k` to
30–40 and `top_k_reranker` to 5–8.

**Why.** This is a *bug fix*, not a quality add-on. With `reranking_model=""`, OWUI's
`RerankCompressor` (`retrieval/utils.py:1702-1713`) does **not** pass the hybrid/RRF fusion through —
it re-embeds every fused candidate with the same embedding model, re-sorts by plain cosine, and cuts
to `top_k_reranker=3`. So `enable_hybrid_search=true` currently buys candidate recall and **zero
ranking**. An independent 2026 benchmark puts gte-modernbert at 83.00% Hit@1 vs a 62.67% no-reranker
baseline at 149M params and 157 ms (<https://aimultiple.com/rerankers> — secondary source, treat the
exact numbers as indicative).

**Precondition.** Ingest real documents into a real knowledge base first. Today there are zero, and
585 embeddings across three transient `web-search-*` collections. A 0.3 GB permanently-resident model
to rerank five SearXNG hits is not a trade.
**Cost.** ~0.30 GB fp16 resident, +150–250 ms per RAG query, 0.6 GB disk.
**Risk.** `OFFLINE_MODE=true` forces `local_files_only=True`, so an un-seeded model fails **silently**
with only a log line — the same failure class as the Phase 5 whisper cache.
**Verify.** Same query before/after; the top-3 should visibly change.
**Rollback.** Blank the config key.

---

#### 3.2 The ComfyUI acceleration chain: cu130 → SageAttention 2.2 → int8_convrot

**What.** One coupled project, in this order: (a) rebuild the ComfyUI image on a cu130 torch triple;
(b) build SageAttention 2.2 for sm_86 in the same image; (c) swap Krea 2's
`krea2_turbo_fp8_scaled.safetensors` for the official `Comfy-Org/Krea-2` →
`krea2_turbo_int8_convrot.safetensors` (13.49 GB).

**Why.** The Ampere-specific finding is real: comfy-kitchen's `TensorWiseINT8Layout` declares
`MIN_SM_VERSION = (7, 5)` while `TensorCoreFP8Layout` needs SM ≥ 8.9 (Ada), so the fp8 file this box
runs today gets **no tensor-core benefit at all** on a 3090
(<https://github.com/Comfy-Org/comfy-kitchen>). And there is a live blocker: ComfyUI logs `You need
pytorch with cu130 or higher to use optimized CUDA operations` with both the `cuda` and `triton`
comfy-kitchen backends `disabled: True` — only `eager` runs. SageAttention 2.2 is the unclaimed half
of VIDEO_QUALITY_ROADMAP item 10 (its "8 min → 5 min at 720p" figure is a 2.2 number; this box is on
1.0.6) and upstream explicitly lists RTX 3090 among the GPUs with measured 2.x speedups
(<https://github.com/thu-ml/SageAttention>).

**Cost.** ~3 GB of wheels, 13.5 GB for the int8 file, one long compile.
**Effort.** A weekend.
**Risk.** **High and coupled.** cu130 can break sageattention, ComfyUI-GGUF and RIFE at once, and the
torchaudio undefined-symbol crash from mismatched versions is already documented in the roadmap. The
int8 speedup on this hardware is **unbenchmarked** — nobody measured it, because the measuring agent
was read-only. Build a new image tag (`comfyui-local:cu130`), never overwrite `:tier2`. Do this only
if you are taking the whole chain; the payoff is on a 16 s image and a 187 s video, while the 162 s
edit path is already fixed for free in Wave 1.
**Verify.** Startup log no longer shows `disabled: True` on the cuda backend; fixed-seed A/B on three
renders per model.
**Rollback.** `docker compose` back to `comfyui-local:tier2`.

---

#### 3.3 Re-evaluate `--lowvram` (benchmark only, do not ship blind)

**What.** Time one Wan and one Krea render with the current `--disable-smart-memory --lowvram --fast
fp16_accumulation` versus dropping `--lowvram`.

**Why.** comfy-aimdo 0.4.10 DynamicVRAM is detected and enabled with async weight offloading on 2
streams and 86.6 GB pinned; in current `model_management.py`, `aimdo_enabled` routes to the same
branch as `NORMAL_VRAM`, so `LOW_VRAM` is largely vestigial while still forcing conservative
per-block offload.

**Risk.** Directly conflicts with item 1.6's "pin these flags". Removing `--lowvram` on a card that
must also host an 18 GB tenant could OOM mid-render. **Gate on the contention test from 2.3.**
**Rollback.** Compose command revert.

---

#### 3.4 Watch list — zero work now, revisit on a trigger

| Item | Trigger to revisit |
|---|---|
| 1-bit / ternary **Bonsai-27B** (3.80 / 7.17 GB) | llama.cpp PR #25707 (CUDA Q2_0) merges **and** Ollama picks up the type. Ollama's Bonsai PR #15213 was closed unmerged |
| **MTP** GGUFs (+0.48 GB, claimed 1.5–2× decode) | Ollama gains CUDA MTP. Today it is MLX/Apple-Silicon only, and there is an open bug that Qwen3.6-35B-A3B MTP GGUFs fail to load. The "90% faster" figure was Apple Silicon |
| **EXL3 / exllamav3** | Only if the coder moves off Ollama for another reason. Measured +8% (39.1 vs 36.2 tok/s) on a 3090, against losing keep-alive auto-unload on a card shared with ComfyUI |
| **Mage-Flow** (4B, MIT, 4-step, 4.16 GB int8) | A tagged ComfyUI release contains it. Merged to master 2026-07-25 only; running master on a live assistant is not the trade |
| **mcpo / MCP tool servers** | Only after the confirmation gate ships, and read-only tools only. Note the correction: legacy FC does **not** block MCP/OpenAPI servers — they merge into `tools_dict` at `middleware.py:2611-2660` regardless of mode. Time it around the 2026-07-28 MCP spec rewrite by pinning server versions |

---

## 4. The VRAM budget after Wave 1 + 2.1

Card total 24576 MiB. Ollama sees 23.5 GiB (24115 MiB) and applies a per-model free-memory floor
(coder 1909 MiB, `gemma3:1b` 1024 MiB) to the **incoming** model only.

Baseline changes from 1023–1070 MiB to **~630 MiB**: desktop 186 (Xorg 104–126 + gnome-shell 26 +
TeamViewer 18 + nautilus 15) + ComfyUI idle 444. The 360 MiB OWUI embedder is gone (item 1.5). Note
the correction to the old docs: "880–1050 MiB desktop overhead" was wrong — the desktop is 186 MiB.

| Mode | Ollama resident | ComfyUI | Desktop | Total | Free of 24576 |
|---|---|---|---|---|---|
| **Idle** (keep-alive expired) | — | 444 | 186 | **630** | 23946 |
| **Chat / vision** (one tenant) | coder 18372 + `gemma3:1b` 1313 | 444 | 186 | **20315** | **4261** |
| **Coding** (identical — same tenant) | coder 18372 + `gemma3:1b` 1313 | 444 | 186 | **20315** | **4261** |
| **Chat + a RAG query** | + bge-m3 927 (transient) | 444 | 186 | **21242** | 3334 |
| **Rendering** (`_free_vram()` has run) | 0 | ComfyUI active | 186 | ComfyUI's own peak + 186 | ~24390 available to ComfyUI |
| **Render prompt-enhance** (before `_free_vram`) | Heretic helper ~5000 | 444 | 186 | **~5630** | 18946 |
| **Voice** (if Parakeet, Wave 2.7) | as chat | 444 | 186 | **20315** | 4261 (STT on CPU) |
| **Voice** (if cached whisper-turbo instead) | as chat + 1399 | 444 | 186 | **21714** | 2862 |

**Why this fits, in Ollama's terms, not just physically.** Load order coder → `gemma3:1b` was measured
resident at 20715/24576 with a 1032 MiB baseline; with the baseline at 630 it is ~20315. The 1b's
1024 MiB floor is satisfied by 4261 MiB free. The reverse order evicts the 1b (`predicted=17.8 GiB
available=19.3 GiB`) — accepted, because a 1b reload is 0.35 s warm. There is no longer any
combination in normal operation that requires three tenants, so `OLLAMA_MAX_LOADED_MODELS=3` becomes
irrelevant rather than fictional.

**What is deliberately *not* in this table:** dolphin (16584 MiB), `gemma4:31b` (21772 MiB) and
`gemma4:e2b` (3307 MiB real / **9.4 GiB effective reservation**) are all retired. Those three are what
made every prior co-residency calculation hard.

---

## 5. Explicitly NOT doing

Recorded so these do not get re-litigated. Each was proposed by at least one research pass.

**Architecture / serving**

- **Turning off `function_calling: "legacy"` or building a native tool loop.** Three passes recommend it; all three describe a multi-week project as a config change. Legacy is what currently delivers web search with working citations, model knowledge, folder files and the code-interpreter XML path — verified in production `chat_message` rows. The pipe never declares `__tools__` and drops `body['tools']` on the floor. Flipping the mode is a guaranteed immediate regression in exchange for tools that require the loop first. The builtin surface it unlocks (26 tools, 37 with all toggles) is real, but the price is a working system.
- **Migrating off Ollama to a llama-server router for `--n-cpu-moe`.** It trades the one component that reliably arbitrates VRAM against ComfyUI (auto-unload on keep-alive) for the ability to run a model that does not fit, at RAM bandwidth, on a box whose actual complaint is latency. vLLM buys nothing at 1 concurrent request. EXL3 kernels are still explicitly not efficient on Ampere (<https://github.com/turboderp-org/exllamav3>).
- **Any second front end** — Pipecat, local-deep-research, `open-webui/computer`, `open-terminal`, Kyutai. Each bypasses the pipe entirely: no media routing, no memory, no RAG, no `_GEN_LOCK`. And you cannot proxy back through OWUI because `auth.enable_api_keys=false`. You would be building a second, worse assistant. `open-webui/computer`'s licence is also "All rights reserved", not OSI, and its gateway runs execute with full tool approval.
- **Multi-agent orchestration (CrewAI/AutoGen/LangGraph).** One large tenant fits; every handoff between differently-sized agents is a 6.3 s warm / 38 s cold model swap. A 5-agent crew doing 3 rounds is up to 15 swaps for a task a single loop finished in 11 s in the probe.
- **`open-terminal` / shell MCP servers.** Arbitrary code execution reachable from chat, and its main payoff (automation working directories) is itself gated behind native FC.
- **Channels / inbound webhooks.** An unauthenticated-ish trigger that can invoke an 18 GB model on a single-GPU box, for a use case nobody asked for.

**Retrieval**

- **Building retrieval infrastructure before there are documents.** Contextual-retrieval preprocessing (an out-of-band script you must re-run on every edit, for a 0.303 → 0.317 nDCG@5 delta in the independent RTX 4090 study — arXiv 2504.19754 — versus Anthropic's much larger headline at <https://www.anthropic.com/engineering/contextual-retrieval>), GraphRAG (~10,000× indexing premium, worthless for single-fact lookup), pgvector migration (a full DB migration for 8.5 MB of vectors), Qdrant migration (does not even implement native hybrid search in OWUI — only pgvector does). Ingest a document, measure, then buy.
- **Late chunking.** Not a free win: it *lost* on MSMarco short passages, 0.630 → 0.503 nDCG@5. OWUI cannot do it anyway.
- **Chroma storage cleanup.** 468 MB (263 MB of never-VACUUMed sqlite + ~214 MB of orphaned HNSW dirs) on a disk with 320 GB free. The correct action is explicitly no action; hand-deleting UUID directories destroys collections irrecoverably.
- **`rag.full_context = true`.** Global, so it would stuff every web-search result too. The underlying advice — keep single documents under ~20k tokens, where chunking is a no-op — needs no config change.
- **An embedding-kNN intent classifier on `app.state.ef`.** Directly contradicts moving embeddings to the Ollama engine (which leaves `app.state.ef` unpopulated), and once the regex is default-deny the classifier fires on a few percent of turns instead of ~50%.

**Models**

- **Ornith-1.0-35B A/B.** Self-reported benchmarks that BenchLM explicitly refuses to rank for lack of independent coverage; 17.8 GB download for a delta you will not perceive in chat.
- **A larger coder quant (IQ4_NL / IQ4_NL_XL).** +0.3–1.8 GB against KLD numbers *interpolated from a different model*, and it erodes exactly the headroom that keeps `gemma3:1b` co-resident.
- **`gemma4:26b` MoE as the vision model.** Superseded by routing vision to the coder, which costs zero GB and zero downloads.
- **Gemma 4 12B for STT.** The proposal itself concedes ~8 GB transient versus faster-whisper base/int8's 407 MiB — a 20× regression.
- **q4_0 KV cache.** Frees 938–1280 MiB but measurably degrades: 22/30 → 20/30 on a deterministic battery, and mean first-token logprob on planted long-context facts −0.528 → −0.818, widening to −0.545 nats at 22773 prompt tokens — exactly the regime a RAG pipe operates in. f16 is strictly worse than q8_0 (+2398 MiB on dolphin for +0.6 tok/s). **Keep q8_0.**
- **Global `OLLAMA_CONTEXT_LENGTH=8192`.** `--context-shift` is on, so oversized RAG turns degrade silently rather than erroring — and post-dolphin the whole lever is worth 327 MiB. Per-request only (2.2).
- **Chasing 3-way residency or a load-order rule.** The winning triple was `e2b → 1b → dolphin`; both `e2b` and dolphin are being retired. The load-order rule's own analysis says the impact is 0.35 s and "the true fix is to let the 1b be evicted".
- **DFlash / speculative decoding / TurboQuant.** DFlash's 3.43× is against a *dense* 27B baseline (37.78 → 129.52 tok/s) that your MoE already beats at 119–125 tok/s, and it collapses at long context (32K prompt = 106 s prefill, 35 tok/s decode). TurboQuant is not merged into llama.cpp.

**Media**

- **LTX-2.3 / Sulphur-2 / Cosmos3.** 22B DiT *plus* a Gemma-3-12B text encoder (9.5–24 GB) — two large sequential loads per clip on a card that also hosts an 18 GB LLM. Sulphur-2 is 46.14 GB despite its card saying "9B". Cosmos3 is 64B, B200-class by NVIDIA's own card. Revisit only if a sub-10B LTX-2.3 distill with a small text encoder appears, or if synced audio becomes the priority (MMAudio at ~5 GB is the cheaper route).
- **Wan 2.5 / 2.6 / 2.7 / 3.0.** Open weights **do not exist** — the Wan-AI HF org has published nothing newer than Wan-Dancer-14B (2026-07-10, dance-only). The blog posts claiming otherwise are SEO content.
- **SeedVR2 upscaling.** A second model load per video job on a box whose whole problem is eviction churn, behind a keyword nobody will type.
- **Cache accelerators (TeaCache/EasyCache/LazyCache).** Already installed but architecturally wrong here: they skip redundant denoising steps and need 20+ step schedules. You run 6.
- **`--fast fp8_matrix_mult` / `CublasOps`.** Ada+ only, and CublasOps requires fp16 weights *and* fp16 compute.

**Other**

- **Context compaction.** Lossy summarisation on the task model, landing in a message list the pipe reconstructs anyway, to solve a problem you do not have at 58 chats. (It is also currently double-dead: disabled, with an 80000-token threshold that can never fire against a 32768 context.)
- **`meta.defaultFeatureIds = ["web_search"]`.** It also force-enables web search on every interactive turn against a SearXNG instance measured suspending 4 of 6 engines after ~50 queries in 10 minutes, with HTTP 200 masking the degradation. The suggested mitigation (a second model entry) undoes the single-entry collapse.
- **Rewriting `task.tools.prompt_template`.** The measured 24/30 → 27/30 improvement is real, but `chat_completion_tools_handler` only runs when `tools_dict` is non-empty, and with `tool_server.connections=[]` and builtin tools gated off, this template governs **zero** calls today. Revisit with mcpo.
- **Wake word / Kyutai Pocket TTS / a faster TTS.** No OWUI hook exists for wake word. And no TTS swap can reduce latency, because `_tts_openai` does `await r.read()` on the whole response, transcodes to MP3 and returns a `FileResponse` — your Kokoro container already supports chunked streaming and OWUI structurally cannot consume it. `split_on=punctuation` is already the optimal setting.
- **Needle (26M tool router) / a new classifier model.** A new inference path and an unproven model for a task the default-deny regex mostly eliminates.

---

## 6. Open questions

Only genuine forks. Everything else above has a recommendation baked in.

**1. Does general chat need to be uncensored?**
Retiring dolphin (2.1) means chat runs on the official Qwen3.6 — 2.4× faster and much stronger, but it
will refuse content dolphin would not. The proposed split scopes decensoring to a ~5 GB Heretic helper
used only by the prompt-rewrite/Photoreal path, where `_free_vram()` clears the card anyway.
*Recommendation: take the split.* If general chat genuinely needs to be uncensored, the alternative is
keeping dolphin as a fourth tag and accepting the eviction churn — which costs you most of the value
of Wave 1. Decide before 2.1.

**2. Are you going to ingest documents into a knowledge base?**
This single answer gates the reranker (3.1), whether the `bge-m3` chunk sizing matters, and whether
Phase 3's Tika/OCR work ever pays off. Today: 0 knowledge bases, 0 documents ingested, all 25 files
are images or video.
*Recommendation: ingest one real document in Wave 1 (a PDF and a `.doc`) as a test, then decide.* If
the answer is no, delete the reranker item and the Phase 3 browser checklist entries permanently.

**3. Are you going to use the mic?**
Gates all of 2.7. Browser checklist item 10 has never been done in seven days of use.
*Recommendation: do nothing until you press it once.* The `WHISPER_VAD_FILTER=true` env var in 1.6 is
free insurance either way.

**4. Confirmation gate: always-on, or only for low-confidence routes?**
Always-on bounds every routing miss to one click, but adds a click to every legitimate render.
Low-confidence-only is better UX but requires a confidence signal the regex does not currently produce.
*Recommendation: ship always-on in Wave 1 (it is 20 lines and the false-positive rate is 35%), then
downgrade to low-confidence-only once the default-deny predicates have been live for a week and you
can measure the new rate.*

**5. Is the ComfyUI acceleration chain (3.2) worth a weekend?**
cu130 + SageAttention 2.2 + int8_convrot is three coupled container rebuilds, any of which can break
GGUF, RIFE or torchaudio, for an **unbenchmarked** gain on a 16 s image and a 187 s video.
*Recommendation: no, not until the Wave 1 edit-path fix has landed and you have lived with the new
render times for a while.* The 162 s → ~30 s edit fix is free and lands first; if video turnaround is
still the thing that annoys you after that, the chain becomes worth it.

---

### Research quality notes

Where the underlying research was thin or contradictory, said plainly:

- **The int8_convrot speedup on this GPU is unmeasured.** The architectural argument (SM 7.5 vs SM 8.9 layout floors) is verified from comfy-kitchen source; the actual tok/s or s/render gain is not. Nobody benchmarked it because the measuring pass was read-only.
- **Ornith-1.0-35B's benchmarks are entirely self-reported** by DeepReinforce, and BenchLM explicitly excludes it from its leaderboard for lack of independent coverage.
- **The reranker Hit@1 figures** (62.67% → 83.00%) come from a single secondary benchmark site, not a peer-reviewed source.
- **Kyutai semantic-VAD VRAM for a single stream** could not be verified — only the "64 concurrent streams on an L40S" figure is published.
- **"Ollama is 1.8× slower than llama.cpp"** is a single opinion piece and could not be corroborated; this box measured 102.9–124.9 tok/s on the coder under Ollama.
- **Contextual retrieval's effect size is contested**: Anthropic reports 49–67% failure reduction; the independent ECIR-2025 study on comparable hardware reports 0.303 → 0.317 nDCG@5. Both are cited above; the gap is not resolved.
- **Three research passes independently recommended flipping off legacy function calling.** All three were wrong about the cost. Treat any future recommendation to do so as requiring the tool loop to exist and be tested first.

# Open WebUI capability upgrade — plan

_Created 2026-07-25. Target: make the stack a genuinely versatile offline assistant (documents, OCR,
search, speech, memory, tools, coding) without breaking the VRAM-budget-first design._

**Progress: 4 / 24 complete**

| Phase | Scope | Status |
|---|---|---|
| 0 | Host unblock (NVIDIA driver) | ✅ resolved + **pinned** 2026-07-25 |
| 1 | RAG router poisoning fix | ✅ live 2026-07-25 12:51 |
| 2 | Architecture: manifold entries | ✅ live 2026-07-25 13:12 |
| 3 | Documents & OCR (Tika + PaddleOCR-VL) | ☐ *awaiting OCR-ambition decision* |
| 4 | Web search (SearXNG) | ◐ legacy FC set; SearXNG container outstanding |
| 5 | Speech (faster-whisper, Kokoro) | ☐ |
| 6 | Memory (Adaptive Memory v4.5.0) | ☐ |
| 7 | Retrieval quality (reranker, hybrid, top_k) | ☐ |
| 8 | Coding model (Qwen3.6-35B-A3B) | ☐ |
| 9 | Full QA sweep | ☐ |

> **Verification status.** Findings below come from a 6-agent research sweep against primary sources
> (running container source, GitHub, HuggingFace/Ollama APIs). The 2-agent adversarial verification
> pass **did not complete** before the host reboot. In the two previous rounds that pass caught real
> errors, including one fabricated benchmark. **Items marked ⚠️ are single-sourced — re-verify before
> acting on them.**
>
> **Post-reboot verification pass (2026-07-25, against the live system).** Six ⚠️ items were checked
> directly. Results below and inline in each phase:
>
> | Claim | Verdict |
> |---|---|
> | Hybrid search may be pgvector-only | ❌ **WRONG** — works on ChromaDB via legacy BM25 fallback |
> | Whisper cache absent → every mic press 500s | ❌ **STALE** — cache is seeded (1.7 GB, 2 models) |
> | `gemma4:e2b` costs ~1.9 GB VRAM | ⚠️ **UNDERSTATED** — 1.95 GB weights, **~3.3 GB** real delta |
> | `tika`/`docling` defaults unresolvable under host net | ✅ confirmed in live config |
> | `mineru_api_url` collides with tunarr on :8000 | ✅ confirmed — :8000 occupied |
> | `gemma4:e2b` declares `audio` capability | ✅ confirmed via `/api/show` (also `tools`, `vision`) |
>
> **New finding:** `rag.paddleocr_vl_base_url` defaults to `http://localhost:8080`, and **:8080 is
> already occupied** on this host (a `python3` listener). Same class of trap as the MinerU/tunarr
> collision. See Phase 3.

---

## Phase 0 — HOST BLOCKER: NVIDIA driver mismatch ✅ RESOLVED

**Resolved 2026-07-25 08:50 by reboot.** Module and userspace now agree at `580.173.02`;
`nvidia-smi` returns normally (CUDA 13.0, 939 MiB idle desktop load on the 3090). All 30 containers
came back including `open-webui` (healthy) and `tday_ollama` (healthy). Nothing needed recreating.

The prevention step below is **still outstanding** — see Question 5.

<details><summary>Original diagnosis (kept for the record)</summary>

**Problem.** Unattended upgrade at `2026-07-25 07:00:58` replaced the driver userspace with
`580.173.02` while the loaded kernel module stayed at `580.159.03`.

```
/proc/driver/nvidia/version : 580.159.03   (loaded module)
NVML library version        : 580.173      (userspace)
nvidia-smi                  : Failed to initialize NVML: Driver/library version mismatch
```

**Impact.** No GPU container can *start*. `open-webui` exited 12:15 UTC and its `unless-stopped`
policy cannot restart it (`prestart hook #0 ... nvidia-container-cli: initialization error`).
ComfyUI and immich_machine_learning still run only because they hold open FDs to the old libs —
they will fail the moment anything restarts them.

**Fix.** Reboot. Module reload is **not** an option: `/dev/nvidia*` is held by Xorg, gnome-shell,
mutter, firefox, chrome, TeamViewer, Plex, coolercontrold and nvtop as well as ComfyUI/Immich.

**Nothing else in this plan can be applied or tested until this is done.** The container is only
`Exited`, not removed, so it returns byte-identically on boot. Full config snapshot saved at
`open-webui-container-config.json` in the session scratchpad if recreation is ever needed.

</details>

**Prevention — ✅ APPLIED 2026-07-25.** `apt-mark hold` on the **entire 580 family, 15 packages**, not
the 3 the original note suggested. Holding only `nvidia-driver-580 / libnvidia-compute-580 /
nvidia-dkms-580` would have left `nvidia-utils-580`, `libnvidia-gl-580`, `libnvidia-decode-580`,
`nvidia-kernel-source-580` and the rest free to move — i.e. the *same class* of userspace-vs-module
desync that caused this outage could still happen. Held:

```
libnvidia-cfg1-580  libnvidia-common-580  libnvidia-compute-580  libnvidia-decode-580
libnvidia-encode-580  libnvidia-extra-580  libnvidia-fbc1-580  libnvidia-gl-580
nvidia-compute-utils-580  nvidia-dkms-580  nvidia-driver-580  nvidia-firmware-580-580.173.02
nvidia-kernel-common-580  nvidia-kernel-source-580  nvidia-utils-580
```

Verify with `apt-mark showhold`. To upgrade deliberately: `sudo apt-mark unhold <list>`, upgrade,
**reboot**, then re-hold. Note a stale 575 family is also still installed but is not the loaded
driver and was left alone.

---

## Phase 1 — F01 RAG router poisoning ✅ DONE

**Problem.** `RAG_SYSTEM_CONTEXT` defaults false (`env.py:362`), so OWUI **prepends** retrieved file
context to the *last user message* (`middleware.py:803-813` → `add_or_update_user_message(append=False)`).
`pipe()` read that blob via `_last_user()` and ran every routing regex over it. `_is_image_request`
fires on a bare `(image|picture|photo)\s+of` or bare `draw|sketch|paint`; `_is_video_request` on bare
`video|animation|footage`. The `_QUESTION`/`_SMALLTALK` guards use anchored `.match()` and could never
fire, because the blob starts with `### Task:`.

**Reproduced** against the real `Pipe` class (`scratchpad/test_router.py`), pre-fix:

| input (with a PDF attached whose text mentions "picture of" / "draw" / "video") | pre-fix route |
|---|---|
| "summarise this document for me" | ❌ `MEDIA[Generating video]` |
| "what does section 4 say?" | ❌ `MEDIA[Generating image]` |
| "make a picture of a cat" | ❌ `MEDIA[Generating video]` (hijacked to video) |

**Fix.** Route on the pre-injection prompt. Middleware stashes it at `middleware.py:2803`
(`metadata['user_prompt'] = get_last_user_message(...)`) *before* the injection at `:2808`, and
`functions.py:262` passes `__metadata__` to the pipe. OWUI uses the identical fallback at `:4860`.

```python
text, ref = self._last_user(msgs)
routed = (__metadata__ or {}).get("user_prompt")
if isinstance(routed, str) and routed.strip():
    text = routed.strip()
```

`text` feeds only routing + media-prompt builders (`_clean_prompt`, `_edit_instruction`, `_video_opts`,
`_merge_video_prompt`). Chat still uses `omsgs` from the full `msgs`, so the model keeps all RAG context.

**Status. ✅ LIVE.** 6/6 tests pass post-fix (`tests/test_router.py`, re-run post-reboot). `py_compile`
clean. `function.content` md5 `e74cd4ad9901d101ac3fbfc9369fe341` (95012 B) — **DB, `pipes/live/` and
`pipes/` all match**. DB backup: `webui.db.bak-ragroute`. OWUI 0.10.2 started 12:51:02 with no
function-load errors, so the fixed pipe is the one serving traffic.

The test file also asserts the *control*: with routing metadata removed, cases A and B still produce
`MEDIA[Generating video]` / `MEDIA[Generating image]`, so the regression stays reproducible and the
test cannot silently pass for the wrong reason.

Remaining: integration checks 8 and 9 in Phase 9 (real PDF through the real UI).

---

## Phase 2 — Architecture ✅ DONE

**Decision taken (confirmed 2026-07-25): shape B — extra entries in the existing manifold.**

### What shipped

`pipes()` now returns three entries, all in the one Function file so they share `_GEN_LOCK`:

| Entry | Model | Media routing | System messages | `_GEN_LOCK` |
|---|---|---|---|---|
| `auto` | dolphin / gemma4:31b | full router (unchanged) | stripped (as before) | renders only |
| `knowledge` | `chat_model` | **none** | **preserved** | no |
| `coder` | `coder_model` | **none** | **preserved** | **yes, whole stream** |

Implementation notes:

- **`_entry(body)`** parses OWUI's `'<function_id>.<pipe_id>'`. Anything unrecognised — including a
  direct API call with a bare model name, or a missing `model` key — falls back to `auto`, so no
  existing caller can regress.
- **Chat-only is enforced by early return**, before a single media regex runs. So on
  `knowledge`/`coder` a document mentioning "video" *cannot* start a render no matter what the router
  would have decided. This is independent of the Phase 1 fix, which protects `auto`.
- **`keep_system=True`** on the new entries. The historical
  `[guard] + [m for m in messages if m.get("role") != "system"]` strip is what silently discarded
  native memory / Adaptive Memory injection — see Phase 6. `auto` keeps the old strip verbatim.
- **Per-entry guards.** The `auto` guard says *"NEVER output JSON, tool calls, function calls"* — fine
  for a media router, actively harmful for a coding model. `coder` gets its own guard; asserted by test.
- **`coder` holds `_GEN_LOCK` for the entire stream**, acquired via `asyncio.to_thread(...acquire)` so
  the event loop is never blocked, released in a `finally` (verified to survive client disconnect).
  `knowledge` deliberately does *not* take it — it reuses already-warm small models, and serializing
  it would stall document Q&A behind renders for no VRAM benefit.
  Ollama evicts among its own models by itself; the lock exists to stop a coder load landing in the
  middle of a **ComfyUI** render, which Ollama cannot see.

**`coder_model` is currently `dolphin-venice:24b`** — a placeholder so the entry works today. Phase 8
swaps it for the Qwen tag.

### Tests

`tests/test_manifold.py` — **24/24 pass**, covering entry parsing (7 cases incl. malformed input),
chat-only enforcement on both new entries, `auto` still rendering, system-message survival per entry,
model selection, guard separation, and three lock assertions (held during stream / released after /
released on early disconnect). `tests/test_router.py` still 6/6 — the `auto` path is unregressed.

### Deploy

`function.content` md5 `5c5212ecaabf66132ef249e7431c000a` (100186 B); `pipes/`, `pipes/live/` and the
DB all match. DB backup `webui.db.bak-manifold`. OWUI restarted 13:12; verified by loading the
function through OWUI's own `load_function_module_by_id` and calling `pipes()` — all three entries
returned.

<details><summary>Original analysis of the three shapes (kept for the record)</summary>

**⚠️ Finding that complicates the naive split:** `auto_assistant.py` is *already* a manifold
(`pipes()` at L58 returns a list). Critically, **each OWUI Function file loads into its own module
namespace**, so a second Function file gets its **own** `threading.Lock` — the module-level
`_GEN_LOCK` at `auto_assistant.py:12` would no longer serialize GPU work across both. That breaks
the single most valuable property of the current design.

Three shapes, honestly compared:

| Shape | Tools/knowledge/citations | `_GEN_LOCK` VRAM arbitration | Verdict |
|---|---|---|---|
| **A.** Separate plain (non-pipe) model entry | ✅ native, free | ❌ bypasses it entirely | risky: can load a 17.7 GB coder mid-render |
| **B.** Extra entries in the *existing* manifold | ⚠️ must implement tool loop | ✅ shared lock | **recommended** |
| **C.** Second Function file | ✅ | ❌ separate lock | rejected |

**Recommendation: B.** Add `knowledge` and `coder` entries to the existing `pipes()` list. Zero new
processes, one VRAM arbiter, and the media path is untouched. The tool loop is the only extra work,
and the mechanism is already available: `functions.py:266` sets
`extra_params['__tools__'] = metadata.get('tools', {})`, injected into any pipe whose signature names
`__tools__`. Run the loop internally and render results as `<details type="tool_calls">` blocks —
**never** emit `delta.tool_calls`, or middleware re-enters the pipe up to
`CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (256).

</details>

**The internal tool loop was NOT built** — you chose legacy function calling instead (Phase 4), which
delivers knowledge bases, web search and citations with zero further pipe code. The `__tools__`
mechanism above remains available if legacy proves too limiting.

---

## Phase 3 — Documents & OCR

**Your pick was `baidu/Unlimited-OCR`. It is real** (MIT, released 2026-06-23, 3B total / ~500M active
MoE) — but it is the wrong choice for a 24 GB card shared with ComfyUI: **6.67 GB of BF16 weights,
~8.5–9.5 GB resident under vLLM.**

**⚠️ Better: `PaddlePaddle/PaddleOCR-VL-1.6`** — ~1.0 GB weights, Apache-2.0, and *higher* accuracy
(PaddleOCR-VL-1.5 already scores 94.5 % on OmniDocBench v1.5 vs Unlimited-OCR's 93.23 %).

Decisive for this box: an **official GGUF** exists (`PaddlePaddle/PaddleOCR-VL-1.6-GGUF`), so it can
run through the **Ollama you already have** — ~1.0–1.5 GB while decoding, and it **auto-unloads on
`keep_alive`**. No new always-on service, no second CUDA context.

| Layer | Choice | VRAM |
|---|---|---|
| Default extraction | `apache/tika:3.3.0.0-full` (bundles Tesseract + eng/fra/deu/ita/spa/jpn) | **0** |
| Hard scans / complex tables | PaddleOCR-VL-1.6 GGUF via Ollama | ~1.0–1.5 GB, transient |
| Routing between them | OWUI `external` document loader → small FastAPI shim | 0 |

OWUI also ships a native `paddleocr_vl` engine (`config.py:938 PADDLEOCR_VL_BASE_URL`, added v0.9.2),
but it has no file-type routing — the `external` shim is preferred for a mixed knowledge base.

**Host-networking trap — ✅ all confirmed against the live config table (2026-07-25):**

| Key | Live value | Problem |
|---|---|---|
| `rag.tika_server_url` | `http://tika:9998` | no Docker DNS under `NetworkMode=host` → must be `localhost` |
| `rag.docling_server_url` | `http://docling:5001` | same |
| `rag.mineru_api_url` | `http://localhost:8000` | **:8000 is occupied** (tunarr) — never enable MinerU as-is |
| `rag.paddleocr_vl_base_url` | `http://localhost:8080` | ⚠️ **NEW: :8080 is also occupied** (a `python3` listener) |
| `rag.content_extraction_engine` | `""` | currently default/unset — nothing routed yet |

**Ports verified free for this work:** 9998 (Tika), 5001, 8081, 8888. Pick SearXNG and any OCR shim
from those, not from 8000/8080.

Note 0.10.2 stores config as a **flat key/value table** (`config(key, value, updated_at)`), not one
JSON blob — relevant for any scripted config change.

Also fixes today's hard failures: `.msg` (no fallback — hard ingest error), `.doc`, legacy `.ppt`.

---

## Phase 4 — Web search (SearXNG)

`searxng/searxng:latest` — ~97 MB, **0 VRAM**, actively maintained.

**⚠️ The repo you linked is superseded.** `github.com/searxng/searxng-docker` now contains only a
LICENSE and a README reading *"searxng-docker repository is superseded."* Use a hand-written compose
per `docs.searxng.org`.

Three things that break virtually every guide:

1. **`search.formats` must include `json`.** Upstream default is `[html]` only; `webapp.py` returns
   404 for `format=json` otherwise. This is the #1 failure.
2. **`limiter: false`** — upstream default. The deprecated template set it `true`, which is why so
   many guides hit 403/429 on server-to-server queries.
3. **`<query>` placeholder is no longer required** in 0.10.2 — `searxng.py` actively strips it
   (*"Normalise legacy `<query>`-style URLs by stripping any query string"*).

`web.loader.engine` has **six** valid values, not four. Start with `safe_web` (engine unset).
Playwright only if pages come back empty — and note `requirements.txt` pins `playwright==1.60.0`
with a *"version must match docker-compose.playwright.yaml"* caution.

### ⚠️ The blocker you must know about

**With a pipe selected and native function calling (the default), the web-search button does
absolutely nothing** — no search is executed anywhere, and the pipe never sees results
(`middleware.py:2457-2459` → stashed for builtin tools → `functions.py` never calls them).

The same applies to **model-attached Knowledge bases** (`:2377`) and **folder/project files**
(`:2363`). Only per-message drag-and-drop attachments are injected. This is why `knowledge=0` has
been masking the problem.

**Fix A (zero pipe code):** set `function_calling: "legacy"` on the pipe's model entry. Forces RAG
injection for knowledge/folders/web-search, and citations come free.
**Fix B:** implement the internal tool loop (Phase 2).

### ✅ Fix A APPLIED 2026-07-25

Gate confirmed in source: `metadata['params']['function_calling'] == 'legacy'` at
`middleware.py:2363` (folder files), `:2377` (model knowledge) and `:2458-2471` (web search) — exactly
as described.

Model rows created in the `model` table (they do not exist until OWUI syncs the manifold, so they were
inserted directly, mirroring the `auto_assistant.auto` row):

| Model entry | `params` |
|---|---|
| `auto_assistant.auto` | `{}` — **left on native FC deliberately** |
| `auto_assistant.knowledge` | `{"function_calling": "legacy"}` |
| `auto_assistant.coder` | `{"function_calling": "legacy"}` |

`auto` stays native: it is the media router, it has no use for knowledge injection, and leaving it
untouched keeps the Phase 1 blast radius at zero.

Legacy FC routes tool selection through the **task model**, which is `gemma4:e2b` — confirmed to
declare the `tools` capability, so this costs no additional VRAM.

**Still outstanding for Phase 4:** the SearXNG container itself (nothing is configured —
`web.search.enable=false`, `engine=""`, `searxng_query_url=""`).

---

## Phase 5 — Speech

**STT. ❌ The "every mic press 500s" claim is now STALE — the cache was seeded during last session's
measurement run.** Live state:

```
/app/backend/data/cache/whisper/models/   (1.7 GB total)
  models--Systran--faster-whisper-base
  models--mobiuslabsgmbh--faster-whisper-large-v3-turbo
WHISPER_MODEL=base   WHISPER_MODEL_DIR=/app/backend/data/cache/whisper/models
audio.stt.engine=""  (local faster-whisper)   audio.stt.whisper_model="base"
```

Both models are present in HF-hub layout, so `local_files_only=True` should now resolve. **STT is
believed working and needs only a live mic test** (Phase 9 item 4) rather than a seeding step.

<details><summary>Original failure-mode analysis (still the correct explanation of <em>why</em> it broke, and what happens if the cache is ever lost)</summary>

`OFFLINE_MODE=true` hard-wires `WHISPER_MODEL_AUTO_UPDATE=False` (`config.py:1517`) →
`local_files_only=True` → raises → retry with `local_files_only=False` → `HF_HUB_OFFLINE=1` blocks →
**second exception uncaught**. Every mic press 500s forever. Requires seeding
`Systran/faster-whisper-base` (~142 MB).

</details>

Measured on this 3090 (176 s speech, beam_size 5): **base/int8 407 MiB**, base/fp16 535 MiB,
turbo/int8 1399 MiB, turbo/fp16 2391 MiB. **Stay on int8** — fp16 costs more VRAM *and* was 20 %
slower for turbo. `base` transcribes correctly punctuated text; there is no quality cliff.

⚠️ `audio.py:222` places whisper on **CUDA** when `DEVICE_TYPE=cuda`. Investigate forcing CPU
(24 idle cores) without disabling CUDA for the embedder.

**Wildcard — ✅ capability confirmed.** `/api/show` on `gemma4:e2b` returns
`capabilities: ['completion', 'vision', 'audio', 'tools', 'thinking']` with
`gemma4.audio.block_count=12`, `gemma4.audio.embedding_length=1024` (5.1B params, Q4_K_M).

Note the `tools` capability too — it makes `gemma4:e2b` a viable tool-selection model for legacy
function calling (Phase 4 Fix A) at no extra VRAM. **Still untested:** whether Ollama actually
*exposes* transcription for it. Capability metadata declared ≠ working `/api/chat` audio input.

**TTS.** Kokoro via Docker, OpenAI-compatible, `audio.tts.engine='openai'` + local base URL.
Prefer **CPU** on this contended card. Note 0.10.2 also ships a client-side `browser-kokoro` engine,
but it fetches voices from HuggingFace at runtime — needs one online warm-up **per browser profile**.

Your `ebook2audiobook` container bundles piper-tts and coqui-tts but is Gradio-only —
`POST /v1/audio/speech` returns 404, so OWUI cannot use it without a shim.

---

## Phase 6 — Memory

**⚠️ Your pick, Adaptive Memory v3, is disqualified on this box: it injects into the SYSTEM message,
which `auto_assistant.py:1242` strips.**

**Use Adaptive Memory v4.5.0** (`1818TusculumSt/owui-adaptive-memory`, MIT, June 2026, requires
OWUI ≥ 0.10.0). v4+ injects into the **last user message** → works with the pipe **unchanged**.

Filters *do* run for pipe models (`main.py:1475` → filters → `chat.py:280` pipe dispatch). Pin
`llm_model_name` to `gemma4:e2b` (already your task model) → **0 new VRAM**. Pointing it at a third
distinct Ollama tag would force evictions on every turn.

OWUI's native memory also injects via system message, so it is likewise dead behind the pipe today.
✅ Confirmed live: `memories.enable=true` **and** `memories.system_context.enable=true` — i.e. native
memory is switched on and injecting into exactly the message `auto_assistant.py:1242` strips. Anything
the user has stored there is currently being silently discarded on every turn.

---

## Phase 7 — Retrieval quality

**Skip LiteLLM.** For local-only Ollama + OWUI it buys model aliasing and spend tracking you do not
need (single user, `auth.enable_api_keys=False`, one GPU) at the cost of an extra hop, container and
failure mode. Its `/rerank` *is* schema-compatible with `rag.external_reranker_url` — but you would
still have to run a reranker server behind it.

**⚠️ `bge-reranker-large` is superseded by `BAAI/bge-reranker-v2-m3`** — same ~0.6 B size class,
multilingual, longer context, BAAI's own successor recommendation. Same VRAM, strictly better.

| Option | VRAM | Note |
|---|---|---|
| OWUI built-in CrossEncoder | ~2.2–2.4 GB fp32, **permanently resident** | zero containers |
| **Infinity** (`michaelfeil/infinity`) | ~1.2 GB fp16, separate CUDA context | native Cohere schema — **recommended** |
| HF TEI | — | ❌ **incompatible**: returns a bare array, not `{results:[...]}` |

Current settings are the weak link — ✅ confirmed live: `rag.top_k=3`, `rag.top_k_reranker=3`,
`rag.enable_hybrid_search=false`, `rag.reranking_model=""`, `rag.relevance_threshold=0`,
`chunk_size=1000/overlap=100`. Target `top_k=20` → `top_k_reranker=3`.

### ✅ RESOLVED: hybrid search **does** work on ChromaDB

The "possibly pgvector-only" flag was **wrong**. `retrieval/utils.py` has two paths:

1. `_supports_native_hybrid_search()` (`:378`) checks the client for `supports_hybrid_search` /
   `hybrid_search`. **Only `pgvector.py` defines one** (`:538`); `chroma.py` has zero matches.
2. So Chroma falls through to the **legacy path** (`:500-535`): langchain `BM25Retriever.from_texts()`
   over the collection's documents, fused with the vector retriever by `EnsembleRetriever` using
   `weights=[hybrid_bm25_weight, 1.0 - hybrid_bm25_weight]` and RRF (deduped on `CHUNK_HASH_KEY`).

That path is **vector-DB agnostic** — pure Python over fetched docs. `VECTOR_DB` is unset, so it is
the `chroma` default (`config.py:493`). **Hybrid can be turned on as-is.**

**Caveat that replaces it:** the legacy path rebuilds the BM25 index *per query* from the whole
collection. Fine today (`data/vector_db` is 188 KB), but it scales linearly with knowledge-base size —
the real reason to consider Qdrant (already running on 6333, unused) later, not a correctness reason.

Embedding upgrade from `all-MiniLM-L6-v2` (384-dim / 256-token) is optional; reranking buys more per
GB. Note the 889 MB cache is bloat — nine redundant serializations of an ~87 MB model.

---

## Phase 8 — Coding model

**⚠️ "Qwen 3.6 Coder 35B-A3B" does not exist.** The Qwen3.6 line has exactly two open-weight
releases: **Qwen3.6-35B-A3B** (MoE, 2026-04-16) and **Qwen3.6-27B** (dense, 2026-04-22). No
Coder-suffixed variant.

**⚠️ Do not pull `ollama run qwen3.6:35b`** — it resolves to a **23.94 GB** layer against ~24.35 GB
usable. It will OOM or silently spill. This is the single most important sizing decision here.

**Use `hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS` — 17.73 GB.**

**⚠️ Headroom is tighter than previously stated.** Measured on this box (2026-07-25, idle desktop
939 MiB, nothing else loaded):

| Source | `gemma4:e2b` cost |
|---|---|
| `/api/tags` on-disk size | 7.16 GB (MatFormer container — the full nested model) |
| `/api/ps` `size_vram` | **1.95 GB** (what the plan quoted) |
| **`nvidia-smi` delta, cold load → resident** | **~3.30 GB** (939 → 4242 MiB) |

The ~1.35 GB gap is CUDA context + KV cache + compute buffers, and it is *real* allocation. So the
budget is **17.73 + 3.30 + 0.94 (desktop) ≈ 21.97 GB of 24.35 GB usable — ~2.4 GB spare, not ~5 GB.**
Still workable, but there is no room for a third tenant, and `top_k`/context increases eat into it.

Also worth knowing: cold-loading `gemma4:e2b` took **54.5 s** (7 GB read from disk). Anything that
evicts the task model pays that on the next new chat, not a few hundred ms. Disk has 363 GB free, so
the 17.73 GB pull itself is not a concern.

MoE vs dense on a 3090: **~135 t/s** (A3B, ~3 B active) vs **~20–25 t/s** (dense 27B) at the same
~17–18 GB residency. The MoE dominates. **Do not add speculative decoding** — all 19 tested configs
were net-negative on Ampere.

**Consider replacing `dolphin-venice:24b` rather than adding a fourth tenant.** Your own `MODELS.md`
already flags this as the one endorsed future swap ("an uncensored Qwen3-30B-A3B would run the
agentic pipe helpers 2-3× faster at similar VRAM"). Maintained uncensored Qwen3.6-35B-A3B derivatives
exist. Caveat: abliteration measurably degrades coding/agentic quality — consider official weights
for the coder entry and an uncensored tag only for the Photoreal helper slot.

### Orchestration (what the small model actually does)

`routers/tasks.py` exposes 8 task endpoints resolved through `get_task_model_id`: title, tags,
retrieval-query generation, autocomplete, follow-up suggestions, image-prompt, and tool selection in
legacy mode. All run on `gemma4:e2b` (1.9 GB).

**⚠️ Landmine:** `utils/task.py:16-27` — `get_task_model_id` branches on `connection_type`. A
misconfiguration here sends task calls to the 14.3 GB chat model instead, costing a lock-serialized
stall on **every new chat**. Verify after any model-entry change.

✅ **Currently correct:** `task.model.default = "gemma4:e2b"` and `task.model.external = "gemma4:e2b"`,
so *both* branches resolve to the small model. Re-check both keys after any model-entry change.

**Models on disk today:** `gemma4:31b` 19.87 GB · `dolphin-venice:24b` 14.33 GB · `gemma4:e2b` 7.16 GB
· `gemma3:1b` 0.82 GB.

Target lifecycle: regex router (0 cost) → task model for decomposition/query-gen (1.9 GB) →
`_free_vram()` → exactly one large tenant per turn under `_GEN_LOCK` → unload.

---

## Phase 9 — QA plan

Each phase ships only when its tests pass. Run `scratchpad/test_router.py` after **any** pipe edit.

**Per-component (individually, before integration):**

1. **Tika** — `curl -T scan.pdf localhost:9998/tika`; text-layer PDF, scanned PDF, `.doc`, `.msg`.
2. **PaddleOCR-VL** — OCR a scanned page; confirm Ollama unloads it (`/api/ps` empty after keep_alive).
3. **SearXNG** — `curl 'localhost:<port>/search?q=test&format=json'` must return JSON, not 404.
4. **Whisper** — load with `local_files_only=True`; transcribe; confirm VRAM delta ≈ 407 MiB.
5. **Kokoro** — `POST /v1/audio/speech`; confirm keyless auth tolerated.
6. **Reranker** — POST Cohere-schema payload; assert `{results:[{index,relevance_score}]}`.
7. **Qwen3.6** — pull UD-IQ4_XS; assert resident ≤ 18 GB; measure t/s; verify tool-call format.

**Integration (the part that actually matters):**

8. Attach a PDF containing "video"/"picture of", ask a question → **must chat, not render**. ✅ passing.
9. Same PDF, ask "make a picture of a cat" → must render an image, prompt taken from the clean words.
10. Knowledge base + question → citations render as clickable badges.
11. Web-search button with the pipe selected → confirm results actually reach the model.
12. Memory: state a fact, new chat, recall it.
13. **Contention test:** start a Wan render, then ask a coder question → must serialize under
    `_GEN_LOCK`, not OOM. This is the test that proves the architecture.
14. Task-model check: new chat → title/tags generated by `gemma4:e2b`, **not** the 14.3 GB model.
15. Re-run the full 17/17 routing regression from `openwebui-improvement-plan.md`.

**Rollback.** DB backup before every deploy (`webui.db.bak-<name>`); `pipes/live/*.py` is the source
of truth and must stay md5-identical to `function.content`.

---

## Questions

**Answered 2026-07-25:**

1. ~~**Architecture.**~~ → **Manifold entries (shape B).** Built, tested, deployed. Phase 2.
3. ~~**Legacy vs native FC.**~~ → **Legacy first, internal tool loop reconsidered later.** Applied to
   both new entries. Phase 4.
5. ~~**Driver pinning.**~~ → **Yes, `apt-mark hold`** — widened to all 15 packages in the 580 family.
   Phase 0.

**Still open — these block the phases named:**

2. **Coder role** (blocks Phase 8). Add Qwen3.6-35B-A3B as a fourth tenant, or **replace**
   `dolphin-venice:24b` (your own `MODELS.md` endorses this)? Note the revised VRAM math: coder
   (17.73) + task model (3.30 measured, not 1.9) + desktop (0.94) ≈ **21.97 GB of 24.35 usable**.
   Replacing frees the 14.33 GB dolphin tag from disk and removes an eviction candidate; adding keeps
   the uncensored helper the Photoreal pipe relies on.
4. **OCR ambition** (blocks Phase 3). Tika-only (0 VRAM, fixes today's `.msg`/`.doc`/`.ppt` hard
   errors), Tika + PaddleOCR-VL-1.6 via Ollama (~1.0–1.5 GB transient, much better on complex
   tables/layout), or Tika now and judge OCR against your real documents afterwards?

---

## Change log

- **2026-07-25** — Plan created. F01 (RAG router poisoning) fixed, tested 6/6, deployed to
  `function.content`; pending OWUI start. Phase 0 host blocker identified (NVIDIA 580.159.03 module
  vs 580.173.02 userspace, upgraded 07:00:58). Adversarial verification pass incomplete — items
  marked ⚠️ are single-sourced.
- **2026-07-25 (post-reboot)** — Phase 0 ✅ resolved by reboot; driver consistent at 580.173.02, all
  containers back. F01 confirmed **live** (OWUI start 12:51:02 clean, DB md5 matches disk, 6/6
  re-run). Ran the deferred verification pass against the live system:
  - ❌ **Corrected:** hybrid search is *not* pgvector-only — Chroma uses the legacy `BM25Retriever` +
    `EnsembleRetriever` fallback. Phase 7 unblocked.
  - ❌ **Corrected:** the whisper cache exists and is seeded (1.7 GB); the "mic always 500s" claim is
    stale. Phase 5 STT reduced to a live test.
  - ⚠️ **Corrected:** `gemma4:e2b` costs **~3.3 GB** measured, not 1.9 GB. Phase 8 coder headroom
    revised from ~5 GB to ~2.4 GB.
  - ✅ Confirmed: tika/docling host-net traps, MinerU↔tunarr :8000 collision, `gemma4:e2b` `audio` +
    `tools` capabilities, task model correctly pinned on both branches, native memory injecting into
    the stripped system message.
  - 🆕 **New:** `rag.paddleocr_vl_base_url` default `:8080` is also an occupied port on this host.
  - 🆕 **New:** 0.10.2 stores config as a flat key/value table, not a single JSON blob.

  Then shipped three phases:
  - **Phase 0 prevention ✅** — `apt-mark hold` on all 15 NVIDIA 580 packages (widened from the 3
    originally suggested, which would have left the same desync possible).
  - **Phase 2 ✅** — manifold entries `knowledge` + `coder` added to `auto_assistant.py`, sharing
    `_GEN_LOCK`. 24/24 new tests, 6/6 existing tests. Deployed (md5 `5c5212ec…`), OWUI restarted,
    entries verified live through OWUI's own function loader.
  - **Phase 4 (partial) ◐** — `function_calling: "legacy"` set on both new model entries, so knowledge
    bases, folder files, web search and citations reach the model. SearXNG itself still to do.

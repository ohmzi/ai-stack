# Open WebUI capability upgrade — plan

_Created 2026-07-25. Target: make the stack a genuinely versatile offline assistant (documents, OCR,
search, speech, memory, tools, coding) without breaking the VRAM-budget-first design._

**Progress: 8.5 / 9 phases**

| Phase | Scope | Status |
|---|---|---|
| 0 | Host unblock (NVIDIA driver) | ✅ resolved + **pinned** (15 pkgs) |
| 1 | RAG router poisoning fix | ✅ live 12:51 |
| 2 | Architecture: manifold entries | ✅ live 13:12 |
| 3 | Documents & OCR | ✅ Tika live on :9998, OCR verified |
| 4 | Web search (SearXNG) | ✅ live on :8888, JSON verified |
| 5 | Speech | ◐ TTS live on :8081; **STT blocked — see below** |
| 6 | Memory (Adaptive Memory **v4.4.1**) | ✅ installed, scoped to knowledge/coder |
| 7 | Retrieval quality | ✅ top_k=20 + hybrid on (reranker deferred) |
| 8 | Coding model (Qwen3.6-35B-A3B) | ◐ pulled + wired; **co-residency problem, see below** |
| 9 | Full QA sweep | ◐ automated done; browser checklist for you |

### ⚠️ Two items need a decision before they can be finished

**1. STT device — the chosen fix is not achievable as specified.** Decision 9 was "force whisper to
CPU without disabling CUDA for the embedder". That is **impossible in 0.10.2**: `audio.py:222` reads
the *global* `DEVICE_TYPE`, which `env.py:45-56` derives solely from `USE_CUDA_DOCKER`. The same
global drives the embedding model (`retrieval.py:151,207`). There is no `WHISPER_DEVICE` override.
So the real options are:

| Option | Whisper | Embedder | Note |
|---|---|---|---|
| Leave as-is | CUDA, 407 MiB | CUDA, **360 MiB measured** | status quo |
| `USE_CUDA_DOCKER=false` | CPU, 0 | CPU, 0 | frees 360 MiB permanently; needs container **recreate** |
| Patch `audio.py` in-container | CPU | CUDA | lost on every image update — not recommended |

`USE_CUDA_DOCKER=false` best matches the intent and over-delivers (all-MiniLM-L6-v2 is 22 M params —
CPU embedding on 24 cores is a non-issue for a personal KB). It needs open-webui recreated, which is
why it was not done unilaterally.

**2. The coder evicts the task model — measured, not theoretical.** Phase 8 assumed the coder and
`gemma4:e2b` stay co-resident. **They do not.** Tested in both load orders:

| Combination | Result |
|---|---|
| coder (18.37 GB) + `gemma4:e2b` (3.3 GB) | ❌ mutual eviction, whichever loads second wins |
| same, both forced to `num_ctx=8192` | ❌ still evicts — not a context-length problem |
| coder + **`gemma3:1b`** (0.99 GB) | ✅ **both resident, 20561 / 24576 MiB** |

Consequence as configured: every new chat generates a title/tags on `gemma4:e2b`, evicting the coder.

**⚠️ Severity revised down after measuring the reload properly.** The 38 s figure is *cold from disk*.
This box has **94 GB RAM with ~75 GB in page cache**, so the 18 GB GGUF stays cached and a re-load
costs **6.1 s**, not 38 s — measured back to back:

| Reload | Time |
|---|---|
| Cold, first ever load from NVMe | 38.2 s |
| **Warm page cache (the normal case)** | **6.1 s** |

So in real use the eviction costs ~6 s on the first coder message after a new chat. Annoying, not
broken. This moves from "must fix" to "fix if it bothers you". Options if it does: switch the task
model back to `gemma3:1b` (co-resides — reverses commit `619a85c`), disable
`task.title.enable`/`task.tags.enable`, or take a smaller coder quant.

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

## Phase 2b — Automatic coder routing on 🪄 auto ✅ DONE

**Question answered: no, you do not have to switch entries to get the coder.** 🪄 Assistant now picks
the model itself. What it *cannot* do is switch on knowledge bases, web search or citations —
`function_calling: "legacy"` is read by middleware at `:2363`/`:2377`/`:2458` **before the pipe is
ever invoked**, so it is a per-entry setting and the pipe has no say. 📚 Knowledge stays the entry
for document- and recall-shaped work.

### How the decision is made — three tiers, so the common cases cost nothing

| Tier | Trigger | Cost | Action |
|---|---|---|---|
| **STRONG** | fenced code, `def`/`class`/`import`/`#include`, `SELECT…FROM`, a traceback, `TypeError:`, or a build/fix verb aimed at a code noun | 0 | → coder immediately |
| **HINT** | programming vocabulary that is regularly used about non-code things (`python`, `rust`, `docker`, `git`, `java`…) | one small LLM call | ask the classifier |
| neither | — | 0 | → normal chat |

**Classifier is `gemma3:1b` (0.99 GB), and the choice is load-bearing.** It is the one model *proven*
to stay co-resident with the 18.37 GB coder (20561/24576 MiB measured), so a classify→answer turn
costs **one** model load. `gemma4:e2b` at ~3.3 GB would evict the coder, making every routed turn pay
**two** loads. Do not "upgrade" the classifier without re-checking co-residency.

### Ordering guarantees

- **Media always wins.** Coder routing is reached only after every media branch declines, so
  *"make a picture of a python snake in a data centre"* still renders an image.
- **Vision beats coder.** A turn carrying an image goes to `gemma4:31b`; the coder is text-only.
- **Routing uses the clean prompt** (`user_prompt` + `_strip_injected_context`), so a RAG blob full of
  Python or a memory saying *"the user is a Rust developer"* cannot drag the conversation to the coder.
- **Failure degrades to chat.** Classifier unreachable, timed out, or returning junk → ordinary chat.
  A routing helper must never be able to break the chat path.
- The coder route holds `_GEN_LOCK` for the whole stream, exactly as the 💻 Coder entry does.

### Measured classifier accuracy — `gemma3:1b`, 12 deliberately ambiguous prompts: **10/12**

Both misses are in the safe direction and are arguably not wrong: *"my python keeps dying on me"* and
*"is javascript still worth learning in 2026?"* → CODE. A coding-tuned model answering a career
question is fine; the chat model fumbling a borrow-checker question is not. **There were no misses in
the dangerous direction** — every genuine coding question routed correctly.

A "refined" classifier prompt was tried and **rejected on evidence**: it scored 7/12, fixing the
JavaScript case but breaking four clear CHAT ones (*"my java tastes burnt this morning"* → CODE,
which would waste an 18 GB load). The original prompt is kept.

### Tests

`tests/test_autoroute.py` — **39 checks, all passing**, including: 8 STRONG cases that must route
without consulting the classifier, 5 plain cases that must not consult it either, media-wins cases
phrased with programming words, an attached image beating the coder, RAG- and memory-injected text
failing to pull the route, and classifier-unreachable degrading to chat.

`tests/qa_live.py` grew cases **B1/B2** — on 🪄 auto, *"tallest mountain in Africa"* → `dolphin`
(answered Kilimanjaro) and *"reverse a string without slicing"* → `Qwen3.6` (answered with a loop).
Both verified at the wire level and graded PASS. **16/16 overall.**

**Toggle:** `AUTO_ROUTE_CODER = True` at the top of `auto_assistant.py`. Set `False` to go back to
manual entry selection.

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

## Phase 6 — Memory ✅ INSTALLED (with a hazard that needed guarding)

**Installed: Adaptive Memory — actual version `v4.4.1`, not v4.5.0.** Repo
`1818TusculumSt/owui-adaptive-memory`, single 401 KB file `adaptive_memory_v4.0.py`, last pushed
2026-07-01. **The GitHub API reports no license** — there is no LICENSE file in the repo, so the
plan's "MIT" claim is unverified. Copy kept at `filters/adaptive_memory.py`.

**Injection target verified by reading the code, not the README.** `_inlet_inject_memories`
(L8819) prepends to the **last user message** (`target="user_message"`), so it works behind the pipe.
Note the call site's comment says *"# 3. Inject into system prompt"* — that comment is stale and
misleading; the implementation does not do that.

**Upstream defaults that would have silently failed on this host — both corrected in the stored valves:**

| Valve | Upstream default | Set to | Why |
|---|---|---|---|
| `llm_model_name` | `llama4:latest` | `gemma4:e2b` | llama4 is not installed here |
| `llm_api_endpoint_url` | `http://host.docker.internal:11434/api/chat` | `http://localhost:11434/api/chat` | open-webui is `NetworkMode=host`; that name does not resolve |
| `embedding_source` | `auto` | `auto` (kept) | prefers OWUI's existing embedder → **no second embedding model, 0 new VRAM** |

Dependencies: `prometheus_client` is missing in the container and `OFFLINE_MODE=true` skips
requirement installation — **harmless**, the import is wrapped in `try/except ImportError` with a
no-op metric fallback. All other imports resolve.

### ⚠️ The hazard this created, and the guard added for it

Adaptive Memory prepends to the last user message. **So does the RAG injection that caused Phase 1.**
And the ordering is against us:

```
middleware.py:2428   inlet FILTERS run, mutating form_data['messages']   <-- memory injected here
middleware.py:2803   metadata['user_prompt'] = get_last_user_message(form_data['messages'])
middleware.py:2808   RAG source context applied (this is what Phase 1 dodged)
```

`user_prompt` is captured **after** filters, from the same `form_data`. So filter-injected text lands
in the pipe's routing input — the Phase 1 failure mode through a different door. A stored memory
reading *"asked for a picture of their dog"* could make an ordinary question start a render.

**Two mitigations, both applied:**

1. **Scoped, not global.** The filter is `is_global=0` and attached via `model.meta.filterIds` to
   `auto_assistant.knowledge` and `auto_assistant.coder` only — the two entries that do no media
   routing at all. `auto` has `filterIds=[]`. This also matches decision 5 (keep `auto` lean).
2. **`_strip_injected_context()` in the pipe.** Removes known filter blocks from the routing text
   before any regex runs. It exists because making the filter global is a *single toggle in the UI*
   and the resulting failure would be silent and expensive.

Tested (`tests/test_router.py`, cases G/H/I): memory block + question → chat; memory block + genuine
image request → still renders; **memory block with no user text at all → chat**. That last case
caught a real bug in the first version of the guard, which returned an empty string and then fell
back to the *unstripped* text, routing to video. The suite includes a control with the guard disabled
that reproduces exactly that.

<details><summary>Original Phase 6 analysis</summary>

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

*(Superseded by Phase 2: `keep_system=True` on the knowledge/coder entries means native memory now
reaches those two entries as well. It remains dead on `auto`, which is intended per decision 5.)*

</details>

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

**Per-component — RUN 2026-07-25:**

| # | Test | Result |
|---|---|---|
| 1 | **Tika** text-layer PDF | ✅ HTTP 200, 12 299 B extracted from a real 286-page-book review PDF |
| 1b | **Tika** legacy `.doc` | ✅ HTTP 200, text extracted — **was a hard ingest error before** |
| 1c | **Tika** OCR on an image-only PDF | ✅ HTTP 200, 2 659 B of clean text (rasterised at 150 dpi to force OCR) |
| 2 | PaddleOCR-VL | ⏭️ skipped per decision 4 — Tika's Tesseract handled the scan well |
| 3 | **SearXNG** `format=json` | ✅ HTTP 200, **28 results** — the `formats: [html, json]` fix works |
| 4 | **Whisper** | ⏸️ blocked on the STT device decision above |
| 5 | **Kokoro** `POST /v1/audio/speech` | ✅ HTTP 200, 38 445 B, valid MPEG layer III 24 kHz mono, **keyless auth tolerated** |
| 6 | Reranker | ⏭️ deferred per decision 7 |
| 7 | **Qwen3.6** | ✅ pulled (17 730 509 792 B = 17.73 GB exactly, matching the plan) |
| 7b | Qwen3.6 residency | ⚠️ **18.37 GB actual** (880 → 19 250 MiB), not 17.73 — that figure is weights-only |
| 7c | Qwen3.6 throughput | ✅ **102.9 tok/s** (894 tok / 8.69 s). Plan predicted ~135; same order, still ~4-5× a dense 27B |
| 7d | Qwen3.6 cold load | 38.2 s |

**Trap avoided during execution:** the first `ollama pull` went to `tday_ollama` — a *different*
Ollama instance on a bridge network belonging to an unrelated project. The pipe uses
`localhost:11434`, which is the **host `ollama.service` (systemd)**. The 18 GB was re-pulled to the
right instance and reclaimed from the wrong one. If you script Ollama work, target the systemd
service, not the container.

**Host Ollama tuning discovered** (`/etc/systemd/system/ollama.service.d/multi-model.conf`) — none of
this was in the plan and all of it affects Phase 8 sizing:

```
OLLAMA_MAX_LOADED_MODELS=3   OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0    OLLAMA_CONTEXT_LENGTH=32768   OLLAMA_KEEP_ALIVE=60s
```

`KEEP_ALIVE=60s` explains the cold-load costs measured throughout: nothing stays warm between turns.

### Live orchestration QA — RUN 2026-07-25, `tests/qa_live.py`, **12/12 PASS**

Real `Pipe`, real Ollama inference, model recorded at the aiohttp transport layer (so it is the
payload actually sent, not what the code claims), answers graded by `gemma4:31b` as judge.

| Case | Entry | Model actually reached | Answer | Proves |
|---|---|---|---|---|
| A1 | knowledge | `dolphin-venice:24b` ✅ | ✅ "Canberra" | entry → correct model |
| A2 | knowledge | `dolphin-venice:24b` ✅ | ✅ 80 km/h | multi-step reasoning intact |
| A3 | knowledge | `dolphin-venice:24b` ✅ | ✅ "Persimmon / matte green" | **`keep_system=True` works end-to-end** |
| C1 | coder | `Qwen3.6-35B-A3B` ✅ | ✅ correct iterative `fib` | coder entry → coder model |
| C2 | coder | `Qwen3.6-35B-A3B` ✅ | ✅ names the mutable-default bug | real code reasoning |
| C3 | coder | `Qwen3.6-35B-A3B` ✅ | ✅ used the `zz_` prefix | system-message conventions honoured |

A3 and C3 are the important ones. Their facts (*"bicycle named Persimmon"*, *"prefix helpers with
`zz_`"*) exist nowhere in training data, so a correct answer is proof the system message survived to
the model — the exact thing the old unconditional strip destroyed. Both showed
`roles=['system','system','user']` on the wire: the pipe's own guard **plus** the caller's system
message.

*Minor observation, not a defect:* C3 answered in JavaScript for a prompt that never named a
language. The criterion was the prefix, so it passed. Specify the language when it matters.

### Your checklist (browser-only — these genuinely cannot be driven from a shell)

Everything shell-drivable is now automated and passing. The OWUI **HTTP API requires your browser
session** (`auth.enable_api_keys=false`, and extracting the session-signing secret to mint a token was
correctly refused), so these need you. Exact prompts and exact pass criteria:

**Setup (30 s)**
1. Open OWUI. In the model picker, confirm **three** entries exist: 🪄 Assistant, 📚 Knowledge, 💻 Coder.

**The Phase 1 regression — the bug that started all this (2 min)**
2. Select **🪄 Assistant**. Attach any PDF whose text contains the words "video" or "picture of"
   (`~/Downloads/ojsadmin,+Men+in+Charge.pdf` works). Send: `summarise this document for me`
   → **PASS = it writes a summary. FAIL = it starts rendering an image or video.**
3. Same chat, same PDF attached, send: `make a picture of a cat`
   → **PASS = renders a cat** (not something built from the document's words).

**Documents / Tika (3 min)**
4. Workspace → Knowledge → create a base. Upload a `.doc`, a `.msg`, and a scanned/photographed PDF.
   → **PASS = all three ingest with no error.** `.doc`/`.msg` used to hard-fail.
5. Attach that base to **📚 Knowledge**, ask a question answerable only from it.
   → **PASS = correct answer AND citation badges appear.**

**Web search — this silently did nothing before (1 min)**
6. On **📚 Knowledge**, toggle the web-search button, ask: `what is the latest stable Linux kernel version?`
   → **PASS = it searches and the answer reflects results.** FAIL = it answers from memory or ignores it.

**Memory (2 min, needs two chats)**
7. On **📚 Knowledge**: `Remember that my project deadline is 14 August and my editor is Helix.`
8. Start a **new chat**, still 📚 Knowledge: `what is my project deadline and which editor do I use?`
   → **PASS = recalls both.** (Adaptive Memory is scoped to Knowledge/Coder — it will *not* work on
   🪄 Assistant, by design.)

**Speech (1 min)**
9. Press the 🔊 speaker icon on any reply → **PASS = audible speech** (Kokoro).
10. Press the 🎤 mic, say a sentence → **PASS = transcribed.** *Currently runs whisper on GPU; see the
    STT decision still outstanding.*

**The architecture test — the one that actually matters (5 min)**
11. On **🪄 Assistant**: `make a video of a dog running through a field`. While it is rendering,
    open a second chat on **💻 Coder** and ask: `write a python decorator that retries on exception`.
    → **PASS = the coder answer waits, then arrives. FAIL = CUDA OOM, or either job dies.**
    This is what `_GEN_LOCK` exists for and nothing else has exercised it.

**Task model (30 s)**
12. Start any new chat and let it auto-title. Then run `ollama ps` in a terminal.
    → **PASS = `gemma4:e2b` appears**, not the 14 GB or 18 GB model.
13. After using 💻 Coder, start a new chat, then go back and ask Coder something.
    → Expect a **~6 s** pause (not 38 s) on the first message — the eviction described above.

**Integration (original list, for reference):**

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

## Decisions

**All open questions closed 2026-07-25. No blockers remain.**

| # | Question | Decision |
|---|---|---|
| 1 | Architecture | **Manifold entries (shape B)** — shipped, Phase 2 |
| 2 | Legacy vs native FC | **Legacy first**, internal tool loop reconsidered later — shipped, Phase 4 |
| 3 | Driver pinning | **Yes, `apt-mark hold`** — widened to all 15 × 580 packages — shipped, Phase 0 |
| 4 | OCR ambition | **Tika now, judge OCR against real documents afterwards** — Phase 3 |
| 5 | `auto` entry treatment | ~~Keep it lean~~ → **REVERSED 2026-07-26: `auto` gets everything.** See below |
| 6 | Memory | **Adaptive Memory v4.5.0**, pinned to `gemma4:e2b` — Phase 6 |
| 7 | Retrieval | **Settings only, 0 VRAM.** `top_k` 3→20, hybrid on, `top_k_reranker` 3. No reranker yet — Phase 7 |
| 8 | Coder role | **Add Qwen3.6-35B-A3B as a fourth tenant**; keep `dolphin-venice:24b` for the uncensored Photoreal helper — Phase 8 |
| 9 | Whisper device | **Force CPU** (0 VRAM, 24 idle cores) without disabling CUDA for the embedder — Phase 5 |
| 10 | TTS | **Kokoro on CPU**, OpenAI-compatible, local base URL — Phase 5 |
| 11 | QA split | **Automated checks run by Claude; a short numbered checklist for the browser-only tests** — Phase 9 |

### Decision 5 reversed — 🪄 Assistant is now fully powered (2026-07-26)

**Trigger.** Toggling web search on 🪄 Assistant produced *"I don't have real-time browsing
capabilities"*, while the identical question on 📚 Knowledge searched and answered correctly. That was
the documented consequence of decision 5, not a bug — but it is the wrong trade in daily use.

**Applied.** All three now carry `function_calling: "legacy"` and the `adaptive_memory` filter, and
the pipe passes `keep_system=True` on `auto` (`AUTO_KEEP_SYSTEM`):

| Entry | function_calling | filters | keep_system |
|---|---|---|---|
| `auto_assistant.auto` | **legacy** *(was native)* | **adaptive_memory** *(was none)* | **True** *(was False)* |
| `auto_assistant.knowledge` | legacy | adaptive_memory | True |
| `auto_assistant.coder` | legacy | adaptive_memory | True |

**Why this is safe — the concern I raised earlier was overstated, and one part of it was simply wrong.**

1. *"Legacy FC injects a RAG blob on every turn."* **Wrong.** Every gate is conditional on something
   being active: `:2456` fires only when the user toggles web search (`features['web_search']`),
   `:2377` only when a knowledge base is attached to the model entry, `:2363` only when folder files
   exist. `features` is popped from the per-message request at `:2438`. An ordinary chat turn injects
   nothing at all, so the media path is untouched.
2. *"It puts document text on the render path."* **It cannot reach the router.**
   `chat_web_search_handler` attaches results to `form_data['files']` — it never touches
   `form_data['messages']`. Messages are only merged at `:2808`, **after** `user_prompt` is captured
   at `:2803`. So search and file context land on exactly the path the Phase 1 fix was built for, and
   `tests/test_router.py` cases A/B/E already prove that path is clean.
3. **System messages are never routing input.** Routing reads `metadata['user_prompt']` plus
   `_strip_injected_context`, so `keep_system=True` has no routing consequence.
4. Memory *does* reach the routing text (filters run at `:2428`, before `:2803`) — which is precisely
   why `_strip_injected_context` exists. Cases G/H/I cover it, and R08 in the eval suite covers it
   end-to-end.

**Not enabled:** `capabilities.image_generation` stays `false` on all three. The pipe does its own
ComfyUI rendering; turning on OWUI's handler as well would double-generate.

**Inventory at time of change:** no tools installed, no tool servers configured, no knowledge bases
created yet. Code interpreter is enabled (pyodide). So "all the tools" currently means web search,
memory, file/folder context and citations — plus the pipe's native media generation.

Tests updated: `test_manifold` previously asserted *"auto: system message stripped"* and correctly
**failed** on this change; it now asserts preservation for all three entries. 9/9 router, 24/24
manifold, 39/39 autoroute all pass.
- **Decision 8 means four large tags on disk (~59 GB of 363 GB free).** Only one large tenant is
  resident at a time under `_GEN_LOCK`, so this costs eviction churn and cold-load latency
  (`gemma4:e2b` alone measured **54.5 s** cold), not simultaneous VRAM.
- **Decision 7 defers the reranker, not cancels it.** Re-measure retrieval after `top_k=20` + hybrid;
  if quality is still short, Infinity + `bge-reranker-v2-m3` (~1.2 GB) is the next step — but with the
  coder resident the budget is ~23.2/24.35 GB, so it is genuinely tight.
- **Decision 9 has an open sub-problem:** `audio.py:222` selects CUDA from `DEVICE_TYPE`. Forcing
  whisper to CPU must not also push the embedding model off the GPU.

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

  Then shipped, in a second pass, Phases 3/4/5(TTS)/6/7/8 — see the change-log entry below.

- **2026-07-25 (execution pass)** — all 11 open decisions answered; six more phases built and verified.
  - **Phase 3 ✅** Tika 3.3.0.0-full on :9998. Verified on a real text-layer PDF, a legacy `.doc`
    (previously a hard error) and an OCR-only scanned PDF. PaddleOCR-VL skipped — Tesseract sufficed.
  - **Phase 4 ✅** SearXNG on :8888 returning JSON with 28 results. `formats: [html, json]` and
    `limiter: false` both set in a hand-written `settings.yml` (the searxng-docker repo is superseded).
  - **Phase 5 ◐** Kokoro CPU TTS on :8081, verified end-to-end. STT blocked on a global-flag problem.
  - **Phase 6 ✅** Adaptive Memory **v4.4.1** (not v4.5.0; **no license file** in the repo), scoped to
    knowledge/coder, upstream `llama4:latest` / `host.docker.internal` defaults corrected.
    **Found and guarded a new Phase-1-class hazard** — filters inject before `user_prompt` is captured.
  - **Phase 7 ✅** `top_k` 3→20, hybrid search on. 0 VRAM. Reranker deferred by decision.
  - **Phase 8 ◐** Qwen3.6-35B-A3B pulled and wired (102.9 tok/s, 18.37 GB actual). **Measured that it
    cannot co-reside with `gemma4:e2b`** — a plan assumption that turned out to be false.
  - New services are version-controlled at `compose/docker-compose.yml`, not ad-hoc `docker run`.

  Earlier the same day, three phases:
  - **Phase 0 prevention ✅** — `apt-mark hold` on all 15 NVIDIA 580 packages (widened from the 3
    originally suggested, which would have left the same desync possible).
  - **Phase 2 ✅** — manifold entries `knowledge` + `coder` added to `auto_assistant.py`, sharing
    `_GEN_LOCK`. 24/24 new tests, 6/6 existing tests. Deployed (md5 `5c5212ec…`), OWUI restarted,
    entries verified live through OWUI's own function loader.
  - **Phase 4 (partial) ◐** — `function_calling: "legacy"` set on both new model entries, so knowledge
    bases, folder files, web search and citations reach the model. SearXNG itself still to do.

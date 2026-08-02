# OpenWebUI workspace & media config — snapshot

_Sanitized: model routing + media toggles only. No API keys / secrets / OAuth / LDAP._
_Updated 2026-08-02, re-read from the live DB._

## Installed Functions

| id | name | type | active | repo source |
|---|---|---|---|---|
| `adaptive_memory` | Adaptive Memory | filter | ✅ | `filters/adaptive_memory.py` |
| `animate_scail` | Animate | pipe | ✅ | `pipes/animate_scail.py` |
| `auto_assistant` | auto_assistant | pipe | ✅ | `pipes/auto_assistant.py` |
| `image_krea` | Krea 2 Image | pipe | ✅ | `pipes/image_krea.py` |
| `uncensored` | Uncensored | pipe | ✅ | **`pipes/photoreal.py`** — the one row where the function id and the filename differ |
| `flux_image` | flux_image | pipe | — | `pipes/flux_image.py` |

The `function.name` column above is **not** what the picker shows for a manifold pipe. OpenWebUI
0.10.2 takes the name from the pipe's own `pipes()` return (`functions.py:104`), overridden by an
active `model` row (`utils/models.py:152`). So `image_krea` displays as `Image`, not "Krea 2 Image".

## Workspace models

| id | name | active | note |
|---|---|---|---|
| `auto_assistant.auto` | Ω Assistant | ✅ | display name is the workspace override on the `model` row; the pipe's own fallback is still `🪄 Assistant`. `meta.filterIds = ['adaptive_memory']` |
| `uncensored.photo` | Photoreal | ✅ |  |
| `animate_scail.scail` | Animate | ✅ | row added 2026-08-02 purely to carry capabilities — `web_search`, `code_interpreter`, `image_generation`, `terminal`, `builtin_tools`, `file_context` and `citations` all **false**, `defaultFeatureIds: []` |
| `image_krea.krea2` | Image | — | **hidden 2026-08-02 by request** — the function stays active and deployed; only the picker entry is gone |
| `flux_image.flux-dev` | Image | — | inactive, and its function is inactive too — not in the picker |
| `gemma3:1b` | Gemma3 1B (hidden task model) | — | the configured task model, but hidden — see the warning below |
| `gemma4:e2b` | Gemma 4 E2B (hidden task model) | — | `params = {"think": false}`; **not** the task model right now |
| `hermes-genesis:apex-compact` | Hermes Genesis Apex-Compact (hidden) | — | hidden 2026-08-02; the pipes call it over `http://localhost:11434` directly |
| `hermes-genesis:agent` | Hermes Genesis Agent (hidden) | — | hidden 2026-08-02; driven by the hermes runtime on `127.0.0.1:8642` |
| `bge-m3:latest` | bge-m3 embeddings (hidden) | — | hidden 2026-08-02; retrieval reaches it via `rag.embedding_engine = "ollama"`, verified by `tests/test_retrieval_quality.py` |

### Decision: the picker is curated down to three entries (2026-08-02)

**Intended state — `Ω Assistant`, `Animate`, `Photoreal`, and nothing else.** Every other row
above is hidden on purpose. If a future change makes `Image`, a `hermes-genesis` tag or `bge-m3`
reappear in the dropdown, that is a regression, not a restoration; `tests/test_deployed.py`
fails when it happens.

Restoring any single one is one row: set `is_active = 1` on it.

## Per-model capabilities

`models.default_metadata` turns every capability on (and `defaultFeatureIds` switches the
image/code/web toggles ON by default), so a pipe with **no `model` row inherits all of them** —
which is why Animate showed web-search and code-interpreter buttons it cannot use. Per-model
values win over the defaults (`utils/models.py:309`, `{**default, **existing}`), so the only way
to take a capability away from a manifold entry is to give it a `model` row.

There is no `model` row for `animate_scail.scail`, so it takes its picker name straight from
`pipes()` (`Animate`).

**How hiding works, and what it costs.** An *inactive* `model` row is not a no-op — it is the
hide switch. `get_all_models` deletes the entry outright (`utils/models.py:169`), and because the
picker and the dispatcher read the same `app.state.MODELS`, a hidden model is also **uncallable**:
`main.py:1026` raises `Model not found` for any id absent from that map. There is no
"hidden but still dispatchable" state. This does not affect pipe-to-pipe work — pipes talk to
ComfyUI and Ollama directly and never go through the model registry.

## Media / task config
```
image_generation.enable = false
image_generation.engine = "comfyui"
image_generation.model = "flux1-dev-fp8.safetensors"
image_generation.size = "1024x1024"
image_generation.steps = 20
images.edit.enable = false
images.edit.engine = "openai"
task.model.default = "gemma3:1b"
task.model.external = "gemma3:1b"
task.query.search.enable = true
task.query.retrieval.enable = true
task.title.enable = true
task.tags.enable = true
task.autocomplete.enable = false
ui.default_models = "auto_assistant.auto"
```

> ⚠️ **No task model is in effect.** `task.model.default`/`task.model.external` are `gemma3:1b`,
> but that row is hidden, and `get_task_model_id` (`utils/task.py:21`) only honours the setting
> `if task_model in models` — a hidden model is not in `models`. So the id falls through to
> `default_model_id`, i.e. **the chat's own model**, and titles/tags/RAG-query prompts are answered
> by `auto_assistant` on a 34.7 B model instead of a 1 B one. This is what the `__task__` guard at
> `pipes/auto_assistant.py` exists to contain — without it these prompts triggered real GPU renders
> of `### Task:` boilerplate.
>
> Separately, the `gemma4:e2b` row still carries `{"think": false}` but nothing points at it.
>
> To actually get a small task model back, its row has to be **visible**. Hiding it and setting it
> as the task model are mutually exclusive in 0.10.2.
>
> ⚠️⚠️ **And neither setting is honoured unless the model is *visible*.** `get_task_model_id`
> (`utils/task.py:16-27`) uses the configured id only if it is present in the loaded model
> registry; otherwise it silently falls back to the chat's current model — for a chat on a pipe,
> the pipe itself. `gemma3:1b` has a `model` row but it is **inactive/hidden**, and a raw Ollama
> tag with no row is admin-only in 0.10.2 (see the access-grant note below), so on 2026-08-02
> every title / tag / follow-up / search-query prompt was being executed by the media pipes —
> as real GPU renders. The pipes now guard `__task__` themselves and answer as plain text; a
> server-side belt-and-braces fix is to give `gemma3:1b` an active row plus an access grant.
> Incident and fix: [IMAGE_CONTINUATION.md](IMAGE_CONTINUATION.md).


## Which config rows the runtime actually reads (0.10.2)

Verified 2026-07-29 against the installed image. This asymmetry has cost time twice, so it is
written down.

**Live — read per request, no restart needed.** `Config.get_many()` issues a fresh SELECT on every
call (`models/config.py:146-163`, no cache), and `ENABLE_PERSISTENT_CONFIG` defaults to true:

- `rag.relevance_threshold`, `rag.top_k`, `rag.top_k_reranker`, `rag.hybrid_bm25_weight`
  (read in `retrieval/utils.py:667-672` inside `query_collection`)
- `web.search.domain.filter_list`, `task.query.prompt_template`, `code_interpreter.engine`

**Write-only — the Admin UI persists them and the runtime ignores them.** These are read once at
import as module constants from `os.getenv`, so the DB row is decorative:

- `web.loader.engine` (`WEB_LOADER_ENGINE`), `web.loader.playwright_ws_url` (`PLAYWRIGHT_WS_URL`),
  `web.loader.playwright_timeout`, `web.loader.timeout`, `web.loader.firecrawl_*`,
  `web.loader.external_web_loader_*`

Changing those requires container environment variables — and `open-webui` here was created by hand
(no `com.docker.compose.*` labels), so it means reconstructing its `docker run`.

Confirmed live: `env | grep -E "WEB_LOADER|PLAYWRIGHT"` returns nothing, and
`open_webui.config.WEB_LOADER_ENGINE` is `''` despite `web.loader.engine = "safe_web"` in the DB.

### Two more things found while checking

- **`WEBUI_SECRET_KEY` is not set.** The container logs a hard warning on every start; sessions are
  signed with a key regenerated per boot, so every restart logs everyone out. Wave 1.6 already lists
  pinning it.
- **`rag.relevance_threshold` was `0`, which is falsy.** The gate is `if self.r_score:`
  (`retrieval/utils.py:1722`), so a zero threshold does not mean "keep everything above 0" — it
  means the filter never runs. Now `0.05`, chosen by measurement; see
  `tests/test_retrieval_quality.py`.

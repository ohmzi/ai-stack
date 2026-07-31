# OpenWebUI workspace & media config — snapshot

_Sanitized: model routing + media toggles only. No API keys / secrets / OAuth / LDAP._
_Updated 2026-07-25 (task model → gemma4:e2b, thinking off)._

## Active Functions (pipes)

| id | name | type | active |
|---|---|---|---|
| `animate_scail` | Animate | pipe | ✅ |
| `auto_assistant` | auto_assistant | pipe | ✅ |
| `image_krea` | Krea 2 Image | pipe | ✅ |
| `uncensored` | Uncensored | pipe | ✅ |
| `flux_image` | flux_image | pipe | — |

## Workspace models

| id | name | active | note |
|---|---|---|---|
| `auto_assistant.auto` | Ω Assistant | ✅ | display name is the workspace override on the `model` row; the pipe's own fallback is still `🪄 Assistant` |
| `dolphin-venice:24b` | Dolphin Venice 24B | ✅ | chat + pipe helpers |
| `uncensored.photo` | Photoreal | ✅ |  |
| `flux_image.flux-dev` | Image | — |  |
| `gemma3:1b` | Gemma3 1B (hidden task model) | — | former task model (hidden, kept for rollback) |
| `gemma4:31b` | Gemma4 (hidden) | — | vision / image-QA (hidden) |
| `gemma4:e2b` | Gemma 4 E2B (hidden task model) | — | task model — titles/tags/query, thinking OFF (hidden) |

## Media / task config
```
image_generation.enable = false
image_generation.engine = "comfyui"
image_generation.model = "flux1-dev-fp8.safetensors"
image_generation.size = "1024x1024"
image_generation.steps = 20
images.edit.enable = false
images.edit.engine = "openai"
task.model.default = "gemma4:e2b"
task.model.external = "gemma4:e2b"
task.query.search.enable = true
task.query.retrieval.enable = true
task.title.enable = true
task.tags.enable = true
task.autocomplete.enable = false
ui.default_models = "auto_assistant.auto"
```

> Task model `gemma4:e2b` advanced params: `{"think": false}` — `think:false` is forwarded to ollama so background tasks run non-thinking (fast, clean).


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

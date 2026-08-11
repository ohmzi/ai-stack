# OpenWebUI workspace & media config — snapshot

_Sanitized: model routing + media toggles only. No API keys / secrets / OAuth / LDAP._
_Updated 2026-08-02, re-read from the live DB._
_Audited 2026-08-08: the `function` and `model` tables were re-read read-only, and every correction
found is dated inline beside the text it corrects rather than replacing it._

## Installed Functions

| id | name | type | active | repo source |
|---|---|---|---|---|
| `adaptive_memory` | Adaptive Memory | filter | ✅ | `filters/adaptive_memory.py` |
| `animate_scail` | Animate | pipe | ✅ | `pipes/animate_scail.py` |
| `auto_assistant` | auto_assistant | pipe | ✅ | `pipes/auto_assistant.py` |
| `image_krea` | Krea 2 Image | pipe | ✅ | `pipes/image_krea.py` |
| `photoreal` | Uncensored | pipe | ✅ | `pipes/photoreal.py`. **Renamed from `uncensored` on 2026-08-02** so the id matches its file; every pipe id now does. `function.name` is unchanged because it mirrors the pipe's own `title:` frontmatter, and `deploy_pipe.py` rewrites `meta.manifest` from that frontmatter but never `name` — changing one without the other would drift |
| `task_mode` | Task | filter | ✅ | `filters/task_mode.py` — the **Task** button in the forked frontend's chat input, added 2026-08-03 (commits `1d63966`, `1217eb3`). A toggle filter: `toggle = True` at module level (`filters/task_mode.py:41`) and mirrored onto the instance in `__init__` (`:86`), because OpenWebUI reads it off the instantiated `Filter`. `scripts/deploy_pipe.py:159` refuses to deploy it without that — a filter that loses `toggle` becomes always-on with no control in the UI |
| `flux_image` | flux_image | pipe | — | `pipes/flux_image.py` |

The `function.name` column above is **not** what the picker shows for a manifold pipe. OpenWebUI
0.10.2 takes the name from the pipe's own `pipes()` return (`functions.py:104`), overridden by an
active `model` row (`utils/models.py:152`). So `image_krea` displays as `Image`, not "Krea 2 Image".

## Workspace models

| id | name | active | note |
|---|---|---|---|
| `auto_assistant.auto` | Assistant | ✅ | display name is the workspace override on the `model` row; the pipe's own fallback is still `Assistant`. `meta.filterIds = ['adaptive_memory']` |
| `photoreal.photo` | Photoreal | ✅ | migrated from `uncensored.photo` 2026-08-02; 3 pre-existing chats still reference the old id and will not resolve it |
| `animate_scail.scail` | Animate | ✅ | row added 2026-08-02 purely to carry capabilities — `web_search`, `code_interpreter`, `image_generation`, `terminal`, `builtin_tools`, `file_context` and `citations` all **false**, `defaultFeatureIds: []` |
| `image_krea.krea2` | Image | — | **hidden 2026-08-02 by request** — the function stays active and deployed; only the picker entry is gone |
| `flux_image.flux-dev` | Image | — | inactive, and its function is inactive too — not in the picker |
| `gemma3:1b` | Gemma3 1B (hidden task model) | — | the configured task model, but hidden — see the warning below |
| `gemma4:e2b` | Gemma 4 E2B (hidden task model) | — | `params = {"think": false}`; **not** the task model right now |
| `hermes-genesis:apex-compact` | Hermes Genesis Apex-Compact (hidden) | — | hidden 2026-08-02; the pipes call it over `http://localhost:11434` directly |
| `hermes-genesis:agent` | Hermes Genesis Agent (hidden) | — | hidden 2026-08-02; driven by the hermes runtime on `127.0.0.1:8642` |
| `bge-m3:latest` | bge-m3 embeddings (hidden) | — | hidden 2026-08-02; retrieval reaches it via `rag.embedding_engine = "ollama"` — the switch and its measurements are [UPGRADE_ROADMAP.md](UPGRADE_ROADMAP.md) §1.5. **Corrected 2026-08-08:** this row cited `tests/test_retrieval_quality.py` as verifying that, and it does not. That harness reads stored Chroma chunks and scores them through the Infinity reranker (`BAAI/bge-reranker-v2-m3` on `localhost:7997`) to pick `rag.relevance_threshold`; it calls no embedder and never reads `rag.embedding_engine` — "No LLM is involved, so it is deterministic" (`tests/test_retrieval_quality.py:36`) |

### Decision: the picker is curated down to three entries (2026-08-02)

**Intended state — `Assistant`, `Animate`, `Photoreal`, and nothing else.** Every other row
above is hidden on purpose. If a future change makes `Image`, a `hermes-genesis` tag or `bge-m3`
reappear in the dropdown, that is a regression, not a restoration; `tests/test_deployed.py`
fails when it happens.

> **The last clause was false when written (corrected 2026-08-08).** `tests/test_deployed.py`
> catches the `Image` half only. `picker_name` (`tests/test_deployed.py:123-146`) resolves
> `image_krea.krea2`, and `check_readme:171` compares what it returns against README's
> *(hidden)*, so reactivating that row fails the comparison. The raw Ollama tags are covered by
> **nothing**. The suite enumerates the `function` table (`:193`) and then reads exactly one
> `model` row per installed pipe — `select name, is_active from model where id = ?`, keyed
> `<function_id>.<pipe_id>` (`:138-139`) — so it never enumerates the `model` table at all.
> Setting `is_active = 1` on `hermes-genesis:apex-compact`, `hermes-genesis:agent`,
> `bge-m3:latest`, `gemma3:1b` or `gemma4:e2b` fails no check today; the tag would simply appear
> in the dropdown with a green suite. The reason is the pattern, not the presentation: README's
> Models table *does* carry four of those five tags as rows (`hermes-genesis:apex-compact`,
> `hermes-genesis:agent`, `gemma3:1b`, `bge-m3`; only `gemma4:e2b` is prose-only, in the
> hidden-tags list), but `readme_roster`'s row pattern is `\|\s*` + backticked `([a-z_]+)`, which
> cannot match an id containing `-` or `:`. So being tabled does not help — the regex skips them
> either way. *(Corrected 2026-08-08: this said README lists them "in prose rather than as table
> rows", which is wrong about README and, worse, implies tabling them would fix the gap.)*
>
> The operator symptom this leaves: a picker that has silently grown a fourth or fifth entry, and
> no test failure to point at it — the same class of drift `tests/test_deployed.py` was written for.

Restoring any single one is one row: set `is_active = 1` on it.

## Per-model capabilities

`models.default_metadata` turns every capability on (and `defaultFeatureIds` switches the
image/code/web toggles ON by default), so a pipe with **no `model` row inherits all of them** —
which is why Animate showed web-search and code-interpreter buttons it cannot use. Per-model
values win over the defaults (`utils/models.py:309`, `{**default, **existing}`), so the only way
to take a capability away from a manifold entry is to give it a `model` row.

`animate_scail.scail` **does** have a `model` row — added 2026-08-02 for exactly that reason, to
carry the capability flags (see the Workspace models table above). The name on the row happens to
match the pipe's own `pipes()` name, `Animate` (`pipes/animate_scail.py:34`), so the override is
invisible in the UI; but it is the row, not `pipes()`, that the picker reads.

> **Corrected 2026-08-08.** Until today this paragraph said the opposite — that there was no
> `model` row for `animate_scail.scail` and that its picker name came straight from `pipes()`.
> That contradicted both the Workspace models table above it and the paragraph immediately before
> it, which explains that a `model` row is the only way to take a capability away. No test caught
> the contradiction because both names are the string `Animate`, so nothing observable differs:
> `picker_name` returns the row's name when the row is active (`tests/test_deployed.py:138-145`)
> and would print the same label either way.

**How hiding works, and what it costs.** An *inactive* `model` row is not a no-op — it is the
hide switch. `get_all_models` deletes the entry outright (`utils/models.py:169`), and because the
picker and the dispatcher read the same `app.state.MODELS`, a hidden model is also **uncallable**:
`main.py:1026` raises `Model not found` for any id absent from that map. There is no
"hidden but still dispatchable" state. This does not affect pipe-to-pipe work — pipes talk to
ComfyUI and Ollama directly and never go through the model registry.

**A second way a capability disappears, per turn rather than per model (added 2026-08-03).** The
`task_mode` filter's inlet sets `features["web_search"]`, `features["code_interpreter"]` and
`features["image_generation"]` to `False` on the body it is handed
(`filters/task_mode.py:113-118`), each gated by a valve that defaults to `True`. It is a toggle
filter, so it only runs on turns where the user switched **Task** on — and it has to do this
server-side, because filter inlets run before OpenWebUI consumes `features` and before a web
search fires. So a model row is not the only place a capability can be absent: on a Task turn all
three are off regardless of what the row says, and the inlet also stamps `metadata["task_mode"] =
True` so the pipe can tell "the filter actually ran" from "the client claimed the control was on".

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

**Superseded 2026-08-01 (commit `0f95516`).** The recipe is committed now: changing one of these
means editing `compose/openwebui/run.sh` and re-running it — `docker rm -f open-webui` (`:45`) and
the whole `docker run` (`:47-72`) are both inside the script, and it is idempotent. Nothing has to
be recovered from `docker inspect`. Two things to know before you run it: the image on the last
line is the **local frontend fork** `ai-stack/open-webui:task-mode`, not upstream, and the script
also generates/passes `secret.env` (see below). The parenthetical above stays true — run.sh is a
plain `docker run`, so the container still carries no `com.docker.compose.*` labels.

Confirmed 2026-07-29: `env | grep -E "WEB_LOADER|PLAYWRIGHT"` returned nothing, and
`open_webui.config.WEB_LOADER_ENGINE` was `''` despite `web.loader.engine = "safe_web"` in the DB.

**Fixed 2026-08-01 (commit `0f95516`), because of this file.** `compose/openwebui/run.sh:68` now
passes `-e WEB_LOADER_ENGINE=safe_web`, and the inline comment above it (`:66-67`) cites
`openwebui-config-snapshot.md` as the reason. The env var and the DB row agree from that recreate
onward; the running container was created by that script, since its image
(`ai-stack/open-webui:task-mode`) is built and launched from nowhere else. The env inside the
container was not re-measured for this update — the Docker socket was not reachable from where the
check ran — so the claim rests on the script, not on a fresh `docker exec`. The `PLAYWRIGHT_*`,
`web.loader.timeout`, firecrawl and external-web-loader vars appear nowhere in run.sh, so those
rows are still write-only and still decorative.

### Two more things found while checking

- **`WEBUI_SECRET_KEY` was not set (2026-07-29).** The container logged a hard warning on every
  start; sessions were signed with a key regenerated per boot inside the container's writable
  layer, so every restart logged everyone out. Wave 1.6 listed pinning it.
  **Fixed 2026-08-01 (commit `0f95516`).** `compose/openwebui/run.sh:38-43` writes
  `/volume1/docker/openwebui/secret.env` under `umask 177` if the file is absent —
  `printf 'WEBUI_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"` — and `:54` passes it with
  `--env-file`, so the key now outlives both a restart and a recreate. The file is never
  committed. run.sh's own header (`:15-17`): "One final logout happens when THIS change lands;
  none after."
- **`rag.relevance_threshold` was `0`, which is falsy.** The gate is `if self.r_score:`
  (`retrieval/utils.py:1722`), so a zero threshold does not mean "keep everything above 0" — it
  means the filter never runs. Now `0.05`, chosen by measurement; see
  `tests/test_retrieval_quality.py`.

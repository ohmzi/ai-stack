# OpenWebUI workspace & media config — snapshot

_Sanitized: model routing + media toggles only. No API keys / secrets / OAuth / LDAP._

## Active Functions (pipes)

| id | name | type | active |
|---|---|---|---|
| `animate_scail` | Animate | pipe | ✅ |
| `auto_assistant` | auto_assistant | pipe | ✅ |
| `image_krea` | Krea 2 Image | pipe | ✅ |
| `uncensored` | Uncensored | pipe | ✅ |
| `flux_image` | flux_image | pipe | — |

## Workspace models

| id | name | active |
|---|---|---|
| `auto_assistant.auto` | 🪄 Assistant (auto chat + image + video) | ✅ |
| `dolphin-venice:24b` | Dolphin Venice 24B | ✅ |
| `uncensored.photo` | Photoreal | ✅ |
| `flux_image.flux-dev` | Image | — |
| `gemma3:1b` | Gemma3 1B (hidden task model) | — |
| `gemma4:31b` | Gemma4 (hidden) | — |

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

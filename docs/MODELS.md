# Model roles on the local stack

Single 24 GB GPU. Three (plus one spare) Ollama models, each with a deliberate role so the
big ones never needlessly evict each other.

| Slot | Model | VRAM | Role |
|---|---|---|---|
| **Chat + pipe helpers** | `dolphin-venice:24b` (dense 24B) | ~14 GB | Text chat, and every prompt helper the pipes run — enhance, edit-rewrite, shot-planning, prompt-merge. Uncensored (the Photoreal pipe's enhancer needs a model that won't refuse). |
| **Vision / image-QA** | `gemma4:31b` (dense) | ~19 GB | The only vision model. Used for `_verify_image` (checking every generation against the request) and for chat *about* an image (dolphin is text-only). |
| **Task model** | `gemma4:e2b` (Gemma 4, ~5B / "E2B") | ~1.9 GB | Background tasks: chat titles, tags, web-search/RAG query generation. **Thinking is OFF** (see below). |
| _spare_ | `gemma3:1b` | ~0.8 GB | Former task model. Kept + hidden for instant rollback. |

Only the chat model and the vision model are "big"; they swap as needed around generation
(the pipes free VRAM between an Ollama helper call and a ComfyUI render). The task model is
tiny on purpose so a title/tag generation never evicts the resident chat model.

## Task model: gemma3:1b → gemma4:e2b (2026-07-25)

Upgraded the background-task model. `gemma4:e2b` is a modern multimodal Gemma 4 that runs at
just **1.9 GB** VRAM (the "E2B" selective-activation footprint, despite a 7.2 GB download) and
produces noticeably better titles/tags/queries than the old 1B.

**The catch — and the fix.** `gemma4:e2b` is a *thinking* model, and OpenWebUI's task calls
don't pass `think:false`, so by default it "thought" through every title (~2–5 s, ~1000 chars
of hidden reasoning) — and could return empty output if the token budget was tight.

There is no Modelfile switch for it (`PARAMETER think false` is rejected by Ollama). The
reliable fix is an **OpenWebUI model param**: OpenWebUI 0.10.2 forwards a `think` key straight
to the Ollama request (`utils/payload.py` → `ollama_root_params`). So the fix is:

```
model row `gemma4:e2b` → advanced params → { "think": false }
```

`ModelParams` uses `extra='allow'`, so the key survives, and `generate_title` →
`generate_chat_completion` → `apply_model_params_to_body_ollama` forwards it. Verified:
titles now generate in **~0.04 s** (warm) with **zero** thinking, vs ~1.4 s + 871 thinking
chars before. Best of both — smarter than gemma3:1b, and as fast.

## Considered, not adopted

- **A 30–35B MoE (e.g. Qwen3-30B-A3B) for the *task* slot** — rejected. MoE saves compute, not
  memory: ~18 GB resident would evict the chat model on every title. Wrong slot.
- **A MoE for the *chat/helper* slot (replacing dolphin)** — a good future option: an
  *uncensored* Qwen3-30B-A3B would run the agentic pipe helpers 2–3× faster at similar VRAM.
  Not done (would need an uncensored/abliterated build to keep the no-refusal behavior).
- **gemma4:e2b as the vision-QA model** — it *is* vision-capable and 1.9 GB, so it could make
  QA faster/leaner, but a 2B-effective model is weaker at the strict QA checks (person counts,
  ages, styles). Left on gemma4:31b for accuracy.

## Rollback

Task model back to gemma3:1b: set `task.model.default` / `task.model.external` = `gemma3:1b`
(its hidden workspace row is still present), restart OpenWebUI.

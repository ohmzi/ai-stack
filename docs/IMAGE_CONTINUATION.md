# Image conversation continuity — the 2026-08-02 correction

## The incident

> "create image of a cat jumping off the building that is on fire…" → good image.
> **"make this picture realistic"** → a photo of a father and son.

In a second chat, **"make this picture animated"** on Photoreal returned an unrelated
realistic woman. Reproduced across all three image pipes. The user's read — *"the chat isn't
able to keep the previous picture in context and also changes my ask into something completely
different"* — was correct on both counts, and they turned out to be two different bugs.

## Root causes

All five confirmed from `webui.db` and `media_metrics.jsonl` before anything was changed.

### 1. OpenWebUI's own background tasks were running as GPU renders

After every turn Open WebUI asks the model for a chat title, tags, follow-up suggestions and a
web-search decision. Those prompts begin `### Task:` and were arriving at the media pipes as
ordinary user requests. The task blob embeds the chat history, so `_is_image_request` matched
it, and the pipes rendered — dozens of rows in `media_metrics.jsonl` with
`request='### Task: Suggest 3-5 relevant follow-up questions…'` at 14–174 s each.

The mechanism is worth stating precisely, because the config *looks* correct:
`task.model.default` / `task.model.external` are set to `gemma3:1b`, but
`get_task_model_id` (`utils/task.py:16-27`) only honours that id **if it is present in the
loaded model registry**. `gemma3:1b`'s row is hidden and a raw Ollama tag without an active
row is admin-only in 0.10.2, so the id was not there — and the fallback is *the chat's current
model*, i.e. the pipe.

### 2. The junk renders poisoned the per-chat "last image"

Task calls carry the real `chat_id`, so each junk render overwrote `_recent[chat_id]`. When
the user then said "make this picture realistic", the pipe handed Qwen-Image-Edit the junk
image — one that happened to contain people. **The edit instruction was fine**
(`"Convert this image into a photorealistic photograph…"`); the reference was not.

### 3. Generated images were invisible to the next turn

The pipes return bare markdown. Open WebUI 0.10 stores that in `message.output` and leaves
`content` empty, and every history scan only read *str* content. So the pipes could never
recover their own output from history, and the whole feature rested on an in-memory dict that
is per-pipe, 30-chat LRU, and **wiped by every deploy** — the exact component §2 was poisoning.

### 4. Photoreal had no follow-up edit path at all

Any text-only follow-up became a fresh SDXL text-to-image with the follow-up words as the
whole prompt. `"make this picture animated"` → a render of the phrase "make this picture
animated". Its hardcoded negative prompt also bans `cartoon, anime, drawing, illustration`,
so the request was unsatisfiable by construction.

### 5. Vision-QA could not catch any of it

The edit QA verified the *rewritten instruction* against *only the produced image*. An edit
that swapped the subjects entirely still satisfied its own instruction, so it passed with
`qa_rounds=0`. Two contributing prompt defects:

- the **t2i enhancer** carried a literal cartoon example (`'A vibrant 3D animated
  cartoon-style illustration…'`) that the model copied onto unstyled requests — which is why
  the cat came out as a cartoon nobody asked for, and therefore why "make it realistic" was
  ever typed;
- the **edit rewriter** was text-only and its worked examples were people-heavy
  (`"the young woman on the right"`, ethnicity lists), so on a cat photo it wrote instructions
  about people;
- the **style-conversion** instruction hard-coded *"the same people — same count, ages,
  genders, ethnicity and skin tone"* into every restyle.

## The fix

New shared module **`pipes/shared/media_session.py`**, deployed to
`/app/backend/data/media_session.py` by `scripts/deploy_pipe.py` (`SIDECARS`) and drift-checked
by `tests/test_deployed.py` (`EXTRA_TWINS`). Every pipe imports it under try/except and degrades to
its previous behaviour if the copy is missing.

| Piece | What it does |
|---|---|
| `is_task_request()` / `answer_task()` | Each pipe declares `__task__` in `pipe()` and short-circuits to a plain-text answer on `gemma3:1b`. Open WebUI pops `metadata` before the pipe sees the body, so **the kwarg is the only usable marker** (`functions.py:193-194, 209, 258`); the `### Task:` prefix is the fallback. Titles and tags keep working; nothing renders; no cache is touched. |
| `remember_image()` / `recall_image()` | Persistent per-chat last image under `/app/backend/data/media_recent/` (newest 40 chats). Survives deploys and pipe reloads, and is **shared by all three image pipes**, so switching models mid-chat keeps the picture on the table. |
| `find_recent_image()` | Recovery across every shape 0.10 sends: str content, list-of-parts content, and `message.output` → `output_text`. Strictly newest-message-first, so a freshly generated image beats an older upload (the old order made edits compound off the original upload). |
| `style_conversion()` | Whole-image restyle instructions, **subject-agnostic**: "keep the composition and every subject exactly as they are". Restyles force the non-Lightning tier because a negative prompt is inert at cfg 1. |
| `T2I_ENHANCE_SYS` | Photographic register by default; elaborate only along photographic axes; adopt a style **only when the user names one**; no style examples in the prompt to copy; no booru tags or `(word:1.5)` weights (Krea 2 reads them as literal text). |
| `EDIT_REWRITE_VISION_SYS` | The rewriter now **sees the reference image** and grounds every noun in what is visible. No example subjects. |
| `VERIFY_EDIT_SYS` + `edit_qa_user_prompt()` | Edit QA takes **both images in one call** (original first, result second) and scores *adherence to the user's original ask* **and** *preservation of everything not mentioned*. A result that swapped subjects now fails. |

Per-pipe changes: routing guards (`"make this picture X"` is never a fresh render; a trailing
`?` on an imperative edit and `"have them use chopsticks"` now edit instead of falling to
chat), `_edit_context` strips injected memory/RAG blocks before truncating, `image_krea` no
longer buckets chats under a shared `default` cache key and now writes `media_metrics` rows,
and Photoreal gained the whole follow-up edit path.

Versions: Assistant **0.6.0**, Image **1.4.0**, Photoreal **0.6.0**.

### Where the prompt contracts come from

They are ports, not house style — worth knowing before editing them:

- **`EDIT_REWRITE_VISION_SYS`** ← the official Qwen-Image *Edit Prompt Enhancer*
  (`QwenLM/Qwen-Image`, `src/examples/tools/prompt_utils.py`, `polish_edit_prompt`). Two of its
  design facts drove the change: the official rewriter is a **VLM that sees the image**, and
  its rules are *"keep the core intention of the original instruction unchanged"* and *"all
  added objects or modifications must align with the logic and style of the edited input
  image's overall scene"*.
- **`style_conversion()`** ← the same file's §4 *Style Conversion*: name the style plus a few
  concrete visual features (its own example: `"Disco style"` → *"1970s disco style: flashing
  lights, disco ball, mirrored walls, colorful tones"*). Re-describing the scene is what
  invites invented subjects, so it does not.
- **`VERIFY_EDIT_SYS`** ← the **ImgEdit** benchmark judge (`PKU-YuanGroup/ImgEdit`,
  `Benchmark/Basic/basic_bench.py` + `prompts.json`), which sends `[rubric, original, edited]`
  in one message and scores instruction-following separately from content preservation. Their
  judge is a fine-tuned Qwen2.5-VL-7B that beat GPT-4o-mini on human agreement — direct
  evidence the local Qwen-VL-family tenant is adequate for this job.
- **`T2I_ENHANCE_SYS`** ← civitai guidance for the RedCraft/Krea-2 family: natural language
  (not tags), photographic vocabulary as the safe axis of elaboration, and the style register
  pinned in the *system* prompt so the model has nothing to invent.
- **Sampler settings** ← the RedCraft creator's own line, `ER_SDE/Euler | Simple | CFG=1 |
  8-12 Steps`. The pipes run 8; 10–12 is the sanctioned quality lever. **cfg stays 1.0**,
  where negatives are provably inert.

## The behaviour contract

What a message does when an image is already on the table:

| You say | What happens |
|---|---|
| "make this picture realistic" / "…animated" / "turn it into a watercolor" | Whole-image restyle of that picture, full-tier Qwen-Image-Edit, negative = the style being left behind |
| "remove the hat", "give him a red scarf", "have them use chopsticks" | Ordinary instruction edit of that picture |
| "can you make it brighter?" | Edit — a trailing `?` on an imperative is politeness, not a question |
| "make the ball realistic" | Ordinary edit of *the ball*, not a restyle (no deictic reference to the whole image) |
| "create an image of a dog" / "another one" | Fresh generation, as always |
| "animate this" / "make a video of it" | Wan 2.2 I2V — motion, not a restyle |
| "what's in this picture?" / "thanks!" | Chat. No render. |
| *(no recoverable image)* | Says so, and offers to generate instead of silently rendering the follow-up words |

## Verified

- **`tests/test_continuation.py`** — 49 host-side checks, no GPU: task detection and the
  `pipe()` short-circuit in all three pipes, recovery across all message shapes, the
  persistent store, style-conversion detection and subject-agnostic wording, and the routing
  guards above.
- All pre-existing suites re-run green (`test_media_intent`, `test_router`, `test_autoroute`,
  `test_photoreal_edit`, `test_edit_tiers`, `test_manifold`, `test_confirm_gate`,
  `test_bgtask_intent`, `test_structured_parse`, `test_deployed`).
- **End-to-end on GPU**, reproducing the original scenario: generate the cat scene, then send
  the follow-up from a **fresh pipe instance** (simulating the post-deploy cache wipe) with an
  Open WebUI-shaped history (`content=''`, markdown in `output`). Image and Assistant took
  "make this picture realistic"; Photoreal took "make this picture animated". All three edited
  the actual cat scene — subjects, composition and framing preserved, style changed.

## Monitoring

```bash
grep -c '### Task' /volume1/docker/openwebui/config/media_metrics.jsonl   # must stay 0
python3 tests/test_continuation.py                                        # 49 checks, no GPU
ls /volume1/docker/openwebui/config/media_recent/ | wc -l                 # grows as images are made
```

A non-zero `### Task` count means the guard regressed and background prompts are burning GPU
again. An empty `media_recent/` after images have been generated means the sidecar did not
deploy — `tests/test_deployed.py` catches that directly.

## Known residuals

- **Style conversion can drift a subject's identity.** Observed once: an anime cat converted
  to photorealistic came back slightly dog-like, and QA accepted it. This is a documented
  Qwen-Image-Edit failure mode on large style jumps. Mitigations available but not adopted:
  keeping the same seed across serial edits, or tightening `VERIFY_EDIT_SYS` to name the
  subject's species explicitly. A follow-up ("make the cat look more like a cat") now works,
  which it did not before.
- **The server-side half of the task fix is not done.** Giving `gemma3:1b` an active `model`
  row plus an access grant would let Open WebUI route tasks to it natively. The in-pipe guard
  makes this optional; doing both would be belt and braces.
- **Continuity is tested at the routing layer, not the pixel layer.** See the gap noted in
  `QA_TEST_PLAN.md` §4.

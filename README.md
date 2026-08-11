# ai-stack

A self-hosted [Open WebUI](https://github.com/open-webui/open-webui) deployment
that does more than chat: it routes between local chat/image/video models,
watches things on a schedule and texts you when they happen, and is measured by
a repeatable eval suite instead of vibes.

**The pipes and filters need no fork** — they install from *Workspace → Functions* as database rows
and run on the stock Python backend, which is what keeps them portable. The **frontend** is a
different story: this host runs a local fork, `ai-stack/open-webui:task-mode`, that adds three
mutually-exclusive mode buttons (Internet / Code / Task) to the chat input and a Background-tasks
shortcut to the sidebar. The backend and CUDA layers come straight from a digest-pinned upstream
image and are untouched — the fork adds nothing the backend depends on — so pointing
`compose/openwebui/run.sh` back at that digest costs those two UI affordances and nothing else.
(This paragraph said "there is no fork and no source patch" until 2026-08-08, five days after the
fork landed.)

```
pipes/       Open WebUI Function pipes (the models you pick in the UI)
  live/        gitignored copy of exactly what is deployed — see "Deploying" below
  shared/      sidecar modules copied onto OpenWebUI's data volume
filters/     Open WebUI filters
scripts/     the alerting side — monitors, discovery, delivery, transports
hermes/      hermes-agent plugins, symlinked into ~/.hermes
compose/     the containers. openwebui/ holds the frontend fork above AND run.sh,
             the only recorded recipe for creating the container; comfyui/ likewise;
             docker-compose.yml holds the five support services; public/ is the
             second, no-login instance — see docs/PUBLIC_INSTANCE.md
branding/    the OhmzAI skin
tests/       unit tests, live QA harnesses and the eval suite
docs/        runbooks, setup, model notes, QA plan
```

## Pipes

The Function column is the OpenWebUI function id — what `scripts/deploy_pipe.py` takes and what
the DB rows are keyed on. It matches the repo filename for every pipe, but the two are separate
things and have drifted apart before.

| Function (OpenWebUI id) | Model in the UI | What it does |
|---|---|---|
| `auto_assistant` | Ω Assistant | One entry that routes by intent: chat (with vision when an image is in play), automatic coder routing, a RedCraft image (create, or edit via Qwen-Image-Edit), a Wan 2.2 video (text-to-video, image-to-video, or a multi-shot sequence), standing background jobs through the local hermes agent, a deterministic job-management path answered from `/api/jobs` rather than delegated, and a flight path that answers from parsed slots instead of letting a fare reach the chat model. Vision-QA on stills and video frames, a confirmation step before a video render, and VRAM choreography around every job. |
| `image_krea` | *(hidden)* | RedCraft (Krea 2 base) text-to-image + Qwen-Image-Edit 2509 instruction editing, optional trained LoRA, local prompt-enhance and vision-grounded edit-rewrite, two-round vision-QA correction. Hidden from the picker on 2026-08-02 — the Assistant covers the same two jobs. Restore by setting `image_krea.krea2` active in the `model` table. |
| `photoreal` | Photoreal | Photorealistic text-to-image on its own SDXL checkpoint. Edits — including text-only follow-ups on the previous picture — run through Qwen-Image-Edit via the shared `identity_edit` module so the subject stays the same person, with an SDXL img2img fallback when Qwen is unavailable. |
| `animate_scail` | Animate | SCAIL-2 (Wan 2.1 14B GGUF) motion transfer: attach a full-body character image and name one of three built-in motions — dance, wave or walk. Your text picks the motion; it is not a prompt. ~2 s clip, ~3 min. |
| `flux_image` | *(disabled)* | FLUX.1-dev text-to-image. Kept deployed and byte-current for rollback; not selectable in the UI. |

`auto_assistant`'s "animate this image" is Wan 2.2 **I2V** — text-directed motion out of the
still. That is a different engine from the Animate pipe, which transfers a preset motion and
ignores your wording.

Highlights:

- **Intent routing** with question/small-talk guards — a question *about* an
  image isn't turned into an edit — and default-deny on media intent.
- **Modes you set, not modes it guesses.** Internet / Code / Task are three exclusive buttons in
  the chat input rather than inferences from your wording. Reading intent from phrasing kept failing
  in both directions at once, and each fix made the other direction worse.
- **Conversation continuity** — "make this picture realistic" keeps editing
  *that* picture. A persistent per-chat reference store survives deploys and
  model switches, and OpenWebUI's own background-task prompts can never reach a
  renderer. [docs/IMAGE_CONTINUATION.md](docs/IMAGE_CONTINUATION.md).
- **Wan 2.2 A14B** video including multi-shot sequences with per-shot frame QA.
- **Vision-QA** verification against the request on the Assistant and Image
  pipes, with a corrected retry. Edits are judged on *both* images — original and
  result — against the user's original wording, so a rewrite that drifted off the
  subject fails instead of passing. (Photoreal does not verify — it trades that
  for seed control and per-job metrics.)
- **VRAM choreography** — a generation lock, real VRAM-release polling,
  idle-gated ComfyUI unloads, and recovery from a wedged allocator on OOM.
- **Native status line** — a live elapsed ticker collapsing to
  `Generated in 1m 05s · RedCraft · 1024×1024 · 8 steps`.

## Filters

| Function | What it does |
|---|---|
| `adaptive_memory` | Vendored Adaptive Memory v4.4.1 (`1818TusculumSt/owui-adaptive-memory`) — extracts, dedupes and embeds per-user memories, then prepends them to the last user message. Attached to `Ω Assistant` specifically rather than globally. Provenance and the license caveat: [docs/CAPABILITY_UPGRADE_PLAN.md](docs/CAPABILITY_UPGRADE_PLAN.md). |
| `task_mode` | The **Task** control in the chat input — one of the three mutually-exclusive mode buttons the frontend fork adds (Internet / Code / Task). While it is on, the turn goes to the hermes background-task agent instead of being guessed at from the user's wording, and Internet / Code are stood down server-side for that turn. Off by default. The *filter* is stateless — read per turn, never remembered server-side — but the fork's `Chat.svelte` does remember the choice **per chat**, and deliberately only when the user made it, so an incidental reset cannot silently re-arm a mode. `toggle` must be set on the *instance*, not the module: OpenWebUI reads it off the instantiated Filter, and a module-level-only `toggle` loads fine, passes every static check, and produces no control in the UI. |

### The picker is curated on purpose

**The model dropdown shows exactly three entries — `Ω Assistant`, `Animate`, `Photoreal` — and
that is the intended state, not drift.** Everything else is hidden behind an inactive `model`
row: `image_krea`, `flux_image`, and every raw Ollama tag (`hermes-genesis:apex-compact`,
`hermes-genesis:agent`, `bge-m3`, `gemma3:1b`, `gemma4:e2b`).

The reasoning: the Assistant already routes to image, edit and video, so a picker full of
near-duplicates is a choice the user shouldn't have to make. `Photoreal` and `Animate` stay
because neither is reachable from the Assistant — one is a different checkpoint, the other a
different engine.

Hiding costs nothing here because **nothing reaches those models through OpenWebUI**: the pipes
call Ollama on `http://localhost:11434` themselves and retrieval goes through
`rag.embedding_engine`. Two consequences worth knowing before you change any of it:

- A hidden model is also **uncallable** through OpenWebUI — the picker and the dispatcher read
  the same map. There is no hidden-but-dispatchable state.
- That is why **no task model is in effect** — see
  [openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md). Being the task model and
  being hidden are mutually exclusive.

`Animate` also carries a `model` row for a second reason: a manifold entry with no row inherits
`models.default_metadata`, which switches *every* capability on. Its row turns off web search,
the code interpreter, image generation, terminal and the builtin tools — that pipe reads one
attached image plus a motion keyword and ignores tool output entirely.

`tests/test_deployed.py` pins this: it fails if the tables above stop matching the running
instance.

## Standing jobs and alerts

"Monitor this price for 2 weeks" becomes a real cron job. The agent side is
documented in [docs/HERMES_AGENT.md](docs/HERMES_AGENT.md); delivery lives in
`scripts/`.

The design point worth knowing: **delivery is not left to the model.** Jobs
write `LOG`/`ALERT` lines and a watcher delivers them, so a model that goes
quiet cannot silently drop an alert. Alerts are verified, retried 3× at 5-minute
spacing, and recorded per attempt. Job creation is checked against the
scheduler, not the agent's word. Price checks are arithmetic done in Python, not
asked of a 34B model. Monitors report their own failures — once, after
confirming them.

The corollary, added 2026-08-08: **verified-to-exist is not verified-to-work.** The creation check
caught jobs the agent claimed but never made; it had nothing to say about a job that exists and
cannot function. So the pipe now holds each freshly-created record to the three parts of the agent's
brief a machine can decide — delivery is exactly `local`, the prompt asks for a `LOG:` line, the
prompt carries no leaked tool-call markup — and repairs what is mechanically repairable in one
`PATCH` before the first run. The boundary is deliberate: whether *"every 6 hours, forever"* is the
duration the user meant is a judgement, and a validator that guessed would be the fabrication it
exists to prevent. A repair that changes the text of the job is announced; a failed repair always
speaks. Every defect is counted on the metric row, so how often the model ignores its brief is
measurable instead of anecdotal.

What monitors can watch, and what they refuse to:

- **Price** — extraction ranked by confidence, including Amazon's JS-rendered buy box; a listing page
  offering many prices has no price of its own and says so.
- **Stock / availability** — three extraction tiers over a closed token vocabulary. Unreadable is
  *reported*, never guessed; it fires on state rather than transition, and a five-minute flapper
  texts once.
- **No URL required.** `scripts/price_search.py` finds the page instead of demanding a link — one
  search per monitor lifetime, with cooldown and roster-outage backoff.
- **Flights: deliberately refused.** All 19 candidate fare sites were measured on 2026-08-07 and
  the gate returned **zero** — 11 block automated clients, 6 expose no fetchable URL, 2 are deal
  feeds. So a flight ask does not become a watch that would fire and text nobody. It gets a real
  answer plus a Google Flights deep link **built from the user's own parsed slots**, never a fare
  typed by a model. Dates, airports and thresholds are parsed deterministically; a season
  ("sometime in the fall") or a named holiday is not a date and is asked about rather than guessed.
  [FLIGHT_RECON.md](docs/FLIGHT_RECON.md) has the site-by-site measurements.

Transport is two channels, and both fire by default (`ALERT_CHANNELS` narrows it): **email** over
SMTP, and **SMS** either through a carrier email-to-SMS gateway or through Twilio — `SMS_METHOD`
defaults to the gateway when `SMS_GATEWAY` is set and to Twilio otherwise. Addresses come from where
they already live: email is authoritative in OpenWebUI's user table, while phone numbers exist
nowhere in it and are opt-in per handle, so a user with no phone entry silently gets email only.
Partial success counts as success — if SMS lands and email fails the user *was* alerted, and
returning failure would re-text them on the next tick to fix an email problem.

Three failures that cost real time and are now pinned by tests: texts containing links are silently
dropped by carriers; an alert address whose domain has no MX record fails without saying so; and a
domain with no MX but an A record is flagged `implicit` rather than silently trusted, because RFC
5321 says mail falls back there and it usually still bounces. The resolver check fails **open** — a
missing `dig` must never be what stops an alert.

The email is set in the OhmzAI brand, not a stock white template — same warm-dark palette and one
amber accent as the rest of the stack, `branding/ohmz.css` copied into `scripts/alert_templates.py`
rather than shared by import, since an email cannot load the site's own stylesheet.

**Two more things an alert can now do, added 2026-08-10:**

- **Cancel or pause from the email itself.** The footer link opens `cancel.ohmz.cloud`, a small
  loopback service (`scripts/cancel_service.py`, behind the same Cloudflare tunnel as everything
  else) authorized by a stateless signed token — no login, no session, one job. GET only ever
  renders the page (mail clients prefetch links; a mutating GET would cancel a monitor before
  anyone read the alert); Cancel and Pause/Resume are POST-only. Cancelling does the full cleanup
  the chat path never did: the Hermes job, the ownership record, queued retries, watcher state,
  and a fare watch's FlightClaw route.
- **A confirmation when a monitor is created**, not just when it fires — "cat board watch is now
  being tracked, and I'll alert you under $50.00," by email and text, through the same template
  and the same cancel link. The container that creates a job holds no SMTP/Twilio credentials, so
  the pipe leaves a note in a file the host-side delivery watcher already polls every 60s and picks
  up from there.

Both are documented in [docs/HERMES_AGENT.md](docs/HERMES_AGENT.md), including two concurrency bugs
the confirmation feature surfaced live during its own development, not in review: the delivery
tick's "nothing new" early return ran *before* the code that would have drained a fresh
confirmation, silently starving it forever, and `.alerts.json` had no lock across processes at all
— an assumption a manual test run and the 60-second systemd timer overlapping in the same minute
disproved outright. Both are fixed and pinned by tests now, not hypothetically.

## Models

One 24 GB RTX 3090, and as of the 2026-07-26 consolidation almost everything
runs on a single tenant:

| Slot | Model | Role |
|---|---|---|
| Everything | `hermes-genesis:apex-compact` | Chat, code, vision, prompt helpers |
| Background agent | `hermes-genesis:agent` | What cron jobs run on. Same weights, `num_ctx 65536` |
| Task model | `gemma3:1b` | Titles, tags, RAG queries — also what the pipes answer task prompts with directly |
| Router | `gemma3:1b` | Chat-vs-code classifier inside the pipe |
| Embeddings | `bge-m3` | RAG, 1024-dim / 8192-token |

The agent tag is the one that costs something. Ollama keys runners by model **plus options**, so a
65536-context tag of the same weights is a *second* ~17 GB runner, and two of those do not fit on a
24 GB card. A job firing mid-conversation evicts the chat tenant and the user's next turn pays a cold
reload — **measured at 22.7 s**. Hence the GPU guard, and hence the pipe releasing the chat tenant
before it hands off rather than letting Ollama evict under memory pressure mid-load.

None of these appear in the model picker. Every one has an inactive `model` row, which is
OpenWebUI's hide switch — the picker is curated down to the three pipes above. That is safe
here because the pipes reach Ollama over `http://localhost:11434` directly and retrieval goes
through `rag.embedding_engine`, so neither path consults the model registry. It is *not* free
in general: hiding a model also makes it uncallable through OpenWebUI
([openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md) has the mechanism, and the
task-model consequence).

Image generation is **RedCraft** (`redcraft23INT8INT4FP8_30Krea2.safetensors`, a Krea 2
base) at the creator's own settings — 8 steps, cfg 1.0, `er_sde`/simple. Krea 2 Turbo is
still on disk beside it; reverting is a one-line change in both image pipes.

Every VRAM number in [docs/MODELS.md](docs/MODELS.md) is a measured `nvidia-smi`
delta. **Do not budget from `/api/ps`** — it omits the multimodal projector and
the CUDA context, and understated real usage by up to 2.8 GB. Several figures in
older docs were wrong because of exactly that.

## Support services

`compose/docker-compose.yml` — five containers: Tika (document extraction), **two** SearXNG
instances, Kokoro (TTS), and an Infinity cross-encoder reranker. Port choices are deliberate
and explained inline; the upstream defaults collide with other services on this
host.

**The two SearXNG instances are the point, not duplication.** `searxng` on `:8888` serves chat;
`searxng-hermes` on `:8889` serves the background monitors and nothing else
(`compose/searxng-hermes/settings.yml`, 1 uwsgi worker against chat's 4, ~130 MB RSS, roughly one
request per ten minutes), and `scripts/web_search.py` talks only to `:8889`.

The split buys **partial** insulation, and the limit is worth stating precisely because three places
in this repo overstated it until 2026-08-08. Engine rate limits are per **source IP**, and both
containers egress from the same host — so splitting the containers does *not* split the budget for an
engine both rosters enable. What actually insulates chat is the roster difference: chat runs
`duckduckgo, bing, mojeek, wikipedia, wikidata`, hermes runs `google, brave, mojeek, bing`, so
`google` and `brave` are hermes-only and `duckduckgo` is chat-only. **`bing` and `mojeek` are shared**,
and a monitor can still CAPTCHA those for chat. The older claim — that a monitor "can never CAPTCHA an
engine chat depends on" — is false for exactly those two.

That matters because a degraded chat search fails *silently*: the model answers from training data and
still looks grounded. `tests/test_web_search.py` pins *chat's* `compose/searxng/settings.yml` by
sha256, so the separation cannot be dissolved by quietly editing the other side.

The reranker is not optional polish: with hybrid search on and no reranker, Open
WebUI re-embeds the fused candidates and re-sorts by plain cosine, discarding the
BM25 half at the final ranking step.

```bash
docker compose -f compose/docker-compose.yml up -d
```

## Branding

[`branding/`](branding/) holds the OhmzAI skin — warm-dark shell, one amber
accent, Ω mark — applied to the running container:

```bash
./branding/apply.sh
```

It re-themes through Tailwind v4 token overrides rather than a selector chase, so
it survives upstream class-name churn. The served static directory lives inside
the image, so re-run it after any `docker rm` or image pull. Details and the
`WEBUI_NAME` caveat: [branding/README.md](branding/README.md).

`apply.sh` also runs `branding/i18n_brand.py`, which is what stops the UI calling itself "WebUI".
Those strings are i18n keys, and `loader.js` cannot reach them — it rebrands by rewriting
`/api/config` at the fetch boundary, but i18next loads its resources with a dynamic `import()`, which
never passes through `window.fetch`. So instead of patching source, it gives the `en-US` keys
non-empty values (they ship as `""`, which is why the *key* is what renders), leaving every other
locale untouched. `--check` reports without changing anything; `--revert` restores stock wording.

## Public instance

`ai.ohmz.cloud`'s sign-in page carries a "Continue without an account" link to
`aipublic.ohmz.cloud` — a **second, disposable OpenWebUI container**, same skin, chat only, one
model, no pipes deployed to it at all, on a network with no route to ComfyUI, the hermes gateway or
qdrant. Every visitor gets a throwaway identity via trusted-header auth, injected by an nginx gate
that a request cannot forge its way past. "No image or video generation" there is a fact about the
network, not a setting someone could flip back.

```bash
./compose/public/up.sh                          # (re)create the stack
python3 scripts/purge_public_guests.py --yes     # reap idle guest accounts — load-bearing, not optional
python3 tests/test_public_instance.py            # isolation matrix, guest flow, branding parity
```

Full architecture, the trusted-header mechanism, bootstrap order (order matters — the first identity
to reach a fresh database becomes admin) and the redirect-scheme bug that behind Cloudflare is worth
reading before touching the gate config: [docs/PUBLIC_INSTANCE.md](docs/PUBLIC_INSTANCE.md).

## Deploying

**Editing a pipe does not deploy it.** Open WebUI does not import pipes from disk — it stores each
Function's source as a row in its own SQLite database, so a committed file with a clean `git status`
and a green test run can all be true while the server goes on serving the previous build. That
happened on 2026-07-28, and again on 2026-08-08 in a way the drift check itself missed.

```bash
python3 scripts/deploy_pipe.py auto_assistant --dry-run   # what would change
python3 scripts/deploy_pipe.py auto_assistant             # write it
python3 scripts/deploy_pipe.py auto_assistant --rollback  # restore the last backup
python3 scripts/deploy_pipe.py --all --dry-run            # audit every mapped pipe
python3 tests/test_deployed.py                            # prove it landed
```

It preflights the file inside the container using Open WebUI's *own* `extract_frontmatter` and
`replace_imports`, so a pipe that cannot import is rejected instead of stored, and refuses outright
to deploy a file whose prose contains a literal `from utils`/`from apps`/`from main`/`from config` —
`replace_imports` is a naive whole-file `str.replace` that would silently rewrite the comment and
show up later as unexplained drift. It backs up the previous row to `.deploy-backups/` first, then
syncs `pipes/live/`. No restart needed; the row is re-read per request.

`tests/test_deployed.py` checks **both links** — tracked `pipes/*.py` → `pipes/live/` → the `webui.db`
row. Only the second link was checked for four of the five pipes until 2026-08-08, which is how
`auto_assistant` ran 696 lines ahead of what was deployed while the suite reported ALL PASS.

## Tests and evals

Every suite is a standalone program, not a pytest module. **`pytest` cannot run any of them** —
each file ends in `sys.exit(main())` at module scope, so `python3 -m pytest tests/` dies during
collection ("no tests collected, 1 error") and reports zero problems and zero tests
indistinguishably. That command was in this README until 2026-08-08 and never worked.

```bash
python3 tests/test_manage_path.py                  # one suite
for t in tests/test_*.py; do python3 "$t"; done    # all of them
```

**2443 checks across 36 offline suites that print a count**, measured 2026-08-10 against the tracked
sources, plus `test_manifold.py` and `test_router.py`, which pass without printing a count — 38 of
40 suites in total. All 38 are green, `test_deployed.py` included: it goes red exactly when the
pipe was edited after the last deploy, which is the check's job, not a standing exception — see
**Deploying**.

The other two need a live service and **fail rather than skip** without one, so a red run is not
automatically a regression: `test_identity_drift.py` (ComfyUI + a free GPU) was red this run for
exactly that reason, and `test_contention.py`'s live GPU-contention case is opt-in (`--live`) and
was not run. Three more suites — `test_websearch.py` (SearXNG), `test_alert_transports.py`'s
resolver checks (`dig`), and `test_retrieval_quality.py` (a running OpenWebUI) — carry the same
live dependency but happened to be green this run, which is a property of what was reachable on
this box that day, not a guarantee. [QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md) has the full list.

**Nineteen of the forty suites take a pipe path and default to the gitignored `pipes/live/`
copy.** When that copy is stale they test the *deployed* code, not what you just wrote, and say
nothing about which one they read. Give them the tracked source explicitly:

```bash
python3 tests/test_job_shape.py pipes/auto_assistant.py
```

Both directions of the trap have now been observed on the same day. A suite for undeployed code
**failed** against the stale copy (`test_hermes_delegation.py`, bare, reports 1 failure of 70 that
the tracked source does not), and before that the same staleness let suites **pass** against code
696 lines behind the repo. A green run and a red run can both be reporting on the wrong file.

`tests/` is 40 suites covering routing and the manage path, media intent and the confirm gate, GPU
diagnosis and lock admission, alert setup/templating/transports/delivery, the cancel-link token and
service, the subscription-confirmation path on both creation routes, price and stock/availability
watching, no-URL price discovery, the background-monitor search layer, flight intent and the two
flight scripts, job-shape enforcement, the Task filter, task ownership, branding, and the deploy
chain. Beyond unit tests:

- `tests/eval/` — a repeatable evaluation suite with objective graders and a
  checked-in baseline. The judge honours `cases.json`; self-grading was removed.
- `tests/qa_live.py`, `tests/media_metrics.py` — live orchestration QA and media
  measurement.
- `tests/test_continuation.py` — the follow-up-after-an-image contract: task
  detection, reference recovery across every message shape Open WebUI sends, the
  persistent store, and the routing guards in all three image pipes. No GPU.
- `tests/bench_models.py` — head-to-head model benchmarking.
- `tests/test_deployed.py` — fails when the running Open WebUI instance has code
  that differs from this repo, including the shared sidecar modules.

Methodology and the current baseline: [docs/QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md).

## Requirements

- Open WebUI — **not the official image directly**: `ai-stack/open-webui:task-mode`, built from
  [`compose/openwebui/fork/`](compose/openwebui/fork/) (a frontend-only fork of a digest-pinned
  upstream image) and created by `compose/openwebui/run.sh`. With Ollama (`localhost:11434`) and
  ComfyUI (`localhost:8188`) reachable.

  ```bash
  docker build -t ai-stack/open-webui:task-mode compose/openwebui/fork/
  compose/openwebui/run.sh          # idempotent; safe to re-run after an image pull
  ./branding/apply.sh               # both static dirs live inside the image, so re-run after any rebuild
  ```
- ComfyUI with the referenced checkpoints/GGUFs (RedCraft on a Krea 2 base,
  Wan 2.2 A14B, Qwen-Image-Edit 2509, SCAIL-2, and the SDXL checkpoint named in
  `pipes/photoreal.py`).

## Docs

| | |
|---|---|
| [STACK_SETUP.md](docs/STACK_SETUP.md) | How the stack is put together |
| [PUBLIC_INSTANCE.md](docs/PUBLIC_INSTANCE.md) | The no-login public instance: gate mechanism, isolation, bootstrap order |
| [HERMES_AGENT.md](docs/HERMES_AGENT.md) | Standing jobs: what runs, why, how to undo it |
| [MODELS.md](docs/MODELS.md) | Model roles, measured VRAM, the consolidation |
| [QA_TEST_PLAN.md](docs/QA_TEST_PLAN.md) | Methodology and baselines |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Failures seen in practice |
| [IMAGE_CONTINUATION.md](docs/IMAGE_CONTINUATION.md) | Why a follow-up edits *that* picture: the task guard, the reference store, the prompt contracts |
| [openwebui-config-snapshot.md](docs/openwebui-config-snapshot.md) | Sanitized workspace config, and which config rows the runtime actually reads |
| [KREA_LORA_GUIDE.md](docs/KREA_LORA_GUIDE.md) · [SCAIL_ANIMATE.md](docs/SCAIL_ANIMATE.md) | Media pipe guides |
| [BACKUPS.md](docs/BACKUPS.md) | Nightly off-disk backups, and the watchdog that alerts on transitions |
| [TRACKING_ENHANCEMENT.md](docs/TRACKING_ENHANCEMENT.md) | What the monitor work measured — including what it refuted |
| [FLIGHT_RECON.md](docs/FLIGHT_RECON.md) | The 19-site fare recon: a negative result, site by site |
| [ROUTING_ROADMAP.md](docs/ROUTING_ROADMAP.md) · [MANAGE_PATH_PLAN.md](docs/MANAGE_PATH_PLAN.md) | How a turn is routed, and the deterministic job-management path |
| [branding/README.md](branding/README.md) · [compose/openwebui/fork/gen/README.md](compose/openwebui/fork/gen/README.md) | The skin, and how to regenerate the frontend patch |
| [UPGRADE_ROADMAP.md](docs/UPGRADE_ROADMAP.md) · [CAPABILITY_UPGRADE_PLAN.md](docs/CAPABILITY_UPGRADE_PLAN.md) · [VIDEO_QUALITY_ROADMAP.md](docs/VIDEO_QUALITY_ROADMAP.md) · [FLIGHT_WATCH_PLAN.md](docs/FLIGHT_WATCH_PLAN.md) · [openwebui-improvement-plan.md](docs/openwebui-improvement-plan.md) | Where this is going |

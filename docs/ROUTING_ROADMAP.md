# Routing roadmap — deterministic hermes involvement without `/task`

Why this exists. Every routing tier in `pipes/auto_assistant.py` that guessed has needed measuring
and walking back — media intent, coder routing, and now background-task detection. The repo's own
policy ("earn a heuristic with data first", `auto_assistant.py` bg block) means the path to
auto-detecting agent work runs through observability, not through more regex courage. This file is
the plan of record; the full researched design with sources lives in the "Deterministic Hermes
Routing" artifact (claude.ai/code/artifact/052fc9ed-535d-49f4-ba08-6f30ff1b9b0e).

Governing constraint, priced into every decision: a false hermes delegation evicts the chat tenant
(~22.7 s measured reload, `HERMES_AGENT.md`) and can hold an SSE stream for up to 300 s
(`HERMES_TIMEOUT_S`). A false negative is a monitor the user believes exists and does not.

## Decisions

* **Explicit deterministic entries first** — a toggle-filter "Agent mode" chip (OWUI
  `self.toggle = True`, confirmed supported by the installed 0.10.2 backend), a second manifold
  entry routed straight into `_hermes_stream`, and an "escalate to agent" action button. None of
  them touch the regexes; all of them are total-determinism paths.
* **Manage turns go deterministic** — "list/pause/cancel my tasks" answered by the pipe directly
  from hermes's `/api/jobs` CRUD. No LLM in the path, no eviction (vs ~23 s today for a "list").
* **Auto-detection becomes a cascade** mirroring the coder tiers: STRONG regex → bge-m3 embedding
  router (exemplar cosine, margin thresholds; ~0.62 GiB occasional cold load under
  `OLLAMA_KEEP_ALIVE=60s`, eviction-free) → gemma3:1b enum classifier via Ollama's native
  `format` JSON-schema grammar at temperature 0 (NOT OpenAI `response_format` — ignored for the
  gemma family, ollama#10001; a startup self-test is mandatory, the silent-format-ignored class is
  real, ollama#15260). Promoted only on shadow-mode data with a minimum-n gate; always behind the
  confirm gate.
* **Rejected**: exposing hermes as an OWUI tool/MCP server (the model cannot be forced to call a
  tool — fails the determinism requirement) and a direct OpenAI connection to `:8642/v1` (bypasses
  scheduler verification, gpuguard awareness, and the `__task__` guard).

## Status

### Done — 2026-08-02 (steps 1–2)

Six detection defects fixed in `pipes/auto_assistant.py`, all pinned by tests:

1. `_BG_MANAGE` now runs before the `_BG_QUESTION` deny-list — "what are my scheduled tasks?"
   reaches hermes instead of a hallucinated chat answer. The reorder is safe only because the
   list/what-are arm now requires a possessive or task qualifier (see review findings below).
2. `_BG_SLASH` accepts `/tasks`.
3. `/research` and `/agent` are word-bounded via `_BG_ONESHOT` — `/agenda review monday` no longer
   ships "a review monday" to hermes.
4. `_BG_MANAGE`'s noun side is anchored — "stop tracking me" and "cancel my job application(s)/
   apps" stay in chat; "stop tracking the gpu price" still manages.
5. `_hermes_stream` yields `_BG_MARK` on the timeout and dead-gateway paths, so "yes, retry"
   keeps task continuity exactly when it matters most.
6. `_pending_phone_request` scans the last TWO assistant turns (people answer questions out of
   order), stopping at any bg-task reply so a consumed request is never resurrected.

Instrumentation (no behavior change): `job:"route"` rows at every `pipe()` branch (route, tier,
rule id, request truncated to 200 chars), `job:"confirm"` outcomes in `_confirm_render` including
counted fail-opens, `job:"classifier"` verdict+latency in `_classify_code`, `job:"hermes"`
verification-class rows via try/finally. Reader: `tests/route_metrics.py`, which also enforces two
standing invariants (a `task_guard` row is always tier 0, and no route row's request may contain
`### Task`) and exits 1 when either breaks.

An adversarial review of the first cut caught three regressions before commit — a trivia hole
opened by the manage/question reorder ("what are the biggest jobs in tech?" would have delegated
consent-free), phone-marker resurrection ("no thanks" one turn after scheduling re-submitted the
job), and clipped plurals slipping the 'job' blocklist ("cancel my job apps"). All three are fixed
and pinned. That is the roadmap's thesis in miniature: heuristic edits ship with measurement and
review, or they ship regressions.

### Done — 2026-08-02 (step 3)

Deterministic job management, per `MANAGE_PATH_PLAN.md`. Listing, pausing, resuming and cancelling
are answered from hermes's REST API with no model in the path and no chat-tenant eviction — a
"list my tasks" turn went from ~22.7 s to one local HTTP call. Nine natural phrasings that used to
reach the chat model (which invented a task list) now route deterministically; the listing regex
measures 16/16 recall against its targets and 0/42 against an ordinary-chat corpus.

Reference-based management resolves "cancel the RTX one" / "pause the second one" through an
ordered, deterministic ladder (exact id → parked ordinal → id prefix → name → name+prompt → token
overlap → bare), where the first stage producing a candidate decides and ties never break by
margin. Bulk ("cancel everything") and exclusion ("all except the rtx one") are guarded and can
never resolve to a single job. Disambiguation uses letters so a number never means two things in
one conversation.

Deleting is irreversible (hermes rmtree's the job's output), so cancel is a two-turn marker gate
that **fails closed**: no single message can delete a job, a bare "ok"/"sure" is not a
confirmation, and a job whose name or schedule changed between the question and the answer is not
deleted. `_confirm_render` is deliberately not used for mutation — it fails open with no client.
Admin-only while hermes has no per-job owner; non-admins fall through to today's agent path.
Kill switch: `MANAGE_DETERMINISTIC`, which makes the broadened vocabulary inert in the same edit.

### Next
4. **Explicit entries** — agent manifold entry (+ its 0.10.2 `access_grant` row and an
   authorization decision: admin-only vs owner-tagged jobs), `filters/agent_toggle.py`, action
   button; marker strings pinned by coupling tests.
5. **Shadow tier 2** — versioned exemplar file, embed-at-init on bge-m3, cosine scorer logging
   into the route rows on every turn without acting; replay the historical metrics corpus; fit
   thresholds; promotion needs ≥50 in-scope shadow decisions, and demotion triggers are defined
   symmetrically before go-live.
6. **Cascade go-live** — tiered decision replaces `_is_bg_task_request` behind the confirm gate
   and kill-switch valves (`ROUTE_TIER2_LIVE` etc. on the Valves class); the confirm gate fails
   CLOSED for heuristic tiers with no client; `tests/test_route_cascade.py` and eval cases land
   first. Also split the `attached_img`/`ref` gate so one image in history stops killing bg and
   coder routing for the whole chat.
7. **Session-id continuity** — hermes `/api/sessions` id in the marker replaces the 1200-char
   transcript replay; `POST /v1/runs` for one-shots that outlive the SSE window.

Hardening that rides along (from the design's completeness review): hermes-side per-job tool
profiles for auto-routed dispatches (injection surface), a dry-run/validate step so the confirm
prompt shows the parsed schedule (verify correctness, not just existence), job-lifecycle governance
(caps, expiry, auto-pause on failures, weekly digest), and route-log rotation/retention.

## Reading the numbers

```
python3 tests/route_metrics.py          # per-route/tier/rule counts, decline rate, invariants
python3 tests/media_metrics.py          # the media rows share the same file
```

The confirm decline rate IS the live false-positive rate of whatever fired the gate. The
classifier failure count is the only place a silently-degrading-to-chat classifier shows up.
Any `hermes` row with outcome `failed` or `finished_job` is a turn where the agent's story and
the scheduler disagreed.

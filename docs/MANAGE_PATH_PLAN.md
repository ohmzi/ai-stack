# Step 3 — deterministic manage path, broadened listing, reference-based cancel

Plan of record for roadmap step 3. Designed and adversarially verified 2026-08-02 (6 agents; 18
load-bearing claims checked against the live gateway source, a read-only `GET /api/jobs`, and
executed regex tests; 6 refutations folded in below, 3 of them blocking).

**Governing rule, which decides every open question:** the pipe must never make a claim about the
scheduler it did not read from `/api/jobs`, and must never mutate the scheduler on a signal weaker
than an exact id.

## Why

Today every manage turn ("list my tasks", "cancel the price monitor") delegates to the agent: a
~22.7 s chat-tenant eviction plus an agent run to answer a question the REST API answers in
milliseconds. Worse, nine natural phrasings ("what are you tracking for me?", "do i have any
monitors running?") match no rule at all and reach the chat model, which **invents a task list**.
Answering deterministically removes the eviction, removes the hallucination, and drops the cost of
a listing false positive to a fast local table render — which is what makes broadening the
vocabulary safe.

## Verified environment facts

- `GET /api/jobs?include_disabled=true` → `{"jobs":[…]}`. Fields confirmed live: `id`, `name`,
  `schedule_display`, `state`, `enabled`, `next_run_at`, `last_run_at`, `last_status`,
  `last_error`, `repeat.times`/`.completed`, `latest_execution.status`.
- `latest_execution` is populated by `list_jobs` **only** — it is absent from `GET /api/jobs/{id}`.
  It is the only way to show an in-flight run, since `state` has no `"running"` value.
- `state ∈ {scheduled, paused, completed, error}`; `error` keeps `enabled=True`. One-shots end as
  `enabled=False, state=completed`, so `include_disabled=true` is required to see them.
- **DELETE is irreversible** — it `shutil.rmtree`s the job's output directory. Jobs also vanish
  with no tombstone when a repeat budget is exhausted, so a parked id can disappear between turns.
- The HTTP layer validates ids with `re.compile(r"[a-f0-9]{12}").fullmatch` → a prefix is not a
  usable API token.
- Error envelopes differ by route: flat `{"error": "str"}` on job routes, nested
  `{"error":{"message":…}}` on auth. Both must be handled.
- Markers survive into history: `_scrub` never touches HTML comments, and `omsgs` carries
  assistant content verbatim — the same mechanism `_pending_phone_request` already relies on.

## Corrections applied to the first draft (blocking)

1. **Authorization was dead on arrival.** The draft defaulted `TASK_ADMINS={"ohmz"}`, but
   `_alert_username` derives the handle from the *email local part* — the real handle is
   `omariqbal97`. With the draft's default, the only user fails the check and the whole path
   silently degrades to the 23 s delegation it exists to remove. The draft also called
   `__user__["role"]` unverified; it is verified present and equals `admin`. **Fix: role is the
   primary signal, `TASK_ADMINS` is a supplementary allow-list, and `contacts.json` already shows
   this handle drift has bitten before.**
2. **Branch-1 wiring contradicted itself** across three sections. Composed as written, *every*
   turn following a rendered table would re-render the table — including the plan's own declared
   non-goal ("what does the second one check?"). **Fix: branch 1 fires only on
   `(pending_confirm) or (parked and _manage_op(text))`, never on a bare parked list.**
3. **An edit instruction would have deleted live code** — the "unreachable duplicate
   `except Exception`" in `_hermes_jobs` does not exist. **Fix: dropped.** Relatedly, every line
   anchor in the hermes region drifts 4–6 lines and must be re-derived at edit time, not trusted.

Non-blocking corrections: the 6-hex prefix minimum stands but its stated rationale was wrong (real
hex-only dictionary words exist: `decade`, `facade`, `beefed` — consequence is benign);
`fromisoformat` handles these offsets on ≥3.7 so the try/except stays for hand-edited records, not
for the claimed version reason; `test_deployed.py` does not link repo→live for this pipe, so
deployment must go through `scripts/deploy_pipe.py`, never `cp`.

## Design

### Intent taxonomy

| intent | path | confirmation |
|---|---|---|
| list | deterministic | — |
| pause / resume | deterministic, one turn | none — reversible and idempotent |
| cancel / delete | deterministic, **two turns** | pipe-rendered marker gate |
| run now | **agent** (deferred) | `trigger_job` re-arms then deletes one-shots — needs judgement |
| create, reschedule/edit | agent (unchanged) | existing confirm gate |
| batch ("cancel all") | agent | no partial-success semantics wanted in v1 |

Structural rule: **the deterministic path returns `None` to mean "I could not answer this"**, and
the existing agent delegation stays the sole fallback (unresolved reference, gateway down,
non-admin). Safe degradation to today's behaviour, never a dead end.

### The list render

Ordinal **and** full 12-hex id are both rendered — the ordinal because that is what people say
("the second one"), the full id because a prefix is not an API token and a copy-paste must work.
Rows stay in API order with no sorting: sorting by state would reorder the list between two renders
the user is comparing, which is the one thing ordinals cannot survive. State is carried by a glyph
(`▶` active · `🔄` running now · `⏸` paused · `✓` finished · `⚠️` scheduling error) chosen from
`latest_execution.status` first, then `state`.

Every cell passes through `_md_cell`, which collapses whitespace, escapes `|`, strips backticks
**and neutralises `<!--`/`-->`** — a job name is attacker-influenced text and must not be able to
forge a marker. `last_error` is multi-line and renders below the table, never in a cell.

The empty list and the API-unreachable case must never look alike: *"That is not the same as having
no tasks"* is stated explicitly in every error string.

### Reference resolution

`_resolve_ref(text, jobs, parked)` → `{status, job, candidates, strategy, needle}` with
`status ∈ {one, many, none, need_list, gone, out_of_range, bad_id}`. Ordered strategies, **first
stage producing ≥1 candidate decides** — never fall through from `many` to a weaker signal:

| | strategy | notes |
|---|---|---|
| R0 | exact 12-hex id | a supplied id that does not exist → `bad_id`, never fall through |
| R1 | ordinal against the **parked** list | no parked list → `need_list`; parked id absent from the fresh fetch → `gone` |
| R2 | id prefix ≥6 hex | |
| R3 | name match, **word-boundary** (contiguous phrase, then all-tokens) | |
| R4 | name + prompt match | |
| R5 | token-overlap score | ties → `many`; never pick by score margin |
| R6 | bare reference ("cancel it") | resolves only when exactly one job exists |

Stopwords include `such`, so *"cancel such and such tracking"* reduces to zero tokens and lands on
R6 — the semantically correct reading of a placeholder. Quantifiers (`all`, `every`, `each`, `both`,
`the rest`) and negations (`not`, `except`, `other than`, `besides`) **force `many`** and never
resolve — bulk and exclusion are exactly where a wrong guess is unrecoverable.

Ambiguity renders the candidates and asks. It never guesses, never acts. Disambiguation uses a
**disjoint handle space (a/b/c)** so a number can never mean two different things in one
conversation.

### Cross-turn state

Two base64 HTML-comment markers, following the established `_PHONE_MARK_RE` convention:

- `<!--bg-jobs:…-->` — `{v, t, ids[], ns[]}` in rendered order. Ordinal *n* = `ids[n-1]`, never
  recomputed. Read from the last two assistant turns; newest wins; unknown `v` or age > 24 h → ignored.
- `<!--bg-confirm:…-->` — the armed op `{v, t, stage, op, id, n, s}`. Read from the **last assistant
  turn only** — an intervening turn means the user moved on, and a stray "yes" must not arm a delete.
  Age > 600 s → reported as expired, not silently ignored.

**Staleness is not handled by the clock.** Every resolution runs against a *fresh* `GET /api/jobs`;
the marker only maps ordinal → id. A parked id missing from the fresh snapshot yields "that one is
already gone" with no API call.

### Destructive-op safety

**`_confirm_render` is deliberately NOT used for job mutation.** It fails open on no-client, on
exception, and on any non-`False` answer — correct for "don't waste the GPU", wrong for "don't
destroy state". The two-turn marker gate **fails closed by construction**: turn N renders the
confirmation and performs no write; the DELETE happens only when turn N+1 carries an explicit
affirmative *and* the last assistant turn carries a fresh `stage:"confirm"` marker *and* the id is
still present *and* the record's name+schedule still match what was confirmed (guarding against the
job changing under us). There is no code path where one message deletes a job.

Affirmatives are split by reversibility: a delete needs `yes|yeah|yep|do it|delete it|confirm` —
bare acknowledgements (`ok`, `sure`, `go ahead`) are **not** sufficient for an irreversible op and
trigger a one-line re-ask. The permissive set stays for the reversible pause downgrade. Because
`stop` is genuinely ambiguous between "switch off" and "destroy", its confirmation offers **pause**
as the first alternative.

The success message carries a compact recreate block (name / schedule / prompt), since there is no
undo and the record is otherwise gone forever.

### Authorization — admin-only, deliberately not broadened

One shared bearer key grants full CRUD over every job on the host; jobs have **no owner field** and
`origin.user_id` is `None` for everything the pipe creates. Owner scoping is impossible today
without a hermes-side change. So: `_may_manage` gates on `__user__["role"] == "admin"` (verified
present and correct for this user) plus a supplementary `TASK_ADMINS` env allow-list. Non-admins
skip the deterministic block entirely and get exactly today's behaviour — this change neither
improves nor worsens their exposure. **The broadened vocabulary is gated on the same predicate**,
so it widens *how an admin can phrase things*, never *who can enumerate jobs*.

### Kill switch

`MANAGE_DETERMINISTIC = True` — one module constant gating both the deterministic path **and** the
broadened vocabulary in the same expression, so disabling (A) makes (B) inert in one edit. Rollback
is: flip the constant (markers drain naturally), redeploy, then revert if needed.

## Additional hardening folded in from the completeness pass

- **Hoist above the `attached_img` gate.** As drafted the whole feature would be dark in any chat
  that ever rendered an image. A job table is text-only and cannot be confused with a render.
- **Thread `is_list` everywhere `is_manage` appears** — the phone gate, the confirm gate, and
  `verify_creation` — or a declined list turn falls into the *creation* machinery.
- **All HTTP in `asyncio.to_thread`**, one jobs fetch per turn, branch 1 short-circuits branch 2.
- **Exactly one route row per turn**, including a `task.manage.declined` row with a reason when the
  deterministic path bows out; `ms` on every list/manage row.
- **Drop `restart` from the manage verbs** so run-now falls to the agent as intended rather than
  being silently absorbed by resume; pre-check resume against `state == "completed"`.
- Relative times render only when the parsed value is tz-aware and the delta is sane; otherwise the
  absolute timestamp. The `🔄` glyph is bounded on `latest_execution` freshness.
- `fold()` NFKD-normalises rather than erasing non-ASCII; every id is validated against
  `^[a-f0-9]{12}$` before it reaches a URL.

## Implementation order

1. Constants: `TASK_ADMINS`, `MANAGE_DETERMINISTIC`, `JOBS_MAX=25`, `CONFIRM_TTL_S=600`,
   `PARK_TTL_S=86400`.
2. REST layer beside `_hermes_key`: `_hermes_api(method, path, body, timeout)` → `(status, data,
   err)`; `_api_err_text` handling both envelope shapes; `_jobs_list()`; lift `_runnable` out of
   `_hermes_stream` as `_job_live` so the table and the verifier cannot disagree.
3. Render layer: `_md_cell`, `_when`, `_job_glyph`, `_jobs_table`, `_render_list`, `_jobs_error`.
4. Regexes: `_BG_LIST` (measured against 55+ non-task sentences), `_MANAGE_VERB`, `_ORDINAL_RE`,
   `_REF_STOP`, `_JOBS_MARK_RE`, `_CONFIRM_MARK_RE`, `_CONFIRM_YES/NO/ALT`.
5. `_is_bg_task_request`: `_BG_LIST` check **between** `_BG_MANAGE` and the `_BG_QUESTION` deny —
   placement is load-bearing, since exactly 4 of the 9 phrasings start with "what" and are killed
   by the deny (measured).
6. Marker layer: `_park_jobs`, `_parked_jobs`, `_confirm_park`, `_choose_park`, `_pending_confirm`.
7. Resolution layer: `_ref_ordinal`, `_ref_tokens`, `_manage_op`, `_resolve_ref`.
8. Execution layer: `_may_manage`, `_do_manage`, `_manage_turn`.
9. `pipe()` wiring: branch 1 (parked reference / armed confirm) before `followup` is consumed;
   branch 2 (list/manage vocabulary) inside the existing bg block; correct the false comment
   claiming manage verbs "run no agent job".
10. Trap sweep, tests, `scripts/deploy_pipe.py`, docs.

## Test plan

- `tests/test_bgtask_intent.py` — the 9 listing phrasings as YES; the measured non-task corpus as NO.
- `tests/test_manage_path.py` (new, offline, stubbed HTTP) — table render incl. empty/unreachable/
  paused/completed/failing rows; marker round-trip and staleness; all 8 worked resolution examples;
  ambiguity never acts; quantifier/negation guards; the two-turn delete gate incl. no-client
  (must NOT delete), expired marker, changed-under-us, and 404-race; marker-injection via a job
  name containing `-->`; pause/resume idempotency; `_may_manage` refusing a non-admin.
- `tests/route_metrics.py` — new invariants: every `task.manage.*` row carries `strategy` or `op`;
  every `task.list` row carries `n_jobs` or `err`.

## Known limits, stated not hidden

- Multi-user scoping is impossible until hermes grows an owner field (roadmap step 4). This plan
  declines to widen exposure rather than pretending to close the hole.
- Run-now, reschedule and batch ops stay with the agent by design.
- R4/R5 (prompt substring, token overlap) are provisional: the live precision signal is the
  `kind="task_cancel"` confirm accept/decline ratio. If that is below ~80% at n ≥ 20, demote them
  to "always disambiguate".
- The reference resolver has 8 worked examples but no adversarial corpus yet; the listing
  vocabulary has 75+ measured sentences. That asymmetry is the main residual risk.

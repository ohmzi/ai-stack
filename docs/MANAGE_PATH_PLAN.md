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

  > **This was false when written, and was measured false in commit 12047f2 (2026-08-03).** The two
  > halves it checked are true and were the wrong halves: `_scrub` does leave the comment alone and
  > `omsgs` does carry the content verbatim, but the *client* escapes it. Two renderings were tried
  > — an HTML comment inline in a paragraph, and the same comment as its own block after a blank
  > line — and OpenWebUI escaped both, showing the payload to the user as base64 noise appended to
  > the reply (`pipes/auto_assistant.py:2026-2037`). Nothing can be parked in the reply body. See
  > **Cross-turn state** below for the mechanism that replaced it.

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

**Superseded 2026-08-03 (commit 403a127) — the `batch` row only.** Naming a set is a request, not an
ambiguity. Refusing "cancel all of them" and offering a one-at-a-time menu made cancelling two tasks
a four-turn negotiation with a renumbered list in the middle — the user cancels one, waits, then has
to find the next one under a different ordinal. Batch is now deterministic and does have
partial-success semantics; the row above records what v1 decided, not what runs. Details under
**Reference resolution**. `run now` and `create, reschedule/edit` still go to the agent as written.

Structural rule: **the deterministic path returns `None` to mean "I could not answer this"**, and
the existing agent delegation stays the sole fallback (unresolved reference, gateway down,
non-admin). Safe degradation to today's behaviour, never a dead end.

**Superseded 2026-08-03.** All three named fallbacks are gone, for three different reasons:

- *Gateway down* is answered in place with `_jobs_error(err, "list")` — "the agent would hit the
  same dead API and cost an eviction to do it" (`pipes/auto_assistant.py:4369-4374`).
- *An unresolved reference* re-renders the list with a lead saying which way it failed (`many`,
  `bad_id`, `out_of_range`, `need_list`, `gone`, or no match) and changes nothing (`:4574-4604`).
  No status returns `None`.
- *A non-admin read-only turn* is refused, not delegated. The agent's job list is the whole host, so
  handing it "list my tasks" would show one user everyone's: "This deliberately narrows the old
  'non-admins fall through to today's behaviour' contract: that fallthrough WAS the leak"
  (`:5996-6008`). Admins still fall through.

`_manage_turn` now returns `None` in exactly two places. One is the kill switch
(`if not MANAGE_DETERMINISTIC: … return None`). The other is an abandoned confirmation whose reply
carries no task vocabulary; that turn resumes ordinary routing rather than being delegated, so
"`None` means not mine" survives in that case.

> **Corrected 2026-08-08.** This paragraph claimed the kill-switch branch was "unreachable from
> `pipe()`, so it only fires for a direct caller". It is reachable, by a second route the claim did
> not consider. `_manage_turn` has **two** call sites. The one inside the manage block is indeed
> gated — `if BG_TASKS and MANAGE_DETERMINISTIC and not ref:` — so the switch cannot be hit through
> it. But `_task_mode_turn` also calls it, and *its* call site is gated on
> `if BG_TASKS and tm_src and (text or "").strip():` — no mention of `MANAGE_DETERMINISTIC`. So with
> the **Task button on** and the switch flipped off, `pipe()` → `_task_mode_turn` → `_manage_turn` →
> kill switch → `None`, from a real user turn. Worth knowing before anyone flips that constant to
> disable the deterministic path: it does not disable it uniformly, and the Task-button route is the
> gap. Both branches are named by behaviour rather than line number here on purpose — the earlier
> line citations had already drifted.

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

**Superseded 2026-08-03 (commit 403a127): bulk resolves, exclusion still does not.** The split is
between a set the user named and a set the user described by what is *missing* from it. `status`
gained an eighth value, `bulk`, which the set listed at the top of this section does not include.

- `_REF_BULK` resolves. It matches `all`, `everything`, `every one`, `everyone`, `each of them`,
  `both`, `the lot`, `all of them` / `all of the` / `all of my`, and `any of them`
  (`pipes/auto_assistant.py:1025-1027`); `_resolve_ref` returns `status="bulk"` carrying every job
  the reader can see (`:2369-2370`). A joined multi-select — "a and b", "1, 2", "the first and the
  third" — returns `"bulk"` too, via `_REF_JOINER`, but only when every part resolves on its own
  *and* to a different job (`:1035`, `:2374-2388`).
- `_REF_QUANTIFIER` is now `the rest` alone (`:1030`). It and `_REF_NEGATION` are the only forms
  still forcing `many` (`:2364-2365`). Bare `each` and `every` match neither pattern, so they no
  longer force `many` either — they fall through the ladder like any other word.

Safety moved from the refusal to the confirmation, and it is not uniform across the ops. Cancel arms
one confirmation that tabulates every job with its schedule and id — a count alone ("delete 4
tasks?") is not something anyone can check — and one "yes" runs the batch (`:4523-4541`,
`:4433-4462`). Pause and resume are reversible, so they act immediately over the batch with no
confirmation at all (`:4542-4545`). `_do_manage_bulk` reports the whole batch as one result with a
line per job: `✅ **Cancelled N tasks.**` when nothing failed, and `**Cancelled N of M tasks.**` plus
a per-job failure line when something did (`:4326-4355`).

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

**Superseded 2026-08-03 (commit 12047f2).** No reply carries a marker any more, because the premise
this rested on — the first *Verified environment fact* above — was measured false: OpenWebUI escapes
HTML comments wherever they appear. `_marks()` is a no-op returning `""`, kept as the single seam
where trailing state used to be appended so a future edit cannot silently re-add one
(`pipes/auto_assistant.py:2026-2037`). `_park_jobs` and `_confirm_park` write per-chat in-memory
stores (`self._parked`, `self._armed`, created in `__init__`) and return `""`; `_parked_jobs` and
`_pending_confirm` read the stores first and treat `_JOBS_MARK_RE` / `_CONFIRM_MARK_RE` as a
legacy read-only fallback for history written before the change — honoured only for an admin, since
a legacy marker carries no handle and so cannot be checked against the reader (`:2157-2263`). Both
stores carry the handle they were rendered for, so an ordinal or a "yes" cannot be resolved by a
different user than the one who was shown the list. The armed record holds more than the draft's
`{v, t, stage, op, id, n, s}`: `ids` / `ns` / `ss` — the batch, its names and its schedules — plus
the singular `id` / `n` / `s` kept for the one-job case (`:2216-2237`). The precedent cited above
moved too: `_phone_prompt` parks in `self._phone_ask` (`:2000-2006`).

The consequence the draft did not have to state: cross-turn state is now in-process. A parked list or
an armed confirmation **does not survive a redeploy or a container restart**, and `_lru` caps each
store at 60 chats (`:2150-2155`), so it is also evicted once 60 other chats have used the path. What
an operator would see is "the second one" answered with *I haven't shown you a list in this chat
yet*, one turn after a list that is still on screen. The cost is continuity, not correctness: every
reader re-fetches `/api/jobs` and confirms against a live record before acting, and a lost armed
confirmation simply cannot be answered, so no delete happens.

Only the carrier changed, not the rules. Everywhere the rest of this document says "marker" —
"pipe-rendered marker gate" in the intent taxonomy, "the two-turn marker gate" under
**Destructive-op safety**, "the marker only maps ordinal → id" in the next paragraph — read it as the
per-chat store. The gate is still two turns, still fingerprints name+schedule, and still fails closed:
turn N writes nothing, and the DELETE needs an explicit affirmative, an armed `stage:"confirm"` record
that has not expired, the id still present in a fresh fetch, and the name and schedule unchanged
(`pipes/auto_assistant.py:4403-4486`).

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

**Superseded 2026-08-03 (commit 8a5e62d).** The premise above still holds, and is exactly *why* the
fix looks the way it does: hermes still has no per-job owner field, and `origin.user_id` is still
`None` for everything the pipe creates (`pipes/auto_assistant.py:4220-4223`). What changed is the
conclusion drawn from it. Rather than wait for a hermes-side change, ownership moved pipe-side into
`TASK_OWNERS_FILE` — `alerts/job_owners.json` (`:334`) — stamped at creation from the scheduler's own
job ids. So the gate is not a boolean and never shipped under the name this plan gave it: it is
`_manage_scope(user, handle)` (`:4217-4232`; `_may_manage` does not exist anywhere in the tree),
which returns `None` for an admin or a `TASK_ADMINS` handle, meaning "everyone's jobs", and otherwise
returns the owning handle. Role is still the primary signal and `TASK_ADMINS` still the supplementary
allow-list, exactly as correction 1 above decided.

Two conclusions in this section are now false:

- **Non-admins do not skip the deterministic block.** `_manage_turn` filters its single `/api/jobs`
  fetch to the owning handle once (`:4385-4394`), and everything downstream reads that filtered
  list — the listing, `_resolve_ref`'s candidate set, the disambiguation table and the armed-confirm
  lookup. `_do_manage` re-reads ownership before any write, so a parked reference to a record that
  changed hands is refused in wording that confirms nothing about a job the user may not know exists
  (`:4249-4258`).
- **An unreadable ownership map fails closed** with an explanation (`:4385-4393`). Rendering an empty
  list would be the precise lie every string in `_jobs_error` exists to prevent — *you have nothing
  scheduled* — and rendering the unfiltered host list would be the exposure this path was built to
  close.

The last sentence survives with a different subject: broadening still changes only *how* a user can
phrase things, and it is scoping, not the admin check, that now decides *whose* jobs they enumerate.

### Kill switch

`MANAGE_DETERMINISTIC = True` — one module constant gating both the deterministic path **and** the
broadened vocabulary in the same expression, so disabling (A) makes (B) inert in one edit. Rollback
is: flip the constant (markers drain naturally), redeploy, then revert if needed.

**Superseded 2026-08-03 (commit 12047f2) — "markers drain naturally".** There are no markers to
drain. Cross-turn state is per-chat and in-process (see **Cross-turn state**), so the redeploy that
applies the flip discards it outright. The gate itself is unchanged and is read in `pipe()` before the
manage block is entered at all (`pipes/auto_assistant.py:5895`). One consequence of flipping it is now
visible to users rather than silent: since 2026-08-03 a non-admin read-only turn does not fall through
to the agent, so with the switch off they get a refusal saying task listing is switched off
(`:5996-6008`) instead of the pre-step-3 delegation.

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
8. Execution layer: `_manage_scope` (planned here as `_may_manage`; see the Authorization
   supersession), `_do_manage`, `_manage_turn`.
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
  name containing `-->`; pause/resume idempotency; `_manage_scope` (planned as `_may_manage`) scoping
  a non-admin — `tests/test_manage_path.py:501-507` asserts it returns `None` for an admin and for a
  `TASK_ADMINS` handle, the handle itself for an ordinary user, and `"user"` for an account with no
  handle.
- `tests/route_metrics.py` — new invariants: every `task.manage.*` row carries `strategy` or `op`;
  every `task.list` row carries `n_jobs` or `err`.

Measured 2026-08-08: `python3 tests/test_manage_path.py` reports **201 checks — ALL PASS**. That count
is larger than this plan's list because it also covers the bulk resolution, the bulk confirmation and
the ownership-scoping behaviour added after the plan was written.

## Known limits, stated not hidden

- Multi-user scoping is impossible until hermes grows an owner field (roadmap step 4). This plan
  declines to widen exposure rather than pretending to close the hole.

  **Superseded 2026-08-03 (commit 8a5e62d).** Scoping moved pipe-side — `alerts/job_owners.json` —
  instead of waiting for a hermes owner field, which hermes still does not have. The exposure is
  narrowed, not closed, and the residual limits are stated rather than hidden: the handle is the email
  local part, so `alice@a.com` and `alice@b.com` collide and every account with no email shares
  `"user"` (`pipes/auto_assistant.py:4225-4228`); and a job nobody owns is admin-only, because
  `_owner_of` returns `None` for it and no handle compares equal to `None` (`:1949-1950`, `:4394`).
  `docs/ROUTING_ROADMAP.md` (the 2026-08-03 per-user-ownership section) points at `HERMES_AGENT.md`
  for the rest: handle collisions, concurrent-creation misattribution, and host-shell access.
- Run-now, reschedule and batch ops stay with the agent by design.

  **Superseded 2026-08-03 (commit 403a127), for batch only.** Batch is deterministic — see the
  supersession under **Reference resolution**. Run-now and reschedule are still the agent's, and
  run-now stays there by omission: `restart` is deliberately absent from `_MANAGE_VERB` because it
  reads as run-now, which re-arms and then deletes a one-shot, so leaving it in would have let
  `resume` silently absorb it (`pipes/auto_assistant.py:984-998`).
- R4/R5 (prompt substring, token overlap) are provisional: the live precision signal is the
  `kind="task_cancel"` confirm accept/decline ratio. If that is below ~80% at n ≥ 20, demote them
  to "always disambiguate".
- The reference resolver has 8 worked examples but no adversarial corpus yet; the listing
  vocabulary has 75+ measured sentences. That asymmetry is the main residual risk.

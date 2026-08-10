# Hermes Agent — background tasks for the assistant

_Installed 2026-07-29. "Monitor this price for 2 weeks" typed into OpenWebUI now becomes a real,
bounded, GPU-safe scheduled job, executed by a local agent and reported back into the UI._

## What runs

| Piece | What / where |
|---|---|
| Runtime | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) **v0.19.0**, pinned at commit `b6729ba9`, installed at `~/.hermes` (uv venv, MIT). Do not run `hermes update` casually — upstream merges ~660 PRs between patch releases; re-verify with the tests below after any update. |
| Model | `hermes-genesis:agent` — a second Ollama tag of the SAME weights as the chat model (`ollama create` from `apex-compact` + `PARAMETER num_ctx 65536`; shares blobs, ~0 extra disk). Exists because hermes hard-requires a 64 K context window, and raising the global `OLLAMA_CONTEXT_LENGTH=32768` would tax every OpenWebUI chat turn instead. |
| Service | `hermes-gateway` systemd **user** service (linger enabled). Hosts the cron scheduler and the API server on `127.0.0.1:8642` (key in `~/.hermes/.env`, a copy staged at `/volume1/docker/openwebui/config/hermes_api_key` so the pipe can read it in-container). |
| GPU guard | `hermes/plugins/gpuguard/` in this repo, **symlinked** to `~/.hermes/plugins/gpuguard/` (config `cron.provider: gpuguard`). A cron scheduler provider that defers ticks while **either** ComfyUI's `/queue` shows work **or** Ollama's `/api/ps` shows a big model that isn't ours. Due jobs are never lost, only deferred to the next 60 s tick. Covered by `tests/test_gpuguard.py` (24 checks). **Caveat:** a hand-run `hermes cron tick` bypasses the provider; the gateway path — the only unattended path — is guarded. |
| ↳ why Ollama too | Added 2026-07-31. The cron tag runs at `num_ctx 65536` and the pipe's chat tenant at 32768; Ollama keys runners by model+options, so those are two **distinct** ~17 GB runners that cannot co-reside on a 24 GB card. A tick firing mid-conversation evicted the chat model and the user's next turn paid a cold reload — **measured at 22.7 s**. Co-residency is unreachable without taxing every chat turn, so the fix is scheduling. `/api/ps` answers "is a big model *resident*", not "*generating*" — with `OLLAMA_KEEP_ALIVE=60s` those differ by at most one tick, which is the accepted trade. Helpers are excluded by footprint (measured: tenant 16.70 GiB vs `gemma4:e2b` 1.81, `gemma3:1b` 0.92, `bge-m3` 0.62 — threshold 10 GiB), because OWUI runs title/tag generation and the route classifier constantly and "any model loaded ⇒ defer" would starve cron permanently. Our own `hermes-genesis:agent` never defers: resident means a job just ran, and reusing that warm runner is the best case. |
| ↳ starvation escape | Continuous chat keeps the tenant resident indefinitely, so the gate cannot be stateless. `HERMES_GPUGUARD_MAX_DEFER_S` (default 900) force-dispatches when **only Ollama** blocks — three cadence periods of the tightest 5-minute monitor. `HERMES_GPUGUARD_HARD_DEFER_S` (default 3600) force-dispatches regardless, releasing a wedged queue. The two tiers deliberately do **not** share a threshold: forcing past a resident-idle tenant costs one recoverable eviction, but forcing past a *running render* OOMs a job that may be twenty minutes in — the exact failure this plugin exists to prevent. |
| Delivery | **Deterministic since 2026-07-30**: jobs run NO delivery commands — they end their response with `LOG: <summary>` (always) and `ALERT(<user>): <msg>` (only when the user's condition holds). `scripts/hermes_delivery.py` (user timer, 1 min) parses each new output under `~/.hermes/cron/output/<job>/` and does the delivery itself: LOG → background-tasks channel webhook, ALERT → `send_alert()` → text (carrier email-to-SMS gateway) + email, both proven live 2026-07-30. Recipients validated against `^[a-z0-9_-]+$`, 3 alerts/run cap, per-leg retry (a failed push never re-posts the channel log). Born from two live failures: an agent-authored job that invented `send_webhook_post()` helpers and delivered nothing, then an agent that *claimed* deliveries which never happened. The LLM writes text; infrastructure delivers. Covered by `tests/test_hermes_delivery.py` (70 checks). |
| Entry point | The `auto_assistant` pipe routes background-task intent (`tests/test_bgtask_intent.py`, 124 checks — 25 positive, 39 negative — default-deny) to `POST 127.0.0.1:8642/v1/chat/completions` — an agent runtime, not an LLM proxy. The agent creates/manages its own cron jobs via its `cronjob` tool and streams confirmation back into the same chat. **No second model row in the picker; the single-pipe architecture holds.** |

## Config decisions that are deliberate

- `model.provider: ollama`, `base_url http://127.0.0.1:11434/v1`, `reasoning_effort: low` — on this
  endpoint `reasoning_effort` IS honoured (2 tokens vs 126 on a trivial call, measured); the pipe's
  native `/api/chat` uses `think:false` instead. The two endpoints behave oppositely — see MODELS.md.
- `platform_toolsets.api_server: [web, file, memory, session_search, todo, cronjob, skills]` —
  **no terminal, no browser, no code execution** on the chat-facing surface. Chat-reachable text
  must not be able to shell out; creating a cron job needs none of those.
- `platform_toolsets.cron: [web, terminal, file, memory, todo, cronjob, skills]` — jobs need
  `terminal` for `curl` (fetching pages, SearXNG at `:8888`, and the delivery POST). Hermes runs
  unattended jobs with dangerous-command approval in DENY mode by default; the hardline blocklist
  (fork bombs, filesystem wipes) applies regardless.
- No Nous Portal account, no cloud keys: search is local SearXNG via curl, fetching is curl. The
  hosted-tier tools (Firecrawl search, cloud browser, image gen) are simply absent.

## Using it

In OpenWebUI, just ask: *"monitor the price of X … check every 6 hours for 2 weeks"* /
*"list my background tasks"* / *"cancel the price monitor"* — or press the **Task** button, one of the
three mutually-exclusive mode buttons the frontend fork adds to the chat input, which routes the turn
here without any wording being interpreted. (`/task` as a prefix still works.) From a terminal:
`hermes cron list`, `hermes cron remove <id>`, `journalctl --user -u hermes-gateway -f`.

Results appear in the **background-tasks** channel, and the fork puts a **Background tasks** shortcut
in the sidebar's *top* nav group so it is reachable without scrolling past every other channel. It
resolves the channel **by name**, not by a pinned uuid — so it works on any install, and it renders
nothing at all when the channel is absent rather than dead-linking. It is also gated on channels
being enabled and the user having channel permission.

### One-shot research — `/research` (added 2026-07-31)

`/research <question>` (or `/agent`) delegates a **single** multi-step question to the same agent
and streams the answer back. No job is created and no scheduler verification runs.

This reaches capability that was installed but unreachable: the chat surface already has `web`,
`file`, `memory`, `session_search` and `todo`, with terminal and code execution deliberately
excluded — but `_is_bg_task_request` requires *recurrence*, so everything one-shot fell through to
plain chat.

**Explicit by design, not a heuristic.** Every routing tier in this stack that guessed has needed
measuring and walking back, and the cost of a false positive here is not a wrong answer: delegating
releases the chat tenant, so the user pays a ~23 s reload for a question that would have been
answered in two. `/img` and `/vid` set the precedent. A heuristic can be earned later, with data.

It runs under `_RESEARCH_BRIEF`, not the cron brief — the two are not interchangeable. The cron
brief instructs the agent to schedule work and to emit `LOG:` / `ALERT(...)` lines, and
`hermes_delivery.py` parses those out of *any* run, so a research answer under the cron brief could
post a false alert. The research brief forbids both, and keeps the "never state a number you did
not read from a source" rule.

### Confirmation before a render

Media routing is default-deny but the residual false-positive rate is ~35%, and a wrong render
evicts the 18 GB chat tenant for minutes. `CONFIRM_RENDERS` in the pipe (`"never"` / `"video"` /
`"all"`, default `"video"`) asks first, keyed on **measured cost** rather than the noun: video
(186–386 s) and image *edits* (~49 s since the speed-LoRA tier, was 162 s) are gated; a fresh Krea
image (16 s) is not, unless set to `"all"`. The edit stayed gated through that 3× speed-up because
the eviction, not the render, is what costs the user — it is the same 18 GB reload either way.

It **always fails open** — no client to ask, a dead socket, an unsupported client or any exception
all proceed. The gate is a courtesy to avoid wasted GPU, never a reason a correctly-routed render
fails; failing closed would hang `tests/eval/run_eval.py`, which drives `pipe()` with no client
attached. Covered by `tests/test_confirm_gate.py`.

Proven end-to-end 2026-07-29 with a bounded demo (books.toscrape.com, every 2 m, repeat 2): run 1
posted "£51.77 (first run)", run 2 read the state file and posted "£51.77 (unchanged)", job then
retired itself.

## Personal alerts — Twilio SMS + SMTP email

A job that fires a condition emits `ALERT(<handle>): <message>`; the watcher routes it through
`scripts/alert_transports.py`, which sends **both** a text and an email by default. SMS is the buzz
(real text, real ringtone, no app, no OS notification settings involved); email is the record.
`ALERT_CHANNELS` narrows it to one.

**SMS has two methods** (`SMS_METHOD`): `gateway` (default when `SMS_GATEWAY` is set) emails the
carrier's free email-to-SMS bridge — e.g. Telus `<10-digit>@msg.telus.com` — so a text is just an
email and rides the same SMTP connection at zero cost; `twilio` uses the REST API (paid, and a
**trial** account is blocked from sending custom text — error 572006 — so it needs a real
upgrade). This box uses the gateway.

Addresses come from where they actually live: **email is derived automatically** from the
OpenWebUI user table by matching the handle against each account's email local part — no
configuration, new users work immediately. **Phone numbers are opt-in** per handle in
`/volume1/docker/openwebui/config/alerts/contacts.json` (the shared file — see the caveat further
down; `~/.hermes/alert_contacts.json` is only an un-migrated fallback and does not merge), because
OpenWebUI has no phone field and not everyone in a household wants texts; a handle with no phone
entry quietly gets email only.

Secrets live in `~/.hermes/alert_transports.env` (0600, never in git; a `.template` sits beside it).
Gmail needs an **app password**, not the account password. A Twilio **trial** account can only text
numbers verified in its console — the first thing to check if SMS 400s.

Design decisions worth keeping:

- **Partial success is success.** SMS delivered + email failed returns delivered, so the watcher
  never re-sends the text every minute to fix a mail problem. Failures are logged per leg.
- **E.164 or refuse.** Numbers are normalized locally and an unnormalizable one is skipped with a
  log line, rather than handed to Twilio to reject with a 400 nobody reads.
- **Unconfigured is not an error.** Missing config, missing phone, or every channel failing folds
  the alert text into the channel post flagged `⚠️ [alert]` — a fired condition is never lost.

Covered by `tests/test_alert_transports.py` (130 checks: normalization, resolution precedence,
fan-out semantics, channel selection, and the Twilio request shape against the documented API).
Everything verifiable offline is pinned there, so a live failure has exactly one unknown left.

### Why ntfy is gone (2026-07-30)

It was built, multi-user, and every server-side leg verified — per-user accounts provisioned by
bcrypt-hash mirroring from OpenWebUI, deny-all ACLs, the APNs wake relay, and debug logs showing the
phone authenticate and fetch each message **within one second of publish**. Alerts reached the app
every time; iOS refused to render a banner, through re-subscribes, base_url alignment, urgent
priority and a settings audit. The failure lived in a layer this stack cannot instrument. SMS has no
equivalent layer: the carrier either delivers a text or returns an error code.



### Alert delivery is verified, retried, and recorded

Every alert goes through a queue rather than a single fire-and-forget send:

- **Verified** — SMTP `send_message` returns the recipients the server *refused*; a non-empty
  result is treated as a failure, so a "sent" is only logged when the server actually accepted it.
  (Beyond that hop — carrier to handset — nothing is verifiable by anyone; that limit is real.)
- **Retried** — up to `MAX_ATTEMPTS` (4: the first try plus 3 retries) spaced `RETRY_AFTER_S`
  (5 minutes) apart, drained by the same 1-minute watcher tick. Backoff is honoured; a delivered
  alert stops immediately; a failed one is never retried forever.
- **Recorded** — every attempt appends to `~/.hermes/cron/output/alert_ledger.jsonl`
  (append-only, never rewritten) with timestamp, attempt number, recipient, per-channel result and
  the exact error. The live queue is `.alerts.json` beside it.

```bash
python3 scripts/hermes_delivery.py --ledger   # history + what is still pending
```

When an alert exhausts its attempts, a loud notice goes to the background-tasks channel with the
last error — an undeliverable alert is never silent.

### Price monitoring is deterministic code, not a per-run scraper

`scripts/price_watch.py` fetches, extracts, compares against saved state and prints the LOG/ALERT
lines itself. A model *does* run — the job is an ordinary agent run — but it is never allowed to
produce the number: the brief (rule 5d) dictates the exact command and tells the agent to print its
output **verbatim as the entire response**, adding nothing. So there is no step at which a price can
be invented, and that property comes from the extractor being deterministic rather than from the
absence of a model.

> Corrected 2026-07-31. This section previously claimed jobs run via `--script <name>.py
> --no-agent`, so "no model runs at all". That was true of the retired model-authored era and is
> not how any live monitor works — commit 2418669 narrowed a blanket `--no-agent` ban that
> "forbade the one thing that fixes the bug it was written about". The only `--no-agent` job left
> in `jobs.json` is the retired `amazon_price_check.py`, whose last status is an error.

State is keyed by `--state` **and bound to the URL it was recorded for**. The agent picks and
reuses these short names — this box already has two different watches both called
`tipping-the-velvet-price` — and a reused name would otherwise inherit the previous watch's
`alerted_price`, so the new monitor's first reading reads as "unchanged" and the user is never
told. A different URL under the same name resets the dampening.

It replaced the model-writes-a-scraper approach after that approach failed three separate ways in
production: a crash inside its own regex, an ALERT assigned to a variable it never printed, and
finally a run on a 1.5 MB Amazon page that abandoned the output protocol entirely and emitted
marketing prose containing an **invented promo code** beside a price it had never read. A price check
is arithmetic on a fetched string; giving it a 34B model added a hallucination surface and nothing
else.

Extraction is confidence-ranked and says which rank it used:

| Rank | Source |
|---|---|
| high | JSON-LD `price`, `og:price:amount`, `itemprop="price"`, known per-site containers |
| low | a bare currency match in visible text, or a marketplace-offer price |

A low-confidence reading is reported with its caveat inline; `--require-confidence` refuses to alert
on one at all. `--selector '<regex with one group>'` pins an exact element when a site needs it.

**Amazon serves rotating page variants, and honesty about which one you got is the whole game.**
On the common variant `corePrice_feature_div` ships **empty** — the buy box is written in by
JavaScript — leaving only the "New (N) from $X" marketplace offer, a real number but not the buy-box
price, reported as `amazon-offer-listing` / **low**. On other fetches of the *same URL* Amazon embeds
`"price"` and `"priceAmount"` as JSON, which reads **high**. So `best_candidates()` retries a few
times and takes a high-confidence variant when one appears, rather than reporting whichever variant
luck supplied. Both variants agreed on $46.99; the model that originally "read" this page had
reported $49.99, a number present in neither.

Counter-intuitive and verified twice on this host: amazon.ca serves the full page to a plain urllib
request and a **robot wall to a spoofed Chrome User-Agent**. Do not "fix" the fetch by adding one.

```bash
python3 scripts/price_watch.py --selftest     # runs both live reference pages
```

### The no-URL recipe — `price_search.py` (brief rule 5d-ii)

`price_watch.py` is **URL-in only**, and the brief tells the agent to run it verbatim. So *"search
online for the Google Fitbit Air, alert me under $150"* left the agent with no vetted recipe, and
live it improvised: it described a search-and-scrape job it never actually created, and the
scheduler diff caught it (*"Verification failed"*). The fix was not a smarter agent but a boring
one-liner it could schedule.

`scripts/price_search.py` answers exactly one question — *which URL should `price_watch` watch for
this query?* — and then **imports and reuses `price_watch` for everything after resolution**:
extraction, thresholds, dampening, failure streaks, recovery, and the LOG/ALERT protocol. There is
one extraction path in this repo, not two.

Search is rationed, because the SearXNG roster is fragile: ~60 queries in 10 minutes got the engines
CAPTCHA'd for an hour (measured — it is also why `search_canary.py` polls only every 30 min). So the
chosen URL is **cached: one search per monitor lifetime** in the steady state. It re-searches only
when the page dies (gone/blocked) or stops yielding a value, never more than once per run, and never
more often than `SEARCH_COOLDOWN_S`. Past `NOT_FOUND_ALERT_AFTER` the wait escalates deliberately:
once the user has been *told* nothing was found, searching harder buys nothing — an every-5-minute
monitor that never resolved spent 144 queries a day across the whole roster (job `99cdcb68d1e1`,
measured).

Covered by `tests/test_price_search.py` (109 checks). It also owns the fare refusal described below,
which is the one `--kind` it will not resolve.

### A page offering many prices has no price of its own (2026-08-07)

`candidates()` assumed a page describes one thing and took the first machine-readable price in
document order. On a category, route or search-result page that is an arbitrary element of a list —
and it arrives labelled `json-ld/price` at **high** confidence, so `--require-confidence` waves it
through and the wrong-product scoring has nothing to object to. The number really is read from the
page. It is simply not the price of anything the user asked about, and no confidence tier can notice
that.

Measured before the threshold was chosen, which is the only reason the number is defensible:

| Page | Distinct machine-readable prices |
|---|---|
| cheapflights.ca route page | **97**, from 103.98 to 358.72+ |
| books.toscrape product page | 2 (with and without tax) |
| amazon.ca product page | 0 high (only the `low` offer-listing reading) |

So `LISTING_MIN_PRICES = 5`: the gate is `>=`, so it trips AT five distinct machine-readable
prices, not above five — `candidates()` returns nothing and records why, and the run
takes the same path an unreadable page takes — honest LOG every run, one `no_value` alert after
three, `--selector` to pin an element. This protects **product** watches, on a path people use.

### Stock and availability — `--mode stock` (2026-08-07)

The four stock kinds were in `alert_templates` and listed here as supported for months while nothing
could read an availability: `candidates()` structurally requires a decimal, and `--kind` was applied
*after* a numeric comparison. So "tell me when it's back in stock" fired on a price threshold or
never fired at all. `--mode stock` is **required** for a stock watch; without it the run still
compares a price. A stock `--kind` passed without it auto-promotes and says so in the LOG.

Three extraction tiers, confidence last in the tuple so the fetch-and-rank loop is shared with
prices:

- **high** — the page states its own availability in a machine-readable field (schema.org JSON-LD,
  `itemprop`, `og:`/`product:` feeds, both attribute orders). Each pattern is *bounded* to a single
  tag; an unbounded `.*?` across a 1.5 MB Amazon page matches a recommendation carousel.
- **medium** — a site container known to hold the box: Amazon's `#availability`, Shopify product
  data gated on a Shopify tell, a `stockStatus` field. This is the tier `price_watch`'s docstring
  had promised since it was written and never once emitted.
- **low** — visible text, where negatives beat positives on purpose. "Add to Cart" ships on nearly
  every retail page, disabled or in a carousel; "Currently unavailable" is almost never decoration.

**The vocabulary is closed.** A token the table has never seen yields no candidate, so the page
takes the "loaded but no value found" path. **An unreadable page is never reported as out of
stock** — Amazon writes its buy box with JavaScript and may write this box the same way, and "I
could not read it" is a different statement from "it is gone". A `PreOrder` or `BackOrder` fires
nothing and is logged every run: a pre-order button is not a restock.

Firing is a **predicate on the current reading**, not a transition — the same rule as prices, so a
first confirmed `in_stock` alerts. `--kind` now *selects* the predicate. A threshold on a
non-`inventory` stock watch is ignored **out loud**, because honouring it silently is how the
original bug stayed invisible. Two suppression layers: a reading must repeat before it is acted on
(high 1, medium 2, low 3 *matching readings* — an unreadable run is evidence neither way), and at
most one stock alert per monitor per six hours. Measured: a page flapping every five minutes for six
hours sends **one** text and logs 71 times.

`--unit` defaults to unset and resolves per mode, because `money(3, "$")` renders "3 left" as a
34px `$3.00` in the email.

### Fares are refused, not attempted (2026-08-07)

Watching a flight fare is **not supported**, and the refusal is deliberate rather than a gap. Two
production jobs established it:

- `99cdcb68d1e1` searched six times and resolved no page at all.
- `52f821a8d3a2` resolved cheapflights.ca and texted *"$358.72, under your $1,000.00 target"* at
  **high** confidence — the first element of the 97-value offers array above, on a page whose own
  title reads "C$ 146+". No date, no itinerary, nothing bookable. Every guard in the extractor was
  satisfied and the reading was still meaningless.

A fare only exists behind an airline's search form, for one itinerary on one date, and nothing
static carries one. So `price_search.py --kind fare` refuses at its first run: one LOG line saying
it cannot work and why, one `fare_needs_itinerary` alert, no search spent, exit 0 — because a
configuration limit is not an infrastructure error, and a run that raises has no LOG line at all.
It refuses **loudly and once** rather than emitting `not_found` forever, so a monitor that cannot
work says so instead of looking busy.

Making this work at all would need a headless browser reading one specific itinerary. Playwright
1.49.1 and Chromium are already on this host, but only under `/usr/bin/python3` — a cron job's bare
`python3` is the hermes venv 3.11, where the import fails. Anything built here must spell the
interpreter out.

**Superseded as the user-facing path (2026-08-08, commit `03c46da`).** Everything above still
describes what happens if a fare job is *created* — the refusal, its kind, its LOG line — but a
flight ask no longer gets that far. `FLIGHT_ROUTE` claims the turn in `pipe()` **before**
`_is_bg_task_request`, so the agent is never delegated to and no job exists to refuse. The reason is
that the refusal, however honest, was still the wrong shape of answer: the user asked where to find a
cheap flight and got a monitor telling them it could not work.

What happens instead: deny arms run first (figurative, past-tense and meta flight talk stays in
chat), then origin/destination/dates are parsed **deterministically** and collected over a short form
if incomplete, and the turn ends in a Google Flights deep link built from those slots. A season
("sometime in the fall") or a named holiday is not a date and is asked about rather than guessed, and
no number is ever supplied by a model. This also closed a worse hole: 7 of 8 realistic flight
phrasings previously reached the plain chat model, which has no fare data and answers with an
invented price.

The recon behind the refusal is unchanged and is what justifies both decisions — 19 sites measured
2026-08-07, 11 blocking automated clients, 6 with no fetchable URL, 2 deal feeds, **0 readable**. See
[FLIGHT_RECON.md](FLIGHT_RECON.md), and `tests/test_flight_intent.py` (115 checks) for the routing.

### The brief is checked, not just stated (2026-08-08)

`_HERMES_BRIEF` is instructions to a local MoE — `hermes-genesis:agent`, the same weights as the chat
tenant, ~3 B active of 34.7 B — and on 2026-08-07 it broke three of them in one job. (This said
"a 20B local model" until 2026-08-08. No 20 B model has ever been on this box; the figure was
invented and then repeated into three files.) Two flight jobs — `676e970c59ad` and `4df0ab5bed14`, both created from the same ask — went
to the scheduler with:

- **`deliver: origin`** instead of the `local` rule 5 demands. The only origin on this host is the
  api_server, which has no push channel, so every run ended in
  `Adapter send failed: API server uses HTTP request/response, not send()`. Worth knowing *why* rule 5
  shouts about this: an **omitted** `deliver` does not default to `local` — hermes defaults it to
  "origin-or-local" (`tools/cronjob_tools.py:316`), which on an api_server session resolves to
  `origin`. The broken value is what you get for free, so the brief is the only thing standing
  between a forgotten parameter and a job that delivers nowhere.
- **no `LOG:` instruction** anywhere in the prompt, so `hermes_delivery.py` correctly refused to read
  the prose as a measurement and posted `⚠️ RUN DID NOT FOLLOW THE OUTPUT PROTOCOL` three times.
- **the model's own tool-call framing stored as part of the prompt** — the two jobs closed with
  *different* tags (`</prompt>` and `</parameter>`), then `<parameter=deliver>` and `origin` on the
  lines after, replayed on every run:

```
...report the cheapest option regardless.</parameter>
<parameter=deliver>
origin
```

The agent also hand-rolled a scraper against Google Flights, Skyscanner and Expedia in defiance of
rules 6b and 5d-ii, pulled 1.8 MB of HTML into a 64 K context, and ran **6 m 59 s** per tick — against
**11 s** for the vetted `price_watch.py` job `52f821a8d3a2` on the same host. Its "final response" was
mid-thought narration (*"Let me write a clean, well-structured script directly with write_file"*)
because it ran out of room before finishing. One earlier run died on
`HTTP 400: Cannot have 2 or more assistant messages at the end of the list`.

The delegation verifier already catches a job the agent *claimed* but never created. It had nothing to
say about a job that exists and cannot work. So `_job_defects` / `_job_patch` / `_enforce_job_shape`
(the pipe) now check the created record before its first run and repair it in one PATCH.

**What it says, and what it doesn't.** The two prompt defects speak, because they change the text of
the job the user asked for and a rewrite they cannot see is one they cannot correct. Rewiring
`deliver` back to `local` is **silent** — it is a host default the brief has to fight rather than
something the agent authored, it will need repairing on a good fraction of all creations, and
announcing it every time is the pipe narrating its own internals. A repair that *fails* always
speaks, whatever its code, and quotes hermes's own reason. The `repaired` count on the `hermes`
metric row counts every defect either way, so silence never costs visibility.

> **This was false when written, and is now true (fixed 2026-08-08).** `repaired` was computed
> *inside* `if shape:`, and `_enforce_job_shape` returns `""` whenever every repair was silent — so a
> job whose only defect was `deliver` sent its PATCH and recorded `repaired: 0`. That is the single
> most common violation on this host (hermes defaults an omitted `deliver` to origin, which is why
> the brief has to fight it), meaning the column under-reported exactly where the reply was already
> quiet, which is the one place the metric was the *only* remaining signal. The count is now taken
> before the repair and is not gated on whether the repair had anything to say. Pinned by a
> dedicated case in `tests/test_hermes_delegation.py` that fails against the pre-fix code.

**Only three checks, and the boundary is the point.** Each is decidable from the stored record with
no opinion, and each had already shipped a broken job. Whether *"every 6 hours, forever"* is the
duration the user asked for is a judgement, and a validator that guessed would be the fabrication it
exists to prevent — that one is reported in the reply and left alone.

Repairs are mechanical only:

- Markup is cut at its first character, **before** the protocol block is appended — the other order
  appends past the cut and then deletes it. Order is load-bearing and pinned by a test.
- A **vetted-extractor job is never appended to.** Its whole prompt is *"print this command's output
  verbatim, add nothing"*; appending a protocol block would make the run add something.
- A prompt that is **nothing but markup** is reported, not truncated to a stub. A scheduled job doing
  something arbitrary is worse than one flagged for the user to cancel.
- A PATCH that does not land can never read as one that did — the whole attempt folds into the
  unrepairable list and quotes hermes's own reason.
- **The protocol block is only appended if it fits.** `_JOB_PROMPT_MAX = 5000` is hermes's own
  `api_server._MAX_PROMPT_LENGTH`, and a PATCH past it is a 400 — so a prompt with no room keeps its
  defect and is *reported* rather than silently truncated to make space. The mirror limit is
  `_JOB_PROMPT_MIN = 24`, below which a markup cut has left no instruction worth keeping.

Covered by `tests/test_job_shape.py` (97 checks, stubbed scheduler), which pins the real 2026-08-07
job record verbatim rather than a paraphrase of it. The `hermes` metric row carries a `repaired`
count, so how often the model ignores its brief is now measurable instead of anecdotal.

### The sending account, and the prefix you cannot remove

Every text arrives with the sending address written in front of it:

```
plexlaking@gmail.com Hi ohmz, Ohmz AI here! Zakkart 2-Pack Cat Scratching Board is $46.99 ...
```

That is the **gateway**, not this stack. An email-originated SMS has no sender field, so the carrier
writes the sender into the message body. Tested on Telus 2026-07-30 with four header variants: a
`From` display name is ignored entirely, and an explicit `Sender` header changes nothing. A subject
IS rendered, as `Subj: <subject>` ahead of the body — which is why gateway sends always use an empty
one. **No header removes the prefix.** Only two things do: a paid SMS API (Twilio et al), or not
using SMS.

So the sending address is worth choosing deliberately — it is the assistant's visible identity on
every text. Switching it changes BOTH legs, because the email leg *is* SMTP and a gateway text is an
email to the carrier:

```bash
python3 scripts/alert_transports.py --set-sender you@gmail.com 'your-app-password'
```

The credentials are proven against the live server **before** anything is written, and a failure
leaves the previous working config byte-for-byte intact. A wrong password here does not degrade one
channel — it silences every alert on the box, which is not a thing to discover three days later.

This also sets the SMS character budget. A single GSM-7 segment holds 160 ASCII characters and the
prefix eats ~21 of them, which is why the body is capped at 140 rather than 160. **A longer sending
address costs message length.**

### Texts must not contain links

Carrier email-to-SMS gateways **silently drop messages containing URLs**. There is no bounce, no
error code, and SMTP reports success — the text simply never arrives. Measured 2026-07-30: two price
alerts carrying an amazon.ca link were accepted by Gmail and never delivered, while a link-free test
sent minutes later arrived immediately.

Because the failure is invisible, it cannot be retried into working; it has to be avoided.
`sms_body()` reduces any URL to its bare host and caps the message at one segment. **Email always
carries the full text with the link intact** — which is the division of labour the two channels
already had: SMS is the buzz, email is the record.

### Setup happens before scheduling, not after the first miss

Asking for a task that should text you, with no number on file, used to produce a perfectly
scheduled job that ran, met its condition, and skipped the alert with `no phone for 'ohmz'` in a log
nobody reads. The user believes they are being watched and hears nothing — the same silent-loss
shape as everything else on this page, arriving one layer earlier.

The pipe now checks first. `_WANTS_ALERT` recognises alert intent ("text me", "notify me when",
"let me know if"), and if the handle has no number the request is **parked** rather than scheduled:
the original wording is base64'd into an invisible `<!--bg-need-phone:…-->` marker, so answering
with a number saves it and runs the original request in one turn — no retyping. A bare
`514-555-0123` matches no task predicate and would otherwise reach the chat model, which would
cheerfully claim to have saved it; the marker is what routes it correctly. A junk number is refused
where it was typed rather than silently at send time. `email only` proceeds without one. Managing or
following up on an existing task is never interrupted by the prompt.

On a verified creation the reply states the delivery setup outright — the number, the address the
text will arrive **from** (an email-to-SMS gateway shows as an address, not a number, which reads as
spam unannounced), the email destination, and that only a check meeting the condition texts you.
That last line pre-empts the most common first-week misdiagnosis: a channel post arrives, no text
does, and the user concludes alerting is broken when the condition simply was not met.

Contacts and the display profile live in `/volume1/docker/openwebui/config/alerts/`, the only path
both sides reach — the pipe runs inside the container, the transports on the host. The directory is
owned by the host user, not root: an atomic `tmp+rename` needs write permission on the DIRECTORY,
and publishing the profile from the unprivileged delivery timer silently failed until it had one.

### What an alert actually says

A job emits a **payload** describing what happened; `scripts/alert_templates.py` turns it into the
three surfaces. The job no longer builds sentences, and the transports no longer slice log lines.

```
ALERT(ohmz): Hi ohmz, the listing you're tracking - Zakkart 2-Pack Cat Scratching Board - is $46.99, under your $50.00 target. Link in email.
ALERT_DATA: {"kind":"price_drop","item":"Zakkart 2-Pack Cat Scratching Board","value":46.99,"prev":49.99,"target":50.0,...}
```

**Both lines, deliberately.** The structured one produces the good text and the laid-out email; the
plain one guarantees that a watcher which did not understand it still delivers something. The
watcher drops the plain line for any recipient whose payload it did understand, so nothing arrives
twice.

**The assistant introduces itself.** `Hi ohmz, Ohmz AI here! …` — because these texts arrive from
a mail-to-SMS gateway, so the handset shows an email address the user has no reason to recognise.
Naming the assistant answers the first question a text from an unknown sender raises, which is what
those characters buy. The name is configured (`ASSISTANT_NAME`, default "Ohmz AI") and injected at
delivery, so renaming it never means editing a job. It is deliberately absent from the email
subject: those first ~45 characters are the notification preview, and the sender is already shown
there.

When the 140 characters run out, the personal greeting is dropped **before** the identity — on a
text from an address you don't recognise, "who is this" beats "hello by name".

**The item names itself.** `item` comes from the page's own `<title>`, cut to its first clause and
stripped of the site name — "Zakkart 2-Pack Cat Scratching Board, 65cm Tall Cardboard L Shape
Vertical Cat Scratchers for Indoor Cats" becomes something a person recognises on a lock screen. It
is remembered in the monitor's state, so a later *failure* alert can still say what it was watching.
No model is involved: a generated product name is a fabrication with extra steps.

Kinds — **17**, and this list was missing two of them until 2026-08-08: `price_drop` `price_rise`
`back_in_stock` `out_of_stock` `fare` `inventory` `availability` `threshold` `change`, plus the
problem kinds `unreachable` `blocked` `no_value` `not_found` `fare_unsupported` `fare_unreadable`
`fare_needs_itinerary` and the closing `recovered`. The two fare problem kinds are deliberately
distinct and the distinction is *the reader's next action*: `fare_unsupported` means no site can be
read and there is nothing the user can do, `fare_unreadable` means this particular page did not
parse, and `fare_needs_itinerary` means the request is missing an origin/destination/date and the
user can fix it by saying one. An **unknown kind renders generically rather than
raising** — a future job type reaches the user before anyone updates the file.

`back_in_stock`, `out_of_stock`, `inventory` and `availability` need `--mode stock` (above); passing
one without it auto-promotes and says so. `fare` is **refused** (above) — it is kept as a kind only
so the refusal can be worded as one.

The kind is chosen by keyword rules on the user's own words ("back in stock", "fare", "under 50"),
and only when the rules cannot tell does the agent pick one at job creation. That is the whole
extent of the model's involvement: it labels a category, once, and never supplies a number.

Decisions worth keeping:

- **The item name leads BOTH surfaces.** With several jobs running, *what* got cheap is the first
  question — so the SMS opens on the item and the subject opens on the kind then the item
  (`Price drop: Zakkart 2-Pack Cat Scratching Board is 46.99, under your 50 target`). The monitor
  name appears in **neither**; it is only a fallback when there is no item at all. Corrected
  2026-08-08 — this bullet used to say the monitor name led the email, which was the pre-templates
  behaviour and had already been replaced when it was written.
- **Confidence is printed on every run, including good ones** — if it only appeared on doubtful
  readings, its absence would need interpreting, and an omission would be indistinguishable from a
  bug. The text shows `(unconfirmed)`; the email explains *why* in full, because a caveat on a lock
  screen gets one glance and the measurement must not be pushed into truncation to make room.
- **Degradation has a fixed order.** Over 140 characters: shorten the item name, then drop the
  greeting, and never the pointer to the email — it is the only thing telling a first-time user
  where the link went. Rendering folds to ASCII *before* measuring, because the transport folds on
  the way out and measuring the prettier string let the transport's own truncation eat the pointer.
- **A page title is untrusted input.** It is HTML-escaped into the email; markup in a product name
  cannot inject.
- **The ledger records the body that was SENT**, not the one that was meant.

### The cancel link (cancel.ohmz.cloud)

_Installed 2026-08-09._ The email footer's "Cancel this monitor" is a real control, not a
sentence: `https://cancel.ohmz.cloud/c?t=<token>` renders a page for that ONE job — live name,
schedule, paused/active state — with **Cancel** (filled, destructive, full cleanup) and
**Pause/Resume** (quiet, reversible, cleans nothing). GET only ever shows the page; every
mutation is a POST from a human press, because mail scanners prefetch links and a mutating GET
would let Outlook cancel the monitor unread.

**The token is the whole authorization.** Stateless HMAC (`scripts/cancel_tokens.py`):
`b64url("1|job_id|handle|issued")` + 160-bit truncated HMAC-SHA256, minted in
`alert_transports.send_alert` (the one seam where the job id, the secret, and the payload
coexist), valid 30 days (`CANCEL_TOKEN_MAX_AGE_DAYS`). Two keys in
`~/.hermes/alert_transports.env`: `CANCEL_SECRET` (64 hex; **rotating it kills every
outstanding link at once — that is the kill switch**) and `CANCEL_BASE_URL`. The secret never
reaches SMS, subject, ledger, the at-rest retry queue, or the container-readable
`profile.json` — each absence is pinned by a test.

**Cancel does the full cleanup** the chat path historically skipped
(`scripts/cancel_service.py`, systemd user unit `cancel-service`, loopback `127.0.0.1:8096`
behind the cloudflared tunnel): read the job first (the prompt is the only record of the
watcher's `--state` slug and `--route-id`), DELETE in Hermes (404 = already gone; verified by
re-listing), then drop the `job_owners.json` entry, flip queued retries in `.alerts.json` to
`cancelled`, delete `~/.hermes/monitor-state/<slug>.*`, and untrack a fare watch's FlightClaw
route. The queue flip alone would not survive the delivery tick's last-writer-wins rewrite, so
the cancel also writes a **tombstone** to `~/.hermes/cron/output/.cancelled.json` that
`hermes_delivery.apply_cancel_tombstones` consults at the top of every tick — that contract is
what makes the kill stick regardless of write order.

**Public routing is a Cloudflare Zero Trust dashboard setting** (the tunnel is token-based; no
local ingress file): Public Hostname `cancel.ohmz.cloud` → `HTTP localhost:8096`. If
`curl -sI https://cancel.ohmz.cloud/healthz` answers with a redirect to `cloudflareaccess.com`
instead of `200`, a wildcard Access policy is covering the hostname and needs a bypass.

### The subscription confirmation

_Installed 2026-08-10._ Creating a monitor now sends a confirmation the moment it exists —
"Zakkart Cat Scratching Board is now being tracked" by email and text — the OTHER end of a
monitor's life from the cancel link above. It exists on the same two creation paths every other
task-mode feature does: the deterministic flight watch (`pipes/auto_assistant.py::_fc_make_watch`)
already holds the structured itinerary (route, target, dates) at the moment it creates the job, so
the confirmation carries all of it; the agent-delegated general watch
(`_enqueue_subscriptions_for`, fired from `_hermes_stream`'s attribution success branch) only has
the job's name, schedule and its own vetted command line to work with, so `_job_flag_value` pulls
`--url`/`--below`/`--unit` out of the prompt best-effort — a value not found is simply left off,
never guessed.

**Why this isn't a direct send.** The OpenWebUI container holds no SMTP/Twilio credentials —
deliberately, the same reason `alert_transports.publish_profile` exists — so the pipe cannot email
or text anyone itself. It can only append to a shared one-shot inbox,
`SUBSCRIBE_INBOX_FILE`/`SUBSCRIBE_INBOX` (`.../alerts/pending_subscriptions.json`, same directory
as `job_owners.json`), and `hermes_delivery.drain_subscriptions` picks it up on its very next tick
(≤60s) and hands the payload to `alert_transports.send_alert` exactly like a price-drop alert —
same brand template, same cancel link, same retry-on-failure, for free, by looking like one more
alert rather than a special case.

**The drain runs BEFORE the tick's early-return**, not after: a brand-new monitor with nothing
else due that minute is exactly the case "no new output files and nothing already pending" was
written to catch, so draining anywhere later would have silently starved every confirmation on an
otherwise-idle box. A dry run drains nothing — it must never mutate the real inbox.

**Two writers, one file, one lock.** The pipe (container) appends and the watcher (host) reads and
clears, in separate processes with no shared Python state — an flock on a `.lock` sidecar, taken
by both sides for the full read-modify-write, is what stops a clear from landing between another
process's read and write and silently discarding a fresh append. The queue key is a hash of the
job id and the payload, so a not-yet-cleared inbox re-read after a crash can never double-send.

**Cancelled before it ever ships?** The drain runs ahead of `apply_cancel_tombstones` in the same
tick, so a job cancelled in the same window it was created dies under the identical tombstone
check as any other pending alert — no special-casing a confirmation for a monitor that is already
gone.

**The whole tick is now one lock, not just the inbox.** `main()` used to load and save
`.alerts.json` with no lock of its own — safe only as long as exactly one process ever touched it.
Building this feature broke that assumption live: the 60s systemd timer and a manual
`python3 hermes_delivery.py` run overlapped in the same minute during development, and whichever
saved last would have silently erased whatever the other had just delivered. `main()` now holds
`.alerts.json.lock` for the entire non-dry-run tick (`--dry-run` performs no writes, so it takes no
lock and never blocks). A freshly-drained confirmation is also persisted immediately, not only at
the tick's end — the end-of-tick save comes after the real SMTP/SMS sends, the likeliest place for
a hang or a kill to land, and the source inbox entry is already gone by then.

### When the monitor itself breaks

A monitor that silently stops working is worse than one that never existed, because it is trusted.
Three failure shapes each get their own message and their own advice:

| shape | detected as | told after |
|---|---|---|
| dead URL | 4xx, or a host that no longer resolves | 2 consecutive |
| site blocking checks | a robot wall instead of the page | 2 consecutive |
| transient | 5xx, timeout, reset | 3 consecutive |
| **loads, but no value readable** | page fetched, extractor finds nothing | 3 consecutive |

A single failure never alerts — blips happen, and a system that texts about them gets muted. After
the threshold it alerts **once per outage**, then stays quiet until it recovers; a broken URL texting
every 6 hours for a week trains the user to ignore the channel. Recovery closes the loop, unless the
condition also fired on that run — in which case one message covers both, since the target alert
already proves the monitor works.

Status alone cannot decide this: a dead amazon.ca product returns **HTTP 500**, not 404. So 4xx and
an unresolvable host are permanent; everything else gets more patience.

### Two more transport decisions

*This heading used to be a second `### What an alert actually says`, and it described the
**pre-`alert_templates.py`** surfaces — `amazon B0DP6D3TRB price: 46.99, …`, with the monitor name
leading and the source ID spelled into the subject. That format was replaced by the section above,
which contradicted it outright, so two sections with the same title told a reader opposite things
depending on which one they scrolled to first. Removed 2026-08-08; the two bullets below were the
only content unique to it, and both are still true.*

- **Everything is folded to ASCII** (`_SMS_ASCII` / `_ascii()` in `alert_templates.py`). A carrier
  gateway is a mail bridge with no promise of UTF-8; a mangled em dash undoes the link-stripping work
  by making the text look broken anyway.
- **The ledger records the body that was SENT.** It used to record the original alert text, so a
  text mangled in transit looked flawless in the one place an operator would check.

### SMTP acceptance is not deliverability

The relay accepting a message says nothing about whether a mailbox exists. Alerts were addressed to
`ohmz@ohmz.com` — the OpenWebUI login domain, which **has no MX record**. Gmail accepted every one,
discovered there was nowhere to deliver it, and bounced asynchronously to the sending account, where
nothing is watching. The ledger recorded "email sent" every time.

This is the same silent-loss shape as the SMS gateway eating links, and it became load-bearing the
moment texts started dropping URLs and saying "(link in email)": an undeliverable email leg leaves
the user holding a pointer to nothing.

`mail_domain_status()` classifies the domain before sending:

| | meaning | behaviour |
|---|---|---|
| `ok` | has MX records | send normally |
| `implicit` | no MX, but an A record | send, and mark the note **UNVERIFIABLE** — RFC 5321 sends mail to that host, which for a parked domain speaks no SMTP |
| `dead` | no MX, no A | refuse; record the reason instead of spending an "ok" on it |
| `unknown` | no resolver available | send — a missing `dig` must never stop an alert |

Addresses are per-handle in **`/volume1/docker/openwebui/config/alerts/contacts.json`** — the
shared file, readable from inside the container as `/app/backend/data/alerts/contacts.json`. It
overrides the OpenWebUI lookup.

> `~/.hermes/alert_contacts.json` is only a **fallback** for un-migrated installs, and
> `load_contacts()` returns the FIRST readable file *whole* — it does not merge. On this box the
> shared file exists and wins, so editing the `~/.hermes` copy changes nothing. Earlier revisions
> of this doc pointed here, which is why that matters. Check which one is live before editing:

```bash
python3 -c "
import json, os
for p in ('/volume1/docker/openwebui/config/alerts/contacts.json',
          os.path.expanduser('~/.hermes/alert_contacts.json')):
    try:
        d = json.load(open(p)); print(f'WINS -> {p}: {sorted(d)}'); break
    except Exception as e: print(f'skip    {p}: {type(e).__name__}')
else: print('neither readable; falling back to the OpenWebUI user table')
"
```

It walks the same two paths in the same order as `load_contacts()` and stops at the first readable
one, so what it prints *is* what would be used. Until 2026-08-08 the command here read only the
`~/.hermes` fallback — the file the paragraph above had just finished explaining never wins — so the
one command offered for "check which is live" could not answer that question, and answering it wrong
is worse than not offering it. Verified on this box: the shared file wins.

### Un-substituted alert templates are never delivered

A model that echoes the protocol example instead of filling it in produces a syntactically perfect
ALERT line. One reached a real phone as
`<what happened, with the number>   (ONLY in a run where the user's alert condition holds)`.
Nothing downstream can distinguish that from a genuine alert, so the watcher drops any alert body
still carrying an angle-bracket placeholder — and says so in the channel log, because a silently
suppressed alert would make a broken job look like a calm one. Arithmetic (`price < 50`) is not
mistaken for a placeholder; `tests/test_hermes_delivery.py` pins both directions.

### Follow-ups in a task conversation

Every hermes reply ends with an invisible `<!--bg-task-->` marker. A short next message ("yes
reenable", "go ahead", "the first one") is routed back to hermes **only when the previous assistant
turn carried that marker** — otherwise the same words are ordinary chat. The prior exchange is sent
along so the answer has a referent, and the agent is told to call `cronjob(action='list')` and work
from real scheduler state.

This exists because of a live failure: the agent asked "re-enable this one, or create a new pair?",
the user answered "yes reenable", the message matched no task predicate, went to the **chat** model
— which read the job id out of the transcript and produced a confident confirmation of something it
had not done and could not do.

### Jobs belong to a user, even though hermes has no idea who that is

A hermes job record carries no owner: `origin.user_id` is `None` for everything created through the
API server, the API key is shared, and `GET /api/jobs` returns every job on the host. That is fine
for one person and wrong the moment there are two — one user could list, read and cancel another's
monitors.

Ownership is therefore recorded **pipe-side**, in
`/volume1/docker/openwebui/config/alerts/job_owners.json`:

```json
{"6dc7813ef231": {"h": "omariqbal97", "t": 1785737368.4, "src": "seed"}}
```

The stamp happens in `_hermes_stream`: the pipe already snapshots `/api/jobs` before and after every
delegation to verify creation, so the ids that appear in that diff are the ids this turn created.
Where the agent printed a real id in its reply (brief rule 9) that id is preferred over the bare
diff — `src` records which, so a stamp made on the weaker signal is auditable. `h` is the handle
from `_alert_username`, i.e. the email local part.

Why not patch hermes: its REST `PATCH` whitelist rejects unknown keys, the agent's `cronjob` tool
cannot set them, and the pipe cannot import hermes across the container boundary. The vendored
checkout is a plain `git pull --ff-only` clone, so a local patch is one `hermes update` away from
being stashed or reset. The sidecar needs none of that and is readable by the host-side delivery
timer, which is the other thing that needs it.

Consequences, all covered by `tests/test_task_ownership.py`:

* an ordinary user's list, reference resolution and every write are filtered to their own jobs;
* an admin (OpenWebUI `role`, or a handle in `TASK_ADMINS`) sees everything, with an Owner column;
* an **unowned** job is admin-only — never adopted by whoever asks first;
* a read-only turn from an ordinary user is **never** delegated to the agent, because the agent's
  job list is the whole host and it has no notion of who is asking. With `MANAGE_DETERMINISTIC`
  off they get a refusal rather than a fallthrough; admins keep the old behaviour;
* if the map is unreadable, an ordinary user is told so. Not an empty list ("you have nothing
  scheduled" is the one lie that matters here) and not the unfiltered host list.

Known limits, stated rather than hidden: the handle is an email local part, so `alice@a.com` and
`alice@b.com` collide and every account without an email shares `user` — the same key the alert
contacts already use. The before/after diff is host-wide, so two simultaneous creations can
cross-stamp; the cited-id preference shrinks that window and `src` makes it repairable. A turn the
user disconnects from cannot stamp at all, leaving that job unowned until an admin assigns it. And
this protects OpenWebUI users from each other — anyone with the hermes key on the host still sees
everything.

### Results go to the owner's channel

`LOG:` output used to post to a single shared background-tasks webhook, so every user read every
other user's monitor results. `hermes_delivery.py` now resolves the job's owner from the sidecar
above and looks the handle up in
`/volume1/docker/openwebui/config/alerts/owner_channels.json`:

```json
{"omariqbal97": "http://<host>/api/v1/channels/webhooks/<id>/<secret>"}
```

Per user, one-time, in the OpenWebUI admin UI: create a channel (e.g. `tasks-alice`), add that user
and the admins, create a channel webhook, paste the URL under their handle.

An unowned job, an owner with no channel mapped, or a corrupt map all fall back to the original
shared webhook. That fallback is deliberate — a routing miss must never *drop* a result — which is
why the shared channel should be restricted to admins. The ALERT leg (text/email) was already
per-user and is unchanged.

## Rollback

```bash
systemctl --user disable --now hermes-gateway     # stop the agent entirely
ollama rm hermes-genesis:agent                    # drop the 64K tag (weights stay via apex-compact)
```
The GPU guard rolls back separately, cheapest first — none of these touch the pipe, the container
or Ollama:

```bash
# 1. neuter the guard but keep the provider (escape fires immediately => effectively fail-open)
systemctl --user set-environment HERMES_GPUGUARD_MAX_DEFER_S=0 HERMES_GPUGUARD_HARD_DEFER_S=0
systemctl --user restart hermes-gateway
# 2. drop back to the stock ticker: set `cron.provider: ""` in ~/.hermes/config.yaml, restart
# 3. revert the plugin itself: git checkout hermes/plugins/gpuguard
```

Set `BG_TASKS = False` in the pipe to disconnect the route without touching hermes. Channels off:
`channels.enable=false` in the OWUI config table (webhook rows in `channel`/`channel_webhook` are
inert while disabled; DB backup at `webui.db.bak-channels`). Full uninstall: `hermes uninstall`.
The delivery watcher is its own timer: `systemctl --user disable --now hermes-delivery.timer`.

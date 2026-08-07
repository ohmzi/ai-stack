# A flight path of its own: deterministic flight routing, and a fare capability that is measured before it ships

> ## ⛔ THE PHASE 1D GATE RETURNED ZERO. PHASES 2 AND 3 ARE ON HOLD.
>
> Measured 2026-08-07 across all 19 sites, plain tier and browser tier: **11 blocked, 6 never
> fetched, 2 wrong-role, 0 readable.** skyscanner.ca was the last candidate and rendering it produced
> a PerimeterX challenge. See **`docs/FLIGHT_RECON.md`** for the evidence and the remaining options.
>
> Per this plan's own Phase 1d, written before anything was measured: *"0 → stop and report. Do not
> ship a flight watcher with nothing behind it."* So Phase 2 shipped as **inert** code and **Phase 3
> (the pipeline flight path) was deliberately not started** — wiring the assistant to a watcher with
> nothing behind it would be building on a guess.
>
> What IS built and tested: `flight_probe.py` (62 checks), `flight_watch.py` (73), `flight_render.py`,
> the registry, and the alert kinds. What is NOT built: every part of Phase 3, and the tests/ files
> for Phase 4 (coverage currently lives in each script's `--selftest`).
>
> Phase 1e (date flexibility) is fully designed below and **implemented in `flight_watch.py`**, but no
> site's `date_flex` could be measured, so the ladder resolves against nothing today.
>
> ---
>
> **STATUS: PLAN, NOT BEHAVIOUR. Nothing here has shipped.** Unlike `HERMES_AGENT.md` or
> `TRACKING_ENHANCEMENT.md`, which record what the stack *does*, this file records what it is
> *going to* do and why. Today a flight watch is still refused by `scripts/price_search.py:354`.
>
> Two files exist so far, both inert: `scripts/flight_sites.json` (all 19 sites, every
> `verdict: "untested"`, so nothing is queried) and `scripts/flight_render.py`. No routing has
> changed, no pipe has been redeployed, and no fare has been read.
>
> The go/no-go gate in Phase 1d is real: if reconnaissance clears zero usable sites, the correct
> outcome is to keep the refusal and ship the findings. Do not read this document as a commitment
> that flight fares will work.
>
> Written 2026-08-07. Supersedes nothing; extends `TRACKING_ENHANCEMENT.md`'s "Still open" item 2
> ("a fare capability, if wanted at all") and claims brief rule slot `5d-iv`, reserved for exactly
> this at `pipes/auto_assistant.py:3173`. Sections marked *"Corrected during implementation"* are
> kept deliberately — the mistake and its correction are both part of the record, which is the same
> reason `TRACKING_ENHANCEMENT.md` keeps its negative results.

## Context

Asking this assistant to watch a flight currently produces a monitor that cannot work. That is by
design, and the design was right at the time: `scripts/price_search.py:354` `refuse_fare()` refuses
every `--kind fare` job before spending a search, because on 2026-08-07 job `52f821a8d3a2` resolved
a cheapflights.ca page and reported **"$358.72, under your $1,000.00 target" at high confidence** —
read out of a JSON-LD array of 97 unrelated itineraries on a page whose own title said "C$ 146+".
Every guard was satisfied and the number was meaningless. `docs/TRACKING_ENHANCEMENT.md` records the
diagnostic that settled it: `{"rows": 10, ..., "reason": "no_candidate"}` — discovery worked,
**extraction** was the gap, and "more engines was never the fix for fares."

That job is still on the box, firing the refusal every 15 minutes.

What changes now is the shape of the question. A fare cannot be read off a *search result*, because a
search result is about no itinerary in particular. A fare **can** be read off a page that was
requested for one specific origin, destination and pair of dates — the itinerary is in the URL, so
any fare on that page is about it by construction. That is the whole argument for this feature, and
it is why the work has to start by collecting the itinerary rather than by scraping.

So this plan does four things the user asked for:

1. **A flight path in the pipeline that is deterministic and separate from item/stock checking** — it
   recognises a flight ask, fills the itinerary slots (origin, destination, depart, return, target
   price, contact) over as many turns as it takes, and only then acts.
2. **All 19 named sites entered into a registry** that records, per site, how to query it and whether
   it actually works — including the ones that turn out not to.
3. **Real reconnaissance before production**, because `docs/TRACKING_ENHANCEMENT.md` exists precisely
   to stop this stack shipping assumptions. Recon is Phase 1, not an afterthought, and it has a
   go/no-go gate.
4. **A deterministic answer when the ask names a month instead of a day** (Phase 1e). "Flights to
   Tokyo in March" is not vague — it is what every serious flight site sells, via whole-month and
   flexible-date search. A four-rung ladder, chosen per site from a *measured* capability field, turns
   that into a real query instead of a question back to the user; and because the fare then comes off
   a page showing many itineraries, the dates it was found on become mandatory output on every
   surface, so the user can reproduce the deal rather than re-hunting the month by hand.

Decisions taken with the user: a flight ask **answers now and offers a watch**; a **target price is a
required slot**; recon runs **in real Chrome first, then confirms headless**; the pipe **creates the
cron job itself over REST** rather than delegating to the Hermes LLM; **exact dates stay exact** (flex
is the fallback for missing dates, never an override of given ones); **trip length is asked once** when
a two-month round trip would otherwise be an unbounded query; and a **weakest-rung reading logs but
never texts on its own**.

---

## What the live system actually is (verified 2026-08-07, not assumed)

| Fact | Consequence for this plan |
|---|---|
| Playwright 1.49 + Chromium import fine under `/usr/bin/python3`; **fail** in the Hermes venv (3.11) | Every browser call is a subprocess with the interpreter spelled out. Never `python3`. |
| `which -a python3` → `/usr/bin/python3` first *in this shell* | Not trustworthy for a cron tick. Belt and braces: hardcode the path **and** verify by firing a real job. |
| `POST /api/jobs` takes `{name, schedule, prompt, deliver, repeat}`; name ≤200, prompt ≤5000; every prompt passes `_scan_cron_prompt` | A `flight_watch.py --origin … ` command trips none of the threat patterns (checked: they target `cat .env`, `rm -rf /`, curl-exfil). The deterministic path is clear. |
| Hermes `api_server` toolset is `[web, file, memory, session_search, todo, cronjob, skills]` — **no terminal, no browser** | The chat-facing agent can never run the extractor. "Answer now" cannot go through `_hermes_stream`. |
| `/volume1/docker/openwebui/config/alerts/` exists, owned `ohmz:ohmz`, and is the container's `/app/backend/data/alerts/` | This is the pipe↔host channel that already carries contacts. It is how "answer now" returns a fare **without granting Hermes terminal access**. |
| OpenWebUI **escapes HTML comments wherever they appear** (`auto_assistant.py:1354-1365`) | Slot state must live in the pipe's per-chat dicts (`self._phone_ask` precedent, `__init__:459`), never in a message marker. |
| Live job `52f821a8d3a2` is a `price_search --kind fare` job hitting the refusal | Delete it; offer to recreate as a flight watch. Note `YTO` is a metro code — the new extractor must accept metro codes (YTO/NYC/LON). |
| SearXNG `:8889` and Hermes `:8642` are **unreachable from Claude's sandbox** (localhost blocked, no Docker socket) | Every live check in Phase 0 must be run by the user with `!` or by a Hermes job. Do not claim a check passed that could not run. |

---

## Phase 0 — Verify the running stack (the "figure out the tools and system" half)

Run these first; each one has already burned time in this repo's history by being assumed. To be run
outside Claude's sandbox (prefix with `!`):

```bash
# 1. Is Google actually in the monitors-only roster? docs/TRACKING_ENHANCEMENT.md:55 says it went
#    missing silently and the fix in a1b4558 is UNVERIFIED. "Nothing in that file fails loudly."
curl -s localhost:8889/config | python3 -c 'import json,sys; print([e["name"] for e in json.load(sys.stdin)["engines"]])'

# 2. What Hermes will actually grant a job
KEY=$(grep -E '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $KEY" localhost:8642/v1/toolsets
systemctl --user is-active hermes-gateway hermes-delivery.timer

# 3. THE critical one: can a cron tick reach Playwright? Fire a throwaway job whose prompt is
#    `/usr/bin/python3 -c "import playwright; print('PW OK')"` and read its output. PATH inside a
#    cron tick is not this shell's PATH, and this is the assumption that would fail in production.
```

Record the answers in `docs/FLIGHT_FARES.md` before writing any extractor. If (3) fails even with the
explicit interpreter, the whole capability is blocked and the honest outcome is to say so — the
existing refusal is a complete outcome, as `docs/TRACKING_ENHANCEMENT.md:90` already says.

---

## Phase 1 — Reconnaissance of the 19 sites

> **Already on disk from the first implementation pass, and needing the Phase 1e fields added:**
> `scripts/flight_sites.json` (all 19 sites, every `verdict: "untested"`, so nothing is queried yet)
> and `scripts/flight_render.py` (the Playwright subprocess, typed exit codes 3/4/5/6, `killpg`-safe).
> Both need the `date_flex` / `month_url_template` / `range_url_template` / `calendar` fields and a
> `--wait-selector` path for calendar reads. No other flight file exists yet.

### 1a. The registry: `scripts/flight_sites.json` (new)

The repo has no site-list config today (domain policy is Python tuples: `DEFAULT_PREFER`
`price_search.py:119`). A registry is warranted here because its contents change when *sites* change,
not when logic changes, and because a human needs to read the verdicts. It is **committed and
hand-reviewed** — the probe writes a report, a person folds it in. No script rewrites committed config.

```json
{
  "version": 1,
  "canonical_probe": {"origin": "YYZ", "dest": "YVR", "depart": "2026-09-15", "ret": "2026-09-22"},
  "sites": [
    {
      "domain": "kayak.com",
      "name": "Kayak",
      "role": "itinerary_search",
      "owner": "booking_holdings",
      "currency": "USD",
      "url_template": "https://www.kayak.com/flights/{origin}-{dest}/{depart}/{ret}?sort=price_a",
      "one_way_template": "https://www.kayak.com/flights/{origin}-{dest}/{depart}?sort=price_a",
      "needs_browser": true,
      "extract": {"kind": "dom", "wait_selector": "…", "fare_selector": "…", "json_path": null},
      "date_flex": "whole_month",
      "month_url_template": "https://www.skyscanner.ca/transport/flights/{origin_lc}/{dest_lc}/{depart_ym}/{ret_ym}/",
      "range_url_template": null,
      "calendar": null,
      "code_map": null,
      "caveat": null,
      "verdict": "untested",
      "measured_at": null,
      "notes": ""
    }
  ]
}
```

Four fields carry most of the value:

- **`role`** — the user's list is not homogeneous, and pretending it is would build a lie into the
  data. `itinerary_search` sites can answer "YYZ→YVR Sep 15–22". **`secretflying.com` and
  `travelpricedrops.com` cannot** — they are deal/error-fare *feeds* with no origin/destination form,
  so they get `role: "deal_feed"` and a `feed_url`, and participate as a cheap secondary signal
  (scan feed items for the watched route) rather than as a fare source. `redirect_only` covers any
  site recon finds is a referral shell with no fares of its own.
- **`owner`** — Booking Holdings owns kayak/momondo/cheapflights/priceline; Expedia Group owns
  orbitz/travelocity; Fareportal owns cheapoair/onetravel. They share anti-bot stacks, so **two
  agreeing sites under one owner are not independent confirmation**, and one blocking predicts its
  siblings. The quorum logic in Phase 2 reads this field.
- **`currency`** — a `$` on orbitz.com is USD and on travelocity.ca is CAD. Comparing a USD fare
  against a CAD target is a wrong alert that looks perfectly reasonable. Unlabelled currency is a
  hard reject, not a guess.

`caveat` exists for `skiplagged.com`: its fares are real but hidden-city, which voids checked bags and
breaches airline terms. If it ships, the alert says so.

All 19 domains go in, whatever their verdict. A set-equality test (Phase 4) makes it impossible to
quietly drop one.

### 1b. Chrome inspection (per the user's choice: real browser first)

Using the `claude-in-chrome` tools against the canonical itinerary **YYZ→YVR, depart 2026-09-15,
return 2026-09-22** (~5 weeks out, dense route, plenty of inventory). Per site:

1. `navigate` to a guessed deep link with the slots in the URL. **Does it skip the search form?** A
   site that requires form interaction is a different, much more fragile class of automation — record
   that verdict rather than building it.
2. `read_page` / `find` the cheapest-fare element; record a selector that is anchored to structure,
   not to a generated class name.
3. `read_network_requests` for an XHR/JSON response carrying fares. **A JSON endpoint beats DOM
   scraping every time** and is what makes kiwi.com, skiplagged.com and trip.com worth trying first —
   their own frontends call one.
4. Note consent/geo interstitials (`.ca` vs `.com` redirects, cookie walls) — these are what break a
   headless run that worked interactively.
5. **Establish the site's `date_flex` rung** (Phase 1e): does it offer a whole-month / flexible-dates
   search, and can that be expressed in the URL? Skyscanner's `YYYYMM` date segment is the shape to
   look for. If not, does it accept an explicit from–to window? If not, does it render a **price
   calendar** whose cells carry both a price and a date? Whichever holds, capture the URL form and the
   selector. This is the single most valuable thing Chrome inspection can find that a headless probe
   cannot, because a flexible-date control is usually a UI affordance whose URL form only becomes
   visible once you click it and watch the address bar.

Politeness and honesty: one canonical itinerary per site, sequential, no hammering. This uses the
user's real browser session, so **everything it finds is unproven under automation** — which is
exactly why 1c exists.

### 1c. Headless confirmation: `scripts/flight_probe.py` (new)

Run under `/usr/bin/python3`. Per site, in registry order, one attempt each:

- Tier 1: plain `urllib` with `price_watch.HEADERS` (free; a few sites may server-render enough).
- Tier 2: Playwright headless via the Phase 2 browser subprocess, only if Tier 1 found no fare.

Report schema (written to `docs/flight-recon/<UTC>/probe.json` alongside a `findings.md`, summary
table to stdout; `--save-html` additionally dumps the page bodies to `pages/`, which is gitignored
because each is up to 512 KB of a third party's HTML — the ones worth keeping get trimmed into
`tests/fixtures/flights/` by hand):

```json
{"domain": "kayak.com", "tier": "browser", "http": 200, "bytes": 412881,
 "robot_wall": false, "consent_wall": true, "distinct_prices": 34,
 "fare_found": true, "fare": 289.0, "currency": "USD", "matched_itinerary": true,
 "teaser_rejected": 2, "path": "dom:[data-resultid] .price-text",
 "ms_to_fare": 8400, "verdict": "usable_with_browser", "error": null}
```

**The probe is run twice: once directly on the host, and once as a real Hermes cron job.** That second
run is the point of the user's "so it's not an issue in production" — it is the only thing that proves
the interpreter, the PATH, the 180 s tool ceiling and the output protocol all hold on the path a live
watch will actually take. A probe that passes only when a human runs it has proven nothing.

**The probe also measures `date_flex`, and it measures it rather than trusting the registry.** For each
site it fetches the exact-date URL *and*, where a `month_url_template` is authored, the whole-month URL,
and records which rung actually produced a fare bound to a date pair. A `date_flex: "whole_month"` claim
that yields no per-itinerary dates is demoted to `calendar` or `none` in the proposal — a flexible
search whose results cannot be tied to dates is useless to this design, however well it renders.

**Teaser control, adapted for flex mode.** The exact-date control is a *different date pair*; the
month-mode control is a **different month**. An identical minimum returned for March and for May is not
a fare, it is a teaser or a cached page — and that is a measurement, not an opinion. This is the
strongest automatable teaser test available and it is mandatory before any site ships.

Discipline: ≥5 s between sites, one request per site per run, `--only <domain>` for re-tests, hard
per-site timeout, whole run bounded. `distinct_prices` reuses the `LISTING_MIN_PRICES` insight
(`price_watch.py:129`) — but here a high count is *expected* (a results page legitimately lists many
itineraries), so the guard inverts: we take the **minimum of validated itinerary fares**, not "the
page's one price". That inversion is the substantive difference from `price_watch.py` and belongs in
the docstring.

**Expect most of these to fail.** Kayak and Skyscanner are among the most headless-hostile sites on
the web; Expedia-group and Booking-group properties fingerprint aggressively. A verdict of `blocked`
with a measured reason is a *successful* probe result and gets written down, per this repo's habit of
recording negative results.

### 1d. Go/no-go gate

After 1c, count sites with `verdict ∈ {usable, usable_with_browser}` that are **not owner-siblings**:

- **≥2 independent** → proceed to Phase 2/3 as planned.
- **exactly 1** → proceed, but the docs and the confirmation message say plainly that the capability
  rests on one site and will break; the watch reports its source every run.
- **0** → **stop and report.** Do not ship a flight watcher with nothing behind it. Keep the refusal,
  update its wording to name what was tried, and hand the user the finding. This is the outcome
  `docs/TRACKING_ENHANCEMENT.md` was written to make sayable.

Deliverable: `docs/FLIGHT_FARES.md` — the per-site table, the Phase 0 answers, what was refuted, and
an explicit statement that a fare capability is a maintenance subscription rather than a feature.

---

## Phase 1e — Date flexibility: when the ask names a month, not a day

**This supersedes the earlier decision in 3c that a bare month must be asked about.** A traveller who
says "flights to Tokyo in March" has not been vague — they have said something precise and useful:
*any* March. Every serious flight site sells exactly that (Skyscanner's whole-month search, Google's
date grid, Kayak's cheapest-month view), so refusing to use it and asking for a day instead throws
away both the user's actual intent and the sites' best feature. The ladder below makes month-shaped
asks deterministic rather than conversational.

### The precision model

`depart` and `ret` stop being dates and become **date specifications**, each with a precision:

| precision | produced by | example |
|---|---|---|
| `exact` | a full date | `Sep 15`, `2026-09-15`, `next Friday` |
| `range` | a part-month or an explicit window | `first week of September`, `early March`, `Mar 10–17` |
| `month` | a bare month | `in March`, `sometime in June`, `March out, April back` |

`exact` behaves exactly as already planned and is untouched — **exact dates stay exact** (the user's
decision). `range` and `month` enter the ladder. A **season** (`in the spring`, `sometime in the fall`)
still asks: three months is not a query any site can express, and the ladder would silently pick one.

### The ladder — four rungs, chosen per site from the registry

Which rung a site takes is a **measured capability**, not a guess. The registry field is `date_flex`.

| rung | `date_flex` | what the watcher does | `date_basis` recorded |
|---|---|---|---|
| **1** | `whole_month` | Use the site's own whole-month/flexible search. Skyscanner is the canonical case: its date segment accepts `YYYYMM` (`/transport/flights/yyz/yvr/202603/202603/`). Best evidence available — the site itself is answering "cheapest in March". | `native_month` |
| **2** | `range` | Send an explicit from–to window the site accepts. Requires a bounded stay length (below). | `explicit_range` |
| **3** | `calendar` | No flex query, but the site renders a price calendar. Read it and take the **cheapest cell together with the date it sits on**. | `calendar_cheapest` |
| **4** | `none` | Nothing flexible and no readable calendar: price **1st of the month out, last of the month back**. | `assumed_month_bounds` |

Rungs are per site, not global: one watch can be reading rung 1 on Skyscanner and rung 4 on OneTravel
in the same run, and the payload records which was used where.

### The rule that makes month mode safe — and it is the whole ballgame

In exact mode, validation rested on one structural fact: the itinerary is in the URL, so a number on
the page is about that itinerary by construction. **Month mode gives that up** — a whole-month results
page legitimately shows dozens of itineraries, which is precisely the shape of the page that produced
`$358.72` on 2026-08-07.

So binding moves from the URL to the element:

> **A month-mode fare is valid only if the fare and its own departure and return dates are extracted
> together, as one tuple, from one itinerary element. A bare minimum price with no dates attached is
> rejected — it is the 97-price array again in a new costume.**

Everything else follows from that:

- The `itinerary_echo` gate changes shape rather than being dropped: origin, destination and the
  **month** must be echoed by the page, and each candidate fare must carry a date pair *inside the
  requested month*. A fare whose extracted dates fall outside the month is a neighbouring-month
  suggestion the site volunteered, and is discarded.
- `LISTING_MIN_PRICES` inverts explicitly here, as already noted for the probe: many prices is the
  *expected* shape. The guard becomes "many prices, each bound to its own dates" — and if the dates
  cannot be recovered, the page yields nothing rather than a minimum.
- Rung 4 is the one rung with no list to bind against, because it queries a single pair. It is
  therefore validated exactly like exact mode — and it is also the weakest evidence, which is what the
  confidence rules below are for.

### Confidence, and what may text

`date_basis` caps confidence, composing with the quorum design already in Phase 2:

| `date_basis` | confidence ceiling | may text alone? |
|---|---|---|
| `native_month`, `explicit_range` | `high` | yes, once quorum is met |
| `calendar_cheapest` | `medium` | no |
| `assumed_month_bounds` | `medium`, and flagged | **no — logged only** (the user's decision) |

With `--require-confidence` on by default for fares, a rung-3 or rung-4 reading can never fire an SMS
on its own; it needs a rung-1/2 source to agree. The reason is worth stating in the docstring: rung 4
prices Mar 1 → Mar 31, and if that pair is $412 while Mar 8 → Mar 15 is $280, then texting "$412 for
March" is not merely imprecise — it would cause the user to book the worse fare believing it was the
best one available.

**Rung 4 may never use the word "cheapest."** Its wording is *"priced Mar 1 → Mar 31, the only dates
this site would quote"*. Rungs 1–3 may say *"cheapest in March"*, because that is what they measured.

### Cross-site agreement when the dates are not fixed

Two sites searching March will often return **different date pairs**. That is not disagreement — both
are claims about the month's floor, and they agree if their values are within `AGREE_PCT`. But the
*bookable* result must come from one site:

- Quorum validates the **magnitude** ("about $420 is achievable in March").
- The reported fare is the **minimum single reading, carrying its own site's date pair**. Values are
  never blended, and a date pair is never taken from a different site than the number beside it.
- The payload records every site's `(value, depart_found, ret_found, date_basis)` so the email shows
  the spread and the reader can see the two sites found different weeks.

### Bounding the combinatorics

A whole-month × whole-month round trip is ~900 date pairs, which most sites will not quote. So:

- **Trip length is asked for once** when two months are given with no duration (the user's decision):
  *"roughly how long is the trip?"* It becomes `--trip-days N`, and rung 2's window is
  depart-month × (depart + N ± `--trip-flex`, default 3).
- Without `--trip-days`, **rung 2 is skipped entirely** — an unbounded range query is meaningless, and
  attempting it would either time out or return a number bound to nothing. The site falls to rung 3,
  then 4.
- `"for a week"`, `"10 days"`, `"a long weekend"` (→ 3) already parse today and supply `--trip-days`
  without a question. A one-way month ask needs no trip length at all.
- Per-run caps stay hard: `--max-sites 3`, one browser load per site, the `RUN_DEADLINE_S` budget
  under the Hermes terminal tool's 180 s ceiling, and the `BROWSER_MIN_INTERVAL_S` clamp. A month
  watch is inherently heavier than an exact-date watch and must not be allowed to grow into the run.

### Month parsing (deterministic, in the pipe)

- `in March` → the **next** occurrence: March 2027 if March 2026 is already past. `next March` explicit.
- `March out, back in April` → two month specs; cross-month round trips are allowed.
- Depart month given, return unstated, round trip → **return month = depart month**, stated in the
  confirmation. A same-month round trip is overwhelmingly the common case, and saying so lets the user
  correct it in one word.
- `early|mid|late March`, `first|last week of March` → `range`, not `month`.
- A month already mostly past (asking for "March" on March 28) → the remaining days only, and the
  confirmation says so.
- Seasons and bare years → ask.

### What the user sees

The confirmation states the mode and the ladder outcome plainly, because a watch whose dates were
chosen by a fallback rung must not look like a watch on dates the user picked:

```
| Out | all of March 2027 (flexible) |
| Back | all of March 2027 · trip ~7 days |
| Dates chosen by | Skyscanner's own whole-month search · 2 other sites priced Mar 1–31 only |
```

### Every surface must carry the dates that were found — this is a hard requirement

A month watch that texts *"March is $412"* has told the user a number they cannot act on. They then
have to re-search the whole month by hand to find which week it was, and if they guess a different
week they conclude the alert was wrong. **The dates the fare was found on are not a detail, they are
the deliverable** — without them the user cannot reproduce the deal, and the alert is worse than
silence because it looks actionable and is not.

So `depart_found` / `ret_found` are **mandatory** in any month- or range-mode payload, and every one
of the three surfaces renders them. An emitted month-mode payload missing either is a bug, pinned by
a test.

**SMS** (140 ASCII, no links — carrier gateways silently drop texts containing them, `URL_RE` :338).
The item string is built short and **dot-free** on purpose: `alert_transports.URL_RE`'s bare-domain
branch deletes dotted tokens, so `Mar. 8` would be eaten while `Mar 8` survives.

```
Hi ohmz, Ohmz AI here! YYZ-YVR Mar 8-15 is $412.00, cheapest in March,
under your $600.00 target. Dates and link in email.                        (~118 of 140)
```

The year is appended (`Mar 8-15 2027`) whenever the month is not in the current year; it is dropped
when it is, to buy back budget. `render_sms`'s existing degradation ladder (shorten item → drop
greeting → drop identity) applies unchanged, but **the dates are never what gets dropped** — they rank
above the greeting and above the identity, because a text the user cannot act on has no value to
protect.

**Email** carries the full reproduction recipe, which is the point of the email:

```
YYZ → YVR, cheapest in March 2027

  Fare            $412.00 CAD
  Dates found     Mon 8 Mar 2027  →  Mon 15 Mar 2027   (7 nights)
  Found on        skyscanner.ca, using its own whole-month search
  Your target     $600.00

  Book these exact dates    <deep link built for Mar 8 → Mar 15>
  What I searched           <the whole-month search link I actually read>

  Also checked
    kiwi.com          $438.00   Mar 11 → Mar 18   (cheapest in March)
    onetravel.com     $529.00   Mar 1  → Mar 31   (only dates this site would quote)
    kayak.com         —         blocked automated checks
```

Two links, deliberately, and this is an addition worth calling out: **"book these exact dates" is
constructed from the site's ordinary `url_template` using the dates that were found**, so one click
reproduces the deal directly rather than dropping the user back into a month search to hunt for it.
**"What I searched"** is the month-mode URL the watcher actually read, so the finding is auditable —
the user can see exactly what the agent saw. Neither is ever put in the SMS.

The **Also checked** block is the existing `sources` list with `depart_found` / `ret_found` /
`date_basis` rendered per row. It is what makes two sites finding different weeks legible instead of
looking like a contradiction, and it is where a rung-4 row is labelled *"only dates this site would
quote"* rather than being allowed to imply it found a cheapest.

**Chat (the "answer now" path, 3e) and the background-tasks LOG line** render the same fare + dates +
source triple. The LOG stays one line, because `hermes_delivery.parse_output()` reads only the first
match:

```
LOG: YYZ-YVR cheapest in March is $412.00 on Mar 8-15 (skyscanner.ca, native month search);
kiwi.com $438.00 Mar 11-18; onetravel.com month-bounds only; kayak.com blocked
```

---

## Phase 2 — The extractor

### `scripts/flight_render.py` — the only place Playwright is touched

*(Named `flight_render.py`, not `flight_browser.py`: it renders one page and prints its HTML. It is
already written; see the Phase 1 note. What it still needs is a calendar-read path for ladder rung 3.)*

A deliberately small, single-purpose subprocess so the interpreter constraint lives in exactly one
place. `flight_watch.py` invokes it as `/usr/bin/python3 <path> --url … --wait-selector … --json`.

- Chromium headless, realistic locale/timezone/viewport, images/fonts/media blocked to cut bandwidth.
- Persistent profile at `~/.hermes/flight-browser-profile` so consent cookies survive between runs —
  the cheapest single defence against interstitials.
- **Real** Chromium UA, not a fabricated one. `price_watch.py:46-54`'s no-fake-UA law was measured on
  *retail HTML over urllib* and does not transfer: these sites require a browser-shaped client
  because they *are* browser applications. Say so in the docstring, next to the old law, so nobody
  "fixes" one by breaking the other.
- Prints candidate fares as JSON and exits. `try/finally` closes the browser; the parent adds
  `subprocess.run(timeout=…)` plus a `kill()`, so a hung page cannot hold a cron tick. Total browser
  budget ≤120 s against the Hermes terminal tool's 180 s ceiling.
- Missing Playwright → **exit 3 with a plain message**, never a silent empty result.

### `scripts/flight_watch.py` (new) — sibling of `price_watch.py`

Reuses `price_watch` by import for state and output, exactly as `price_search.py` already does
(`_load("price_watch")`): `read_state` / `write_state` (:409/:416), `emit` (:511), the
`Blocked`/`Gone`/`classify_error` taxonomy (:59-88), and the `FAIL_ALERT_AFTER` escalation shape
(:464).

```
flight_watch.py --origin YYZ --dest YVR --depart 2026-09-15 [--return 2026-09-22 | --one-way]
                --below 600 --state yyz-yvr-20260915 --alert-to ohmzaiowui
                [--adults 1] [--cabin economy] [--max-stops N] [--sites a.com,b.com]
                [--monitor '<name>'] [--schedule 'every 6h'] [--unit '$'] [--currency CAD]
                [--result-out <path>] [--once] [--selftest]

  # Phase 1e — month / range mode. Exactly one of --depart or --depart-month, likewise for return.
  --depart-month 2027-03          whole month, ladder-resolved per site
  --return-month 2027-03          defaults to --depart-month on a round trip
  --depart-range 2027-03-08:2027-03-15    explicit window (early/mid/late month, "first week of")
  --return-range 2027-03-15:2027-03-22
  --trip-days N                   required for rung 2 on a two-month round trip; rung 2 is
                                  SKIPPED without it rather than sending an unbounded query
  --trip-flex N                   default 3, the ± around --trip-days
  --allow-assumed-dates           rung 4 may be READ without this; the flag only permits it to
                                  count toward a text, and the pipe never passes it
```

**Fare validation — what makes this different from the refused path.** A candidate is rejected unless
all hold:

1. It came from a page requested **for this itinerary** (slots in the URL). Structural, not heuristic.
2. It is attached to an itinerary element that also carries a date/time/airline — not a page-level
   number. This is what kills the "C$ 146+" title case.
3. It is not preceded by a teaser cue (`from`, `starting at`, `as low as`, `one-way from`).
4. Sanity band per cabin (economy ≈ $40–$20,000); reject financing-style "per month" figures.
5. Its **currency is known** from the registry or read off the page. Unknown → reject.
6. Report the **minimum of validated fares** on the page, with its source recorded.

**In month/range mode, rules 1 and 2 are replaced by the tuple rule from Phase 1e** — the fare and its
own departure and return dates must come out of one itinerary element together, both dates must fall
inside the requested window, and a bare minimum with no dates attached is rejected. Rules 3–5 apply
unchanged in every mode. Rule 6 becomes "report the minimum validated *tuple*", never a loose number.

**Cross-site agreement.** Query the usable sites in registry order, stop early once two
**owner-independent** sites agree within 15%. Report the minimum, name its source, and record which
sites were tried and what each said. One site alone still reports but, with `--require-confidence` on
by default for fares, **does not text** — it logs, and waits for a second source.

In month/range mode the quorum validates the **magnitude only**, because two sites searching March
will legitimately find different weeks. The reported fare stays a single site's `(value, depart_found,
ret_found)` tuple; values and dates are never blended across sites.

**Firing.** `--below` is required (the user's decision). Fire when `min_fare <= below`, literally and
state-based per brief rule 8, deduped on an identical repeated value. Add
`FARE_COOLDOWN_S = 6 * 3600` — this closes the open bug at `docs/TRACKING_ENHANCEMENT.md:91` for the
fare path, and it defaults its timestamp to **`None`, not `0`**, because defaulting to `0` is the exact
bug an injected clock caught in stock mode (`:104-106`).

**Dedupe in flex mode keys on the tuple, not the number.** State records
`alerted = (value, depart_found, ret_found)`. Suppression still triggers on an identical *value*, so a
month whose floor sits at $412 for a week does not text daily — but when the value is unchanged and the
**dates have moved**, that is logged explicitly and still not re-texted. The reverse (same dates, lower
value) is a real improvement and fires normally, subject to the cooldown. Keying on value alone would
hide a genuine change; keying on the tuple alone would text every time the cheapest week drifted by a
day.

**Honest failure.** Zero validated fares → log every run, and after `FAIL_ALERT_AFTER`-style
consecutive failures send **one** alert of a new kind `fare_unreadable` ("no site would show me a fare
for YYZ→YVR Sep 15–22"), then back off rather than repeating. The job keeps running: a site that
blocks today often does not tomorrow. It never guesses a fare, ever.

### `scripts/alert_templates.py` — additions

- `fare` already has a renderer (`_fare`); extend the payload with `itinerary` ("YYZ→YVR Sep 15–22"),
  `source` (domain), `sites_tried`, and a `fare_url` that only the **email** carries — the SMS path
  strips links because carrier gateways silently drop texts containing them (`URL_RE` :338).
- **Flex-mode payload fields** (Phase 1e), all mandatory when the mode is month or range:
  `depart_found`, `ret_found`, `date_basis`, `window` (the month or range asked for), and
  `book_url` — a deep link built from the site's ordinary exact-date `url_template` using the dates
  that were *found*, so one click reproduces the deal instead of re-opening a month search.
  `fare_url` keeps its meaning as *what the watcher actually read* (the month-mode URL), so the
  finding stays auditable. `sources` gains `depart_found` / `ret_found` / `date_basis` per row.
- `_fare`'s sentence gains the found dates and, on rungs 1–3, the "cheapest in \<month\>" clause. On
  rung 4 it says **"only dates this site would quote"** and the word *cheapest* is structurally
  unavailable to it — a `date_basis == "assumed_month_bounds"` reading takes a different branch, so
  the claim cannot be made by accident.
- New `fare_unreadable` renderer + `PROBLEM_KINDS` entry + `ADVICE` entry.
- **Keep `fare_unsupported`**: `price_search.py` still refuses fares and is still right to, because a
  search result is still not an itinerary. Reword its `ADVICE` to point at the new capability ("give
  me an origin, destination and dates and I can watch the fare itself") instead of "I can't watch
  flight fares."
- SMS stays inside 140 ASCII: `"YYZ-YVR Sep15-22 is $289, under your $600 target"` fits, and so does
  the flex form measured in Phase 1e (~118). The degradation ladder may drop the greeting and the
  identity but **never the found dates** — a text the user cannot act on has no value to protect.
  Date strings are built dot-free (`Mar 8-15`, never `Mar. 8`) because `URL_RE`'s bare-domain branch
  would silently eat a dotted token.

---

## Phase 3 — The pipeline flight path

All in `pipes/auto_assistant.py`, deployed with `scripts/deploy_pipe.py`.

### 3a. Where it sits

Inside the existing `if BG_TASKS and not attached_img and not ref:` block (`:5070`), so media, the
`__task__` guard (`:4865`) and the deterministic manage path (`:5042`) all keep precedence — "cancel
my flight watch" is a manage op and must stay one. New order inside that block:

1. `oneshot` (`/research`, `/agent`) — unchanged (`:5087`)
2. `_pending_phone_request` — unchanged (`:5101`)
3. **NEW: `_is_flight_request(text)`** — start the form / act on complete slots
4. existing `followup or _is_bg_task_request` (`:5109`)

**Corrected during implementation — the pending-form check needs its own, earlier insertion point.**
A reply to the itinerary form must be handled **above** the deterministic manage block (`:5042`), not
inside the bg block. `_MANAGE_VERB` (`:958`) anchors `cancel|stop|pause|end|kill` at position 0, and
the `referring` branch (`:5061`) fires whenever a job table was rendered in the last two turns — so a
bare "cancel" or "stop" meant to abandon the form would be read as a scheduler reference and act on a
real job. The form owns its own abandonment vocabulary, which means it must be asked first.

Placing 4 **before** 5 is the point of the whole exercise: today a flight ask only reaches the agent
if `_BG_VERB AND _BG_RECURRENCE` both match, so "find me a cheap flight to Tokyo in March" falls
through to plain chat, and "watch flights YYZ to YVR" reaches the generic watch machinery which builds
a `price_search --kind fare` job that refuses itself. Both now enter the flight path instead.

### 3b. Intent detection — deterministic first, default deny

Follows the three-tier coder pattern (`_CODE_STRONG` :573 → `_CODE_HINT` :586 → `_classify_code` :611)
and the DEFAULT-DENY law at `:868-877`.

- `_FLIGHT_SLASH` — `/flight`, an explicit escape hatch, precedent `/img` `/vid` `/research`.
- `_FLIGHT_STRONG` — a flight noun (`flight(s)|airfare|fare|plane ticket|airline ticket`) **and**
  either a route shape (`\b[A-Z]{3}\s*(?:-|–|to|→)\s*[A-Z]{3}\b`, `from … to …`) or an
  intent verb (`find|search|look for|cheapest|how much|book|price|watch|track|monitor|alert me`).
- `_FLIGHT_DENY`, checked first — possessive/past-tense and idiom: `my flight`, `our flight`, `the
  flight I`, `was delayed|got cancelled|missed my`, `flight attendant`, `in-flight`, `flight
  simulator`, `flight of stairs`, `flight risk`, `took flight`, and "how do flight prices work"-style
  meta questions.
- `_FLIGHT_HINT` → `gemma3:1b` classifier (`ROUTE_CLASSIFIER_MODEL` :329), one word,
  `FLIGHT`/`OTHER`, temp 0, `num_predict 4`, failure ⇒ `False`. Same fail-closed contract as
  `_classify_code`, which writes a `job:"classifier"` metric row.

A labelled corpus of ≥30 positives and ≥30 negatives seeds `tests/test_flight_intent.py`, including
the real negatives already in `tests/test_bgtask_intent.py` (e.g. `"I watched a great video about
sourdough yesterday"` must still not route anywhere near here).

`_KIND_RULES`' `fare` entry (`:1110`) stays as-is — it is still how a fare is *worded* — but the fare
steer at `:4243` that pushes fares toward `price_search.py --kind fare` is removed, since fares no
longer take that path.

### 3c. Slots and the fill loop

Six required slots: **origin, destination, depart, return-or-one-way, target price, contact** — where
depart and return may each be an exact date, a range or a whole month (Phase 1e). One conditional
seventh: **trip length**, when the ask is a round trip in month mode with no duration given.
Optional: adults, cabin, max stops.

- **Airports**: a curated city/alias → IATA table **inline in `auto_assistant.py`**, including metro
  codes (YTO, NYC, LON, PAR). Deliberately *not* a `pipes/shared/` sidecar: `deploy_pipe.py`'s
  `SIDECARS` is an explicit dict (not a glob), so a new sidecar means a new registration, a new
  `tests/test_deployed.py` byte-check, and a new silent-staleness mode — which that file's own comment
  names as "the reason a stale copy would never surface on its own." The table is small and static;
  inlining it ships atomically with the pipe. The **pipe alone** resolves names to codes and passes
  only 3-letter codes to `flight_watch.py`, which rejects anything else — one table, nothing to drift.
  Unrecognised place → ask, offering the closest matches.
- **Dates** are **date specifications with a precision**, per Phase 1e — `exact`, `range` or `month`.
  Parsed deterministically: ISO, `Sep 15`, `September 15th`, `15/09`, `next Friday`, `in 3 weeks`,
  `Sep 3 back Sep 10`, `for a week` (depart + 7) all yield `exact`; `early|mid|late March`,
  `first|last week of September` yield `range`; `in March`, `sometime in June`, `March out back in
  April` yield `month`. Past dates rejected with the correction; a partly-elapsed month narrows to its
  remaining days and says so. Relative dates and the "next occurrence" month rule resolve against
  `date.today()`.

  **A bare month no longer asks** — this is the change Phase 1e makes to the original design. It
  enters the ladder instead, which is both what the user meant and what the sites are built for.
  Seasons (`in the spring`) and bare years still ask, because three months is not a query any site
  can express.
- **Trip length** — a *conditional* slot, asked only when the itinerary is a round trip in month mode
  with no duration anywhere in the request. One question (*"roughly how long is the trip?"*), because
  without it rung 2 is unbounded and gets skipped. `for a week`, `10 days`, `a long weekend` (→ 3)
  supply it silently. Never asked for a one-way, and never asked when dates are `exact`.
- **One-way**: `one way|one-way|no return|just going`.
- **Target**: `under|below|less than|at most|max $N`, `$N or less`, `budget of N`. Required — if
  absent, ask. (The user chose this over a baseline heuristic.)
- **State carrier**: `self._flight_ask[cid] = {"t": ts, "slots": {...}, "rounds": n}` in `__init__`
  beside `self._phone_ask` (`:459`), with the same LRU cap and `PARK_TTL_S` expiry. **Not** an HTML
  comment — `_marks()` (`:1354-1365`) records that OpenWebUI escapes those wherever they appear and
  printed a wall of base64 under every answer.
- **The form**: one message listing *everything* understood and *everything* missing, then re-parse
  the free-text reply and merge. Cap at 3 rounds, then bail with a plain sentence rather than looping.

### 3d. Contact gating

Reuses `_contact` (:1153), `_alert_email` (:1177), `_save_phone` (:1181), `_phone_prompt` (:1328),
`_alert_setup_block` (:1287) unchanged. Rules for flights:

- Email is derived from `webui.db` and is almost always present; phone is asked for.
- The existing gate (`:5151`) fires **before** anything is scheduled — keep that ordering exactly: its
  comment is the reason ("a monitor that runs, fires, and texts nobody — the user believing they are
  covered").
- `_PHONE_DECLINE` (`:1092`) → **email-only is allowed** and the confirmation says so plainly. A
  flight watch that emails is a working flight watch; refusing to create one because the user does not
  want texts would be worse than the thing the gate exists to prevent.
- No email *and* no phone → refuse to schedule and say why.

### 3e. Answer now, then offer the watch

The chat-facing Hermes has **no terminal tool**, so the pipe cannot ask it to run the extractor. It
uses the shared alerts volume instead — the same pipe↔host channel that already carries contacts:

1. Pipe `POST /api/jobs` — a **one-shot** job (brief rule 7's shape) whose prompt runs
   `flight_watch.py --once --result-out /volume1/docker/openwebui/config/alerts/flight_results/<state>.json`,
   then `POST /api/jobs/{id}/run` to fire it immediately.
2. Pipe polls `/app/backend/data/alerts/flight_results/<state>.json` (same directory, container side)
   for up to ~90 s, streaming a status line so the turn never looks hung.
3. Renders the fares with their sources, then offers the watch: "want me to keep checking every 6h
   until Sep 14?"
4. Timeout or empty result → say exactly that, and still offer the watch.

Needs a new `flight_results/` subdirectory under the host `alerts/` dir (already `ohmz:ohmz`, so
writable), plus pruning of results older than a day.

### 3f. Deterministic job creation

`_flight_create_job(slots, handle)` — no LLM in the path (the user's choice, and
`docs/MANAGE_PATH_PLAN.md` established the precedent for deterministic REST):

- `prompt` = `"Run this terminal command and print its output verbatim as your entire response. Add
  nothing.\n/usr/bin/python3 /home/ohmz/ai-stack/scripts/flight_watch.py --origin … "` — the
  interpreter spelled out, no URL in the prompt (the registry holds those), well under 5000 chars,
  and clean against `_scan_cron_prompt`.
- `name` ≤200 chars, ASCII only (it reaches SMS): `"YYZ-YVR Sep15-22 under $600"`.
- `schedule` `"every 6h"` by default; **`repeat` bounded so the watch stops the day before
  departure** — brief rule 1's requirement, computed rather than left to a model.
- `deliver: "local"`.
- Then read back `GET /api/jobs?include_disabled=true` via `_jobs_list` (:4157) as ground truth and
  `_stamp_owner([id], handle, src="flight_rest")` (:1245) — an unowned job is invisible to the person
  who asked for it.
- **Fallback**: any REST failure → `_hermes_stream` with a new brief rule **`5d-iv`** carrying the
  same command, so the feature degrades to today's behaviour instead of dying.

  **Corrected during implementation:** the rule is `5d-iv`, not `5f`. `5e` is already taken
  ("the extractor also reports its OWN failures", `:3264`), and the comment at `:3173-3174` already
  reserves the number for exactly this work — *"iv = the next one (a fare extractor is the expected
  claimant) … allocate the number here first."*

### 3g. Instrumentation

New `_route_metric` (:2637) rows, tier 0 for deterministic hits: `flight.search`, `flight.watch`,
`flight.slotfill`, `flight.deny`, with rule ids `flight_slash`, `flight_strong`, `flight_classifier`,
`flight_form_reply`, `flight_deny`. Add to `tests/route_metrics.py` the invariants that a
`flight.watch` row is always accompanied by a created job id, and that no `flight.*` row's request
text contains `### Task`.

---

## Phase 4 — Tests, docs, deploy, verification

Suites are standalone scripts (`python3 tests/test_x.py`, `sys.exit(main())`), fixtures offline,
clocks injected. Note the existing convention the new pipe suites must follow: they load
`pipes/live/auto_assistant.py` (the *deployed* bytes) by default and accept a path argument, so a pipe
suite is run either after `deploy_pipe.py` or with `pipes/auto_assistant.py` passed explicitly.
`tests/test_bgtask_intent.py:213-220` already asserts four flight phrasings classify as `fare`; those
stay passing because `_KIND_RULES` is untouched.

| New/changed | What it pins |
|---|---|
| `tests/test_flight_intent.py` | The ≥60-case corpus; deny-list beats strong; classifier failure ⇒ `False`; existing `test_bgtask_intent.py` negatives still don't route here |
| `tests/test_flight_slots.py` | Date/IATA/target parsing incl. metro codes and past dates; **the precision model** — `exact` / `range` / `month` classification per phrasing, next-occurrence month rollover, `March out back in April`, return-month defaulting to depart-month, partly-elapsed month narrowing, seasons and bare years still asking; the conditional trip-length question fires only for a round trip in month mode with no duration; the 3-round form state machine; TTL expiry; **city→IATA resolved only in the pipe** |
| `tests/test_flight_watch.py` | Saved HTML/JSON per usable site; teaser rejection; **currency mismatch rejected**; sanity band; owner-independent quorum; flap cooldown with an injected clock and the `None`-not-`0` default; honest-failure escalation and back-off |
| `tests/test_flight_flex.py` (new) | **The Phase 1e ladder.** Rung selection per `date_flex` value; rung 2 **skipped** when `--trip-days` is absent; the tuple rule — a month-mode page whose fares carry no dates yields **nothing**, and a fixture of the 97-price shape produces no value rather than a minimum; extracted dates outside the requested window discarded; `date_basis` confidence ceilings, and specifically that a lone `assumed_month_bounds` reading **logs and does not text**; the word "cheapest" is unreachable on rung 4; quorum across two sites that found *different* weeks agrees on magnitude while the reported tuple stays one site's; dedupe on `(value, dates)` — same value + moved dates logs and does not re-text, same dates + lower value fires; **`depart_found`/`ret_found` present in every emitted flex payload** and rendered in SMS, email and LOG; `book_url` built from the found dates and absent from the SMS; date strings dot-free |
| `tests/test_flight_registry.py` | Registry schema; **set equality against the 19 requested domains**; every `usable*` site has an `extract` block; templates contain the required placeholders; `date_flex` in the closed vocabulary, and a `whole_month` site has a `month_url_template` containing `{depart_ym}` while a `calendar` site has a `calendar` selector block. Drift-test precedent: `test_web_search.py:346-360` |
| `tests/test_flight_create.py` | REST body shape; name ≤200 ASCII; prompt ≤5000; a local copy of `_CRON_THREAT_PATTERNS` passes; `repeat` bounded to departure |
| `tests/test_price_search.py` | Fares still refused there, with the reworded advice |
| `tests/route_metrics.py` | The new `flight.*` invariants |
| `docs/FLIGHT_FARES.md` (new) | Phase 0 answers, the per-site table, what was refuted, the maintenance-subscription framing |
| `docs/HERMES_AGENT.md` | "Fares are refused, not attempted" → qualified: refused *from a search result*, attempted *from an itinerary URL*; add brief rule 5f |
| `docs/TRACKING_ENHANCEMENT.md` | Resolve open item 2; note item 3 (flap cooldown) is now closed for the fare path |
| `docs/QA_TEST_PLAN.md` | Register the new suites, and fix the existing gap — `test_price_search.py` is missing from that table |

### End-to-end verification

1. `python3 tests/test_flight_*.py` — all green offline.
2. `python3 scripts/flight_watch.py --selftest` and one `--once` run against the canonical itinerary
   on the host.
3. `python3 scripts/deploy_pipe.py` to push the pipe, then `python3 tests/test_deployed.py`.
4. In OpenWebUI: **"find me flights from Toronto to Vancouver Sep 15 back Sep 22 under $600"** → the
   form fills, the one-shot answers in chat, the watch offer appears.
5. Partial ask: **"watch flights to Vancouver"** → the form asks for origin, dates and target, and
   nothing is scheduled until it has them.
5b. **Month ask: "watch flights from Toronto to Tokyo in March under $900"** → no date question is
   asked; the form asks only for trip length and the target if missing; the confirmation says "all of
   March 2027 (flexible)" and names which rung each site will use. Force a run and confirm the SMS,
   the email and the LOG line **all carry the found departure and return dates**, that the email's
   "book these exact dates" link opens that exact itinerary, and that any rung-4 site appears as
   "only dates this site would quote" and did not on its own cause the text.
5c. **Season ask: "flights to Lisbon sometime in the fall"** → still asks which month(s), and
   schedules nothing.
6. Negative controls: **"my flight was delayed"**, **"what airline flies to Osaka?"**, **"track the
   price of this monitor <url> under $200"** → chat, chat, and the *existing* price path respectively.
7. `curl localhost:8642/api/jobs` shows the created job with the right `repeat`; force a run and
   confirm both SMS and email arrive with a fare, a source and an itinerary.
8. Delete job `52f821a8d3a2` and its `yto-yvr.search` state.

---

## Risks, and what each one costs

| Risk | Mitigation |
|---|---|
| **Most of the 19 sites will block headless automation.** Kayak/Skyscanner especially. | The Phase 1d gate decides go/no-go on measured evidence rather than hope; verdicts are recorded so nobody re-derives them. Zero usable sites ⇒ report, don't ship. |
| A fare is read but is not bookable — the exact 2026-08-07 failure, repeated. | Itinerary is in the URL (structural), plus five independent validation rules, plus owner-independent cross-site agreement, plus the source named in every alert. |
| Currency confusion: a USD fare against a CAD target. | `currency` is a registry field; unknown currency is a hard reject. |
| Playwright unreachable from a cron tick. | Explicit `/usr/bin/python3` in both the job prompt and the subprocess call, **and** Phase 0 check 3 proves it before any code is written. Missing Playwright exits 3 loudly. |
| A hung page holds a cron tick or leaks Chromium. | `try/finally` close, parent-side `timeout` + `kill()`, ≤120 s browser budget against the 180 s tool ceiling. |
| A false flight route evicts the chat tenant (~23 s, `ROUTING_ROADMAP.md:10`). | Deterministic REST creation means the *common* path never loads the agent at all — strictly better than today. Detection is default-deny with a deny-list checked first and a 60-case corpus. |
| 19 sites × frequent polling looks like abuse. | Default `every 6h` (not 15m), early-exit at two agreeing sites, one request per site per run, `repeat` bounded to departure. |
| Site markup drifts and the watcher goes quiet. | `fare_unreadable` alerts once and backs off rather than failing silently; every run logs which sites were tried and what each said. |
| **Month mode gives up the URL-binding guarantee** that justified the whole feature — a whole-month page legitimately shows dozens of itineraries, which is the exact shape that produced `$358.72`. | The tuple rule: fare + its own two dates extracted from one element, both inside the requested window, or the page yields nothing. Binding moves from the URL to the element rather than being dropped. Pinned by a 97-price fixture asserting **no value**, not a minimum. |
| **An alert names a month but not the dates**, so the user cannot reproduce the deal and concludes the alert was wrong. | `depart_found`/`ret_found` mandatory in every flex payload and rendered on all three surfaces; the SMS degradation ladder may drop the greeting and the identity but never the dates; the email carries a "book these exact dates" link built from the found dates. Test-pinned per surface. |
| **Rung 4 implies it found the cheapest fare in the month when it priced one arbitrary pair** — and the user books the worse fare believing it was the best. | `assumed_month_bounds` takes a separate renderer branch where the word "cheapest" is structurally unavailable; its confidence ceiling is `medium`; and with `--require-confidence` on by default it **logs and never texts alone** (the user's decision). |
| **Combinatorial blow-up**: a whole-month × whole-month round trip is ~900 date pairs. | Trip length asked once; rung 2 **skipped** rather than sent unbounded when it is missing; `--max-sites 3`, one browser load per site, `RUN_DEADLINE_S` under the 180 s tool ceiling. |
| Month mode is heavier than exact mode (calendars, more renders) and could grow into the run budget or the IP's reputation. | `BROWSER_MIN_INTERVAL_S` clamp holds the browser tier independently of the job's schedule and logs when it does; default `every 6h` unchanged; the clamp lives in the script so honouring the user's literal schedule never means doing the heavy work every tick. |
| **`book_url` is constructed, so it is a claim** — a wrong link is worse than no link. | It is built only from the same exact-date `url_template` the site was *measured* on. A site with no working deep link gets **no** `book_url`, and the email says which dates to search instead of linking somewhere unverified. |
| Scope creep: this touches routing, a new extractor, browser automation and job creation at once. | Phases are independently shippable. Phase 1 alone (registry + findings doc) has standalone value even if the gate says stop. |

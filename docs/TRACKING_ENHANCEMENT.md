# Tracking enhancement — what shipped, and what the measurements said

Supersedes the working draft `~/Desktop/hermes-tracking-requirements.md` (2026-08-06). That file
captured requirements and three open uncertainties; this one records what happened when they were
tested against the live stack on 2026-08-06/07. Kept because most of the value here is **negative
results**, and the cost of re-deriving them is two production jobs and one wrong text message.

The ask was: make the tracking that already exists find its own sources on the open web — free, no
API keys, Google included — so that asking the Assistant to "watch this" produces a working monitor
for item prices, flight fares, and stock/availability. Routing, scheduling, GPU-guarding, ownership,
delivery and retry were already built and proven; the gap was **discovery and extraction**.

## Where each piece landed

| Piece | Outcome |
|---|---|
| **Stock / availability** | **Shipped and live.** `price_watch.py --mode stock`, brief rule 5d-iii. See HERMES_AGENT.md. |
| **Search isolation** | **Shipped.** A second SearXNG instance on `127.0.0.1:8889` for monitors only; chat's roster untouched and sha256-pinned by a test. |
| **Multi-engine search** | **Partly delivered.** The fan-out, merge, score-sort and scoreboard work; the roster does not yet include Google. |
| **Exact bookable fares** | **Refused, deliberately.** Not a gap — a measured decision. |

## The three open uncertainties, answered

**1. How to keep the two search consumers apart.** The draft hypothesised one instance with engines
present but `disabled: true`, selectable per-query via `&engines=`. Rejected before testing: an
unknown or ignored `engines=` value is *silently ignored* and falls back to SearXNG's full default
roster of dozens of engines, so one typo CAPTCHAs the IP with no error anywhere. A second container
was built instead, and the roster itself is the fan-out — the client sends only `q` and `format=json`.
Chat's `settings.yml` is byte-identical and pinned.

Engine limits are **per source IP, not per container**, so the rosters are kept almost disjoint. That
is the real isolation: a monitor cannot spend an engine chat depends on.

**2. Whether exact keyless fares are sustainable.** **No, and the failure mode is worse than
nothing.** The draft worried fares would "break monthly". What actually happens is that they appear
to *work*: job `52f821a8d3a2` reported `$358.72` at high confidence from a page carrying 97 different
prices. Refusal shipped. See HERMES_AGENT.md for the full account.

**3. Whether retailers' availability is server-rendered.** Partly, and the design assumes it often
is not. The extractor reports "I could not read it" rather than guessing, which is the honest answer
for a JS-written availability box.

## What the live instance measured (2026-08-07)

First contact with `:8889` refuted three things the config asserted about itself:

- **`startpage` and `qwant` CAPTCHA on sight** — on the very first query the instance ever issued,
  and every one since (startpage `suspended_time=3600`, qwant `suspended_time=0` so it is retried
  forever). Not blocked by accumulated load. Both dropped.
- **`brave` is intermittent, not dead** — 180-second suspensions, and between them it produced the
  only relevant rows in every test. Kept.
- **`bing` alone is not weak, it is junk** — for "sony wh-1000xm5 price" it returned `sony.ca`,
  `playstation.com` and the Wikipedia article on Sony, and no product page; for a Toronto→Vancouver
  flight query it returned `speedycash.com`, a payday-loan site, five times.
- **`google` is absent from the roster and nothing said so.** It exists in the image's defaults
  (`settings.yml:1217`, distinct from `google cse` at `:1236`), but listing it redundantly in the
  `engines:` flip block — where it needs no flip, since unlike bing and mojeek it does not ship
  disabled — appears to shadow the real definition. The container started clean, exit 0, empty logs,
  five engines. **Nothing in that file fails loudly. Verify every roster change against `/config`.**

**Consequence:** the isolation half is worth having on its own — it moved 144 queries/day/engine off
chat's roster (an `every 5m` monitor that never resolves searches 144 times a day). The
"more and better engines" half is not delivered until Google is back.

## The diagnostic that settled the fare question

The draft could not tell whether the original fare job failed at *discovery* or at *extraction*,
because `fruitless_streak` increments identically either way. `sstate["last_search"]` now records it:

```json
{"rows": 10, "merged": 10, "fetched": 1, "dead": ["brave","qwant","startpage"], "reason": "no_candidate"}
```

`rows: 10` — discovery worked. `reason: no_candidate` — nothing extractable. **More engines was never
the fix for fares.** Read it with `--engine-scores` alongside for the per-kind engine record.

`reason` distinguishes three outcomes deliberately: `no_results` means more engines might help,
`no_candidate` means the roster was never the problem, `deadline` means nothing was proven at all.

`fetched: 1` of a budget of 4 is its own finding: nine of ten rows were discarded before any fetch by
one-candidate-per-host, which is what a spammy single-engine result set collapses to.

## Still open

1. **Google. The flip-block theory is REFUTED — measured 2026-08-07, later the same day.** The
   container had never been recreated after `a1b4558`, so the roster it was running was the *old*
   one: `/config` returned `startpage, brave, qwant, mojeek, bing` while both declarations said
   `google, brave, mojeek, bing`. Two bugs at once, pointing opposite ways — an addition that never
   landed and a removal that never landed — and `tests/test_web_search.py` passed throughout, because
   it pins the two *declarations* to each other and neither to the live instance.

   After `docker compose up -d --force-recreate searxng-hermes` (a changed **mount** needs a
   recreate, not a restart):

   ```
   live = ['bing', 'brave', 'mojeek']
   ```

   **startpage and qwant are gone, and google is still absent.** That recreate is the control: it
   proves the file WAS reloaded, so "the container never picked it up" is no longer available as an
   explanation. And the flip block now contains only `mojeek` and `bing` — `google` appears nowhere
   in this file except `keep_only` and prose. So the hypothesis at :55-59, that a redundant
   flip-block entry shadows the real definition with a module-less stub, **does not hold**: google was
   absent with the entry and is absent without it. Its presence there was never the variable.

   What is still unexplained is why `keep_only: [google, ...]` drops google specifically, silently,
   with the container exiting 0. Do NOT change `search.default_lang` next just because this file
   used to say so — that was the follow-on guess to a hypothesis now known to be wrong. Diagnose
   before editing: whether the image's own defaults declare an engine named exactly `google` and
   whether it ships disabled, whether the container logged anything at load, and whether `/config`
   reports `enabled` for the three that survive. Verify against `/config` after every change —
   nothing in that file fails loudly.

   Until then, prefer `price_watch.py --url` with a real link over the no-URL path.

   None of this touches flight fares: `scripts/flight_watch.py` issues no search queries at all,
   because it builds its URL from an itinerary instead of finding one.
2. **A fare capability — ANSWERED, and the answer is no on the free path.** Measured 2026-08-07
   across all 19 sites the user named, both tiers: **11 blocked, 6 never fetched (no authorable deep
   link), 2 wrong-role, 0 readable.** Full record in `docs/FLIGHT_RECON.md`.

   The prediction in this line was half right. A headless browser with the interpreter spelled out
   was indeed necessary — and it was not sufficient. kayak, momondo and cheapflights served headless
   Chromium the *same* "What is a bot?" wall they served urllib; skyscanner's 708-byte React shell
   rendered into a PerimeterX challenge. Getting past those needs proxies and CAPTCHA solving, which
   is a different kind of problem from reading a page and not one worth solving here.

   So the refusal stands, and it is now a refusal *by measurement* rather than by assertion. What
   changed is that it is no longer a dead end: `price_search.py` emits `fare_needs_itinerary` naming
   what is missing, and `scripts/flight_watch.py` exists, tested and inert, reading only sites a
   human has marked shippable. It turns on by editing one field the day a readable source appears —
   including a keyed API, whose fetch layer is the only part that would differ.

   Two things genuinely still open, in order: Chrome recon on the six unmeasured `no_deeplink` sites
   (the only remaining free path, worth ~3 independent sources not 6), and costing a keyed fare API
   (Amadeus / Duffel / Kiwi partner), which is the option that actually works.
3. **Price mode has no flap cooldown.** A price oscillating either side of a target still texts every
   run. The cooldown is implemented mode-agnostically and gated to stock mode with a comment.
4. **`--label` is not sanitised.** Page phrases and `--query` are both folded; a user-supplied label
   reaches the ALERT sentence as given.

## Two lessons about testing, both learned the hard way here

**A check can pass while testing nothing.** Four existing checks — "amazon.ca wins a tie *from second
place*", "a *later* high-confidence page beats a rank-1 low-confidence one" — passed only because
`shop-a` sorts before `shop-f` once ranking by score was introduced. And the first version of the
listing-page checks passed while reading a stale fixture, because `best_candidates` is stubbed
earlier in that suite. Both now assert what they claim.

**An injected clock finds what production hides.** The stock flap cooldown defaulted its timestamp to
`0`, so `now - 0` read as a recent alert and swallowed the very first firing — invisible on a real
epoch, which is not a property to depend on.

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

1. **Google.** The one variable left is whether removing it from the flip block restores it; if not,
   `search.default_lang: "en-CA"` is the next single thing to change. Until then, prefer
   `price_watch.py --url` with a real link over the no-URL path.
2. **A fare capability, if wanted at all.** Would need a headless browser reading one specific
   itinerary, with an interpreter spelled out explicitly, and an honest verdict that it is a
   maintenance subscription rather than a feature. The refusal is a complete outcome without it.
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

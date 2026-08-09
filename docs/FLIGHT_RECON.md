# Flight site reconnaissance — what 19 sites actually gave an automated client

Measured 2026-08-07 against the live web, plain tier, `YYZ→YVR 2026-09-15/2026-09-22`, from this
host's IP. Instrument: `scripts/flight_probe.py`. Raw record:
`docs/flight-recon/20260807T184200Z/probe.json`, with the served bodies under `pages/` (gitignored).
The two browser-tier runs have their own records, cited in the browser-tier section below.

**Headline: zero of nineteen sites can currently be read on the plain tier, and the plan's Phase 1d
gate says stop rather than ship.** This file exists so nobody re-derives that, and so the parts that
are *not* yet settled are visibly distinguished from the parts that are.

Written in `TRACKING_ENHANCEMENT.md`'s spirit: most of the value here is negative results, and the
cost of re-deriving them is 33 requests to the 11 hostile commercial hosts that have a fetchable URL
(51 requests across all three recorded runs), from a residential IP.

**Corrected 2026-08-08.** This read "57 requests to 19 hostile commercial hosts". No recorded run
made 57 requests and only 11 hosts were ever contacted: the plain run that produced the table below
records `requests_made` 33 (three per fetched site — robots.txt, primary, control — across the 11
sites carrying a `url_template`), and the two browser runs record 15 and 3. 57 is 19 × 3, the
hypothetical cost if every site had a fetchable URL: the eight sites that have none — six
`no_deeplink`, two `unusable_role` — were never requested at all, which the `no_deeplink` note below
insists on for its six. Quoting 57 charges the reader for traffic that never happened.

## The result

| verdict | n | sites |
|---|---|---|
| `blocked` | **11** | google.com, skiplagged.com, kayak.com, momondo.ca, cheapflights.ca, priceline.com, trip.com, orbitz.com, travelocity.ca, edreams.com, **skyscanner.ca** |
| `no_deeplink` | **6** | kiwi.com, cheapoair.ca, onetravel.com, flighthub.com, airwander.com, flightsfinder.com |
| `unusable_role` | **2** | secretflying.com, travelpricedrops.com |

**Every site with a usable URL is blocked. Not one of nineteen is readable.** The six
`no_deeplink` sites were never fetched at all, so they are unmeasured rather than refused — that is
the only remaining honest uncertainty in this table.

### Read it by operator, not by host

Nineteen sites are not nineteen chances, and counting them that way is the mistake this table exists
to prevent:

- **kayak.com, momondo.ca and cheapflights.ca served the same wall** — a 250–305 KB page whose own
  `<title>` is *"What is a bot?"*. One Booking Holdings backend, three hosts, one measurement.
  priceline.com is a fourth Booking property and refused separately with a 403.
- **orbitz.com and travelocity.ca both answered HTTP 429** on the first request this host has ever
  made to either. That is fingerprinting, not throttling. One Expedia backend, two hosts.
- **cheapoair.ca and onetravel.com** are one Fareportal engine, and neither has a deep link.

Eleven blocks are about seven operators: booking_holdings (kayak, momondo, cheapflights, priceline),
expedia_group (orbitz, travelocity), google, skiplagged, skyscanner, trip_com_group, edreams_odigeo.

**Corrected 2026-08-08.** This line read "Ten blocks are about six operators", which was the right
count until skyscanner.ca flipped `js_only → blocked` (the flip recorded below). It then contradicted
this doc's own result table above, which already listed eleven blocked sites including skyscanner.ca.

### The distinctions that matter more than the counts

**skyscanner.ca was the last open question, and it is now closed.** On the plain tier it returned
HTTP 200 and **708 bytes** — a bare React shell (`<div id="root">` plus *"You need to enable
JavaScript to run this app"*), which is not a block and warranted a browser attempt. Rendering it
produced **8240 bytes carrying `captcha`, `px-captcha` and `human verification`**: a PerimeterX
challenge. The shell was not a page waiting to render; it was the wrapper around a challenge. That
retires the roster's only `js_only` verdict and, with it, the last reason to expect a different
answer from more browser work.

**`no_deeplink` means never fetched, so nothing was learned.** Six sites were not probed at all
because no slot-bearing URL could be honestly authored — Fareportal encodes its search into a token,
FlightHub appears to POST and return a session URL, kiwi.com routes by city slug rather than IATA.
Recording these as `blocked` would be a fabrication: they may block, they may not, and no request was
made. Chrome recon is what turns them into measurements.

**google.com is blocked but its deep link is CORRECT.** It served a 1.2 MB body (1,203,013 bytes)
containing "captcha" — and it **echoed the requested dates back**. So the `?q=` URL form reached the
right itinerary and only the client was rejected. That makes it the best browser-tier candidate, not
the worst. It was not alone in echoing: skiplagged.com's Cloudflare interstitial reflected the
requested path too, dates included. In both cases the dates appear inside a reflected URL on a
wrapper page — google's saved body is `/travel/flights/unsupported`, skiplagged's is a
`__cf_chl_tk` challenge — so the echo proves the URL grammar, not that an itinerary page was served.

**Corrected 2026-08-08.** This entry read "a 512 KB challenge" and "the only site in the roster that
**echoed the requested dates back**". Both were wrong. 512 KB is not a measurement: it is
`flight_probe.py`'s `MAX_SAVE_BYTES = 512_000` (scripts/flight_probe.py:83), applied by the saver at
:866, which is why the body on disk is exactly 512,196 bytes while the recorded `bytes` for the same
fetch is 1,203,013. The registry note for google.com carried the same 512 KB figure when this was
found. And skiplagged.com's record echoes all four slots (origin, dest, depart, ret), identical to
google.com; orbitz.com and travelocity.ca echoed `origin` only. Publishing a save cap as a served
size also broke the comparison the browser table below is making: plain was already 1.2 MB, so
"512 KB → 2 MB" read as growth where the measurement shows a body that was large before Chromium
ever ran.

**edreams.com could never have worked on this tier** regardless of its 403: its itinerary lives in a
URL *hash fragment*, which is never sent to the server.

**cheapflights.ca is worth one line of its own.** This is the site that produced the `$358.72`
incident — the 97-price route page that satisfied every guard and texted a meaningless number. It is
now behind a bot wall, so that specific failure is no longer even reachable by this path.

## The browser tier: a wall served to urllib is served to headless Chromium too

Measured 2026-08-07, `--tier browser`, same itinerary. Raw records, both tracked in git:
`docs/flight-recon/20260807T185945Z/probe.json` (the run that aborted, 6 sites walked, 15 requests)
and `docs/flight-recon/20260807T190939Z/probe.json` (the skyscanner.ca run, 1 site, 3 requests).
Every browser-tier number below, and the 8240-byte PerimeterX challenge that closed skyscanner.ca
above, come from those two files. Until 2026-08-08 this doc named only the plain-tier record, so the
numbers a reader was asked to trust could not be reached from any path it gave.

Five sites were fetched before the run aborted (a sixth, kiwi.com, was walked past unfetched for want
of a deep link — the run's `probed` is 6 because the skipped entry is still appended to the results,
while `requests_made` is 15 = 5 fetched sites × 3):

| site | plain | browser | change |
|---|---|---|---|
| google.com | captcha, 1.2 MB | captcha, hit the renderer's 2 MB truncation cap | none |
| skiplagged.com | 403, 5.6 KB | 200, 27 KB, still "enable javascript and cookies" | none |
| kayak.com | "What is a bot?", 305 KB | **same wall**, 379 KB | none |
| momondo.ca | "What is a bot?", 267 KB | **same wall**, 337 KB | none |
| cheapflights.ca | "What is a bot?", 252 KB | **same wall**, 323 KB | none |

The bodies grew because JavaScript ran; the wall is what it rendered. **Headless Chromium with a
truthful UA and a persistent profile did not get past a single one of them**, which was the
prediction and is now the measurement.

One row is a cap rather than a size: google.com's browser `bytes` is exactly 2,000,000, which is
`flight_render.py`'s `--max-bytes` default (scripts/flight_render.py:82, "truncate returned HTML",
applied at :166-170). The served body was at least that and is not measured. That cap is a *different*
number from the probe's 512 KB `MAX_SAVE_BYTES` save cap, and the earlier version of this table
conflated them — it printed the save cap as google's plain-tier size and called the render cap "the
2 MB cap", which made a 1.2 MB → ≥2 MB row look like a 512 KB → 2 MB one.

### The run aborted before reaching the site it existed to test

`ABORT_AFTER_BLOCKED` fired on four consecutive blocks — correctly, that is a fingerprinted IP — but
it fired at cheapflights.ca, and **skyscanner.ca sits eighth in the registry's authored order**. The
guard was right and the run was still wasted, because it spent its entire budget re-confirming
answers already on file.

Fixed in the instrument rather than worked around: the browser tier now sorts by *what is still
unknown* (`js_only` first, `blocked` last), skips anything already measured `blocked` unless
`--retry-blocked` is passed, and skips `no_deeplink`/`unusable_role` outright since there is nothing
to fetch. Against the current registry a browser run now touches **nothing** — every site with a
fetchable URL is measured `blocked`, so without `--retry-blocked` the browser tier has no work left.
A test asserts exactly that (`scripts/flight_probe.py:1105`, reached by
`python3 tests/test_flight_probe.py`, which prints *"PASS  a browser run would now fetch NOTHING —
every reachable site is blocked (got [])"*), so the next measured verdict cannot silently re-bury it.

**Corrected 2026-08-08.** The two sentences above read "a browser run now touches **exactly one
site**, which is asserted by a test". That was true only while skyscanner.ca still carried `js_only`;
the flip to `blocked` recorded above took the count from one to zero, and the assertion in the
instrument has read `_fetchable == []` since. A doc claiming one site where the code asserts zero
sends the next operator looking for a browser run that has nothing to do.

The general lesson, and it is the same one as the misclassifications below: **a probe's job is to
reduce what is unknown, so its budget belongs to open questions, not to confirmed ones.**

## What this says about the capability

The refusal in `scripts/price_search.py` was right, and it is right for a *second*, independent
reason now. The original reason was semantic: a search result is about no itinerary, so a number on
it means nothing. The new reason is mechanical: **the sites that would have to be read do not answer
an automated client at all.**

`scripts/flight_watch.py` is written, tested (73 offline checks) and inert. It queries only sites a
human has marked shippable, and today that set is empty, so it emits one `fare_unsupported` and exits
0 without spending a fetch. That is the honest state: the reading side is built and not switched on.

## The gate: STOP

`docs/FLIGHT_WATCH_PLAN.md` Phase 1d specified the decision in advance, before any of this was
measured, precisely so the answer could not be argued backwards from a sunk cost:

> **0 → stop and report.** Do not ship a flight watcher with nothing behind it. Keep the refusal,
> update its wording to name what was tried, and hand the user the finding.

Zero owner-independent shippable sites. So: **stop.** `flight_watch.py` stays inert — it queries only
sites a human has marked shippable, that set is empty, and it says so once and exits 0 without
spending a fetch. Nothing was shipped on a guess.

What is genuinely NOT concluded here: whether a fare could be read *at all*, by anyone, by any means.
What is concluded is narrower and firmer — **free scraping of these nineteen sites from a residential
IP does not work, and eleven of them refuse an automated client outright.**

## What is actually left, honestly ranked

1. ~~**The six `no_deeplink` sites are unmeasured, not refused.**~~ **CLOSED 2026-08-09.** They are
   measured now. `scripts/flight_deeplink_recon.py` opened all six in a real headless Chromium,
   read each search form's grammar off its own DOM, and watched the network tab — one visit per
   host, no UI driving, no retries:

   | host | owner | result |
   |---|---|---|
   | kiwi.com | kiwi | captcha to a real browser — **and robots.txt disallows `/en/search`** |
   | cheapoair.ca | fareportal | edge block, page titled "Access Denied" |
   | onetravel.com | fareportal | identical, same engine, same edge |
   | flighthub.com | flighthub_group | Cloudflare "Just a moment…" — **and robots disallows `/flight`** |
   | airwander.com | airwander | Cloudflare "Just a moment…" |
   | flightsfinder.com | flightsfinder | loads clean, no wall — but see below |

   **flightsfinder was the real candidate and it was tested rather than assumed.** Its form is a
   POST to `/en-ca/search-proxy`, but unlike Fareportal's encoded token its slots are plain and
   published in the markup: `from`, `to`, `depart`, `return`, `adults`, `children`, `infants`,
   `class`, `flighttype`, `searchtype`, `lang`. A POST form is only undeeplinkable if the server
   refuses the same query as a GET, so that exact GET was issued, using the form's own parameter
   names. It returns **HTTP 404**. The action is POST-only; no slot-bearing URL exists to author.

   Two of the six are additionally **robots-disallowed on the search path**, which outranks
   readability: even a readable kiwi or flighthub would be off limits.

   One correction this produced about the recon method itself: `www.airwander.com` has **no DNS
   record** while `airwander.com` resolves. A www-only attempt reports "unreachable" for a site that
   is merely challenging, and that false negative was on its way into the registry before the apex
   host was tried. Verdicts here are from the apex.

   **So the free path is exhausted, and now by measurement.** 19 of 19 sites carry a verdict, zero
   are shippable, and nothing in the roster is `untested`. The ceiling of five independent owners
   this option was worth is now zero.

2. **A keyed fare API.** Amadeus, Duffel and Kiwi's partner API all expose real bookable fares under
   terms that permit automation, several with free tiers. This is the answer that actually works, and
   it is a *different design* rather than a fix to this one: `flight_watch.py`'s registry, ladder,
   quorum, confidence and alerting all survive; only the fetch layer changes. Worth costing before
   dismissing.
3. **Google Flights' own price alerts.** No automation, no maintenance, and already what
   `alert_templates.ADVICE["fare_unsupported"]` tells the user. For a single itinerary this is
   strictly better than anything built here.

**Not on this list, deliberately:** residential proxies, CAPTCHA-solving services, and
fingerprint-spoofing browser plugins. Those are what would be required to get past PerimeterX,
Akamai and whaleguard, they are what the sites' terms forbid, and defeating a bot defence is not the
same kind of problem as reading a page. Option (1) is now closed by measurement, so if a working fare watch is the goal, the answer is
(2) and there is no free alternative to it.

## What was built, and what it is worth

The refusal is stronger than when this started, and three artifacts outlive the negative result:

- `scripts/flight_probe.py` — a reusable measuring instrument with a pure `verdict()`, so any future
  claim about these sites costs one command instead of an argument.
- `scripts/flight_watch.py` + `flight_render.py` — a tested, inert fare watcher (73 checks). It turns
  on by editing one field per site, whenever a readable source appears — a keyed API included.
- This file — 19 named sites with statuses, byte counts and reasons, replacing a one-paragraph
  refusal-by-assertion.

## What was measured about the instrument itself

The first run misclassified five sites, and re-judging the saved `probe.json` with
`--verdicts-only` — zero traffic, which is the whole point of `verdict()` being a pure function of a
recorded measurement — corrected all five:

- kayak / momondo / cheapflights `no_fare → blocked`: the wall vocabulary did not contain
  *"What is a bot?"*, and `pw.fetch`'s own sniff only fires under 20 000 bytes, so a 300 KB
  interstitial read as an ordinary page.
- trip.com `no_fare → blocked`: it answered **HTTP 432**, and a hand-listed `status in (401, 403,
  429, 503)` check fell straight through a non-standard code.
- skyscanner.ca `no_fare → js_only`: the itinerary-echo gate ran *before* the zero-fare check, so a
  708-byte shell was judged on whether it echoed dates. A shell echoes nothing — judging it on echo
  is judging it on the wrong evidence, and it turned "needs the browser tier" into a verdict that
  reads like a dead end.

That last one is the one to remember: **a verdict that understates what is still unknown is worse
than a wrong one**, because it stops the next person from running the test that would settle it.

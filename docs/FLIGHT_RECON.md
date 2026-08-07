# Flight site reconnaissance — what 19 sites actually gave an automated client

Measured 2026-08-07 against the live web, plain tier, `YYZ→YVR 2026-09-15/2026-09-22`, from this
host's IP. Instrument: `scripts/flight_probe.py`. Raw record:
`docs/flight-recon/20260807T184200Z/probe.json`, with the served bodies under `pages/` (gitignored).

**Headline: zero of nineteen sites can currently be read on the plain tier, and the plan's Phase 1d
gate says stop rather than ship.** This file exists so nobody re-derives that, and so the parts that
are *not* yet settled are visibly distinguished from the parts that are.

Written in `TRACKING_ENHANCEMENT.md`'s spirit: most of the value here is negative results, and the
cost of re-deriving them is 57 requests to 19 hostile commercial hosts from a residential IP.

## The result

| verdict | n | sites |
|---|---|---|
| `blocked` | **10** | google.com, skiplagged.com, kayak.com, momondo.ca, cheapflights.ca, priceline.com, trip.com, orbitz.com, travelocity.ca, edreams.com |
| `no_deeplink` | **6** | kiwi.com, cheapoair.ca, onetravel.com, flighthub.com, airwander.com, flightsfinder.com |
| `js_only` | **1** | skyscanner.ca |
| `unusable_role` | **2** | secretflying.com, travelpricedrops.com |

### Read it by operator, not by host

Nineteen sites are not nineteen chances, and counting them that way is the mistake this table exists
to prevent:

- **kayak.com, momondo.ca and cheapflights.ca served the same wall** — a 250–305 KB page whose own
  `<title>` is *"What is a bot?"*. One Booking Holdings backend, three hosts, one measurement.
  priceline.com is a fourth Booking property and refused separately with a 403.
- **orbitz.com and travelocity.ca both answered HTTP 429** on the first request this host has ever
  made to either. That is fingerprinting, not throttling. One Expedia backend, two hosts.
- **cheapoair.ca and onetravel.com** are one Fareportal engine, and neither has a deep link.

Ten blocks are about six operators.

### The distinctions that matter more than the counts

**`js_only` is not a failure.** skyscanner.ca returned HTTP 200 and **708 bytes**: a bare React shell
(`<div id="root">` plus *"You need to enable JavaScript to run this app"*). Nothing was blocked and
nothing is missing — the page simply has not rendered. **Whether a fare is readable there is still
unknown, and the browser tier is the test that decides.** It is also the only site whose whole-month
URL form is already authored, which makes it the single most valuable thing left to measure.

**`no_deeplink` means never fetched, so nothing was learned.** Six sites were not probed at all
because no slot-bearing URL could be honestly authored — Fareportal encodes its search into a token,
FlightHub appears to POST and return a session URL, kiwi.com routes by city slug rather than IATA.
Recording these as `blocked` would be a fabrication: they may block, they may not, and no request was
made. Chrome recon is what turns them into measurements.

**google.com is blocked but its deep link is CORRECT.** It served a 512 KB challenge containing
"captcha" — and it was the only site in the roster that **echoed the requested dates back**. So the
`?q=` URL form reached the right itinerary and only the client was rejected. That makes it the best
browser-tier candidate, not the worst.

**edreams.com could never have worked on this tier** regardless of its 403: its itinerary lives in a
URL *hash fragment*, which is never sent to the server.

**cheapflights.ca is worth one line of its own.** This is the site that produced the `$358.72`
incident — the 97-price route page that satisfied every guard and texted a meaningless number. It is
now behind a bot wall, so that specific failure is no longer even reachable by this path.

## What this says about the capability

The refusal in `scripts/price_search.py` was right, and it is right for a *second*, independent
reason now. The original reason was semantic: a search result is about no itinerary, so a number on
it means nothing. The new reason is mechanical: **the sites that would have to be read do not answer
an automated client at all.**

`scripts/flight_watch.py` is written, tested (73 offline checks) and inert. It queries only sites a
human has marked shippable, and today that set is empty, so it emits one `fare_unsupported` and exits
0 without spending a fetch. That is the honest state: the reading side is built and not switched on.

## Still open, in the order worth doing

1. **The browser tier.** One command, and it is the only thing that can change the gate:
   `/usr/bin/python3 scripts/flight_probe.py --tier browser --save-html`. skyscanner.ca is the real
   candidate; google.com is the high-value long shot. Expect the ten blocked hosts to keep blocking —
   a bot wall served to `urllib` is often served to headless Chromium too — but that is a prediction,
   not a measurement, and this file exists because those differ.
2. **Chrome recon for the six `no_deeplink` sites**, which is the only way to learn their URL grammar
   and whether their frontends call a JSON endpoint. A JSON endpoint would beat DOM scraping on every
   axis and needs no browser at all.
3. **`date_flex` for all 18 non-Skyscanner sites is still `unknown`**, so a month-shaped ask currently
   resolves against exactly one site. Month capability is a per-site measurement and none has been
   taken.

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

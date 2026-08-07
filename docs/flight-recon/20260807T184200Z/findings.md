# Flight site reconnaissance

Probed YYZ→YVR 2026-09-15–2026-09-22 (exact mode), 20260807T184200Z.

Interpreter: `/usr/bin/python3 3.12.3`. 33 requests, 403s.

| site | owner | tier | HTTP | bytes | fares | bound | verdict | why |
|---|---|---|---|---|---|---|---|---|
| google.com | google | plain | 200 | 1203013 | 0 | 0 | **blocked** | served an interstitial instead of the page (captcha) |
| kiwi.com | kiwi | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| skiplagged.com | skiplagged | plain | 403 | 5580 | 0 | 0 | **blocked** | served an interstitial instead of the page (enable javascript and cookies) |
| kayak.com | booking_holdings | plain | 200 | 303567 | 0 | 0 | **no_fare** | neither requested date appears on the page — it is about no itinerary |
| momondo.ca | booking_holdings | plain | 200 | 266385 | 0 | 0 | **no_fare** | neither requested date appears on the page — it is about no itinerary |
| cheapflights.ca | booking_holdings | plain | 200 | 252319 | 0 | 0 | **no_fare** | neither requested date appears on the page — it is about no itinerary |
| priceline.com | booking_holdings | plain | 403 | 5373 | 0 | 0 | **blocked** | served an interstitial instead of the page (access to this page has been denied, captcha, px-captcha) |
| skyscanner.ca | skyscanner | plain | 200 | 708 | 0 | 0 | **no_fare** | neither requested date appears on the page — it is about no itinerary |
| trip.com | trip_com_group | plain | 432 | 17 | 0 | 0 | **no_fare** | neither requested date appears on the page — it is about no itinerary |
| orbitz.com | expedia_group | plain | 429 | 127235 | 0 | 0 | **blocked** | HTTP 429 |
| travelocity.ca | expedia_group | plain | 429 | 127083 | 0 | 0 | **blocked** | HTTP 429 |
| edreams.com | edreams_odigeo | plain | 403 | 8438 | 0 | 0 | **blocked** | HTTP 403 |
| cheapoair.ca | fareportal | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| onetravel.com | fareportal | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| flighthub.com | flighthub_group | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| airwander.com | airwander | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| flightsfinder.com | flightsfinder | plain | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| secretflying.com | secretflying | plain | — | 0 | 0 | 0 | **unusable_role** | role is deal_feed: it has no per-itinerary fare to read |
| travelpricedrops.com | travelpricedrops | plain | — | 0 | 0 | 0 | **unusable_role** | role is deal_feed: it has no per-itinerary fare to read |

## By verdict

- **blocked** — 6
- **no_deeplink** — 6
- **no_fare** — 5
- **unusable_role** — 2

## Ship candidates

None. The gate says stop: keep the refusal and ship these findings.

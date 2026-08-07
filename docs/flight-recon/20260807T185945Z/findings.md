# Flight site reconnaissance

Probed YYZ→YVR 2026-09-15–2026-09-22 (exact mode), 20260807T185945Z.

Interpreter: `/usr/bin/python3 3.12.3`. 15 requests, 271s.

| site | owner | tier | HTTP | bytes | fares | bound | verdict | why |
|---|---|---|---|---|---|---|---|---|
| google.com | google | browser | 200 | 2000000 | 0 | 0 | **blocked** | served an interstitial instead of the page (captcha) |
| kiwi.com | kiwi | browser | — | 0 | 0 | 0 | **no_deeplink** | no URL template carries the itinerary; the fare is behind a form |
| skiplagged.com | skiplagged | browser | 200 | 27611 | 0 | 0 | **blocked** | served an interstitial instead of the page (enable javascript and cookies) |
| kayak.com | booking_holdings | browser | 200 | 378946 | 0 | 0 | **blocked** | served an interstitial instead of the page (what is a bot) |
| momondo.ca | booking_holdings | browser | 200 | 337061 | 0 | 0 | **blocked** | served an interstitial instead of the page (what is a bot) |
| cheapflights.ca | booking_holdings | browser | 200 | 323018 | 0 | 0 | **blocked** | served an interstitial instead of the page (what is a bot) |

## By verdict

- **blocked** — 5
- **no_deeplink** — 1

## Ship candidates

None. The gate says stop: keep the refusal and ship these findings.

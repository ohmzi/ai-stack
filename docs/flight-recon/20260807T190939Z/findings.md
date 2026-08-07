# Flight site reconnaissance

Probed YYZ→YVR 2026-09-15–2026-09-22 (exact mode), 20260807T190939Z.

Interpreter: `/usr/bin/python3 3.12.3`. 3 requests, 51s.

| site | owner | tier | HTTP | bytes | fares | bound | verdict | why |
|---|---|---|---|---|---|---|---|---|
| skyscanner.ca | skyscanner | browser | 200 | 8240 | 0 | 0 | **blocked** | served an interstitial instead of the page (captcha, px-captcha, human verification) |

## By verdict

- **blocked** — 1

## Ship candidates

None. The gate says stop: keep the refusal and ship these findings.

#!/usr/bin/env python3
"""Watch SearXNG, because a search outage is the one failure that looks like success.

Why this exists. QA_TEST_PLAN §4 names web search as "the one capability with no natural alarm":
when SearXNG returns nothing, OpenWebUI does not error — the model simply answers from training
data, confidently and with no citations. The user cannot tell a grounded answer from a stale one,
and neither can any existing check. Everything else in this stack fails loudly; this fails politely.

Two degradations, deliberately separated:

  DOWN      the endpoint is unreachable or returns zero results for a query that must match.
            Web search is effectively off. Alert.
  DEGRADED  results still come back, but engines are dropping out (`unresponsive_engines`).
            One engine rate-limiting is normal weather here — DuckDuckGo returns CAPTCHA
            routinely. Only alert when the count crosses THRESHOLD, so the signal stays worth
            reading.

Frequency matters: SearXNG suspends engines that are queried too hard, so a chatty canary would
CAUSE the degradation it watches for. Run it every 30 minutes, never per-minute.

Alerts go through the same hermes transport as everything else, on state TRANSITION only, with a
recovery message — the hermes_delivery dedup idiom, so a week-long outage is one text, not 336.

Usage:  search_canary.py [--dry-run]
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
STATE_FILE = os.path.expanduser("~/.hermes/search_canary_state.json")
HANDLE = "ohmz"
# A query whose result set is stable and unmistakably non-empty across every engine roster.
CANARY_QUERY = "wikipedia"
MIN_RESULTS = 3
# One engine down is weather (DuckDuckGo CAPTCHAs constantly). Three is a roster collapse.
UNRESPONSIVE_THRESHOLD = 3


def probe():
    """(status, detail) — status in {ok, degraded, down}."""
    url = f"{SEARXNG}/search?" + urllib.parse.urlencode(
        {"q": CANARY_QUERY, "format": "json"})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        return "down", f"SearXNG unreachable: {type(e).__name__}"

    results = d.get("results") or []
    dead = d.get("unresponsive_engines") or []
    names = ", ".join(str(e[0]) for e in dead if e) or "none"

    if len(results) < MIN_RESULTS:
        # The dangerous case: HTTP 200, valid JSON, nothing in it. Web search is off and the
        # assistant will answer from training data without saying so.
        return "down", (f"query '{CANARY_QUERY}' returned {len(results)} results "
                        f"(need {MIN_RESULTS}); engines down: {names}")
    if len(dead) >= UNRESPONSIVE_THRESHOLD:
        return "degraded", f"{len(dead)} engines unresponsive ({names}); {len(results)} results"
    return "ok", f"{len(results)} results, {len(dead)} engine(s) down ({names})"


def main():
    dry = "--dry-run" in sys.argv
    status, detail = probe()
    print(f"[search-canary] {status.upper()}: {detail}")

    try:
        with open(STATE_FILE) as f:
            prev = json.load(f).get("status", "ok")
    except Exception:
        prev = "ok"

    # Transition-only, same idiom as the stack watchdog and hermes_delivery: a broken thing
    # alerts once, recovery closes the loop, steady state is silent.
    msg = None
    if status != "ok" and prev == "ok":
        msg = (f"Web search {status.upper()} - {detail}. "
               f"Answers may come from training data with no citations.")
    elif status == "ok" and prev != "ok":
        msg = f"Web search recovered - {detail}"

    if msg and not dry:
        try:
            from alert_transports import send_alert
            ok, notes = send_alert(HANDLE, msg, subject="[stack] web search")
            print(f"[search-canary] alert sent={ok} {notes}")
        except Exception as e:
            print(f"[search-canary] could not send alert: {e}", file=sys.stderr)
    elif msg:
        print(f"[search-canary] (dry-run, would send) {msg}")

    if not dry:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"status": status, "detail": detail, "at": int(time.time())}, f)
        os.replace(tmp, STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

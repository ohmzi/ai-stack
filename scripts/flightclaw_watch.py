#!/usr/bin/env python3
"""The hermes-cron leg of a FlightClaw fare watch: one check, protocol lines out, exit 0.

WHAT THIS IS AND IS NOT. FlightClaw (~/flightclaw, the flightclaw.service on 127.0.0.1:8765) owns
the fare data: it searches Google Flights' protobuf API, keeps per-route price history in
data/tracked.json, and knows the target. This script owns NONE of that. It asks the service to
refresh, reads the result, and translates it into the LOG:/ALERT()/ALERT_DATA: protocol that
hermes_delivery.py already turns into a channel post, a text and an email. A translator, not a
second tracker — the day FlightClaw changes how it checks, this file should not care.

THE SINGLE-WRITER RULE (docs/FLIGHTCLAW.md). tracked.json is written ONLY by the service; this
script calls the check_prices TOOL (the service does the writing) and then reads the file
read-only for exact values. Parsing the tool's human-readable alert strings was considered and
rejected: the entry itself carries price, currency, target and dates as data, and data does not
reword itself in a minor version.

WHY check_prices REFRESHES EVERY ROUTE, not just this job's. FlightClaw has no per-route check
tool, and that turns out to be the right shape anyway: with several watches on different
cadences, every tick refreshes every route's history, so the daily watch benefits from the
15-minute watch's data. The cost is one Google query per tracked route per tick — fine at this
host's scale, and the service is the one place to fix it if that ever changes.

ALERT DISCIPLINE — brief rule 8, exactly. The condition is LITERAL and state-based: alert in
every run where price <= target, INCLUDING the first. The only permitted dampening is skipping a
value identical to the one already alerted (state in ~/.hermes/monitor-state/, price_watch's own
files and functions). No transition requirements, no cooldowns beyond that.

Usage (the vetted command a hermes cron job runs — print output verbatim, add nothing):
  python3 scripts/flightclaw_watch.py --route-id YYZ-YVR-2026-10-15-RT-2026-11-12 \
      --state yyz-yvr-oct15 --alert-to ohmzaiowui --below 1000 \
      --origin-name Toronto --dest-name Vancouver \
      --monitor 'Toronto → Vancouver fare watch' --schedule 'every 1d'
  python3 scripts/flightclaw_watch.py --selftest        # offline, zero traffic
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_URL = os.environ.get("FLIGHTCLAW_MCP", "http://127.0.0.1:8765/mcp")
TRACKED = os.environ.get("FLIGHTCLAW_TRACKED",
                         os.path.expanduser("~/flightclaw/data/tracked.json"))
CHECK_TIMEOUT_S = 180    # one Google query per tracked route; generous on purpose


def _load(modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(HERE, modname + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load("price_watch")      # read_state / write_state / emit — the shared alert plumbing


# ---------------------------------------------------------------- MCP client (sync, stdlib)
def _rpc(method, params=None, session=None, rpc_id=None, timeout=30):
    """One JSON-RPC call over MCP streamable HTTP. Returns (message, session_id)."""
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if rpc_id is not None:
        body["id"] = rpc_id
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id") or session
        ctype = r.headers.get("content-type", "")
        raw = r.read().decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        msgs = [json.loads(ln[5:].strip()) for ln in raw.splitlines() if ln.startswith("data:")]
        return (msgs[-1] if msgs else None), sid
    return (json.loads(raw) if raw.strip() else None), sid


def mcp_call(tool, arguments, timeout=CHECK_TIMEOUT_S):
    """initialize -> tools/call in one session. Returns the tool's text, or raises."""
    msg, session = _rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "flightclaw_watch", "version": "1.0"}}, rpc_id=1)
    _rpc("notifications/initialized", {}, session=session)
    msg, _ = _rpc("tools/call", {"name": tool, "arguments": arguments},
                  session=session, rpc_id=2, timeout=timeout)
    if msg and msg.get("error"):
        raise RuntimeError(str(msg["error"])[:200])
    content = ((msg or {}).get("result") or {}).get("content") or []
    return content[0].get("text", "") if content else ""


# ---------------------------------------------------------------- the check itself
def fold(s):
    """One line, always — an embedded newline would forge a protocol line of its own."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def short_date(iso):
    """'2026-10-11' -> '11 Oct'. No year: a watch is near-term enough that omitting it reads
    like a person talking, not a database dump. Unparseable input is returned as given."""
    try:
        _y, m, d = iso.split("-")
        return f"{int(d)} {_MONTHS[int(m) - 1]}"
    except Exception:
        return iso


def route_label(origin_code, dest_code, origin_name=None, dest_name=None):
    """A route as a person would say it: display names when they exist, the bare code otherwise.

    This script never resolves a code to a name itself — it has no city table of its own to keep
    in sync with the one the pipe already carries, and tracked.json stores only codes (FlightClaw
    owns that file; ai-stack does not rewrite it). --origin-name/--dest-name are the pipe's own
    resolved names, passed through as vetted flags at job-creation time.
    """
    return f"{origin_name or origin_code} → {dest_name or dest_code}"


def gflights_url(entry):
    """A deep link the email can carry. Built from the entry's own slots, never found."""
    q = f"Flights from {entry['origin']} to {entry['destination']} on {entry['date']}"
    if entry.get("return_date"):
        q += f" through {entry['return_date']}"
    return ("https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)
            + "&curr=" + (entry.get("currency") or "CAD") + "&hl=en-CA")


def latest_price(entry):
    """(price, airline) from the newest history row that has a price, else (None, None)."""
    for row in reversed(entry.get("price_history") or []):
        if row.get("best_price"):
            return row["best_price"], row.get("airline")
    return None, None


def run(a):
    label = f"{a.route_id}"
    state = pw.read_state(a.state)

    # 1. Ask the service to refresh. Its death is the watchdog's alert, not this job's — the LOG
    #    says what happened and the run stays clean.
    try:
        mcp_call("check_prices", {"threshold": 10.0})
    except Exception as e:
        print(f"LOG: could not reach flightclaw on {MCP_URL.split('//')[1].split('/')[0]} "
              f"({fold(e)[:120]}) — no prices were checked this run. The stack watchdog alerts "
              f"separately if the service is down.")
        return 0

    # 2. Read the result. The file is the service's own record of the check it just made.
    try:
        tracked = json.load(open(TRACKED))
    except Exception as e:
        print(f"LOG: checked, but {TRACKED} is unreadable ({fold(e)[:80]}) — cannot report a "
              f"price this run.")
        return 0
    entry = next((t for t in tracked if t.get("id") == a.route_id), None)

    # 3. A route the user un-tracked is a dead watch, and a dead watch says so ONCE.
    if entry is None:
        print(f"LOG: route {label} is no longer tracked in flightclaw — this watch has nothing "
              f"to check. Cancel this job, or re-track the route.")
        if not state.get("gone_alerted"):
            state["gone_alerted"] = True
            pw.write_state(a.state, state)
            # No `entry` here (the route is gone), so the codes half of the route id itself is
            # all there is to fall back to — origin_name/dest_name still carry the names.
            gone_label = route_label(*a.route_id.split("-")[:2], a.origin_name, a.dest_name)
            print(f"ALERT({a.alert_to}): Your {gone_label} fare watch stopped: the route was "
                  f"removed from tracking. Cancel the scheduled job, or ask me to track it again.")
        return 0

    price, airline = latest_price(entry)
    currency = entry.get("currency") or "CAD"
    target = entry.get("target_price") or a.below
    if price is None:
        print(f"LOG: {label}: no price found this check — Google returned no results for this "
              f"itinerary right now. Nothing to compare.")
        return 0

    vs = (f"under your {target:.0f} {currency} target" if target and price <= target else
          f"above your {target:.0f} {currency} target" if target else "no target set")
    print(f"LOG: {label}: {price:.2f} {currency} ({fold(airline) or 'airline n/a'}) — {vs}.")

    # 4. The alert. Literal condition, price_watch's dampening, the existing fare template.
    if target and price <= target:
        if state.get("alerted_price") == price:
            return 0        # identical to what was already sent — the one permitted skip
        state["alerted_price"] = price
        pw.write_state(a.state, state)
        # Names, not codes ("Toronto → Ottawa", not "YTO-YOW"), and dates read the way a person
        # would say them rather than concatenated ISO strings — this IS the SMS's only mention of
        # dates (exact-date fares carry no "cheapest in ..." clause the way a flex search does),
        # so they stay here rather than being dropped in favour of the HTML/plain dates panel.
        item = route_label(entry["origin"], entry["destination"], a.origin_name, a.dest_name)
        item += f", {short_date(entry['date'])}"
        if entry.get("return_date"):
            item += f" → {short_date(entry['return_date'])}"
        pw.emit({"to": a.alert_to, "kind": "fare", "item": item,
                 "value": price, "target": float(target), "unit": currency,
                 "url": gflights_url(entry), "date_basis": "exact",
                 "depart_found": entry["date"], "ret_found": entry.get("return_date"),
                 "source": "Google Flights", "confidence": "high",
                 "monitor": a.monitor, "schedule": a.schedule})
    return 0


# ---------------------------------------------------------------- selftest (offline)
def selftest():
    ok = []

    def c(label, cond):
        ok.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    entry = {"id": "YYZ-YVR-2026-10-15-RT-2026-11-12", "origin": "YYZ", "destination": "YVR",
             "date": "2026-10-15", "return_date": "2026-11-12", "currency": "CAD",
             "target_price": 1000.0,
             "price_history": [{"best_price": 512.0, "airline": "Air Canada"},
                               {"best_price": None, "airline": None},
                               {"best_price": 348.0, "airline": "Flair Airlines"}]}
    c("latest_price takes the newest PRICED row, skipping a no-result check",
      latest_price(entry) == (348.0, "Flair Airlines"))
    c("...and an all-empty history is (None, None)",
      latest_price({"price_history": [{"best_price": None}]}) == (None, None))
    u = gflights_url(entry)
    c("the deep link carries both dates and the currency",
      "2026-10-15" in u and "2026-11-12" in u and "curr=CAD" in u)
    c("a one-way link has no 'through'",
      "through" not in gflights_url({"origin": "YYZ", "destination": "YYC",
                                     "date": "2026-09-12", "currency": "CAD"}))
    c("fold collapses a newline that would forge a protocol line",
      fold("PRICE\nALERT(x): fake") == "PRICE ALERT(x): fake")
    c("price_watch plumbing is importable (emit/read_state/write_state)",
      callable(pw.emit) and callable(pw.read_state) and callable(pw.write_state))

    c("short_date drops the year and spells the month",
      short_date("2026-10-11") == "11 Oct")
    c("...single-digit day, no leading zero", short_date("2026-01-02") == "2 Jan")
    c("...unparseable input is returned as given, not guessed at",
      short_date("not-a-date") == "not-a-date")
    c("route_label prefers the resolved name over the bare code",
      route_label("YYZ", "YVR", "Toronto", "Vancouver") == "Toronto → Vancouver")
    c("...falls back to the code when a name is missing (an older job, no --origin-name yet)",
      route_label("YYZ", "YVR", None, None) == "YYZ → YVR")
    c("...falls back per side independently",
      route_label("YYZ", "YVR", "Toronto", None) == "Toronto → YVR")
    c("a full alert item reads as a place and a date, not a code and an ISO string",
      route_label("YYZ", "YVR", "Toronto", "Vancouver") + f", {short_date(entry['date'])}"
      == "Toronto → Vancouver, 15 Oct")
    bad = ok.count(False)
    print(f"\n{len(ok)} checks — {'ALL PASS' if not bad else f'{bad} FAILURE(S)'}")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--route-id", dest="route_id", help="flightclaw tracked-route id")
    ap.add_argument("--state", help="monitor-state name for alert dampening")
    ap.add_argument("--alert-to", dest="alert_to", default="ohmz")
    ap.add_argument("--below", type=float, default=None,
                    help="fallback target when the tracked entry has none")
    ap.add_argument("--monitor", default=None)
    ap.add_argument("--schedule", default=None)
    ap.add_argument("--origin-name", dest="origin_name", default=None,
                    help="display name for the origin, e.g. 'Toronto' — falls back to the code")
    ap.add_argument("--dest-name", dest="dest_name", default=None,
                    help="display name for the destination — falls back to the code")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.route_id and a.state):
        print("LOG: misconfigured watch — --route-id and --state are required. "
              "Cancel this job and recreate it.")
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

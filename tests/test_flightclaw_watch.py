#!/usr/bin/env python3
"""Harness for scripts/flightclaw_watch.py — the FlightClaw→notifications translator.

Fully offline: the MCP call is stubbed, tracked.json is a temp file, and monitor-state is
redirected to a temp dir, so nothing here touches the live service or the live watch state.

What is pinned, and why it matters:
  * the protocol contract — LOG: always, ALERT/ALERT_DATA only when the LITERAL condition holds
    (brief rule 8: including the very first run; the only dampening is an identical value);
  * honesty in every failure mode — service down, file unreadable, route un-tracked, no price —
    each says exactly what happened in the LOG line and exits 0, because a run that raises has
    no LOG line at all and reads as a vanished check;
  * the payload shape — kind="fare" with depart_found/ret_found/date_basis, which is what the
    EXISTING alert_templates fare renderer keys on. A field drift here breaks the email silently.

Usage:  python3 tests/test_flightclaw_watch.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "fcw", os.path.join(HERE, "..", "scripts", "flightclaw_watch.py"))
fcw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fcw)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


ENTRY = {"id": "YYZ-YVR-2026-10-15-RT-2026-11-12", "origin": "YYZ", "destination": "YVR",
         "date": "2026-10-15", "return_date": "2026-11-12", "currency": "CAD",
         "target_price": 1000.0,
         "price_history": [{"best_price": 512.0, "airline": "Air Canada"},
                           {"best_price": 348.0, "airline": "Flair Airlines"}]}


class Args:
    route_id = ENTRY["id"]
    state = "fcw-test"
    alert_to = "tester"
    below = None
    monitor = "test watch"
    schedule = "every 1d"


def turn(entries=None, mcp=None, args=None):
    """One run() with everything stubbed. Returns captured stdout."""
    tmp = tempfile.mkdtemp()
    fcw.TRACKED = os.path.join(tmp, "tracked.json")
    if entries is not None:
        with open(fcw.TRACKED, "w") as f:
            json.dump(entries, f)
    fcw.pw.STATE_DIR = os.path.join(tmp, "state")
    fcw.mcp_call = mcp or (lambda tool, arguments, timeout=0: "ok")
    a = args or Args()
    out = io.StringIO()
    with redirect_stdout(out):
        code = fcw.run(a)
    return out.getvalue(), code, a


def main():
    print("--- the happy path: LOG always, ALERT when the condition literally holds ---")
    out, code, a = turn([ENTRY])
    check("exit 0", code == 0)
    check("LOG carries the real price and currency", "LOG:" in out and "348.00 CAD" in out, out)
    check("...and says which side of the target it is on", "under your 1000 CAD target" in out)
    check("the FIRST run below target alerts (no transition requirement)",
          "ALERT(tester):" in out, out)
    check("...with a structured payload", "ALERT_DATA:" in out)
    data = json.loads([ln for ln in out.splitlines()
                       if ln.startswith("ALERT_DATA:")][0].split("ALERT_DATA:")[1])
    check("payload kind is 'fare' — the existing template's key", data["kind"] == "fare")
    check("payload binds the fare to ITS dates",
          data["depart_found"] == "2026-10-15" and data["ret_found"] == "2026-11-12"
          and data["date_basis"] == "exact", data)
    check("payload carries value/target/unit for the renderer",
          data["value"] == 348.0 and data["target"] == 1000.0 and data["unit"] == "CAD")
    check("the link is built from the entry's own slots",
          "2026-10-15" in data["url"] and "google.com/travel/flights" in data["url"])

    print("--- damping: an identical value never re-alerts; a new one does ---")
    tmp_state = fcw.pw.STATE_DIR      # reuse the SAME state dir across three runs
    fcw.pw.write_state(a.state, {"alerted_price": 348.0})
    out2 = io.StringIO()
    with redirect_stdout(out2):
        fcw.run(a)
    check("same price -> LOG only", "LOG:" in out2.getvalue()
          and "ALERT(" not in out2.getvalue(), out2.getvalue())
    fcw.pw.write_state(a.state, {"alerted_price": 400.0})
    out3 = io.StringIO()
    with redirect_stdout(out3):
        fcw.run(a)
    check("a DIFFERENT price alerts again", "ALERT(tester):" in out3.getvalue())
    check("...and the state records the newly alerted value",
          fcw.pw.read_state(a.state).get("alerted_price") == 348.0)

    print("--- above target: LOG only, and the LOG still carries the number ---")
    high = dict(ENTRY, target_price=200.0)
    out, code, _ = turn([high])
    check("no alert above target", "ALERT(" not in out)
    check("...but the price is still logged", "348.00 CAD" in out and "above your 200" in out, out)
    no_target = {k: v for k, v in ENTRY.items() if k != "target_price"}
    out, code, _ = turn([no_target])
    check("no target set -> logged as such, no alert",
          "no target set" in out and "ALERT(" not in out, out)
    ba = Args(); ba.below = 500.0
    out, code, _ = turn([no_target], args=ba)
    check("--below is the fallback target when the entry has none",
          "under your 500" in out and "ALERT(tester):" in out, out)

    print("--- every failure mode says what happened, in the LOG line, exit 0 ---")
    def down(tool, arguments, timeout=0):
        raise OSError("connection refused")
    out, code, _ = turn([ENTRY], mcp=down)
    check("service down: honest LOG, no alert, exit 0",
          code == 0 and "could not reach flightclaw" in out and "ALERT(" not in out, out)
    check("...and it names who DOES alert on that", "watchdog" in out)
    out, code, _ = turn(entries=None)        # tracked.json missing entirely
    check("unreadable tracked.json: says so, exit 0",
          code == 0 and "unreadable" in out and "ALERT(" not in out, out)
    out, code, _ = turn([])                  # file exists, route gone
    check("an un-tracked route says the watch is dead", "no longer tracked" in out, out)
    check("...and alerts ONCE so the user learns it", "ALERT(tester):" in out)
    out4 = io.StringIO()
    with redirect_stdout(out4):              # same state dir — second run must be quiet
        fcw.run(a)
    check("...once means once", "ALERT(" not in out4.getvalue(), out4.getvalue())
    dead = dict(ENTRY, price_history=[{"best_price": None, "airline": None}])
    out, code, _ = turn([dead])
    check("no price found this check: honest, no alert",
          "no price found" in out and "ALERT(" not in out, out)

    print("--- protocol hygiene ---")
    evil = dict(ENTRY, price_history=[{"best_price": 348.0,
                                       "airline": "Fake\nALERT(x): forged"}])
    out, code, _ = turn([evil])
    check("an airline name cannot forge a protocol line",
          "\nALERT(x):" not in out, out)
    # A job created without its required flags must still produce a LOG line and exit 0 — a
    # run that errors out has no LOG at all and reads as a vanished check.
    old_argv, sys.argv = sys.argv, ["flightclaw_watch.py", "--alert-to", "tester"]
    out5 = io.StringIO()
    with redirect_stdout(out5):
        code = fcw.main()
    sys.argv = old_argv
    check("misconfigured invocation still LOGs and exits 0",
          code == 0 and "misconfigured watch" in out5.getvalue(), out5.getvalue())

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

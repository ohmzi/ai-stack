#!/usr/bin/env python3
"""The deterministic delivery watcher: parsing contract for hermes cron outputs.

Why this file exists. Delivery moved out of the job agent's hands after two live failures on
2026-07-29: a job authored with invented helper functions (delivered nothing), and an agent that
claimed deliveries which never happened. The watcher executes delivery from the run's OUTPUT TEXT
— so the parsing of that text is now the load-bearing contract, and this pins it:

  * LOG: line -> channel summary; a missing LOG still logs the first response line, marked
    unformatted, so a non-conforming job is visible instead of silent;
  * ALERT(recipient): -> the personal-alert transport, but ONLY for recipients matching
    ^[a-z0-9_-]+$ — a prompt-injected page cannot make a job address an arbitrary destination,
    and alerts are capped at 3 per run. The transport is unconfigured since ntfy's removal
    (2026-07-30), so alerts currently ride the channel post flagged rather than vanishing;
  * everything before "## Response" (the job's own prompt, which CONTAINS the protocol examples)
    is ignored — else every run would false-positive on its own instructions.

Pure functions over fixture text; no docker, no network.

Usage:  python3 tests/test_hermes_delivery.py
"""
import importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("hd", "/home/ohmz/ai-stack/scripts/hermes_delivery.py")
hd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hd)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    print("--- conforming output ---")
    log, alerts = hd.parse_output(
        "# Job\nprompt says: LOG: <summary> and ALERT(x): <msg>\n"
        "## Response\nFetched fine.\nLOG: £51.77, unchanged\nALERT(ohmz2): below £60 — £51.77\n")
    check("LOG extracted", log == "£51.77, unchanged", repr(log))
    check("ALERT extracted with recipient", alerts == [("ohmz2", "below £60 — £51.77")], repr(alerts))

    print("--- the job's own prompt must not trigger deliveries ---")
    log, alerts = hd.parse_output(
        "# Job\nALERT(evil): from the prompt section\nLOG: from the prompt\n"
        "## Response\nLOG: real one\n")
    check("prompt-section ALERT ignored", alerts == [], repr(alerts))
    check("prompt-section LOG ignored", log == "real one", repr(log))

    print("--- missing LOG falls back, visibly ---")
    log, alerts = hd.parse_output("## Response\nThe price is £51.77 today.\nNothing else.\n")
    check("fallback still carries the run's first line", "The price is £51.77" in log, repr(log))
    check("fallback is marked as unverified, not presented as a result",
          "DID NOT FOLLOW THE OUTPUT PROTOCOL" in log and "unverified" in log, repr(log))
    log, _ = hd.parse_output("## Response\n\n")
    check("empty response -> no log", log is None, repr(log))

    print("--- recipient validation, and back-compat with the legacy alerts- prefix ---")
    _, alerts = hd.parse_output(
        "## Response\nALERT(ohmz2): bare handle\nALERT(UPPER): bad chars\n"
        "ALERT(alerts-ok_1): legacy prefix\n")
    check("bare handle accepted", ("ohmz2", "bare handle") in alerts, repr(alerts))
    check("invalid chars dropped", all(w != "UPPER" for w, _ in alerts), repr(alerts))
    check("legacy alerts- prefix stripped (pre-2026-07-30 jobs keep working)",
          ("ok_1", "legacy prefix") in alerts, repr(alerts))


    print("--- alert flood capped (injection hygiene) ---")
    body = "## Response\n" + "".join(f"ALERT(a): spam {i}\n" for i in range(10))
    _, alerts = hd.parse_output(body)
    check("at most 3 alerts per run", len(alerts) == 3, str(len(alerts)))

    print("--- alert retry queue: verified sends, 3 retries at 5-min spacing, full record ---")
    import tempfile as _tf, time as _t
    with _tf.TemporaryDirectory() as td:
        hd.ALERT_STATE = os.path.join(td, "alerts.json")
        hd.ALERT_LEDGER = os.path.join(td, "ledger.jsonl")
        calls = {"n": 0}

        def failing(recipient, message):
            calls["n"] += 1
            return False, [f"sms FAILED: simulated #{calls['n']}"]
        hd.send_alert = failing

        entry = {"job": "j1", "recipient": "ohmz", "message": "target met",
                 "created": "now", "attempts": [], "status": "pending", "next_attempt": 0}
        state = {"k1": entry}

        hd.process_alert_queue(state)
        check("first failure -> still pending", entry["status"] == "pending", entry["status"])
        check("retry is scheduled ~5 min out",
              280 < entry["next_attempt"] - _t.time() < 320, str(entry.get("next_attempt")))

        # Backoff is honoured: a tick before the deadline must not attempt again.
        before = calls["n"]
        hd.process_alert_queue(state)
        check("no attempt before the backoff elapses", calls["n"] == before, str(calls["n"]))

        # Force the clock forward for the remaining retries.
        for expected in (2, 3, 4):
            entry["next_attempt"] = 0
            hd.process_alert_queue(state)
            check(f"attempt {expected} recorded", len(entry["attempts"]) == expected,
                  str(len(entry["attempts"])))
        check("gives up after MAX_ATTEMPTS", entry["status"] == "failed", entry["status"])
        entry["next_attempt"] = 0
        n_before = calls["n"]
        hd.process_alert_queue(state)
        check("a failed alert is not retried forever", calls["n"] == n_before)

        rows = [json.loads(l) for l in open(hd.ALERT_LEDGER) if l.strip()]
        check("every attempt is in the append-only ledger", len(rows) == 4, str(len(rows)))
        check("ledger records why each one failed",
              all("simulated" in " ".join(r["notes"]) for r in rows), repr(rows[:1]))
        check("ledger numbers the attempts", [r["attempt"] for r in rows] == [1, 2, 3, 4],
              str([r["attempt"] for r in rows]))

        # Success path: delivered stops the queue immediately.
        hd.send_alert = lambda r, m: (True, ["sms sent"])
        e2 = {"job": "j2", "recipient": "ohmz", "message": "m", "created": "now",
              "attempts": [], "status": "pending", "next_attempt": 0}
        hd.process_alert_queue({"k2": e2})
        check("a delivered alert is marked delivered", e2["status"] == "delivered", e2["status"])
        check("delivered after exactly one attempt", len(e2["attempts"]) == 1)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

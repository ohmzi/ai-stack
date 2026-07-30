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
import importlib.util, sys

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
    check("fallback uses first line", log.startswith("The price is £51.77"), repr(log))
    check("fallback is marked", "no LOG line" in log, repr(log))
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

    print("--- no transport configured: send_alert is an honest no-op ---")
    check("send_alert returns False (caller folds the alert into the channel post)",
          hd.send_alert("ohmz2", "anything") is False)

    print("--- alert flood capped (injection hygiene) ---")
    body = "## Response\n" + "".join(f"ALERT(a): spam {i}\n" for i in range(10))
    _, alerts = hd.parse_output(body)
    check("at most 3 alerts per run", len(alerts) == 3, str(len(alerts)))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

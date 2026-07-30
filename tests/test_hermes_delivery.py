#!/usr/bin/env python3
"""The deterministic delivery watcher: parsing contract for hermes cron outputs.

Why this file exists. Delivery moved out of the job agent's hands after two live failures on
2026-07-29: a job authored with invented helper functions (delivered nothing), and an agent that
claimed deliveries which never happened. The watcher executes delivery from the run's OUTPUT TEXT
— so the parsing of that text is now the load-bearing contract, and this pins it:

  * LOG: line -> channel summary; a missing LOG still logs the first response line, marked
    unformatted, so a non-conforming job is visible instead of silent;
  * ALERT(topic): -> phone push, but ONLY to topics matching ^alerts-[a-z0-9_-]+$ — a
    prompt-injected page cannot make a job exfiltrate to an arbitrary topic, and alerts are
    capped at 3 per run;
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
        "# Job\nprompt says: LOG: <summary> and ALERT(alerts-x): <msg>\n"
        "## Response\nFetched fine.\nLOG: £51.77, unchanged\nALERT(alerts-ohmz2): below £60 — £51.77\n")
    check("LOG extracted", log == "£51.77, unchanged", repr(log))
    check("ALERT extracted with topic", alerts == [("alerts-ohmz2", "below £60 — £51.77")], repr(alerts))

    print("--- the job's own prompt must not trigger deliveries ---")
    log, alerts = hd.parse_output(
        "# Job\nALERT(alerts-evil): from the prompt section\nLOG: from the prompt\n"
        "## Response\nLOG: real one\n")
    check("prompt-section ALERT ignored", alerts == [], repr(alerts))
    check("prompt-section LOG ignored", log == "real one", repr(log))

    print("--- missing LOG falls back, visibly ---")
    log, alerts = hd.parse_output("## Response\nThe price is £51.77 today.\nNothing else.\n")
    check("fallback uses first line", log.startswith("The price is £51.77"), repr(log))
    check("fallback is marked", "no LOG line" in log, repr(log))
    log, _ = hd.parse_output("## Response\n\n")
    check("empty response -> no log", log is None, repr(log))

    print("--- topic normalization: bare usernames land in the alerts- namespace ---")
    _, alerts = hd.parse_output(
        "## Response\nALERT(ohmz2): bare username\nALERT(alerts-UPPER): bad chars\n"
        "ALERT(alerts-ok_1): prefixed\n")
    check("bare username normalized (live failure 7f0b1c921896)",
          ("alerts-ohmz2", "bare username") in alerts, repr(alerts))
    check("invalid chars still dropped", all(t != "alerts-UPPER" for t, _ in alerts), repr(alerts))
    check("prefixed form still works", ("alerts-ok_1", "prefixed") in alerts, repr(alerts))

    print("--- alert flood capped (injection hygiene) ---")
    body = "## Response\n" + "".join(f"ALERT(alerts-a): spam {i}\n" for i in range(10))
    _, alerts = hd.parse_output(body)
    check("at most 3 alerts per run", len(alerts) == 3, str(len(alerts)))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

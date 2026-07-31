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
    log, alerts, _d = hd.parse_output(
        "# Job\nprompt says: LOG: <summary> and ALERT(x): <msg>\n"
        "## Response\nFetched fine.\nLOG: £51.77, unchanged\nALERT(ohmz2): below £60 — £51.77\n")
    check("LOG extracted", log == "£51.77, unchanged", repr(log))
    check("ALERT extracted with recipient", alerts == [("ohmz2", "below £60 — £51.77")], repr(alerts))

    print("--- the job's own prompt must not trigger deliveries ---")
    log, alerts, _d = hd.parse_output(
        "# Job\nALERT(evil): from the prompt section\nLOG: from the prompt\n"
        "## Response\nLOG: real one\n")
    check("prompt-section ALERT ignored", alerts == [], repr(alerts))
    check("prompt-section LOG ignored", log == "real one", repr(log))

    print("--- missing LOG falls back, visibly ---")
    log, alerts, _d = hd.parse_output("## Response\nThe price is £51.77 today.\nNothing else.\n")
    check("fallback still carries the run's first line", "The price is £51.77" in log, repr(log))
    check("fallback is marked as unverified, not presented as a result",
          "DID NOT FOLLOW THE OUTPUT PROTOCOL" in log and "unverified" in log, repr(log))
    log, _, _d = hd.parse_output("## Response\n\n")
    check("empty response -> no log", log is None, repr(log))

    print("--- recipient validation, and back-compat with the legacy alerts- prefix ---")
    _, alerts, _d = hd.parse_output(
        "## Response\nALERT(ohmz2): bare handle\nALERT(UPPER): bad chars\n"
        "ALERT(alerts-ok_1): legacy prefix\n")
    check("bare handle accepted", ("ohmz2", "bare handle") in alerts, repr(alerts))
    check("invalid chars dropped", all(w != "UPPER" for w, _ in alerts), repr(alerts))
    check("legacy alerts- prefix stripped (pre-2026-07-30 jobs keep working)",
          ("ok_1", "legacy prefix") in alerts, repr(alerts))


    print("--- un-substituted protocol templates are never delivered ---")
    # Live failure 2026-07-30: a run emitted the brief's own example line verbatim. It parsed
    # perfectly and a real phone received "<what happened, with the number>   (ONLY in a run
    # where the user's alert condition holds)". Syntax cannot distinguish this from a real
    # alert — only the placeholder can.
    log, alerts, _d = hd.parse_output(
        "## Response\nLOG: checked\n"
        "ALERT(ohmz): <what happened, with the number>   (ONLY in a run where the user's "
        "alert condition holds)\n")
    check("template alert is dropped, not sent", alerts == [], repr(alerts))
    check("suppression is announced in the channel log", "suppressed 1" in (log or ""), repr(log))
    check("the real LOG survives alongside the notice", "checked" in (log or ""), repr(log))
    for bad in ("<msg>", "<summary of the run>", "ALERT text here (ONLY in a run where it holds)"):
        check(f"placeholder {bad!r} rejected", hd.is_template(bad))
    for good in ("46.99 is below your 50.00 target — https://x.com/dp/B0",
                 "CPU at 91% (was 40%)", "price dropped to $12.34"):
        check(f"real message {good[:28]!r}... delivered", not hd.is_template(good))
    # A "<" that is arithmetic, not a placeholder, must still get through.
    check("'price < 50 now' is not a template", not hd.is_template("price < 50 now, at 46.99"))

    print("--- a corrupt .delivered.json must not wedge the watcher forever ---")
    # It was written with a bare json.dump(open(...,"w")) — truncate-in-place, no tmp+rename — and
    # read with an unguarded json.load. A crash mid-write left a half-file, and every subsequent
    # tick aborted at the read BEFORE examining a single output: no LOG, no ALERT, no ledger row,
    # no notice. Dead subsystem, silent records, once per minute, forever.
    import glob as _glob, tempfile as _tf
    for broken in ('{"/x/a.md": {"log": tru', "", "not json at all"):
        d = _tf.mkdtemp()
        hd.OUT_DIR, hd.STATE = d, os.path.join(d, ".delivered.json")
        hd.ALERT_STATE = os.path.join(d, ".alerts.json")
        hd.ALERT_LEDGER = os.path.join(d, "ledger.jsonl")
        os.makedirs(os.path.join(d, "job1"))
        open(os.path.join(d, "job1", "run.md"), "w").write("## Response\nLOG: it ran\n")
        open(hd.STATE, "w").write(broken)
        posted = []
        hd.post_channel = lambda summary, job: posted.append(summary) or True
        sys.argv = ["hd"]
        rc = hd.main()
        label = repr(broken[:18])
        check(f"tick survives {label}", rc == 0, f"rc={rc}")
        check(f"...and still delivers the run {label}", posted == ["it ran"], repr(posted))
        check(f"...quarantines the bad file {label}", os.path.exists(hd.STATE + ".corrupt"))
        check(f"...leaving valid state behind {label}",
              isinstance(json.load(open(hd.STATE)), dict))

    print("--- state is written atomically, so a crash cannot half-write it ---")
    d = _tf.mkdtemp()
    hd.OUT_DIR, hd.STATE = d, os.path.join(d, ".delivered.json")
    hd.ALERT_STATE = os.path.join(d, ".alerts.json")
    hd.ALERT_LEDGER = os.path.join(d, "ledger.jsonl")
    os.makedirs(os.path.join(d, "j"))
    open(os.path.join(d, "j", "r.md"), "w").write("## Response\nLOG: x\n")
    hd.post_channel = lambda *a: True
    real_replace, seen = os.replace, []
    os.replace = lambda a, b: seen.append((a, b)) or real_replace(a, b)
    sys.argv = ["hd"]
    hd.main()
    os.replace = real_replace
    check("the state write went through a tmp file + rename",
          any(a.endswith(".tmp") and b.endswith(".delivered.json") for a, b in seen), repr(seen))

    print("--- the live schedule wins over the one baked into the job ---")
    # A job carries whatever schedule it was created with, inside its own prompt. Reschedule it and
    # the run keeps reporting the old one, so the email says "Checked every 6h" about a monitor now
    # running every 5 minutes. The scheduler is the only thing that knows.
    import tempfile as _tf2
    d = _tf2.mkdtemp()
    hd.JOBS_FILE = os.path.join(d, "jobs.json")
    json.dump([{"id": "abc123", "name": "Giant Tiger bedside table",
                "schedule_display": "every 5m"}], open(hd.JOBS_FILE, "w"))
    facts = hd.job_facts()
    check("reads the friendly name", facts["abc123"]["name"] == "Giant Tiger bedside table")
    check("reads the CURRENT schedule", facts["abc123"]["schedule"] == "every 5m")
    # A finite job deletes itself when its last run completes, and the watcher reads the scheduler
    # a minute later — so the FINAL post of every bounded monitor went out as "🤖 acdf3fbb8b6d:".
    # The last message about a task is the one most worth labelling.
    hd.NAMES_CACHE = os.path.join(d, ".job_names.json")
    hd.job_facts()                                    # populates the cache while the job exists
    json.dump([], open(hd.JOBS_FILE, "w"))            # job finishes and removes itself
    after = hd.job_facts()
    check("a finished job keeps its name for its last post",
          after.get("abc123", {}).get("name") == "Giant Tiger bedside table", after)
    check("...and its schedule", after.get("abc123", {}).get("schedule") == "every 5m", after)
    # The scheduler is still authoritative when both know: a rename must not be masked by the cache.
    json.dump([{"id": "abc123", "name": "Renamed", "schedule_display": "every 1h"}],
              open(hd.JOBS_FILE, "w"))
    check("the live scheduler wins over the cache",
          hd.job_facts()["abc123"]["name"] == "Renamed", hd.job_facts())
    hd.JOBS_FILE = os.path.join(d, "missing.json")
    hd.NAMES_CACHE = os.path.join(d, "missing_cache.json")
    check("a missing jobs file degrades to empty, never raises", hd.job_facts() == {})

    print("--- alert flood capped (injection hygiene) ---")
    body = "## Response\n" + "".join(f"ALERT(a): spam {i}\n" for i in range(10))
    _, alerts, _d = hd.parse_output(body)
    check("at most 3 alerts per run", len(alerts) == 3, str(len(alerts)))

    print("--- send_alert's real contract (a stub with the wrong shape hid a live crash) ---")
    # Checked by inspection rather than by waiting for a TypeError mid-run: when a kwarg is added
    # to the transport, every stub in this file must grow it too, and the failure should say so in
    # one line instead of surfacing as an unrelated crash three sections later. It has drifted
    # three times in one day.
    import inspect
    _real = inspect.signature(hd.send_alert).parameters
    check("send_alert's parameters are the ones the stubs mimic",
          list(_real) == ["recipient", "message", "job", "job_id", "when", "payload"],
          list(_real))
    # 2026-07-30: the retry queue did `ok, notes = send_alert(...)` while the real wrapper returned
    # a bare bool. Every test passed, because the tests replaced send_alert with a 2-tuple version —
    # the double was more correct than the code. The SMS went out and the watcher then died with
    # "cannot unpack non-iterable bool object", taking the rest of the tick with it. So exercise the
    # REAL wrapper here, stubbing only the transport beneath it.
    import types
    fake = types.ModuleType("alert_transports")
    # Signature mirrors production exactly — kwargs included. This stub going stale is precisely
    # what these checks exist to catch, and it caught itself when job/job_id/when were added.
    fake.send_alert = lambda r, m, job=None, job_id=None, when=None, payload=None: (True, ["sms sent", "email sent"])
    sys.modules["alert_transports"] = fake
    got = hd.send_alert("ohmz", "target met")
    check("returns a 2-tuple, not a bool", isinstance(got, tuple) and len(got) == 2, repr(got))
    check("unpacks the way attempt_alert calls it", got[0] is True and "sms sent" in got[1], repr(got))

    fake.send_alert = lambda r, m, job=None, job_id=None, when=None, payload=None: (
        (_ for _ in ()).throw(RuntimeError("smtp down")))
    got = hd.send_alert("ohmz", "target met")
    check("a raising transport still returns the pair", isinstance(got, tuple) and len(got) == 2, repr(got))
    check("...reporting failure", got[0] is False, repr(got))
    check("...and carrying the cause into the ledger", any("smtp down" in n for n in got[1]), repr(got))
    del sys.modules["alert_transports"]

    print("--- alert retry queue: verified sends, 3 retries at 5-min spacing, full record ---")
    import tempfile as _tf, time as _t
    with _tf.TemporaryDirectory() as td:
        hd.ALERT_STATE = os.path.join(td, "alerts.json")
        hd.ALERT_LEDGER = os.path.join(td, "ledger.jsonl")
        calls = {"n": 0}

        def failing(recipient, message, job=None, job_id=None, when=None, payload=None):
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
        hd.send_alert = lambda r, m, job=None, job_id=None, when=None, payload=None: (True, ["sms sent"])
        e2 = {"job": "j2", "recipient": "ohmz", "message": "m", "created": "now",
              "attempts": [], "status": "pending", "next_attempt": 0}
        hd.process_alert_queue({"k2": e2})
        check("a delivered alert is marked delivered", e2["status"] == "delivered", e2["status"])
        check("delivered after exactly one attempt", len(e2["attempts"]) == 1)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""The watchdog alerts on state TRANSITIONS — once down, once recovered, never per tick.

Why this file exists. The watchdog runs every 5 minutes forever. Get the dedup wrong in one
direction and a single dead unit texts the owner 288 times a day until they mute the sender —
after which the channel is worthless precisely when it matters. Get it wrong in the other
direction and a failure alerts zero times, which is the silence this watchdog exists to end.
Both wrong directions look fine in a single manual run, so they get a harness.

Fully offline: checks, transport and state file are all substituted. No systemd, no SMTP.

Usage:  python3 tests/test_watchdog.py
"""
import importlib.util
import os
import sys
import tempfile
import types

sys.path.insert(0, "/home/ohmz/ai-stack/scripts")
spec = importlib.util.spec_from_file_location("wd", "/home/ohmz/ai-stack/scripts/stack_watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


SENT = []
fake_transport = types.ModuleType("alert_transports")
fake_transport.send_alert = lambda handle, msg, **kw: (SENT.append((handle, msg)) or (True, []))
sys.modules["alert_transports"] = fake_transport

VERDICTS = {}


def stub(key):
    def _c():
        v = VERDICTS.get(key, True)
        if isinstance(v, Exception):
            raise v
        return v, f"{key} detail"
    return _c


def run(dry=False):
    SENT.clear()
    argv = sys.argv
    sys.argv = ["stack_watchdog.py"] + (["--dry-run"] if dry else [])
    try:
        wd.main()
    finally:
        sys.argv = argv
    return list(SENT)


def main():
    tmp = tempfile.mkdtemp()
    wd.STATE_FILE = os.path.join(tmp, "state.json")
    for k in ("gateway", "api", "delivery", "backup"):
        setattr(wd, f"check_{k}", stub(k))
        VERDICTS[k] = True
    wd.report_only = lambda: ["stubbed"]

    print("--- steady state is silent ---")
    check("first all-OK run sends nothing", run() == [])
    check("second all-OK run sends nothing", run() == [])

    print("--- one failure = one alert, not one per tick ---")
    VERDICTS["delivery"] = False
    sent = run()
    check("the transition to FAIL alerts exactly once", len(sent) == 1, repr(sent))
    check("...and names the failing check", sent and "delivery detail" in sent[0][1], repr(sent))
    check("still failing on the next tick sends NOTHING", run() == [])
    check("...or the next ten", all(run() == [] for _ in range(10)))

    print("--- recovery closes the loop with one message ---")
    VERDICTS["delivery"] = True
    sent = run()
    check("recovery alerts exactly once", len(sent) == 1, repr(sent))
    check("...and says recovered", sent and "recovered" in sent[0][1], repr(sent))
    check("steady state is silent again", run() == [])

    print("--- two failures in one tick share one text ---")
    VERDICTS["gateway"] = False
    VERDICTS["backup"] = False
    sent = run()
    check("one message covers both (a 3am incident is one buzz, not four)",
          len(sent) == 1 and "gateway detail" in sent[0][1] and "backup detail" in sent[0][1],
          repr(sent))
    VERDICTS["gateway"] = True
    VERDICTS["backup"] = True
    run()

    print("--- a crashing checker is a FAIL, not a crash ---")
    VERDICTS["api"] = RuntimeError("probe exploded")
    sent = run()
    check("checker exception alerts as a failure", len(sent) == 1 and "checker error" in sent[0][1],
          repr(sent))
    check("...and the watchdog itself did not raise", True)
    VERDICTS["api"] = True
    run()

    print("--- dry-run observes but never sends ---")
    VERDICTS["gateway"] = False
    check("a transition under --dry-run sends nothing", run(dry=True) == [])
    # And the dry run must not have consumed the transition: the next real run still alerts.
    sent = run()
    check("the real run after it still alerts (dry-run must not swallow the edge)",
          len(sent) == 1, repr(sent))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

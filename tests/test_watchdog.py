#!/usr/bin/env python3
"""The watchdog alerts on CONFIRMED transitions — once down, once recovered, never per tick.

Why this file exists. The watchdog runs every 5 minutes forever. Get the dedup wrong in one
direction and a single dead unit texts the owner 288 times a day until they mute the sender —
after which the channel is worthless precisely when it matters. Get it wrong in the other
direction and a failure alerts zero times, which is the silence this watchdog exists to end.
Both wrong directions look fine in a single manual run, so they get a harness.

Updated 2026-09-29 for the health_alert engine: a failure now alerts on its SECOND failed run,
not its first (approved after 2026-09-22..24 sent 20 texts-plus-emails, mostly a DOWN and a
"recovered" minutes apart for blips), so each "one alert" case below fails two runs first; and a
recovery, like the end of an unalerted streak, needs two ok runs in a row (flap damping), so each
recovery below is healthy two runs. The texts come from the engine ("Hermes gateway DOWN: ..."),
not "Stack watchdog - DOWN: ...".
tests/test_stack_watchdog_alerts.py covers the rest of the new policy (backup's confirm_after=1,
state migration, failed sends, stack_alert) and the durability checks added 2026-09-29 (ticker,
gwrestarts, backlog, hermesver, and the api probe's move to /health).

Fully offline: checks, transport and state file are all substituted. No systemd, no SMTP.

Usage:  python3 tests/test_watchdog.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile

sys.path.insert(0, "/home/ohmz/StudioProjects/ai-stack/scripts")
# The real module with its senders replaced: the engine renders through its sms_body and
# alert_subject, so the old stand-in module with only send_alert no longer loads the watchdog.
import alert_transports  # noqa: E402

SENT = []


def _never(*a, **k):
    raise AssertionError("a test reached a real transport")


alert_transports.send_sms = alert_transports.send_email = alert_transports.send_alert = _never
alert_transports.send_report = lambda handle, sms, subject, plain, **kw: (
    SENT.append((handle, sms, plain)) or (True, []))
alert_transports.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}   # never the live config
alert_transports.resolve = lambda handle, conf=None, contacts=None: ("to@test", "+15145550100")

spec = importlib.util.spec_from_file_location("wd", "/home/ohmz/StudioProjects/ai-stack/scripts/stack_watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)
_TMP = tempfile.TemporaryDirectory(prefix="wd-")
wd.ha.FALLBACK_DIR = _TMP.name        # the engine's tmpfs fallback, never the live runtime dir

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


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
    with contextlib.redirect_stdout(io.StringIO()):
        wd.main(["--dry-run"] if dry else [])
    return list(SENT)


def main():
    tmp = tempfile.mkdtemp()
    wd.STATE_FILE = os.path.join(tmp, "state.json")
    for k, fn in (("gateway", "check_gateway"), ("api", "check_api"),
                  ("delivery", "check_delivery"), ("backup", "check_backup"),
                  ("flightclaw", "check_flightclaw"), ("pubgate", "check_public_gate"),
                  ("pubquota", "check_public_quota"), ("ticker", "check_ticker"),
                  ("gwrestarts", "check_gateway_restarts"), ("backlog", "check_backlog"),
                  ("hermesver", "check_hermes_version")):
        setattr(wd, fn, stub(k))
        VERDICTS[k] = True
    wd.report_only = lambda: ["stubbed"]
    check("every alerting check is stubbed (an unstubbed one would probe the live host)",
          set(VERDICTS) == set(wd.CHECKS) == set(wd._checkers()),
          sorted(set(wd.CHECKS) ^ set(VERDICTS)))

    print("--- steady state is silent ---")
    check("first all-OK run sends nothing", run() == [])
    check("second all-OK run sends nothing", run() == [])

    print("--- one failure = one alert, not one per tick ---")
    VERDICTS["delivery"] = False
    check("the first failed run is not yet an alert (confirmation)", run() == [])
    sent = run()
    check("the confirmed failure alerts exactly once", len(sent) == 1, repr(sent))
    check("...and names the failing check",
          sent and "Delivery timer DOWN: delivery detail" in sent[0][1], repr(sent))
    check("still failing on the next tick sends NOTHING", run() == [])
    check("...or the next ten", all(run() == [] for _ in range(10)))

    print("--- recovery closes the loop with one message ---")
    VERDICTS["delivery"] = True
    check("the first healthy run is not yet a recovery (two in a row)", run() == [])
    sent = run()
    check("recovery alerts exactly once", len(sent) == 1, repr(sent))
    check("...and says recovered", sent and "RECOVERED" in sent[0][1], repr(sent))
    check("steady state is silent again", run() == [])

    print("--- a one-run blip never alerts, in either direction ---")
    VERDICTS["gateway"] = False
    check("one failed run: silent", run() == [])
    VERDICTS["gateway"] = True
    check("healthy next two runs: no 'recovered' for an outage never reported",
          run() == [] and run() == [])

    print("--- two failures in one tick share one text ---")
    VERDICTS["gateway"] = False
    VERDICTS["api"] = False
    run()
    sent = run()
    check("one message covers both (a 3am incident is one buzz, not four)",
          len(sent) == 1 and "Hermes gateway" in sent[0][1] and "Hermes API" in sent[0][1],
          repr(sent))
    VERDICTS["gateway"] = True
    VERDICTS["api"] = True
    run()
    run()

    print("--- a crashing checker is a FAIL, not a crash ---")
    VERDICTS["api"] = RuntimeError("probe exploded")
    run()
    sent = run()
    check("checker exception alerts as a failure", len(sent) == 1 and "checker error" in sent[0][2],
          repr(sent))
    check("...and the watchdog itself did not raise", True)
    VERDICTS["api"] = True
    run()
    run()

    print("--- dry-run observes but never sends ---")
    VERDICTS["gateway"] = False
    run()
    check("a confirmed failure under --dry-run sends nothing", run(dry=True) == [])
    # And the dry run must not have consumed the transition: the next real run still alerts.
    sent = run()
    check("the real run after it still alerts (dry-run must not swallow the edge)",
          len(sent) == 1, repr(sent))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

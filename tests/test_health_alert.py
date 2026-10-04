#!/usr/bin/env python3
"""health_alert: confirm-before-alert policy, rendering and delivery. Offline, no clock, no mail.

Why this file exists. The search canary sent 22 alerts (each an SMS and an email) in 8 days, for
11 outages of which 8 were one failed probe, healthy again at the next run. Every rule below
fails silently in one of two directions, and both look fine in a single manual run:

  * too eager: a blip alerts, the user mutes the sender, and the real outage is silent too;
  * too quiet: an alert is swallowed forever (a failed send recorded as delivered, a corrupt or
    old-format state file, a clock that jumped), which is the silence the monitors exist to end.

So the policy is driven with an injected `now`, the transports are stubbed at every layer, and
the canary's REAL history (tests/fixtures/search_canary_history.txt, from its journal) is
replayed to measure what confirmation buys. The replay measures cross-run confirmation alone:
in-run retries cannot be replayed from one-probe-per-run history.

Never sends anything: health_alert's transport is replaced before any test runs, and send_report
is exercised on a separately loaded alert_transports whose SMS and SMTP legs are stubs.

Usage:  python3 tests/test_health_alert.py
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time

os.environ["TZ"] = "America/Toronto"
time.tzset()

sys.path.insert(0, "/home/ohmz/StudioProjects/ai-stack/scripts")
FIXTURE = "/home/ohmz/StudioProjects/ai-stack/tests/fixtures/search_canary_history.txt"
JOB_FOOTER = "You are receiving this because a background task you scheduled"
WORDS = {"DOWN", "DEGRADED", "RECOVERED", "REMINDER", "FAILED"}

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ha = load("/home/ohmz/StudioProjects/ai-stack/scripts/health_alert.py", "ha")


def _never(*a, **k):
    raise AssertionError("a test reached a real transport")


# Belt and braces: whatever path a test takes, nothing leaves this machine, and the channel
# lookup never reads the live transport config.
ha._at.send_sms = _never
ha._at.send_email = _never
ha._at.send_report = _never
ha._at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}
ha._at.resolve = lambda handle, conf=None, contacts=None: ("to@test", "+15145550100")
# save_state's tmpfs fallback must never land in the live runtime directory.
_FALLBACK = tempfile.TemporaryDirectory(prefix="ha-fallback-")
ha.FALLBACK_DIR = _FALLBACK.name

T = 1790709468.0          # 2026-09-29 15:17:48 EDT
RUN = 1800                # the canary's 30-minute timer


def C(status, key="search_chat", label="Web search (chat)", **kw):
    kw.setdefault("detail", f"{status} detail")
    return ha.Check(key=key, label=label, status=status, **kw)


class Sender:
    """send_report stand-in: records calls, returns a scripted verdict."""
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, handle, sms, subject, plain, html=None, channels=None):
        self.calls.append(dict(handle=handle, sms=sms, subject=subject, plain=plain, html=html,
                               channels=channels))
        return self.ok, ["stub"]


def step(state, status_or_results, t, confirm_after=2, remind_after_s=86400, deliver=True,
         **kw):
    res = status_or_results if isinstance(status_or_results, list) else [C(status_or_results)]
    evs = ha.evaluate(state, res, t, confirm_after, remind_after_s, **kw)
    if evs and deliver:
        ha.mark_delivered(state, evs, t)
    return evs


def kinds(evs):
    return [e.kind for e in evs]


def read_fixture():
    rows = []
    for line in open(FIXTURE):
        if line.startswith("#") or not line.strip():
            continue
        ts, status, detail = line.rstrip("\n").split("\t", 2)
        rows.append((datetime.datetime.fromisoformat(ts).timestamp(), status.lower(), detail))
    return rows


def main():
    print("--- confirm_after: one failed run is not an alert ---")
    st = ha.fresh_state()
    check("run 1 down: silent", step(st, "down", T) == [])
    evs = step(st, "down", T + RUN)
    check("run 2 down: one DOWN", kinds(evs) == ["down"], kinds(evs))
    check("run 3 down: silent (already alerted)", step(st, "down", T + 2 * RUN) == [])
    st = ha.fresh_state()
    got = [kinds(step(st, "down", T + i * RUN, confirm_after=3)) for i in range(3)]
    check("confirm_after=3 alerts on the third run", got == [[], [], ["down"]], got)
    st = ha.fresh_state()
    check("confirm_after=1 alerts at once", kinds(step(st, "down", T, confirm_after=1)) == ["down"])
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "ok", T + RUN)
    evs = step(st, "down", T + 2 * RUN)
    check("ONE ok run does not break a streak: down,ok,down confirms on the second down",
          kinds(evs) == ["down"], kinds(evs))
    check("...and the footer counts the two failing runs",
          "Sent after 2 failed probes over 1 hour." in ha.build(evs, host="h").plain)
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "ok", T + RUN)
    step(st, "ok", T + 2 * RUN)
    check("two ok runs in a row do: the streak starts over", step(st, "down", T + 3 * RUN) == [])

    print("--- recovery: only for an outage the user was told about, after two ok runs ---")
    st = ha.fresh_state()
    step(st, "down", T)
    evs = step(st, "ok", T + RUN) + step(st, "ok", T + 2 * RUN)
    e = st["checks"]["search_chat"]
    check("never alerted -> recovery is silent", evs == [], kinds(evs))
    check("...and the entry is reset", e["fail_streak"] == 0 and e["first_bad_at"] is None
          and e["status"] == "ok" and not e["alerted"], e)
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)
    evs = step(st, "ok", T + 11520, deliver=False)
    check("alerted, one ok run: not yet recovered (flap damping)", evs == [], kinds(evs))
    evs = step(st, "ok", T + 11520 + RUN, deliver=False)
    check("alerted, second ok run -> RECOVERED", kinds(evs) == ["recovered"], kinds(evs))
    ev = evs[0]
    check("...with the outage measured from the first failure to the FIRST ok run",
          ev.recovered_at - ev.first_bad_at == 11520, ev.recovered_at - ev.first_bad_at)
    n = ha.build(evs, host="h")
    check("...and said in the SMS", "RECOVERED after 3h12m" in n.sms, n.sms)
    check("...and in the email", "3 hours 12 minutes" in n.plain, n.plain)
    evs2 = step(st, "ok", T + 11520 + 2 * RUN, deliver=False)
    check("an undelivered recovery is owed again next run", kinds(evs2) == ["recovered"])
    check("...with the ORIGINAL recovery time", evs2[0].recovered_at == T + 11520)
    ha.mark_delivered(st, evs2)
    check("delivered -> entry reset", not st["checks"]["search_chat"]["alerted"])
    check("next ok run: silent", step(st, "ok", T + 11520 + 3 * RUN) == [])
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)
    step(st, "ok", T + 2 * RUN)
    evs = step(st, "down", T + 3 * RUN)
    check("a relapse after one ok run continues the outage: no RECOVERED, no second DOWN",
          evs == [] and st["checks"]["search_chat"]["alerted"], kinds(evs))
    evs = step(st, "ok", T + 4 * RUN, recover_after=1)
    check("recover_after=1 recovers on the first ok run", kinds(evs) == ["recovered"])

    print("--- flap damping and intermittent outages (the 2026-09-24 gateway shape) ---")
    W = 300
    st, n = ha.fresh_state(), []
    for i in range(144):                       # 12 h of down,down,ok on the 5-minute timer
        n += step(st, "down" if i % 3 != 2 else "ok", T + i * W)
    check("down,down,ok for 12 hours: ONE notification, not a DOWN/RECOVERED pair every 15 min",
          kinds(n) == ["down"], kinds(n))
    st, n = ha.fresh_state(), []
    for i in range(48):                        # 24 h of down,ok on the 30-minute timer
        n += step(st, "down" if i % 2 == 0 else "ok", T + i * RUN)
    check("failing every other run for 24 hours alerts (it used to never alert)",
          kinds(n) == ["down"], kinds(n))

    print("--- a streak does not survive a gap or a reboot ---")
    st = ha.fresh_state()
    step(st, "down", T, max_gap_s=5400)
    evs = step(st, "down", T + 3 * 86400, max_gap_s=5400)
    check("fail, 3 days of nothing, fail: no DOWN (it used to say 'down for 72 hours')",
          evs == [] and st["checks"]["search_chat"]["fail_streak"] == 1, kinds(evs))
    check("...the new streak starts at the second failure",
          st["checks"]["search_chat"]["first_bad_at"] == T + 3 * 86400)
    check("...and the next failure within the gap confirms",
          kinds(step(st, "down", T + 3 * 86400 + RUN, max_gap_s=5400)) == ["down"])
    st = ha.fresh_state()
    step(st, "down", T)
    killed, note = ha.start_run(st, T + 900, max_gap_s=5400, boot_id="boot-B")
    check("start_run on the first run records the boot, restarts nothing",
          killed == 0 and note == "" and st["run"]["boot_id"] == "boot-B", (killed, note))
    st["run"]["inflight_at"] = None
    ha.start_run(st, T + 1800, max_gap_s=5400, boot_id="boot-C")
    check("a reboot restarts an unalerted streak (failure before shutdown + one after boot)",
          st["checks"]["search_chat"]["fail_streak"] == 0, st["checks"]["search_chat"])
    check("...and a run cut short by the reboot is not counted as killed",
          st["run"]["killed_streak"] == 0, st["run"])
    evs = step(st, "down", T + 1800)
    check("...so the first failure after boot is run 1 of 2", evs == [])
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)
    ha.start_run(st, T + 5 * 86400, max_gap_s=5400, boot_id="x")
    check("an ALERTED check keeps its alert across a gap (its recovery is still owed)",
          st["checks"]["search_chat"]["alerted"] and st["checks"]["search_chat"]["fail_streak"])

    print("--- killed runs count as runs, and two in a row alert ---")
    st = ha.fresh_state()
    step(st, "down", T)
    for i in (1, 2, 3):                        # three runs killed at TimeoutStartSec, 5 min apart
        k, _ = ha.start_run(st, T + i * W, max_gap_s=900, boot_id="b")
    check("each killed run is counted by the next start", k == 2, k)
    evs = step(st, "down", T + 3 * W + 30, max_gap_s=900)
    check("killed runs are not a gap: the next finished failure confirms",
          kinds(evs) == ["down"], kinds(evs))
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "wd.json")
        snd = Sender()
        kw = dict(label="Watchdog runs", unit="stack-watchdog.service", timeout_s=120,
                  max_gap_s=3600, boot_id="b", sender=snd, log=None)
        r = ha.begin("watchdog", sp, now=T, **kw)
        check("begin() saves the in-flight marker before probing",
              r.saved and json.load(open(sp))["run"]["inflight_at"] == T)
        ha.begin("watchdog", sp, now=T + W, **kw)                 # previous run killed: 1
        check("one killed run: no alert", snd.calls == [])
        r = ha.begin("watchdog", sp, now=T + 2 * W, **kw)         # 2 in a row
        check("two killed runs in a row: ONE alert, text and email, before any probing",
              len(snd.calls) == 1 and snd.calls[0]["channels"] == ["sms", "email"],
              [c["sms"] for c in snd.calls])
        check("...that says what is happening",
              snd.calls[0]["sms"] == "Watchdog runs DOWN: 2 runs in a row killed by systemd",
              snd.calls[0]["sms"])
        check("...and names the time limit in the email", "TimeoutStartSec=120" in
              snd.calls[0]["plain"])
        ha.begin("watchdog", sp, now=T + 3 * W, **kw)
        check("a third killed run: silent (already alerted)", len(snd.calls) == 1)
        ha.run("watchdog", [C("ok", key="api")], sp, now=T + 3 * W + 20, sender=snd, log=None,
               unit="stack-watchdog.service")
        st = json.load(open(sp))
        check("the run that finishes clears the marker and the count, and does not recover yet",
              st["run"]["inflight_at"] is None and st["run"]["killed_streak"] == 0
              and len(snd.calls) == 1, st["run"])
        ha.begin("watchdog", sp, now=T + 4 * W, **kw)
        ha.run("watchdog", [C("ok", key="api")], sp, now=T + 4 * W + 20, sender=snd, log=None)
        check("the second finished run in a row sends the recovery",
              len(snd.calls) == 2 and "Watchdog runs RECOVERED" in snd.calls[1]["sms"],
              [c["sms"] for c in snd.calls])
        for i in (5, 6):
            ha.begin("watchdog", sp, now=T + i * W, **kw)
            ha.run("watchdog", [C("ok", key="api")], sp, now=T + i * W + 20, sender=snd, log=None)
        check("normal runs after it: silent", len(snd.calls) == 2)
        for i in (7, 8, 9, 10, 11, 12):                # killed, finished, killed, finished, ...
            ha.begin("watchdog", sp, now=T + i * W, **kw)
            if i % 2 == 0:
                ha.run("watchdog", [C("ok", key="api")], sp, now=T + i * W + 20, sender=snd,
                       log=None)
        check("one run in two killed: a single DOWN, not a DOWN/RECOVERED pair per cycle",
              [c["sms"].split(":")[0] for c in snd.calls[2:]] == ["Watchdog runs DOWN"],
              [c["sms"] for c in snd.calls[2:]])

    print("--- reminders: once per window while still broken ---")
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)                                  # alerted at T+RUN
    a = T + RUN
    check("just under 24h: silent", step(st, "down", a + 86399) == [])
    check("24h after the alert: REMINDER", kinds(step(st, "down", a + 86400)) == ["reminder"])
    check("an hour later: silent", step(st, "down", a + 86400 + 3600) == [])
    check("24h after the reminder: REMINDER again",
          kinds(step(st, "down", a + 2 * 86400)) == ["reminder"])
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)
    step(st, "down", a + 86400, deliver=False)
    check("an undelivered reminder is retried next run",
          kinds(step(st, "down", a + 86400 + RUN)) == ["reminder"])
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)
    check("remind_after_s is honoured",
          kinds(step(st, "down", a + 3600, remind_after_s=3600)) == ["reminder"])

    print("--- escalation: degraded -> down re-alerts, down -> degraded does not ---")
    st = ha.fresh_state()
    step(st, "degraded", T)
    evs = step(st, "degraded", T + RUN)
    check("degraded x2 -> DEGRADED", kinds(evs) == ["degraded"], kinds(evs))
    check("...by email only", ha.channels_for(evs) == ["email"], ha.channels_for(evs))
    evs = step(st, "down", T + 2 * RUN)
    check("then down -> DOWN (escalated)", kinds(evs) == ["down"] and evs[0].escalated, kinds(evs))
    check("...by text and email", ha.channels_for(evs) == ["sms", "email"])
    check("...and says it was degraded", "was degraded" in ha.build(evs, host="h").sms)
    check("down again: silent", step(st, "down", T + 3 * RUN) == [])
    check("easing to degraded: silent", step(st, "degraded", T + 4 * RUN) == [])
    check("the escalation is remembered as a DOWN alert",
          st["checks"]["search_chat"]["alerted_status"] == "down")
    step(st, "ok", T + 5 * RUN)
    evs = step(st, "ok", T + 6 * RUN)
    check("recovery after an escalation texts", ha.channels_for(evs) == ["sms", "email"])
    foot = ha.build(evs, host="h").plain
    check("...and its footer dates the DOWN alert by the escalation, not the degraded alert",
          f"This closes the down alert sent {ha._when(T + 2 * RUN)} (first alerted as degraded "
          f"{ha._when(T + RUN)})." in foot, foot)
    st = ha.fresh_state()
    step(st, "degraded", T)
    evs = step(st, "down", T + RUN)
    check("unconfirmed degraded then down: alerts as DOWN, not escalated",
          kinds(evs) == ["down"] and not evs[0].escalated)

    print("--- severity routing ---")
    def ev(kind, status, was=None):
        return ha.Event(kind=kind, check=C(status), at=T, was=was)
    for label, evs, want in [
        ("DOWN alert", [ev("down", "down")], ["sms", "email"]),
        ("DEGRADED alert", [ev("degraded", "degraded")], ["email"]),
        ("recovered from DOWN", [ev("recovered", "ok", "down")], ["sms", "email"]),
        ("recovered from DEGRADED", [ev("recovered", "ok", "degraded")], ["email"]),
        ("reminder of DOWN", [ev("reminder", "down", "down")], ["sms", "email"]),
        ("reminder, now degraded", [ev("reminder", "degraded", "down")], ["email"]),
        ("unit failure", [ev("failed", "down")], ["sms", "email"]),
        ("degraded + recovered-from-down", [ev("degraded", "degraded"),
                                            ev("recovered", "ok", "down")], ["sms", "email"]),
    ]:
        got = ha.channels_for(evs)
        check(f"{label} -> {'+'.join(want)}", got == want, got)
        check(f"...build() carries it", ha.build(evs, host="h").channels == want)
    uf = ha.render_unit_failure("x.service", "h", ["boom"], now=T)
    check("render_unit_failure -> sms+email", uf.channels == ["sms", "email"], uf.channels)

    print("--- grouping: one run, one notification ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "wd.json")
        snd = Sender()
        wd = lambda bad: [C("down" if k in bad else "ok", key=k, label=k.upper())
                          for k in ("gateway", "api", "backup", "delivery")]
        r1 = ha.run("watchdog", wd({"gateway", "api", "backup"}), sp, now=T, sender=snd, log=None)
        check("first failing run: no send", snd.calls == [] and r1.events == [])
        r2 = ha.run("watchdog", wd({"gateway", "api", "backup"}), sp, now=T + 300, sender=snd,
                    log=None)
        check("three checks confirm in one run -> ONE send", len(snd.calls) == 1, len(snd.calls))
        check("...carrying three events", kinds(r2.events) == ["down"] * 3, kinds(r2.events))
        check("...titled '3 checks'", "3 checks" in snd.calls[0]["plain"])
        check("...one SMS naming them", "GATEWAY" in snd.calls[0]["sms"]
              and "API" in snd.calls[0]["sms"], snd.calls[0]["sms"])
        ha.run("watchdog", wd({"gateway"}), sp, now=T + 600, sender=snd, log=None)
        r3 = ha.run("watchdog", wd({"gateway"}), sp, now=T + 900, sender=snd, log=None)
        check("two recover in the same run -> one more send", len(snd.calls) == 2
              and kinds(r3.events) == ["recovered", "recovered"], kinds(r3.events))
        html = snd.calls[0]["html"]
        check("grouped email lists every check (passing ones too)",
              all(k.upper() in html for k in ("gateway", "api", "backup", "delivery")))

    print("--- delivery failure never becomes silence ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        bad, good = Sender(ok=False), Sender(ok=True)
        ha.run("canary", [C("down")], sp, now=T, sender=bad, log=None)
        ha.run("canary", [C("down")], sp, now=T + RUN, sender=bad, log=None)
        check("failed send: attempted", len(bad.calls) == 1)
        check("...and NOT recorded as alerted", not json.load(open(sp))["checks"]["search_chat"]["alerted"])
        r = ha.run("canary", [C("down")], sp, now=T + 2 * RUN, sender=good, log=None)
        check("next run tries again", len(good.calls) == 1 and kinds(r.events) == ["down"])
        check("...and the footer counts every failed check",
              "Sent after 3 failed probes over 1 hour" in good.calls[0]["plain"],
              good.calls[0]["plain"])

    print("--- dry run neither sends nor consumes the edge ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        snd = Sender()
        ha.run("canary", [C("down")], sp, now=T, sender=snd, log=None)
        before = open(sp).read()
        r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None, dry_run=True)
        check("dry run renders the notification", r.notification is not None)
        check("...sends nothing", snd.calls == [])
        check("...writes nothing", open(sp).read() == before)
        r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None)
        check("the real run still owes the alert", len(snd.calls) == 1)

    print("--- corrupt, missing and odd state never crash and never gag ---")
    with tempfile.TemporaryDirectory() as td:
        for name, content in [("missing", None), ("not json", "{not json"), ("a list", "[1, 2]"),
                              ("null", "null"), ("empty", ""),
                              ("junk entry", json.dumps({"version": 2, "checks": {"search_chat": "x"}})),
                              ("junk fields", json.dumps({"version": 2, "checks": {"search_chat": {
                                  "status": 7, "fail_streak": "lots", "first_bad_at": "yesterday",
                                  "alerted": "yes", "alerted_at": [1]}}}))]:
            sp = os.path.join(td, name.replace(" ", "_") + ".json")
            if content is not None:
                open(sp, "w").write(content)
            snd = Sender()
            try:
                ha.run("canary", [C("down")], sp, now=T, sender=snd, log=None)
                r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None)
                check(f"{name}: a genuine outage still alerts", kinds(r.events) == ["down"],
                      kinds(r.events))
            except Exception as e:
                check(f"{name}: no crash", False, repr(e))
        st = ha.migrate({"version": 2, "checks": {"search_chat": {
            "status": "down", "fail_streak": 5, "alerted": True, "alerted_status": "down",
            "alerted_at": T + 10 * 86400, "first_bad_at": T}}})
        evs = step(st, "down", T + 86400)
        check("an alert stamped in the future cannot park reminders forever",
              kinds(step(st, "down", T + 2 * 86400)) == ["reminder"], kinds(evs))
        st = ha.migrate({"version": 2, "checks": {"search_chat": {"status": "down",
                                                                   "alerted": True}}})
        step(st, "down", T)
        check("alerted with no timestamp: reminder a window later",
              kinds(step(st, "down", T + 86400)) == ["reminder"])
        sp = os.path.join(td, "atomic.json")
        ok, _ = ha.save_state(sp, ha.fresh_state("x"))
        check("save_state writes and leaves no tmp behind",
              ok and json.load(open(sp))["source"] == "x"
              and not [f for f in os.listdir(td) if f.endswith(".tmp")])

    print("--- a state file that cannot be written: tmpfs fallback, then held sends ---")
    with tempfile.TemporaryDirectory() as td:
        ro = os.path.join(td, "ro")
        os.makedirs(ro)
        sp = os.path.join(ro, "s.json")
        snd = Sender()
        ha.run("canary", [C("down")], sp, now=T, sender=snd, log=None)
        os.chmod(ro, 0o500)                           # a stand-in for ENOSPC / a read-only remount
        try:
            fb = ha.fallback_path(sp)
            check("the fallback lives in FALLBACK_DIR, named after the full path",
                  os.path.dirname(fb) == ha.FALLBACK_DIR and fb.endswith(".s.json"), fb)
            r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None)
            check("state dir unwritable: the second DOWN run still confirms and sends",
                  kinds(r.events) == ["down"] and len(snd.calls) == 1 and r.saved, kinds(r.events))
            check("...and its bookkeeping went to the fallback", os.path.exists(fb)
                  and json.load(open(fb))["checks"]["search_chat"]["alerted"])
            r = ha.run("canary", [C("down")], sp, now=T + 2 * RUN, sender=snd, log=None)
            check("...which the next run reads (newer saved_at): no repeat DOWN",
                  r.events == [] and len(snd.calls) == 1, kinds(r.events))
        finally:
            os.chmod(ro, 0o755)
        r = ha.run("canary", [C("down")], sp, now=T + 3 * RUN, sender=snd, log=None)
        check("writable again: saved to the file, the stale fallback removed",
              r.saved and json.load(open(sp))["checks"]["search_chat"]["alerted"]
              and not os.path.exists(fb))
        ha.run("canary", [C("ok")], sp, now=T + 4 * RUN, sender=snd, log=None)   # 1 of 2 ok
        real_fb = ha.FALLBACK_DIR
        ha.FALLBACK_DIR = "/proc/no/such/dir"
        os.chmod(ro, 0o500)
        try:
            ok, err = ha.save_state(sp, ha.fresh_state())
            check("both writes failing: an error, not an exception", not ok and "fallback" in err,
                  err)
            snd = Sender()
            evs_run = [ha.run("canary", [C("ok")], sp, now=T + (5 + i) * RUN, sender=snd, log=None)
                       for i in range(3)]
            check("...an owed RECOVERED (its clearing unsaveable) is held back, not texted every run",
                  all(kinds(r.events) == ["recovered"] for r in evs_run) and snd.calls == []
                  and any("held back" in n for n in evs_run[1].notes), evs_run[1].notes)
            check("...and every such run reports saved=False", all(r.saved is False for r in evs_run))
            wd_sp = os.path.join(ro, "wd.json")
            bk = [ha.run("watchdog", [C("down", key="backup", label="Backup freshness")], wd_sp,
                         now=T + i * 300, sender=snd, confirm_after=1, log=None) for i in range(3)]
            check("...a confirm_after=1 DOWN is held too (it would repeat every run)",
                  snd.calls == [] and all(kinds(r.events) == ["down"] for r in bk), len(snd.calls))
        finally:
            os.chmod(ro, 0o755)
            ha.FALLBACK_DIR = real_fb

    print("--- overlapping runs: one lock ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        with ha.exclusive(sp) as first:
            with ha.exclusive(sp) as second:
                check("a second run cannot take the lock while the first holds it",
                      first is True and second is False, (first, second))
        with ha.exclusive(sp) as again:
            check("...and it is free once the first is done", again is True)
        with ha.exclusive(os.path.join(td, "missing", "s.json")) as none:
            check("no lock file possible: proceed unlocked (None), never skip monitoring",
                  none is None)

    print("--- a clock stepped back cannot rewrite stored times ---")
    st = ha.fresh_state()
    step(st, "down", T)
    step(st, "down", T + RUN)                                   # DOWN alerted at T+RUN
    evs = step(st, "down", T - 270 * 86400)                     # one run with a bad RTC
    e = st["checks"]["search_chat"]
    check("the bad-clock run: no event, and alerted_at is untouched",
          evs == [] and e["alerted_at"] == T + RUN, (kinds(evs), e["alerted_at"]))
    evs = step(st, "down", T + 3600)
    check("the next correct run: no 'still DOWN after 270d' reminder", evs == [], kinds(evs))
    st = ha.fresh_state()
    step(st, "down", T - 86400)
    step(st, "down", T - 86400 + RUN, deliver=False)
    evs = step(st, "down", T - 86400 * 400)
    check("a confirmation during a bad-clock run is stamped at the last trusted time",
          kinds(evs) == ["down"] and evs[0].at == T - 86400 + RUN, [e.at for e in evs])
    st = ha.fresh_state()
    step(st, "down", T + 5 * 3600)                              # a run whose clock was 5 h fast
    for i in range(3):
        step(st, "down", T + i * RUN)
    check("three runs in a row agreeing: the stored stamp was ahead, the new clock is adopted",
          st["run"]["last_run_at"] == T + 2 * RUN
          and st["checks"]["search_chat"]["first_bad_at"] <= T + 2 * RUN, st["run"])

    print("--- a newline in a probe detail never reaches a header ---")
    nl = ha.Event(kind="down", at=T, first_bad_at=T - 300, fail_streak=2, failed_probes=2,
                  check=C("down", key="api", label="Hermes API",
                          detail="no HTTP answer on :8642 (NOTHTTP banner\n)"))
    n = ha.build([nl], host="h")
    check("subject has no CR or LF (the 2026-09-29 BadStatusLine repro)",
          "\n" not in n.subject and "\r" not in n.subject, repr(n.subject))
    check("...nor the SMS", "\n" not in n.sms and "\r" not in n.sms, repr(n.sms))

    print("--- old state formats migrate, and recovery still closes the loop ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "canary.json")
        json.dump({"status": "down", "detail": "SearXNG unreachable: URLError", "at": T}, open(sp, "w"))
        st = ha.load_state(sp, legacy_key="search_chat")
        e = st["checks"]["search_chat"]
        check("old canary DOWN -> already alerted", e["alerted"] and e["alerted_status"] == "down"
              and e["migrated"], e)
        snd = Sender()
        ha.run("canary", [C("ok")], sp, now=T + RUN, sender=snd, log=None)
        r = ha.run("canary", [C("ok")], sp, now=T + 2 * RUN, sender=snd, log=None)
        check("...its recovery is announced (no legacy_key: the only check adopts it)",
              kinds(r.events) == ["recovered"], kinds(r.events))
        check("...and the email is honest that the start is a bound",
              "at least" in snd.calls[0]["plain"], snd.calls[0]["plain"])
        json.dump({"status": "down", "detail": "x", "at": T}, open(sp, "w"))
        r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None)
        check("old canary DOWN, still down: no duplicate DOWN", r.events == [])
        json.dump({"status": "ok", "detail": "10 results", "at": T}, open(sp, "w"))
        st = ha.load_state(sp, legacy_key="search_chat")
        check("old canary OK -> fresh ok entry", not st["checks"]["search_chat"]["alerted"])
        json.dump({"status": "degraded", "detail": "x", "at": T}, open(sp, "w"))
        st = ha.load_state(sp, legacy_key="search_chat")
        check("old canary DEGRADED keeps its status",
              st["checks"]["search_chat"]["alerted_status"] == "degraded")
        json.dump({"status": "down", "detail": "x", "at": T}, open(sp, "w"))
        r = ha.run("canary", [C("ok", key="a"), C("ok", key="b")], sp, now=T + RUN,
                   sender=Sender(), log=None)
        check("old canary shape with several checks and no legacy_key: no crash", r.events == [])

        wp = os.path.join(td, "watchdog.json")
        json.dump({"gateway": {"ok": False, "detail": "hermes-gateway service", "at": T},
                   "api": {"ok": True, "detail": "hermes API", "at": T}}, open(wp, "w"))
        st = ha.load_state(wp)
        check("old watchdog FAIL -> alerted DOWN",
              st["checks"]["gateway"]["alerted"] and st["checks"]["gateway"]["alerted_status"] == "down")
        check("old watchdog OK -> fresh", not st["checks"]["api"]["alerted"])
        snd = Sender()
        wd_ok = [C("ok", key="gateway", label="Hermes gateway"),
                 C("ok", key="api", label="Hermes API")]
        ha.run("watchdog", wd_ok, wp, now=T + 300, sender=snd, log=None)
        r = ha.run("watchdog", wd_ok, wp, now=T + 600, sender=snd, log=None)
        check("old watchdog FAIL recovering -> RECOVERED (by text: it was a DOWN)",
              kinds(r.events) == ["recovered"] and snd.calls[0]["channels"] == ["sms", "email"],
              kinds(r.events))
        check("state is rewritten in the new shape", json.load(open(wp)).get("version") == 2)

    print("--- retry: in-run confirmation ---")
    def probe(seq):
        it = iter(seq)

        def fn():
            v = next(it)
            if isinstance(v, Exception):
                raise v
            return C(v)
        return fn
    slept = []
    r = ha.retry(probe(["down", "down", "ok"]), sleep=slept.append)
    check("first ok wins", r.status == "ok" and r.attempts == 3, (r.status, r.attempts))
    check("...sleeping delay_s between tries only", slept == [60, 60], slept)
    slept.clear()
    r = ha.retry(probe(["down", "degraded", "down"]), sleep=slept.append)
    check("all failed -> attempts annotated, two pauses",
          r.status == "down" and r.attempts == 3 and len(slept) == 2, (r.status, r.attempts))
    r = ha.retry(probe(["down", "down", "degraded"]), sleep=lambda s: None)
    check("all failed -> the WORST failure, not the last (down,down,degraded is down, so it texts)",
          r.status == "down" and ha.channels_for([ha.Event("down", r, T)]) == ["sms", "email"],
          r.status)
    seq = iter([C("degraded", detail="first"), C("degraded", detail="second")])
    r = ha.retry(lambda: next(seq), attempts=2, sleep=lambda s: None)
    check("...the latest among equally bad tries, for the freshest detail", r.detail == "second")
    r = ha.retry(probe(["ok"]), sleep=_never)
    check("ok first time: no sleep, attempts=1", r.attempts == 1)
    r = ha.retry(probe([RuntimeError("x"), "ok"]), attempts=2, delay_s=5, sleep=slept.append)
    check("a raising try is a failed try", r.status == "ok" and r.attempts == 2)
    r = ha.retry(probe(["down", RuntimeError("x")]), attempts=2, sleep=lambda s: None)
    check("last try raising returns the earlier failure", r.status == "down" and r.attempts == 2)
    try:
        ha.retry(probe([RuntimeError("a"), RuntimeError("b")]), attempts=2, sleep=lambda s: None)
        check("every try raising propagates", False)
    except RuntimeError as e:
        check("every try raising propagates", str(e) == "b")
    check("an unknown status reads as down", C("weird").status == "down")

    print("--- the SMS: one ASCII segment, no web address ---")
    url_re = ha._at.URL_RE
    samples = ha.samples()
    long_bad = ha.Event(kind="down", at=T, check=C(
        "down", label="Web search (chat) — primary é",
        detail="see https://searx.example.com/stats and www.example.com — " + "x" * 300))
    cases = [(k, n.sms) for k, n in samples.items()] + [
        ("long + url + unicode", ha.build([long_bad], host="h").sms),
        ("unit with a URL in its last log line",
         ha.render_unit_failure("x.service", "h", ["ok", "fetch https://api.example.com/v1 failed"],
                                now=T).sms)]
    for name, sms in cases:
        check(f"{name}: <=140 ASCII, no URL",
              len(sms) <= 140 and sms.isascii() and not url_re.search(sms), f"{len(sms)} {sms!r}")
    uf = samples["unit_failed"]
    check("unit SMS carries the last journal line only",
          "status=78/CONFIG" in uf.sms and "Starting" not in uf.sms, uf.sms)
    check("down subject: monitor and measurement in the first 45 chars",
          samples["down"].subject.index("0 results") < 45, samples["down"].subject)
    check("subjects keep the [stack] prefix mail filters match",
          all(n.subject.startswith("[stack] ") for n in samples.values()))

    print("--- the email: the backup template, a truthful footer ---")
    for name, n in samples.items():
        h = n.html or ""
        check(f"{name}: HTML rendered", bool(h) and not n.html_error, n.html_error)
        check(f"{name}: masthead 'Ohmz Stack'", "&#937;" in h and ">Stack</span>" in h)
        check(f"{name}: footnote under the panel", "automated health check" in h)
        check(f"{name}: NOT the job-alert footer", JOB_FOOTER not in h and JOB_FOOTER not in n.plain)
        check(f"{name}: plain part is a full record", len(n.plain) > 200
              and n.plain.splitlines()[0].split(":")[0] in WORDS and "automated health check" in n.plain,
              n.plain[:80])
    oe = load("/home/ohmz/StudioProjects/ai-stack/scripts/ohmz_email.py", "oe_t")
    pills = {"down": ("Down", oe.RED), "degraded": ("Degraded", oe.AMBER_BRIGHT),
             "recovered": ("Recovered", oe.GREEN), "reminder": ("Reminder", oe.RED),
             "unit_failed": ("Failed", oe.RED), "watchdog_multi": ("Down", oe.RED)}
    for name, (word, col) in pills.items():
        h = samples[name].html
        check(f"{name}: pill '{word}' in {col}", f"background:{col};border-radius:999px" in h
              and f">{word}</td>" in h)
    d = samples["down"].html
    for sec in ("What&#x27;s wrong", "Timeline", "Checks"):
        check(f"down email has section {sec.replace('&#x27;', chr(39))!r}", sec in d)
    check("down footer: how many probes over how long",
          "Sent after 6 failed probes across 2 runs over 32 minutes." in samples["down"].plain,
          samples["down"].plain)
    check("down footer: the recovery promise names the right channels",
          "one more text and email when it recovers" in samples["down"].plain)
    check("degraded footer promises email only",
          "one more email when it recovers" in samples["degraded"].plain)
    check("recovered footer gives the outage length",
          "The outage lasted 3 hours 12 minutes" in samples["recovered"].plain,
          samples["recovered"].plain)
    check("the caller's inspect hint is in the footer",
          "Inspect: journalctl --user -u search-canary" in samples["down"].html)
    check("per-engine items are listed", "duckduckgo" in d and "&#10007;" in d)
    check("unit email carries the whole journal excerpt",
          all(s in samples["unit_failed"].html for s in ("Starting hermes-gateway", "exit 78")))
    st = ha.fresh_state()
    step(st, "degraded", T)
    step(st, "degraded", T + RUN)
    rem = step(st, "degraded", T + RUN + 86400, deliver=False)
    h = ha.build(rem, host="h").html
    check("reminder of a degraded check: amber pill",
          f"background:{oe.AMBER_BRIGHT};border-radius:999px" in h and ">Reminder</td>" in h)

    print("--- HTML failure degrades to plain text, never loses the alert ---")
    import ohmz_email
    real_shell = ohmz_email.shell
    ohmz_email.shell = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("template broke"))
    try:
        with tempfile.TemporaryDirectory() as td:
            sp = os.path.join(td, "s.json")
            snd = Sender()
            ha.run("canary", [C("down")], sp, now=T, sender=snd, log=None)
            r = ha.run("canary", [C("down")], sp, now=T + RUN, sender=snd, log=None)
            check("the alert still goes out", len(snd.calls) == 1 and r.sent)
            check("...as plain text (html=None)", snd.calls[0]["html"] is None)
            check("...with the reason recorded", "template broke" in r.notification.html_error)
            check("...and the plain body intact", "down detail" in snd.calls[0]["plain"])
    finally:
        ohmz_email.shell = real_shell

    print("--- grouped tiles: the clock and the count are honest ---")
    st = ha.fresh_state()
    X = lambda s: C(s, key="x", label="Monitor search", detail="3 engines out")
    Y = lambda s: C(s, key="y", label="Web search (chat)", detail="0 results")
    step(st, [X("degraded"), Y("ok")], T)
    step(st, [X("degraded"), Y("ok")], T + RUN)
    step(st, [X("degraded"), Y("down")], T + 86400)
    evs = step(st, [X("degraded"), Y("down")], T + RUN + 86400, deliver=False)
    tiles = ha._tiles(evs, [X("degraded"), Y("down")])
    check("a degraded reminder beside a fresh DOWN: the clock is the DOWN's, not the day-old one",
          sorted(kinds(evs)) == ["down", "reminder"] and tiles[0] == ("30m", "down for"), tiles)
    res7 = [C("down" if k in ("gateway", "api", "flightclaw") else "ok", key=k, label=k)
            for k in ("gateway", "api", "delivery", "backup", "flightclaw", "pubgate", "pubquota")]
    evs = [ha.Event("down", r, T, first_bad_at=T - 600) for r in res7 if r.key != "gateway"
           and r.status == "down"]
    tiles = dict((t[1], t[0]) for t in ha._tiles(evs, res7))
    check("watchdog, gateway alerted earlier and still down: 3 failing, agreeing with 4/7 passing",
          tiles.get("failing") == "3" and tiles.get("passing") == "4/7", tiles)
    one = [ha.Event("down", C("down"), T, first_bad_at=T - 60, failed_probes=6)]
    check("single-event tile names what it counts: failed probes",
          ("6", "failed probes") in [t[:2] for t in ha._tiles(one, None)], ha._tiles(one, None))

    print("--- the footer promises only channels that can deliver ---")
    dn = ha.Event("down", C("down"), T, first_bad_at=T - 1800, fail_streak=2, failed_probes=2)
    dg = ha.Event("degraded", C("degraded"), T, first_bad_at=T - 1800, fail_streak=2,
                  failed_probes=2)
    n = ha.build([dn], host="h", available=["email"])
    check("email only (ALERT_CHANNELS=email, or no phone): a DOWN goes by email",
          n.channels == ["email"], n.channels)
    check("...and promises one more EMAIL, not a text that will never come",
          "one more email when it recovers" in n.plain and "text" not in n.plain.split("recovers")[0][-40:],
          n.plain[-400:])
    n = ha.build([dg], host="h", available=["sms"])
    check("sms only: a DEGRADED (email route) falls back to a text instead of nothing",
          n.channels == ["sms"] and "one more text when it recovers" in n.plain, n.channels)
    n = ha.build([dn], host="h", available=[])
    check("nothing can deliver: the route is kept, so the send fails loudly", n.channels ==
          ["sms", "email"], n.channels)
    check("available_channels() reads ALERT_CHANNELS and the handle's addresses",
          ha.available_channels("ohmz") == ["sms", "email"], ha.available_channels("ohmz"))
    real_resolve = ha._at.resolve
    ha._at.resolve = lambda handle, conf=None, contacts=None: ("to@test", None)
    try:
        check("...dropping sms when the handle has no phone",
              ha.available_channels("ohmz") == ["email"], ha.available_channels("ohmz"))
        with tempfile.TemporaryDirectory() as td:
            got = Sender()
            ha._at.send_report = got
            sp = os.path.join(td, "s.json")
            ha.run("canary", [C("down")], sp, now=T, log=None)
            ha.run("canary", [C("down")], sp, now=T + RUN, log=None)
            check("run() with the real transport narrows the route before rendering",
                  got.calls and got.calls[0]["channels"] == ["email"]
                  and "one more email when it recovers" in got.calls[0]["plain"],
                  got.calls and got.calls[0]["channels"])
    finally:
        ha._at.resolve = real_resolve
        ha._at.send_report = _never

    print("--- unit failures in one burst: one notification ---")
    fs = [{"unit": u, "lines": [f"{u}: boom"], "facts": [["oom-kill", "result"]], "at": T + i}
          for i, u in enumerate(("hermes-gateway.service", "flightclaw.service",
                                 "hermes-delivery.service"))]
    n = ha.render_unit_failures(fs, host="h")
    check("three units -> ONE grouped text, text and email",
          n.sms == "Stack: FAILED: hermes-gateway.service, flightclaw.service, "
                   "hermes-delivery.service" and n.channels == ["sms", "email"], n.sms)
    check("...titled '3 units', every unit's journal under its name",
          n.plain.startswith("FAILED: 3 units") and all(f"{f['unit']}:" in n.plain for f in fs),
          n.plain[:200])
    check("...systemd's verdict in each card", n.plain.count("(systemd: result oom-kill)") == 3)
    check("...one truthful footer: within 2 seconds, one message",
          "3 units entered the failed state on h within 2 seconds of each other" in n.plain,
          n.plain[-500:])
    check("...no job footer, HTML rendered", JOB_FOOTER not in n.plain and n.html
          and not n.html_error, n.html_error)
    fast = [dict(f, at=T + i * 0.4) for i, f in enumerate(fs)]      # 2026-09-24: 0.79 s apart
    check("...a sub-second burst reads 'within a second', not 'within 0 seconds'",
          "within a second of each other" in ha.render_unit_failures(fast, host="h").plain)
    one = ha.render_unit_failures(fs[:1], host="h")
    check("one unit renders exactly as render_unit_failure",
          one.sms == ha.render_unit_failure("hermes-gateway.service", "h",
                                            ["hermes-gateway.service: boom"],
                                            facts=[("oom-kill", "result")], now=T).sms)

    print("--- send_report: pre-rendered bodies, partial success is success ---")
    at = load("/home/ohmz/StudioProjects/ai-stack/scripts/alert_transports.py", "at_report")
    at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}
    at.resolve = lambda h, c=None, k=None: ("to@test", "+15145579764")
    at.mail_domain_status = lambda addr, timeout=6: ("ok", "stubbed")
    sent = {}

    def ok_sms(phone, body, conf):
        sent["sms"] = body
        return "gateway:x"

    def ok_mail(to, subj, body, conf, html=None):
        sent.update(to=to, subj=subj, body=body, html=html)
        return True

    def boom(*a, **k):
        raise RuntimeError("leg down")
    at.send_sms, at.send_email = ok_sms, ok_mail
    ok, notes = at.send_report("ohmz", "Web search DOWN https://x.example.com/a", "subj",
                               "plain body", html="<b>h</b>")
    check("both legs -> ok", ok, notes)
    check("SMS goes through sms_body (URL stripped)", "https" not in sent["sms"]
          and "example.com" not in sent["sms"], sent["sms"])
    check("the logged SMS body is the one sent", f"body={sent['sms']!r}" in " ".join(notes), notes)
    check("email gets subject, plain and html as given",
          (sent["subj"], sent["body"], sent["html"]) == ("subj", "plain body", "<b>h</b>"))
    at.send_sms = boom
    ok, notes = at.send_report("ohmz", "s", "subj", "p")
    check("SMS fails, email lands -> ok", ok and any("sms FAILED" in n for n in notes), notes)
    at.send_sms, at.send_email = ok_sms, boom
    ok, notes = at.send_report("ohmz", "s", "subj", "p")
    check("email fails, SMS lands -> ok", ok and any("email FAILED" in n for n in notes), notes)
    at.send_sms = boom
    ok, notes = at.send_report("ohmz", "s", "subj", "p")
    check("both fail -> not ok", not ok, notes)
    texted = []
    at.send_sms, at.send_email = (lambda *a, **k: texted.append(a) or "x"), ok_mail
    ok, notes = at.send_report("ohmz", "s", "subj", "p", channels=["email"])
    check("channels=['email'] never touches SMS", ok and texted == [], notes)
    check("...and says so in the log", any("sms not requested" in n for n in notes), notes)
    at.load_conf = lambda: {"ALERT_CHANNELS": "email"}
    ok, notes = at.send_report("ohmz", "s", "subj", "p", channels=["sms", "email"])
    check("ALERT_CHANNELS stays the master switch", ok and texted == [], notes)
    at.load_conf = lambda: {"ALERT_CHANNELS": "sms"}
    texted.clear()
    ok, notes = at.send_report("ohmz", "s", "subj", "p", channels=["email"])
    check("requested channel disabled: sends by what IS enabled, and says so",
          ok and len(texted) == 1 and any("sending by sms instead" in n for n in notes), notes)
    at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}
    ok, notes = at.send_report("ohmz", "s", "Hermes API DOWN: banner\r\nBcc: x@y", "p",
                               channels=["email"])
    check("a CR/LF in the subject is collapsed, and the email leg is still called",
          ok and sent["subj"] == "Hermes API DOWN: banner Bcc: x@y", repr(sent.get("subj")))
    at.load_conf = lambda: {}
    ok, notes = at.send_report("ohmz", "s", "subj", "p")
    check("unconfigured -> not ok, no exception", not ok and "no transport" in notes[0])
    at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}
    at.mail_domain_status = lambda addr, timeout=6: ("dead", "no MX")
    at.send_sms = ok_sms
    ok, notes = at.send_report("ohmz", "s", "subj", "p")
    check("a dead mailbox is refused, like send_alert", any("NOT SENT" in n for n in notes), notes)

    print("--- send_alert is unchanged for job alerts ---")
    at.mail_domain_status = lambda addr, timeout=6: ("ok", "stubbed")
    got = {}
    at.send_sms = lambda phone, body, conf: got.__setitem__("sms", body) or "gateway:x"
    at.send_email = lambda to, subj, body, conf, html=None: got.update(subj=subj, body=body,
                                                                        html=html) or True
    msg = "amazon price: 46.99, under your 50.00 target"
    ok, _ = at.send_alert("ohmz", msg, job="cat board watch", job_id="ae57", when="now")
    check("payload-less call still delivers", ok)
    check("...with the old subject", got["subj"] == at.alert_subject(msg, "cat board watch"),
          got["subj"])
    check("...the old body, byte for byte",
          got["body"] == at.alert_email_body(msg, "cat board watch", "ae57", "now", "+15145579764"))
    check("...still the job footer (true for a scheduled job)", JOB_FOOTER in got["body"])
    check("...no HTML part", got["html"] is None)
    check("...and the old SMS", got["sms"] == at.sms_body(msg, "cat board watch"), got["sms"])
    ok, _ = at.send_alert("ohmz", "fallback", payload={"kind": "price_drop", "item": "Board",
                                                       "value": 46.99, "target": 50.0, "unit": "$"})
    check("a payload call still renders through alert_templates (HTML part present)",
          ok and bool(got["html"]))

    print("--- replay: the canary's real history under the policy ---")
    rows = read_fixture()
    check("fixture parsed", len(rows) > 300, len(rows))
    prev, old = "ok", 0
    for _, status, _ in rows:
        if (status != "ok") != (prev != "ok"):
            old += 1
        prev = status
    check("the old transition rule reproduces the journal's 22 alerts", old == 22, old)

    def replay(confirm_after, recover_after=ha.RECOVER_AFTER):
        st = ha.fresh_state("replay")
        out = []
        for ts, status, detail in rows:
            evs = ha.evaluate(st, [C(status, detail=detail)], ts, confirm_after,
                              recover_after=recover_after)
            if evs:
                out.append((ts, kinds(evs), ha.channels_for(evs)))
                ha.mark_delivered(st, evs, ts)
        return out
    check("the engine at confirm_after=1, recover_after=1 agrees with the old code",
          len(replay(1, 1)) == 22, len(replay(1, 1)))
    check("confirm_after=2 with recovery on one ok run: 6", len(replay(2, 1)) == 6,
          len(replay(2, 1)))
    new = replay(2)
    texts = sum(1 for n in new if "sms" in n[2])
    print(f"  replay: old code 22 notifications (22 texts); confirm_after=2, recover_after=2 -> "
          f"{len(new)} notifications ({texts} with a text)")
    for ts, ks, ch in new:
        print(f"    {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}  {'+'.join(ks):10} "
              f"{'+'.join(ch)}")
    check("the policy would have sent 8 notifications, not 22", len(new) == 8, len(new))
    check("...four outages, each one DOWN and one RECOVERED",
          [k for _, ks, _ in new for k in ks] == ["down", "recovered"] * 4)
    check("...the added one is 2026-09-23 11:18 DOWN, 11:50 OK, 12:22 DOWN, which one ok run erased",
          time.strftime("%m-%d %H:%M", time.localtime(new[0][0])) == "09-23 12:22",
          time.strftime("%m-%d %H:%M", time.localtime(new[0][0])))
    check("...all four were DOWN outages, so each message texts", all(ch == ["sms", "email"] for _, _, ch in new))

    print("--- CLI renders every sample without sending ---")
    with tempfile.TemporaryDirectory() as td:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ha.main(["--sample", "all", "--out", td])
        files = sorted(os.listdir(td))
        check("exit 0", rc == 0)
        check("an .html and a .txt per sample", len(files) == 12 and all(
            f"{n}.{x}" in files for n in ("down", "degraded", "recovered", "reminder",
                                          "unit_failed", "watchdog_multi") for x in ("html", "txt")),
            files)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

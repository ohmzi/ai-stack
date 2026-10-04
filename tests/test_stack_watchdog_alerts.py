#!/usr/bin/env python3
"""stack_watchdog and stack_alert on the health_alert engine. Offline: no systemd, no SMTP, no clock.

Why this file exists. Both scripts run unattended forever, and each rule below fails silently in
one of two directions:

  * too eager: the watchdog alerted on the FIRST failed run, and 2026-09-22..24 paid for it with
    20 texts-plus-emails, mostly a DOWN and a "recovered" minutes apart for a gateway restart or a
    5-minute gate blip. A channel that cries wolf gets muted.
  * too quiet: an alert swallowed for good. A dry run that saves state, a failed send recorded as
    delivered, an old-format state file that forgets an outage in progress, or backup freshness
    made to wait a second run it gains nothing from.

The durability checks (ticker, gwrestarts, backlog, hermesver) watch what the up/down probes
cannot: a cron thread dead inside a live gateway, restarts that finish between two probes,
results withheld by a failing webhook, an unplanned `hermes update`. Their probes are tested
against real temporary files, and their DEGRADED severity (email only) through the engine.

And stack_alert's text was never wrong, only useless: all 11 unit-failure texts of 2026-09 quoted a
"Last log" that did not say why (systemd bookkeeping, or the unit's NEXT run). The journal samples
below are those real sequences, trimmed.

Transports: the real alert_transports is loaded and its send_sms, send_email, send_report and
send_alert are replaced before either script is imported, so no path can reach a real transport.

Usage:  python3 tests/test_stack_watchdog_alerts.py
"""
import contextlib
import importlib.util
import io
import json
import os
import socket
import shutil
import sys
import tempfile
import time
import types
import urllib.error

os.environ["TZ"] = "America/Toronto"
time.tzset()

SCRIPTS = "/home/ohmz/StudioProjects/ai-stack/scripts"
sys.path.insert(0, SCRIPTS)
JOB_FOOTER = "You are receiving this because a background task you scheduled"
HOST = socket.gethostname()

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def _never(*a, **k):
    raise AssertionError("a test reached a real transport")


import alert_transports as at  # noqa: E402
at.send_sms = at.send_email = at.send_report = at.send_alert = _never
at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}           # never the live config
at.resolve = lambda handle, conf=None, contacts=None: ("to@test", "+15145550100")
import health_alert as ha  # noqa: E402
_TMP = tempfile.TemporaryDirectory(prefix="wd-alerts-")
ha.FALLBACK_DIR = os.path.join(_TMP.name, "runtime")                # never the live runtime dir
os.makedirs(ha.FALLBACK_DIR)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


wd = load(f"{SCRIPTS}/stack_watchdog.py", "wd")
sa = load(f"{SCRIPTS}/stack_alert.py", "sa")
sa.BATCH_FILE = os.path.join(_TMP.name, "stack_alert_batch.json")    # never ~/.hermes
sa._sleep = lambda s: None

T = 1790709468.0          # 2026-09-29 15:17:48 EDT
RUN = 300                 # stack-watchdog.timer: every 5 minutes


class Sender:
    """send_report stand-in: records calls, returns a scripted verdict."""
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, handle, sms, subject, plain, html=None, channels=None):
        self.calls.append(dict(handle=handle, sms=sms, subject=subject, plain=plain, html=html,
                               channels=list(channels or [])))
        return self.ok, ["stub " + ("sent" if self.ok else "FAILED")]


# --------------------------------------------------------------------------- watchdog harness

FNS = {"gateway": "check_gateway", "api": "check_api", "delivery": "check_delivery",
       "backup": "check_backup", "flightclaw": "check_flightclaw",
       "pubgate": "check_public_gate", "pubquota": "check_public_quota",
       "ticker": "check_ticker", "gwrestarts": "check_gateway_restarts",
       "backlog": "check_backlog", "hermesver": "check_hermes_version"}
REAL = {k: getattr(wd, fn) for k, fn in FNS.items()}
BAD, RAISING = set(), set()


def _stub(key):
    def fn():
        if key in RAISING:
            raise RuntimeError("probe exploded")
        return (False, f"{key} broken") if key in BAD else (True, f"{key} fine")
    return fn


for _k, _fn in FNS.items():
    setattr(wd, _fn, _stub(_k))
wd.report_only = lambda: ["comfyui=DOWN", "ollama=DOWN"]


def fresh_state_file(old=None):
    d = tempfile.mkdtemp()
    wd.STATE_FILE = os.path.join(d, "watchdog_state.json")
    if old is not None:
        with open(wd.STATE_FILE, "w") as f:
            json.dump(old, f)
    return wd.STATE_FILE


def tick(t, bad=(), raising=(), sender=None, dry=False, log=None):
    BAD.clear()
    BAD.update(bad)
    RAISING.clear()
    RAISING.update(raising)
    snd = sender or Sender()
    res = wd.notify(wd.probe_all(), dry=dry, now=t, sender=snd, log=log)
    return res, snd.calls


def kinds(res):
    return [(ev.kind, ev.check.key) for ev in res.events]


def saved():
    with open(wd.STATE_FILE) as f:
        return json.load(f)


# --------------------------------------------------------------------------- journal samples

def J(msg, ident="systemd", inv=None):
    d = {"MESSAGE": msg, "SYSLOG_IDENTIFIER": ident}
    if inv:
        d["USER_INVOCATION_ID" if ident == "systemd" else "_SYSTEMD_INVOCATION_ID"] = inv
    return json.dumps(d)


GW, DL = "hermes-gateway.service", "hermes-delivery.service"
I_PREV, I_FAIL = "a2a010155e45463eb12b514f71a2d7f9", "2440ada31ad943d493ca855cc2d06bfa"
# hermes-gateway, 2026-09-24 13:35. The old text said "Last log: hermes-gateway.service: Consumed
# 13min 2.379s CPU time, 184.4M mem...".
OOM = [
    J("2026-09-24 12:34:41,006 WARNING gateway.platforms.api_server: API server rejected invalid "
      "API key", "python", I_PREV),
    J(f"{GW}: Consumed 11min 15.872s CPU time, 183.5M memory peak, 0B memory swap peak.", inv=I_PREV),
    J(f"{GW}: Scheduled restart job, restart counter is at 3.", inv=I_PREV),
    J(f"Started {GW} - Hermes Agent Gateway - Messaging Platform Integration.", inv=I_FAIL),
    J(f"{GW}: systemd-oomd killed some process(es) in this unit.", inv=I_FAIL),
    J(f"{GW}: Main process exited, code=killed, status=9/KILL", inv=I_FAIL),
    J(f"{GW}: Failed with result 'oom-kill'.", inv=I_FAIL),
    J(f"{GW}: Triggering OnFailure= dependencies.", inv=I_FAIL),
    J(f"{GW}: Consumed 13min 2.379s CPU time, 184.4M memory peak, 0B memory swap peak.", inv=I_FAIL),
]
I_D1, I_D2 = "6e0ad54675e84c90b3178b1b844b30f2", "0f0c3d1e2a4b4c5d8e9f0a1b2c3d4e5f"
DESC = "Deliver hermes cron outputs (channel log + conditional phone push)"
# hermes-delivery, 2026-09-24 15:29: the per-minute oneshot ran again, and succeeded, before the
# alert read the journal. The old text said "Last log: Finished hermes-delivery.service - ...".
DELIVERY = [
    J(f"Starting {DL} - {DESC}...", inv=I_D1),
    J(f"{DL}: systemd-oomd killed 1 process(es) in this unit.", inv=I_D1),
    J(f"{DL}: Main process exited, code=killed, status=9/KILL", inv=I_D1),
    J(f"{DL}: Failed with result 'oom-kill'.", inv=I_D1),
    J(f"Failed to start {DL} - {DESC}.", inv=I_D1),
    J(f"{DL}: Triggering OnFailure= dependencies.", inv=I_D1),
    J(f"{DL}: Consumed 42.994s CPU time.", inv=I_D1),
    J(f"Starting {DL} - {DESC}...", inv=I_D2),
    J("nothing new", "python3", I_D2),
    J(f"Finished {DL} - {DESC}.", inv=I_D2),
]
I_78 = "7878787878787878787878787878aaaa"
# The failure stack_alert exists for: exit 78 parks Restart=always for good. The reason is the
# service's own last words, which "Failed with result 'exit-code'" would otherwise bury.
EXIT78 = [
    J(f"Started {GW} - Hermes Agent Gateway - Messaging Platform Integration.", inv=I_78),
    J("hermes-gateway: profile 'coding' has no model configured", "python", I_78),
    J("hermes-gateway: refusing to start with an incomplete profile (exit 78)", "python", I_78),
    J(f"{GW}: Main process exited, code=exited, status=78/CONFIG", inv=I_78),
    J(f"{GW}: Failed with result 'exit-code'.", inv=I_78),
    J(f"{GW}: Triggering OnFailure= dependencies.", inv=I_78),
    J(f"{GW}: Consumed 2.113s CPU time.", inv=I_78),
]
OOM_ENV = {"MONITOR_SERVICE_RESULT": "oom-kill", "MONITOR_EXIT_CODE": "killed",
           "MONITOR_EXIT_STATUS": "KILL", "MONITOR_INVOCATION_ID": I_FAIL, "MONITOR_UNIT": GW}


class Journal:
    """Stands in for stack_alert's subprocess module: journalctl answers with `lines`."""
    def __init__(self, lines=None, raises=None):
        self.lines, self.raises, self.argv = lines or [], raises, None

    def run(self, argv, **kw):
        self.argv = argv
        if self.raises:
            raise self.raises
        return types.SimpleNamespace(stdout="\n".join(self.lines) + "\n", returncode=0)


def quietly(fn, *a):
    """fn(*a) with stdout and stderr captured. -> (result, captured text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*a), buf.getvalue()


def alert(argv, env, journal, sender=None):
    """Run stack_alert.main with the journal and transport substituted. -> (rc, calls)."""
    snd = sender or Sender()
    real_sub = sa.subprocess
    sa.subprocess = journal
    at.send_report = snd
    try:
        rc, _ = quietly(sa.main, argv, env)
    finally:
        sa.subprocess = real_sub
        at.send_report = _never
    return rc, getattr(snd, "calls", [])


def main():
    check("the scripts share the patched transport module", ha._at is at and wd.ha is ha)

    print("--- watchdog: keys and per-check confirmation are the approved ones ---")
    check("old check keys unchanged and first, so old alerted flags carry over; new ones after",
          list(wd.CHECKS) == ["gateway", "api", "delivery", "backup", "flightclaw", "pubgate",
                              "pubquota", "ticker", "gwrestarts", "backlog", "hermesver"],
          list(wd.CHECKS))
    ONE_RUN = {"backup", "gwrestarts", "backlog", "hermesver"}
    check("backup, gwrestarts, backlog, hermesver confirm and recover on 1 run; the rest on 2",
          {k: wd._policy(k) for k in wd.CHECKS} ==
          {k: ((1, 1) if k in ONE_RUN else (2, 2)) for k in wd.CHECKS},
          {k: wd._policy(k) for k in wd.CHECKS})
    check("gwrestarts, backlog, hermesver fail DEGRADED; every other check fails DOWN",
          {k: wd.SEVERITY.get(k, "down") for k in wd.CHECKS} ==
          {k: ("degraded" if k in {"gwrestarts", "backlog", "hermesver"} else "down")
           for k in wd.CHECKS}, wd.SEVERITY)

    print("--- watchdog: one failed run is a blip, two are an outage ---")
    fresh_state_file()
    res, sent = tick(T, bad={"gateway"})
    check("run 1 failing: nothing sent", sent == [] and res.events == [], kinds(res))
    check("...but the failure is counted", saved()["checks"]["gateway"]["fail_streak"] == 1)
    res, sent = tick(T + RUN)
    res2, sent2 = tick(T + 2 * RUN)
    check("healthy for the next two runs: the blip ends in silence (the 2026-09-23 pattern)",
          sent == [] == sent2 and res.events == [] == res2.events
          and saved()["checks"]["gateway"]["fail_streak"] == 0, kinds(res))
    T0 = T + RUN
    tick(T0 + 2 * RUN, bad={"gateway"})
    res, sent = tick(T0 + 3 * RUN, bad={"gateway"})
    check("two consecutive failed runs: exactly one notification", len(sent) == 1, sent)
    s = sent[0] if sent else {}
    check("...text AND email (every watchdog check is up/down)",
          s.get("channels") == ["sms", "email"], s.get("channels"))
    check("...the text names the check and says why",
          s.get("sms") == "Hermes gateway DOWN: gateway broken", s.get("sms"))
    check("...subject keeps the [stack] prefix mail filters match on",
          s.get("subject", "").startswith("[stack] Hermes gateway DOWN"), s.get("subject"))
    check("...sent to the owner's handle", s.get("handle") == "ohmz", s.get("handle"))
    quiet = [tick(T0 + (4 + i) * RUN, bad={"gateway"})[1] for i in range(10)]
    check("still failing for the next 10 runs: nothing more", all(q == [] for q in quiet))
    lines = []
    res, sent = tick(T0 + 14 * RUN, log=lines.append)
    check("first healthy run: not yet a recovery (two ok runs in a row are needed)", sent == [],
          sent)
    check("...and the journal line says so",
          any("[alerted; 1 of 2 ok runs to recover]" in l for l in lines), lines)
    res, sent = tick(T0 + 15 * RUN)
    check("second healthy run: exactly one notification", len(sent) == 1, sent)
    check("...that says RECOVERED with the outage length, measured to the first ok run",
          sent and sent[0]["sms"].startswith("Hermes gateway RECOVERED after 1h00m") and
          sent[0]["channels"] == ["sms", "email"], sent and sent[0]["sms"])
    check("steady state is silent again", tick(T0 + 16 * RUN)[1] == [])

    print("--- watchdog: fail, ok, fail is an outage (the 2026-09-24 gateway) ---")
    fresh_state_file()
    tick(T, bad={"gateway"})
    tick(T + RUN)
    res, sent = tick(T + 2 * RUN, bad={"gateway"})
    check("one ok run between two failures no longer hides them: DOWN on the second",
          kinds(res) == [("down", "gateway")] and len(sent) == 1, kinds(res))

    print("--- watchdog: backup freshness alerts on its first failed run ---")
    fresh_state_file()
    res, sent = tick(T, bad={"backup", "gateway"})
    check("backup stale once: alerted at once, gateway (1 of 2) is not",
          kinds(res) == [("down", "backup")] and len(sent) == 1, kinds(res))
    check("...the text is about the backup", sent and "Backup freshness DOWN" in sent[0]["sms"],
          sent and sent[0]["sms"])
    res, sent = tick(T + RUN, bad={"backup", "gateway"})
    check("next run: only gateway is news (backup already alerted)",
          kinds(res) == [("down", "gateway")] and len(sent) == 1, kinds(res))
    res, sent = tick(T + 2 * RUN, bad={"gateway"})
    check("...and backup recovers on its first fresh run too (one run adds no evidence)",
          kinds(res) == [("recovered", "backup")], kinds(res))

    print("--- watchdog: one notification per run, however many checks changed ---")
    fresh_state_file()
    tick(T, bad={"gateway", "api"})
    res, sent = tick(T + RUN, bad={"gateway", "api"})
    check("gateway + api confirmed in the same run: one notification", len(sent) == 1, sent)
    check("...one grouped text", sent and sent[0]["sms"] == "Stack: DOWN: Hermes gateway, Hermes API",
          sent and sent[0]["sms"])
    fresh_state_file()
    tick(T, bad={"gateway"})
    res, sent = tick(T + RUN, bad={"gateway", "backup"})
    check("events from both confirm groups in one run still make ONE notification",
          len(sent) == 1 and kinds(res) == [("down", "gateway"), ("down", "backup")],
          (len(sent), kinds(res)))

    print("--- watchdog: the email is the branded engine report ---")
    fresh_state_file()
    tick(T, bad={"pubgate"})
    res, sent = tick(T + RUN, bad={"pubgate"})
    s = sent[0] if sent else {}
    html, plain = s.get("html") or "", s.get("plain") or ""
    check("an HTML part is sent", html.startswith("<!doctype html>"), html[:40])
    check("...with the Ohmz Stack masthead and the check as title",
          "Ohmz" in html and "Stack" in html and "Public gate" in html)
    check("...and every alerting check listed", all(lbl in html for lbl, _ in wd.CHECKS.values()))
    check("never the job-alert footer", JOB_FOOTER not in html and JOB_FOOTER not in plain)
    check("the footer says what happened: 2 failed probes over the 5 minutes between runs",
          "Sent after 2 failed probes over 5 minutes." in plain, plain[-600:])
    check("...and where to look", "Inspect: journalctl --user -u stack-watchdog" in plain)

    print("--- watchdog: a crashing checker is a failure, not a crash ---")
    fresh_state_file()
    tick(T, raising={"api"})
    res, sent = tick(T + RUN, raising={"api"})
    check("checker exception alerts after confirmation like any failure",
          len(sent) == 1 and "checker error" in sent[0]["plain"], sent and sent[0]["sms"])

    print("--- watchdog: a failed send is retried, not recorded as delivered ---")
    fresh_state_file()
    tick(T, bad={"flightclaw"})
    res, sent = tick(T + RUN, bad={"flightclaw"}, sender=Sender(ok=False))
    check("send failed", len(sent) == 1 and res.sent is False)
    check("...so the check is not marked alerted", saved()["checks"]["flightclaw"]["alerted"] is False)
    res, sent = tick(T + 2 * RUN, bad={"flightclaw"})
    check("the next run sends the DOWN again",
          len(sent) == 1 and kinds(res) == [("down", "flightclaw")],
          kinds(res))
    check("...and now it is marked", saved()["checks"]["flightclaw"]["alerted"] is True)

    print("--- watchdog: --dry-run observes, never sends, never saves ---")
    fresh_state_file()
    tick(T, bad={"delivery"})
    with open(wd.STATE_FILE) as f:
        before = f.read()
    lines = []
    res, sent = tick(T + RUN, bad={"delivery"}, dry=True, log=lines.append)
    with open(wd.STATE_FILE) as f:
        after = f.read()
    check("an owed alert under --dry-run sends nothing", sent == [] and not res.sent)
    check("...shows what it would send", any("would send via sms+email" in l for l in lines) and
          any("Delivery timer DOWN" in l for l in lines), lines)
    check("...and leaves the state file byte-identical", before == after)
    res, sent = tick(T + 2 * RUN, bad={"delivery"})
    check("the real run after it still alerts (dry-run must not swallow the edge)",
          len(sent) == 1 and kinds(res) == [("down", "delivery")], kinds(res))
    fresh_state_file()
    lines = []
    tick(T, bad={"api"}, log=lines.append)
    check("the journal line shows confirmation progress",
          any(l.startswith("[watchdog] api") and "FAIL" in l and "[1 of 2 runs, not alerted]" in l
              for l in lines), lines)

    print("--- watchdog: the old state file migrates through the engine ---")
    old = {k: {"ok": True, "detail": f"{k} ok", "at": int(T - 60)} for k in wd.CHECKS}
    old["pubgate"] = {"ok": False, "detail": "public gate (no HTTP answer on :4568)",
                      "at": int(T - 600)}
    fresh_state_file(old)
    tick(T)
    res, sent = tick(T + RUN)
    check("an outage the old code alerted still gets its recovery (on the second ok run)",
          kinds(res) == [("recovered", "pubgate")] and len(sent) == 1, kinds(res))
    st = saved()
    check("...and the file is rewritten in the engine's shape, same keys",
          st.get("version") == 2 and set(st.get("checks", {})) == set(wd.CHECKS), st.keys())
    check("the run after is silent", tick(T + 2 * RUN)[1] == [])
    fresh_state_file(old)
    res, sent = tick(T, bad={"pubgate"})
    check("an old outage still failing is not re-alerted as new", sent == [], kinds(res))
    res, sent = tick(T - 600 + 86400 + 1, bad={"pubgate"})
    check("...it gets the daily reminder instead",
          kinds(res) == [("reminder", "pubgate")] and "still DOWN" in (sent and sent[0]["sms"] or ""),
          sent and sent[0]["sms"])
    fresh_state_file()
    with open(wd.STATE_FILE, "w") as f:
        f.write("{not json")
    res, sent = tick(T)
    check("a corrupt state file is a fresh start, not a crash", res.events == [] and
          saved().get("version") == 2)

    print("--- watchdog: main() ---")
    fresh_state_file()
    snd = Sender()
    at.send_report = snd
    try:
        rc, _ = quietly(wd.main, [])
        BAD.update({"gateway"})
        quietly(wd.main, [])
        rc2, out = quietly(wd.main, [])
    finally:
        at.send_report = _never
        BAD.clear()
    check("main exits 0 and report-only checks never alert (comfyui/ollama stubbed DOWN)",
          rc == 0 and rc2 == 0 and len(snd.calls) == 1 and "comfyui" not in snd.calls[0]["plain"]
          and "comfyui" not in saved()["checks"], [c["sms"] for c in snd.calls])
    check("...though they are still logged", "info: comfyui=DOWN, ollama=DOWN" in out, out)
    wd.STATE_FILE = "/dev/null/watchdog_state.json"
    at.send_report = Sender()
    try:
        rc, out = quietly(wd.main, [])
        check("state file unwritable, tmpfs fallback writable: saved there, exit 0",
              rc == 0 and os.path.exists(ha.fallback_path(wd.STATE_FILE)), (rc, out[-300:]))
        real_fb, ha.FALLBACK_DIR = ha.FALLBACK_DIR, "/dev/null/runtime"
        try:
            rc, out = quietly(wd.main, [])
        finally:
            ha.FALLBACK_DIR = real_fb
    finally:
        at.send_report = _never
    check("neither writable: exits non-zero (it would forget its alerted flags)", rc == 1, rc)

    print("--- watchdog: a real run is locked, marked in flight, and bounded ---")
    fresh_state_file()
    snd = Sender()
    at.send_report = snd
    try:
        with ha.exclusive(wd.STATE_FILE):
            BAD.update({"gateway"})
            rc, out = quietly(wd.main, [])
            rc2, out2 = quietly(wd.main, ["--dry-run"])
        check("a run while another holds the lock: exit 0, no probe, no send, says why",
              rc == 0 and snd.calls == [] and "another run is in progress" in out
              and not os.path.exists(wd.STATE_FILE), out)
        check("...--dry-run needs no lock (it runs beside the holder) and writes nothing",
              rc2 == 0 and "[watchdog] gateway" in out2 and not os.path.exists(wd.STATE_FILE),
              out2[-200:])
        kw = dict(label=wd.RUNS_LABEL, unit=wd.UNIT, timeout_s=wd.TIMEOUT_START_S,
                  max_gap_s=wd.MAX_GAP_S, log=None, sender=snd)
        ha.begin("watchdog", wd.STATE_FILE, **kw)          # two runs that systemd killed
        ha.begin("watchdog", wd.STATE_FILE, **kw)
        BAD.clear()
        rc, out = quietly(wd.main, [])
        check("main() after two killed runs: ONE 'Watchdog runs DOWN' text, before probing",
              [c["sms"] for c in snd.calls] == ["Watchdog runs DOWN: 2 runs in a row killed by "
                                                "systemd"], [c["sms"] for c in snd.calls])
        check("...and it quotes the unit's TimeoutStartSec",
              snd.calls and "TimeoutStartSec=120" in snd.calls[0]["plain"])
        check("...the run itself finished: marker cleared", saved()["run"]["inflight_at"] is None)
        quietly(wd.main, [])
        check("the second finished run recovers it",
              len(snd.calls) == 2 and snd.calls[1]["sms"].startswith("Watchdog runs RECOVERED"),
              [c["sms"] for c in snd.calls])
    finally:
        at.send_report = _never
        BAD.clear()
    import threading
    gate = threading.Event()
    real_backup = wd.check_backup
    wd.check_backup = lambda: (gate.wait(5), (True, "late"))[1]
    try:
        t0 = time.monotonic()
        rs = wd.probe_all(budget_s=0.3)
        took = time.monotonic() - t0
    finally:
        gate.set()
        wd.check_backup = real_backup
    bk = [r for r in rs if r.key == "backup"][0]
    check("a hung probe cannot hold the run: DOWN 'did not finish' at the budget",
          bk.status == "down" and "did not finish within 0.3 s" in bk.detail and took < 2,
          (bk.detail, took))
    check("...the other checks still report, in CHECKS order",
          [r.key for r in rs] == list(wd.CHECKS) and all(r.status == "ok" for r in rs
                                                          if r.key != "backup"))

    print("--- watchdog: real probes word their verdicts for a text ---")
    d = tempfile.mkdtemp()
    stamp = os.path.join(d, "LAST_OK")
    open(stamp, "w").close()
    real_path, wd.BACKUP_LAST_OK = wd.BACKUP_LAST_OK, stamp
    try:
        fresh_ok = REAL["backup"]()
        os.utime(stamp, (time.time() - 31 * 3600,) * 2)
        stale = REAL["backup"]()
        os.unlink(stamp)
        missing = REAL["backup"]()
    finally:
        wd.BACKUP_LAST_OK = real_path
    check("backup fresh", fresh_ok == (True, "LAST_OK 0h old"), fresh_ok)
    check("backup stale names age and limit", stale == (False, "LAST_OK 31h old (limit 26h)"), stale)
    check("backup missing says so", missing[0] is False and "unreadable" in missing[1], missing)
    real_run = wd._run
    wd._run = lambda cmd, timeout=10: types.SimpleNamespace(stdout="failed\n", returncode=3)
    try:
        gw = REAL["gateway"]()
    finally:
        wd._run = real_run
    check("gateway reports the unit state", gw == (False, "service failed"), gw)
    real_open = wd.urllib.request.urlopen

    def http_401(url, timeout=5):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    def refused(url, timeout=5):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    try:
        wd.urllib.request.urlopen = http_401
        api_up = REAL["api"]()
        wd.urllib.request.urlopen = refused
        api_down = REAL["api"]()
    finally:
        wd.urllib.request.urlopen = real_open
    check("a 401 from the API is a live server", api_up == (True, "HTTP 401 on :8642"), api_up)
    check("no answer says why in two words",
          api_down == (False, "no HTTP answer on :8642 (Connection refused)"), api_down)

    seen = []

    def http_200(url, timeout=5):
        seen.append(url)
        return contextlib.nullcontext(types.SimpleNamespace(status=200))
    try:
        wd.urllib.request.urlopen = http_200
        api_ok = REAL["api"]()
    finally:
        wd.urllib.request.urlopen = real_open
    check("the API probe asks the keyless /health, not /v1/models (item 18: 247 log lines a day)",
          seen == ["http://127.0.0.1:8642/health"] and api_ok == (True, "HTTP 200 on :8642"),
          (seen, api_ok))

    print("--- watchdog: ticker (a dead or failing cron thread in a live gateway) ---")
    home = tempfile.mkdtemp()
    tdir = os.path.join(home, "cron")
    cdir = os.path.join(home, "profiles", "coding", "cron")
    os.makedirs(tdir)
    os.makedirs(cdir)
    # hermes only ticks a dir under profiles/ that carries an identity marker, so the fixture's
    # coding profile needs one, exactly as the live profile has config.yaml.
    with open(os.path.join(home, "profiles", "coding", "config.yaml"), "w") as f:
        f.write("model: {}\n")

    def beat(d, hb_age, ok_age="same"):
        now = time.time()
        for name, age in (("ticker_heartbeat", hb_age),
                          ("ticker_last_success", hb_age if ok_age == "same" else ok_age)):
            p = os.path.join(d, name)
            if age is None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(p)
            else:
                with open(p, "w") as f:
                    f.write(str(now - age) if isinstance(age, (int, float)) else age)

    real_dirs = (wd.TICKER_DIR, wd.TICKER_PROFILES_ROOT)
    wd.TICKER_DIR, wd.TICKER_PROFILES_ROOT = tdir, os.path.join(home, "profiles")
    try:
        beat(tdir, 20)
        beat(cdir, 21)
        fresh = REAL["ticker"]()
        beat(cdir, 12 * 60 + 5)
        stale = REAL["ticker"]()
        beat(cdir, 20, ok_age=7 * 60 + 1)
        failing = REAL["ticker"]()
        beat(cdir, 20, ok_age=None)
        never = REAL["ticker"]()
        beat(cdir, 20)
        beat(tdir, None)
        missing = REAL["ticker"]()
        beat(tdir, "not-a-number")
        garbage = REAL["ticker"]()
        beat(tdir, -600)                                # a clock stepped back: the beat is "ahead"
        ahead = REAL["ticker"]()
        beat(tdir, wd.TICKER_MAX_AGE_S - 30)
        edge_ok = REAL["ticker"]()
        beat(tdir, wd.TICKER_MAX_AGE_S + 1)
        edge_bad = REAL["ticker"]()
        # Directories under profiles/ that hermes does NOT tick, so nothing refreshes their beat.
        # Each used to fail the check as DOWN (a text, then a daily reminder) until deleted.
        beat(tdir, 20)
        beat(cdir, 20)
        prof = os.path.join(home, "profiles")
        bak = os.path.join(prof, "coding.bak-20261001")      # `cp -a coding coding.bak-...`
        shutil.copytree(os.path.join(prof, "coding"), bak, symlinks=True)
        beat(os.path.join(bak, "cron"), 60 * 60)
        ghost = os.path.join(prof, "ghost", "cron")           # runtime side effect: cron/ only
        os.makedirs(ghost)
        gone = os.path.join(prof, "retired")                  # deleted, then re-created by a
        os.makedirs(os.path.join(gone, "cron"))               # stale process: tombstoned
        with open(os.path.join(gone, "config.yaml"), "w") as f:
            f.write("model: {}\n")
        os.makedirs(os.path.join(prof, ".deleted"))
        with open(os.path.join(prof, ".deleted", "retired"), "w") as f:
            f.write("deleted\n")
        strays = REAL["ticker"]()
        # ...while a real second profile (valid id, a marker, no tombstone) is still watched.
        extra = os.path.join(prof, "research")
        os.makedirs(os.path.join(extra, "cron"))
        os.symlink(os.path.join(home, "no-such-soul.md"), os.path.join(extra, "SOUL.md"))
        real_second = REAL["ticker"]()
        shutil.rmtree(extra)
        # An unreadable profiles/ is a FAIL, not a silently shorter list.
        real_list = wd._ticked_profiles
        wd._ticked_profiles = lambda root=None: (_ for _ in ()).throw(PermissionError(13, "Permission denied"))
        try:
            unreadable = REAL["ticker"]()
        finally:
            wd._ticked_profiles = real_list
    finally:
        wd.TICKER_DIR, wd.TICKER_PROFILES_ROOT = real_dirs
    check("both profiles fresh: OK, naming each profile it found",
          fresh[0] is True and fresh[1].startswith("beat 2") and "(default, coding)" in fresh[1],
          fresh)
    check("a profile's heartbeat 12 min old: FAIL naming the profile, the age and the limit",
          stale == (False, "coding: last beat 12 min ago (limit 5)"), stale)
    check("heartbeat fresh but no successful tick for 7 min: FAIL, alive and failing every tick",
          failing == (False, "coding: beating, but no successful tick in 7 min"), failing)
    check("...and with no success marker at all", never == (False, "coding: beating, but no "
                                                            "successful tick recorded"), never)
    check("the default profile's heartbeat missing: FAIL, not skipped",
          missing[0] is False and missing[1].startswith("default: heartbeat ")
          and "No such file" in missing[1], missing)
    check("a heartbeat that is not a number: FAIL", garbage == (False, "default: heartbeat "
                                                                 "unreadable"), garbage)
    check("a beat in the future (clock stepped back) is fresh, not negative", ahead[0] is True,
          ahead)
    check(f"just under {wd.TICKER_MAX_AGE_S} s: OK; just over: FAIL",
          edge_ok[0] is True and edge_bad[0] is False, (edge_ok, edge_bad))
    check("a .bak copy, a marker-less ghost and a tombstoned dir are not profiles: still OK",
          strays[0] is True and "(default, coding)" in strays[1], strays)
    check("a second real profile (dangling symlinked marker counts, as in hermes) IS watched",
          real_second[0] is False and "research: heartbeat " in real_second[1]
          and "coding" not in real_second[1], real_second)
    check("an unreadable profiles/ fails the check instead of watching fewer profiles",
          unreadable[0] is False and unreadable[1].startswith("profiles: Permission denied"),
          unreadable)
    fresh_state_file()
    tick(T, bad={"ticker"})
    res, sent = tick(T + RUN, bad={"ticker"})
    check("ticker confirms on 2 runs and texts: a dead ticker is jobs not firing",
          kinds(res) == [("down", "ticker")] and sent and sent[0]["channels"] == ["sms", "email"]
          and sent[0]["sms"] == "Cron ticker DOWN: ticker broken", sent)

    print("--- watchdog: gwrestarts (restarts is-active never sees) ---")
    ledger = os.path.join(home, "gateway-starts.log")

    def starts(*hours_ago, junk=False):
        now = time.time()
        lines = [repr(now - h * 3600) for h in hours_ago] + (["", "garbage", "1e"] if junk else [])
        with open(ledger, "w") as f:
            f.write("\n".join(lines) + "\n")
        return REAL["gwrestarts"]()

    real_ledger, wd.GATEWAY_STARTS_LOG = wd.GATEWAY_STARTS_LOG, ledger
    try:
        two = starts(30, 5.5, 1)
        three = starts(5.9, 3, 0.01)
        slid = starts(6.01, 3, 0.01)
        junk = starts(2, 1, junk=True)
        none = starts(48, 30)
        future = starts(2, 1, -0.5)                     # a clock stepped back
        open(ledger, "w").close()
        empty = REAL["gwrestarts"]()
        os.unlink(ledger)
        gone = REAL["gwrestarts"]()
    finally:
        wd.GATEWAY_STARTS_LOG = real_ledger
    check("2 starts in 6 h: OK, with the count and the last one",
          two[0] is True and two[1].startswith("2 starts in 6 h, last "), two)
    check("3 starts in 6 h: FAIL, saying when it alerts",
          three[0] is False and three[1].startswith("3 starts in 6 h, last ")
          and three[1].endswith("(alerts at 3)"), three)
    check("the oldest slides out of the window: OK again", slid[0] is True and
          slid[1].startswith("2 starts"), slid)
    check("blank and unparseable lines are ignored, as hermes's own reader does",
          junk[0] is True and junk[1].startswith("2 starts"), junk)
    check("only old starts: OK, 'no starts in 6 h'", none == (True, "no starts in 6 h"), none)
    check("a start stamped in the future still counts", future[0] is False, future)
    check("an empty ledger: OK", empty == (True, "no starts in 6 h"), empty)
    check("no ledger at all: FAIL (the check would be blind)",
          gone[0] is False and "gateway-starts.log unreadable" in gone[1], gone)
    fresh_state_file()
    lines = []
    res, sent = tick(T, bad={"gwrestarts"}, log=lines.append)
    check("gwrestarts alerts on its first failed run, DEGRADED, by email only",
          kinds(res) == [("degraded", "gwrestarts")] and len(sent) == 1
          and sent[0]["channels"] == ["email"], (kinds(res), sent and sent[0]["channels"]))
    check("...the subject says DEGRADED",
          sent and sent[0]["subject"].startswith("[stack] Gateway restarts DEGRADED"),
          sent and sent[0]["subject"])
    check("...and the journal line says WARN, not FAIL",
          any(l.startswith("[watchdog] gwrestarts WARN gwrestarts broken") for l in lines), lines)
    fresh_state_file()
    tick(T, bad={"gateway"})
    res, sent = tick(T + RUN, bad={"gateway", "gwrestarts"})
    check("gateway DOWN and gwrestarts DEGRADED in one run: one notification, and it texts",
          len(sent) == 1 and sent[0]["channels"] == ["sms", "email"]
          and kinds(res) == [("down", "gateway"), ("degraded", "gwrestarts")], kinds(res))
    res, sent = tick(T + 2 * RUN, bad={"gateway"})
    check("the window slides past: RECOVERED on the first ok run, by email (it was degraded)",
          kinds(res) == [("recovered", "gwrestarts")] and sent[0]["channels"] == ["email"],
          (kinds(res), sent and sent[0]["channels"]))

    print("--- watchdog: backlog (LOG results withheld by a failing webhook) ---")
    out = os.path.join(home, "output")
    job = os.path.join(out, "e76a6b27c17f")
    os.makedirs(job)
    state_path = os.path.join(out, ".delivered.json")

    def output(name, age_s):
        p = os.path.join(job, name)
        open(p, "w").close()
        os.utime(p, (time.time() - age_s,) * 2)
        return p

    def backlog(state):
        with open(state_path, "w") as f:
            json.dump(state, f)
        return REAL["backlog"]()

    old1, old2 = output("2026-09-29_01-00-00.md", 3 * 3600), output("2026-09-29_02-00-00.md", 1200)
    young = output("2026-09-29_03-00-00.md", 14 * 60)
    done = output("2026-09-28_03-00-00.md", 86400)
    pruned = os.path.join(job, "2026-09-01_00-00-00.md")      # in the state, file deleted
    hook = "https://hooks.example.invalid/webhooks/SECRET-TOKEN"
    real_state, wd.DELIVERED_STATE = wd.DELIVERED_STATE, state_path
    try:
        healthy = backlog({done: {"log": True, "alerts": True},
                           pruned: {"log": False, "alerts": True},
                           young: {"log": False, "alerts": True}})
        stuck = backlog({done: {"log": True, "alerts": True}, old1: {"log": False, "alerts": True},
                         old2: {"log": False, "alerts": True, "note": hook},
                         young: {"log": False, "alerts": True}, pruned: {"log": False}})
        alerts_leg = backlog({old1: {"log": True, "alerts": False}})
        legacy = backlog([old1, old2])
        odd = backlog({old1: "yes", old2: {"alerts": True}, "": {"log": False}})
        with open(state_path, "w") as f:
            f.write('{"truncated": ')
        corrupt = REAL["backlog"]()
        os.unlink(state_path)
        absent = REAL["backlog"]()
    finally:
        wd.DELIVERED_STATE = real_state
    check("a 14-min-old retry and a pruned file are not a backlog",
          healthy == (True, "no LOG result withheld"), healthy)
    check("two results undelivered past 15 min: FAIL with the count, oldest age and job",
          stuck == (False, "2 LOG results undelivered, oldest 180 min (job e76a6b27c17f; "
                           "limit 15)"), stuck)
    check("...never a webhook URL, even one sitting in the state file",
          "http" not in stuck[1] and "SECRET" not in stuck[1] and "hooks" not in stuck[1], stuck)
    check("only the LOG leg counts (alerts have their own retry queue)", alerts_leg[0] is True,
          alerts_leg)
    check("the original list format (all delivered): OK", legacy[0] is True, legacy)
    check("entries of an unexpected shape are skipped, not a crash", odd[0] is True, odd)
    check("unreadable state: FAIL", corrupt[0] is False and "unreadable" in corrupt[1], corrupt)
    check("no state file yet: OK", absent == (True, "no delivery state yet"), absent)
    fresh_state_file()
    res, sent = tick(T, bad={"backlog"})
    check("backlog alerts on its first failed run, DEGRADED, email only",
          kinds(res) == [("degraded", "backlog")] and sent[0]["channels"] == ["email"],
          (kinds(res), sent and sent[0]["channels"]))

    print("--- watchdog: hermesver (the pinned checkout and version) ---")
    tag, commit, version = wd.HERMES_PIN
    real_run = wd._run
    git = {"out": commit + "\n", "rc": 0}
    health = {"body": json.dumps({"status": "ok", "platform": "hermes-agent",
                                  "version": version}).encode()}

    def fake_run(cmd, timeout=10):
        if cmd[:1] != ["git"]:
            raise AssertionError(cmd)
        return None if git["rc"] is None else types.SimpleNamespace(stdout=git["out"],
                                                                    returncode=git["rc"])

    def fake_open(url, timeout=5):
        if health["body"] is None:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return contextlib.nullcontext(io.BytesIO(health["body"]))
    wd._run, wd.urllib.request.urlopen = fake_run, fake_open
    try:
        pinned = REAL["hermesver"]()
        git["out"] = "0123456789abcdef0123456789abcdef01234567\n"
        moved = REAL["hermesver"]()
        git["out"] = commit + "\n"
        health["body"] = b'{"status": "ok", "version": "0.22.0"}'
        newer = REAL["hermesver"]()
        health["body"] = None
        silent = REAL["hermesver"]()
        git["rc"] = 128
        broken = REAL["hermesver"]()
        git["rc"] = None
        no_git = REAL["hermesver"]()
    finally:
        wd._run, wd.urllib.request.urlopen = real_run, real_open
    check("the pinned commit, running the pinned version: OK",
          pinned == (True, f"{tag} at {commit[:8]}, gateway runs {version}"), pinned)
    check("HEAD moved (a `hermes update`): FAIL naming both commits and the tag",
          moved == (False, f"checkout at 01234567, pinned {commit[:8]} ({tag})"), moved)
    check("the gateway reports another version: FAIL", newer == (False, f"gateway runs 0.22.0, "
                                                                 f"pinned {version}"), newer)
    check("gateway silent: judged on the checkout alone (liveness is the api check's alert)",
          silent == (True, f"{tag} at {commit[:8]}, gateway runs unknown (no answer)"), silent)
    check("git failing or missing: FAIL, the check cannot see",
          broken == (False, "checkout commit unreadable") == no_git, (broken, no_git))
    fresh_state_file()
    res, sent = tick(T, bad={"hermesver"})
    check("hermesver alerts on its first failed run, DEGRADED, email only",
          kinds(res) == [("degraded", "hermesver")] and sent[0]["channels"] == ["email"],
          (kinds(res), sent and sent[0]["channels"]))

    print("--- watchdog: severity survives a crash or a timeout ---")
    fresh_state_file()
    RAISING.update({"backlog"})
    try:
        rs = {r.key: r for r in wd.probe_all()}
    finally:
        RAISING.clear()
    check("a degraded check whose probe raises is DEGRADED, not DOWN",
          rs["backlog"].status == "degraded" and "checker error" in rs["backlog"].detail,
          (rs["backlog"].status, rs["backlog"].detail))
    gate2 = threading.Event()
    real_ver = wd.check_hermes_version
    wd.check_hermes_version = lambda: (gate2.wait(5), (True, "late"))[1]
    try:
        rs = {r.key: r for r in wd.probe_all(budget_s=0.3)}
    finally:
        gate2.set()
        wd.check_hermes_version = real_ver
    check("...and one that does not finish is DEGRADED too",
          rs["hermesver"].status == "degraded" and "did not finish" in rs["hermesver"].detail,
          (rs["hermesver"].status, rs["hermesver"].detail))
    check("...while an up/down check still fails DOWN", all(
        r.status == "ok" for k, r in rs.items() if k != "hermesver"))

    print("--- stack_alert: the excerpt says why ---")
    ex = sa.excerpt_from_json("\n".join(OOM), GW, I_FAIL)
    check("oom-kill: the last line is systemd's verdict, not its bookkeeping",
          ex[-1:] == ["Failed with result 'oom-kill'."], ex)
    check("...no 'Consumed' or 'Triggering' line anywhere",
          not any("Consumed" in l or "Triggering" in l for l in ex), ex)
    check("...the unit-name prefix is dropped", not any(l.startswith(GW + ":") for l in ex), ex)
    check(f"...at most {sa.EXCERPT_LINES} lines", len(ex) == sa.EXCERPT_LINES, len(ex))
    ex = sa.excerpt_from_json("\n".join(DELIVERY), DL, I_D1)
    check("a later successful run is cut off at the failed invocation",
          ex[-1] == "Failed with result 'oom-kill'." and "nothing new" not in ex, ex)
    check("...and 'Failed to start X' (the headline again) is skipped",
          not any(l.startswith("Failed to start") for l in ex), ex)
    ex = sa.excerpt_from_json("\n".join(DELIVERY), DL, "")
    check("without an invocation id nothing is cut (degrades to a plain tail)",
          ex[-1].startswith("Finished"), ex)
    ex = sa.excerpt_from_json("\n".join(EXIT78), GW, I_78, drop_restated=True)
    check("exit-code: the service's own last words are last",
          ex[-1] == "hermes-gateway: refusing to start with an incomplete profile (exit 78)", ex)
    check("...systemd's verdict lines are left to the tiles",
          not any("Main process exited" in l or "Failed with result" in l for l in ex), ex)
    # The service printed nothing in the failed run (an earlier run's line is still in the tail):
    # dropping the verdict would make the text quote this run's "Started ..." line.
    silent = [J("an earlier run's line", "python", I_PREV), EXIT78[0]] + EXIT78[3:]
    ex = sa.excerpt_from_json("\n".join(silent), GW, I_78, drop_restated=True)
    check("exit-code from a silent service: the verdict stays last, not 'Started ...'",
          ex[-1] == "Failed with result 'exit-code'." and "Main process exited, code=exited, "
          "status=78/CONFIG" in ex, ex)
    ex = sa.excerpt_from_json("\n".join([J("Consumed 3 apples CPU time", "python"),
                                         json.dumps({"MESSAGE": [104, 105, 255],
                                                     "SYSLOG_IDENTIFIER": "python"}),
                                         "not json", J("line one\nline two", "python")]), GW)
    check("only systemd's own lines are skipped; binary and multi-line messages survive",
          ex == ["Consumed 3 apples CPU time", "hi�", "line one", "line two"], ex)

    print("--- stack_alert: sent through the engine ---")
    j = Journal(OOM)
    rc, sent = alert([GW], OOM_ENV, j)
    s = sent[0] if sent else {}
    check("exit 0, one send", rc == 0 and len(sent) == 1, (rc, len(sent)))
    check("journalctl read the unit's user journal as JSON",
          j.argv and j.argv[:4] == ["journalctl", "--user", "-u", GW] and "json" in j.argv, j.argv)
    check("no confirmation: text AND email at once", s.get("channels") == ["sms", "email"],
          s.get("channels"))
    check("the text is the last line: the reason",
          s.get("sms") == f"{GW} FAILED on {HOST}. Last log: Failed with result 'oom-kill'.",
          s.get("sms"))
    check("subject: [stack] <unit> FAILED on <host>",
          s.get("subject") == f"[stack] {GW} FAILED on {HOST}", s.get("subject"))
    plain = s.get("plain") or ""
    journal = (plain.split("Journal\n", 1)[1].split("\n\n", 1)[0].splitlines()
               if "Journal\n" in plain else [])
    check("the email carries the last 5 journal lines", len(journal) == 5 and
          journal[-1].strip() == "Failed with result 'oom-kill'.", journal)
    check("...systemd's verdict as tiles", "oom-kill" in (s.get("html") or "") and
          "KILL" in (s.get("html") or "") and "signal" in (s.get("html") or ""))
    check("...branded, never the job footer", (s.get("html") or "").startswith("<!doctype html>")
          and JOB_FOOTER not in plain)

    rc, sent = alert([GW], {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited",
                            "MONITOR_EXIT_STATUS": "78", "MONITOR_INVOCATION_ID": I_78},
                     Journal(EXIT78))
    check("exit 78: the text quotes the refusal",
          sent and sent[0]["sms"].endswith("refusing to start with an incomplete profile (exit 78)"),
          sent and sent[0]["sms"])
    check("...and the email tiles say exit status 78", sent and "exit status" in sent[0]["html"])

    url_line = J("fetch failed: https://api.example.com/v1/models returned 503", "python", I_78)
    rc, sent = alert([GW], {}, Journal(EXIT78[:3] + [url_line]))
    check("a link in the last line never reaches the text",
          sent and "http" not in sent[0]["sms"] and "example.com" not in sent[0]["sms"],
          sent and sent[0]["sms"])
    check("...but the email keeps it",
          sent and "https://api.example.com/v1/models" in sent[0]["plain"])
    check("without MONITOR_* variables systemd's verdict lines are kept",
          sa.excerpt_from_json("\n".join(EXIT78), GW, I_78)[-1] == "Failed with result 'exit-code'.")

    rc, sent = alert([], dict(OOM_ENV), Journal(OOM))
    check("no argument: the unit comes from $MONITOR_UNIT", sent and sent[0]["sms"].startswith(GW),
          sent and sent[0]["sms"])
    rc, sent = alert([GW], {}, Journal(raises=FileNotFoundError("journalctl")))
    check("journalctl missing: still sends, exit 0", rc == 0 and len(sent) == 1)
    check("...and says no lines were captured",
          sent and "(no journal lines captured)" in sent[0]["plain"])

    print("--- stack_alert: never raises, always exits 0 ---")

    def boom(*a, **k):
        raise OSError("smtp down")
    rc, _ = alert([GW], OOM_ENV, Journal(OOM), sender=boom)
    check("transport raising: exit 0", rc == 0)
    rc, sent = alert([GW], OOM_ENV, Journal(OOM), sender=Sender(ok=False))
    check("transport failing: exit 0", rc == 0 and len(sent) == 1)
    real_render = ha.render_unit_failures
    ha.render_unit_failures = boom
    try:
        rc, sent = alert([GW], OOM_ENV, Journal(OOM))
    finally:
        ha.render_unit_failures = real_render
    s = sent[0] if sent else {}
    check("renderer broken: a plain report still goes out, text and email",
          rc == 0 and len(sent) == 1 and s["channels"] == ["sms", "email"], sent)
    check("...its text names the unit and the reason",
          s.get("sms") == f"{GW} FAILED on {HOST}. Last log: Failed with result 'oom-kill'.",
          s.get("sms"))
    check("...its subject is the engine's", s.get("subject") == f"[stack] {GW} FAILED on {HOST}",
          s.get("subject"))
    check("...and its body is true for a unit failure: never the job footer",
          JOB_FOOTER not in s.get("plain", "") and "Sent by systemd (OnFailure=); the formatted "
          "report failed to render: OSError: smtp down" in s.get("plain", "")
          and f"journalctl --user -n 50 -u {GW}" in s.get("plain", ""), s.get("plain"))
    real_facts = sa.systemd_facts
    sa.systemd_facts = boom
    try:
        rc, sent = alert([GW], OOM_ENV, Journal(OOM))
    finally:
        sa.systemd_facts = real_facts
    check("an unexpected error anywhere: exit 0", rc == 0)

    print("--- stack_alert: a burst of unit failures is ONE alert ---")
    FC, DLV = "flightclaw.service", "hermes-delivery.service"
    followers = []

    def burst(seconds):
        # While the leader waits, the other two units fail (the 2026-09-24 14:42:55 burst).
        for u in (DLV, GW):
            followers.append(quietly(sa.run, [u], {"MONITOR_SERVICE_RESULT": "oom-kill"}))
    sa._sleep = burst
    try:
        rc, sent = alert([FC], {"MONITOR_SERVICE_RESULT": "oom-kill"}, Journal(OOM))
    finally:
        sa._sleep = lambda s: None
    check("three units within the window: ONE send", rc == 0 and len(sent) == 1, len(sent))
    check("...the followers only joined (exit 0, 'joined the open batch')",
          [f[0] for f in followers] == [0, 0] and all("joined the open batch" in f[1]
                                                     for f in followers), followers)
    check("...one grouped text naming all three",
          sent and sent[0]["sms"] == f"Stack: FAILED: {FC}, {DLV}, {GW}", sent and sent[0]["sms"])
    check("...one email with each unit's journal", sent and all(f"{u}:" in sent[0]["plain"]
                                                            for u in (FC, DLV, GW)))
    check("...and the batch is closed", not os.path.exists(sa.BATCH_FILE))
    with open(sa.BATCH_FILE, "w") as f:
        json.dump({"opened_at": time.time() - 3600, "leader": 1,
                   "pending": [{"unit": DLV, "lines": ["killed"], "facts": [], "at": T}]}, f)
    rc, sent = alert([GW], OOM_ENV, Journal(OOM))
    check("a batch whose leader died is adopted by the next failure, not lost",
          len(sent) == 1 and sent[0]["sms"] == f"Stack: FAILED: {DLV}, {GW}",
          sent and sent[0]["sms"])
    real_batch, sa.BATCH_FILE = sa.BATCH_FILE, "/dev/null/stack_alert_batch.json"
    try:
        rc, sent = alert([GW], OOM_ENV, Journal(OOM))
    finally:
        sa.BATCH_FILE = real_batch
    check("batch file unusable: the failure is still sent, alone",
          rc == 0 and len(sent) == 1 and sent[0]["sms"].startswith(f"{GW} FAILED"),
          sent and sent[0]["sms"])

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

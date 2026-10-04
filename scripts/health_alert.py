#!/usr/bin/env python3
"""Confirm-before-alert engine and renderer for the stack's own health alerts.

Why this exists. The infra monitors (search_canary, stack_watchdog, stack_alert) handed a bare
sentence to alert_transports.send_alert, which is built for background JOBS: the email was plain
text ending "You are receiving this because a background task you scheduled met its alert
condition", which is false for a health check nobody scheduled. And they alerted on the FIRST
failed probe. Measured from its journal, 2026-09-21..29: the search canary sent 22 alerts, each an
SMS and an email, for 11 outages of which 8 were a single failed probe already healthy at the next
run 30 minutes later. A channel that cries wolf that often gets muted, and then the real outage is
silent too. Replaying that history under the policy below gives 8 notifications for 4 outages
(tests/test_health_alert.py).

Policy (approved by the user), per check:

  * CONFIRM   non-ok for `confirm_after` runs (default 2) before DOWN / DEGRADED, where only
              `recover_after` ok runs in a row (default 2) end a streak: down,ok,down confirms.
              A caller can also retry() inside a run, so one run is itself several probes.
  * RECOVERED only when an alert was actually sent for that outage, after `recover_after` ok runs
              in a row, with the outage duration. An unconfirmed blip resets silently; a
              "recovered" for an outage the user never heard about is noise. Requiring two ok runs
              is flap damping (evaluate()): it costs every recovery one run of delay, and it adds
              one outage to the replay above (2026-09-23 11:18 DOWN, 11:50 OK, 12:22 DOWN), which
              a single ok run used to erase.
  * FRESH     a reboot, or a gap longer than the caller's max_gap_s, restarts unalerted streaks
              (start_run()): a failure before a shutdown plus one while services start after boot
              is not an outage.
  * KILLED    runs systemd kills at TimeoutStartSec leave a marker, and two in a row send
              "<monitor> runs DOWN" at once (begin()). On 2026-09-24, 21 killed watchdog runs
              pushed the first DOWN to 14:42; replayed with the markers, the first text goes out
              at 10:30.
  * REMINDER  still non-ok `remind_after_s` (default 24h) after the alert, then at most once per
              window. A week-long outage is one alert plus a daily nudge, not silence and not 336.
  * ESCALATE  alerted as degraded and now down: send DOWN. Down easing to degraded is still
              broken, so it does not re-alert.
  * One run's events go out as ONE notification: a 3am incident is one buzz, not four.
  * Severity routing: anything DOWN-related (a DOWN alert, recovery from one, a reminder of one,
              a failed unit) texts AND emails; DEGRADED-only goes by email. Degraded means "still
              answering, worse than usual", which can wait until morning.

Alert bookkeeping is committed only after delivery succeeds (mark_delivered): a send that fails
leaves the check unalerted, so the next run tries again instead of the outage going silent. State
that is missing, corrupt or in either old format never crashes and never suppresses a genuine
alert forever (see migrate()). State that cannot be written goes to a tmpfs copy (save_state()),
a run holds an exclusive lock for its whole cycle (exclusive()), and a clock that steps back
cannot rewrite stored times (_clock()).

A monitor's real run is: exclusive() -> begin() -> probe -> run() (or, for per-check policies,
evaluate() per group + finish_run() + deliver(), as stack_watchdog.notify does).

The email is the backup report's template (ohmz_email), masthead "Ohmz Stack", with a footer
that says what actually happened and what will happen next. Rendering is guarded at every layer:
a broken HTML renderer degrades to plain text, never to a lost alert. Delivery outranks
presentation.

Samples:  python3 scripts/health_alert.py --sample all --out /tmp/samples
"""
import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alert_transports as _at  # noqa: E402  (stdlib-only; referenced at call time so tests can patch)

STATUSES = ("ok", "degraded", "down")
RANK = {"ok": 0, "degraded": 1, "down": 2}
KINDS = ("down", "degraded", "recovered", "reminder", "failed")
WORD = {"down": "DOWN", "degraded": "DEGRADED", "recovered": "RECOVERED",
        "reminder": "REMINDER", "failed": "FAILED", "ok": "OK"}
CONFIRM_AFTER = 2
RECOVER_AFTER = 2
REMIND_AFTER_S = 86400
# A run whose clock reads more than this before the last run's is taken to have the wrong clock.
CLOCK_SLACK_S = 300
# ...unless this many runs in a row agree, in which case it was the stored stamp that was ahead.
CLOCK_TRUST_AFTER = 3
# Runs systemd killed before their final save are observations of a synthetic check, RUNS_KEY:
# each killed run is a failure (seen by the next run's begin()), each finished run a success
# (finish_run()). This many killed runs confirm "<monitor> runs DOWN"; RECOVER_AFTER finished
# runs in a row recover it, so a thrashing host that finishes one run in three does not flap.
KILLED_ALERT_AFTER = 2
RUNS_KEY = "_runs"
# Where save_state writes when the state file itself cannot be written; None means the user's
# runtime directory (tmpfs). Tests point it at a temp dir.
FALLBACK_DIR = None
FOOTNOTE = "Ohmz Cloud · automated health check"
SUBJECT_PREFIX = "[stack] "   # kept from the old subjects so existing mail filters still match
STATE_VERSION = 2


# --------------------------------------------------------------------------- data

@dataclass
class Check:
    """One probe verdict. `key` is the stable id state is filed under; `label` is what a human
    reads. facts are ordered (value, label) stat tiles; items are (name, ok, note) rows such as
    per-engine results; where is subtitle text (an endpoint). summary is an optional SHORT
    measurement ("0 results") used in the SMS and subject so it lands inside the first ~45
    characters a phone shows; it falls back to detail. attempts is set by retry()."""
    key: str
    label: str
    status: str
    detail: str = ""
    facts: list = field(default_factory=list)
    items: list = field(default_factory=list)
    where: str = ""
    summary: str = ""
    attempts: int = 1

    def __post_init__(self):
        s = str(self.status or "").strip().lower()
        # A probe that returns something unexpected is a failed probe. Fail loud, not quiet.
        self.status = s if s in STATUSES else "down"
        self.attempts = max(1, int(self.attempts or 1))

    @property
    def headline(self):
        # One line, always: this reaches the email Subject, where a bare LF makes EmailMessage raise
        # (a probe detail can carry one: stack_watchdog._why() of a BadStatusLine ends in "\n").
        return " ".join(str(self.summary or self.detail or "").split())


@dataclass
class Event:
    """Something worth telling the user, snapshotted at the moment it was decided. kind is one of
    KINDS. was is the status last alerted as (for recovered / reminder / escalation). Times are
    epoch seconds; at is the run's `now`."""
    kind: str
    check: Check
    at: float
    was: str = None
    first_bad_at: float = None
    confirmed_at: float = None
    alerted_at: float = None
    escalated_at: float = None
    recovered_at: float = None
    fail_streak: int = 0
    failed_probes: int = 0
    escalated: bool = False
    migrated: bool = False
    confirm_after: int = CONFIRM_AFTER

    @property
    def wants_sms(self):
        if self.kind in ("down", "failed"):
            return True
        if self.kind == "recovered":
            return self.was == "down"
        if self.kind == "reminder":
            return self.check.status == "down"
        return False


@dataclass
class Notification:
    """Everything one send needs. html is None when rendering it failed (html_error says why)."""
    sms: str
    subject: str
    plain: str
    html: str
    channels: list
    events: list
    html_error: str = ""


@dataclass
class RunResult:
    """saved: True once the state reached disk (or the tmpfs fallback), False if neither write
    worked, None when nothing was saved on purpose (a dry run)."""
    events: list
    notification: Notification = None
    sent: bool = False
    notes: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    saved: bool = None


# --------------------------------------------------------------------------- probing

def retry(probe_fn, attempts=3, delay_s=60, sleep=time.sleep):
    """Call probe_fn() (-> Check) up to `attempts` times, sleeping delay_s between tries. Returns
    the first ok result (attempts = tries it took), else the WORST failure with attempts = all
    tries (the latest among equally bad ones, for the freshest detail). A raising try counts as a
    failed try; if no try ever returned a Check, the last exception propagates, since retry cannot
    invent a key or label for it.

    Worst, not last: with down, down, degraded (0 results twice, then results with three engines
    out) the last try would route the run as DEGRADED, email only, although search was off for
    two of its three probes. Which try happened to come last must not decide whether it texts.

    In-run retry is the cheap half of confirmation: a single-probe Bing timeout, the canary's
    commonest false alarm, is usually gone a minute later."""
    n = max(1, int(attempts))
    worst, err = None, None
    for i in range(1, n + 1):
        try:
            r = probe_fn()
        except Exception as e:
            err = e
        else:
            if r.status == "ok":
                return dataclasses.replace(r, attempts=i)
            if worst is None or RANK[r.status] >= RANK[worst.status]:
                worst = r
        if i < n:
            sleep(delay_s)
    if worst is None:
        raise err
    return dataclasses.replace(worst, attempts=n)


# --------------------------------------------------------------------------- state

_TIMES = ("first_bad_at", "confirmed_at", "alerted_at", "escalated_at", "last_reminder_at",
          "recovered_at", "last_seen_at")


def _fresh_entry():
    return {"status": "ok", "fail_streak": 0, "ok_streak": 0, "failed_probes": 0,
            "first_bad_at": None, "confirmed_at": None, "alerted": False, "alerted_status": None,
            "alerted_at": None, "escalated_at": None, "last_reminder_at": None,
            "recovered_at": None, "last_seen_at": None, "last_detail": "", "label": "",
            "migrated": False}


def _fresh_run():
    """Per-monitor run bookkeeping, as opposed to per-check. inflight_at is set when a run starts
    and cleared by its final save, so a run systemd killed is visible to the next one;
    last_attempt_at and boot_id let start_run() tell a gap or a reboot from consecutive runs;
    last_run_at and clock_behind_runs are _clock()'s."""
    return {"inflight_at": None, "killed_streak": 0, "last_attempt_at": None, "boot_id": "",
            "last_run_at": None, "clock_behind_runs": 0}


def fresh_state(source=""):
    return {"version": STATE_VERSION, "source": source, "checks": {}, "run": _fresh_run()}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _count(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def _clean_run(r):
    out = _fresh_run()
    if not isinstance(r, dict):
        return out
    for k in ("inflight_at", "last_attempt_at", "last_run_at"):
        out[k] = _num(r.get(k))
    for k in ("killed_streak", "clock_behind_runs"):
        out[k] = _count(r.get(k))
    out["boot_id"] = str(r.get("boot_id") or "")
    return out


def _clean_entry(e):
    """Coerce one stored entry to the current shape, field by field. Anything unreadable falls
    back to the fresh default for that field, which errs toward alerting: an unknown status reads
    as ok, so real failures start a new confirmation rather than being swallowed."""
    if not isinstance(e, dict):
        return None
    out = _fresh_entry()
    st = e.get("status")
    out["status"] = st if st in STATUSES else "ok"
    for k in ("fail_streak", "ok_streak", "failed_probes"):
        out[k] = _count(e.get(k))
    for k in _TIMES:
        out[k] = _num(e.get(k))
    out["alerted"] = e.get("alerted") is True
    a = e.get("alerted_status")
    out["alerted_status"] = a if a in ("degraded", "down") else None
    if out["alerted"] and not out["alerted_status"]:
        out["alerted_status"] = out["status"] if out["status"] != "ok" else "down"
    out["last_detail"] = str(e.get("last_detail") or "")
    out["label"] = str(e.get("label") or "")
    out["migrated"] = e.get("migrated") is True
    return out


def _from_legacy(status, detail, at):
    """One entry from either old shape. The old code alerted on the FIRST failure, so an old
    non-ok entry was already alerted: marking it so is what lets the recovery close the loop
    instead of the outage ending in silence. first_bad_at is only "by" the old `at` (that field
    was the last run, not the first failure), hence migrated=True for the renderer."""
    e = _fresh_entry()
    s = str(status or "").lower()
    e["last_detail"] = str(detail or "")
    if s == "ok":
        return e
    s = s if s in STATUSES else "down"
    at = _num(at)
    e.update(status=s, fail_streak=1, failed_probes=1, first_bad_at=at, confirmed_at=at,
             alerted=True, alerted_status=s, alerted_at=at, migrated=True)
    return e


def migrate(raw, source="", legacy_key=None):
    """Any stored shape -> current state. Never raises.

      current   {"version": 2, "checks": {key: entry}}      entries re-validated
      canary    {"status": "down", "detail": ..., "at": ...}  filed under legacy_key, or held as
                                                            "legacy" until a single-check run
                                                            adopts it (see evaluate)
      watchdog  {key: {"ok": bool, "detail": ..., "at": ...}}
      garbage   anything else                               fresh state
    """
    st = fresh_state(source)
    if not isinstance(raw, dict):
        return st
    if isinstance(raw.get("checks"), dict):
        for k, e in raw["checks"].items():
            ent = _clean_entry(e)
            if ent is not None:
                st["checks"][str(k)] = ent
        if isinstance(raw.get("legacy"), dict):
            st["legacy"] = _clean_entry(raw["legacy"])
        st["run"] = _clean_run(raw.get("run"))
        if _num(raw.get("saved_at")) is not None:
            st["saved_at"] = _num(raw.get("saved_at"))
        return st
    if isinstance(raw.get("status"), str):
        ent = _from_legacy(raw.get("status"), raw.get("detail"), raw.get("at"))
        if legacy_key:
            st["checks"][legacy_key] = ent
        else:
            st["legacy"] = ent
        return st
    for k, v in raw.items():
        if isinstance(v, dict) and "ok" in v:
            st["checks"][str(k)] = _from_legacy("ok" if v.get("ok") is True else "down",
                                                v.get("detail"), v.get("at"))
    return st


def fallback_path(path):
    """Where save_state puts `path`'s state when `path` cannot be written: the runtime directory,
    a tmpfs (measured: /run/user/1000 had 9.5G free while / was at 92%). The name carries a hash
    of the full path, so a test's temp state can never shadow a live monitor's."""
    d = FALLBACK_DIR or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    h = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:10]
    return os.path.join(d, f"health_alert.{h}.{os.path.basename(path)}")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_state(path, source="", legacy_key=None):
    """Read and migrate `path`, or its tmpfs fallback copy if that was saved later (by saved_at).
    Missing or unreadable -> fresh state. Never raises."""
    best = None
    for raw in (_read_json(path), _read_json(fallback_path(path))):
        if raw is None:
            continue
        stamp = _num(raw.get("saved_at")) if isinstance(raw, dict) else None
        if best is None or (stamp is not None and (best[0] is None or stamp > best[0])):
            best = (stamp, raw)
    if best is None:
        return fresh_state(source)
    return migrate(best[1], source, legacy_key)


def _write_json(path, state):
    """Atomic write (tmp in the same directory + os.replace). -> (ok, error)."""
    tmp = None
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".health-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True, ""
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False, f"{type(e).__name__}: {e}"


def save_state(path, state, now=None):
    """Save `state` to `path`, else to fallback_path(path). -> (saved, note): note is "" for a
    clean save, says where it went when the fallback was used, and says why when neither write
    worked. Never raises: a full disk must not turn an alert that was already sent into a crashed
    run.

    Why a fallback. Confirmation and delivery bookkeeping exist only in this file. If it cannot be
    written, every run reloads the same old state: a check needing two runs never alerts (the
    canary exited 0, silently, through six DOWN runs with a read-only state directory), a check
    needing one alerts every run, and a delivered recovery is re-sent every run because its
    clearing is never saved. / was at 92% on a host that pulls model weights, so ENOSPC is not
    hypothetical. saved_at is stamped on every save so load_state can pick the newer copy."""
    state["saved_at"] = time.time() if now is None else float(now)
    ok, err = _write_json(path, state)
    fb = fallback_path(path)
    if ok:
        # A stale fallback could otherwise win a later load on saved_at if the clock steps back.
        with contextlib.suppress(Exception):
            if os.path.exists(fb):
                os.unlink(fb)
        return True, ""
    ok2, err2 = _write_json(fb, state)
    if ok2:
        return True, f"saved to {fb} instead: {err}"
    return False, f"{err}; fallback {fb}: {err2}"


@contextlib.contextmanager
def exclusive(state_path):
    """Hold an exclusive, non-blocking flock on `state_path`.lock for a whole real run (load,
    probe, send, save). Yields True if held, False if another run holds it (the caller should log
    and exit 0), None if no lock file could be opened (proceed unlocked: a lock must never be the
    reason nothing is monitored).

    Why: two runs that overlap both load the same state, both send, and the last save wins. With a
    one-second sender, two overlapping runs texted "chat DOWN" twice, and in another order a DOWN
    was delivered but its alerted flag overwritten, so its recovery would never be announced.
    systemd never overlaps a oneshot with itself; a manual run beside the timer's does, and a
    failing canary run lasts ~3 minutes, which is exactly when someone re-runs it by hand. flock
    is released by the kernel when the process dies, so a killed run cannot leave it stuck."""
    try:
        fd = os.open(f"{state_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield None
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def current_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- policy

def _snapshot(kind, r, e, now, escalated=False, confirm_after=CONFIRM_AFTER):
    return Event(kind=kind, check=r, at=now, was=e["alerted_status"],
                 first_bad_at=e["first_bad_at"], confirmed_at=e["confirmed_at"],
                 alerted_at=e["alerted_at"], escalated_at=e["escalated_at"],
                 recovered_at=e["recovered_at"], fail_streak=e["fail_streak"],
                 failed_probes=e["failed_probes"], escalated=escalated,
                 migrated=e["migrated"], confirm_after=confirm_after)


def _forget(e):
    """Drop an unalerted streak. The user never heard of it, so it ends without a word."""
    e.update(status="ok", fail_streak=0, failed_probes=0, first_bad_at=None, confirmed_at=None,
             recovered_at=None, migrated=False)


def _clock(state, now):
    """-> (the time this run's decisions use, whether this run's clock is trusted).

    evaluate() used to clamp every stored timestamp later than `now` down to `now`, and save it.
    So one run with a clock that is behind (a bad RTC at boot, before NTP syncs) rewrote alerted_at
    and first_bad_at with the bad time, and the next correctly-clocked run texted a REMINDER
    reading "still DOWN after 270d" half an hour after the DOWN. Now the last run's time is kept:
    a run whose clock reads more than CLOCK_SLACK_S before it is taken to be wrong, and decides
    at the last trusted time instead (no clamping, no reminder, stamps that stay in order). Only
    if CLOCK_TRUST_AFTER runs in a row agree is it the stored stamp that was ahead (an RTC kept
    in local time reads hours fast until NTP corrects it); then the new clock is adopted and
    future-dated values are clamped, as before. Idempotent within one run (same raw `now`)."""
    run = state.setdefault("run", _fresh_run())
    if run.get("clock_raw") == now and run.get("clock_now") is not None:
        return run["clock_now"], run["clock_now"] == now
    last = _num(run.get("last_run_at"))
    eff = now
    if last is not None and now < last - CLOCK_SLACK_S:
        run["clock_behind_runs"] = _count(run.get("clock_behind_runs")) + 1
        if run["clock_behind_runs"] < CLOCK_TRUST_AFTER:
            eff = last
        else:
            run.update(clock_behind_runs=0, last_run_at=now)
    else:
        run.update(clock_behind_runs=0, last_run_at=now if last is None else max(last, now))
    run["clock_raw"], run["clock_now"] = now, eff
    return eff, eff == now


def evaluate(state, results, now=None, confirm_after=CONFIRM_AFTER,
             remind_after_s=REMIND_AFTER_S, recover_after=RECOVER_AFTER, max_gap_s=None,
             adopt_legacy=True):
    """Fold this run's Check results into `state` (mutated) and return the Events it owes.

    Only OBSERVATIONS are written here (status, streaks, first failure). Whether an alert went out
    is written by mark_delivered() after a successful send, so a failed send is retried by the
    next run rather than recorded as delivered. Deterministic given `now`.

    A streak is ended only by `recover_after` consecutive ok runs, in both directions:
      * an alerted check is RECOVERED on its recover_after-th ok run in a row (recovered_at is
        still its first ok run, so the outage length is right). With recovery on a single ok run,
        a check flapping down,down,ok texted DOWN and RECOVERED every three runs: 96 texts and
        emails in 12 hours on the watchdog's timer. Now: the DOWN, then daily reminders.
      * an unalerted streak survives a lone ok run, so down,ok,down confirms on the second down.
        A single ok run used to reset it, and an instance failing every other run never alerted
        at all (0 notifications for 24 failed runs in 24 hours); the canary's history has that
        shape (2026-09-23 11:18 DOWN, 11:50 OK, 12:22 DOWN), and so did the gateway during the
        2026-09-24 incident (11:40 FAIL, 12:34 OK, 13:35 FAIL).
    max_gap_s: an unalerted streak whose last observation is older than this is restarted, so a
    failure days ago plus one now is not two consecutive runs. start_run() refreshes last_seen_at
    for every check at each run's start, so runs systemd killed do not count as gaps."""
    now, trusted = _clock(state, time.time() if now is None else float(now))
    confirm_after = max(1, int(confirm_after))
    recover_after = max(1, int(recover_after))
    results = list(results)
    checks = state.setdefault("checks", {})
    if adopt_legacy:
        legacy = state.pop("legacy", None)
        if legacy and len(results) == 1 and results[0].key not in checks:
            checks[results[0].key] = legacy
    events = []
    for r in results:
        e = _clean_entry(checks.get(r.key)) or _fresh_entry()
        e["label"] = r.label
        if trusted:
            # A clock that jumped backwards must not park reminders in the future forever.
            for k in _TIMES:
                if e[k] is not None and e[k] > now:
                    e[k] = now
        if e["alerted"] and e["alerted_at"] is None:
            e["alerted_at"] = now
        if (max_gap_s and not e["alerted"] and e["last_seen_at"] is not None
                and now - e["last_seen_at"] > max_gap_s):
            _forget(e)
        e["last_seen_at"] = now

        if r.status == "ok":
            e["ok_streak"] = min(e["ok_streak"] + 1, recover_after)   # "at least", not a counter
            if e["alerted"]:
                if e["recovered_at"] is None:
                    e["recovered_at"] = now
                if e["ok_streak"] >= recover_after:
                    events.append(_snapshot("recovered", r, e, now, confirm_after=confirm_after))
                    e["status"], e["fail_streak"] = "ok", 0
            elif e["status"] == "ok" or e["ok_streak"] >= recover_after:
                _forget(e)
        else:
            e["ok_streak"] = 0
            if not e["alerted"] and e["status"] == "ok":
                e.update(first_bad_at=now, confirmed_at=None, fail_streak=0, failed_probes=0)
            # A relapse before a recovery message got out continues the same outage.
            e["recovered_at"] = None
            e["fail_streak"] += 1
            e["failed_probes"] += r.attempts
            if e["first_bad_at"] is None:
                e["first_bad_at"] = now
            e["status"] = r.status
            if not e["alerted"]:
                if e["fail_streak"] >= confirm_after:
                    if e["confirmed_at"] is None:
                        e["confirmed_at"] = now
                    events.append(_snapshot(r.status, r, e, now, confirm_after=confirm_after))
            elif r.status == "down" and e["alerted_status"] == "degraded":
                events.append(_snapshot("down", r, e, now, escalated=True,
                                        confirm_after=confirm_after))
            elif trusted:
                base = max(t for t in (e["alerted_at"], e["last_reminder_at"], e["escalated_at"])
                           if t is not None)
                if now - base >= remind_after_s:
                    events.append(_snapshot("reminder", r, e, now, confirm_after=confirm_after))
        e["last_detail"] = r.detail
        checks[r.key] = e
    return events


def start_run(state, now=None, max_gap_s=None, boot_id=""):
    """The start of a real run, before any probing. Mutates `state`; the caller saves it at once
    (begin() does). -> (killed_streak, note), note saying why unalerted streaks were restarted.

      * inflight_at still set means the previous run died before its final save. On 2026-09-24
        systemd killed 21 watchdog runs between 10:19 and 14:02 at TimeoutStartSec (the host was
        thrashing; at 13:43 even SIGTERM timed out). A killed run never reached evaluate(), so the
        engine could not tell it from no run at all, and confirmation waited for runs that
        finished: the first DOWN would have gone out at 14:42, 3 hours after the old code's 11:40
        text. Counting killed runs is what lets begin() say so after two of them.
      * A reboot (boot_id changed), or more than max_gap_s since the previous run STARTED, killed
        or not, restarts every unalerted streak: a failure logged just before a shutdown plus one
        from the first run after boot, while services are still starting, is not an outage. The
        canary's history has one: 2026-09-24 14:43 (0 results) and 16:07 (unreachable, 10 min
        after boot), 84 minutes apart with two reboots between, confirmed a DOWN. A run cut short
        by the reboot is not counted as killed.
      * Every check's last_seen_at becomes now, so evaluate()'s own max_gap_s test measures from
        this start: killed runs keep the chain unbroken, the gaps above break it."""
    now = time.time() if now is None else float(now)
    run = state.setdefault("run", _fresh_run())
    prev = _num(run.get("last_attempt_at"))
    rebooted = bool(boot_id and run.get("boot_id") and boot_id != run.get("boot_id"))
    died = run.get("inflight_at") is not None and not rebooted
    run["killed_streak"] = _count(run.get("killed_streak")) + 1 if died else 0
    gap = now - prev if prev is not None else 0.0
    why = "rebooted since the last run" if rebooted else (
        f"{human(gap)} since the last run started" if max_gap_s and gap > max_gap_s else "")
    note, dropped = "", []
    checks = state.setdefault("checks", {})
    for key, e in checks.items():
        if not isinstance(e, dict):
            continue
        if why and not e.get("alerted") and _count(e.get("fail_streak")):
            _forget(e)
            dropped.append(key)
        # max(): a run whose clock is behind (see _clock) must not stamp a time that makes the
        # next correct run see a months-long gap.
        e["last_seen_at"] = max(_num(e.get("last_seen_at")) or now, now)
    if dropped:
        note = f"{why}: restarted the unalerted streak of {', '.join(dropped)}"
    run.update(inflight_at=now, last_attempt_at=max(prev or now, now),
               boot_id=boot_id or run.get("boot_id") or "")
    return run["killed_streak"], note


def finish_run(state, now=None, where=""):
    """The end of a run that reached its final save: clear the in-flight marker and record a
    finished run for RUNS_KEY, which after RECOVER_AFTER in a row owes the recovery of a "runs
    are being killed" alert. -> events."""
    run = state.setdefault("run", _fresh_run())
    killed = _count(run.get("killed_streak"))
    run.update(inflight_at=None, killed_streak=0)
    e = state.get("checks", {}).get(RUNS_KEY)
    if not isinstance(e, dict):
        return []
    detail = (f"A run finished after {killed} in a row were killed." if killed
              else "Runs are finishing again.")
    chk = Check(key=RUNS_KEY, label=e.get("label") or "Monitor runs", status="ok", where=where,
                detail=detail, summary="runs finishing")
    return evaluate(state, [chk], now, confirm_after=KILLED_ALERT_AFTER,
                    recover_after=RECOVER_AFTER, adopt_legacy=False)


def mark_delivered(state, events, now=None):
    """Record that `events` reached the user. Call only after a successful send."""
    checks = state.setdefault("checks", {})
    for ev in events:
        e = checks.get(ev.check.key)
        if e is None or ev.kind == "failed":
            continue
        t = ev.at if now is None else float(now)
        if ev.kind in ("down", "degraded"):
            if ev.escalated:
                e.update(alerted_status="down", escalated_at=t)
            else:
                e.update(alerted=True, alerted_status=ev.kind, alerted_at=t,
                         last_reminder_at=None, escalated_at=None)
        elif ev.kind == "reminder":
            e["last_reminder_at"] = t
        elif ev.kind == "recovered":
            keep = {k: e.get(k) for k in ("label", "last_detail", "status", "last_seen_at")}
            e.clear()
            e.update(_fresh_entry(), **keep)


def channels_for(events):
    """["sms", "email"] if any event is DOWN-related, else ["email"]."""
    return ["sms", "email"] if any(ev.wants_sms for ev in events) else ["email"]


def available_channels(handle="ohmz"):
    """The channels that can deliver to `handle` now, in ("sms", "email") order: enabled in
    ALERT_CHANNELS and with an address to go to. None if that cannot be read (no transport file,
    an error), which leaves the routing as it is. Reads config only; sends nothing."""
    try:
        conf = _at.load_conf()
        if not conf:
            return None
        enabled = [c.strip() for c in conf.get("ALERT_CHANNELS", "sms,email").split(",")
                   if c.strip()]
        email, phone = _at.resolve(handle, conf)
        return [c for c in ("sms", "email") if c in enabled and (phone if c == "sms" else email)]
    except Exception:
        return None


def _deliverable(route, available):
    """route narrowed to what can deliver. If none of it can, whatever can (a DEGRADED email with
    email switched off goes by text rather than nowhere); if nothing can, the route unchanged, so
    the send fails loudly instead of the notification quietly shrinking to nothing."""
    if available is None:
        return list(route)
    eff = [c for c in route if c in available]
    return eff or [c for c in ("sms", "email") if c in available] or list(route)


def _medium(chans):
    return {("sms",): "text", ("email",): "email"}.get(tuple(chans), "text and email")


# --------------------------------------------------------------------------- words

def human(sec):
    """Prose duration: '45 seconds', '32 minutes', '3 hours 12 minutes', '2 days 4 hours'."""
    sec = max(0, int(sec or 0))

    def u(n, w):
        return f"{n} {w}{'' if n == 1 else 's'}"
    if sec < 60:
        return u(sec, "second")
    m = sec // 60
    if m < 60:
        return u(m, "minute")
    h, m = divmod(m, 60)
    if h < 24:
        return u(h, "hour") + (f" {u(m, 'minute')}" if m else "")
    d, h = divmod(h, 24)
    return u(d, "day") + (f" {u(h, 'hour')}" if h else "")


def _window(sec):
    """A reminder window in words: '24 hours' reads better than '1 day' for the default."""
    sec = int(sec)
    return f"{sec // 3600} hours" if sec % 3600 == 0 and 3600 < sec <= 172800 else human(sec)


def _dur(sec):
    """Tile and SMS duration, to the minute: 32m, 3h12m, 1d4h. Floored, so a tile never says
    more than the prose next to it ("32 minutes"); seconds are noise on an outage clock."""
    sec = max(0, int(sec or 0))
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def _when(ts):
    if ts is None:
        return "unknown"
    return time.strftime("%a %b %d %H:%M %Z", time.localtime(ts))


def _stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _headline_kind(events):
    """The pill: the worst thing in the notification."""
    kinds = {ev.kind for ev in events}
    for k in ("down", "failed", "degraded", "reminder", "recovered"):
        if k in kinds:
            return k
    return "down"


def _title(events):
    keys = []
    for ev in events:
        if ev.check.key not in keys:
            keys.append(ev.check.key)
    if len(keys) == 1:
        return events[0].check.label
    return f"{len(keys)} units" if all(ev.kind == "failed" for ev in events) \
        else f"{len(keys)} checks"


def _where(events):
    ws = {ev.check.where for ev in events}
    return ws.pop() if len(ws) == 1 else ""


def _one_line(ev, host):
    c = ev.check
    tail = f": {c.headline}" if c.headline else ""
    if ev.kind == "failed":
        return f"{c.label} FAILED on {host}. Last log: {c.summary}" if c.summary \
            else f"{c.label} FAILED on {host}."
    if ev.kind == "recovered":
        lasted = (ev.recovered_at or ev.at) - (ev.first_bad_at or ev.at)
        return f"{c.label} RECOVERED after {_dur(lasted)}{tail}"
    if ev.kind == "reminder":
        return f"{c.label} still {WORD[c.status]} after {_dur(ev.at - (ev.first_bad_at or ev.at))}{tail}"
    if ev.escalated:
        return f"{c.label} now DOWN (was degraded){tail}"
    return f"{c.label} {WORD[ev.kind]}{tail}"


def _grouped_line(events):
    """'DOWN: Hermes gateway, Hermes API; recovered: Backup freshness', worst first."""
    groups = {}
    for ev in events:
        k = f"still {ev.check.status}" if ev.kind == "reminder" else \
            ("recovered" if ev.kind == "recovered" else WORD[ev.kind])
        labels = groups.setdefault(k, [])
        if ev.check.label not in labels:
            labels.append(ev.check.label)
    order = ["DOWN", "FAILED", "DEGRADED", "still down", "still degraded", "recovered"]
    return "; ".join(f"{k}: {', '.join(groups[k])}" for k in order if k in groups)


def render_sms(events, host=None):
    """One ASCII segment, <=140 chars, no web addresses (alert_transports.sms_body enforces it:
    carrier gateways silently drop link-bearing texts). Label and verdict lead, so truncation
    only ever eats the tail of the measurement."""
    host = host or socket.gethostname()
    if len(events) == 1:
        msg = _one_line(events[0], host)
    else:
        msg = f"Stack: {_grouped_line(events)}"
    return _at.sms_body(msg)


def render_subject(events, host=None):
    """Monitor and measurement inside the first ~45 characters, the same shape as the SMS so the
    two read as one event (alert_transports.alert_subject's rationale)."""
    host = host or socket.gethostname()
    if len(events) == 1 and events[0].kind == "failed":
        # The log line is in the SMS and the body; a subject that long only truncates.
        head = f"{events[0].check.label} FAILED on {host}"
    elif len(events) == 1:
        head = _one_line(events[0], host)
    else:
        head = _grouped_line(events)
    return SUBJECT_PREFIX + _at.alert_subject(head)


def _promise(ev, remind_after_s, available=None):
    """What happens next, stated only as far as the policy guarantees it, and in the medium that
    can actually carry it: with ALERT_CHANNELS=email, or no phone on file, a DOWN email must not
    promise a text that will never be sent. {it} becomes "each" when one sentence covers several
    checks."""
    route = ["sms", "email"] if ev.check.status == "down" else ["email"]
    medium = _medium(_deliverable(route, available))
    return (f"You'll get one more {medium} when {{it}} recovers, and a reminder if it is still "
            f"{ev.check.status} in {_window(remind_after_s)}.")


def _footer_sentences(ev, remind_after_s, host, available=None):
    c = ev.check
    bad_for = ev.at - (ev.first_bad_at or ev.at)
    # A migrated entry only knows the old file's last-run stamp, which is no earlier than the
    # real first failure or alert, so it is reported as a bound rather than as the moment.
    since = "at least " if ev.migrated else ""
    by = "by " if ev.migrated else ""
    if ev.kind == "failed":
        return [f"Sent by systemd (OnFailure=) each time {c.label} enters the failed state on "
                f"{host}. Units send no recovery message: check it with "
                f"systemctl --user status {c.label} once fixed."]
    if ev.kind == "recovered":
        lasted = (ev.recovered_at or ev.at) - (ev.first_bad_at or ev.at)
        # An escalation keeps alerted_at (the degraded alert's time) and adds escalated_at; the
        # down alert went out at the latter, which is what the Timeline's "Went down" row says.
        closes = (f"This closes the down alert sent {_when(ev.escalated_at)} (first alerted as "
                  f"degraded {by}{_when(ev.alerted_at)})." if ev.escalated_at else
                  f"This closes the {ev.was or 'earlier'} alert sent {by}{_when(ev.alerted_at)}.")
        return [f"{closes} The outage lasted {since}{human(lasted)}. Nothing more unless it fails "
                f"again."]
    if ev.kind == "reminder":
        return [f"Still {c.status} after {since}{human(bad_for)}; first alerted "
                f"{by}{_when(ev.alerted_at)}. You'll be reminded again in {_window(remind_after_s)} "
                f"if it stays broken, and told once when it recovers."]
    if ev.escalated:
        return [f"First alerted as degraded {_when(ev.alerted_at)}; it has since gone down.",
                _promise(ev, remind_after_s, available)]
    probes = f"{ev.failed_probes} failed probe{'' if ev.failed_probes == 1 else 's'}"
    runs = (f" across {ev.fail_streak} runs" if ev.fail_streak > 1
            and ev.failed_probes != ev.fail_streak else "")
    span = f"over {human(bad_for)}" if bad_for >= 1 else "in a single run"
    return [f"Sent after {probes}{runs} {span}.", _promise(ev, remind_after_s, available)]


def _footer_lines(events, remind_after_s, host, inspect, available=None):
    """Truthful closing lines. With several checks, a sentence that is true of more than one is
    said once with all their labels, so a two-check incident does not read as two incidents."""
    if len(events) > 1 and all(ev.kind == "failed" for ev in events):
        units = [ev.check.label for ev in events]
        span = max(ev.at for ev in events) - min(ev.at for ev in events)
        within = "a second" if span < 1 else human(span)
        lines = [f"Sent by systemd (OnFailure=): {len(units)} units entered the failed state on "
                 f"{host} within {within} of each other, so they come as one message. Units "
                 f"send no recovery message: check each with systemctl --user status <unit> once "
                 f"fixed."]
        return lines + ([f"Inspect: {inspect}"] if inspect else [])
    said = {}
    for ev in events:
        for s in _footer_sentences(ev, remind_after_s, host, available):
            labels = said.setdefault(s, [])
            if ev.check.label not in labels:
                labels.append(ev.check.label)
    lines = []
    for s, labels in said.items():
        s = s.replace("{it}", "each" if len(labels) > 1 else "it")
        lines.append(f"{', '.join(labels)}: {s}" if len(events) > 1 else s)
    if inspect:
        lines.append(f"Inspect: {inspect}")
    return lines


def _timeline(ev):
    """(label, value) rows; values None are skipped by the renderers."""
    first = _when(ev.first_bad_at)
    if ev.migrated:
        first = f"by {first} (carried over from the old state file)"
    rows = [("First failure", first),
            ("Confirmed", _when(ev.confirmed_at) if ev.confirmed_at else None)]
    if ev.kind in ("down", "degraded") and not ev.escalated:
        rows.append(("Alerted", f"{_when(ev.at)} (this message)"))
    else:
        rows.append(("Alerted", _when(ev.alerted_at) if ev.alerted_at else None))
    if ev.escalated:
        rows.append(("Went down", f"{_when(ev.at)} (this message)"))
    elif ev.escalated_at:
        rows.append(("Went down", _when(ev.escalated_at)))
    if ev.kind == "recovered":
        rows.append(("Recovered", _when(ev.recovered_at or ev.at)))
        lasted = (ev.recovered_at or ev.at) - (ev.first_bad_at or ev.at)
        rows.append(("Outage", ("at least " if ev.migrated else "") + human(lasted)))
    elif ev.kind != "failed":
        rows.append((f"{ev.check.status.capitalize()} for",
                     ("at least " if ev.migrated else "") + human(ev.at - (ev.first_bad_at or ev.at))))
    return rows


def _check_rows(results):
    rows = []
    for r in results:
        note = r.headline if r.status == "ok" else f"{r.status}: {r.headline}"
        note = note if len(note) <= 64 else note[:61] + "..."
        rows.append((r.label, r.status == "ok", note))
    return rows


def _tiles(events, results):
    if len(events) == 1:
        ev = events[0]
        c = ev.check
        if ev.kind == "failed":
            # A unit failure has no streak or outage clock; its tiles are what systemd said.
            return [tuple(f) for f in (c.facts or [])]
        if ev.kind == "recovered":
            lasted = (ev.recovered_at or ev.at) - (ev.first_bad_at or ev.at)
            tiles = [(_dur(lasted), "outage", None), (str(ev.failed_probes), "failed probes")]
        else:
            tiles = [(_dur(ev.at - (ev.first_bad_at or ev.at)), f"{c.status} for", None),
                     (str(ev.failed_probes), "failed probes")]
        return tiles + [tuple(f) for f in (c.facts or [])]
    if all(ev.kind == "failed" for ev in events):
        first = min(ev.at for ev in events)
        return [(str(len(events)), "units failed"),
                (time.strftime("%H:%M", time.localtime(first)), "first failed at")]
    bad = [ev for ev in events if ev.kind != "recovered"]
    tiles = []
    if bad:
        # The clock belongs to the worst status only: a 1-day-old degraded reminder beside a
        # 30-minute DOWN must not make a red "1d DOWN FOR".
        worst = max((ev.check.status for ev in bad), key=lambda st: RANK[st])
        longest = max(ev.at - (ev.first_bad_at or ev.at) for ev in bad
                      if ev.check.status == worst)
        tiles.append((_dur(longest), f"{worst} for"))
    if results and len(results) > 1:
        # Counted from the results, like "passing" beside it: a check alerted in an earlier run
        # and still down has no event now, but it is failing.
        tiles.append((str(sum(1 for r in results if r.status != "ok")), "failing"))
    else:
        tiles.append((str(len(bad)), "alerting now"))
    rec = [ev for ev in events if ev.kind == "recovered"]
    if rec:
        tiles.append((str(len(rec)), "recovered"))
    if results and len(results) > 1:
        ok = sum(1 for r in results if r.status == "ok")
        tiles.append((f"{ok}/{len(results)}", "passing"))
    return tiles


def _status_colour(oe, status):
    return {"down": oe.RED, "degraded": oe.AMBER_BRIGHT, "ok": oe.GREEN}.get(status, oe.RED)


def _pill_colour(oe, events):
    k = _headline_kind(events)
    if k in ("down", "failed"):
        return oe.RED
    if k == "degraded":
        return oe.AMBER_BRIGHT
    if k == "recovered":
        return oe.GREEN
    worst = max((ev.check.status for ev in events if ev.kind == "reminder"),
                key=lambda s: RANK[s], default="down")
    return _status_colour(oe, worst)


def render_plain(events, results=None, host=None, inspect="", remind_after_s=REMIND_AFTER_S,
                 now=None, journal=None, available=None):
    """The full record for a text-only client: everything the HTML says, nothing the SMS
    dropped."""
    host = host or socket.gethostname()
    kind = _headline_kind(events)
    title = _title(events)
    where = _where(events)
    out = [f"{WORD[kind]}: {title}", f"{host}" + (f" · {where}" if where else ""), ""]

    problems = [ev for ev in events if ev.kind != "recovered"]
    if problems:
        out.append("What's wrong")
        for ev in problems:
            word = "DOWN (escalated from degraded)" if ev.escalated else \
                (f"STILL {WORD[ev.check.status]}" if ev.kind == "reminder" else WORD[ev.kind])
            out += [f"  {word} · {ev.check.label}", f"  {ev.check.detail}", ""]
    rec = [ev for ev in events if ev.kind == "recovered"]
    if rec:
        out.append("Recovered")
        for ev in rec:
            out += [f"  {ev.check.label}", f"  {ev.check.detail}", ""]

    tiles = _tiles(events, results)[:4]
    if tiles:
        out.append("Numbers")
        w = max(len(str(t[1])) for t in tiles)
        out += [f"  {str(t[1]).ljust(w)}  {t[0]}" for t in tiles]
        out.append("")

    if kind != "failed":
        out.append("Timeline")
        for ev in events:
            if len(events) > 1:
                out.append(f"  {ev.check.label}")
            rows = [r for r in _timeline(ev) if r[1] is not None]
            w = max(len(r[0]) for r in rows)
            out += [f"    {r[0].ljust(w)}  {r[1]}" if len(events) > 1 else f"  {r[0].ljust(w)}  {r[1]}"
                    for r in rows]
        out.append("")

    if journal:
        out.append("Journal")
        out += [f"  {l}" for l in journal]
        out.append("")

    check_rows = _check_rows(results) if results and len(results) > 1 else []
    item_blocks = [(ev.check.label, ev.check.items) for ev in events if ev.check.items]
    if check_rows or item_blocks:
        out.append("Checks")
        def rows(items, indent):
            w = max(len(str(it[0])) for it in items)
            for it in items:
                mark = " ok " if it[1] else ("FAIL" if it[1] is False else " -- ")
                note = it[2] if len(it) > 2 and it[2] else ""
                out.append(f"{indent}[{mark}] {str(it[0]).ljust(w)}  {note}".rstrip())
        if check_rows:
            rows(check_rows, "  ")
        for label, items in item_blocks:
            nested = bool(check_rows) or len(item_blocks) > 1
            if nested:
                out.append(f"  {label}:")
            rows(items, "    " if nested else "  ")
        out.append("")

    out += _footer_lines(events, remind_after_s, host, inspect, available)
    out += ["", "--", FOOTNOTE]
    return "\n".join(out)


def render_html(events, results=None, host=None, inspect="", remind_after_s=REMIND_AFTER_S,
                now=None, journal=None, available=None):
    """The backup report's template, masthead "Ohmz Stack". Raises on failure; build() is the
    caller that turns a failure into a plain-text email."""
    import ohmz_email as oe
    host = host or socket.gethostname()
    now = events[0].at if now is None else now
    kind = _headline_kind(events)
    title = _title(events)
    pill = oe.verdict_pill("Reminder" if kind == "reminder" else WORD[kind].capitalize(),
                           _pill_colour(oe, events))
    rows = [oe.masthead("Stack", _stamp(now)),
            oe.title_block(title, host, _where(events), pill)]

    tiles = []
    for t in _tiles(events, results):
        accent = None
        if t[1].endswith(" for"):
            accent = _status_colour(oe, "down" if t[1].startswith("down") else "degraded")
        elif t[1] == "outage":
            accent = oe.GREEN
        tiles.append((t[0], t[1], t[2] if len(t) > 2 and t[2] else accent))
    rows.append(oe.stat_tiles(tiles))

    problems = [ev for ev in events if ev.kind != "recovered"]
    if problems:
        cards = ""
        for ev in problems:
            if ev.kind == "failed":
                label, col = f"Failed · {ev.check.label}", oe.RED
            elif ev.kind == "reminder":
                label = f"Still {ev.check.status} · {ev.check.label}"
                col = _status_colour(oe, ev.check.status)
            elif ev.escalated:
                label, col = f"Down, was degraded · {ev.check.label}", oe.RED
            else:
                label, col = f"{ev.kind} · {ev.check.label}", _status_colour(oe, ev.kind)
            cards += oe.message_card(label, ev.check.detail, col)
        rows.append(oe.section("What's wrong", cards))
    rec = [ev for ev in events if ev.kind == "recovered"]
    if rec:
        rows.append(oe.section("Recovered", "".join(
            oe.message_card(f"Recovered · {ev.check.label}", ev.check.detail, oe.GREEN)
            for ev in rec)))

    if kind != "failed":
        inner = "".join(oe.kv_list(_timeline(ev), ev.check.label if len(events) > 1 else "")
                        for ev in events)
        rows.append(oe.section("Timeline", inner))

    if journal:
        rows.append(oe.section("Journal", oe.pre_block(journal)))

    check_rows = _check_rows(results) if results and len(results) > 1 else []
    item_blocks = [(ev.check.label, ev.check.items) for ev in events if ev.check.items]
    if check_rows or item_blocks:
        inner = oe.check_list(check_rows) if check_rows else ""
        for label, items in item_blocks:
            cap = label if (check_rows or len(item_blocks) > 1) else ""
            inner += oe.check_list(items, cap)
        rows.append(oe.section("Checks", inner))

    rows.append(oe.footer(_footer_lines(events, remind_after_s, host, inspect, available)))
    return oe.shell(f"{WORD[kind]} · {title}", rows, FOOTNOTE)


def build(events, results=None, host=None, inspect="", remind_after_s=REMIND_AFTER_S,
          now=None, journal=None, channels=None, available=None):
    """One Notification for all of a run's events, or None if there are none. Each surface is
    guarded separately and degrades rather than raising: no rendering bug may cost the alert.
    available: the channels that can deliver (available_channels()), or None if unknown; the
    route is narrowed to them and the footer's promises follow what is left."""
    if not events:
        return None
    host = host or socket.gethostname()
    chans = _deliverable(channels or channels_for(events), available)
    try:
        sms = render_sms(events, host)
    except Exception:
        sms = _at.sms_body(f"Stack alert: {', '.join(ev.check.label for ev in events)}")
    try:
        subject = render_subject(events, host)
    except Exception:
        subject = SUBJECT_PREFIX + "health alert"
    try:
        plain = render_plain(events, results, host, inspect, remind_after_s, now, journal,
                             available)
    except Exception as e:
        plain = "\n".join([sms, ""] + [f"{ev.kind.upper()} {ev.check.label}: {ev.check.detail}"
                                       for ev in events] + ["", f"(full report failed: {e})"])
    html, err = None, ""
    try:
        html = render_html(events, results, host, inspect, remind_after_s, now, journal,
                           available)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return Notification(sms=sms, subject=subject, plain=plain, html=html, channels=chans,
                        events=list(events), html_error=err)


def _unit_event(unit, host, journal_lines, facts, now):
    if isinstance(journal_lines, str):
        journal_lines = journal_lines.splitlines()
    lines = [l.rstrip() for l in (journal_lines or []) if l is not None]
    last = next((" ".join(l.split()) for l in reversed(lines) if l.strip()), "")
    detail = f"{unit} entered the failed state on {host}."
    if last:
        detail += f" Last log: {last}"
    chk = Check(key=unit, label=unit, status="down", detail=detail, summary=last[:140],
                where="systemd --user",
                facts=[("failed", "state"), (time.strftime("%H:%M", time.localtime(now)),
                                            "failed at")] + [tuple(f) for f in (facts or [])])
    return Event(kind="failed", check=chk, at=now), lines


def render_unit_failure(unit, host=None, journal_lines=(), facts=None, inspect=None, now=None):
    """Notification for a systemd unit that entered the failed state (stack_alert.py). Pill
    FAILED, text AND email. The email carries the whole journal excerpt; the SMS only the last
    line, stripped of addresses. facts: optional (value, label) tiles, e.g. from systemd's
    $MONITOR_SERVICE_RESULT / $MONITOR_EXIT_STATUS."""
    now = time.time() if now is None else float(now)
    host = host or socket.gethostname()
    ev, lines = _unit_event(unit, host, journal_lines, facts, now)
    return build([ev], host=host, inspect=inspect or f"journalctl --user -u {unit} -n 50",
                 now=now, journal=lines or ["(no journal lines captured)"],
                 channels=["sms", "email"])


def render_unit_failures(failures, host=None, now=None):
    """ONE Notification for units that failed together (stack_alert coalesces a burst). failures:
    dicts with unit, lines, facts and at (epoch). One unit renders exactly as
    render_unit_failure; several get one grouped text ("Stack: FAILED: a, b, c"), one email with
    each unit's journal excerpt under its name, and systemd's verdict in each unit's card."""
    fs = [f for f in failures if f and f.get("unit")]
    now = time.time() if now is None else float(now)
    host = host or socket.gethostname()
    if len(fs) == 1:
        f = fs[0]
        return render_unit_failure(f["unit"], host, f.get("lines") or (), f.get("facts"),
                                   now=_num(f.get("at")) or now)
    events, journal = [], []
    for f in fs:
        ev, lines = _unit_event(f["unit"], host, f.get("lines") or (), f.get("facts"),
                                _num(f.get("at")) or now)
        verdict = ", ".join(f"{x[1]} {x[0]}" for x in (f.get("facts") or []) if len(x) == 2)
        if verdict:
            ev.check.detail += f" (systemd: {verdict})"
        events.append(ev)
        journal += ([""] if journal else []) + [f"{f['unit']}:"] + \
            [f"  {l}" for l in lines or ["(no journal lines captured)"]]
    inspect = "journalctl --user -n 50 " + " ".join(f"-u {f['unit']}" for f in fs)
    return build(events, host=host, inspect=inspect, now=max(ev.at for ev in events),
                 journal=journal, channels=["sms", "email"])


# --------------------------------------------------------------------------- sending

def send(notification, handle="ohmz", sender=None):
    """Deliver via alert_transports.send_report (or `sender`, same signature). -> (ok, notes).
    Never raises."""
    fn = sender or _at.send_report
    try:
        return fn(handle, notification.sms, notification.subject, notification.plain,
                  html=notification.html, channels=notification.channels)
    except Exception as e:
        return False, [f"send failed: {type(e).__name__}: {e}"]


def _repeats_unrecorded(ev):
    """Would this event be owed again next run if its delivery cannot be recorded? A recovery
    (its clearing is never saved), a reminder, an escalation, and a DOWN or DEGRADED from a check
    that confirms on one run all would, every run: measured with a read-only state directory,
    "Hermes API RECOVERED" was texted on 6 of 6 runs and backup's DOWN 10 times in 50 minutes."""
    return (ev.kind in ("recovered", "reminder") or ev.escalated
            or (ev.kind in ("down", "degraded") and ev.confirm_after == 1))


def deliver(source, state, state_path, events, *, results=None, handle="ohmz", host=None,
            inspect="", remind_after_s=REMIND_AFTER_S, now=None, dry_run=False, sender=None,
            log=print, available=None):
    """Send one notification for `events`, commit what was delivered, save. The second half of
    every monitor's cycle (run(), begin(), stack_watchdog.notify()). -> RunResult.

    Observations are saved BEFORE sending. That keeps them if the send hangs until systemd kills
    the run, and it proves the state can be written: when neither the file nor its tmpfs fallback
    can be, the events _repeats_unrecorded() names are held back rather than sent again on every
    run, and the run reports saved=False. available: as build(); None with the default transport
    means ask available_channels(), a custom sender is taken at its word."""
    now = time.time() if now is None else float(now)
    say = log or (lambda *_a, **_k: None)
    res = RunResult(events=list(events), state=state)
    send_now = list(events)
    if send_now and not dry_run:
        ok, err = save_state(state_path, state, now)
        if not ok:
            held = [ev for ev in send_now if _repeats_unrecorded(ev)]
            send_now = [ev for ev in send_now if not _repeats_unrecorded(ev)]
            if held:
                what = ", ".join(f"{ev.kind}:{ev.check.key}" for ev in held)
                res.notes.append(f"held back (state cannot be saved, so it would repeat every "
                                 f"run): {what}")
                say(f"[{source}] state cannot be saved ({err}); holding back {what}")
    if send_now:
        if available is None and sender is None:
            available = available_channels(handle)
        res.notification = n = build(send_now, results=results, host=host, inspect=inspect,
                                     remind_after_s=remind_after_s, now=now, available=available)
        summary = ", ".join(f"{ev.kind}:{ev.check.key}" for ev in send_now)
        if n.html_error:
            say(f"[{source}] html render failed, sending plain text ({n.html_error})")
        if dry_run:
            say(f"[{source}] (dry-run, would send via {'+'.join(n.channels)}) {summary}: {n.sms}")
        else:
            res.sent, notes = send(n, handle, sender)
            res.notes += notes
            say(f"[{source}] notify {summary} via {'+'.join(n.channels)} sent={res.sent} {notes}")
            if res.sent:
                mark_delivered(state, send_now)
    if not dry_run:
        ok, err = save_state(state_path, state, now)
        res.saved = ok
        if not ok:
            res.notes.append(f"state not saved: {err}")
            say(f"[{source}] could not save state to {state_path}: {err}")
        elif err:
            say(f"[{source}] {err}")
    return res


def begin(source, state_path, *, label, unit, timeout_s, handle="ohmz", host=None, inspect="",
          max_gap_s=None, boot_id=None, now=None, sender=None, log=print, legacy_key=None):
    """Call at the start of a real run, under exclusive(), before probing. Marks the run in flight
    and saves at once, so a run systemd kills leaves evidence (start_run()). A previous run that
    died is a failed observation of RUNS_KEY; KILLED_ALERT_AFTER of them send "<label> DOWN: N
    runs in a row killed by systemd", text and email, from here, before probing: while no run
    finishes the probes cannot say anything, and that silence is the failure. finished runs
    recover it (finish_run()). -> RunResult."""
    now = time.time() if now is None else float(now)
    say = log or (lambda *_a, **_k: None)
    state = load_state(state_path, source, legacy_key)
    killed, note = start_run(state, now, max_gap_s,
                             current_boot_id() if boot_id is None else boot_id)
    if note:
        say(f"[{source}] {note}")
    events = []
    if killed:
        say(f"[{source}] the previous {killed} run(s) did not finish (killed at "
            f"TimeoutStartSec={timeout_s} s?)")
        chk = Check(key=RUNS_KEY, label=label, status="down", where=unit,
                    summary=f"{killed} runs in a row killed by systemd",
                    detail=(f"The last {killed} runs of {unit} did not finish: systemd killed each "
                            f"at TimeoutStartSec={timeout_s} s, before it could probe, alert or "
                            f"save. The host is likely overloaded (swapping), or a probe is hung. "
                            f"Until a run finishes, nothing this monitor watches is checked."),
                    facts=[(str(killed), "runs killed"), (f"{timeout_s} s", "time limit")])
        events = evaluate(state, [chk], now, confirm_after=KILLED_ALERT_AFTER,
                          recover_after=RECOVER_AFTER, adopt_legacy=False)
    return deliver(source, state, state_path, events, handle=handle, host=host, inspect=inspect,
                   now=now, sender=sender, log=log)


def run(source, results, state_path, *, handle="ohmz", host=None, inspect="",
        confirm_after=CONFIRM_AFTER, remind_after_s=REMIND_AFTER_S, recover_after=RECOVER_AFTER,
        max_gap_s=None, now=None, dry_run=False, legacy_key=None, sender=None, log=print,
        unit=""):
    """The whole cycle for one monitor run: load state, evaluate, send one notification, commit
    what was delivered, save (deliver()). dry_run evaluates and renders but neither sends nor
    saves: saving would consume the edge, and the next real run would owe an alert it no longer
    knows about. A real run should have called begin() first. -> RunResult. Never raises on
    state or delivery problems."""
    now = time.time() if now is None else float(now)
    state = load_state(state_path, source, legacy_key)
    events = evaluate(state, results, now, confirm_after, remind_after_s, recover_after,
                      max_gap_s)
    events += finish_run(state, now, where=unit)
    return deliver(source, state, state_path, events, results=results, handle=handle, host=host,
                   inspect=inspect, remind_after_s=remind_after_s, now=now, dry_run=dry_run,
                   sender=sender, log=log)


# --------------------------------------------------------------------------- samples

# 2026-09-29 15:17:48 EDT: the canary's last real DOWN, so the samples read like its history.
_T0 = 1790709468.0
_ENGINES_DOWN = [("duckduckgo", False, "CAPTCHA"), ("bing", False, "timeout"),
                 ("mojeek", True, "7 results"), ("wikipedia", True, "infobox"),
                 ("wikidata", True, "")]


def _canary(status, **kw):
    base = dict(key="search_chat", label="Web search (chat)", where="127.0.0.1:8888")
    if status == "down":
        base.update(detail="query 'wikipedia' returned 0 results (need 3); engines down: "
                           "bing, duckduckgo", summary="0 results for 'wikipedia'",
                    facts=[("0", "results"), ("2/5", "engines out")], items=_ENGINES_DOWN,
                    attempts=3)
    elif status == "degraded":
        base.update(detail="3 engines unresponsive (duckduckgo, mojeek, bing); 10 results",
                    summary="3 of 5 engines out",
                    facts=[("10", "results"), ("3/5", "engines out")],
                    items=[("duckduckgo", False, "CAPTCHA"), ("bing", False, "timeout"),
                           ("mojeek", False, "429"), ("wikipedia", True, "infobox"),
                           ("wikidata", True, "")], attempts=3)
    else:
        base.update(detail="10 results, 1 engine(s) down (duckduckgo)", summary="10 results",
                    facts=[("10", "results"), ("1/5", "engines out")],
                    items=[("duckduckgo", False, "CAPTCHA"), ("bing", True, "6 results"),
                           ("mojeek", True, "4 results"), ("wikipedia", True, "infobox"),
                           ("wikidata", True, "")])
    base.update(kw)
    return Check(status=status, **base)


_WD = {"gateway": "Hermes gateway", "api": "Hermes API", "delivery": "Delivery timer",
       "backup": "Backup freshness", "flightclaw": "Flightclaw", "pubgate": "Public gate",
       "pubquota": "Guest quota"}


def _wd(bad=(), recovered=()):
    details = {"gateway": ("hermes-gateway service is failed", "not active"),
               "api": ("no HTTP answer on :8642", "no answer on :8642"),
               "backup": ("LAST_OK 31h old (limit 26h)", "LAST_OK 31h old")}
    out = []
    for k, label in _WD.items():
        if k in bad:
            d, s = details.get(k, (f"{label} failing", "failing"))
            out.append(Check(key=k, label=label, status="down", detail=d, summary=s))
        else:
            ok = {"backup": ("LAST_OK 2h old", "LAST_OK 2h old"),
                  "delivery": ("last fired 12s ago", "fired 12s ago")}.get(k, ("answering", ""))
            out.append(Check(key=k, label=label, status="ok", detail=ok[0], summary=ok[1]))
    return out


def _simulate(steps, confirm_after=CONFIRM_AFTER):
    """Drive the real engine through (t, results) steps, delivering every notification but the
    last, which is returned with the results that produced it."""
    state = fresh_state("sample")
    last = None
    for t, results in steps:
        evs = evaluate(state, results, t, confirm_after)
        if evs:
            last = (evs, results, t)
            mark_delivered(state, evs, t)
    return last


def samples(host="ohmz-homelab"):
    """name -> Notification, rendered by the real engine from simulated runs."""
    inspect_canary = "journalctl --user -u search-canary"
    inspect_wd = "journalctl --user -u stack-watchdog"
    out = {}

    def make(name, steps, inspect):
        evs, results, t = _simulate(steps)
        out[name] = build(evs, results=results, host=host, inspect=inspect, now=t)

    run2 = _T0 + 1977
    make("down", [(_T0, [_canary("down")]), (run2, [_canary("down")])], inspect_canary)
    make("degraded", [(_T0, [_canary("degraded")]), (run2, [_canary("degraded")])],
         inspect_canary)
    make("recovered", [(_T0, [_canary("down")]), (run2, [_canary("down")]),
                       (_T0 + 3600, [_canary("down")]),
                       (_T0 + 11520, [_canary("ok", attempts=1)]),
                       (_T0 + 11520 + 1920, [_canary("ok", attempts=1)])], inspect_canary)
    make("reminder", [(_T0, [_canary("down")]), (run2, [_canary("down")]),
                      (run2 + 86400 + 60, [_canary("down")])], inspect_canary)
    make("watchdog_multi", [
        (_T0 - 7200, _wd(bad=("backup",))), (_T0 - 6900, _wd(bad=("backup",))),
        (_T0, _wd(bad=("gateway", "api"))),
        (_T0 + 300, _wd(bad=("gateway", "api")))], inspect_wd)
    out["unit_failed"] = render_unit_failure(
        "hermes-gateway.service", host,
        ["Starting hermes-gateway.service - Hermes gateway...",
         "hermes-gateway: loading profiles from ~/.hermes/profiles",
         "hermes-gateway: profile 'coding' has no model configured",
         "hermes-gateway: refusing to start with an incomplete profile (exit 78)",
         "hermes-gateway.service: Main process exited, code=exited, status=78/CONFIG"],
        facts=[("exit-code", "result"), ("78", "status")], now=_T0)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render health-alert samples.")
    ap.add_argument("--sample", required=True,
                    choices=["down", "degraded", "recovered", "reminder", "unit_failed",
                             "watchdog_multi", "all"])
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "ohmz-health-samples"))
    ap.add_argument("--send-email", action="store_true",
                    help="email (never text) each sample to handle ohmz, subject prefixed [sample]")
    a = ap.parse_args(argv)
    allx = samples()
    names = list(allx) if a.sample == "all" else [a.sample]
    os.makedirs(a.out, exist_ok=True)
    rc = 0
    for name in names:
        n = allx[name]
        with open(os.path.join(a.out, f"{name}.html"), "w") as f:
            f.write(n.html or "")
        with open(os.path.join(a.out, f"{name}.txt"), "w") as f:
            f.write(n.plain + "\n")
        print(f"{name:15} channels={'+'.join(n.channels):10} sms[{len(n.sms)}]={n.sms!r}")
        print(f"{'':15} subject={n.subject!r}")
        if a.send_email:
            ok, notes = _at.send_report("ohmz", n.sms, "[sample] " + n.subject, n.plain,
                                        html=n.html, channels=["email"])
            print(f"{'':15} sent={ok} {notes}")
            rc |= 0 if ok else 1
    print(f"wrote {len(names)} sample(s) to {a.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Send one alert about a failed systemd unit. Invoked by stack-alert@.service via OnFailure=.

Why this exists: the alerting layer had no alarm on itself. hermes-gateway runs Restart=always,
so the only way it PERMANENTLY dies is exit 78 (RestartPreventExitStatus) — which parked it dead
with no signal to anyone. hermes-delivery is a per-minute oneshot whose failures went only to a
journal nobody tails. Either way, a dead alerter silently eats every scheduled alert while the
monitors keep "running" — the exact failure this stack was built to prevent for prices.

Usage: stack_alert.py <unit-name>

Policy: alert within WINDOW_S seconds, text AND email, no confirmation. Unlike the watchdog's
probes, a unit only reaches the failed state after systemd has given up on it (restarts exhausted,
or a oneshot that exited non-zero), so the failure is already confirmed when this runs. Rendered by
health_alert.render_unit_failures: the email is the branded Ohmz Stack report with the last
EXCERPT_LINES journal lines and systemd's verdict as tiles ($MONITOR_SERVICE_RESULT and
$MONITOR_EXIT_STATUS, which systemd sets for OnFailure= units); the text carries the last line only,
with no links (carrier gateways silently drop link-bearing texts). Sent by
alert_transports.send_report.

The excerpt is chosen, not just tailed. Measured on all 11 unit-failure texts of 2026-09-22..24:
not one "Last log" said why. Eight quoted systemd's post-mortem bookkeeping ("Consumed 13min
2.379s CPU time" for a gateway systemd-oomd had killed, two lines below "Failed with result
'oom-kill'"), two described the unit's NEXT run (a restart's "Started ...", and a later, successful
"Finished ..." of the per-minute hermes-delivery), and one was an HTTP access-log line. So
journal_excerpt() cuts the journal at the failed invocation's last entry and skips systemd's
boilerplate. Replaying those 11 failures through it: 9 texts now name the cause (8 oom-kill,
1 signal) and the other 2 say the unit was stopped, which is what happened.

Units that fail together are ONE alert. Each OnFailure=stack-alert@%n.service instance runs on
its own, and 8 of the 11 texts above came in bursts: 2026-09-22 20:31:24 (flightclaw,
hermes-gateway), 2026-09-24 14:42:55 and 15:29:16 (flightclaw, hermes-delivery, hermes-gateway
each time), the instances starting within 0.95 s of each other. That was 3 texts and 3 emails in
a second. So the first instance becomes the batch's leader (enqueue()): it waits WINDOW_S,
drains every failure that joined meanwhile, and sends one notification ("Stack: FAILED:
flightclaw.service, hermes-delivery.service, hermes-gateway.service"); the others append and exit.
A batch whose leader died before draining is adopted by the next failure rather than lost, and if
the batch file cannot be used at all, each failure is sent alone, as before.

Never raises and always exits 0: an alert about a failure must not itself become a failure loop.
If the engine cannot render, a plain-text report still goes out through send_report, text and
email, with every sentence in it true for a unit failure: delivery outranks presentation.

Time budget: stack-alert@.service has TimeoutStartSec=60. Journal read (10 s timeout) + WINDOW_S +
the send (about 45 s if both SMTP legs time out) is 60 s only in that worst case; a typical run
is under 10 s.

Known limitation, accepted: this rides the same SMTP as everything else, so a total SMTP outage
is invisible. Recorded in docs/HERMES_AGENT.md; a second transport is the fix, not more code here.
"""
import contextlib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WATCHDOG_HANDLE = "ohmz"
BATCH_FILE = os.path.expanduser("~/.hermes/stack_alert_batch.json")
WINDOW_S = 5          # a burst's instances start within 0.95 s of each other (measured above)
STALE_S = 50          # a batch this old lost its leader (TimeoutStartSec=60 killed it)
_sleep = time.sleep   # tests replace it
EXCERPT_LINES = 5     # what the email shows; the text takes the last of these
JOURNAL_SCAN = 60     # read back further than that, so cut and skipped lines leave enough
INVOCATION_FIELDS = ("_SYSTEMD_INVOCATION_ID", "USER_INVOCATION_ID", "INVOCATION_ID")

# systemd's own lines that follow EVERY failure and say nothing about why (matched after the
# "<unit>: " prefix is stripped). "Failed to start X - description" only repeats the headline.
_BOOKKEEPING = re.compile(r"^(Consumed .* CPU time.*"
                          r"|Triggering On(Failure|Success)= dependencies\.?"
                          r"|Failed to start .*)$")
# systemd's verdict lines, which the email also shows as tiles ($MONITOR_SERVICE_RESULT,
# $MONITOR_EXIT_STATUS). Skipped only when the result is exit-code AND the failed run logged
# something of its own: an exit status never says why, the service's own last words do ("refusing
# to start ..." before an exit 78). A service that died silently keeps them, or the text would
# quote the run's "Started ..." line. Any other result (oom-kill, signal, timeout,
# start-limit-hit) is systemd's own diagnosis, so it stays and is last.
_RESTATED = re.compile(r"^(Main process exited, .*|Failed with result '.*'\.?)$")


def systemd_facts(env=None):
    """Tiles from the variables systemd (v251+) hands an OnFailure= unit: the result
    ("exit-code", "oom-kill", "start-limit-hit") and the exit status or signal."""
    env = os.environ if env is None else env
    facts = []
    if env.get("MONITOR_SERVICE_RESULT"):
        facts.append((env["MONITOR_SERVICE_RESULT"], "result"))
    if env.get("MONITOR_EXIT_STATUS"):
        killed = env.get("MONITOR_EXIT_CODE") in ("killed", "dumped")
        facts.append((env["MONITOR_EXIT_STATUS"], "signal" if killed else "exit status"))
    return facts


def excerpt_from_json(text, unit, invocation="", drop_restated=False):
    """The meaningful tail of `journalctl -o json` output, oldest first, at most EXCERPT_LINES.

      * Cut after the last entry of the failed invocation ($MONITOR_INVOCATION_ID): a per-minute
        oneshot can run again, and succeed, before this reads the journal. Earlier invocations
        stay, since after start-limit-hit the reason is in the runs before the last one.
      * Skip systemd's bookkeeping, and with drop_restated its verdict lines, unless the failed
        invocation (the whole excerpt, without an id) has no line from the service itself. Only
        entries whose SYSLOG_IDENTIFIER is systemd qualify: a service may print anything,
        including text that looks like systemd's."""
    entries = []
    for raw in text.splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict):
            entries.append(d)
    failed = entries
    if invocation:
        mine = [i for i, d in enumerate(entries)
                if any(d.get(f) == invocation for f in INVOCATION_FIELDS)]
        if mine:
            entries = entries[:mine[-1] + 1]
            failed = [entries[i] for i in mine]
    if drop_restated and not any(d.get("SYSLOG_IDENTIFIER") != "systemd" and d.get("MESSAGE")
                                 for d in failed):
        drop_restated = False
    lines = []
    for d in entries:
        msg = d.get("MESSAGE")
        if isinstance(msg, list):                  # journald's form for a non-UTF-8 message
            msg = bytes(b & 0xFF for b in msg if isinstance(b, int)).decode("utf-8", "replace")
        if not isinstance(msg, str):
            continue
        if d.get("SYSLOG_IDENTIFIER") == "systemd":
            if msg.startswith(unit + ": "):
                msg = msg[len(unit) + 2:]
            if _BOOKKEEPING.match(msg) or (drop_restated and _RESTATED.match(msg)):
                continue
        lines += [part.rstrip() for part in msg.splitlines() if part.strip()]
    return lines[-EXCERPT_LINES:]


def journal_excerpt(unit, invocation="", drop_restated=False):
    """excerpt_from_json over the unit's recent user journal. [] on any error."""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(JOURNAL_SCAN), "--no-pager", "-o",
             "json", "--output-fields=" + ",".join(("MESSAGE", "SYSLOG_IDENTIFIER")
                                                    + INVOCATION_FIELDS)],
            capture_output=True, text=True, timeout=10).stdout
        return excerpt_from_json(out, unit, invocation, drop_restated)
    except Exception:
        return []


@contextlib.contextmanager
def _flock(path, wait_s=5.0):
    """Exclusive flock on `path`, waiting up to wait_s (holders only read or write a small file)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _read_batch(path):
    try:
        with open(path) as f:
            b = json.load(f)
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


def _write_batch(path, batch):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".stack-alert-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(batch, f)
    os.replace(tmp, path)


def enqueue(entry, now=None, path=None):
    """Add one failure to the open batch. -> True if this invocation must lead it (wait WINDOW_S,
    drain(), send), False if a leader already has it. A batch older than STALE_S lost its leader;
    its failures are adopted by this one rather than dropped. Raises if the batch file cannot be
    used; the caller then sends alone."""
    path = path or BATCH_FILE
    now = time.time() if now is None else float(now)
    with _flock(path + ".lock"):
        b = _read_batch(path)
        opened = b.get("opened_at")
        pending = [p for p in (b.get("pending") or []) if isinstance(p, dict)]
        if isinstance(opened, (int, float)) and 0 <= now - opened < STALE_S:
            _write_batch(path, dict(b, pending=pending + [entry]))
            return False
        _write_batch(path, {"opened_at": now, "leader": os.getpid(), "pending": pending + [entry]})
        return True


def drain(path=None):
    """Close the batch. -> every failure in it, oldest first."""
    path = path or BATCH_FILE
    with _flock(path + ".lock"):
        pending = [p for p in (_read_batch(path).get("pending") or []) if isinstance(p, dict)]
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    return sorted(pending, key=lambda p: p.get("at") or 0)


def _fallback(batch, err, host=None):
    """The alert when the engine cannot render: plain text through send_report, text and email.
    It used to be send_alert, whose email closes "a background task you scheduled met its alert
    condition", false for a systemd failure; send_report comes from the same module, so the
    switch costs no robustness."""
    host = host or socket.gethostname()
    units = [b["unit"] for b in batch]
    if len(batch) == 1:
        lines = batch[0].get("lines") or []
        last = " ".join(lines[-1].split())[:140] if lines else ""
        sms = f"{units[0]} FAILED on {host}." + (f" Last log: {last}" if last else "")
        subject = f"[stack] {units[0]} FAILED on {host}"
    else:
        sms = f"Stack: FAILED: {', '.join(units)}"
        subject = f"[stack] {len(units)} units FAILED on {host}: {', '.join(units)}"
    body = []
    for b in batch:
        body += [f"{b['unit']} entered the failed state on {host}."]
        body += [f"  {l}" for l in (b.get("lines") or ["(no journal lines captured)"])] + [""]
    body += [f"Sent by systemd (OnFailure=); the formatted report failed to render: {err}",
             "Inspect: journalctl --user -n 50 " + " ".join(f"-u {u}" for u in units)]
    from alert_transports import send_report
    return send_report(WATCHDOG_HANDLE, sms, subject, "\n".join(body), channels=["sms", "email"])


def run(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    unit = (argv[0] if argv else "") or env.get("MONITOR_UNIT") or "unknown-unit"
    facts = systemd_facts(env)
    lines = journal_excerpt(unit, env.get("MONITOR_INVOCATION_ID", ""),
                            drop_restated=env.get("MONITOR_SERVICE_RESULT") == "exit-code")
    entry = {"unit": unit, "lines": lines, "facts": [list(f) for f in facts], "at": time.time()}
    batch = [entry]
    try:
        if not enqueue(entry):
            print(f"stack_alert: {unit} joined the open batch; its leader sends one alert for all")
            return 0
        _sleep(WINDOW_S)
        batch = drain() or [entry]
    except Exception as e:
        print(f"stack_alert: batching unavailable ({type(e).__name__}: {e}), sending "
              f"{', '.join(b['unit'] for b in batch)} alone", file=sys.stderr)
    try:
        import health_alert as ha
        n = ha.render_unit_failures(batch)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"stack_alert: renderer failed ({err}), sending plain text", file=sys.stderr)
        ok, notes = _fallback(batch, err)
        print(f"stack_alert: sent={ok} (plain) {notes}")
        return 0
    if n.html_error:
        print(f"stack_alert: html render failed, sending plain text ({n.html_error})",
              file=sys.stderr)
    ok, notes = ha.send(n, WATCHDOG_HANDLE)              # never raises
    print(f"stack_alert: sent={ok} via {'+'.join(n.channels)} for "
          f"{', '.join(b['unit'] for b in batch)} {notes}")
    return 0


def main(argv=None, env=None):
    try:
        return run(argv, env)
    except Exception as e:
        # Nothing sane to do — the alerting path itself is broken. Say so in the journal, exit 0
        # so systemd does not chain another failure onto this one.
        print(f"stack_alert: could not send ({type(e).__name__}: {e})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())

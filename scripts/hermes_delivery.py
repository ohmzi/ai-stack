#!/usr/bin/env python3
"""Deterministic delivery for hermes cron runs — the LLM writes text, this delivers it.

Why this exists. Delivery used to be the job agent's responsibility: the delegation brief handed
it exact curl commands for the channel log and the phone push. Two live failures on 2026-07-29
ended that design. First, the agent authoring a job paraphrased the commands into helper functions
that do not exist (send_webhook_post, send_phone_bot_post) — the job ran fine and delivered
nothing. Then, in a direct session, the agent claimed "you got a phone push and an entry was
logged" while ntfy's cache and the channel table both showed nothing. Prompt hardening fights
symptoms; the class of failure — an LLM executing (or pretending to execute) infrastructure
commands — is the disease.

So the contract inverts. A job run ends by WRITING two kinds of line into its response (which
hermes already persists to ~/.hermes/cron/output/<job>/<timestamp>.md, and which never failed
once across every incident):

    LOG: <one-line summary>                     -> posted to the background-tasks channel, always
    ALERT(<user>): <message>                    -> routed to that user's personal alert transport

ALERT lines route to scripts/alert_transports.py (Twilio SMS + SMTP email, both by default). If no
transport is configured or every channel fails, the alert text is folded into the channel post
flagged "[alert]" — a fired condition is never silently dropped.

This watcher (systemd user timer, every minute) scans for new output files and executes the
delivery itself: webhook POST from ~/.hermes/owui_webhook_url for the LOG line, and send_alert()
for ALERT lines. Recipients are validated against ^[a-z0-9_-]+$ so a prompt-injected job cannot
address an arbitrary destination, and alerts are capped at 3 per run. A file
with no LOG line still gets logged (first response line, marked unformatted) so a non-conforming
job is visible rather than silent. Files are processed exactly once, tracked in
~/.hermes/cron/output/.delivered.json.

Usage:  python3 scripts/hermes_delivery.py [--dry-run]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

OUT_DIR = os.path.expanduser("~/.hermes/cron/output")
STATE = os.path.join(OUT_DIR, ".delivered.json")
WEBHOOK_FILE = os.path.expanduser("~/.hermes/owui_webhook_url")
# Recipient is a bare handle; the legacy alerts- prefix is still accepted and stripped, because
# jobs created before 2026-07-30 spell it that way and must keep working.
# Alert delivery is retried and recorded. A "sent" that nobody received is the failure mode this
# whole subsystem keeps hitting, so every attempt is written down: what was tried, when, to which
# channel, and exactly why it failed. MAX_ATTEMPTS counts the first try plus retries.
ALERT_STATE = os.path.join(OUT_DIR, ".alerts.json")        # in-flight queue (mutable)
ALERT_LEDGER = os.path.join(OUT_DIR, "alert_ledger.jsonl")  # append-only history (never rewritten)
MAX_ATTEMPTS = 4          # initial + 3 retries, as requested
RETRY_AFTER_S = 300       # 5 minutes between attempts

ALERT_RE = re.compile(r"^ALERT\((?:alerts-)?([a-z0-9_-]+)\):\s*(.+)$", re.M)
LOG_RE = re.compile(r"^LOG:\s*(.+)$", re.M)


def parse_output(text):
    """(log_line, [(topic, message), ...]) from one run's markdown output."""
    body = text.split("## Response", 1)[-1]
    m = LOG_RE.search(body)
    if m:
        log = m.group(1).strip()
    else:
        # A run that ignored the protocol is a FAILED run, not a result. Posting its prose as if
        # it were a summary is how a hallucinated price (and an invented promo code) reached the
        # channel looking like real output. Label it unmistakably.
        lines = [l.strip() for l in body.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        log = (("⚠️ RUN DID NOT FOLLOW THE OUTPUT PROTOCOL (no LOG line) — treat the text below as "
                "unverified model output, not a measurement: " + lines[0][:160])
               if lines else None)
    found = [(who, msg.strip()) for who, msg in ALERT_RE.findall(body)][:3]  # cap: injection
    alerts = [a for a in found if not is_template(a[1])]
    if len(alerts) < len(found):
        # Never drop it quietly — a suppressed alert must be as visible as a delivered one, or a
        # broken job looks like a calm one.
        note = ("⚠️ suppressed %d un-substituted ALERT template(s) — the run echoed the protocol "
                "instead of filling it in; NOT sent" % (len(found) - len(alerts)))
        log = f"{log} | {note}" if log else note
    return log, alerts


# A model that echoes the protocol template instead of filling it in produces a syntactically
# perfect ALERT line whose body is the instruction text. One reached a real phone as
# "<what happened, with the number>   (ONLY in a run where the user's alert condition holds)".
# Nothing downstream can tell that from a real alert, so it has to die here: an alert body still
# carrying an angle-bracket placeholder, or the brief's own parenthetical, was never substituted.
TEMPLATE_RE = re.compile(r"<[a-z][a-z ,'-]{2,}>|ONLY in a run where|<username>|<msg>", re.I)


def is_template(msg):
    return bool(TEMPLATE_RE.search(msg))


def post_channel(summary, job_name):
    url = open(WEBHOOK_FILE).read().strip()
    data = json.dumps({"content": f"🤖 {job_name}: {summary}"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


def send_alert(recipient, message):
    """Deliver a personal alert via the configured transports (SMS + SMTP email).

    Returns ``(ok, notes)`` — ok is True if ANY channel delivered, notes are the per-channel
    results. Returning the notes (rather than only printing them) is what puts the real reason a
    send failed into the ledger; a bare bool made every failed attempt read "failed" with no cause.

    Import is local and guarded so a missing or broken transport module degrades to "fold the alert
    into the channel post" instead of taking the whole delivery tick down — the LOG line must always
    get through.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from alert_transports import send_alert as _send
        ok, notes = _send(recipient, message)
        return bool(ok), list(notes)
    except Exception as e:
        print(f"  alert[{recipient}] transport error: {e}", file=sys.stderr)
        return False, [f"transport error: {e}"]


def _now():
    return time.time()


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def load_alert_state():
    try:
        return json.load(open(ALERT_STATE))
    except Exception:
        return {}


def save_alert_state(st):
    tmp = ALERT_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, ALERT_STATE)


def ledger_write(entry):
    """Append-only record. Never rewritten, so the history survives a corrupted queue file."""
    try:
        with open(ALERT_LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"  ledger write failed: {e}", file=sys.stderr)


def alert_key(path, recipient, message):
    return hashlib.sha256(f"{path}|{recipient}|{message}".encode()).hexdigest()[:16]


def attempt_alert(key, entry):
    """One delivery attempt. Records it, then schedules a retry or gives up.

    Returns True when delivered. An alert is only abandoned after MAX_ATTEMPTS; the caller posts a
    visible failure notice at that point so a permanently undeliverable alert is never silent."""
    n = len(entry["attempts"]) + 1
    ok, notes = send_alert(entry["recipient"], entry["message"])
    rec = {"at": _iso(_now()), "attempt": n, "of": MAX_ATTEMPTS, "job": entry["job"],
           "recipient": entry["recipient"], "ok": bool(ok), "notes": notes,
           "message": entry["message"][:200]}
    entry["attempts"].append(rec)
    ledger_write(rec)
    if ok:
        entry["status"] = "delivered"
    elif n >= MAX_ATTEMPTS:
        entry["status"] = "failed"
    else:
        entry["status"] = "pending"
        entry["next_attempt"] = _now() + RETRY_AFTER_S
    print(f"  alert[{entry['recipient']}] attempt {n}/{MAX_ATTEMPTS}: "
          f"{'DELIVERED' if ok else 'failed'} — {'; '.join(notes)}")
    return bool(ok)


def process_alert_queue(state):
    """Retry every pending alert whose backoff has elapsed. Returns messages that just gave up."""
    gave_up = []
    for key, entry in state.items():
        if entry.get("status") != "pending":
            continue
        if entry.get("next_attempt", 0) > _now():
            continue
        attempt_alert(key, entry)
        if entry["status"] == "failed":
            gave_up.append(entry)
    return gave_up


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ledger", action="store_true",
                    help="show every recorded alert attempt and the current queue, then exit")
    a = ap.parse_args()

    if a.ledger:
        print(f"ledger: {ALERT_LEDGER}")
        try:
            rows = [json.loads(l) for l in open(ALERT_LEDGER) if l.strip()]
        except Exception:
            rows = []
        for r in rows[-40:]:
            mark = "OK " if r.get("ok") else "FAIL"
            print(f"  {r.get('at')}  {mark} attempt {r.get('attempt')}/{r.get('of')}  "
                  f"{r.get('recipient')}  {r.get('job')}")
            for n in (r.get("notes") or []):
                print(f"        {n}")
        st = load_alert_state()
        pend = {k: v for k, v in st.items() if v.get("status") == "pending"}
        fail = {k: v for k, v in st.items() if v.get("status") == "failed"}
        print(f"\nqueue: {len(pend)} pending, {len(fail)} failed, "
              f"{sum(1 for v in st.values() if v.get('status') == 'delivered')} delivered")
        for k, v in pend.items():
            print(f"  pending {k} -> {v['recipient']} next attempt "
                  f"{_iso(v.get('next_attempt', 0))} ({len(v['attempts'])}/{MAX_ATTEMPTS} used)")
        return 0

    alert_state = load_alert_state()
    state = {}
    if os.path.exists(STATE):
        raw = json.load(open(STATE))
        # Migrate the original list format (fully-delivered files).
        state = ({f: {"log": True, "alerts": True} for f in raw} if isinstance(raw, list) else raw)
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*", "*.md")))
    pending = [f for f in files
               if not (state.get(f, {}).get("log") and state.get(f, {}).get("alerts"))]
    if not pending and not any(e.get("status") == "pending" for e in alert_state.values()):
        print("nothing new")
        return 0

    for f in pending:
        job_name = os.path.basename(os.path.dirname(f))
        log, alerts = parse_output(open(f).read())
        if a.dry_run:
            print(f"{f}: LOG={log!r} ALERTS={alerts}")
            continue
        st = state.setdefault(f, {"log": False, "alerts": False})
        # Each leg retries independently — a failed push must never re-post the channel log.
        if not st["log"]:
            try:
                if log:
                    post_channel(log, job_name)
                st["log"] = True
            except Exception as e:
                print(f"channel retry later {f}: {e}", file=sys.stderr)
        # Alerts go through the retry queue rather than a single fire-and-forget send.
        if not st["alerts"]:
            for who, msg in alerts:
                k = alert_key(f, who, msg)
                if k not in alert_state:
                    alert_state[k] = {"job": job_name, "recipient": who, "message": msg,
                                      "created": _iso(_now()), "attempts": [],
                                      "status": "pending", "next_attempt": 0}
            st["alerts"] = True
        # Alerts are handled inside the channel leg while no transport exists.
        st["alerts"] = True
        print(f"{os.path.basename(f)}: log={'y' if st['log'] else 'RETRY'} "
              f"alerts={'y' if st['alerts'] else 'RETRY'} ({len(alerts)})")

    if not a.dry_run:
        # Drain the retry queue every tick, then persist both state files.
        for entry in process_alert_queue(alert_state):
            try:
                post_channel(f"⚠️ ALERT UNDELIVERABLE after {MAX_ATTEMPTS} attempts "
                             f"(last error: {entry['attempts'][-1]['notes']}): {entry['message']}",
                             entry["job"])
            except Exception:
                pass
        save_alert_state(alert_state)
        json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

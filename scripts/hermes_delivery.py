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
import json
import os
import re
import sys
import urllib.request

OUT_DIR = os.path.expanduser("~/.hermes/cron/output")
STATE = os.path.join(OUT_DIR, ".delivered.json")
WEBHOOK_FILE = os.path.expanduser("~/.hermes/owui_webhook_url")
# Recipient is a bare handle; the legacy alerts- prefix is still accepted and stripped, because
# jobs created before 2026-07-30 spell it that way and must keep working.
ALERT_RE = re.compile(r"^ALERT\((?:alerts-)?([a-z0-9_-]+)\):\s*(.+)$", re.M)
LOG_RE = re.compile(r"^LOG:\s*(.+)$", re.M)


def parse_output(text):
    """(log_line, [(topic, message), ...]) from one run's markdown output."""
    body = text.split("## Response", 1)[-1]
    m = LOG_RE.search(body)
    if m:
        log = m.group(1).strip()
    else:
        lines = [l.strip() for l in body.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        log = (lines[0][:200] + " (job wrote no LOG line)") if lines else None
    alerts = [(who, msg.strip()) for who, msg in ALERT_RE.findall(body)][:3]  # cap: injection
    return log, alerts


def post_channel(summary, job_name):
    url = open(WEBHOOK_FILE).read().strip()
    data = json.dumps({"content": f"🤖 {job_name}: {summary}"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


def send_alert(recipient, message):
    """Deliver a personal alert via the configured transports (Twilio SMS + SMTP email).

    Returns True if ANY channel delivered. Import is local and guarded so a missing or broken
    transport module degrades to "fold the alert into the channel post" instead of taking the whole
    delivery tick down — the LOG line must always get through.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from alert_transports import send_alert as _send
        ok, notes = _send(recipient, message)
        for n in notes:
            print(f"  alert[{recipient}]: {n}")
        return ok
    except Exception as e:
        print(f"  alert[{recipient}] transport error: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    state = {}
    if os.path.exists(STATE):
        raw = json.load(open(STATE))
        # Migrate the original list format (fully-delivered files).
        state = ({f: {"log": True, "alerts": True} for f in raw} if isinstance(raw, list) else raw)
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*", "*.md")))
    pending = [f for f in files
               if not (state.get(f, {}).get("log") and state.get(f, {}).get("alerts"))]
    if not pending:
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
                # With no alert transport, an ALERT line still has to reach the user somehow — it
                # rides the channel post, flagged, rather than vanishing.
                undelivered = [m for who, m in alerts if not send_alert(who, m)]
                line = log or ""
                if undelivered:
                    line = (line + "  ⚠️ [alert] " + " | ".join(undelivered)).strip()
                if line:
                    post_channel(line, job_name)
                st["log"] = True
            except Exception as e:
                print(f"channel retry later {f}: {e}", file=sys.stderr)
        # Alerts are handled inside the channel leg while no transport exists.
        st["alerts"] = True
        print(f"{os.path.basename(f)}: log={'y' if st['log'] else 'RETRY'} "
              f"alerts={'y' if st['alerts'] else 'RETRY'} ({len(alerts)})")

    if not a.dry_run:
        json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

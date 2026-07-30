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
    ALERT(alerts-<user>): <message>             -> pushed to that user's phone topic, only if present

This watcher (systemd user timer, every minute) scans for new output files and executes the
delivery itself: webhook POST from ~/.hermes/owui_webhook_url, ntfy push via the bot token in
~/.hermes/ntfy_alert. Topics are validated against ^alerts-[a-z0-9_-]+$ — a prompt-injected job
cannot exfiltrate to an arbitrary topic, and the bot token never appears in any prompt. A file
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
ALERT_FILE = os.path.expanduser("~/.hermes/ntfy_alert")
ALERT_RE = re.compile(r"^ALERT\((alerts-[a-z0-9_-]+)\):\s*(.+)$", re.M)
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
    alerts = [(t, msg.strip()) for t, msg in ALERT_RE.findall(body)][:3]  # cap: injection hygiene
    return log, alerts


def post_channel(summary, job_name):
    url = open(WEBHOOK_FILE).read().strip()
    data = json.dumps({"content": f"🤖 {job_name}: {summary}"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


def push_phone(topic, message):
    base, token = open(ALERT_FILE).read().split()
    req = urllib.request.Request(
        f"{base}/{topic}", data=message.encode(),
        # HTTP headers are latin-1; an emoji title raises UnicodeEncodeError inside urllib —
        # observed live: the push died after the channel post, and the retry loop then
        # duplicated the channel line every tick. ASCII title; the emoji rides as a ntfy tag.
        headers={"Authorization": f"Bearer {token}", "Title": "Alert",
                 "Tags": "dart", "Priority": "high"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


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
                if log:
                    post_channel(log, job_name)
                st["log"] = True
            except Exception as e:
                print(f"channel retry later {f}: {e}", file=sys.stderr)
        if not st["alerts"]:
            try:
                for topic, msg in alerts:
                    push_phone(topic, msg)
                st["alerts"] = True
            except Exception as e:
                print(f"push retry later {f}: {e}", file=sys.stderr)
        print(f"{os.path.basename(f)}: log={'y' if st['log'] else 'RETRY'} "
              f"alerts={'y' if st['alerts'] else 'RETRY'} ({len(alerts)})")

    if not a.dry_run:
        json.dump(state, open(STATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

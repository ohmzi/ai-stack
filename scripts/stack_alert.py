#!/usr/bin/env python3
"""Send one alert about a failed systemd unit. Invoked by stack-alert@.service via OnFailure=.

Why this exists: the alerting layer had no alarm on itself. hermes-gateway runs Restart=always,
so the only way it PERMANENTLY dies is exit 78 (RestartPreventExitStatus) — which parked it dead
with no signal to anyone. hermes-delivery is a per-minute oneshot whose failures went only to a
journal nobody tails. Either way, a dead alerter silently eats every scheduled alert while the
monitors keep "running" — the exact failure this stack was built to prevent for prices.

Usage: stack_alert.py <unit-name>

Deliberately tiny and dependency-free: it reuses alert_transports.send_alert (SMS gateway + SMTP),
adds a small journal excerpt so the text says WHY, and never raises — an alert about a failure
must not itself become a failure loop.

Known limitation, accepted: this rides the same SMTP as everything else, so a total SMTP outage
is invisible. Recorded in docs/HERMES_AGENT.md; a second transport is the fix, not more code here.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WATCHDOG_HANDLE = "ohmz"


def main():
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown-unit"
    excerpt = ""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", "5", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        # Last line only, and no URLs — carrier gateways silently drop link-bearing texts.
        last = out.splitlines()[-1] if out else ""
        excerpt = " Last log: " + " ".join(last.split())[:140] if last else ""
    except Exception:
        pass
    try:
        from alert_transports import send_alert
        ok, notes = send_alert(
            WATCHDOG_HANDLE,
            f"{unit} entered FAILED state on ohmz-homelab.{excerpt}",
            subject=f"[stack] {unit} failed")
        print(f"stack_alert: sent={ok} {notes}")
    except Exception as e:
        # Nothing sane to do — the alerting path itself is broken. Say so in the journal, exit 0
        # so systemd does not chain another failure onto this one.
        print(f"stack_alert: could not send ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

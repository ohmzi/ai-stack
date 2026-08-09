#!/usr/bin/env python3
"""The stack's own health check: alert once when something breaks, once when it recovers.

Why this exists: the pieces that DELIVER alerts had no alarm covering them. A wedged
hermes-gateway (alive as a process, dead as an API), a delivery timer that quietly stopped
firing, or a backup job that stopped stamping LAST_OK all looked identical to a healthy
quiet system.

Checks, each fail-safe (a probe error is a FAIL for that check, never a crash):
  gateway   systemctl --user is-active hermes-gateway
  api       GET 127.0.0.1:8642 answers HTTP at all (any status — auth errors prove liveness;
            catches wedged-but-active, which is-active cannot)
  delivery  hermes-delivery.timer last trigger < 10 min (it fires every minute)
  backup    /media/SandiskSSD/ai-stack-backups/LAST_OK newer than 26 h
  flightclaw  systemctl --user is-active flightclaw + GET 127.0.0.1:8765/mcp answers HTTP
            at all (406 to a bare GET is a live MCP server refusing politely)
Report-only (logged, never alerted — they have their own recovery stories and the pipe
already surfaces them to the user): comfyui /system_stats, ollama /api/version.

Alerting is ON STATE TRANSITION only, tracked per-check in ~/.hermes/watchdog_state.json —
the same dedup idiom as hermes_delivery's fail_alerted: a broken thing alerts once, not every
5 minutes; recovery closes the loop with one message. All checks that changed state share one
text (a 3am incident is one buzz, not four).

Runs every 5 min from stack-watchdog.timer. Test drive: stack_watchdog.py --dry-run prints
verdicts and sends nothing.

Known limitation, accepted and recorded: alerts ride the same SMTP as everything else, so a
total SMTP outage is invisible to this too.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WATCHDOG_HANDLE = "ohmz"
STATE_FILE = os.path.expanduser("~/.hermes/watchdog_state.json")
BACKUP_LAST_OK = "/media/SandiskSSD/ai-stack-backups/LAST_OK"
BACKUP_MAX_AGE_S = 26 * 3600
DELIVERY_MAX_AGE_S = 10 * 60


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def check_gateway():
    r = _run(["systemctl", "--user", "is-active", "hermes-gateway"])
    return bool(r) and r.stdout.strip() == "active", "hermes-gateway service"


def check_api():
    try:
        req = urllib.request.Request("http://127.0.0.1:8642/v1/models")
        with urllib.request.urlopen(req, timeout=5) as r:
            return True, "hermes API"
    except urllib.error.HTTPError:
        return True, "hermes API"       # 401/404 is still a live server answering
    except Exception:
        return False, "hermes API (no HTTP answer on :8642)"


def check_delivery():
    r = _run(["systemctl", "--user", "show", "hermes-delivery.timer",
              "--property=LastTriggerUSec", "--property=ActiveState"])
    if not r or "ActiveState=active" not in r.stdout:
        return False, "hermes-delivery.timer (not active)"
    try:
        # systemctl prints *USec properties FORMATTED ("Sat 2026-08-01 02:38:04 EDT"), not as raw
        # microseconds — learned by this check reporting a healthy timer as unreadable. `date -d`
        # parses that form, weekday and zone included.
        stamp = r.stdout.split("LastTriggerUSec=")[1].splitlines()[0].strip()
        if not stamp or stamp == "n/a":
            return False, "hermes-delivery.timer (never fired)"
        d = _run(["date", "-d", stamp, "+%s"])
        age = time.time() - int(d.stdout.strip())
        return age < DELIVERY_MAX_AGE_S, f"hermes-delivery.timer (last fired {int(age)}s ago)"
    except Exception:
        return False, "hermes-delivery.timer (unreadable)"


def check_flightclaw():
    """The fare engine. Same wedged-but-active reasoning as check_api: is-active proves the
    process, an HTTP answer proves the server. /mcp answers 406 to a bare GET (streamable HTTP
    wants POST + SSE accept), and a 406 from it is a LIVE server refusing politely."""
    r = _run(["systemctl", "--user", "is-active", "flightclaw"])
    if not (r and r.stdout.strip() == "active"):
        return False, "flightclaw service"
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/mcp")
        with urllib.request.urlopen(req, timeout=5):
            return True, "flightclaw API"
    except urllib.error.HTTPError:
        return True, "flightclaw API"   # 406/405 is still a live server answering
    except Exception:
        return False, "flightclaw API (no HTTP answer on :8765)"


def check_backup():
    try:
        age = time.time() - os.path.getmtime(BACKUP_LAST_OK)
        return age < BACKUP_MAX_AGE_S, f"backup (LAST_OK {int(age / 3600)}h old)"
    except OSError:
        return False, "backup (no LAST_OK yet)"


def report_only():
    out = []
    for name, url in (("comfyui", "http://127.0.0.1:8188/system_stats"),
                      ("ollama", "http://127.0.0.1:11434/api/version")):
        try:
            with urllib.request.urlopen(url, timeout=5):
                out.append(f"{name}=up")
        except Exception:
            out.append(f"{name}=DOWN")
    return out


def main():
    dry = "--dry-run" in sys.argv
    checks = {"gateway": check_gateway, "api": check_api,
              "delivery": check_delivery, "backup": check_backup,
              "flightclaw": check_flightclaw}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}

    failed_msgs, recovered_msgs = [], []
    for key, fn in checks.items():
        try:
            ok, detail = fn()
        except Exception as e:                                 # belt over each check's braces
            ok, detail = False, f"{key} (checker error: {e})"
        was_ok = state.get(key, {}).get("ok", True)
        state[key] = {"ok": ok, "detail": detail, "at": int(time.time())}
        print(f"[watchdog] {key:9} {'OK  ' if ok else 'FAIL'} {detail}")
        if was_ok and not ok:
            failed_msgs.append(detail)
        elif not was_ok and ok:
            recovered_msgs.append(detail)

    print(f"[watchdog] info: {', '.join(report_only())}")

    if not dry and (failed_msgs or recovered_msgs):
        parts = []
        if failed_msgs:
            parts.append("DOWN: " + "; ".join(failed_msgs))
        if recovered_msgs:
            parts.append("recovered: " + "; ".join(recovered_msgs))
        try:
            from alert_transports import send_alert
            ok, notes = send_alert(WATCHDOG_HANDLE, "Stack watchdog - " + ". ".join(parts),
                                   subject="[stack] watchdog")
            print(f"[watchdog] alert sent={ok} {notes}")
        except Exception as e:
            print(f"[watchdog] could not send alert: {e}", file=sys.stderr)

    if not dry:
        # Dry runs must not persist: writing state consumes the fail/recover EDGE, so a --dry-run
        # at the wrong moment would swallow the one real alert the next timer tick owed the owner.
        # Caught by tests/test_watchdog.py before it ever bit live.
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The stack's own health check: alert when something is really broken, once when it recovers.

Why this exists: the pieces that DELIVER alerts had no alarm covering them. A wedged
hermes-gateway (alive as a process, dead as an API), a delivery timer that quietly stopped
firing, or a backup job that stopped stamping LAST_OK all looked identical to a healthy
quiet system.

Checks, each fail-safe (a probe error is a FAIL for that check, never a crash):
  gateway   systemctl --user is-active hermes-gateway
  api       GET 127.0.0.1:8642/health answers HTTP at all (any status proves liveness; catches
            wedged-but-active, which is-active cannot). /health is the one keyless route, so the
            probe no longer logs an "invalid API key" warning every 5 minutes
  delivery  hermes-delivery.timer last trigger < 10 min (it fires every minute)
  backup    /media/SandiskSSD/ai-stack-backups/LAST_OK newer than 26 h
  flightclaw  systemctl --user is-active flightclaw + GET 127.0.0.1:8765/mcp answers HTTP
            at all (406 to a bare GET is a live MCP server refusing politely)
  pubgate   GET 127.0.0.1:4568/api/config answers HTTP (docs/PUBLIC_INSTANCE.md) — proves
            owui-public-gate and open-webui-public are both up. Does NOT cover the Ollama
            pinhole (owui-public-ollama) — verified 2026-08-11 this endpoint answers 200
            regardless of that container's state, since it doesn't touch Ollama
  pubquota  owui-public-quota's container healthcheck. Separate from pubgate because the guest
            quota fails CLOSED and invisibly to it: auth_request turns any non-204/403 into a
            500, so guest chat breaks while /api/config keeps answering and pubgate stays green
  ticker    every profile's cron ticker_heartbeat AND ticker_last_success < 5 min old. The
            ticker is a thread inside the gateway: it can die, or fail every tick, while the
            process and the API stay up, and then no cron job fires and every check above passes
  gwrestarts  fewer than 3 gateway starts in the trailing 6 h (~/.hermes/gateway-starts.log).
            Liveness restarts (exit 75) are back within seconds, between two is-active probes
  backlog   no LOG result undelivered for 15 min (~/.hermes/cron/output/.delivered.json): a
            channel webhook that fails every minute otherwise withholds results in silence
  hermesver the hermes checkout is still the pinned commit and the gateway runs the pinned
            version (HERMES_PIN). Nothing verifies the pipe's contracts after an unplanned update
Severity: gwrestarts, backlog and hermesver fail as DEGRADED, which goes by email only; the rest
are up/down and text AND email (see SEVERITY).
Report-only (logged, never alerted — they have their own recovery stories and the pipe
already surfaces them to the user): comfyui /system_stats, ollama /api/version.

Alert policy, applied by scripts/health_alert.py (the engine shared with the search canary):
  * CONFIRM   a check must fail 2 runs (5-10 min of real failure on the 5-minute timer) before it
              alerts, and only 2 ok runs in a row end a streak, so fail,ok,fail confirms. The old
              rule alerted on the first failed run, and blips paid for it: 20 notifications (each
              a text AND an email) 2026-09-22..24, for incidents such as a gateway restart during
              an upgrade or the public gate down for one 5-minute run, each a DOWN then a
              "recovered" minutes later. Replaying all 2051 invocations in the journal
              (2026-09-21..29), the 21 that systemd killed included, through this policy gives 9:
              7 for the 3 outages that outlasted one run, and the KILLED pair below. The cost,
              accepted: an outage has to span two runs to be reported at all.
  * RECOVERED only for an outage that was alerted, after 2 ok runs in a row (flap damping); an
              unconfirmed blip resets silently.
  * backup confirms and recovers on 1 run, not 2: its threshold is 26 hours of staleness, so it
              is already slow-moving, and a second run 5 minutes later adds no evidence. The same
              holds for gwrestarts (a 6-hour count read from a ledger), backlog (15 minutes of
              failed delivery ticks) and hermesver (a commit id, which does not blip).
  * KILLED    each run marks itself in flight before probing (health_alert.begin). On 2026-09-24
              systemd killed 21 runs between 10:19 and 14:02 at TimeoutStartSec on a thrashing
              host; the engine could not see them, and confirmation would have waited until
              14:42 for the gateway's DOWN, 3 hours after the old code's 11:40 text. Now two
              killed runs in a row text "Watchdog runs DOWN" at once: in the replay, at 10:30,
              with the gateway's own DOWN at 13:35 (fail 11:40, ok 12:34, fail 13:35). Each run
              is also bounded: the probes run concurrently with PROBE_BUDGET_S between them and a
              verdict, and a probe still running then is a failure of that check.
  * FRESH     a reboot, or more than MAX_GAP_S since the last run started, restarts unalerted
              streaks: the first runs after boot (OnBootSec=3min) often see services starting.
              MAX_GAP_S is an hour because the thrashing host above went 42 minutes between
              runs, and those were real, consecutive observations.
  * REMINDER  once a day while an alerted check stays down.
  * One notification per run for every check that changed: a 3am incident is one buzz, not four.
  * SEVERITY  a down check texts AND emails; a degraded one only emails (the engine's routing).
              Degraded is for checks where the service still answers: a gateway that restarted
              itself 3 times and came back, results waiting on a webhook, a version nobody
              verified. If the service stops answering, gateway/api/ticker go down and text. The
              email is the branded Ohmz Stack report; the text is one line, no links.
  * A send that fails is retried by the next run; it is not recorded as delivered.
State lives in ~/.hermes/watchdog_state.json under the same keys as before the engine, so an
old-format file migrates on first load and an outage in progress still gets its recovery. A real
run holds ~/.hermes/watchdog_state.json.lock throughout; a manual run while the timer's is in
flight logs "another run is in progress" and exits 0.

Runs every 5 min from stack-watchdog.timer. Test drive: stack_watchdog.py --dry-run probes,
shows what would be sent, and neither sends nor saves state (nor takes the lock).

Known limitation, accepted and recorded: alerts ride the same SMTP as everything else, so a
total SMTP outage is invisible to this too.
"""
import concurrent.futures
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import health_alert as ha  # noqa: E402  (stdlib-only; its transports are patched by the tests)

WATCHDOG_HANDLE = "ohmz"
SOURCE = "watchdog"
STATE_FILE = os.path.expanduser("~/.hermes/watchdog_state.json")
BACKUP_LAST_OK = "/media/SandiskSSD/ai-stack-backups/LAST_OK"
BACKUP_MAX_AGE_S = 26 * 3600
DELIVERY_MAX_AGE_S = 10 * 60
INSPECT = "journalctl --user -u stack-watchdog -n 40, or re-probe: stack_watchdog.py --dry-run"
UNIT = "stack-watchdog.service"
RUNS_LABEL = "Watchdog runs"
TIMEOUT_START_S = 120      # the unit's TimeoutStartSec, quoted in the killed-runs alert
# The probes' shared deadline. They normally finish in about a second; the send after them can
# take ~45 s when SMTP is slow (20 s per leg plus a 6 s MX check), and 45 + 45 fits the 120.
PROBE_BUDGET_S = 45
MAX_GAP_S = 3600           # see FRESH in the docstring
CONFIRM_AFTER = 2          # failed runs before an alert; the policy is in the docstring
RECOVER_AFTER = 2          # ok runs in a row before a recovery
# Per-check (confirm_after, recover_after) overrides: evidence that a second look 5 minutes later
# cannot change (the docstring's "backup confirms and recovers on 1 run").
POLICY = {"backup": (1, 1), "gwrestarts": (1, 1), "backlog": (1, 1), "hermesver": (1, 1)}
# The status a failing check reports; unlisted checks are "down". See SEVERITY in the docstring.
SEVERITY = {"gwrestarts": "degraded", "backlog": "degraded", "hermesver": "degraded"}

HERMES_HOME = os.path.expanduser("~/.hermes")
API_HEALTH_URL = "http://127.0.0.1:8642/health"
# The default profile's ticker files; each multiplexed profile keeps its own pair under
# profiles/<name>/cron/ (cron/jobs.py record_ticker_heartbeat, scoped per profile store).
TICKER_DIR = os.path.join(HERMES_HOME, "cron")
TICKER_PROFILES_ROOT = os.path.join(HERMES_HOME, "profiles")
# Which directories under profiles/ hermes treats as profiles, and so ticks and heartbeats. Copied
# from hermes_cli/profiles.py _iter_named_profile_dirs at v0.21.4: a valid id (hermes_constants.py
# PROFILE_ID_RE), never "default", at least one identity marker (_PROFILE_IDENTITY_MARKERS; a
# dangling symlinked marker still counts), and no tombstone at profiles/.deleted/<name>. A glob of
# profiles/*/cron was wider than that: a `cp -a coding coding.bak-<date>` copy, a marker-less ghost
# shell holding only cron/, or a deleted profile's dir that a stale process re-created all have a
# heartbeat nobody writes, so each one failed this check as DOWN, texted, and then sent a daily
# reminder until someone deleted the dir.
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
# Five ticks. The ticker writes both files every 60 s, deferred ticks included: gpuguard's gate
# returning False skips dispatch, not the beat (in the multiplex loop a skipped cycle leaves no
# profile error, so success is recorded too). Watched 2026-09-29 23:33-23:40 with a model named
# hermes-genesis-* resident to trip the gate: 4 deferred ticks, 23:35-23:38, and both files in
# both profiles advanced every 60.0 s throughout, within 0.1 s of each "cron tick deferred" log
# line. Over agent.log's history, 1306 of 1311 gaps between consecutive deferred ticks sit on the
# 60 s grid (+-2 s); off it are 3 gateway restarts, the longest 257 s (the 2026-09-23 upgrade),
# and 2 ticks 33-38 s late that morning. All stay under 300 s, and confirmation takes 2 runs.
TICKER_MAX_AGE_S = 300
# hermes's own respawn-storm ledger (gateway/status.py record_start_and_check_storm): one float
# epoch per line, appended at every gateway start, kept to the last 40. Read instead of systemd's
# NRestarts, which resets at every boot; the 2026-09-23/24 incident spanned 10 reboots.
GATEWAY_STARTS_LOG = os.path.join(HERMES_HOME, "gateway-starts.log")
GW_STARTS_ALERT = 3
GW_STARTS_WINDOW_S = 6 * 3600
# hermes_delivery.py's state: {output path: {"log": bool, "alerts": bool}}. log stays false while
# the channel webhook fails and is retried every minute; 15 minutes is 15 failed ticks in a row.
DELIVERED_STATE = os.path.join(HERMES_HOME, "cron", "output", ".delivered.json")
BACKLOG_MAX_AGE_S = 15 * 60
# The hermes build this repo is verified against (docs/HERMES_AGENT.md): tag, commit, and the
# version its /health reports. Bump all three in the same change as a deliberate upgrade.
HERMES_CHECKOUT = os.path.join(HERMES_HOME, "hermes-agent")
HERMES_PIN = ("v2026.9.21", "d337b736aa1e8ebecfab043842d13e4a2d2f48a3", "0.21.4")

# key -> (label, where). The keys are the state file's from before the engine: renaming one
# orphans its alerted flag, and an outage in progress would end without its recovery message.
CHECKS = {
    "gateway": ("Hermes gateway", "hermes-gateway.service"),
    "api": ("Hermes API", "127.0.0.1:8642"),
    "delivery": ("Delivery timer", "hermes-delivery.timer"),
    "backup": ("Backup freshness", BACKUP_LAST_OK),
    "flightclaw": ("Flightclaw", "flightclaw.service, 127.0.0.1:8765"),
    "pubgate": ("Public gate", "127.0.0.1:4568"),
    "pubquota": ("Guest quota", "owui-public-quota container"),
    "ticker": ("Cron ticker", "~/.hermes/cron/ticker_heartbeat (every profile)"),
    "gwrestarts": ("Gateway restarts", "~/.hermes/gateway-starts.log"),
    "backlog": ("Delivery backlog", "~/.hermes/cron/output/.delivered.json"),
    "hermesver": ("Hermes version", f"~/.hermes/hermes-agent, pinned {HERMES_PIN[0]}"),
}


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _why(e):
    """'Connection refused' or 'timed out', not a urllib repr: this lands in a 140-char text."""
    r = getattr(e, "reason", None) or e
    return (getattr(r, "strerror", None) or str(r) or type(r).__name__)[:60]


def _http_answers(url, timeout=5):
    """(answered, what). ANY HTTP status proves a live server: the gateway's 401, the MCP
    endpoint's 406 and the gate's 200 all mean something answered. Only silence is a failure,
    and silence is what a wedged-but-active process looks like."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, _why(e)


def _unit_state(unit):
    r = _run(["systemctl", "--user", "is-active", unit])
    return (r.stdout.strip() if r else "") or "unknown"


def check_gateway():
    state = _unit_state("hermes-gateway")
    return state == "active", f"service {state}"


def check_api():
    """GET /health, the one route hermes v0.21.4 serves without auth (api_server.py
    _handle_health has no @_require_auth): 200 {"status": "ok", "platform": "hermes-agent",
    "version": "0.21.4"}, and nothing in gateway.log. This probe used to GET /v1/models with no
    key, and the gateway logged every one as "API server rejected invalid API key ... GET
    /v1/models user_agent='Python-urllib/3.12'": 247 lines a day in errors.log, 5246 of its 5438
    lines 2026-09-22..29 (96.5%), burying the errors that log exists for. Any HTTP answer still
    proves liveness (_http_answers), so if upstream ever puts /health behind auth the probe reads
    a 401 as live, not as a false DOWN. No key either way: a secret in a 5-minute liveness check
    buys nothing."""
    ok, what = _http_answers(API_HEALTH_URL)
    return ok, f"{what} on :8642" if ok else f"no HTTP answer on :8642 ({what})"


def check_delivery():
    r = _run(["systemctl", "--user", "show", "hermes-delivery.timer",
              "--property=LastTriggerUSec", "--property=ActiveState"])
    if not r or "ActiveState=active" not in r.stdout:
        return False, "timer not active"
    try:
        # systemctl prints *USec properties FORMATTED ("Sat 2026-08-01 02:38:04 EDT"), not as raw
        # microseconds — learned by this check reporting a healthy timer as unreadable. `date -d`
        # parses that form, weekday and zone included.
        stamp = r.stdout.split("LastTriggerUSec=")[1].splitlines()[0].strip()
        if not stamp or stamp == "n/a":
            return False, "timer never fired"
        d = _run(["date", "-d", stamp, "+%s"])
        age = int(time.time() - int(d.stdout.strip()))
    except Exception:
        return False, "last trigger time unreadable"
    if age < DELIVERY_MAX_AGE_S:
        return True, f"last fired {age}s ago"
    return False, f"last fired {age // 60} min ago (limit {DELIVERY_MAX_AGE_S // 60})"


def check_flightclaw():
    """The fare engine. Same wedged-but-active reasoning as check_api: is-active proves the
    process, an HTTP answer proves the server. /mcp answers 406 to a bare GET (streamable HTTP
    wants POST + SSE accept), and a 406 from it is a LIVE server refusing politely."""
    state = _unit_state("flightclaw")
    if state != "active":
        return False, f"service {state}"
    ok, what = _http_answers("http://127.0.0.1:8765/mcp")
    return ok, f"{what} on :8765" if ok else f"no HTTP answer on :8765 ({what})"


def check_public_gate():
    ok, what = _http_answers("http://127.0.0.1:4568/api/config")
    return ok, f"{what} on :4568" if ok else f"no HTTP answer on :4568 ({what})"


def check_public_quota():
    """The guest message quota (docs/PUBLIC_INSTANCE.md). Worth its own check because it fails
    CLOSED and INVISIBLY to the check above: nginx's auth_request turns any non-204/403 answer into
    a 500, so if this service dies every guest chat breaks while /api/config — which does not go
    through auth_request — keeps answering 200 and `pubgate` stays green.

    Read off the container's own healthcheck rather than probing directly: the service publishes no
    host port (only the gate talks to it, over the compose network), and docker is already polling
    it every 30s."""
    r = _run(["docker", "inspect", "-f", "{{.State.Health.Status}}", "owui-public-quota"])
    if not r or r.returncode != 0:
        return False, "container missing"
    status = r.stdout.strip() or "without a health status"
    return status == "healthy", f"container {status}"


def check_backup():
    try:
        age = time.time() - os.path.getmtime(BACKUP_LAST_OK)
    except OSError as e:
        return False, f"LAST_OK unreadable ({e.strerror or type(e).__name__})"
    hours = int(age / 3600)
    if age < BACKUP_MAX_AGE_S:
        return True, f"LAST_OK {hours}h old"
    return False, f"LAST_OK {hours}h old (limit {BACKUP_MAX_AGE_S // 3600}h)"


def _epoch_file(path):
    """The float epoch hermes writes as the file's whole content (str(time.time())). -> float.
    Raises OSError or ValueError, which the caller words."""
    with open(path, encoding="utf-8") as f:
        return float(f.read().strip())


def _ticked_profiles(root=None):
    """[(name, cron dir)] for each named profile hermes ticks (see PROFILE_ID_RE above), sorted by
    name. No profiles/ dir means no named profiles. Any other OSError propagates to the caller."""
    root = root or TICKER_PROFILES_ROOT
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        home = os.path.join(root, name)
        if name == "default" or not PROFILE_ID_RE.match(name) or not os.path.isdir(home):
            continue
        if not any(os.path.isfile(os.path.join(home, m)) or os.path.islink(os.path.join(home, m))
                   for m in PROFILE_MARKERS):
            continue
        if os.path.exists(os.path.join(root, ".deleted", name)):
            continue
        out.append((name, os.path.join(home, "cron")))
    return out


def check_ticker():
    """Every profile's cron ticker is beating AND completing ticks. The ticker is a daemon thread
    in the gateway; when it dies, or every tick raises, the process stays active and the API keeps
    answering, so every other check passes while no cron job fires. The case that came close:
    v0.21.4 added keywords to InProcessCronScheduler.start, gpuguard's fixed signature raised on
    every spawn, and the supervisor would have respawned it in a loop forever (fixed in a0d828a
    before it shipped). Two files per profile, because they fail apart: ticker_heartbeat is
    written every iteration, ticker_last_success only when the tick did not raise, so a fresh
    heartbeat with a stale success marker is a ticker alive and failing every tick."""
    now = time.time()
    bad, newest = [], 0.0
    try:
        named = _ticked_profiles()
    except OSError as e:
        # An unreadable profiles/ is a probe error, so a FAIL: skipping it would stop watching every
        # named profile without saying so.
        named = []
        bad.append(f"profiles: {e.strerror or 'unreadable'}")
    dirs = [("default", TICKER_DIR)] + named
    limit = TICKER_MAX_AGE_S // 60
    for name, d in dirs:
        try:
            beat = _epoch_file(os.path.join(d, "ticker_heartbeat"))
        except (OSError, ValueError) as e:
            why = e.strerror if isinstance(e, OSError) and e.strerror else "unreadable"
            bad.append(f"{name}: heartbeat {why}")
            continue
        age = max(0.0, now - beat)
        newest = max(newest, age)
        if age >= TICKER_MAX_AGE_S:
            bad.append(f"{name}: last beat {int(age // 60)} min ago (limit {limit})")
            continue
        try:
            ok_age = max(0.0, now - _epoch_file(os.path.join(d, "ticker_last_success")))
        except (OSError, ValueError):
            ok_age = None
        if ok_age is None or ok_age >= TICKER_MAX_AGE_S:
            since = "recorded" if ok_age is None else f"in {int(ok_age // 60)} min"
            bad.append(f"{name}: beating, but no successful tick {since}")
    if bad:
        return False, "; ".join(bad)
    return True, f"beat {int(newest)}s ago ({', '.join(n for n, _ in dirs)})"


def check_gateway_restarts():
    """Fewer than GW_STARTS_ALERT gateway starts in the trailing GW_STARTS_WINDOW_S. A liveness
    restart (exit 75) or an OOM kill is usually back within seconds, between two 5-minute probes,
    so `is-active` reads active and the gateway check misses it. The ledger shows 11 starts on
    2026-09-24 between 10:59 and 15:57, 7 of them without a reboot. Counted from hermes's own
    ledger, not NRestarts, which resets at every boot (10 of them 2026-09-23..24). Planned
    restarts and reboots count too: 3 in 6 hours is worth an email even when each had a reason."""
    try:
        with open(GATEWAY_STARTS_LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        # hermes writes the ledger at every start unless gateway.respawn_storm.max_starts <= 0.
        return False, f"gateway-starts.log unreadable ({e.strerror or type(e).__name__})"
    starts = []
    for line in lines:
        with contextlib.suppress(ValueError):
            starts.append(float(line))
    now = time.time()
    # Later than now counts too: a clock stepped back leaves recent starts in the "future".
    recent = sorted(t for t in starts if t > now - GW_STARTS_WINDOW_S)
    hours = GW_STARTS_WINDOW_S // 3600
    n = len(recent)
    if not recent:
        return True, f"no starts in {hours} h"
    detail = (f"{n} start{'' if n == 1 else 's'} in {hours} h, last "
              f"{time.strftime('%m-%d %H:%M', time.localtime(recent[-1]))}")
    if n >= GW_STARTS_ALERT:
        return False, f"{detail} (alerts at {GW_STARTS_ALERT})"
    return True, detail


def check_backlog():
    """No LOG result withheld for BACKLOG_MAX_AGE_S. hermes_delivery.py retries a failed channel
    post every minute, forever and quietly (its stderr), and the phone ALERT leg does not depend
    on it, so a dead webhook withholds every result from the channel while the jobs, the timer
    and the alerts all look healthy. Reads only the delivery state: never the webhook URL, which
    appears nowhere in the detail. An output file that is gone (pruned) is not a backlog."""
    try:
        with open(DELIVERED_STATE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return True, "no delivery state yet"
    except (OSError, ValueError) as e:
        why = getattr(e, "strerror", None) or type(e).__name__
        return False, f".delivered.json unreadable ({why})"
    if not isinstance(state, dict):
        # The original format, a list of fully delivered files (hermes_delivery migrates it).
        return True, "no LOG result withheld"
    now = time.time()
    stuck = []
    for path, entry in state.items():
        if not isinstance(entry, dict) or entry.get("log") is not False:
            continue
        try:
            age = now - os.path.getmtime(path)
        except (OSError, TypeError, ValueError):
            continue
        if age >= BACKLOG_MAX_AGE_S:
            stuck.append((age, path))
    if not stuck:
        return True, "no LOG result withheld"
    age, path = max(stuck)
    job = os.path.basename(os.path.dirname(path))
    return False, (f"{len(stuck)} LOG result{'' if len(stuck) == 1 else 's'} undelivered, oldest "
                   f"{int(age // 60)} min (job {job}; limit {BACKLOG_MAX_AGE_S // 60})")


def _running_version():
    """The version the gateway's keyless /health reports, or None when it does not answer (the
    api check owns liveness; this check must not turn an outage into a version alert too)."""
    try:
        with urllib.request.urlopen(API_HEALTH_URL, timeout=5) as r:
            v = json.load(r).get("version")
        return str(v) if v else None
    except Exception:
        return None


def check_hermes_version():
    """The checkout is the pinned commit, and the gateway runs the pinned version. Alerting, not
    report-only, because drift breaks things silently: the v0.21.4 upgrade changed
    InProcessCronScheduler.start's signature (gpuguard would have raised on every spawn, no job
    firing, the API serving throughout) and its config migration added `connections` to both
    platform toolset lists unasked. Nothing re-runs the tests docs/HERMES_AGENT.md lists after an
    update nobody planned, and cron jobs run with a terminal (platform_toolsets.cron) that an
    agent could run `hermes update` from. DEGRADED, email only: drift proves nothing is broken,
    only that nothing is verified. The commit is the real pin: upstream merges ~660 PRs between
    version bumps, so a `hermes update` can move HEAD and still report 0.21.4."""
    tag, commit, version = HERMES_PIN
    r = _run(["git", "-C", HERMES_CHECKOUT, "rev-parse", "HEAD"])
    head = r.stdout.strip() if r and r.returncode == 0 else ""
    running = _running_version()
    bad = []
    if not head:
        bad.append("checkout commit unreadable")
    elif head != commit:
        bad.append(f"checkout at {head[:8]}, pinned {commit[:8]} ({tag})")
    if running is not None and running != version:
        bad.append(f"gateway runs {running}, pinned {version}")
    if bad:
        return False, "; ".join(bad)
    return True, f"{tag} at {commit[:8]}, gateway runs {running or 'unknown (no answer)'}"


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


def _checkers():
    # Looked up at call time, so a test can replace any check_* on the module.
    return {"gateway": check_gateway, "api": check_api, "delivery": check_delivery,
            "backup": check_backup, "flightclaw": check_flightclaw,
            "pubgate": check_public_gate, "pubquota": check_public_quota,
            "ticker": check_ticker, "gwrestarts": check_gateway_restarts,
            "backlog": check_backlog, "hermesver": check_hermes_version}


def probe_all(budget_s=None):
    """Every alerting check as a health_alert.Check, in CHECKS order, all probed concurrently and
    bounded by budget_s (PROBE_BUDGET_S) together. A failure is "down" unless SEVERITY says
    otherwise, a probe error or timeout included: it is that check's failure, at that check's
    severity. A check still running at the deadline is DOWN
    "did not finish": the probes had only per-call timeouts, and os.path.getmtime on the USB SSD
    has none, so one hung probe used to hold the whole run until systemd killed it. The pool is
    not joined (a thread stuck in the kernel cannot be), which is why __main__ ends in os._exit."""
    budget_s = PROBE_BUDGET_S if budget_s is None else budget_s
    fns = _checkers()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(CHECKS))
    futs = {key: pool.submit(fns[key]) for key in CHECKS}
    concurrent.futures.wait(futs.values(), timeout=budget_s)
    pool.shutdown(wait=False, cancel_futures=True)
    out = []
    for key, (label, where) in CHECKS.items():
        f = futs[key]
        if not f.done():
            ok, detail = False, (f"probe did not finish within {budget_s:g} s "
                                 f"(host overloaded or hung)")
        else:
            try:
                ok, detail = f.result()
            except Exception as e:                             # belt over each check's braces
                ok, detail = False, f"checker error: {type(e).__name__}: {e}"
        out.append(ha.Check(key=key, label=label, status="ok" if ok else SEVERITY.get(key, "down"),
                            detail=detail, where=where))
    return out


_WORD = {"ok": "OK  ", "degraded": "WARN", "down": "FAIL"}   # the journal line's verdict column


def _policy(key):
    return POLICY.get(key, (CONFIRM_AFTER, RECOVER_AFTER))


def _confirm(key):
    return _policy(key)[0]


def _progress(r, entry, owed):
    """Where a check stands, for the journal line: confirming, alerting, alerted, recovering."""
    if r.status == "ok":
        if r.key in owed:
            return "  [recovered, notifying]"
        if entry.get("alerted"):
            return (f"  [alerted; {entry.get('ok_streak', 0)} of {_policy(r.key)[1]} ok runs "
                    f"to recover]")
        return ""
    if entry.get("alerted"):
        return f"  [alerted {time.strftime('%m-%d %H:%M', time.localtime(entry['alerted_at']))}]" \
            if entry.get("alerted_at") else "  [alerted]"
    if r.key in owed:
        return "  [confirmed, alerting]"
    return f"  [{entry.get('fail_streak', 0)} of {_confirm(r.key)} runs, not alerted]"


def notify(results, dry=False, now=None, sender=None, log=print):
    """health_alert.run() for checks that do not all share one policy.

    run() takes a single confirm_after and recover_after, and the POLICY checks need 1 and 1 where
    the rest need 2 and 2, so this is run()'s cycle spelled out: one load, one evaluate() per
    policy, finish_run(), then ONE notification for the events of every group (health_alert.deliver:
    mark_delivered only after a successful send, then one save). A dry run neither sends nor
    saves: a saved dry run would count as one of the confirming runs (or, healthy, reset the
    streak), so a test drive would change when the real alert goes out. log=None silences it.
    -> health_alert.RunResult."""
    log = log or (lambda *_a, **_k: None)
    now = time.time() if now is None else float(now)
    state = ha.load_state(STATE_FILE, SOURCE)
    # migrate() files a canary-shaped file under "legacy" for a single-check run to adopt, and one
    # of the per-group evaluate() calls below is single-check. The watchdog never owns that shape.
    state.pop("legacy", None)
    events = []
    for conf, rec in sorted({_policy(r.key) for r in results}):
        events += ha.evaluate(state, [r for r in results if _policy(r.key) == (conf, rec)], now,
                              confirm_after=conf, recover_after=rec, max_gap_s=MAX_GAP_S)
    order = [r.key for r in results]
    events.sort(key=lambda ev: order.index(ev.check.key))
    events += ha.finish_run(state, now, where=UNIT)

    owed = {ev.check.key for ev in events}
    for r in results:
        entry = state["checks"].get(r.key, {})
        log(f"[{SOURCE}] {r.key:10} {_WORD[r.status]} {r.detail}"
            f"{_progress(r, entry, owed)}")

    res = ha.deliver(SOURCE, state, STATE_FILE, events, results=results, handle=WATCHDOG_HANDLE,
                     inspect=INSPECT, now=now, dry_run=dry, sender=sender, log=log)
    if dry and res.notification:
        log(f"[{SOURCE}]   subject: {res.notification.subject}")
    return res


def main(argv=None):
    """-> exit code. A real run: take the lock, mark the run in flight (health_alert.begin), probe
    under PROBE_BUDGET_S, alert, save. Never os._exit here: tests call main()."""
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv
    with (contextlib.nullcontext(True) if dry else ha.exclusive(STATE_FILE)) as held:
        if held is False:
            print(f"[{SOURCE}] another run is in progress ({STATE_FILE}.lock); leaving it to "
                  f"that one")
            return 0
        try:
            if not dry:
                ha.begin(SOURCE, STATE_FILE, label=RUNS_LABEL, unit=UNIT,
                         timeout_s=TIMEOUT_START_S, handle=WATCHDOG_HANDLE, inspect=INSPECT,
                         max_gap_s=MAX_GAP_S)
            results = probe_all()
            res = notify(results, dry=dry)
        except Exception as e:
            # The engine guards its own rendering and delivery; reaching here is a bug. Exit
            # non-zero so it shows in `systemctl --user --failed` instead of passing for a quiet
            # night.
            print(f"[{SOURCE}] alerting failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    print(f"[{SOURCE}] info: {', '.join(report_only())}")
    # An unsaved state file forgets the alerted flags, so the next run would alert again.
    return 1 if res.saved is False else 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Not sys.exit: interpreter shutdown joins every worker thread, and a probe stuck in
    # uninterruptible sleep (the 2026-09-24 case) would hold the process until systemd killed it,
    # after its state was already saved. The state is saved and the output flushed by now.
    os._exit(rc)

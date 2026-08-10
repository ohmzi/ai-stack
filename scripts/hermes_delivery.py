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
import fcntl
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
# Per-user results routing. Job ownership is recorded by the pipe at creation time (it is the only
# component that knows which OpenWebUI user asked); this side reads it to decide WHICH channel a
# job's results belong in. Both files live in the OpenWebUI config dir because that is the one path
# the container and the host share.
#   job_owners.json     {job_id: {"h": handle, ...}}         written by pipes/auto_assistant.py
#   owner_channels.json {handle: "<channel webhook url>"}    written by hand, one line per user
# A job with no owner, or an owner with no channel, falls back to WEBHOOK_FILE. That fallback is
# deliberate: a routing miss must never DROP a result. Keep the shared channel admin-only so the
# fallback leaks to admins rather than to everyone.
OWNERS_FILE = os.environ.get(
    "TASK_OWNERS", "/volume1/docker/openwebui/config/alerts/job_owners.json")
OWNER_CHANNELS_FILE = os.environ.get(
    "OWNER_CHANNELS", "/volume1/docker/openwebui/config/alerts/owner_channels.json")
# Recipient is a bare handle; the legacy alerts- prefix is still accepted and stripped, because
# jobs created before 2026-07-30 spell it that way and must keep working.
# Alert delivery is retried and recorded. A "sent" that nobody received is the failure mode this
# whole subsystem keeps hitting, so every attempt is written down: what was tried, when, to which
# channel, and exactly why it failed. MAX_ATTEMPTS counts the first try plus retries.
ALERT_STATE = os.path.join(OUT_DIR, ".alerts.json")        # in-flight queue (mutable)
ALERT_LEDGER = os.path.join(OUT_DIR, "alert_ledger.jsonl")  # append-only history (never rewritten)
# Tombstones from out-of-band cancellation (the email cancel link): {job_id: iso_ts}, written by
# cancel_service. This tick consults them because it CANNOT trust an external edit to .alerts.json:
# the queue is loaded at tick start and saved at tick end with no lock, so a purge landing mid-tick
# is silently overwritten — last writer wins, and the purged entries rise from the dead. A
# tombstone the tick reads itself makes the kill stick no matter who wrote last.
CANCELLED = os.path.join(OUT_DIR, ".cancelled.json")
CANCELLED_TTL_S = 7 * 86400  # far past MAX_ATTEMPTS * RETRY_AFTER_S — no queue entry lives longer
MAX_ATTEMPTS = 4          # initial + 3 retries, as requested
RETRY_AFTER_S = 300       # 5 minutes between attempts

ALERT_RE = re.compile(r"^ALERT\((?:alerts-)?([a-z0-9_-]+)\):\s*(.+)$", re.M)
LOG_RE = re.compile(r"^LOG:\s*(.+)$", re.M)
# A structured alert. Jobs that can describe WHAT happened emit this alongside the plain sentence,
# and it is what produces a well-written text and a laid-out email instead of a sliced-up log line.
# Both forms are emitted so that a watcher which did not understand this line would still deliver
# something — the plain line is dropped below whenever the structured one was understood.
ALERT_DATA_RE = re.compile(r"^ALERT_DATA:\s*(\{.*\})\s*$", re.M)


RECIPIENT_RE = re.compile(r"^[a-z0-9_-]+$")

# hermes's own "nothing to report" token. Anchored and allowed only alongside markdown punctuation
# or a trailing sentence, so it matches a genuinely empty run and not a real result that mentions it.
SILENT_RE = re.compile(r"^[\s#*_>-]*\[SILENT\][\s.*_>-]*$", re.I)


def parse_output(text):
    """(log_line, [(who, message), ...], [(who, payload), ...]) from one run's markdown output."""
    body = text.split("## Response", 1)[-1]

    # hermes prefixes EVERY cron prompt with its own convention: "If there is genuinely nothing new
    # to report, respond with exactly [SILENT]". That collides head-on with our brief, which demands
    # a LOG line on every run — so a job obeying its own runtime scored as a protocol violation and
    # posted the loud warning below. It has already happened
    # (~/.hermes/cron/output/aa92f2b114b7/2026-07-30_23-51-42.md).
    #
    # A bare [SILENT] means the job ran and had nothing to say: post nothing, warn about nothing.
    # Deliberately narrow — only a response that is ESSENTIALLY nothing but the token qualifies, so
    # a run that reports a real measurement and happens to contain the word is still parsed normally.
    if SILENT_RE.match(body.strip()):
        return None, [], []

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
    payloads = []
    for raw in ALERT_DATA_RE.findall(body)[:3]:      # cap: injection
        try:
            d = json.loads(raw)
        except Exception:
            continue
        who = str(d.get("to") or "").strip().lower()
        if RECIPIENT_RE.match(who):
            payloads.append((who, d))
    structured = {who for who, _ in payloads}
    found = [(who, msg.strip()) for who, msg in ALERT_RE.findall(body)][:3]  # cap: injection
    # Drop the plain line for any recipient whose structured payload was understood — otherwise the
    # same event is delivered twice, seconds apart.
    found = [(who, msg) for who, msg in found if who not in structured]
    alerts = [a for a in found if not is_template(a[1])]
    if len(alerts) < len(found):
        # Never drop it quietly — a suppressed alert must be as visible as a delivered one, or a
        # broken job looks like a calm one.
        note = ("⚠️ suppressed %d un-substituted ALERT template(s) — the run echoed the protocol "
                "instead of filling it in; NOT sent" % (len(found) - len(alerts)))
        log = f"{log} | {note}" if log else note
    return log, alerts, payloads


# A model that echoes the protocol template instead of filling it in produces a syntactically
# perfect ALERT line whose body is the instruction text. One reached a real phone as
# "<what happened, with the number>   (ONLY in a run where the user's alert condition holds)".
# Nothing downstream can tell that from a real alert, so it has to die here: an alert body still
# carrying an angle-bracket placeholder, or the brief's own parenthetical, was never substituted.
TEMPLATE_RE = re.compile(r"<[a-z][a-z ,'-]{2,}>|ONLY in a run where|<username>|<msg>", re.I)


def is_template(msg):
    return bool(TEMPLATE_RE.search(msg))


JOBS_FILE = os.path.expanduser("~/.hermes/cron/jobs.json")


NAMES_CACHE = os.path.join(OUT_DIR, ".job_names.json")


def job_facts():
    """{job_id: {"name":…, "schedule":…}}, from the scheduler and a cache of what it used to hold.

    The schedule is read here rather than trusted from the job's own output because a job carries
    whatever schedule it was created with, baked into its prompt. Reschedule it — "change this to
    every 5 minutes" — and the run keeps reporting the old one, so the email says "Checked every 6h"
    about a monitor now running every 5 minutes. The scheduler is the only thing that knows.

    The cache exists because a finite job DELETES ITSELF when its last run completes, and the
    watcher reads the scheduler a minute later — so the final post of every bounded monitor lost
    its name and went out as "🤖 acdf3fbb8b6d:". The last message about a task is the one most
    worth labelling.
    """
    live = {jid: {"name": (j.get("name") or "").strip() or jid,
                  "schedule": j.get("schedule_display") or ""}
            for jid, j in _jobs().items()}
    try:
        with open(NAMES_CACHE) as f:
            cached = json.load(f)
    except Exception:
        cached = {}
    if live:
        cached.update(live)
        try:
            tmp = NAMES_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cached, f)
            os.replace(tmp, NAMES_CACHE)
        except Exception:
            pass                      # a cache that cannot be written must never stop delivery
    merged = dict(cached)
    merged.update(live)               # the scheduler always wins where both know
    return merged


def _jobs():
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    jobs = data if isinstance(data, list) else data.get("jobs", data)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}


def job_titles():
    """{job_id: human name} from the scheduler's own store.

    Channel posts and email subjects read "a433462a8b25" today, which tells the user nothing about
    which of their monitors just fired. The scheduler already knows the name they gave it.
    """
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    jobs = data if isinstance(data, list) else data.get("jobs", data)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out = {}
    for j in jobs:
        if isinstance(j, dict) and j.get("id"):
            out[j["id"]] = (j.get("name") or "").strip() or j["id"]
    return out


def _read_map(path):
    """A JSON object from disk, or {} — never raises. A corrupt map degrades this to the shared
    channel, which is the safe direction: results still arrive, just not privately."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def channel_for(job_id):
    """(webhook_url, owner_handle_or_None) for this job's results.

    Falls back to the shared webhook whenever ownership or routing is unknown. Dropping a result
    would be worse than posting it to the admin channel: the user is waiting on the answer, and a
    silently swallowed run is the failure mode this whole subsystem was built to stop.
    """
    owner = (_read_map(OWNERS_FILE).get(job_id) or {}).get("h")
    if owner:
        url = (_read_map(OWNER_CHANNELS_FILE).get(owner) or "").strip()
        if url:
            return url, owner
    return open(WEBHOOK_FILE).read().strip(), None


def post_channel(summary, job_name, job_id=None):
    url, owner = channel_for(job_id) if job_id else (open(WEBHOOK_FILE).read().strip(), None)
    # The owner's own channel needs no name tag; the shared fallback does, so an admin reading it
    # can tell whose unrouted job they are looking at.
    data = json.dumps({"content": f"🤖 {job_name}: {summary}"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200


def _plain(payload):
    """A readable one-liner for a structured payload — the channel log and the ledger both want a
    sentence, not JSON, and it is the fallback if template rendering ever fails."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import alert_templates
        return alert_templates.render_sms(payload, limit=10_000)
    except Exception:
        return f"{payload.get('kind', 'alert')} on {payload.get('url') or 'your monitor'}"


def send_alert(recipient, message, job=None, job_id=None, when=None, payload=None):
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
        ok, notes = _send(recipient, message, job=job, job_id=job_id, when=when, payload=payload)
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
    ok, notes = send_alert(entry["recipient"], entry["message"],
                           job=entry.get("job"), job_id=entry.get("job_id"),
                           when=entry.get("created"), payload=entry.get("payload"))
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


def apply_cancel_tombstones(state):
    """Kill queued alerts for jobs cancelled out-of-band. Returns how many were killed.

    Entries are marked "cancelled" rather than deleted so the queue file stays a legible history
    of what happened, and each kill gets a ledger row — an alert that silently stopped retrying
    would look exactly like the delivery bug this subsystem exists to prevent. Tombstones older
    than CANCELLED_TTL_S are pruned here (the service only ever appends); an unparseable
    timestamp is pruned too, since anything a week of retries cannot outlive protects nothing.
    A missing or corrupt tombstone file is a no-op — cancellation is an optional feature and this
    is the delivery path.
    """
    # The read and the prune-write both happen under the lock cancel_service takes: they are two
    # unsynchronized read-modify-writes on the same file otherwise, and a prune that started
    # before a cancel landed would write back the pre-image — erasing a tombstone before it was
    # ever applied, which is the exact resurrection this file went to the trouble of preventing.
    lock = None
    try:
        lock = open(CANCELLED + ".lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except Exception:
        pass
    try:
        return _apply_cancel_tombstones(state)
    finally:
        if lock:
            lock.close()


def _apply_cancel_tombstones(state):
    try:
        stones = json.load(open(CANCELLED))
        stones = stones if isinstance(stones, dict) else {}
    except Exception:
        return 0
    killed = 0
    for entry in state.values():
        if entry.get("status") == "pending" and entry.get("job_id") in stones:
            entry["status"] = "cancelled"
            killed += 1
            ledger_write({"at": _iso(_now()), "job": entry.get("job"),
                          "recipient": entry.get("recipient"), "ok": False,
                          "notes": ["cancelled via email link before delivery"],
                          "message": (entry.get("message") or "")[:200]})
            print(f"  alert[{entry.get('recipient')}] cancelled via email link — not sent")
    keep = {}
    for jid, ts in stones.items():
        try:
            if _now() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) < CANCELLED_TTL_S:
                keep[jid] = ts
        except Exception:
            pass
    if keep != stones:
        try:
            tmp = CANCELLED + ".tmp"
            with open(tmp, "w") as f:
                json.dump(keep, f, indent=1)
            os.replace(tmp, CANCELLED)
        except Exception as e:
            print(f"  tombstone prune failed: {e}", file=sys.stderr)
    return killed


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
        try:
            with open(STATE) as f:
                raw = json.load(f)
        except Exception as e:
            # A truncated state file used to raise here and abort the tick BEFORE any output was
            # examined — every minute, forever, with no LOG, no ALERT, no ledger row and no notice.
            # The subsystem would be dead while every record it keeps stayed silent about it.
            # Starting from empty re-posts recent runs, which is noisy; being deaf is not survivable.
            print(f"⚠️ {STATE} unreadable ({e}) — starting from empty; recent runs may re-post",
                  file=sys.stderr)
            try:
                os.replace(STATE, STATE + ".corrupt")
            except Exception:
                pass
            raw = {}
        # Migrate the original list format (fully-delivered files).
        state = ({f: {"log": True, "alerts": True} for f in raw} if isinstance(raw, list) else raw)
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*", "*.md")))
    pending = [f for f in files
               if not (state.get(f, {}).get("log") and state.get(f, {}).get("alerts"))]
    if not pending and not any(e.get("status") == "pending" for e in alert_state.values()):
        print("nothing new")
        return 0

    facts = job_facts()
    for f in pending:
        job_id = os.path.basename(os.path.dirname(f))
        job_name = facts.get(job_id, {}).get("name", job_id)
        log, alerts, payloads = parse_output(open(f).read())
        if a.dry_run:
            print(f"{f}: LOG={log!r} ALERTS={alerts} DATA={[p for _, p in payloads]}")
            continue
        st = state.setdefault(f, {"log": False, "alerts": False})
        # Each leg retries independently — a failed push must never re-post the channel log.
        if not st["log"]:
            try:
                if log:
                    post_channel(log, job_name, job_id)
                st["log"] = True
            except Exception as e:
                print(f"channel retry later {f}: {e}", file=sys.stderr)
        # Alerts go through the retry queue rather than a single fire-and-forget send.
        if not st["alerts"]:
            for who, msg in alerts:
                k = alert_key(f, who, msg)
                if k not in alert_state:
                    alert_state[k] = {"job": job_name, "job_id": job_id, "recipient": who,
                                      "message": msg, "created": _iso(_now()), "attempts": [],
                                      "status": "pending", "next_attempt": 0}
            for who, data in payloads:
                k = alert_key(f, who, json.dumps(data, sort_keys=True))
                if k not in alert_state:
                    # Fill in what the job could not know about delivery itself, so the rendered
                    # email can say "a text went to ..." truthfully.
                    data.setdefault("monitor", job_name)
                    live_sched = facts.get(job_id, {}).get("schedule")
                    if live_sched:
                        data["schedule"] = live_sched      # scheduler wins over the baked-in value
                    alert_state[k] = {"job": job_name, "job_id": job_id, "recipient": who,
                                      "message": _plain(data), "payload": data,
                                      "created": _iso(_now()), "attempts": [],
                                      "status": "pending", "next_attempt": 0}
            st["alerts"] = True
        print(f"{os.path.basename(f)}: log={'y' if st['log'] else 'RETRY'} "
              f"alerts={'y' if st['alerts'] else 'RETRY'} ({len(alerts)})")

    if not a.dry_run:
        # Tombstones first, THEN the drain: an entry parsed seconds ago for a job the user just
        # cancelled from the email must die in this same tick, not send once and die later.
        apply_cancel_tombstones(alert_state)
        # Drain the retry queue every tick, then persist both state files.
        for entry in process_alert_queue(alert_state):
            try:
                # Routed to the owner's channel too: an undeliverable alert is news for the person
                # who is waiting on it, not only for whoever reads the shared log.
                post_channel(f"⚠️ ALERT UNDELIVERABLE after {MAX_ATTEMPTS} attempts "
                             f"(last error: {entry['attempts'][-1]['notes']}): {entry['message']}",
                             entry["job"], entry.get("job_id"))
            except Exception:
                pass
        save_alert_state(alert_state)
        # tmp+rename, matching save_alert_state. Truncating the live file in place leaves a
        # half-written state if the process dies mid-write — which is exactly how the read above
        # came to need a guard.
        _tmp = STATE + ".tmp"
        with open(_tmp, "w") as _f:
            json.dump(state, _f)
        os.replace(_tmp, STATE)
        # Refresh the non-secret profile the OpenWebUI pipe reads to describe alert delivery.
        # Done here because this is the one process that runs on the host every minute AND can
        # read ~/.hermes — so the pipe's view can never drift from the live config.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from alert_transports import publish_profile
            publish_profile()
        except Exception as e:
            print(f"profile publish skipped: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

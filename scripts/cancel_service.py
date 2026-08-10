#!/usr/bin/env python3
"""The page behind the email's cancel link: manage ONE monitor with a token, no login.

Why this exists. Alert emails carry "Cancel this monitor" — a link that must work from a phone on
any network, held by someone with no session anywhere. The token in the link is the entire
authorization (see cancel_tokens.py), so this service is deliberately tiny: it can render a page
for one job, pause or resume that job, and cancel it. It cannot list jobs, reach any other Hermes
route, or be talked into a different job id than the one signed into the token.

The GET/POST split is not a style choice. Mail scanners and link-preview bots FETCH every link in
an email — sometimes before the user has seen it. A GET that cancelled would let Outlook's
prefetcher kill the monitor unread. So GET only shows the job and its buttons; every mutation is
a POST from an explicit human press, and the destructive one is worded as such.

Cancel here does the FULL cleanup the chat path historically skipped, in a fixed order:
read the job first (its prompt is the only record of the watcher's --state slug and --route-id,
and it dies with the job), then DELETE in Hermes (404 = already gone = success, verified by
re-listing rather than trusting the API's word), then the legs that outlive a deleted job:
the ownership record in job_owners.json, queued alert retries (flipped to "cancelled" AND
tombstoned in .cancelled.json — the delivery tick has no file lock, so only a tombstone it reads
itself survives a last-writer-wins rewrite), watcher state files, and the FlightClaw route for a
fare watch. Each leg is independent and best-effort: a failed leg is reported, never fatal.

Runs on loopback only; the Cloudflare tunnel is the sole way in and TLS ends at the edge. The
config (CANCEL_SECRET, token lifetime) is re-read per request so rotating the secret needs no
restart — and rotation is the kill switch for every outstanding link at once.

Usage:  python3 scripts/cancel_service.py            (systemd unit: cancel-service.service)
"""
import fcntl
import glob
import html
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cancel_tokens

# Paths and endpoints. Plain module globals so tests can point the whole service at a fake tree
# and a fake Hermes the way test_hermes_delivery.py does.
CONF = os.path.expanduser("~/.hermes/alert_transports.env")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")
HERMES_API = "http://127.0.0.1:8642"
OWNERS_FILE = os.environ.get("TASK_OWNERS",
                             "/volume1/docker/openwebui/config/alerts/job_owners.json")
OUT_DIR = os.path.expanduser("~/.hermes/cron/output")
STATE_DIR = os.path.expanduser("~/.hermes/monitor-state")
FC_MCP = "http://127.0.0.1:8765/mcp"
BIND, PORT = "127.0.0.1", 8096
TIMEOUT = 10
# Where someone who arrived here without a link should actually be going. Overridable with
# ASSISTANT_URL in alert_transports.env, because the hostname is deployment trivia and this file
# should not have to be edited to move it.
ASSISTANT_URL = "https://ai.ohmz.cloud"
REDIRECT_S = 5

_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
# The watcher's --state slug comes out of a job prompt, which an agent wrote — semi-trusted at
# best. It names files under STATE_DIR and NOTHING may let it name a path: no slash, no leading
# dot, bounded length.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_ROUTE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Failed-verify limiter. The 160-bit HMAC is the real defense; this only keeps garbage cheap.
# Module globals rather than tunables-in-conf so tests can collapse the window.
FAIL_LIMIT = 30          # failed verifies per minute before 429
FAIL_SLEEP_S = 0.25      # tax on every failure
_fails = {"start": 0.0, "n": 0}
_fails_lock = threading.Lock()

# The brand palette, from the one module that already owns it. The fallback copy exists because
# this is a standalone service: an edit to alert_templates must not be able to take the cancel
# page down with it.
try:
    from alert_templates import _OHMZ as C
except Exception:
    C = {"canvas": "#1a1917", "panel": "#211f1d", "raise": "#262421",
         "line": "#3a3733", "line_soft": "#302d2a",
         "fg": "#f0edea", "secondary": "#cbc5be",
         "amber": "#e0913f", "on_amber": "#241f18"}


def _env(path):
    """KEY=VALUE lines, same grammar as alert_transports.load_conf. Missing file => {}."""
    conf = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = re.split(r"\s+#", v, 1)[0].strip()
    except Exception:
        pass
    return conf


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


# ---------------------------------------------------------------- hermes + flightclaw clients

def hermes(method, path):
    """(status, decoded json). status 0 means the gateway was unreachable — distinct from any
    HTTP answer, because "Hermes said no" and "Hermes wasn't there" produce different pages."""
    key = _env(HERMES_ENV).get("API_SERVER_KEY", "")
    req = urllib.request.Request(HERMES_API + path, method=method,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _mcp_post(body, session_id=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(FC_MCP, method="POST", data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        sid = r.headers.get("mcp-session-id") or session_id
        raw = r.read().decode()
        if "text/event-stream" in (r.headers.get("content-type") or ""):
            msgs = [json.loads(ln[5:].strip()) for ln in raw.splitlines()
                    if ln.startswith("data:")]
            return (msgs[-1] if msgs else None), sid
        return (json.loads(raw) if raw.strip() else None), sid


def fc_remove_tracked(route_id):
    """Untrack a fare route in FlightClaw. The MCP handshake mirrors the pipe's _fc_call —
    initialize, initialized, tools/call — because that is the shape the server actually speaks."""
    msg, sid = _mcp_post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "cancel_service", "version": "1.0"}}})
    _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
              session_id=sid)
    msg, _ = _mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "remove_tracked",
                                   "arguments": {"route_id": route_id}}}, session_id=sid)
    if msg and msg.get("error"):
        raise RuntimeError(str(msg["error"])[:200])
    content = ((msg or {}).get("result") or {}).get("content") or []
    return content[0].get("text", "") if content else ""


# ---------------------------------------------------------------- job facts + cleanup legs

def get_job(jid):
    """(status, job-dict-or-{}) for one id."""
    status, data = hermes("GET", f"/api/jobs/{jid}")
    return status, (data or {}).get("job") or {}


def cached_name(jid):
    """The last name a deleted job was known by, from the delivery watcher's cache."""
    try:
        return json.load(open(os.path.join(OUT_DIR, ".job_names.json")))[jid]["name"]
    except Exception:
        return None


def prompt_args(prompt):
    """--state and --route-id lifted from the job's own vetted command line. The authoring side
    always shell-quotes values, so shlex round-trips them; a prompt shlex chokes on yields {}
    rather than a guess — missing cleanup is recoverable, deleting a wrong path is not."""
    try:
        toks = shlex.split(prompt or "")
    except ValueError:
        return {}
    out = {}
    for i, t in enumerate(toks[:-1]):
        if t == "--state":
            out["state"] = toks[i + 1]
        elif t == "--route-id":
            out["route_id"] = toks[i + 1]
    return out


def _rewrite_json(path, mutate):
    """Read-mutate-write with tmp+rename. Returns mutate's result, or None on any failure.

    Nothing here may raise: every caller runs AFTER the job has already been deleted in Hermes,
    where an exception would turn a completed cancel into a 500 page and lose the report of what
    else got cleaned. A leg that cannot run is a leg that did not run.
    """
    try:
        data = json.load(open(path))
        result = mutate(data)
        if result is None:
            return None
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
        return result
    except Exception as e:
        print(f"cleanup leg failed on {path}: {e}", file=sys.stderr)
        return None


def tombstone_lock(path):
    """Exclusive lock for a tombstone read-modify-write, held across the tmp+rename.

    Two processes edit .cancelled.json: this service appends, and the delivery tick prunes
    expired entries. Both are read-modify-write, and without a lock a prune that started before
    a cancel lands can write back the pre-image — deleting a tombstone that was never applied,
    which is exactly the resurrection the tombstone exists to prevent. The lock is a sidecar file
    so it survives the os.replace that swaps the tombstone file itself.
    """
    fh = open(path + ".lock", "a+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def full_cancel(jid, handle):
    """The complete cancellation. Returns (outcome, name, details) where outcome is "cancelled",
    "gone" (was already cancelled), or "error" (Hermes refused or vanished — nothing cleaned,
    because half-cleaning a job that still runs would orphan a live monitor's state)."""
    status, job = get_job(jid)
    if status == 0:
        return "error", None, []
    name = job.get("name") or cached_name(jid)
    args = prompt_args(job.get("prompt")) if job else {}
    already_gone = status == 404

    if not already_gone:
        del_status, _ = hermes("DELETE", f"/api/jobs/{jid}")
        # An ALLOWLIST, not a blocklist of known-bad codes: cleanup deletes the only records of a
        # monitor, so an unfamiliar answer (409, 429, a proxy's 502) must mean "leave everything
        # alone", never "assume it worked". 404 is success — the job was already gone.
        if del_status not in (200, 202, 204, 404):
            return "error", name, []
        # The house rule from the chat path: report what the scheduler says, not what the API
        # claimed. A job still listed after a 2xx DELETE is a lie worth surfacing.
        list_status, data = hermes("GET", "/api/jobs?include_disabled=true")
        if list_status == 200 and any((j or {}).get("id") == jid
                                      for j in (data or {}).get("jobs") or []):
            return "error", name, ["Hermes accepted the cancel but still lists the job — "
                                   "nothing was cleaned up; try again in a minute"]

    details = ["schedule removed" if not already_gone else "schedule was already gone"]

    def drop_owner(owners):
        return owners.pop(jid, None) is not None or None
    if _rewrite_json(OWNERS_FILE, drop_owner):
        details.append("ownership record cleared")

    flipped = []

    def flip(state):
        for entry in state.values():
            if entry.get("status") == "pending" and entry.get("job_id") == jid:
                entry["status"] = "cancelled"
                flipped.append(entry)
        return True
    _rewrite_json(os.path.join(OUT_DIR, ".alerts.json"), flip)
    if flipped:
        details.append(f"{len(flipped)} queued alert{'s' if len(flipped) != 1 else ''} stopped")

    # The tombstone goes in even when nothing was pending: an output file parsed mid-tick can
    # add an entry AFTER this write, and the next tick's tombstone pass is what kills it.
    stones_path = os.path.join(OUT_DIR, ".cancelled.json")
    try:
        with tombstone_lock(stones_path):
            try:
                stones = json.load(open(stones_path))
                stones = stones if isinstance(stones, dict) else {}
            except Exception:
                stones = {}
            stones[jid] = _iso()
            tmp = stones_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(stones, f, indent=1)
            os.replace(tmp, stones_path)
    except Exception as e:
        print(f"tombstone write failed: {e}", file=sys.stderr)

    try:
        with open(os.path.join(OUT_DIR, "alert_ledger.jsonl"), "a") as f:
            f.write(json.dumps({"at": _iso(), "job": name or jid, "job_id": jid,
                                "recipient": handle, "ok": True,
                                "notes": ["cancelled via email link"], "message": ""}) + "\n")
    except Exception:
        pass

    slug = args.get("state")
    if slug and _SLUG_RE.match(slug):
        gone = 0
        for p in glob.glob(os.path.join(STATE_DIR, slug + ".*")):
            try:
                os.remove(p)
                gone += 1
            except OSError:
                pass
        if gone:
            details.append("watcher state removed")

    route = args.get("route_id")
    if route and _ROUTE_RE.match(route):
        try:
            reply = fc_remove_tracked(route)
            details.append("flight route untracked"
                           if "not found" not in reply.lower() else "flight route was already gone")
        except Exception as e:
            details.append("flight route cleanup failed (logged)")
            print(f"flightclaw remove_tracked failed: {e}", file=sys.stderr)

    return ("gone" if already_gone else "cancelled"), name, details


# ---------------------------------------------------------------- pages

def _assistant_url():
    """The configured assistant address, or the default. Never taken from the request — a
    redirect target that a visitor can set is an open redirect with this domain's name on it."""
    return _env(CONF).get("ASSISTANT_URL", ASSISTANT_URL)


def _page(title, inner, label="MONITOR", head=""):
    e = html.escape
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{e(title)}</title>{head}<style>
body{{margin:0;min-height:100vh;background:{C["canvas"]};color:{C["fg"]};
font-family:'Space Grotesk',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;padding:24px 12px;box-sizing:border-box;}}
.card{{max-width:440px;width:100%;background:{C["panel"]};border:1px solid {C["line_soft"]};
border-radius:12px;padding:30px 28px;}}
.label{{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
font-weight:500;letter-spacing:.12em;text-transform:uppercase;color:{C["amber"]};}}
h1{{margin:12px 0 0;font-size:20px;font-weight:600;letter-spacing:-0.02em;line-height:1.35;}}
p{{margin:12px 0 0;font-size:15px;line-height:1.6;color:{C["secondary"]};}}
.meta{{margin-top:6px;font-size:13px;color:{C["secondary"]};}}
.mono{{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
color:{C["secondary"]};margin-top:4px;}}
form{{display:inline-block;margin:20px 8px 0 0;}}
button{{font:inherit;font-size:15px;font-weight:500;padding:10px 18px;border-radius:10px;
border:1px solid transparent;cursor:pointer;}}
.primary{{background:{C["amber"]};color:{C["on_amber"]};}}
.ghost{{background:transparent;border-color:{C["line"]};color:{C["fg"]};}}
a.btn{{display:inline-block;margin-top:20px;font-size:15px;font-weight:500;padding:10px 18px;
border-radius:10px;border:1px solid transparent;text-decoration:none;}}
.count{{font-variant-numeric:tabular-nums;}}
.foot{{margin-top:26px;padding-top:14px;border-top:1px solid {C["line_soft"]};font-size:12px;
line-height:1.6;color:{C["secondary"]};}}
</style></head><body><div class="card"><div class="label">{e(label)}</div>
{inner}
<div class="foot">This link came from an alert email and manages only this monitor.</div>
</div></body></html>"""


def _forms(token, state):
    """The state-appropriate actions. Cancel is the filled button — it is what this page is FOR
    (the hostname says so) — and the reversible action is the quiet one beside it."""
    e = html.escape
    cancel = (f'<form method="post" action="/cancel">'
              f'<input type="hidden" name="t" value="{e(token)}">'
              f'<button class="primary" type="submit">Cancel this monitor</button></form>')
    other = "resume" if state == "paused" else "pause"
    label = "Resume" if other == "resume" else "Pause instead"
    ghost = (f'<form method="post" action="/{other}">'
             f'<input type="hidden" name="t" value="{e(token)}">'
             f'<button class="ghost" type="submit">{label}</button></form>')
    return cancel + ghost


def page_confirm(token, job):
    e = html.escape
    name = job.get("name") or "your monitor"
    sched = job.get("schedule_display") or ""
    paused = job.get("state") == "paused" or job.get("enabled") is False
    meta = f'<div class="meta">Checked {e(sched)}{" · currently paused" if paused else ""}</div>' \
        if sched else ('<div class="meta">currently paused</div>' if paused else "")
    lead = ("<p>Cancelling stops it for good — there is no undo. "
            "Pausing keeps it set up but quiet until you resume.</p>"
            if not paused else
            "<p>This monitor is paused: still set up, not checking, not emailing. "
            "You can resume it or cancel it for good — there is no undo on cancel.</p>")
    return _page(name, f"<h1>{e(name)}</h1>{meta}{lead}"
                       f"{_forms(token, 'paused' if paused else 'active')}",
                 label="MANAGE MONITOR")


def page_result(outcome, name, details, token=None, state=None):
    e = html.escape
    name = name or "your monitor"
    if outcome == "cancelled":
        rows = "".join(f'<div class="mono">· {e(d)}</div>' for d in details)
        return _page("Cancelled", f"<h1>Cancelled ✓</h1><p>{e(name)} will not run or email "
                                  f"again.</p>{rows}<p>There is no undo — ask the assistant if "
                                  f"you want it set up again.</p>", label="CANCELLED")
    if outcome == "gone":
        return _page("Already cancelled",
                     f"<h1>Already cancelled</h1><p>{e(name)} is not running any more. "
                     f"Nothing left to do.</p>", label="CANCELLED")
    if outcome == "paused":
        return _page("Paused", f"<h1>Paused</h1><p>{e(name)} stays set up but will not check or "
                               f"email until you resume it.</p>{_forms(token, 'paused')}",
                     label="PAUSED")
    if outcome == "resumed":
        return _page("Resumed", f"<h1>Resumed</h1><p>{e(name)} is back on its schedule.</p>"
                                f"{_forms(token, 'active')}", label="RESUMED")
    return _page("Try again", "<h1>Can't reach the scheduler</h1><p>The monitor is untouched. "
                              "Try again in a minute.</p>", label="UNAVAILABLE")


def _assistant_button(text="Open the assistant"):
    """The way out of every dead end here. A page that tells someone their link is no good and
    then leaves them on it has only described the problem."""
    return (f'<a class="btn primary" href="{html.escape(_assistant_url())}">'
            f'{html.escape(text)} &rarr;</a>')


def page_landing():
    """For someone who typed the hostname, or followed a link with no token in it.

    They are not looking at a broken link — they are in the wrong place, and the right place is
    the assistant, where every monitor can be seen and changed rather than just this one. So the
    page says that and takes them there, with the redirect declared in a <meta> so it does not
    depend on scripts, and a button for anyone who would rather not wait.
    """
    url = html.escape(_assistant_url())
    head = f'<meta http-equiv="refresh" content="{REDIRECT_S};url={url}">'
    return _page(
        "Ohmz AI",
        f'<h1>Nothing to manage here</h1>'
        f'<p>This page opens from a link in an alert email, and manages the one monitor that '
        f'email was about. There is no link, so there is nothing to show.</p>'
        f'<p>Taking you to the assistant in <span class="count" id="n">{REDIRECT_S}</span> '
        f'seconds, where you can see and change every monitor you have.</p>'
        f'{_assistant_button("Go now")}'
        f'<script>(function(){{var e=document.getElementById("n"),s={REDIRECT_S};'
        f'setInterval(function(){{if(--s>=0)e.textContent=s;}},1000);}})();</script>',
        label="OHMZ AI", head=head)


def page_invalid(why):
    if why == "expired":
        return _page("Link expired",
                     "<h1>This link has expired</h1><p>Cancel links stop working after a while "
                     "on purpose, so an old email cannot act on a monitor you have since "
                     "changed. Your monitors are all still there.</p>"
                     + _assistant_button("Manage them in the assistant"), label="EXPIRED")
    return _page("Not found",
                 "<h1>This link isn't valid</h1><p>It may be damaged, or from an email older "
                 "than the last time the keys changed. Nothing has happened to your "
                 "monitors.</p>" + _assistant_button("Manage them in the assistant"),
                 label="NOT FOUND")


# ---------------------------------------------------------------- http plumbing

def _too_many_failures():
    with _fails_lock:
        now = time.time()
        if now - _fails["start"] > 60:
            _fails["start"], _fails["n"] = now, 0
        _fails["n"] += 1
        over = _fails["n"] > FAIL_LIMIT
    time.sleep(FAIL_SLEEP_S)
    return over


def _verify(token):
    conf = _env(CONF)
    secret = conf.get("CANCEL_SECRET", "")
    try:
        days = float(conf.get("CANCEL_TOKEN_MAX_AGE_DAYS", "30"))
    except ValueError:
        days = 30.0
    return cancel_tokens.verify(token, secret, max_age_s=days * 86400)


class Handler(BaseHTTPRequestHandler):
    server_version = "cancel-service"
    # Without this, a POST that DECLARES Content-Length: 4096 and then sends nothing holds its
    # thread in rfile.read() until the client goes away — and this is a public endpoint, so
    # anyone can open those faster than they time out until ThreadingHTTPServer has a thread per
    # socket. BaseHTTPRequestHandler.timeout defaults to None, meaning wait forever; setting it
    # makes StreamRequestHandler put a real timeout on the socket.
    timeout = TIMEOUT

    def log_message(self, fmt, *args):
        # The default logger prints the request line VERBATIM — including ?t=<token>. A journal
        # entry must never be a copy of the capability it describes.
        print(re.sub(r"\?\S*", "?[redacted]", fmt % args), file=sys.stderr)

    def _send(self, status, body, ctype="text/html; charset=utf-8", head_only=False):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _token_gate(self, token):
        """(job_id, handle) or None after having answered the request."""
        jid, handle, why = _verify(token)
        if why == "ok":
            return jid, handle
        if _too_many_failures():
            self._send(429, "too many requests", ctype="text/plain; charset=utf-8")
        else:
            self._send(404, page_invalid(why))
        return None

    def do_HEAD(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/healthz", "/"):
            self._send(200, "ok", ctype="text/plain; charset=utf-8", head_only=True)
        else:
            self._send(404, "", ctype="text/plain; charset=utf-8", head_only=True)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/healthz":
            return self._send(200, "ok", ctype="text/plain; charset=utf-8")
        token = (urllib.parse.parse_qs(parts.query).get("t") or [""])[0]
        # No token at all is a person, not a forged link: someone typed the hostname or followed
        # a truncated link. Answered BEFORE the token gate so it neither reads as "your link is
        # broken" nor counts against the failed-verify limiter. A wrong path lands here too —
        # it says the same true thing, with the same way out.
        if not token:
            return self._send(200 if parts.path in ("/", "/c") else 404, page_landing())
        if parts.path != "/c":
            return self._send(404, page_invalid("bad"))
        gate = self._token_gate(token)
        if not gate:
            return
        jid, _ = gate
        status, job = get_job(jid)
        if status == 404:
            return self._send(200, page_result("gone", cached_name(jid), []))
        # Only 200 is an answer about the job. A 401 (rotated gateway key) or a 500 would
        # otherwise fall through and render a confirm page for an empty job called "your monitor".
        if status != 200:
            return self._send(503, page_result("error", None, []))
        self._send(200, page_confirm(token, job))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/cancel", "/pause", "/resume"):
            return self._send(404, page_invalid("bad"))
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
        except ValueError:
            length = 0
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        token = (urllib.parse.parse_qs(body).get("t") or [""])[0]
        gate = self._token_gate(token)
        if not gate:
            return
        jid, handle = gate

        if path == "/cancel":
            outcome, name, details = full_cancel(jid, handle)
            code = {"cancelled": 200, "gone": 200}.get(outcome, 503)
            return self._send(code, page_result(outcome, name, details))

        # pause/resume: fire, then RE-READ and render what is actually true now. A no-op press
        # (pausing a paused job) and a successful one land on the same honest page.
        #
        # The mutation's status is checked rather than discarded: labelling the page purely from
        # the state afterwards means a pause that FAILED renders "Resumed ✓" — a success page for
        # the opposite of what the user pressed. Same for a job read that comes back 401 or 500,
        # which is not an answer about the job at all; only 200 is.
        act_status, _ = hermes("POST", f"/api/jobs/{jid}{path}")
        if act_status not in (200, 201, 202, 204, 404):
            return self._send(503, page_result("error", None, []))
        status, job = get_job(jid)
        if status == 404 or act_status == 404:
            return self._send(200, page_result("gone", cached_name(jid), []))
        if status != 200:
            return self._send(503, page_result("error", None, []))
        paused = job.get("state") == "paused" or job.get("enabled") is False
        self._send(200, page_result("paused" if paused else "resumed",
                                    job.get("name"), [], token=token))


def main():
    if not _env(CONF).get("CANCEL_SECRET"):
        print("cancel_service: CANCEL_SECRET missing from alert_transports.env — refusing to "
              "serve links nobody can mint", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"cancel_service: listening on {BIND}:{PORT}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

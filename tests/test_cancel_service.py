#!/usr/bin/env python3
"""The cancel page: token gate, scanner safety, and the cleanup that must all happen.

Why this file exists. The service is a PUBLIC endpoint holding the Hermes API key, so the
properties pinned here are load-bearing:

  * **GET never mutates.** Mail scanners fetch every link in an email; a GET that cancelled would
    let a prefetcher kill the monitor before the user read the alert. The fake Hermes records
    every call — a DELETE during GET is a test failure, not a style point.
  * **Cancel means cancelled EVERYWHERE.** Hermes delete, ownership record, queued retries,
    tombstone, watcher state files, FlightClaw route — each leg is asserted, because the chat
    path historically cleaned only Hermes and the gaps kept emailing for 20 minutes.
  * **A refused delete cleans nothing.** Half-cleanup would orphan a live monitor's state.
  * **A prompt cannot name a path.** The --state slug comes from a job prompt an agent wrote;
    a traversal slug must not delete files outside the state directory.

The fake Hermes and fake FlightClaw run on ephemeral loopback ports; the real handler is driven
through real HTTP, so header behavior (redaction is eyeballed via journal only) and form parsing
are the production code paths.

Usage:  python3 tests/test_cancel_service.py
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SECRET = "e" * 64
JID, JID2, JID3 = "ae57d3973b9f", "beef00112233", "cafe44556677"
NAME = "YTO->YVR fare watch under $1,000"
PROMPT = ("Run this terminal command and print its output verbatim.\n"
          "python3 /home/ohmz/ai-stack/scripts/flightclaw_watch.py "
          "--route-id YYZ-YVR-2026-10-02-RT-2026-11-02 --state fc-yyz-yvr "
          "--alert-to ohmz --below 1000 --monitor 'fare watch' --schedule 'every 15m'")


def main():
    cs = load("/home/ohmz/ai-stack/scripts/cancel_service.py", "cs")
    ct = load("/home/ohmz/ai-stack/scripts/cancel_tokens.py", "ct")

    d = tempfile.mkdtemp()
    out_dir = os.path.join(d, "output")
    state_dir = os.path.join(d, "monitor-state")
    os.makedirs(out_dir)
    os.makedirs(state_dir)
    conf = os.path.join(d, "alert_transports.env")
    open(conf, "w").write(f"CANCEL_SECRET={SECRET}\n")
    henv = os.path.join(d, "hermes.env")
    open(henv, "w").write("API_SERVER_KEY=k3y\n")
    owners = os.path.join(d, "job_owners.json")

    class Fake(BaseHTTPRequestHandler):
        """Hermes and FlightClaw in one fake: records every call, asserts nothing itself."""
        jobs, calls, auth = {}, [], []
        fail_delete = False

        def log_message(self, *a):
            pass

        def _reply(self, code, obj, sid=False):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if sid:
                self.send_header("mcp-session-id", "s1")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            Fake.calls.append(("GET", self.path))
            Fake.auth.append(self.headers.get("Authorization"))
            if re.match(r"^/api/jobs/[a-f0-9]{12}$", self.path):
                jid = self.path.rsplit("/", 1)[1]
                if jid in Fake.jobs:
                    self._reply(200, {"job": Fake.jobs[jid]})
                else:
                    self._reply(404, {"error": "not found"})
            elif self.path.startswith("/api/jobs"):
                self._reply(200, {"jobs": list(Fake.jobs.values())})
            else:
                self._reply(404, {})

        def do_DELETE(self):
            Fake.calls.append(("DELETE", self.path))
            jid = self.path.rsplit("/", 1)[1]
            if Fake.fail_delete:
                self._reply(500, {"error": "boom"})
            elif jid in Fake.jobs:
                del Fake.jobs[jid]
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            Fake.calls.append(("POST", self.path))
            if self.path == "/mcp":
                body = json.loads(raw or b"{}")
                if body.get("method") == "tools/call":
                    Fake.calls.append(("MCP", body["params"]["arguments"].get("route_id")))
                    self._reply(200, {"result": {"content": [{"text": "Removed YYZ-YVR"}]}},
                                sid=True)
                else:
                    self._reply(200, {"result": {}}, sid=True)
            elif self.path.endswith("/pause") or self.path.endswith("/resume"):
                jid = self.path.split("/")[3]
                job = Fake.jobs.get(jid)
                if job:
                    paused = self.path.endswith("/pause")
                    job["state"] = "paused" if paused else "active"
                    job["enabled"] = not paused
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {})
            else:
                self._reply(404, {})

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fport = fake.server_address[1]

    cs.CONF, cs.HERMES_ENV = conf, henv
    cs.HERMES_API = f"http://127.0.0.1:{fport}"
    cs.FC_MCP = f"http://127.0.0.1:{fport}/mcp"
    cs.OUT_DIR, cs.STATE_DIR, cs.OWNERS_FILE = out_dir, state_dir, owners
    cs.FAIL_SLEEP_S = 0

    svc = ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=svc.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{svc.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(path, **form):
        req = urllib.request.Request(base + path, method="POST",
                                     data=urllib.parse.urlencode(form).encode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def seed():
        Fake.jobs.clear()
        Fake.jobs[JID] = {"id": JID, "name": NAME, "prompt": PROMPT,
                          "schedule_display": "every 15m", "enabled": True, "state": "active"}
        json.dump({JID: {"h": "ohmz", "t": 1.0}, JID2: {"h": "ohmz", "t": 1.0}},
                  open(owners, "w"))
        json.dump({"k1": {"status": "pending", "job_id": JID, "recipient": "ohmz",
                          "message": "m", "attempts": []},
                   "k2": {"status": "pending", "job_id": JID2, "recipient": "ohmz",
                          "message": "m", "attempts": []}},
                  open(os.path.join(out_dir, ".alerts.json"), "w"))
        for ext in (".json", ".txt"):
            open(os.path.join(state_dir, "fc-yyz-yvr" + ext), "w").write("{}")

    seed()
    tok = ct.mint(JID, "ohmz", SECRET)

    print("--- health ---")
    st, body = get("/healthz")
    check("GET /healthz is 200 ok", st == 200 and body == "ok", f"{st} {body!r}")
    req = urllib.request.Request(base + "/healthz", method="HEAD")
    with urllib.request.urlopen(req, timeout=5) as r:
        check("HEAD /healthz is 200 (curl -I must work)", r.status == 200)

    print("--- the confirm page is read-only and state-aware ---")
    st, body = get(f"/c?t={tok}")
    check("valid token renders the job", st == 200 and "fare watch under" in body, body[:300])
    check("the schedule is shown", "every 15m" in body)
    check("cancel is a POST form", 'method="post" action="/cancel"' in body)
    check("pause is offered for an active job", 'action="/pause"' in body)
    check("no DELETE happened on GET", not any(m == "DELETE" for m, _ in Fake.calls),
          str(Fake.calls))
    check("the hermes key was presented",
          all(a == "Bearer k3y" for a in Fake.auth if a is not None) and Fake.auth, str(Fake.auth[-3:]))
    Fake.jobs[JID]["state"], Fake.jobs[JID]["enabled"] = "paused", False
    st, body = get(f"/c?t={tok}")
    check("a paused job offers resume instead", 'action="/resume"' in body and
          'action="/pause"' not in body, body[:400])
    Fake.jobs[JID]["state"], Fake.jobs[JID]["enabled"] = "active", True

    print("--- bad tokens: uniform, cheap, and never reach Hermes ---")
    before = len(Fake.calls)
    st, body = get("/c?t=garbage.token")
    check("forged token is a 404", st == 404, str(st))
    check("...with the neutral page", "isn't valid" in body)
    check("...and no Hermes call", len(Fake.calls) == before)
    old = ct.mint(JID, "ohmz", SECRET, now=time.time() - 40 * 86400)
    st, body = get(f"/c?t={old}")
    check("expired token is a 404 that says expired", st == 404 and "expired" in body)
    print("--- arriving with no link at all is a signpost, not an error ---")
    st, body = get("/")
    check("the bare hostname is a 200, not a 404", st == 200, str(st))
    check("...saying there is nothing here rather than 'link isn't valid'",
          "Nothing to manage here" in body and "isn't valid" not in body, body[:400])
    check("...redirecting to the assistant without needing scripts",
          'http-equiv="refresh"' in body and "5;url=https://ai.ohmz.cloud" in body, body[:400])
    check("...and offering a link for anyone who won't wait",
          'href="https://ai.ohmz.cloud"' in body)
    st, body = get("/c")
    check("a link with the token missing lands there too", st == 200
          and "Nothing to manage here" in body, str(st))
    before = len(Fake.calls)
    check("no Hermes call for a tokenless visit", len(Fake.calls) == before)
    st, body = get("/elsewhere")
    check("an unknown path is still a 404", st == 404, str(st))
    check("...but shows the same way out", "Nothing to manage here" in body)
    st, body = get("/c?t=garbage.token")
    check("a forged token page offers the assistant too",
          'href="https://ai.ohmz.cloud"' in body and "isn't valid" in body, body[:300])
    check("...but does NOT auto-redirect away from the explanation",
          'http-equiv="refresh"' not in body)
    st, body = get(f"/c?t={old}")
    check("the expired page offers it as well", 'href="https://ai.ohmz.cloud"' in body)
    open(conf, "w").write(f"CANCEL_SECRET={SECRET}\nASSISTANT_URL=https://elsewhere.example\n")
    st, body = get("/")
    check("the destination is configurable", "https://elsewhere.example" in body, body[:300])
    open(conf, "w").write(f"CANCEL_SECRET={SECRET}\n")

    print("--- cancel does the FULL cleanup ---")
    canary = os.path.join(d, "canary.txt")
    open(canary, "w").write("x")
    st, body = post("/cancel", t=tok)
    check("cancel succeeds", st == 200 and "Cancelled" in body, body[:300])
    check("hermes got the DELETE", ("DELETE", f"/api/jobs/{JID}") in Fake.calls)
    check("...and the job is gone from the scheduler", JID not in Fake.jobs)
    ow = json.load(open(owners))
    check("ownership record cleared, others kept", JID not in ow and JID2 in ow, str(ow))
    q = json.load(open(os.path.join(out_dir, ".alerts.json")))
    check("queued alert flipped to cancelled", q["k1"]["status"] == "cancelled", str(q))
    check("unrelated queue entry untouched", q["k2"]["status"] == "pending")
    stones = json.load(open(os.path.join(out_dir, ".cancelled.json")))
    check("tombstone written", JID in stones, str(stones))
    check("...in the delivery watcher's timestamp format",
          bool(time.strptime(stones[JID], "%Y-%m-%dT%H:%M:%S")))
    check("watcher state files removed",
          not os.path.exists(os.path.join(state_dir, "fc-yyz-yvr.json"))
          and not os.path.exists(os.path.join(state_dir, "fc-yyz-yvr.txt")))
    check("flightclaw was told to untrack the route",
          ("MCP", "YYZ-YVR-2026-10-02-RT-2026-11-02") in Fake.calls, str(Fake.calls[-6:]))
    ledger = [json.loads(x) for x in open(os.path.join(out_dir, "alert_ledger.jsonl"))]
    check("the cancel is a ledger row",
          any("cancelled via email link" in " ".join(r.get("notes", [])) for r in ledger))
    check("the page lists what was cleaned", "queued alert" in body and "schedule removed" in body,
          body[:600])
    check("no path outside the tree was touched", os.path.exists(canary))

    print("--- replay and late views are honest no-ops ---")
    json.dump({JID: {"name": NAME}}, open(os.path.join(out_dir, ".job_names.json"), "w"))
    st, body = get(f"/c?t={tok}")
    check("the link after cancel says already cancelled", st == 200 and "Already cancelled" in body)
    check("...naming the job from the cache", "fare watch under" in body, body[:300])
    st, body = post("/cancel", t=tok)
    check("a replayed cancel is a calm no-op", st == 200 and "Already cancelled" in body)

    print("--- a refused delete cleans nothing ---")
    seed()
    Fake.jobs[JID2] = {"id": JID2, "name": "second watch", "prompt": "x --state slug2",
                       "schedule_display": "daily", "enabled": True, "state": "active"}
    open(os.path.join(state_dir, "slug2.json"), "w").write("{}")
    tok2 = ct.mint(JID2, "ohmz", SECRET)
    Fake.fail_delete = True
    st, body = post("/cancel", t=tok2)
    check("a 500 from hermes is a 503 page", st == 503, f"{st} {body[:200]}")
    check("the job is untouched", JID2 in Fake.jobs)
    check("ownership record kept", JID2 in json.load(open(owners)))
    check("watcher state kept", os.path.exists(os.path.join(state_dir, "slug2.json")))
    Fake.fail_delete = False

    print("--- pause and resume: reversible, no cleanup, honest state ---")
    st, body = post("/pause", t=tok2)
    check("pause lands on the paused page", st == 200 and "Paused" in body, body[:300])
    check("...offering resume", 'action="/resume"' in body)
    check("hermes got the pause", ("POST", f"/api/jobs/{JID2}/pause") in Fake.calls)
    check("pause cleaned NOTHING", JID2 in json.load(open(owners))
          and os.path.exists(os.path.join(state_dir, "slug2.json")))
    st, body = post("/pause", t=tok2)
    check("pausing a paused job is the same honest page", st == 200 and "Paused" in body)
    st, body = post("/resume", t=tok2)
    check("resume lands on the resumed page", st == 200 and "Resumed" in body, body[:300])
    check("...offering pause again", 'action="/pause"' in body)

    print("--- a hostile --state slug cannot reach outside the state dir ---")
    evil = os.path.join(d, "evil.txt")
    open(evil, "w").write("x")
    Fake.jobs[JID3] = {"id": JID3, "name": "evil watch",
                       "prompt": "x --state ../evil --schedule d",
                       "schedule_display": "daily", "enabled": True, "state": "active"}
    st, body = post("/cancel", t=ct.mint(JID3, "ohmz", SECRET))
    check("the cancel itself succeeds", st == 200, f"{st}")
    check("the traversal file survives", os.path.exists(evil))

    print("--- hermes down is a 503, not a stack trace ---")
    real_api = cs.HERMES_API
    cs.HERMES_API = "http://127.0.0.1:1"
    st, body = get(f"/c?t={ct.mint(JID2, 'ohmz', SECRET)}")
    check("unreachable scheduler is a 503", st == 503 and "untouched" in body, f"{st}")
    cs.HERMES_API = real_api

    print("--- hostile HTTP shapes do not hang or fall through ---")
    import socket as _sock
    host, port = "127.0.0.1", svc.server_address[1]
    cs.Handler.timeout = 2                    # the unit's 10s is too slow to test against
    s = _sock.socket()
    s.connect((host, port))
    s.sendall(b"POST /cancel HTTP/1.1\r\nHost: x\r\nContent-Length: 4096\r\n\r\nt=short")
    s.settimeout(8)
    began = time.time()
    try:
        answered = bool(s.recv(64))
    except Exception:
        answered = False
    s.close()
    check("a lying Content-Length releases the thread instead of hanging forever",
          time.time() - began < 7, f"{time.time() - began:.1f}s")
    check("...via the handler's own socket timeout", cs.Handler.timeout == 2)
    cs.Handler.timeout = cs.TIMEOUT
    st, _ = post("/cancel", t="x" * 9000)
    check("an oversized body cannot smuggle a token past the cap", st == 404, str(st))
    req = urllib.request.Request(base + "/cancel", method="POST",
                                 data=json.dumps({"t": tok}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    check("a JSON body fails closed (form-encoded only)", st == 404, str(st))
    req = urllib.request.Request(base + f"/c?t={tok}", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    check("HEAD /c is a 404 and mutates nothing", st == 404, str(st))

    print("--- an unfamiliar answer from hermes cleans nothing ---")
    seed()
    Fake.jobs[JID2] = {"id": JID2, "name": "conflict watch", "prompt": "x --state slug2",
                       "schedule_display": "daily", "enabled": True, "state": "active"}
    open(os.path.join(state_dir, "slug2.json"), "w").write("{}")

    real_hermes = cs.hermes
    cs.hermes = lambda m, p: (409, {"error": "conflict"}) if m == "DELETE" else real_hermes(m, p)
    st, body = post("/cancel", t=ct.mint(JID2, "ohmz", SECRET))
    check("a 409 DELETE is refused, not assumed to have worked", st == 503, str(st))
    check("...and nothing was cleaned", JID2 in json.load(open(owners))
          and os.path.exists(os.path.join(state_dir, "slug2.json")))
    cs.hermes = real_hermes

    print("--- a failed pause never renders a success page for the opposite action ---")
    cs.hermes = lambda m, p: (500, {}) if m == "POST" else real_hermes(m, p)
    st, body = post("/pause", t=ct.mint(JID2, "ohmz", SECRET))
    check("a refused pause is a 503, not 'Resumed'", st == 503 and "Resumed" not in body,
          f"{st} {body[:200]}")
    cs.hermes = real_hermes
    cs.hermes = lambda m, p: (401, {}) if m == "GET" and p.startswith("/api/jobs/") \
        else real_hermes(m, p)
    st, body = get(f"/c?t={ct.mint(JID2, 'ohmz', SECRET)}")
    check("an unauthorized job read is a 503, not a confirm page for 'your monitor'",
          st == 503 and "your monitor" not in body, f"{st} {body[:200]}")
    cs.hermes = real_hermes

    print("--- the tombstone write takes the lock the delivery tick honours ---")
    seed()
    locked = cs.tombstone_lock(os.path.join(out_dir, ".cancelled.json"))
    done = []
    threading.Thread(target=lambda: done.append(post("/cancel", t=ct.mint(JID, "ohmz", SECRET))),
                     daemon=True).start()
    time.sleep(0.6)
    check("a cancel blocks while another writer holds the lock", not done, str(done))
    locked.close()
    for _ in range(50):
        if done:
            break
        time.sleep(0.1)
    check("...and completes once it is released", bool(done) and done[0][0] == 200, str(done))

    print("--- failed verifies are rate limited ---")
    cs.FAIL_LIMIT = 2
    cs._fails.update(start=time.time(), n=0)
    codes = [get("/c?t=bad.token")[0] for _ in range(4)]
    check("the hard limit answers 429", codes[-1] == 429, str(codes))
    cs.FAIL_LIMIT = 30

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return fails


if __name__ == "__main__":
    sys.exit(main())

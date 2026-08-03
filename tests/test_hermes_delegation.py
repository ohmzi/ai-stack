#!/usr/bin/env python3
"""Delegation to hermes-agent is verified against the SCHEDULER, not the agent's narration.

Why this file exists. `_hermes_stream` ends every background-task turn by comparing a snapshot of
`/api/jobs` taken before delegating against one taken after, and reports one of six verdicts. That
block is the single load-bearing anti-hallucination guard in the background-task path — it exists
because the agent has, live:

  * claimed jobs it never created;
  * correctly RESCHEDULED a job while the pipe reported that nothing had been created — the
    loudest possible way to report success;
  * pointed at a job of the right name that had already FINISHED and called it "already running",
    leaving the user with a monitor that silently did not exist.

Every one of those is a case where the agent's prose and the scheduler's state disagree, and each
verdict below is the specific wording that tells them apart. When the check AGREES it now says
nothing: a success line was the pipe narrating its own internals over an answer the agent had
already given. The check still runs — it is only the reporting that is conditional on disagreement. Nothing tested it until now: the
existing harnesses cover whether a request *reaches* hermes (test_bgtask_intent.py) and whether a
finished run's output is parsed (test_hermes_delivery.py), but not what the pipe concludes.

Fully offline and deterministic — the HTTP session, the scheduler and the clock are all stubbed,
so this never talks to hermes and never creates a job.

Usage:  python3 tests/test_hermes_delegation.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys, tempfile, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_del", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


# ---- a scripted SSE endpoint ------------------------------------------------------------------
class _Content:
    def __init__(self, lines, exc=None):
        self._lines, self._exc = lines, exc

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
            if self._exc is not None:   # the stream dies instead of reaching [DONE]
                raise self._exc
        return gen()


class _Resp:
    def __init__(self, lines, status=200, exc=None):
        self.status, self.content = status, _Content(lines, exc)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "stub error body"


SENT = {}          # last payload the pipe posted to hermes, for contract assertions


class _Session:
    def __init__(self, lines, status=200, exc=None, post_exc=None):
        self._lines, self._status = lines, status
        self._exc, self._post_exc = exc, post_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **kw):
        if self._post_exc is not None:  # the gateway is not even listening
            raise self._post_exc
        SENT.clear()
        SENT.update(kw.get("json") or {})
        return _Resp(self._lines, self._status, self._exc)


class _Aiohttp:
    """Proxies the real aiohttp except for ClientSession, so ClientTimeout and the exception
    types the pipe catches keep working."""
    def __init__(self, real, lines, status=200, exc=None, post_exc=None):
        self._real, self._lines, self._status = real, lines, status
        self._exc, self._post_exc = exc, post_exc

    def __getattr__(self, k):
        return getattr(self._real, k)

    def ClientSession(self, *a, **kw):
        return _Session(self._lines, self._status, self._exc, self._post_exc)


def sse(text):
    """The agent's reply, as the token stream the pipe actually parses.

    Spaces are preserved deliberately. The verifier resolves cited job ids with
    `\\b[0-9a-f]{12}\\b`, so a stream that silently concatenated its tokens would bury the id in a
    longer alphanumeric run, kill the word boundary, and make every citation test pass for the
    wrong reason.
    """
    toks = text.split(" ")
    toks = [t + " " for t in toks[:-1]] + toks[-1:]
    out = [b"data: " + json.dumps(
        {"choices": [{"delta": {"content": tok}}]}).encode() for tok in toks]
    return out + [b"data: [DONE]"]


def job(jid, sched="every 5m", enabled=True, state="active", repeat=None):
    return {"id": jid, "schedule_display": sched, "enabled": enabled,
            "state": state, "repeat": repeat}


OWNERS_DIR = tempfile.mkdtemp()


def fresh_owners(seed=None):
    """A private ownership map for one scenario, so stamps from different drives cannot mix."""
    path = os.path.join(OWNERS_DIR, f"o{len(os.listdir(OWNERS_DIR))}.json")
    if seed is not None:
        json.dump(seed, open(path, "w"))
    mod.TASK_OWNERS_FILE = path
    return path


def owners_now():
    try:
        return json.load(open(mod.TASK_OWNERS_FILE))
    except Exception:
        return {}


def drive(reply, snapshots, verify=True, status=200, brief=None, exc=None, post_exc=None,
          scoped=False, uname="ohmz"):
    """Run one delegation turn. `snapshots` is what _hermes_jobs returns on successive calls.
    `exc` kills the SSE stream before [DONE]; `post_exc` kills the connection attempt itself."""
    p = mod.Pipe()
    seq = list(snapshots)
    released = []

    def fake_jobs():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    p._hermes_jobs = fake_jobs
    p._hermes_key = lambda: "test-key"
    drive.metrics = []
    p._metric = lambda **f: drive.metrics.append(f)
    p._alert_setup_block = lambda uname: "\n<alert-setup>"
    # Never touch the real Ollama from a test; record that the handoff released the tenant.
    p._release_chat_tenant = lambda: released.append(p.chat_model)
    drive.released = released

    real_aiohttp = mod.aiohttp
    real_sleep = mod.asyncio.sleep

    async def no_sleep(_s):          # the verifier polls 6x1s; tests must not take six seconds
        return None

    lines = sse(reply)
    if exc is not None:
        lines = lines[:-1]   # a dying stream never delivers its [DONE]
    mod.aiohttp = _Aiohttp(real_aiohttp, lines, status, exc, post_exc)
    mod.asyncio.sleep = no_sleep
    chunks = []
    try:
        async def go():
            async for c in p._hermes_stream("watch this price every 5m", uname, verify, brief,
                                            scoped=scoped):
                chunks.append(c)
        asyncio.run(go())
    finally:
        mod.aiohttp = real_aiohttp
        mod.asyncio.sleep = real_sleep
    return "".join(chunks)


def main():
    print("--- _changed_jobs: only fields a user would recognise ---")
    cj = mod.Pipe._changed_jobs
    base = {"a": job("a", "every 5m")}
    check("a rescheduled job is a change",
          cj(base, {"a": job("a", "every 10m")}) == [("a", "rescheduled")])
    check("disabling is a change", cj(base, {"a": job("a", enabled=False)}) == [("a", "disabled")])
    check("a state transition is a change",
          cj(base, {"a": job("a", state="completed")}) == [("a", "now completed")])
    # next_run_at ticks forward every minute; reporting it would claim a change on every turn.
    noisy = {"a": dict(job("a"), next_run_at="2026-08-01T00:00:00", last_status="ok")}
    check("a ticking next_run_at is NOT a change", cj(base, noisy) == [], repr(cj(base, noisy)))
    check("a job that did not exist before is not an update", cj({}, base) == [])

    print("--- the six verdicts ---")
    before = {"old": job("old")}

    out = drive("scheduled it for you", [before, {**before, "new1": job("new1", "every 5m")}])
    # The check still runs — that is the anti-hallucination guard — but it says nothing when it
    # AGREES with the agent. A success line is the pipe narrating its own internals: the agent has
    # already told the user what was scheduled and quoted the id. Every verdict below still speaks,
    # because each of those is the check DISAGREEING, which is the part the reader needs.
    check("a verified creation is confirmed in the metrics",
          any(m.get("job") == "hermes" and m.get("outcome") == "created" for m in drive.metrics),
          repr(drive.metrics)[:200])
    check("...and says nothing about it in the reply", "Verified scheduled" not in out, out[-160:])
    check("...but still shows how alerts will reach the user", "<alert-setup>" in out, out[-160:])
    out = drive("made two", [before, {**before, "n1": job("n1"), "n2": job("n2")}])
    check("more jobs than the agent described IS worth saying", "2 tasks were created" in out,
          out[-200:])

    # A completed job appearing is NOT a creation — _runnable() must reject it, or "cancel this"
    # would report a brand new monitor.
    out = drive("done", [before, {**before, "z": job("z", state="completed", enabled=False)}])
    check("a COMPLETED new row is not counted as a creation",
          "Verified scheduled" not in out, out[-160:])

    out = drive("changed it", [before, {"old": job("old", "every 10m")}])
    check("a reschedule is Verified updated, not 'nothing created'",
          "✅ **Verified updated**" in out and "Verification failed" not in out, out[-200:])
    check("...reports what actually changed", "rescheduled" in out, out[-200:])

    # The job must be present BEFORE as well as after — a row that only appears afterwards is a
    # creation, which is a different verdict entirely.
    live_before = {**before, "abc123def456": job("abc123def456")}
    out = drive("that is already running as abc123def456", [live_before, live_before])
    check("pointing at an existing ACTIVE job is not a fabrication",
          "ℹ️ **No new job created**" in out, out[-200:])
    check("...and confirms it is still scheduled", "still scheduled" in out, out[-200:])

    # Completed in BOTH snapshots: a job that finished DURING the turn would be a state change, and
    # _changed_jobs would (correctly) report that instead.
    dead = job("abc123def456", state="completed", enabled=False)
    dead_before = {**before, "abc123def456": dead}
    out = drive("already running as abc123def456", [dead_before, dead_before])
    check("pointing at a FINISHED job is reported as nothing scheduled",
          "⚠️ **Nothing is scheduled**" in out, out[-220:])
    check("...and tells the user how to recover", "create a new one" in out, out[-220:])

    out = drive("I have scheduled your monitor", [before, before])
    check("a described job with no scheduler entry is Verification failed",
          "⚠️ **Verification failed**" in out, out[-200:])

    out = drive("scheduled", [None, None])
    check("an unreachable /api/jobs says so rather than accusing the agent",
          "could not verify" in out and "Verification failed" not in out, out[-160:])

    print("--- scope: verification only where it belongs ---")
    out = drive("here are your jobs", [before, before], verify=False)
    check("a list/cancel turn (verify_creation=False) adds no verdict",
          "Verified" not in out and "Verification failed" not in out, out[-160:])
    check("the agent's own text is still streamed through", "here are your jobs" in out, out[:120])
    # Continuity used to ride in the reply as <!--bg-task-->. OpenWebUI escapes HTML comments, so
    # that printed as visible junk under every answer; it now lives on the pipe (_mark_bg), set at
    # the pipe() call site BEFORE the stream starts — which also means it survives a stream that
    # never finishes. What the reply must contain is nothing at all.
    check("no marker leaks into the reply body", "<!--" not in out, repr(out[-60:]))

    out = drive("nope", [before, before], status=503)
    check("a non-200 from hermes surfaces as an error, not a silent pass",
          "hermes-agent HTTP 503" in out, out[:160])

    print("--- continuity survives timeouts and a dead gateway ---")
    # Live failure shape: after a timeout the pipe suggests "ask me to list tasks" — but the reply
    # carried no _BG_MARK, so that very follow-up matched no predicate and landed in plain chat,
    # exactly when continuity mattered most.
    out = drive("partial answer", [before, before], verify=False, exc=asyncio.TimeoutError())
    check("a timeout explains itself", "did not finish" in out, out[-200:])
    check("...streams what arrived before dying", "partial answer" in out, out[:120])
    check("...without leaking a marker", "<!--" not in out, repr(out[-60:]))

    dead = mod.aiohttp.ClientConnectorError.__new__(mod.aiohttp.ClientConnectorError)
    out = drive("never sent", [before, before], verify=False, post_exc=dead)
    check("a dead gateway names the fix", "hermes-gateway" in out, out[:160])
    check("...without leaking a marker", "<!--" not in out, repr(out[-60:]))

    print("--- continuity is held on the pipe, so it survives a stream that never finishes ---")
    pc = mod.Pipe()
    check("a fresh chat is not mid-task", not pc._was_bg_turn("c9", []))
    pc._mark_bg("c9")
    check("marking makes the next short reply a follow-up", pc._is_bg_followup("yes, retry", [], "c9"))
    check("...and only in THAT chat", not pc._is_bg_followup("yes, retry", [], "c-other"))
    check("legacy history still counts as a task turn",
          pc._is_bg_followup("yes, retry",
                             [{"role": "assistant", "content": "x" + mod.Pipe._BG_MARK}], "c-old"))

    print("--- the GPU handoff ---")
    # The cron tag runs at num_ctx 65536 and chat at 32768; Ollama keys runners by model+options,
    # so they are two distinct ~17 GB allocations and only one fits on the card.
    drive("ok", [before, before], verify=False)
    check("delegating releases the chat tenant first", drive.released == [mod.Pipe().chat_model],
          repr(drive.released))

    print("--- one-shot research uses its OWN contract, not the cron brief ---")
    rb = mod.Pipe._RESEARCH_BRIEF
    drive("looked it up", [None, None], verify=False, brief=rb)
    sysmsg = next((m["content"] for m in SENT.get("messages", []) if m["role"] == "system"), "")
    check("the research brief is what actually goes on the wire", rb[:60] in sysmsg, sysmsg[:100])
    check("...and the cron brief does not", mod.Pipe._HERMES_BRIEF[:60] not in sysmsg)
    # These two are the whole reason the briefs cannot be shared: the cron brief instructs the agent
    # to schedule and to emit LOG/ALERT lines, and hermes_delivery.py parses those out of any run.
    check("research forbids creating cron jobs", "not create" in rb.lower() or "do not create" in rb.lower())
    check("research forbids LOG/ALERT lines (they would be delivered as a real alert)",
          "ALERT(" in rb and "LOG:" in rb)
    check("research still bans unsourced numbers", "did not read" in rb.lower())

    # And the default path must be unchanged by the parameterisation.
    drive("scheduled", [before, before], verify=False)
    sysmsg = next((m["content"] for m in SENT.get("messages", []) if m["role"] == "system"), "")
    check("the cron brief is still the default", mod.Pipe._HERMES_BRIEF[:60] in sysmsg)

    print("--- every job this turn creates is stamped to the requester ---")
    # Ownership is what makes a per-user view possible at all: hermes records no owner, so the one
    # moment the pipe can attribute a job is the instant it appears in the before/after diff. A job
    # created and not stamped is invisible to the person who asked for it, forever.
    fresh_owners()
    drive("scheduled it", [before, {**before, "new1": job("new1")}], uname="alice")
    check("a created job is owned by whoever asked", owners_now().get("new1", {}).get("h") == "alice",
          repr(owners_now()))
    check("...and jobs that already existed are not claimed", "old" not in owners_now(),
          repr(owners_now()))

    fresh_owners()
    drive("made both", [before, {**before, "n1": job("n1"), "n2": job("n2")}], uname="alice")
    check("EVERY new job is stamped, not just the first",
          {"n1", "n2"} <= set(owners_now()), repr(owners_now()))

    # The diff is host-wide, so a job another user created in the same seconds would otherwise be
    # attributed here. The agent prints the real id it made (brief rule 9) — prefer that.
    fresh_owners()
    drive("created abc123abc123 for you",
          [before, {**before, "abc123abc123": job("abc123abc123"), "someoneelse1": job("someoneelse1")}],
          uname="alice")
    o = owners_now()
    check("a cited id wins over the bare diff", o.get("abc123abc123", {}).get("h") == "alice", repr(o))
    check("...and the concurrently-created stranger is left alone", "someoneelse1" not in o, repr(o))
    check("...and the stamp records that it was cited",
          o.get("abc123abc123", {}).get("src") == "cited", repr(o))

    # The gap this closes: "yes, create a new one" travels the follow-up path, which wants no
    # verdict — and used to take no snapshot either, so the job it created ended up unowned.
    fresh_owners()
    drive("done", [before, {**before, "fup1": job("fup1")}], verify=False, uname="alice")
    check("a job created on a no-verdict turn is still owned",
          owners_now().get("fup1", {}).get("h") == "alice", repr(owners_now()))

    fresh_owners()
    drive("partial", [before, {**before, "late1": job("late1")}], verify=False,
          exc=asyncio.TimeoutError(), uname="alice")
    check("a job created by a turn that TIMED OUT is claimed before we tell the user to go look",
          owners_now().get("late1", {}).get("h") == "alice", repr(owners_now()))

    print("--- a scoped turn is told which jobs are the user's, and narrates no others ---")
    fresh_owners({"old": {"h": "bob", "t": time.time()},
                  "mine1": {"h": "alice", "t": time.time()}})
    base2 = {"old": job("old"), "mine1": job("mine1")}
    drive("ok", [base2, base2], verify=False, scoped=True, uname="alice")
    sysmsg = next((m["content"] for m in SENT.get("messages", []) if m["role"] == "system"), "")
    check("the duplicate-check scope names only the user's own job",
          "mine1" in sysmsg.split("Duplicate-check scope:")[-1]
          and "old" not in sysmsg.split("Duplicate-check scope:")[-1], sysmsg[-300:])
    check("...and forbids describing anyone else's", "never name, cite, quote or describe" in sysmsg)
    drive("ok", [base2, base2], verify=False, scoped=False, uname="alice")
    sysmsg = next((m["content"] for m in SENT.get("messages", []) if m["role"] == "system"), "")
    check("an admin turn carries no scope restriction", "Duplicate-check scope" not in sysmsg)

    print("--- ...and refuses to report on a job the user does not own ---")
    # Real 12-hex ids: the verifier resolves citations with \b[0-9a-f]{12}\b, so a placeholder
    # name would never be recognised as a citation at all and the test would pass vacuously.
    BOBS = "bbbb11112222"
    snap = {BOBS: job(BOBS)}
    fresh_owners({BOBS: {"h": "bob", "t": time.time()}})
    out = drive(f"that is already running as {BOBS}", [snap, snap], scoped=True, uname="alice")
    check("pointing at somebody else's job says nothing was created for you",
          "not yours" in out and "No new job was created for you" in out, out[-260:])
    check("...without leaking its id", f"`{BOBS}`" not in out, out[-260:])
    fresh_owners({BOBS: {"h": "alice", "t": time.time()}})
    out = drive(f"that is already running as {BOBS}", [snap, snap], scoped=True, uname="alice")
    check("...but her OWN job is reported normally",
          "No new job created" in out and f"`{BOBS}`" in out, out[-200:])

    fresh_owners({BOBS: {"h": "bob", "t": time.time()}})
    out = drive("changed it", [{BOBS: job(BOBS, "every 5m")}, {BOBS: job(BOBS, "every 10m")}],
                scoped=True, uname="alice")
    check("a change to another user's job is not narrated to this one",
          "Verified updated" not in out, out[-200:])

    print("--- /research is EXPLICIT: no heuristic may fire on ordinary chat ---")
    p = mod.Pipe()
    for t in ["research the best way to cook rice", "can you look into this for me",
              "agent smith is a character in the matrix", "what should I cook tonight?"]:
        check(f"plain text does not delegate: {t!r}",
              not t.lower().startswith(("/research", "/agent")) and not p._is_bg_task_request(t))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

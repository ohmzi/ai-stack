#!/usr/bin/env python3
"""Delegation to hermes-agent is verified against the SCHEDULER, not the agent's narration.

Why this file exists. `_hermes_stream` ends every background-task turn by comparing a snapshot of
`/api/jobs` taken before delegating against one taken after, and reports one of six verdicts. That
block is the single load-bearing anti-hallucination guard in the background-task path — it exists
because the agent has, live:

  * claimed jobs it never created (hence "confirmed against the scheduler, not the agent's word");
  * correctly RESCHEDULED a job while the pipe reported that nothing had been created — the
    loudest possible way to report success;
  * pointed at a job of the right name that had already FINISHED and called it "already running",
    leaving the user with a monitor that silently did not exist.

Every one of those is a case where the agent's prose and the scheduler's state disagree, and each
verdict below is the specific wording that tells them apart. Nothing tested it until now: the
existing harnesses cover whether a request *reaches* hermes (test_bgtask_intent.py) and whether a
finished run's output is parsed (test_hermes_delivery.py), but not what the pipe concludes.

Fully offline and deterministic — the HTTP session, the scheduler and the clock are all stubbed,
so this never talks to hermes and never creates a job.

Usage:  python3 tests/test_hermes_delegation.py [pipe_path]
"""
import asyncio, importlib.util, json, sys

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


def drive(reply, snapshots, verify=True, status=200, brief=None, exc=None, post_exc=None):
    """Run one delegation turn. `snapshots` is what _hermes_jobs returns on successive calls.
    `exc` kills the SSE stream before [DONE]; `post_exc` kills the connection attempt itself."""
    p = mod.Pipe()
    seq = list(snapshots)
    released = []

    def fake_jobs():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    p._hermes_jobs = fake_jobs
    p._hermes_key = lambda: "test-key"
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
            async for c in p._hermes_stream("watch this price every 5m", "ohmz", verify, brief):
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
    check("a real creation is Verified scheduled", "✅ **Verified scheduled**" in out, out[-160:])
    check("...names the job and its schedule", "`new1`" in out and "every 5m" in out, out[-160:])
    check("...and shows how alerts will reach the user", "<alert-setup>" in out, out[-160:])

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
    check("every hermes turn carries the invisible follow-up marker",
          out.endswith(mod.Pipe._BG_MARK), repr(out[-40:]))

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
    check("...and still carries the follow-up marker",
          out.endswith(mod.Pipe._BG_MARK), repr(out[-40:]))

    dead = mod.aiohttp.ClientConnectorError.__new__(mod.aiohttp.ClientConnectorError)
    out = drive("never sent", [before, before], verify=False, post_exc=dead)
    check("a dead gateway names the fix", "hermes-gateway" in out, out[:160])
    check("...and still carries the follow-up marker",
          out.endswith(mod.Pipe._BG_MARK), repr(out[-40:]))

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

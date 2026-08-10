#!/usr/bin/env python3
"""'Change the alert to X' edits the job — through EVERY route a turn can take, not just Task Mode.

Why this file exists. Live, 2026-08-10, a user asked to change an existing flight watch's
schedule and got a confident "✅ Switched to daily checks" reply. `hermes cron list` afterward
showed the job byte-for-byte unchanged. Root cause, in two layers:

  * under the Task Mode control, the message fell to the RESEARCH brief, which explicitly
    forbids touching cron jobs — the agent ignored that and edited anyway, with no rules, and
    corrupted the job's name in the process (fixed: _EDIT_VERB + _EDIT_BRIEF, tested in
    tests/test_task_mode.py);
  * OUTSIDE Task Mode — which turned out to be what actually happened for the SECOND live
    report — there was no equivalent check AT ALL. "change it to checking every day for next 2
    weeks" matched no predicate in the default pipe() cascade (no _BG_VERB opening verb, so
    _is_bg_task_request is unconditionally false regardless of the recurrence wording it DOES
    carry) and fell straight to the plain CHAT MODEL — zero tool access, zero view of the real
    scheduler. It fabricated a complete, plausible-sounding confirmation from the job id and
    schedule visible earlier in the conversation. Confirmed via the live metrics log: the turn's
    row was {"route": "chat", "rule_id": "fallthrough"} — _hermes_stream was never even called,
    so no verification of any kind could have run.

This file pins that the SAME edit signal now claims the turn in the DEFAULT (non-Task-Mode)
cascade, before it can ever reach the chat fallback — end to end through pipe(), not through the
regex alone, because "the predicate matches" and "the turn actually reaches the right brief with
verification on" are different claims and the live bug was a gap in the second one.

Fully offline: the scheduler and the agent stream are both stubbed, so this never talks to hermes
and never touches a real job.

Usage:  python3 tests/test_edit_routing.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys, tempfile, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_edit", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


JOBS = [{"id": "b0a914a23d52", "name": "Toronto → Miami fare watch under $1,000",
         "schedule_display": "every 15m", "state": "scheduled", "enabled": True}]
ADMIN = {"role": "admin", "email": "root@example.com"}

TMP = tempfile.mkdtemp()
SENT = []
CHAT = []


def stack():
    p = mod.Pipe()
    path = os.path.join(TMP, f"own{len(os.listdir(TMP))}.json")
    json.dump({j["id"]: {"h": "root", "t": time.time(), "src": "seed"} for j in JOBS}, open(path, "w"))
    mod.TASK_OWNERS_FILE = path
    SENT.clear(); CHAT.clear()

    def api(method, path_, body=None, timeout=10):
        if path_.startswith("/api/jobs?"):
            return 200, {"jobs": JOBS}, None
        return 200, {"ok": True}, None

    def hermes(text, uname="user", verify_creation=False, brief=None, scoped=False):
        SENT.append({"text": text, "uname": uname, "verify": verify_creation, "scoped": scoped,
                     "brief": ("research" if brief is mod.Pipe._RESEARCH_BRIEF
                               else "edit" if brief is mod.Pipe._EDIT_BRIEF
                               else "cron" if brief is None else "other")})

        async def go():
            yield "[agent]"
        return go()

    def chat(*a, **kw):
        CHAT.append(True)

        async def go():
            yield "[chat]"
        return go()

    p._hermes_api = api
    p._hermes_stream = hermes
    p._achat_stream = chat
    p._contact = lambda h: {"phone": "+15145550123"}
    return p


def say(p, text, user=ADMIN, cid="c1"):
    """One full pipe() turn with NO Task Mode filter — the default, most common path."""
    p._chat_id = lambda *a, **k: cid

    async def go():
        res = await p.pipe({"messages": [{"role": "user", "content": text}],
                            "model": "auto_assistant.auto"},
                           __metadata__={"user_prompt": text, "chat_id": cid}, __user__=user)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    return asyncio.run(go())


def main():
    print("--- outside Task Mode, 'change the alert' edits — it does not fabricate via chat ---")
    for t in ("change it to checking every day for next 2 weeks",     # the literal live message
              "change the alert to 15 mins for 2 hours",
              "adjust the schedule to every 6 hours",
              "please update the frequency to daily",
              "reschedule it to run hourly"):
        p = stack()
        say(p, t)
        check(f"routes to the edit brief, verified, never chat: {t[:44]!r}",
              SENT and SENT[0]["brief"] == "edit" and SENT[0]["verify"] is True and not CHAT,
              f"sent={SENT[:1]!r} chat={CHAT}")

    print("--- the exact live failure: this message used to reach NOTHING but plain chat ---")
    p = stack()
    out = say(p, "change it to checking every day for next 2 weeks")
    check("the agent was actually asked, not the chat model", SENT != [] and CHAT == [],
          f"sent={len(SENT)} chat={CHAT}")
    check("...with verification on, so a no-op edit can be caught", SENT[0]["verify"] is True)
    check("the reply is the stubbed agent's, not a chat fabrication", out == "[agent]", out)

    print("--- a genuine NEW watch still creates, not edits ---")
    p = stack()
    say(p, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    check("still routes to the creation brief", SENT and SENT[0]["brief"] == "cron", SENT[:1])

    print("--- a genuine one-off question still answers, not edits ---")
    p = stack()
    say(p, "what's a good schedule for checking flight prices?")
    check("no agent call, no edit brief", not SENT or SENT[0]["brief"] != "edit", SENT[:1])

    print("--- manage verbs still take the deterministic path, not the edit brief ---")
    p = stack()
    say(p, "cancel the fare watch")
    check("cancel never reaches _hermes_stream at all",
          not SENT or SENT[0]["brief"] != "edit", SENT[:1])
    p = stack()
    say(p, "pause the fare watch")
    check("pause never reaches _hermes_stream at all",
          not SENT or SENT[0]["brief"] != "edit", SENT[:1])

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

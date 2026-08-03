#!/usr/bin/env python3
"""One user's background tasks are not another user's business.

Why this file exists. Background tasks were built single-user: hermes records no owner on a job,
its API key is shared, and `GET /api/jobs` returns every job on the host. The pipe's first answer
was to make management admin-only — which did not so much solve the problem as decline it, because
an ordinary user asking "list my trackers" fell through to the agent, whose `cronjob` tool sees
everything. Either way, one person could read another person's monitors.

Ownership now lives in a pipe-side sidecar, stamped from the scheduler's own job ids at the moment
a job is created. This file pins the contract that ownership is supposed to buy, end to end through
`pipe()` rather than through the helpers:

  * a user sees their own tasks and only their own — not the names, not the ids, not the count;
  * a user cannot pause, resume or delete a job they do not own, by id or by name;
  * an admin sees everything, with an Owner column;
  * an unowned job belongs to nobody and is admin-only, never silently adopted;
  * when ownership cannot be determined, an ordinary user is told so — never shown an empty list
    (which reads as "you have nothing scheduled") and never shown the whole host's;
  * no read-only turn from an ordinary user is ever handed to the agent, because the agent has no
    concept of who is asking.

Fully offline: the scheduler is a stub, so this never talks to hermes and can never create, pause
or delete a real job.

Usage:  python3 tests/test_task_ownership.py [pipe_path]
"""
import asyncio, importlib.util, json, os, sys, tempfile, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_own", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


JOBS = [
    {"id": "a1a1a1a1a1a1", "name": "RTX 5090 newegg price watch", "schedule_display": "every 30m",
     "state": "scheduled", "enabled": True},
    {"id": "b2b2b2b2b2b2", "name": "btc drop alert below 60k", "schedule_display": "every 60m",
     "state": "scheduled", "enabled": True},
    {"id": "c3c3c3c3c3c3", "name": "orphan legacy monitor", "schedule_display": "daily",
     "state": "scheduled", "enabled": True},
]
OWNERS = {"a1a1a1a1a1a1": "alice", "b2b2b2b2b2b2": "bob"}     # c3… deliberately unowned

ALICE = {"role": "user", "email": "alice@example.com"}
BOB = {"role": "user", "email": "bob@example.com"}
ADMIN = {"role": "admin", "email": "root@example.com"}

TMP = tempfile.mkdtemp()
CALLS = []
DELEGATED = []


def stack(owners=OWNERS):
    """One pipe with a stubbed scheduler and a private ownership map."""
    p = mod.Pipe()
    state = {"jobs": [dict(j) for j in JOBS]}
    path = os.path.join(TMP, f"own{len(os.listdir(TMP))}.json")
    if owners is not None:
        with open(path, "w") as f:
            f.write(owners if isinstance(owners, str)
                    else json.dumps({k: {"h": v, "t": time.time(), "src": "seed"}
                                     for k, v in owners.items()}))
    mod.TASK_OWNERS_FILE = path
    CALLS.clear()
    DELEGATED.clear()

    def api(method, path_, body=None, timeout=10):
        CALLS.append((method, path_))
        if path_.startswith("/api/jobs?"):
            return 200, {"jobs": state["jobs"]}, None
        jid = path_.split("/api/jobs/")[1].split("/")[0]
        if method == "DELETE":
            state["jobs"] = [j for j in state["jobs"] if j["id"] != jid]
        return 200, {"ok": True}, None

    def never_delegate(text, uname="user", verify_creation=False, brief=None, scoped=False):
        DELEGATED.append({"text": text, "uname": uname, "scoped": scoped})

        async def go():
            yield "[agent]"
        return go()

    def never_chat(*a, **kw):
        DELEGATED.append({"text": "<chat>", "uname": None, "scoped": None})

        async def go():
            yield "[chat]"
        return go()

    p._hermes_api = api
    p._hermes_stream = never_delegate
    # Stub the chat path too: a turn that escapes to the model would otherwise make a REAL Ollama
    # call, which is both slow and a false pass — the reply would look like a plausible answer.
    p._achat_stream = never_chat
    p._state = state
    return p


def say(p, user, text, cid=None):
    """One full pipe() turn as OpenWebUI would drive it."""
    cid = cid or f"chat-{user['email']}"
    p._chat_id = lambda *a, **k: cid

    async def go():
        res = await p.pipe({"messages": [{"role": "user", "content": text}],
                            "model": "auto_assistant.auto"},
                           __metadata__={"user_prompt": text, "chat_id": cid},
                           __user__=user)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    return asyncio.run(go())


def main():
    print("--- each user sees their own tasks, and only their own ---")
    p = stack()
    a = say(p, ALICE, "list all my trackers")
    check("alice sees her own monitor", "RTX 5090" in a, a[:200])
    check("...and no trace of bob's name", "btc drop" not in a, a)
    check("...or of bob's id", "b2b2b2b2b2b2" not in a, a)
    check("...or of the unowned job", "orphan legacy" not in a, a)
    check("...and the count reflects only hers", "1 active" in a, a[:120])
    check("...answered locally: the agent was never asked", DELEGATED == [], repr(DELEGATED))

    b = say(p, BOB, "list all my trackers")
    check("bob sees his own monitor", "btc drop" in b, b[:200])
    check("...and none of alice's", "RTX 5090" not in b and "a1a1a1a1a1a1" not in b, b)

    print("--- an admin sees everything, and who owns it ---")
    adm = say(p, ADMIN, "list all my trackers")
    check("all three jobs are listed", all(j["name"][:12] in adm for j in JOBS), adm[:400])
    check("...with an Owner column", "| Owner |" in adm, adm[:300])
    check("...naming both owners", "alice" in adm and "bob" in adm, adm[:400])
    check("...and marking the unowned one unclaimed", "—" in adm, adm[:400])

    print("--- nobody can reach into somebody else's tasks ---")
    p = stack()
    say(p, ALICE, "list my trackers")
    CALLS.clear()
    by_id = say(p, ALICE, "cancel b2b2b2b2b2b2")
    check("cancelling bob's job by id writes nothing",
          "DELETE" not in [m for m, _ in CALLS], repr(CALLS))
    check("...and the answer does not confirm it exists",
          "btc drop" not in by_id and "Cancel this task" not in by_id, by_id[:200])
    CALLS.clear()
    by_name = say(p, ALICE, "cancel the btc monitor")
    check("cancelling it by name writes nothing either",
          "DELETE" not in [m for m, _ in CALLS], repr(CALLS))
    check("...and still names nothing of bob's", "btc drop alert" not in by_name, by_name[:200])
    CALLS.clear()
    say(p, ALICE, "pause the btc monitor")
    check("pausing somebody else's job writes nothing", not [m for m, _ in CALLS if m == "POST"],
          repr(CALLS))

    print("--- the unowned job belongs to nobody, and is never adopted ---")
    p = stack()
    orphan = say(p, ALICE, "cancel the orphan legacy monitor")
    # The reply echoes what she typed, which is not a leak — what must not appear is the job
    # itself: its id, or a row for it in the table she is shown.
    check("an ordinary user cannot claim an unowned job",
          "DELETE" not in [m for m, _ in CALLS] and "c3c3c3c3c3c3" not in orphan, orphan[:200])
    check("...and it is absent from the list she is shown",
          orphan.count("| **") <= 1, orphan[:300])

    print("--- a user CAN fully manage their own ---")
    p = stack()
    say(p, ALICE, "list my trackers")
    ask = say(p, ALICE, "cancel the RTX one")
    check("her own job asks for confirmation first", "Cancel this task for good?" in ask, ask[:140])
    check("...naming it", "RTX 5090 newegg price watch" in ask, ask[:300])
    check("...and writing nothing yet", "DELETE" not in [m for m, _ in CALLS], repr(CALLS))
    done = say(p, ALICE, "yes")
    check("...then deleting on an explicit yes", "Cancelled" in done, done[:140])
    check("...exactly once", [m for m, _ in CALLS].count("DELETE") == 1, repr(CALLS))
    check("...and it is really gone from the scheduler",
          not any(j["id"] == "a1a1a1a1a1a1" for j in p._state["jobs"]))
    after = say(p, ALICE, "list my trackers")
    check("her list is now empty — and says so as HERS", "no background tasks" in after.lower()
          and "Only your own tasks" in after, after[:200])

    print("--- when ownership is unknowable, say so; never guess in either direction ---")
    p = stack(owners="{ not json at all")
    bad = say(p, ALICE, "list my trackers")
    check("the user is told ownership could not be read",
          "could not work out which tasks" in bad, bad[:200])
    check("...and that this is NOT an empty list", "not the same as" in bad, bad[:240])
    check("...while no job leaks in the meantime",
          not any(j["name"][:10] in bad for j in JOBS), bad[:240])
    check("...and nothing was delegated to the agent instead", DELEGATED == [], repr(DELEGATED))
    admin_ok = say(p, ADMIN, "list my trackers")
    check("an admin still gets the full list from a broken map",
          "RTX 5090" in admin_ok and "btc drop" in admin_ok, admin_ok[:200])

    print("--- a read-only turn from an ordinary user NEVER reaches the agent ---")
    # The agent's job list is the whole host and it has no notion of who is asking, so delegating
    # a listing turn would undo everything above. With the deterministic path switched off there
    # is deliberately no fallback for scoped users — only a refusal.
    old = mod.MANAGE_DETERMINISTIC
    try:
        mod.MANAGE_DETERMINISTIC = False
        # "list my tasks" rather than "trackers": the kill switch takes the BROADENED vocabulary
        # with it by design, so the phrasings it added stop being task requests at all. This one
        # is matched by the older manage rule, so it still reaches the fallback decision.
        p = stack()
        off = say(p, ALICE, "list my tasks")
        check("she is told listing is unavailable", "switched off" in off, off[:200])
        check("...and the agent was NOT asked",
              not [d for d in DELEGATED if d["uname"]], repr(DELEGATED))
        p = stack()
        say(p, ADMIN, "list my tasks")
        check("an admin still falls through to the agent as before",
              len([d for d in DELEGATED if d["uname"]]) == 1, repr(DELEGATED))
    finally:
        mod.MANAGE_DETERMINISTIC = old

    print("--- creating a task tells hermes whose it is ---")
    p = stack()
    say(p, ALICE, "/task watch the rtx price every 6 hours")
    check("the creation reached the agent", len(DELEGATED) == 1, repr(DELEGATED))
    check("...tagged with the requesting user", DELEGATED[0]["uname"] == "alice", repr(DELEGATED))
    check("...and marked scoped, so its duplicate check cannot cite another user's job",
          DELEGATED[0]["scoped"] is True, repr(DELEGATED))
    p = stack()
    say(p, ADMIN, "/task watch the rtx price every 6 hours")
    check("an admin's creation is not scoped", DELEGATED[0]["scoped"] is False, repr(DELEGATED))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

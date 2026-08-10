#!/usr/bin/env python3
"""The Task control means what it says: while it is on, the turn goes to the agent.

Why this file exists. Background-task requests used to be detected by reading the user's wording —
a monitoring verb plus evidence of recurrence. It kept failing in both directions, and each fix
made the other direction worse:

  * "track the item <url> when the price is under 10" was answered with a Python scraping script,
    because the recurrence pattern had no arm for "is under";
  * "can you tracker the price" matched nothing, because the verb pattern requires `track\\b`;
  * every widening that caught those then claimed ordinary sentences ("what are the biggest jobs
    in tech?", "cancel my job apps", "show me my monitor resolution settings").

A control the user turns on has neither failure mode. This file pins what that buys, end to end
through `pipe()`:

  * with the control ON, EVERY message reaches the agent — including ones the media and coder
    tiers would otherwise have swallowed, which is the entire point of where the branch sits;
  * ...except the ones the pipe can answer from the scheduler itself, which stay instant and
    never load the agent;
  * OpenWebUI's own internal prompts are still never delegated;
  * the guards that protect against real harm survive the control — the phone gate, per-user
    ownership, and the refusal to hand an ordinary user the host's job list;
  * with the control OFF, routing is exactly what it was.

Fully offline: the scheduler, the agent, the chat model and the GPU handoff are all stubbed, so a
turn that escapes to any of them is a loud failure rather than a real call.

Usage:  python3 tests/test_task_mode.py [pipe_path]
"""
import asyncio, importlib.util, json, os, re, sqlite3, sys, tempfile, time

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
FILTER_PATH = "/home/ohmz/ai-stack/filters/task_mode.py"
OWUI_DB = "/volume1/docker/openwebui/config/webui.db"

spec = importlib.util.spec_from_file_location("aa_task", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


JOBS = [{"id": "a1a1a1a1a1a1", "name": "acme widget stock watch",
         "schedule_display": "every 30m", "state": "scheduled", "enabled": True}]
ADMIN = {"role": "admin", "email": "root@example.com"}
ALICE = {"role": "user", "email": "alice@example.com"}

TMP = tempfile.mkdtemp()
SENT = []      # every _hermes_stream call: text, brief, verify, scoped
CHAT = []      # every escape to the chat model — must stay empty under the control
MEDIA = []     # every render attempt — must stay empty under the control
EVICT = []     # every chat-tenant eviction, i.e. every agent load
METRICS = []


def stack(owners=None, phone="+15145550123"):
    p = mod.Pipe()
    state = {"jobs": [dict(j) for j in JOBS]}
    path = os.path.join(TMP, f"own{len(os.listdir(TMP))}.json")
    json.dump(owners if owners is not None
              else {j["id"]: {"h": "root", "t": time.time()} for j in JOBS}, open(path, "w"))
    mod.TASK_OWNERS_FILE = path
    SENT.clear(); CHAT.clear(); MEDIA.clear(); EVICT.clear(); METRICS.clear()

    def api(method, path_, body=None, timeout=10):
        if path_.startswith("/api/jobs?"):
            return 200, {"jobs": state["jobs"]}, None
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
    # Every agent load evicts the 18 GB chat tenant. "the deterministic path does not evict" is
    # otherwise unobservable, and it is the reason listing stays instant with the control on.
    p._release_chat_tenant = lambda: EVICT.append(True)
    def media(*a, **kw):
        MEDIA.append(True)

        async def go():
            return "![stub](data:,)"
        return go()

    for name in ("_gen_and_cache", "_gen_video_and_cache", "_gen_i2v_and_cache",
                 "_gen_multishot_and_cache"):
        setattr(p, name, media)
    p._metric = lambda **f: METRICS.append(f)
    saved = {}
    p._contact = lambda h: ({"phone": phone} if phone else dict(saved))
    p._save_phone = lambda h, e164: (saved.update(phone=e164), True)[1]
    p._state = state
    return p


def say(p, text, user=ADMIN, on=True, task=None, cid="c1"):
    """One full pipe() turn. `on` is the Task control, which rides in metadata.filter_ids."""
    p._chat_id = lambda *a, **k: cid
    md = {"user_prompt": text, "chat_id": cid,
          "filter_ids": [mod.TASK_MODE_ID] if on else []}

    async def go():
        res = await p.pipe({"messages": [{"role": "user", "content": text}],
                            "model": "auto_assistant.auto"},
                           __metadata__=md, __user__=user, __task__=task)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    return asyncio.run(go())


def main():
    print("--- ON: every message reaches the agent, whatever it looks like ---")
    # These are exactly the messages the tiers below the branch would have claimed. Reaching the
    # agent anyway is the whole reason the branch sits above them.
    for t, why in [("draw a cat", "the renderer would have taken it"),
                   ("write me a python script to sort a list", "the coder would have taken it"),
                   ("what's the weather", "chat would have taken it"),
                   ("make a video of a dog running", "the video tier would have taken it"),
                   ("can you tracker the price that its under 20", "nothing would have taken it")]:
        p = stack()
        say(p, t)
        check(f"{why}: {t[:42]!r}",
              len(SENT) == 1 and not CHAT and not MEDIA, f"sent={len(SENT)} chat={CHAT} media={MEDIA}")

    print("--- ON: the two phrasings that failed live ---")
    URL = "https://www.amazon.ca/Anker-Type-C/dp/B08PVPTNZL/?th=1"
    p = stack()
    say(p, f"track the item {URL} when the price is under 10")
    check("a monitoring verb alone is enough to schedule now",
          SENT and SENT[0]["brief"] == "cron" and SENT[0]["verify"] is True, repr(SENT[:1])[:200])
    p = stack()
    say(p, "can you tracker the price that its under 20")
    check("...but a typo with no schedule ANSWERS rather than scheduling",
          SENT and SENT[0]["brief"] == "research" and SENT[0]["verify"] is False, repr(SENT[:1])[:200])
    check("the research contract still forbids creating jobs",
          "not create" in mod.Pipe._RESEARCH_BRIEF.lower())

    print("--- ON: 'change the alert to X' edits, it does not research or create ---")
    # Live, 2026-08-10: this exact phrasing matched neither _MANAGE_VERB (cancel/pause/resume take
    # no new value) nor _BG_VERB/_BG_RECURRENCE (create-a-new-watch signals) — the typo "2 Horus"
    # doesn't even match \d+\s+hours. It fell to the research brief, which flatly forbids touching
    # cron jobs ("Do NOT create, modify or mention cron jobs") — and the agent edited anyway, with
    # no rules at all: it renamed the job to "2 Horus" and dropped the "for 2 hours" bound.
    for t in ("change the alert to 15 mins for 2 Horus",       # the literal live message, typo included
              "change the alert to 15 mins for 2 hours",       # the same request, spelled correctly
              "adjust the schedule to every 6 hours",
              "please can you update the frequency to daily",
              "reschedule it to run hourly"):
        p = stack()
        say(p, t)
        check(f"routes to the edit brief, verified: {t[:46]!r}",
              SENT and SENT[0]["brief"] == "edit" and SENT[0]["verify"] is True,
              repr(SENT[:1])[:220])
    check("the edit brief tells the agent PATCH is partial and never to rename unasked",
          "partial" in mod.Pipe._EDIT_BRIEF.lower()
          and "never include 'name'" in mod.Pipe._EDIT_BRIEF.lower(), mod.Pipe._EDIT_BRIEF[:400])
    check("...and to translate a bound into a repeat count, the same as creating one",
          "repeat" in mod.Pipe._EDIT_BRIEF.lower() and "8" in mod.Pipe._EDIT_BRIEF,
          mod.Pipe._EDIT_BRIEF[:800])

    print("--- ON: edit phrasing does not steal messages that are actually about something else ---")
    for t, why in [("track the item https://x.example/y when the price is under 10",
                    "a genuine new-watch request — _BG_VERB/_BG_RECURRENCE already claim it"),
                   ("cancel my price alert", "a manage verb — _MANAGE_VERB already claims it")]:
        p = stack()
        say(p, t)
        check(f"{why}: {t[:42]!r}",
              not SENT or SENT[0]["brief"] != "edit", repr(SENT[:1])[:200])

    print("--- ON: what the pipe can answer itself, it answers — without loading the agent ---")
    p = stack()
    out = say(p, "list my trackers")
    check("the table is rendered locally", "acme widget" in out, out[:160])
    check("...the agent was never asked", SENT == [], repr(SENT))
    check("...and the chat tenant was never evicted", EVICT == [], repr(EVICT))

    print("--- ON: a fare is not delegated, because the agent cannot watch one ---")
    # REPORTED LIVE, 2026-08-09. The flight gate was fixed and verified through OWUI's own loader,
    # and the user still got job 0cf56b8c3afd out of the real UI: the Task control sits ABOVE both
    # flight branches in pipe(), so with the chip on, the gate is unreachable and the ask goes
    # straight to the agent — which builds a fare watch with no itinerary. The control decides
    # WHETHER to delegate, never WHAT the request is, and a fare is the second thing (after the
    # scheduler reads above) that this pipe answers without an agent at all.
    p = stack()
    out = say(p, "track price from Toronto to Vancouver and text me if the price is under 1000, "
                 "check every 15 mins next 2 hours")
    check("a route fare ask under the control is answered, not delegated", SENT == [], repr(SENT))
    check("...and it asks for the dates it does not have",
          "Depart" in out and "need this" in out, out[:200])
    check("...naming the route it did resolve", "YTO" in out and "YVR" in out, out[:200])
    p = stack()
    out = say(p, "track flights from Toronto to Vancouver departing September 15 returning "
                 "September 22, text me under 600")
    check("a complete itinerary under the control answers with the timeline",
          SENT == [] and "Depart" in out and "Return" in out, out[:220])
    # The exception must be exactly this shape and no wider: an ordinary watch still delegates.
    p = stack()
    say(p, f"track the item {URL} when the price is under 10")
    check("a product watch under the control still reaches the agent",
          len(SENT) == 1 and SENT[0]["brief"] == "cron", repr(SENT[:1])[:160])
    p = stack()
    say(p, "how much does it cost to ship a package from toronto to vancouver")
    check("a route travelled another way is not claimed by the flight path", SENT != [], repr(SENT))

    print("--- ON: OpenWebUI's own prompts are still not the user talking ---")
    p = stack()
    say(p, "track the price every hour", task="title_generation")
    check("a task-kwarg turn is never delegated", SENT == [] and CHAT == [], repr(SENT))
    p = stack()
    say(p, "### Task:\nGenerate a concise, 3-5 word title for this chat.")
    check("...nor is one detected by its prefix", SENT == [] and CHAT == [], repr(SENT))

    print("--- ON: the guards that prevent real harm survive the control ---")
    p = stack(phone=None)
    out = say(p, "track the rtx price every 6h and text me")
    check("no number on file asks first, and schedules nothing",
          "number" in out.lower() and SENT == [], out[:120])
    p2 = stack(phone=None)
    say(p2, "track the rtx price every 6h and text me")
    out = say(p2, "514-555-0123")
    check("...then the number is accepted on the next turn", "Saved" in out or SENT, out[:120])

    p = stack(owners={"a1a1a1a1a1a1": {"h": "root", "t": time.time()}})
    out = say(p, "list my trackers", user=ALICE)
    check("another user's job is not shown to them",
          "acme widget" not in out and SENT == [], out[:200])

    old = mod.MANAGE_DETERMINISTIC
    try:
        mod.MANAGE_DETERMINISTIC = False
        p = stack()
        out = say(p, "list my tasks", user=ALICE)
        check("with listing switched off an ordinary user is refused, not handed to the agent",
              "switched off" in out and SENT == [], (out or "")[:160])
        p = stack()
        say(p, "list my tasks", user=ADMIN)
        check("...while an admin still falls through", len(SENT) == 1, repr(SENT))
    finally:
        mod.MANAGE_DETERMINISTIC = old

    print("--- ON: consent is the control itself, and that is recorded ---")
    p = stack()
    say(p, "monitor the rtx price every 6 hours")
    skipped = [m for m in METRICS if m.get("outcome") == "skipped_task_mode"]
    check("the confirmation prompt is skipped", len(SENT) == 1, repr(SENT)[:120])
    check("...but written down, so the decline rate stays an honest measure", bool(skipped),
          repr(METRICS)[:200])
    rows = [m for m in METRICS if m.get("job") == "route"]
    check("every route row marks the mode and which signal carried it",
          rows and all(r.get("task_mode") and r.get("src") for r in rows), repr(rows)[:220])
    check("...at tier 0 — a declaration, not a guess",
          all(r.get("tier") == 0 for r in rows), repr(rows)[:200])

    print("--- OFF: routing is exactly what it was (regression fence) ---")
    for t, want in [("draw a cat", "media"), ("write me a python script to sort a list", "chat"),
                    ("what's the weather", "chat"), ("monitor the rtx price every 6 hours", "agent")]:
        p = stack()
        say(p, t, on=False)
        got = "agent" if SENT else "chat" if CHAT else "media" if MEDIA else "local"
        check(f"OFF: {t[:38]!r} still routes to {want}", got == want, f"got {got}")

    print("--- the mode is read per turn, never remembered ---")
    p = stack()
    say(p, "monitor the rtx price every 6 hours", on=True)
    n_on = len(SENT)
    say(p, "draw a cat", on=False, cid="c1")
    check("turning it off in the same chat takes effect immediately",
          len(SENT) == n_on and bool(MEDIA), f"sent={len(SENT)} media={MEDIA}")

    print("--- either signal alone is enough to enter the mode ---")
    p = stack()
    p._chat_id = lambda *a, **k: "c9"

    async def stamped_only():
        res = await p.pipe({"messages": [{"role": "user", "content": "what's the weather"}],
                            "model": "auto_assistant.auto"},
                           # No filter_ids: the filter ran (so it stamped) but the client sent none.
                           __metadata__={"user_prompt": "what's the weather", "chat_id": "c9",
                                         "task_mode": True},
                           __user__=ADMIN)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    asyncio.run(stamped_only())
    check("the filter's own stamp is honoured without the client's list", len(SENT) == 1, repr(SENT))
    check("...and the row says which signal it was",
          any(r.get("src") == "stamp" for r in METRICS if r.get("job") == "route"),
          repr([r for r in METRICS if r.get("job") == "route"])[:200])

    print("--- the filter stands the other modes down, server-side ---")
    fspec = importlib.util.spec_from_file_location("tm", FILTER_PATH)
    tm = importlib.util.module_from_spec(fspec)
    fspec.loader.exec_module(tm)
    f = tm.Filter()
    body = {"features": {"web_search": True, "code_interpreter": True, "image_generation": True}}
    meta = {}
    out = f.inlet(body, meta)
    check("web search is turned off", out["features"]["web_search"] is False, repr(out["features"]))
    check("the code interpreter is turned off", out["features"]["code_interpreter"] is False)
    check("image generation is turned off", out["features"]["image_generation"] is False)
    check("and the turn is stamped for the pipe", meta.get("task_mode") is True, repr(meta))
    check("a body with no features grows one rather than raising",
          f.inlet({}, {})["features"]["web_search"] is False)
    check("a null features block is survivable too",
          f.inlet({"features": None}, {})["features"]["web_search"] is False)
    check("it never touches the messages — injected text would be read as the request",
          f.inlet({"messages": [{"role": "user", "content": "hi"}]}, {})["messages"]
          == [{"role": "user", "content": "hi"}])

    print("--- the two files agree, and the control is actually installed ---")
    fsrc = open(FILTER_PATH, encoding="utf-8").read()
    psrc = open(PIPE_PATH, encoding="utf-8").read()
    fid = re.search(r'^TASK_MODE_ID\s*=\s*"([^"]+)"', fsrc, re.M)
    pid = re.search(r'^TASK_MODE_ID\s*=\s*"([^"]+)"', psrc, re.M)
    check("the filter declares an id", bool(fid), fsrc[:80])
    check("the pipe declares one too", bool(pid), "missing TASK_MODE_ID in the pipe")
    check("...and they are the same string — a rename on one side is a silent no-op",
          bool(fid and pid) and fid.group(1) == pid.group(1),
          f"{fid and fid.group(1)!r} vs {pid and pid.group(1)!r}")
    # Without this the filter is ALWAYS-ON: it would force web search and the code interpreter off
    # on every turn of every chat, with nothing in the interface to show for it.
    check("the filter is a toggle, not an always-on filter",
          bool(re.search(r"^toggle\s*=\s*True\s*$", fsrc, re.M)))
    # ...and it has to be on the INSTANCE. OpenWebUI instantiates the Filter and reads `toggle`
    # off that object, so a module-level declaration alone loads cleanly, passes every static
    # check, and still produces no control in the interface — which is exactly what happened.
    check("...and the INSTANCE exposes it, which is what OpenWebUI actually reads",
          getattr(tm.Filter(), "toggle", None) is True)
    check("...along with the icon, resolved the same way",
          str(getattr(tm.Filter(), "icon", "")).startswith("data:image/svg+xml"))
    check("it carries frontmatter starting on line 1 (or the icon never reaches the UI)",
          fsrc.startswith('"""\n') and "\ntitle: " in fsrc[:400])

    if os.path.exists(OWUI_DB):
        try:
            db = sqlite3.connect(f"file:{OWUI_DB}?mode=ro", uri=True)
            fid_s = fid.group(1)
            row = db.execute("select is_active from function where id=?", (fid_s,)).fetchone()
            check("the function is installed and active", bool(row) and row[0] == 1, repr(row))
            meta_row = db.execute("select meta from model where id='auto_assistant.auto'").fetchone()
            attached = fid_s in json.loads(meta_row[0]).get("filterIds", []) if meta_row else False
            # Installed but unattached is the silent total failure: no control, nothing sent, and
            # every other check in this file still passes.
            check("...and attached to the assistant, or the control never appears", attached,
                  repr(meta_row[0])[:200] if meta_row else "no model row")
            db.close()
        except Exception as e:
            print(f"  [SKIP] database not readable here ({type(e).__name__})")
    else:
        print("  [SKIP] no OpenWebUI database on this host")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

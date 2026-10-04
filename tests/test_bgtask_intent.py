#!/usr/bin/env python3
"""Background-task intent: do NOT hand ordinary conversation to the hermes agent.

Why this file exists. On 2026-07-29 the pipe gained a fourth route: requests for standing jobs
("monitor this price for 2 weeks") are delegated to a local hermes-agent gateway, which creates a
GPU-guarded cron job and posts results to the background-tasks channel. That delegation starts an
agent session that may run tools and create persistent scheduled state — strictly more consequential
than a wrong chat answer, so the predicate follows the same DEFAULT-DENY discipline
test_media_intent.py enforces for renders: mentioning a monitor is not asking for one.

The three ways in, mirroring the media predicates' shape:
  /task prefix        — the explicit escape hatch
  management verbs    — list/cancel/pause aimed at existing tasks
  imperative verb     — monitor/track/watch/alert/remind AND evidence of recurrence
                        (a schedule word, a bounded duration, or an alert condition).
Questions are rejected before anything else: asking ABOUT monitoring is chat.

Ordering in the pipe matters and is asserted here structurally: media intent is checked first
(a request to render a "security guard monitoring screens" stays a render), and the bg-task check
runs before coder routing ("track the price and alert me" must not reach the coder).

Usage:  python3 tests/test_bgtask_intent.py [pipe_path]
"""
import asyncio, importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/StudioProjects/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_bg", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Must delegate to hermes.
YES = [
    "monitor the price of the RTX 5090 on newegg for 2 weeks",
    "/task check hacker news every morning and summarize",
    "track BTC and alert me when it drops below 60k",
    "watch this product page daily and tell me when it's back in stock",
    "remind me in 2 hours to take the bread out",
    "notify me if the price falls under $500, check every 6 hours",
    "keep an eye on the ollama github releases every day for a month",
    "monitor r/localllama weekly for posts about qwen",
    "track the flight price to karachi for the next 10 days",
    "ping me when it goes below 300, check hourly",
    "list my background tasks",
    "show my scheduled jobs",
    "cancel the price monitor",
    "pause the btc tracking job",
    "/tasks",                          # the plural fell through to chat for a month
    "what are my scheduled tasks?",    # was dead code: _BG_QUESTION won before _BG_MANAGE ran
    "what are my background tasks",
    "stop tracking the gpu price",     # bare 'tracking' with a THING as object stays manageable
    # The live 2026-08-05 phrasing that fell through to chat: "create alert" matched no verb,
    # and neither "is under 150" nor "every 5 mins" / "for next 20 mins" counted as recurrence.
    "create alert to search online for Google Fitbit Air when the price is under 150, "
    "send me the link and notify me on text, check every 5 mins for next 20 mins",
    "notify me when the price is under $150, check every 5 mins",
    "create an alert for the fitbit air when the price is below 150, check hourly",
    "track the flight price to karachi and alert me when the fare is under 900, check daily",
    "set up a price alert for the fitbit air, every 6 hours for a week",
    "create alert for fitbit air when price is under 150, check hourly",   # bare, article-free
    "set up alerts for the fitbit air when the price is under 150, check every 5 mins",
]

# Must NOT delegate (ordinary conversation, questions, coder work, media).
NO = [
    "I watched a great video about sourdough yesterday",
    "what's a good price tracker app?",
    "how do I monitor GPU temperature in linux?",
    "the price of eggs is crazy right now",
    "track and field is my favorite sport",
    "watch out for that bug in the parser",
    "my monitor resolution is stuck at 1080p",
    "can I track a package with python?",
    "write a script that monitors a folder for changes",   # coder's job, not a standing task
    "keep an eye on the kids tonight",
    "she watches the news every morning",
    "monitor lizards are fascinating animals",
    "is there a way to watch netflix on linux?",
    "alert fatigue is a real problem in ops teams",
    "stop tracking me",                       # a person as object is a privacy plea, not job mgmt
    "cancel my job application",              # 'job' heading a non-task noun phrase
    "cancel my job apps",                     # ...including the clipped plural
    "can you stop tracking me across websites?",
    "/taskscheduler is a windows thing, right?",
    # The manage-before-question reorder must NOT open trivia to the manage arm: these are
    # general-knowledge questions that happen to end in a task noun. Caught by adversarial
    # review of the reorder — routing any of these delegates to hermes consent-free.
    "what are the biggest jobs in tech?",
    "what are the best jobs for new grads",
    "what are the night watches in game of thrones?",
    "what are some good monitors for gaming?",
    # The create-alert verb arm requires the alert noun, and the widened recurrence arms stay
    # inside the guarded when/if window — these probe the borders that widening opened.
    "how do I create an alert in grafana?",
    "create an alert dialog in react",
    "make a tracker app with react",
    "when the price is right you should buy it",
    # The alert-noun must end its phrase or take a complement — artifact nouns must not count.
    "create an alert rule in prometheus for when the value goes above 90",
    "make a tracker component that polls the api every 5 min",
    "set up a stock alert widget in my react app, poll every 5 min",
    "add a price alert column to the spreadsheet, one for every day of the week",
    "set up a watch party for friday, every week",
    # Short human durations are talk, not schedules.
    "keep an eye on the oven for 20 mins",
    "watch the kids for 30 mins",
    # Comparatives without a number are prose, not thresholds.
    "ping me when it's under review",
    "notify me if the price is under warranty",
    "remind me when the cost is more than I can afford",
]

# Media must win first: these mention monitoring but ask for a render.
MEDIA_FIRST = [
    "make a picture of a security guard monitoring screens",
    "create a video of a trader watching price charts",
]

results = []


def check(label, ok, detail=None):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label[:70]}"
          + (f"   {detail!r}"[:300] if detail is not None and not ok else ""))


HERMES_REPLY = ("Created two watches: `abc123abc123` (RTX 5090) and `def456def456` (RTX 5080), "
                "both every 6 hours.")
CHAT_REPLY = "Paris is the capital of France."


def followup_rig(hermes_reply=HERMES_REPLY, chat_reply=CHAT_REPLY):
    """A pipe whose hermes stream, chat stream and scheduler are stubs. Records who answered.
    Either reply may be a list, consumed one per call (the last one repeats)."""
    q = mod.Pipe()
    sent, chat = [], []

    def pick(r, n):
        return r if isinstance(r, str) else r[min(n, len(r) - 1)]

    def api(method, path, body=None, timeout=10):
        return (200, {"jobs": []}, None) if path.startswith("/api/jobs") else (200, {}, None)

    def hermes(text, uname="user", verify_creation=False, brief=None, scoped=False):
        sent.append(text)
        out = pick(hermes_reply, len(sent) - 1)

        async def go():
            yield out
        return go()

    def achat(*a, **kw):
        chat.append(True)
        out = pick(chat_reply, len(chat) - 1)

        async def go():
            yield out
        return go()

    q._hermes_api, q._hermes_stream, q._achat_stream = api, hermes, achat
    q._contact = lambda h: {"phone": "+15145550123"}
    q._is_code_request = lambda t: False
    q._route_metric = lambda *a, **k: None
    q._metric = lambda **f: None
    return q, sent, chat


ADMIN = {"role": "admin", "email": "tester@example.com"}
BOB = {"role": "user", "email": "bob@example.com", "name": "bob"}


def pipe_turn(q, history, text, cid="c-followup", user=ADMIN, keep=True, meta=None):
    """One full pipe() turn (default path unless `meta` turns a control on). Appends both sides
    to `history` unless keep=False: a branch OpenWebUI later discards (an edited message)."""
    msgs = history + [{"role": "user", "content": text}]

    async def go():
        res = await q.pipe({"messages": msgs, "model": "auto_assistant.auto", "chat_id": cid},
                           __metadata__={"user_prompt": text, "chat_id": cid, **(meta or {})},
                           __user__=user)
        return "".join([c async for c in res]) if hasattr(res, "__aiter__") else res
    out = asyncio.run(go())
    if keep:
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": out}])
    return out


def main():
    p = mod.Pipe()
    print("--- must delegate ---")
    for t in YES:
        check(t, p._is_bg_task_request(t))
    print("--- must NOT delegate ---")
    for t in NO:
        check(t, not p._is_bg_task_request(t))
    print("--- media renders must still win (pipe checks media first) ---")
    for t in MEDIA_FIRST:
        # The structural claim: even if the bg predicate matched, the pipe's ordering sends these
        # to the render path. Assert the predicate itself stays quiet so ordering never matters.
        check(t, (p._is_image_request(t) or p._is_video_request(t)) and not p._is_bg_task_request(t))

    print("--- /research and /agent are word-bounded (the /agenda bug) ---")
    # startswith("/agent") also captured "/agenda review monday", and the anchored strip then
    # mangled it to "a review monday" before shipping it to hermes as a research question.
    ONESHOT = mod.Pipe._BG_ONESHOT
    check("/research fires", bool(ONESHOT.match("/research best 24GB gpu under 500")))
    check("/agent fires behind leading spaces", bool(ONESHOT.match("  /agent check the notes")))
    check("/agenda does NOT fire", not ONESHOT.match("/agenda review monday"))
    check("/researching does NOT fire", not ONESHOT.match("/researching apples"))
    m = ONESHOT.match("/research   best gpu")
    check("the question survives the strip intact", "/research   best gpu"[m.end():] == "best gpu")

    print("--- conversational follow-ups continue the task exchange, but ONLY there ---")
    # Live failure: the agent asked "re-enable this one, or create new?"; the user answered
    # "yes reenable"; that matched no bg predicate, went to the CHAT model, and produced a
    # confident hallucinated confirmation citing the real job id it had read from the transcript.
    MARK = mod.Pipe._BG_MARK
    after_task = [{"role": "assistant", "content": "Job `37d9907d5dfa` already exists. "
                                                   "Re-enable it or create a new one?" + MARK}]
    after_chat = [{"role": "assistant", "content": "Paris is the capital of France."}]
    for t in ["yes reenable", "yes re-enable it", "go ahead", "resume it", "the first one",
              "cancel it", "ok", "both"]:
        check(f"follow-up after a task reply: {t!r}", p._is_bg_followup(t, after_task))
    for t in ["yes reenable", "yes", "ok thanks", "go ahead", "cancel it"]:
        check(f"same words in ordinary chat stay chat: {t!r}", not p._is_bg_followup(t, after_chat))
    check("a long message is not a follow-up",
          not p._is_bg_followup("yes and also please write a detailed essay about scheduling "
                                "systems and their history in computing", after_task))
    check("hermes replies carry the invisible marker", MARK.startswith("<!--") and MARK.endswith("-->"))

    print("--- ...and the window closes at the first reply hermes did not write ---")
    # The marker used to be set by every hermes reply and cleared by nothing, so for PARK_TTL_S
    # (24 h) any short "no ..."/"remove ..." went to hermes: past the confirm gate, with the CHAT
    # model's last answer quoted back as "you previously said", and told to act on the real
    # scheduler. End to end through pipe(), because the fix lives in pipe()'s ordering.
    q, sent, chat = followup_rig()
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    check("setup: the watch request reached hermes", len(sent) == 1)
    out = pipe_turn(q, hist, "no, remove the second one")
    check("straight after a hermes reply, 'no, remove the second one' reaches hermes",
          len(sent) == 2 and out == HERMES_REPLY and not chat)
    check("...as a follow-up that quotes what hermes itself said",
          len(sent) == 2 and "You previously said" in sent[1] and "def456def456" in sent[1])

    q, sent, chat = followup_rig()
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    out = pipe_turn(q, hist, "what is the capital of France?")
    check("setup: an ordinary question in between is answered by the chat model",
          out == CHAT_REPLY and len(chat) == 1 and len(sent) == 1)
    check("...and that chat reply ends the follow-up window",
          not q._was_bg_turn("c-followup", hist + [{"role": "user", "content": "no"}]))
    out = pipe_turn(q, hist, "no, remove the second one")
    check("after an intervening chat reply, 'no, remove the second one' does NOT reach hermes",
          len(sent) == 1 and out == CHAT_REPLY and len(chat) == 2)
    pipe_turn(q, hist, "monitor the price of the RTX 5080 on newegg for 2 weeks")
    out = pipe_turn(q, hist, "yes, cancel the first one")
    check("a new hermes reply reopens it for the very next turn",
          len(sent) == 3 and out == HERMES_REPLY)

    print("--- the window belongs to the hermes REPLY, not to whichever turn comes next ---")
    # The one-turn marker was consumed by any pipe() call, so a turn the user later discarded, or
    # a clarifying question, spent it, and the answer to hermes's own question went to the
    # tool-less chat model: the live failure behind _BG_FOLLOWUP, where it confirmed a
    # re-enable that never happened, citing the real job id from the transcript.
    Q = "Job `37d9907d5dfa` is finished. Re-enable it, or create a new one?"
    q, sent, chat = followup_rig(hermes_reply=Q, chat_reply="The weather is mild today.")
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    pipe_turn(q, hist, "hmm what is the weather", keep=False)       # edited away afterwards
    out = pipe_turn(q, hist, "yes reenable")                          # ...into this
    check("an edited message after hermes's question still reaches hermes",
          len(sent) == 2 and out == Q, (len(sent), out))
    check("...quoting hermes's question", len(sent) == 2 and "37d9907d5dfa" in sent[1])

    q, sent, chat = followup_rig(hermes_reply=Q,
                                 chat_reply="Re-enabling resumes the old job with its old schedule.")
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    out = pipe_turn(q, hist, "what does re-enable mean?")
    check("setup: a clarifying question is answered by the chat model", len(chat) == 1)
    out = pipe_turn(q, hist, "yes reenable")
    check("after one clarifying exchange, 'yes reenable' still reaches hermes",
          len(sent) == 2 and out == Q, (len(sent), out))
    check("...quoting HERMES's question, never the chat model's answer",
          len(sent) == 2 and "37d9907d5dfa" in sent[1] and "old schedule" not in sent[1])
    pipe_turn(q, hist, "what is the capital of France?")
    pipe_turn(q, hist, "and of Italy?")
    out = pipe_turn(q, hist, "yes")
    check("...but only one exchange away: after two, a bare 'yes' is chat again",
          len(sent) == 2 and len(chat) == 4, (len(sent), len(chat)))

    q, sent, chat = followup_rig(hermes_reply=Q, chat_reply=[
        "Paris is the capital of France. Want to know more about it?", CHAT_REPLY])
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")
    pipe_turn(q, hist, "what is the capital of France?")
    out = pipe_turn(q, hist, "yes please")
    check("a 'yes please' that answers the CHAT model's own question stays chat",
          len(sent) == 1 and len(chat) == 2, (len(sent), len(chat)))

    q, sent, chat = followup_rig(hermes_reply=Q)
    pipe_turn(q, [], "monitor the price of the RTX 5090 on newegg for 2 weeks")
    hist = []
    pipe_turn(q, hist, "what is the capital of France?")               # the SAME turn, edited
    check("editing the message that produced the hermes reply drops the record",
          "c-followup" not in q._bg_turn)
    out = pipe_turn(q, hist, "yes please")
    check("...so a later 'yes please' is chat, not hermes", len(sent) == 1 and len(chat) == 2)

    q, sent, chat = followup_rig(hermes_reply=Q)
    pipe_turn(q, [], "monitor the price of the RTX 5090 on newegg for 2 weeks")
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks")  # regenerate
    out = pipe_turn(q, hist, "yes reenable")
    check("a regenerated hermes reply keeps the window open for its answer",
          len(sent) == 3 and out == Q, (len(sent), out))

    print("--- an ordinary user's job-changing follow-up never goes to hermes ---")
    # hermes's cronjob tool sees every job on the host; the scoped context only tells it not to
    # describe other users' jobs. "no, delete them all" matched _BG_FOLLOWUP (no task noun for
    # _BG_MANAGE, no job table for `referring`) and went to hermes unconfirmed.
    for label, user, text, to_hermes in (
            ("user: 'no, delete them all'", BOB, "no, delete them all", False),
            ("user: 'remove both'", BOB, "remove both", False),
            ("user: 'cancel all of them'", BOB, "cancel all of them", False),
            ("user: 'yes reenable' (a state change too)", BOB, "yes reenable", False),
            ("user: 'yes please' is not a change: hermes", BOB, "yes please", True),
            ("admin: 'no, delete them all' is unchanged: hermes", ADMIN, "no, delete them all",
             True)):
        q, sent, chat = followup_rig()
        managed = []
        real_manage = q._manage_turn

        async def manage(cid, text, parked, rule, pending=None, user=None, handle="",
                         _real=real_manage, _log=managed):
            _log.append((text, rule, handle))
            return await _real(cid, text, parked, rule, pending=pending, user=user, handle=handle)
        q._manage_turn = manage
        hist = []
        pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks", user=user)
        out = pipe_turn(q, hist, text, user=user)
        if to_hermes:
            check(label, len(sent) == 2 and not managed, (len(sent), managed))
        else:
            check(label + " -> the ownership-checked manage path",
                  len(sent) == 1 and managed and managed[-1][1] == "bg_followup_scoped"
                  and out != HERMES_REPLY, (len(sent), managed, out[:120]))
    q, sent, chat = followup_rig()
    hist = []
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks", user=BOB)
    out = pipe_turn(q, hist, "both of them, delete", user=BOB)
    check("user: a change the manage path cannot place is refused with a way forward",
          len(sent) == 1 and "list my tasks" in out and not chat, out[:160])
    q, sent, chat = followup_rig()
    hist = []
    task_on = {"task_mode": True}
    pipe_turn(q, hist, "monitor the price of the RTX 5090 on newegg for 2 weeks", user=BOB,
              meta=task_on)
    out = pipe_turn(q, hist, "no, delete them all", user=BOB, meta=task_on)
    check("...and the same under the Task control", len(sent) == 1 and out != HERMES_REPLY,
          (len(sent), out[:120]))
    ctx_src = open(PIPE_PATH).read()
    check("the scoped context forbids changing a job outside the user's list",
          '"describe one, and never treat one as this user\'s duplicate. Never modify, "' in ctx_src
          and '"pause, resume or delete any job that is not in this list."' in ctx_src)

    print("--- a FINISHED job must not block a new one (live failure 37d9907d5dfa) ---")
    brief = mod.Pipe._HERMES_BRIEF
    for phrase in ("NEVER write your own script into a job", "never printed",
                   "'every Nm'", "ONE-SHOT that runs once and deletes itself",
                   "repeat 4", "WITHOUT a fake browser User-Agent",
                   "ACTIVE and still has runs left", "completed, exhausted, disabled",
                   "create a NEW one instead", "already running"):
        check(f"rule 6 covers {phrase!r}", phrase in brief)

    # The ban is on MODEL-AUTHORED scripts, not on script mode itself: the stack ships a tested
    # extractor, and pointing a job at it is the whole reason hallucinated prices stopped. A brief
    # that bans script mode outright would forbid the fix for the bug it was written about.
    check("brief routes price monitoring to the vetted extractor",
          "scripts/price_watch.py" in brief and "do NOT write your own scraper" in brief)
    check("...and prints its output verbatim rather than summarising it",
          "print its output verbatim" in brief)
    # 5d-ii: the no-URL case that produced the live "Verification failed" — the agent had no
    # vetted recipe for "search online for X", improvised, and narrated a job it never created.
    check("brief routes NO-URL watches to the search-first extractor",
          "scripts/price_search.py" in brief and "--query" in brief)
    check("...and forbids asking for a link or improvising a search job",
          "do NOT ask for one" in brief and "never a URL" in brief)
    check("...and keeps URL-bearing requests on price_watch",
          "rule 5d applies instead" in brief)
    # 5d-iii: stock was advertised in the kinds list and in the docs for months while --kind was
    # applied AFTER a numeric comparison, so "tell me when it's back in stock" fired on a price
    # threshold or never fired at all. The brief has to name the mode, or the bug comes back as
    # prose: rule 5d used to say it handled "price, stock, fare, availability" itself.
    check("brief routes stock and availability to --mode stock",
          "5d-iii" in brief and "--mode stock" in brief)
    check("...and says why the flag is not optional",
          "--mode stock is REQUIRED" in brief and "never fires" in brief)
    check("...and forbids a price threshold on a stock watch",
          "price threshold in disguise" in brief)
    check("...and promises an unreadable page is reported, not guessed",
          "never guesses 'out of stock'" in brief)
    check("...and that a pre-order is not a restock",
          "pre-order is not a restock" in brief)
    check("rule 5d no longer claims to handle stock itself",
          "WATCHING A PAGE (price, stock, fare, availability)" not in brief
          and "For stock or availability, rule 5d-iii applies instead" in brief)
    check("rule 6b's vetted-extractor exception names 5d-iii too",
          "rules 5d, 5d-ii and 5d-iii" in brief)
    check("...as does the self-contained-prompt rule",
          "rule 5d, 5d-ii or 5d-iii applies" in brief)
    check("a no-URL stock request is told to ask for a link, not sent to price_search",
          "finds pages by their PRICE" in brief)
    # Live miss, job 5b3041dcf6d2: "Toronto to Vancouver flights" got price_drop because the fare
    # pattern only matched the singular, so --kind fare and every fare-specific branch stayed off.
    for phrase in ("track Toronto to Vancouver flights and tell me when it is under $1000",
                   "watch the flight price Toronto to Vancouver",
                   "alert me when fares to karachi drop",
                   "watch round trips to lisbon"):
        check(f"fare wins on {phrase[:44]!r}", mod.Pipe._guess_kind(phrase) == "fare")
    check("out_of_stock is classified BEFORE back_in_stock (both match 'in stock')",
          mod.Pipe._guess_kind("tell me when it is no longer in stock") == "out_of_stock")
    check("...while a real restock request still classifies as back_in_stock",
          mod.Pipe._guess_kind("tell me when the fitbit is back in stock")
          == "back_in_stock")
    check("...and 'sold out' still wins",
          mod.Pipe._guess_kind("text me if it sells out") == "out_of_stock")
    check("brief no longer contradicts itself about alerts being configured",
          "none is configured right now" not in brief)
    check("brief states alerts are configured (agent claimed otherwise)",
          "text message AND an email" in brief and "Never tell the user alerts are unconfigured" in brief)

    print("--- the delegation brief stays paraphrase-proof (learned from job dc1230a29d82) ---")
    brief = mod.Pipe._HERMES_BRIEF
    for phrase in ("LOG:", "ALERT(", "does NOT run any delivery commands",
                   "helper functions (they do not exist)",
                   "including the very first run",
                   "Never add prior-state or transition requirements",
                   "Describing a job is not creating it"):
        check(f"brief contains {phrase!r}", phrase in brief)

    fails = results.count(False)
    n_yes, n_no = len(YES), len(NO) + len(MEDIA_FIRST)
    print(f"\n{len(results)} checks ({n_yes} positive, {n_no} negative) — "
          f"{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

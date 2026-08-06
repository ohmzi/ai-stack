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
import importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
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


def check(label, ok):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label[:70]}")


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

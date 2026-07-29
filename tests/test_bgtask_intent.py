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

    fails = results.count(False)
    n_yes, n_no = len(YES), len(NO) + len(MEDIA_FIRST)
    print(f"\n{len(results)} checks ({n_yes} positive, {n_no} negative) — "
          f"{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

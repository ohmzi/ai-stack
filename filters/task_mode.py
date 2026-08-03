"""
title: Task
author: local
version: 0.1.0
required_open_webui_version: 0.5.0
description: Send this turn to the background-task agent instead of guessing from the wording. While Task is on, Internet and Code are ignored for the turn. Leaving it on costs an agent load (~23 s) on every message.
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuNzUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTkgNEg3YTIgMiAwIDAgMC0yIDJ2MTNhMiAyIDAgMCAwIDIgMmgxMGEyIDIgMCAwIDAgMi0yVjZhMiAyIDAgMCAwLTItMmgtMiIvPjxyZWN0IHg9IjkiIHk9IjIuNSIgd2lkdGg9IjYiIGhlaWdodD0iMy41IiByeD0iMSIvPjxwYXRoIGQ9Im05IDEzIDIgMiA0LTQiLz48L3N2Zz4=
"""

# Why this exists.
#
# Background-task requests were detected by reading the user's wording — an imperative verb plus
# evidence of recurrence. Every phrasing that slipped through got answered by the chat model
# instead ("track the item <url> when the price is under 10" came back as a Python scraping
# script), and every widening of the pattern to catch it bought a new false positive somewhere
# else. Guessing is the wrong mechanism for something that creates persistent scheduled state.
#
# This filter replaces the guess with a declaration. It is a TOGGLE filter, so OpenWebUI renders
# it as a control the user turns on, and it only runs on turns where they did. The pipe reads the
# mode and delegates the whole turn to the hermes agent — no regex gets a vote.
#
# It also enforces the mutual exclusivity the UI shows. That has to happen here rather than in the
# browser: filter inlets run BEFORE OpenWebUI consumes `features` and before web search fires, so
# this is the last point at which "Task is on" can actually mean "Internet is off". A frontend
# that disagrees with the server is how a button silently stops working.

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# Pinned against the pipe's own constant by tests/test_task_mode.py. Renaming it on one side only
# would turn the control into a no-op with no error anywhere.
TASK_MODE_ID = "task_mode"

# LOAD-BEARING. This is what makes OpenWebUI treat the filter as user-controlled: it is stamped
# into the function's meta on save, it is what puts the control in the UI, and it is what makes
# `get_sorted_filter_ids` run this filter ONLY when the user enabled it. Remove it and the filter
# becomes always-on — silently forcing web search and the code interpreter off on every turn of
# every chat, with nothing in the interface to show for it. scripts/deploy_pipe.py refuses to
# deploy a filter whose `toggle` is not True, for exactly that reason.
toggle = True

# Fallback for the icon when the frontmatter manifest is missing (the manifest wins when present).
icon = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9"
    "IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuNzUiIHN0cm9rZS1saW5l"
    "Y2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTkgNEg3YTIgMiAwIDAgMC0yIDJ2MTNh"
    "MiAyIDAgMCAwIDIgMmgxMGEyIDIgMCAwIDAgMi0yVjZhMiAyIDAgMCAwLTItMmgtMiIvPjxyZWN0IHg9IjkiIHk9IjIu"
    "NSIgd2lkdGg9IjYiIGhlaWdodD0iMy41IiByeD0iMSIvPjxwYXRoIGQ9Im05IDEzIDIgMiA0LTQiLz48L3N2Zz4="
)


class Filter:
    class Valves(BaseModel):
        # Ordering does not currently matter: OpenWebUI consumes `features` after every inlet has
        # run, so no other filter can undo this one. Exposed because the platform reads it and
        # because a future filter might legitimately need to sort around this.
        priority: int = Field(
            default=0,
            description="Filter execution order. Ordering is not significant for this filter.",
        )
        disable_web_search: bool = Field(
            default=True,
            description="Turn web search off for turns where Task is on. Web search rewrites the "
                        "message with retrieved context before the assistant sees it, which is "
                        "how a job id once came back as a citation from an unrelated study.",
        )
        disable_code_interpreter: bool = Field(
            default=True,
            description="Turn the code interpreter off for turns where Task is on. Otherwise "
                        "'track this price' is answered with a scraping script instead of being "
                        "scheduled.",
        )
        disable_image_generation: bool = Field(
            default=True,
            description="Turn image generation off for turns where Task is on.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def inlet(
        self,
        body: Dict[str, Any],
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Claim the turn for the background-task agent, and stand the other modes down.

        Runs only when the user enabled Task (OpenWebUI gates toggle filters on that), so there is
        no "is it on?" check to make here — being called IS the signal.

        Timing is the whole reason this works: inlets run before OpenWebUI reads `features` off the
        body, and well before it performs a web search or appends the code-interpreter prompt. By
        the time anything would act on those flags, this has already cleared them.

        Never raises. A filter that throws turns the whole turn into a 500, and a chat that will
        not send is a worse outcome than exclusivity that quietly did not apply.
        """
        try:
            v = self.valves
            features = body.get("features")
            if not isinstance(features, dict):
                features = {}
                body["features"] = features
            if v.disable_web_search:
                features["web_search"] = False
            if v.disable_code_interpreter:
                features["code_interpreter"] = False
            if v.disable_image_generation:
                features["image_generation"] = False

            # A second, independent signal for the pipe. The enabled-filter list says "the client
            # says the control was on"; this says "the filter actually ran, so the lines above
            # actually happened". A direct API caller can produce the first without the second,
            # and that difference should be visible in a metrics row rather than invisible.
            for meta in (__metadata__, body.get("metadata")):
                if isinstance(meta, dict):
                    meta["task_mode"] = True
        except Exception:
            pass
        # Deliberately nothing else. In particular this NEVER writes into `messages`: text spliced
        # into the last user message ends up being read as part of the user's request — by the
        # router, by the agent, and by the title model. The signal stays out of band.
        return body

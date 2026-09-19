"""
title: Notebook
author: local
version: 0.1.0
required_open_webui_version: 0.5.0
description: Answer this turn from one notebook in Open Notebook instead of the usual assistant. Name the notebook in your message ("check islamic guidance on ..."); if it cannot tell which you mean it offers the closest matches. While Notebook is on, Internet, Code and Task are stood down.
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuNzUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTQgMTkuNUEyLjUgMi41IDAgMCAxIDYuNSAxN0gyMCIvPjxwYXRoIGQ9Ik02LjUgMkgyMHYyMEg2LjVBMi41IDIuNSAwIDAgMSA0IDE5LjV2LTE1QTIuNSAyLjUgMCAwIDEgNi41IDJ6Ii8+PHBhdGggZD0iTTkgN2g3Ii8+PHBhdGggZD0iTTkgMTFoNyIvPjwvc3ZnPg==
"""

# Why this exists.
#
# "Answer this from my Islamic guidance notebook" was previously impossible to express: the
# assistant has no notion of a notebook, and nothing in this stack had ever spoken to Open
# Notebook. The nearest thing was hoping the wording would carry the intent through routing.
#
# This filter replaces the guess with a declaration, exactly as Task does. It is a TOGGLE filter,
# so OpenWebUI renders it as a control the user turns on, and it only runs on turns where they
# did. The pipe reads the mode, resolves the named notebook deterministically, and delegates the
# turn to Open Notebook's own retrieval.
#
# It also enforces the mutual exclusivity the UI shows. Filter inlets run BEFORE OpenWebUI
# consumes `features` and before web search fires, so this is the last point at which
# "Notebook is on" can actually mean "Internet is off". A frontend that disagrees with the server
# is how a button silently stops working.
#
# It deliberately does NOT arbitrate against Task. If both are somehow on, the pipe decides (Task
# wins), because a filter fighting another filter is how you get order-dependent behaviour.

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# Pinned against the pipe's own constant by tests/test_notebook_mode.py. Renaming it on one side
# only would turn the control into a no-op with no error anywhere.
NOTEBOOK_MODE_ID = "notebook_mode"

# LOAD-BEARING. This is what makes OpenWebUI treat the filter as user-controlled: it is stamped
# into the function's meta on save, it is what puts the control in the UI, and it is what makes
# `get_sorted_filter_ids` run this filter ONLY when the user enabled it. Remove it and the filter
# becomes always-on — silently hijacking every turn of every chat into a notebook lookup, with
# nothing in the interface to show for it. scripts/deploy_pipe.py refuses to deploy a filter whose
# `toggle` is not True, for exactly that reason.
toggle = True

# Fallback for the icon when the frontmatter manifest is missing (the manifest wins when present).
icon = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9"
    "IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuNzUiIHN0cm9rZS1saW5l"
    "Y2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBhdGggZD0iTTQgMTkuNUEyLjUgMi41IDAgMCAxIDYu"
    "NSAxN0gyMCIvPjxwYXRoIGQ9Ik02LjUgMkgyMHYyMEg2LjVBMi41IDIuNSAwIDAgMSA0IDE5LjV2LTE1QTIuNSAyLjUg"
    "MCAwIDEgNi41IDJ6Ii8+PHBhdGggZD0iTTkgN2g3Ii8+PHBhdGggZD0iTTkgMTFoNyIvPjwvc3ZnPg=="
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
            description="Turn web search off for turns where Notebook is on. Web search rewrites "
                        "the message with retrieved context before the assistant sees it, which "
                        "would compete with the notebook for the answer and could displace the "
                        "notebook's name from the text the resolver reads.",
        )
        disable_code_interpreter: bool = Field(
            default=True,
            description="Turn the code interpreter off for turns where Notebook is on. A question "
                        "about a source should be answered from that source, not computed.",
        )
        disable_image_generation: bool = Field(
            default=True,
            description="Turn image generation off for turns where Notebook is on.",
        )
        answer_engine: str = Field(
            default="auto",
            description="Which engine writes the answer. 'auto' uses Open Notebook's own ask "
                        "endpoint when the running server actually honours a notebook scope, and "
                        "refuses loudly when it does not — it never answers from the whole "
                        "knowledge base while claiming to be scoped. 'open_notebook' pins that "
                        "behaviour; 'local' is reserved for a future fallback in which this "
                        "assistant writes the answer from the notebook's own excerpts.",
        )

    def __init__(self):
        self.valves = self.Valves()
        # These have to be on the INSTANCE, not just at module level. OpenWebUI's loader
        # instantiates the Filter class and then reads `getattr(function_module, "toggle")` off
        # that instance — so a module-level declaration alone is invisible to it, and the control
        # simply never appears in the interface with nothing logged anywhere. Declared once above
        # and mirrored here so the two can never drift.
        self.toggle = toggle
        self.icon = icon

    def inlet(
        self,
        body: Dict[str, Any],
        __metadata__: Optional[Dict[str, Any]] = None,
        __user__: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Claim the turn for Open Notebook, and stand the other modes down.

        Runs only when the user enabled Notebook (OpenWebUI gates toggle filters on that), so there
        is no "is it on?" check to make here — being called IS the signal.

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
                    meta["notebook_mode"] = True
        except Exception:
            pass
        # Deliberately nothing else. In particular this NEVER writes into `messages`: text spliced
        # into the last user message ends up being read as part of the user's request — by the
        # resolver, by the router, and by the title model. The signal stays out of band.
        return body

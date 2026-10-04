#!/usr/bin/env python3
"""The coding policy: which backend a coding turn uses, which model, and why it failed.

Pure and I/O-free on purpose. The launcher (harness/deepseek/deepseek), the Hermes
coding_task plugin and (later) the Code-button bridge all read this, so "local by
default, cloud only on purpose" is written once instead of three times.

It does NOT route. router.py still maps a model NAME to an upstream; this module
answers what a front door should ASK FOR. The distinction is why an unknown model
name can still never bill the API (router.json's `"*"` catches it) while an
unconfirmed cloud ask here is refused outright.

    python3 -c "import coding_policy as c; print(c.resolve())"
"""
from __future__ import annotations

import re

DEFAULT_BACKEND = "local"

LOCAL_MODEL = "qwen38-coder:q4-128k"
LOCAL_CONTEXT_WINDOW = 131072
LOCAL_OUTPUT_RESERVE = 32768

CLOUD_MODEL = "deepseek-flash[1m]"

# The exact vocabulary a front door renders choices from. A free string would let
# two front doors disagree about what "busy" is called.
FailureReason = {"gpu_busy", "model_missing", "harness_error", "context_overflow", "cancelled"}

_REASONS = (
    ("gpu_busy",         re.compile(r"gpu is busy|gpu busy|render(ing)? in progress", re.I)),
    ("model_missing",    re.compile(r"model not found|no such model|not found on PATH", re.I)),
    ("context_overflow", re.compile(r"exceeds the available context size|context length|too long", re.I)),
    ("cancelled",        re.compile(r"cancel", re.I)),
    ("harness_error",    re.compile(r"traceback|exited [1-9]|harness", re.I)),
)


class BackendRefused(Exception):
    """Raised when a backend is asked for without the intent it requires."""


def resolve(backend: str | None = None, confirmed: bool = False) -> dict:
    """(backend, model, context_window, output_reserve) for a coding turn.

    Defaults to local. `cloud` requires `confirmed=True` — the policy never infers
    intent from wording, and an unanswered or unknown backend is refused rather
    than downgraded, because a silent downgrade is the failure this module exists
    to prevent.
    """
    backend = DEFAULT_BACKEND if backend is None else backend
    if backend == "local":
        return {"backend": "local", "model": LOCAL_MODEL,
                "context_window": LOCAL_CONTEXT_WINDOW, "output_reserve": LOCAL_OUTPUT_RESERVE}
    if backend == "cloud":
        if not confirmed:
            raise BackendRefused("cloud requires an explicit confirmation")
        # No window is pinned here: Claude Code keeps its own for the cloud model,
        # and advertising the local one would truncate the cloud model's context.
        return {"backend": "cloud", "model": CLOUD_MODEL,
                "context_window": None, "output_reserve": None}
    raise BackendRefused(f"unknown backend {backend!r} (expected 'local' or 'cloud')")


def reason_from(text: str | None) -> str | None:
    """Map a failure message to one of FailureReason, or None when it names none.

    None is a real answer: a front door must be able to say "failed, cause unknown"
    rather than guess a reason and offer the wrong choices.
    """
    for reason, rx in _REASONS:
        if text and rx.search(text):
            return reason
    return None

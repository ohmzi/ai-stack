"""GPU-aware cron scheduler for a box that shares one RTX 3090 between three tenants.

Why this exists. Hermes cron jobs run agent sessions against hermes-genesis (18.3 GB) via the
local Ollama. ComfyUI needs essentially the whole 24 GB card during an image or video render, and
the OpenWebUI pipe unloads every Ollama model before starting one — a cron job firing mid-render
would load 18 GB straight back into a card the render is counting on. The stock ticker has no idea
the GPU exists.

The built-in InProcessCronScheduler already exposes the right seam: an optional `can_dispatch`
gate, checked before each dispatch, whose skipped ticks leave due jobs intact for the next allowed
tick (cron/scheduler_provider.py). This provider subclasses the built-in and composes that gate
with GPU probes — jobs are never lost, only deferred.

Subclassing matters for a second reason: gateway/run.py passes its own drain gate only when
`isinstance(provider, InProcessCronScheduler)`, so a from-scratch provider would silently lose the
gateway's external-drain handling.

## Why the Ollama probe was added (2026-07-31)

Originally this gated on ComfyUI alone, because when it was written the pipe was the only thing
loading Ollama models around a render. That left the newer half of the contention unguarded: the
pipe's own chat/coder tenant is `hermes-genesis:apex-compact` at 32768 ctx, while cron jobs run
`hermes-genesis:agent` at 65536 ctx. Ollama keys runners by model+options, so those are two
DISTINCT ~17 GB runners that cannot co-reside on a 24 GB card. A tick firing mid-conversation
evicted the chat model, and the user's next turn paid a cold reload — measured at 22.7 s.

Co-residency is not reachable without raising the global context for every chat turn, so the fix
is scheduling, not co-residency.

## What the probe can and cannot see

`/api/ps` answers "is a big model RESIDENT", not "is one GENERATING". With OLLAMA_KEEP_ALIVE=60s
those differ by at most that window, which is the deliberate trade:

  * false defer  — the user's last turn was <60 s ago and they walked away. Bounded at one tick.
                   On the tightest monitor (5 min) that is <=20% jitter; hourly, ~1.7%.
  * false allow  — nothing resident, the tick fires, the user types five seconds later. No probe
                   can see this; it needs predicting a human. Structurally unfixable from the cron
                   side, and accepted.

nvidia-smi was rejected as the signal: it measures how MUCH, never WHO. A resident-idle chat
tenant and a ComfyUI weight cache look identical to it, and the correct response to each is the
opposite one.

Installed at ~/.hermes/plugins/gpuguard/ (the user-plugin dir, which survives `hermes update`).
Selected via `cron.provider: gpuguard` in config.yaml. Every probe fails OPEN — a dead ComfyUI or
a dead Ollama is not holding the GPU, and failing closed would strand every job.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

from cron.scheduler_provider import InProcessCronScheduler

logger = logging.getLogger("cron.gpuguard")

COMFY_QUEUE_URL = "http://127.0.0.1:8188/queue"
OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
PROBE_TIMEOUT_S = 3
# Cache the probe briefly so a tick over many due jobs doesn't hammer the endpoints.
PROBE_CACHE_S = 10

# Our OWN cron tag. Resident means a job ran within the keep-alive window, which is the BEST case:
# the next job reuses that warm runner with no eviction at all. Deferring on it would make cron
# defer on its own previous success, and with a 60 s tick against a 60 s keep-alive two back-to-back
# jobs would ping-pong forever.
AGENT_MODEL = "hermes-genesis:agent"

# Measured on this box 2026-07-31 via /api/ps `size_vram`:
#     hermes-genesis:apex-compact  16.70 GiB   <- the chat/coder tenant
#     gemma4:e2b                    1.81 GiB   <- largest helper
#     gemma3:1b                     0.92 GiB
#     bge-m3                        0.62 GiB
# 10 GiB sits 5.5x above the largest helper and 1.7x below the tenant.
#
# Excluding the helpers is essential, not an optimisation: OpenWebUI generates a title and tags on
# gemma4:e2b for every new conversation, embeds on bge-m3, and runs the gemma3:1b route classifier
# on every ambiguous turn. None of those preclude an 18 GB load. A naive "any model loaded => defer"
# converts ordinary UI activity into permanent cron starvation.
BIG_MODEL_BYTES = 10 * 1024**3
# Belt-and-braces beside the size rule: the size test survives a tag rename, the name test survives
# size_vram reporting something unexpected. (It already under-reports — nvidia-smi showed 19995 MiB
# resident while /api/ps claimed 16.70 GiB, because it omits the projector and the CUDA context.)
BIG_MODEL_PREFIXES = ("hermes-genesis",)

# Starvation escape. Continuous chat keeps the tenant resident via rolling keep-alives, so a
# stateless gate would defer forever, and a monitor that never fires is the whole product failing.
#
# Two tiers, deliberately NOT sharing a threshold, because the two blockers have asymmetric costs:
#   * forcing past a resident-idle Ollama tenant costs one eviction and a ~23 s reload — the user
#     sees a slow turn, and that is recoverable.
#   * forcing past a RUNNING render pushes 18 GB into a card the render is counting on, likely
#     OOMing a job that may be twenty minutes in. That is the exact failure this plugin was written
#     to prevent, so an escape that re-opens it would be a regression dressed as a fix.
#
# MAX_DEFER_S = 15 min = three cadence periods of the tightest realistic monitor (5 min), so a job
# must lose three consecutive opportunities before we take the damaging action. HARD_DEFER_S is set
# above the legitimate maximum render (6 shots x ~5 min plus QA retries), so it only ever releases a
# wedged queue, not real work. Both are env-overridable so the escape is testable in two minutes
# rather than fifteen.
MAX_DEFER_S = int(os.environ.get("HERMES_GPUGUARD_MAX_DEFER_S", "900"))
HARD_DEFER_S = int(os.environ.get("HERMES_GPUGUARD_HARD_DEFER_S", "3600"))


class GpuGuardCronScheduler(InProcessCronScheduler):
    """The built-in 60s ticker, gated on the GPU being free of other tenants."""

    def __init__(self) -> None:
        self._last_probe_at = 0.0
        self._last_probe_result = True
        self._last_ps_at = 0.0
        self._last_ps_result = True
        # The composed decision is memoised separately from the two probes — see _gpu_available.
        self._last_gate_at = 0.0
        self._last_gate_result = True
        self._deferring_since: float | None = None

    @property
    def name(self) -> str:
        return "gpuguard"

    def is_available(self) -> bool:
        return True

    def _comfy_idle(self) -> bool:
        now = time.monotonic()
        if now - self._last_probe_at < PROBE_CACHE_S:
            return self._last_probe_result
        idle = True
        try:
            with urllib.request.urlopen(COMFY_QUEUE_URL, timeout=PROBE_TIMEOUT_S) as r:
                d = json.load(r)
            running = d.get("queue_running") or []
            pending = d.get("queue_pending") or []
            idle = not running and not pending
            if not idle:
                logger.info(
                    "cron tick deferred: ComfyUI busy (%d running, %d pending)",
                    len(running), len(pending),
                )
        except Exception:
            # ComfyUI unreachable => it is not holding the GPU. Fail open.
            idle = True
        self._last_probe_at = now
        self._last_probe_result = idle
        return idle

    def _ollama_idle(self) -> bool:
        """False when a big model that is NOT ours is resident in Ollama."""
        now = time.monotonic()
        if now - self._last_ps_at < PROBE_CACHE_S:
            return self._last_ps_result
        idle = True
        try:
            with urllib.request.urlopen(OLLAMA_PS_URL, timeout=PROBE_TIMEOUT_S) as r:
                d = json.load(r)
            models = d.get("models") or []
            if not isinstance(models, list):
                models = []
            for m in models:
                if not isinstance(m, dict):
                    continue
                name = m.get("name") or ""
                if name == AGENT_MODEL:
                    continue  # our own warm runner: reuse, not contention
                size = m.get("size_vram") or 0
                if size >= BIG_MODEL_BYTES or name.startswith(BIG_MODEL_PREFIXES):
                    idle = False
                    logger.info(
                        "cron tick deferred: Ollama busy (%s, %.1f GiB resident)",
                        name, size / 1024**3,
                    )
                    break
        except Exception:
            # Ollama unreachable or malformed => nothing of ours is resident. Fail open.
            idle = True
        self._last_ps_at = now
        self._last_ps_result = idle
        return idle

    def _gpu_available(self) -> bool:
        """The composed decision, memoised, with the starvation escape.

        Memoising the COMPOSED result matters: `can_dispatch` is invoked twice per tick — once in
        the provider loop (scheduler_provider.py) and again inside tick() (scheduler.py). Without
        this, the deferral clock would advance at twice the intended rate, and a decision that
        flipped between the two calls would let the loop enter tick() only for tick() to dispatch
        nothing.
        """
        now = time.monotonic()
        if now - self._last_gate_at < PROBE_CACHE_S:
            return self._last_gate_result

        comfy_ok = self._comfy_idle()
        ollama_ok = self._ollama_idle()
        allow = comfy_ok and ollama_ok

        if allow:
            self._deferring_since = None
        else:
            if self._deferring_since is None:
                self._deferring_since = now
            waited = now - self._deferring_since
            # A wall clock, not a tick counter: wall time is invariant to the double invocation.
            if waited >= HARD_DEFER_S:
                logger.error(
                    "gpuguard hard escape: dispatching after %.0fs deferred "
                    "(comfy_idle=%s ollama_idle=%s) — treat a block this long as a wedge",
                    waited, comfy_ok, ollama_ok,
                )
                allow = True
                self._deferring_since = None
            elif waited >= MAX_DEFER_S and comfy_ok:
                # Only Ollama is blocking: forcing costs one eviction, which is recoverable.
                logger.warning(
                    "gpuguard starvation escape: dispatching after %.0fs deferred on a "
                    "resident Ollama tenant — the next chat turn will pay a reload",
                    waited,
                )
                allow = True
                self._deferring_since = None

        self._last_gate_at = now
        self._last_gate_result = allow
        return allow

    def start(self, stop_event, *, adapters=None, loop=None, interval=60,
              can_dispatch=None, profile_homes=None):
        if can_dispatch is None:
            gate = self._gpu_available
        else:
            def gate():
                return can_dispatch() and self._gpu_available()
        logger.info(
            "gpuguard cron scheduler active (probes: %s, %s; max_defer=%ds hard=%ds)",
            COMFY_QUEUE_URL, OLLAMA_PS_URL, MAX_DEFER_S, HARD_DEFER_S,
        )
        super().start(stop_event, adapters=adapters, loop=loop, interval=interval,
                      can_dispatch=gate, profile_homes=profile_homes)

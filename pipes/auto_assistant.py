"""
title: Assistant (auto)
author: local
version: 0.6.0
required_open_webui_version: 0.5.0
description: One model that decides - chats (with vision), makes a RedCraft image (with follow-up edits that stay anchored to the previous picture), or a Wan video. Background-task calls never render; QA checks edits against the original ask and the original image. Non-blocking (async). Never uses the uncensored model.
"""
import asyncio, aiohttp, requests, time, base64, hashlib, fcntl, os, random, re, json, sqlite3, sys, threading
import calendar, datetime, urllib.parse   # flight slots: month lengths, date arithmetic, deep links
from pydantic import BaseModel, Field

# Conversation continuity for the media paths (task guard, persistent last-image store,
# reference recovery, official-guidance prompt contracts). Source of truth is
# pipes/shared/media_session.py; a copy lives on the OpenWebUI data mount because OWUI
# execs each Function standalone. Degrade to the old in-memory behaviour when missing.
try:
    if "/app/backend/data" not in sys.path:
        sys.path.insert(0, "/app/backend/data")
    import media_session as ms
except Exception:  # noqa: BLE001 - the pipe must always load
    ms = None

# Serialize the VRAM-manipulating generation section so two concurrent Assistant invocations
# can't both free/reload models on the single 24 GB card and OOM each other.
def _extract_balanced(text, start_pos, opener, closer):
    """The substring from `start_pos` through its matching bracket, honouring strings/escapes."""
    depth, in_string, escape = 0, False, False
    for i, ch in enumerate(text[start_pos:], start=start_pos):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start_pos:i + 1]
    return None


def _extract_json(text):
    """JSON out of an LLM reply that may wrap it in prose or a fence. None if there is none.

    COPIED from filters/adaptive_memory.py:242-271 (JSONParser.extract_and_parse) rather than
    imported: OpenWebUI Functions deploy as a SINGLE file, so there is nothing to import from.
    Diff the two if either changes.

    Replaces `re.search(r"\\[.*\\]", out, re.S)`, which is greedy over DOTALL — a reply whose
    commentary happens to contain a later `]` swallowed everything between, and the shot planner
    then silently fell back to a single clip.
    """
    if not text:
        return None
    try:                                    # 1. the whole reply is JSON
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)   # 2. fenced
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    for opener, closer in (("[", "]"), ("{", "}")):           # 3. balanced scan through prose
        pos = 0
        while True:
            start = text.find(opener, pos)
            if start == -1:
                break
            cand = _extract_balanced(text, start, opener, closer)
            if cand is not None:
                try:
                    out = json.loads(cand)
                    if isinstance(out, (list, dict)):
                        return out
                except Exception:
                    pass
            pos = start + 1
    return None


_GEN_LOCK = threading.Lock()

# --- the GPU lock, as a file lock -----------------------------------------------------------------
# _GEN_LOCK alone was not an identity you could build on. OpenWebUI re-execs function.content on ANY
# diff, so every deploy mints a FRESH lock object: a render started under lock A and a coder turn
# taking lock B then ran concurrently, unarbitrated. Deploying a change to this very file is the
# event that triggers it.
#
# flock fixes both halves of that: the lock lives in the filesystem so its identity survives the
# re-exec, and the OS releases it if the process dies — where a leaked threading.Lock could only be
# cleared by another redeploy.
#
# Honest scope: UVICORN_WORKERS is unset and defaults to 1, so cross-PROCESS arbitration is latent
# today. It becomes real the moment workers are raised (roadmap 1.6 lists that as a pin to make).
# _GEN_LOCK is kept alongside because flock is per-fd and does NOT arbitrate threads within one
# process — both are needed, always in this order.
GPU_LOCK_PATH = os.environ.get("AA_GPU_LOCK", "/app/backend/data/.gpu.lock")
_FLOCK_FH = None


def _gpu_lock_acquire(timeout):
    """Take the in-process lock then the file lock. True if both were won within `timeout`."""
    global _FLOCK_FH
    if not _GEN_LOCK.acquire(True, timeout):
        return False
    try:
        import fcntl
        if _FLOCK_FH is None:
            _FLOCK_FH = open(GPU_LOCK_PATH, "a+")
        fcntl.flock(_FLOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Contended: another PROCESS holds it. Give the in-process lock back rather than sitting on
        # it while we wait, and let the caller poll again.
        _GEN_LOCK.release()
        return False
    except Exception:
        # Cannot use flock AT ALL — no fcntl, an unwritable or missing path (tests and any host
        # without the container's data volume), a filesystem that does not support it. Degrade to
        # the in-process lock rather than refusing to render.
        #
        # This must NOT be `except OSError`: FileNotFoundError is a subclass of it, so a missing
        # lock file took the contended branch and _gpu_lock_acquire could never succeed — every
        # locked stream then polled forever. Caught by tests/test_admission.py, which hung.
        pass
    return True


class _gpu_lock:
    """Blocking context manager over the same pair, for the synchronous media pipelines.

    They previously did `with _GEN_LOCK:` directly, which would have left media on the in-process
    lock while the coder path moved to the file lock — two mechanisms arbitrating one GPU, which is
    the same class of bug as having no lock at all.
    """
    def __enter__(self):
        while not _gpu_lock_acquire(1.0):
            pass
        return self

    def __exit__(self, *exc):
        _gpu_lock_release()
        return False


def _gpu_lock_release():
    global _FLOCK_FH
    try:
        if _FLOCK_FH is not None:
            import fcntl
            fcntl.flock(_FLOCK_FH.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _GEN_LOCK.release()
    except RuntimeError:
        pass

V_QUALITY = "best"  # "best" = Wan 2.2 A14B two-expert Lightning (default) | "fast" = TI2V 5B
V_W, V_H = 832, 480            # default 480p
V_W_HQ, V_H_HQ = 1280, 704     # "720p"/"hq" keyword → native 720p (~5 min with sage)
V_LEN_5B, V_STEPS_5B, V_FPS_5B = 49, 20, 24   # 5B fast path
V_LEN_14B, V_FPS_14B = 81, 16                  # A14B path (16 fps native, 5 s)
V_LEN_LONG = 121               # "longer" keyword → 7.5 s (requires the RifleX RoPE patch node)
V_STEPS_14B = 6                # 3 high + 3 low (up from 4 — better motion/anatomy)
V_HIGH_CFG = 3.0               # cfg>1 on the HIGH stage re-enables the negative prompt and
                               # restores motion strength/prompt adherence (Lightning motion recipe)
V_HIGH_LORA = 0.8              # Lightning LoRA strength on the high expert (motion recipe)
V_RIFE = 2                     # RIFE interpolation multiplier: 16 fps → 32 fps output
V_SAGE = True                  # SageAttention patch on the A14B experts (NEVER on the 5B — noise)
V_COMPILE = False              # torch.compile the experts: currently BROKEN with --lowvram GGUF
                               # (dynamo trips on ComfyUI's patched Conv3d: "'Conv3d' has no
                               # attribute '_v'"). Plumbing is wired — flip on when the
                               # ComfyUI/GGUF compile path stabilizes.
V_SHOT_MAX = 6                 # multi-shot: max chained 5 s shots (~30 s)
# Text-to-image parity with the standalone "Image" (Krea 2) pipe — keep these in sync with that
# pipe's LORA_FILE / LORA_STRENGTH / TRIGGER / WIDTH / HEIGHT valves so the same request produces
# the same subject in either. Empty LORA = base Krea 2 (the current default on both).
IMG_T2I_LORA = ""            # e.g. "krea2/mylora_comfyui.safetensors"
IMG_T2I_LORA_STRENGTH = 1.0
IMG_T2I_TRIGGER = ""         # prepended to the prompt when a LoRA is set
IMG_T2I_W, IMG_T2I_H = 1024, 1024
IMG_DENOISE = 0.30  # gentle Krea 2 img2img edit strength (lower = closer to the attached image)
# Instruction-edit sampler tiers. Measured 2026-08-01, fixed seed, 10 renders on one source
# (full table and the negative-prompt proof in UPGRADE_ROADMAP.md §1.3):
#
#              time    source fidelity (MAE)   grain kept   negative prompt
#   best      152 s    16.04 whole / 9.99 wood     84.5%     works
#   balanced   36 s     9.36 whole / 2.68 wood    100.8%     INERT
#   fast       20 s     9.28 whole / 3.24 wood     98.5%     INERT
#
# The Lightning LoRA is cfg-distilled, so cfg MUST stay 1.0 with it: at 2.5 the negative only
# half-bites and the render costs 68 s, and at 4.0 the LoRA plus the uncond pass OOMs the 24 GB
# card outright. That is why the tier carries its own cfg rather than letting a caller pick.
EDIT_TIERS = {
    "best":     {"lightning": False, "steps": 20, "cfg": 4.0},
    "balanced": {"lightning": True,  "steps": 8,  "cfg": 1.0},
    "fast":     {"lightning": True,  "steps": 4,  "cfg": 1.0},
}
EDIT_LORA = "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"
IMG_ENHANCE = True  # expand short prompts into richer ones via the local LLM (text-to-image only)
IMG_VERIFY = True   # vision-check the result against the request; one corrected retry on mismatch
# One JSON line per finished media job. The QA loop above can turn a 16 s render into a 3-minute one
# (measured: a plain "make a picture" ran a 154.8 s Qwen-Image-Edit correction on an image that then
# scored 4/4 on independent VQA), and nothing recorded whether it fired — so its cost could only be
# argued about. Set to "" to disable. Read it with tests/media_metrics.py. The default path is inside
# the container; MEDIA_METRICS lets the harnesses — which import this file on the host, where that
# path does not exist — contribute rows too, since the eval runs are where slow cases surface.
METRICS_PATH = os.environ.get("MEDIA_METRICS", "/app/backend/data/media_metrics.jsonl")

# Sampling for the user-facing chat stream. Until now the pipe sent no `options` at all and the
# model file carries no PARAMETER lines, so every chat, coding and vision turn inherited Ollama's
# defaults — temperature 0.8, top_p 0.9, top_k 40. That is far too hot for instruction following:
# QA_TEST_PLAN records CO01 at 4/6 with --repeat 6, "roughly one run in three the coder ignores the
# 'without slicing' constraint". That is temperature, not capability. It also put a +/-2-3 case
# noise floor on a 32-case suite, which made every other change unmeasurable.
#
# Split by route rather than by model tag: chat, code and vision all resolve to the SAME tag since
# the 2026-07-26 consolidation, so the tag cannot discriminate. The coder guard can.
# Per-request context sizing — see _fit_ctx. The floor is generous on purpose: --context-shift
# means an under-sized window truncates SILENTLY rather than erroring, so the failure mode of
# guessing low is a wrong answer, while guessing high is only wasted VRAM.
CTX_FLOOR = 16384          # never ask for less, whatever the arithmetic says
CTX_MAX = 32768            # OLLAMA_CONTEXT_LENGTH; asking beyond it buys nothing
CTX_HEADROOM = 2048        # room for the reply and the system guard

CHAT_OPTIONS = {"temperature": 0.45, "top_p": 0.9}
CODER_OPTIONS = {"temperature": 0.15, "top_p": 0.9, "top_k": 20, "repeat_penalty": 1.05}
# The harness measures behaviour, not dice. With this set, both routes go fully greedy from a fixed
# seed so a flipped case means the change flipped it. Mirrors the MEDIA_METRICS override above.
EVAL_DETERMINISTIC = bool(os.environ.get("AA_EVAL_DETERMINISTIC"))
EVAL_SEED = 20260728

# --- background tasks via hermes-agent (2026-07-29) ----------------------------------------------
# "Monitor this price for 2 weeks" is not a chat turn — it is a standing job. Those are delegated
# to a local NousResearch hermes-agent gateway (v0.19.0, pinned; ~/.hermes) whose API server is an
# agent RUNTIME on localhost: it creates/manages its own cron jobs (60 s ticker, GPU-guarded by the
# ~/.hermes/plugins/gpuguard provider so a due job never fires while ComfyUI is rendering), runs
# each job in an isolated session against hermes-genesis:agent (the same weights as the chat model,
# second tag with num_ctx 65536 — hermes hard-requires a 64 K window), and posts results to the
# OpenWebUI "background-tasks" channel via its webhook. The pipe stays the single front end: this
# is delegation over HTTP, not a second model entry in the picker.
BG_TASKS = True
HERMES_URL = "http://127.0.0.1:8642/v1"
# The API key lives in hermes's .env; a copy is staged where the container can read it so the pipe
# carries no secret. Empty/missing file => the feature reports itself unavailable rather than 401s.
# Env override for the host-side harnesses, same pattern as MEDIA_METRICS.
HERMES_KEY_FILE = os.environ.get("HERMES_KEY_FILE", "/app/backend/data/hermes_api_key")
HERMES_TIMEOUT_S = 300  # an agent turn can run several tool calls before answering

# --- deterministic job management (2026-08-02) ---------------------------------------------------
# Listing and changing jobs used to be delegated to the agent like everything else, which meant
# "list my tasks" cost a ~22.7 s chat-tenant eviction plus an agent run to answer a question the
# REST API answers in milliseconds — and phrasings the regex missed reached the CHAT model, which
# invented a task list. hermes exposes full cron CRUD, so the pipe answers these itself from
# /api/jobs and never asks a model what is scheduled.
#
# ONE switch for both halves on purpose: the broadened listing vocabulary below is only safe
# BECAUSE the deterministic path is cheap (a false positive renders a table instead of evicting the
# chat model), so turning the path off must make the vocabulary inert in the same edit.
MANAGE_DETERMINISTIC = True
# The user-facing "Task" control (filters/task_mode.py). When it is on for a turn, that turn goes
# to the background-task agent — no routing predicate gets a vote.
#
# This exists because detection-by-wording kept failing in both directions. "track the item <url>
# when the price is under 10" was answered with a scraping script because the recurrence pattern
# had no arm for "is under"; widening the pattern to catch it then made ordinary sentences look
# like monitors. A control the user turns on has neither failure mode: it is right by construction
# on the turns it is on, and absent on the turns it is off.
#
# The id must match filters/task_mode.py's TASK_MODE_ID. tests/test_task_mode.py pins that, because
# a rename on one side alone leaves a control that does nothing and reports nothing.
TASK_MODE_ID = "task_mode"
# Job management is ADMIN-ONLY. hermes has no per-job owner — one shared API key sees and can
# delete every job on the host — so one user's list is every user's list. Primary signal is the
# OpenWebUI role; this allow-list is supplementary, for handles that should qualify anyway.
# NB the handle comes from _alert_username, i.e. the EMAIL LOCAL PART (omariqbal97@… -> omariqbal97),
# not the display name. contacts.json already carries both spellings from an earlier drift.
TASK_ADMINS = {h for h in os.environ.get("TASK_ADMINS", "ohmz omariqbal97").lower()
               .replace(",", " ").split() if h}
JOBS_MAX = 25           # rows rendered before "…and N more"; keeps a runaway list readable
CONFIRM_TTL_S = 600     # an armed delete older than this is refused, not silently ignored
PARK_TTL_S = 86400      # a rendered list older than this stops backing ordinals

# ---------- flight fare requests -----------------------------------------------------------------
# A flight ask is not a price ask, and until now it was not routed like one either — it was not
# routed at all. Measured against this file on 2026-08-07, seven of eight realistic phrasings
# ("find me a cheap flight to Tokyo in March", "watch flights YYZ to YVR in September") reach NO
# background task: _is_bg_task_request needs an imperative verb AND independent evidence of
# recurrence, and "in September" is not recurrence. They fall through to the chat model, which has
# no fare data and answers with a number it invented. _guess_kind already returns "fare" for all
# eight and nothing consumed it, because the branch that would was never reached.
#
# So this path exists to stop a fabricated fare, which it can do WITHOUT being able to read a real
# one. Measured across all 19 sites the user named (docs/FLIGHT_RECON.md): 16 block automated
# clients outright, 1 has no fetchable URL, 2 are deal feeds. Zero are readable. So the terminal
# action here is deliberately NOT "create a watch" — creating one would leave a monitor that fires,
# reports fare_unsupported and texts nobody, which is the exact failure the phone gate at :5147
# exists to prevent. The terminal action is an honest answer plus a Google Flights link built from
# the user's own slots, which is a thing they can actually click.
#
# When a readable source appears — Chrome recon on the six unmeasured sites, or a keyed fare API —
# the slot collection below is unchanged and only that final step moves.
FLIGHT_ROUTE = True
# FlightClaw — the fare engine (docs/FLIGHTCLAW.md). An always-on MCP server over Google Flights'
# protobuf API; measured live 2026-08-09 returning real CAD fares with booking links, no key.
# The OWUI container runs network_mode: host, so 127.0.0.1 is the host's loopback.
FLIGHTCLAW_URL = os.environ.get("FLIGHTCLAW_MCP", "http://127.0.0.1:8765/mcp")
FLIGHTCLAW_SEARCH_TIMEOUT_S = 75   # one protobuf query; date-grid searches are the slow end
FLIGHTCLAW_WATCH_MAX_RUNS = 360    # cap for an unbounded cadence scaled to a far-out departure
FLIGHT_CLASSIFIER = True   # the gemma3:1b tier for the ambiguous band; independent of the coder's
FLIGHT_DRAFT_TTL_S = 3600  # an abandoned itinerary stops owning the conversation after an hour
FLIGHT_MAX_TURNS = 6       # asks before the flow gives up and says so rather than looping
# Alert wiring the pipe can see from inside the container. Both live in the OpenWebUI config
# directory because that is the only path shared with the host, where the transports run:
#   contacts — read AND written here, so a phone number the user types in chat is usable at once
#   profile  — non-secret display facts (sender address, channels), refreshed by the delivery
#              watcher every minute so this can never describe a setup that is no longer true
ALERT_CONTACTS_FILE = os.environ.get("ALERT_CONTACTS",
                                     "/app/backend/data/alerts/contacts.json")
ALERT_PROFILE_FILE = os.environ.get("ALERT_PROFILE", "/app/backend/data/alerts/profile.json")
# Who owns which background job. hermes has no per-job owner field and cannot be given one that
# survives an upgrade (its REST PATCH whitelist rejects unknown keys, the agent's cronjob tool
# cannot set them, and the pipe cannot import hermes across the container boundary), so ownership
# is recorded HERE, keyed by the job id the scheduler hands back at creation time.
#
# Same directory as the alert files, and for the same reason: it is the one path shared with the
# host, where the delivery watcher runs and needs to route each job's results to its owner.
#   {job_id: {"h": handle, "t": epoch, "src": "cited"|"diff"|"seed"}}
# `src` records HOW the job was attributed, so a stamp made on a weak signal is auditable and
# repairable by hand rather than being indistinguishable from a certain one.
TASK_OWNERS_FILE = os.environ.get("TASK_OWNERS", "/app/backend/data/alerts/job_owners.json")
# A one-shot "send this confirmation" inbox, same directory and same reason as TASK_OWNERS_FILE:
# the container holds no SMTP/Twilio credentials (deliberately — see alert_transports.py's
# publish_profile), so the pipe cannot email or text anyone itself. It can only leave a note where
# the host-side delivery watcher (scripts/hermes_delivery.py, every 60s) will find it. A LIST, not
# a dict keyed by job id: two jobs can share an id only across restarts, never in the same tick,
# but the failure mode of a dict — a second enqueue silently overwriting the first's fields before
# the watcher ever reads either — costs nothing to avoid by just appending.
SUBSCRIBE_INBOX_FILE = os.environ.get("SUBSCRIBE_INBOX",
                                      "/app/backend/data/alerts/pending_subscriptions.json")
# A finite job deletes itself from the scheduler on its last run, but the delivery watcher reads
# that run's output up to a minute later and the results still have to reach the right person.
# Keep the ownership record well past the disappearance rather than pruning on sight.
OWNER_PRUNE_S = 72 * 3600
# Read-only, for resolving a handle to its account email exactly as the delivery side does.
OWUI_DB = os.environ.get("OWUI_DB", "/app/backend/data/webui.db")
VID_ENHANCE = True  # expand terse video ideas ("guy shooting hoops") into detailed prompts — the
                    # single biggest quality lever for Wan; terse prompts produce broken scenes
VID_VERIFY = True   # vision-check a mid frame of the clip against the request; one corrected retry
VID_VERIFY_MODE = "anchors"  # multi-shot QA scope: "anchors" = shot 1 + last shot only | "all" = every shot

# --- automatic coder routing on the 'auto' entry -------------------------------------------------
# Lets Assistant hand a coding question to the big coder tenant without the user switching entries.
# Media routing always wins first; this only ever affects turns that were already going to be chat.
AUTO_ROUTE_CODER = True
# Classifier for prompts the regexes can't call confidently. gemma3:1b is 0.99 GB and is PROVEN to
# stay co-resident with the 18.37 GB coder (measured 20561/24576 MiB), so a classify->answer turn
# costs ONE model load. Do NOT use gemma4:e2b here: at ~3.3 GB it evicts the coder, so every routed
# turn would pay two loads instead of none.
ROUTE_CLASSIFIER_MODEL = "gemma3:1b"
ROUTE_CLASSIFIER_TIMEOUT = 12       # seconds; on any failure we fall back to normal chat

# Let OpenWebUI's own system messages through on the 'auto' entry — native memory, folder system
# prompts, and anything else injected as role=system. Historically 'auto' replaced every system
# message with the anti-dalle guard below, which silently discarded all of it.
#
# Safe with respect to routing: system messages are NEVER part of the routing text. Routing reads
# metadata['user_prompt'] (the user's verbatim words) and strips known filter blocks on top; see
# _strip_injected_context. Web-search and file context are likewise safe — chat_web_search_handler
# attaches to form_data['files'], and middleware only merges that into the messages at :2808, AFTER
# user_prompt is captured at :2803.
AUTO_KEEP_SYSTEM = True

# --- confirmation before a render ----------------------------------------------------------------
# Media routing is default-deny and heavily measured, but the residual false-positive rate is ~35%
# (UPGRADE_ROADMAP.md:864) and no regex will drive that to zero — the remaining cases are genuinely
# ambiguous English. A wrong render is not a wrong answer: it evicts the 18 GB chat tenant, holds
# the card for minutes, and the user waits for something they never asked for.
#
#   "never"  no gate
#   "video"  ask before video / animate / multi-shot only          (default)
#   "all"    ask before images too
#
# Video is the default line because that is where the asymmetry lives: a Wan render is 3-6 minutes
# (V04 measured 386 s) against ~16-48 s for a Krea image, so a false positive on video costs an
# order of magnitude more than the interruption of asking. Gating images too would nag on the 65%
# of media turns that were correct all along, to save 30 seconds.
#
# ALWAYS fails open. The gate is a courtesy to avoid wasted GPU, not a safety control: when there is
# no client to ask — the eval harness drives pipe() directly, and so does any API caller — the
# render proceeds. Failing closed would hang tests/eval/run_eval.py on every media case.
CONFIRM_RENDERS = "video"

# --- structured outputs on helper calls ----------------------------------------------------------
# Ollama 0.32.1 accepts `format` as a JSON Schema and constrains decoding to it. Verified on this
# box, including the part that mattered: with two solid-colour images and an enum schema, the
# answers TRACKED the image (red -> "red", blue -> "blue"), so the grammar composes with the
# multimodal path rather than bypassing the projector.
#
# This is NOT the roadmap's latency argument, which is dead: every helper sets keep_alive:0 and so
# pays a ~23 s cold load, against which a ~2 s decode saving is noise. It is a CORRECTNESS argument.
# The same probe with format OFF returned 'The user wants me to identify the dominant color of the
# provided image.' and nothing else — the verdict never appeared inside the token budget, and the
# old verifier scored exactly that as a pass.
#
# Set STRUCTURED_OUTPUT = False to disable all of it, or any single _*_FORMAT to None to disable
# that site alone. Every site parses JSON first and falls back to the regex path on the same raw
# text, so a model that ignores the grammar lands on the old behaviour rather than failing.
STRUCTURED_OUTPUT = True

# Property ORDER is load-bearing: llama.cpp emits required properties in schema order, so `ok`
# comes before `fix` and the verdict is committed before any justification is written. No
# minLength on `fix` — a grammar cannot early-close a structure, so hitting num_predict yields
# INVALID json, where the regex path could still use a truncated FIX line.
_VERIFY_FORMAT = {"type": "object",
                  "properties": {"ok": {"type": "boolean"}, "fix": {"type": "string"}},
                  "required": ["ok", "fix"]}
# Envelope only — the prose inside each shot is unconstrained. Deliberately no minItems: the count
# is enforced in Python, where a short plan can be reported instead of silently truncated.
_SHOTS_FORMAT = {"type": "array", "items": {"type": "string"}}
_EDIT_FORMAT = {"type": "object",
                "properties": {"edit": {"type": "string"},
                               "avoid": {"type": "array", "items": {"type": "string"}}},
                "required": ["edit", "avoid"]}


class Pipe:
    class Valves(BaseModel):
        # This pipe carried none of these as valves for its whole life — every knob above is a
        # module constant needing a redeploy to change. Only the one measured tier knob is exposed
        # here; the rest stay constants deliberately rather than becoming a wall of settings.
        EDIT_QUALITY: str = Field(
            default="balanced",
            description="Instruction-edit tier. 'balanced' (~36 s, 8-step speed LoRA) is the "
                        "default: measured, it tracks the source photo CLOSER than 'best' and "
                        "keeps its film grain, at a quarter of the time. 'best' (~152 s) renders "
                        "newly-added objects with finer micro-texture and is the only tier where "
                        "the negative prompt does anything — style conversions, 'you barely "
                        "changed it' retries and QA corrections always use it whatever this says. "
                        "'fast' (~20 s, 4-step) trades a little more object detail again.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        # ONE MODEL FOR EVERYTHING. Chat, code, vision and the uncensored prompt helpers all run on
        # this single tenant — across every pipe on the box, not just this one.
        #
        # Why this tag rather than the stock Qwen3.6 it replaces: measured head-to-head
        # (tests/bench_models.py, see UPGRADE_ROADMAP.md §0), it matched stock Qwen on 27/27 executed
        # coding tasks and on critical thinking, ran marginally faster (135.3 vs 130.8 tok/s) in
        # slightly less VRAM (18285 vs 18369 MiB) — and complied with 5/5 prompt-enhancer requests
        # where stock Qwen REFUSED 2 of 5. That refusal gap was the only reason dolphin-venice:24b
        # still existed, so closing it is what allows the box to drop to one large tenant.
        #
        # ⚠️ Known caveat, recorded rather than hidden: the coding benchmark does not discriminate —
        # all candidates scored 27/27, so it shows no DETECTABLE regression, not equal quality. The
        # upstream author states this build is tuned for uncensored roleplay and that a different
        # model is better for coding. Adopted on the user's explicit decision. If code quality feels
        # worse, the rollback is in UPGRADE_ROADMAP.md §0.6.
        self.chat_model = "hermes-genesis:apex-compact"
        # Same tenant again — it ships its own F16 projector and passed the vision check in the
        # head-to-head. gemma4:31b, the old vision model, is gone: Ollama predicted 25.5 GiB for it
        # at 32k context, more than the card, so it evicted everything unconditionally on every
        # image turn.
        self.vision_model = "hermes-genesis:apex-compact"
        # Same tenant a third time. Kept as a separate attribute rather than collapsed into one
        # field because the coder ROUTE still differs — it gets its own guard prompt and holds
        # _GEN_LOCK for the stream — and because splitting them again later should be a one-line
        # change, not a refactor.
        self.coder_model = "hermes-genesis:apex-compact"
        # OpenWebUI's title/tag/query tasks are CONFIGURED to run on a small model, but the
        # server silently falls back to the chat's model (this pipe) whenever that id is
        # missing from the visible model registry — observed in media_metrics.jsonl as real
        # GPU renders of '### Task:' boilerplate. pipe() now guards on __task__ / the text
        # prefix and answers them as plain text; see media_session.is_task_request.
        self._recent = {}        # chat_id -> last produced image b64 (for follow-up edits without re-upload)
        self._recent_video = {}  # chat_id -> (prompt, seed) of the last produced video (for follow-up changes)
        # Cross-turn state for background tasks, keyed by chat and held HERE rather than hidden in
        # the message text. It used to ride in HTML comments (<!--bg-jobs:…-->); OpenWebUI escapes
        # those and printed a wall of base64 under every answer, wherever in the message they sat —
        # inline or as their own block. Anything embedded in the reply is potentially visible, so
        # the reply now carries nothing at all. Same lifetime and eviction policy as _recent above:
        # lost on a pipe reload, which costs one turn of continuity and never correctness, because
        # every reader re-fetches /api/jobs and confirms against a live record before acting.
        self._parked = {}        # chat_id -> {"t", "ids", "ns"} — the job list last rendered
        self._armed = {}         # chat_id -> the destructive op awaiting a yes
        self._bg_turn = {}       # chat_id -> ts of the last background-task reply
        self._phone_ask = {}     # chat_id -> the request parked behind "what number should I text?"
        # The itinerary being assembled in this chat. Same lifetime and eviction policy as the
        # stores above: lost on a pipe reload, which costs continuity and never correctness, because
        # _flight_draft_from_reply re-reads the slot table out of the last assistant turn. The state
        # the user can SEE is the state of record — which is also why it is a visible table and not
        # an HTML comment (see _marks: OpenWebUI escapes those wherever they appear).
        self._flight_draft = {}  # chat_id -> {"t", "turns", "slots"}

    # ONE entry. It chats, sees images, writes code on the big coder tenant, renders images and
    # video, searches the web, reads your documents and remembers things — choosing the model per
    # turn rather than making you choose an entry.
    #
    # There used to be three (auto / knowledge / coder). Once `auto` gained legacy function calling,
    # the memory filter and keep_system, `knowledge` became a STRICT SUBSET of it — same tools, same
    # model, but unable to render or reach the coder. It only subtracted. `coder` only forced a model
    # that _is_code_request now selects automatically. Both were removed rather than left as
    # near-duplicates the user has to choose between.
    #
    # The single entry must still live in this one Function file: each OpenWebUI Function loads into
    # its own module namespace, so a second file would get its own _GEN_LOCK and nothing would
    # serialize GPU work between them (see CAPABILITY_UPGRADE_PLAN.md Phase 2, shape C).
    def pipes(self):
        return [{"id": "auto", "name": "Assistant"}]

    def _entry(self, body):
        """Which manifold entry was selected. OpenWebUI sends '<function_id>.<pipe_id>'; anything
        unrecognised (or a direct API call with a bare model name) falls back to the historical
        'auto' behaviour so nothing regresses."""
        mid = (body or {}).get("model") or ""
        if not isinstance(mid, str):
            return "auto"
        leaf = mid.rsplit(".", 1)[-1].strip().lower()
        return leaf if leaf in ("auto", "knowledge", "coder") else "auto"

    # Text that OpenWebUI or its filters splice into the last user message BEFORE the routing prompt
    # is captured. Each entry is (marker, position):
    #   "prefix" — block comes first, the user's real words follow after a blank line
    #   "suffix" — the user's words come first and the block is appended to the end
    _INJECTED_MARKERS = (
        # Adaptive Memory (and any v4+ memory filter). Inlet filters run at middleware.py:2428 and
        # PREPEND to the last user message.
        ("User Memories (", "prefix"),
        # OpenWebUI's own code-interpreter prompt. In LEGACY function-calling mode middleware.py:2482
        # calls add_or_update_user_message(prompt, ...), which defaults to append=True (misc.py:527),
        # tacking ~2.2 kB onto the END of the user's message.
        #
        # Measured: that block on its own matches _CODE_STRONG. So with the code-interpreter toggle
        # on, EVERY message — "what is 2+2?" included — would route to the 18 GB coder. Stripping it
        # is what stops a UI toggle silently hijacking model selection.
        ("#### Code Interpreter", "suffix"),
    )

    # OpenWebUI's RAG / web-search envelope, PREPENDED to the user's own message whenever sources
    # are attached (middleware.apply_source_context_to_messages -> add_or_update_user_message with
    # append=False). It opens "### Task:" and ends with the retrieved sources in a <context> block;
    # the user's actual words follow. Cutting through the closing tag leaves the question they
    # typed, which is the only thing routing should ever see.
    _RAG_ENVELOPE = re.compile(r"^\s*#{2,4}\s*Task\s*:[\s\S]*?</context>\s*", re.I)
    # Same shape media_session.is_task_request uses. Duplicated here on purpose: that module is a
    # sidecar copied into OpenWebUI's data volume, so it is absent from any host-side harness and
    # from a container where the copy failed — and when it is absent the whole text-prefix half of
    # the task guard silently disappears. That is survivable when the fallback is chat; it is not
    # survivable under the Task control, where it would hand OpenWebUI's own title and tag prompts
    # to the background-task agent.
    _OWUI_TASK_PREFIX = re.compile(r"^\s*#{2,4}\s*Task\s*:", re.I)
    _OWUI_RAG_MARKS = ("</context>", "<source",
                       "Respond to the user query using the provided context")

    @classmethod
    def _is_owui_task(cls, text):
        """True when this text is OpenWebUI's own machinery talking, not the user.

        The RAG exclusion matters as much as the match: OpenWebUI's retrieval envelope opens with
        the same '### Task:' heading and is wrapped around a real user turn.
        """
        t = text or ""
        if not cls._OWUI_TASK_PREFIX.match(t):
            return False
        return not any(m in t for m in cls._OWUI_RAG_MARKS)

    def _strip_injected_context(self, text):
        """Remove app- and filter-injected blocks from the text used for ROUTING.

        `metadata['user_prompt']` is captured at middleware.py:2803, but several things mutate the
        last user message before that: inlet filters at :2428, and the code-interpreter prompt at
        :2482. Whatever they add lands in the router's input — the Phase 1 failure mode through a
        different door.

        Note what is deliberately NOT listed here, because it does not need to be: retrieved file
        context, web-search results and folder files all arrive via form_data['files'] and are merged
        into the messages only at :2808, i.e. AFTER user_prompt is captured. Those are already safe.
        """
        if not isinstance(text, str):
            return text
        m = self._RAG_ENVELOPE.search(text)
        if m:
            text = text[m.end():]
        for marker, pos in self._INJECTED_MARKERS:
            idx = text.find(marker)
            if idx == -1:
                continue
            if pos == "suffix":
                text = text[:idx]
                continue
            sep = text.find("\n\n", idx + len(marker))
            # Prefix block is followed by a blank line then the real message; with no separator the
            # whole string was injected context, leaving whatever preceded it (normally "").
            text = text[sep + 2:] if sep != -1 else text[:idx]
        return text

    # ---------- coder intent (auto entry) ----------
    # Three tiers so the common cases cost nothing:
    #   STRONG  -> certainly code, route immediately, no LLM call
    #   HINT    -> could go either way ("what does this python error mean" vs "my python died")
    #              -> ask the 1 B classifier
    #   neither -> normal chat, no LLM call
    # Everything here runs on the CLEAN prompt (post user_prompt + _strip_injected_context), never on
    # RAG or filter-injected text — that distinction is what Phase 1 was about.

    # Literal code on screen, or an explicit build/fix instruction aimed at a code noun.
    _CODE_STRONG = re.compile(
        r"```|~~~"                                                    # fenced block
        r"|^\s*(?:def|class|import|from|package|func|fn|const|let|var|public|private)\s+\w"
        r"|#include\s*<|</?[a-z]+>|\bSELECT\b[\s\S]{0,80}\bFROM\b"
        r"|\b(?:traceback|stack\s?trace|segmentation fault|core dumped)\b"
        r"|\b(?:[A-Za-z]*(?:Error|Exception))\b\s*:"                  # TypeError:, NullPointerException:
        r"|\b(?:write|create|generate|implement|refactor|rewrite|debug|fix|optimi[sz]e|profile|"
        r"unit[\s-]?test|benchmark)\b[^.?!]{0,60}\b(?:function|method|class|script|program|query|"
        r"regex|regexp|api|endpoint|component|module|algorithm|snippet|code|test|parser|decorator|"
        r"middleware|migration|schema|dockerfile|makefile|cli)\b",
        re.I | re.M)

    # Vocabulary that *suggests* programming but is regularly used about non-code things.
    _CODE_HINT = re.compile(
        r"\b(python|javascript|typescript|node|react|vue|svelte|rust|golang|\bgo\b|java|kotlin|swift|"
        r"c\+\+|c#|php|ruby|perl|scala|haskell|elixir|bash|zsh|shell|powershell|sql|postgres|mysql|"
        r"sqlite|mongo|redis|regex|docker|kubernetes|k8s|terraform|ansible|nginx|git|github|gitlab|"
        r"json|yaml|xml|csv|api|rest|graphql|npm|yarn|pnpm|pip|poetry|cargo|gradle|maven|webpack|"
        r"vite|compile|compiler|runtime|stacktrace|async|await|thread|mutex|pointer|dataframe|numpy|"
        r"pandas|pytorch|tensorflow|linter|lint|refactor|codebase|repository|repo|commit|merge|"
        r"pull request|branch|deploy|ci/cd|pipeline|latency|throughput|algorithm|recursion|"
        r"big[- ]o|complexity|syntax|variable|array|dictionary|hashmap|struct|interface|inheritance)\b",
        re.I)

    _CLASSIFY_PROMPT = (
        "Classify the user's message. Answer with ONE word, nothing else.\n"
        "Answer CODE if they want software written, explained, debugged, reviewed or optimised, "
        "or are asking about programming, databases, shells, APIs or developer tooling.\n"
        "Answer CHAT for anything else, including casual talk that merely mentions technology.\n\n"
        "Examples:\n"
        "fix this null pointer in my java service -> CODE\n"
        "my python died last week, poor snake -> CHAT\n"
        "how do I make a rest api paginate -> CODE\n"
        "is javascript a good career in 2026 -> CHAT\n"
        "explain big-o for quicksort -> CODE\n"
        "what should I cook tonight -> CHAT\n\n"
        "Message: {msg}\nAnswer:")

    def _classify_code(self, text):
        """Ask the small classifier whether an ambiguous prompt is a coding request.

        Any failure (model absent, timeout, junk output) returns False so the turn falls back to
        ordinary chat — a routing helper must never be able to break the chat path.

        Every call writes a job:'classifier' metrics row (verdict, latency, error class). A
        timed-out classifier degrades to chat SILENTLY by contract, which means detection quality
        quietly varies with GPU load — the row is the only place that shows it happening.
        """
        t0 = time.monotonic()
        try:
            r = requests.post(
                f"{self.ollama}/api/chat",
                json={"model": ROUTE_CLASSIFIER_MODEL, "stream": False, "think": False,
                      "options": {"temperature": 0, "num_predict": 4},
                      "messages": [{"role": "user",
                                    "content": self._CLASSIFY_PROMPT.format(msg=text[:600])}]},
                timeout=ROUTE_CLASSIFIER_TIMEOUT)
            latency = round((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                self._metric(job="classifier", ok=False, error=f"http_{r.status_code}",
                             latency_ms=latency)
                return False
            verdict = ((r.json().get("message") or {}).get("content") or "").strip().upper()
            self._metric(job="classifier", ok=True, verdict=verdict[:12], latency_ms=latency)
            return verdict.startswith("CODE")
        except Exception as e:
            self._metric(job="classifier", ok=False, error=type(e).__name__,
                         latency_ms=round((time.monotonic() - t0) * 1000))
            return False

    def _is_code_request(self, text):
        """True when the 'auto' entry should answer with the coder tenant instead of the chat model."""
        if not AUTO_ROUTE_CODER or not text:
            return False
        if self._CODE_STRONG.search(text):
            return True
        if not self._CODE_HINT.search(text):
            return False
        return self._classify_code(text)

    async def _locked_stream(self, inner, emitter=None):
        """Hold the GPU lock for a whole streamed reply, so loading a large tenant can't land in
        the middle of a ComfyUI render.

        Acquired by POLLING on a worker thread, which is the whole point. It used to be a single
        blocking `to_thread(_GEN_LOCK.acquire)` placed OUTSIDE the try — and asyncio.to_thread
        cannot interrupt a thread already blocked in acquire(). A client disconnecting while that
        thread waited cancelled the coroutine before the `try` was entered, so the `finally` never
        ran; the worker then won the lock with nobody left to release it and every later render and
        coder turn blocked forever, recoverable only by a redeploy. The old docstring claimed the
        `finally` prevented exactly that. Writing the regression test for this wedged the test
        process, which is as good a demonstration as it gets.

        Polling with a timeout makes cancellation observable, and `held` means the release in
        `finally` can never fire for a lock we do not own.
        """
        held = False
        acq = None          # the in-flight acquire, so `finally` can settle it
        waited = 0.0
        try:
            while not held:
                # shield: cancelling US must not cancel the worker mid-acquire, or we could never
                # find out whether it won the lock. Polling alone is NOT enough — the thread can
                # win it in the instant after we are cancelled, and then `held` is never assigned
                # and nothing releases. That is the leak, one second narrower.
                acq = asyncio.ensure_future(asyncio.to_thread(_gpu_lock_acquire, 1.0))
                held = await asyncio.shield(acq)
                acq = None
                if not held:
                    waited += 1.0
                    # The coder path is the real "appears hung": it blocks with no output at all,
                    # for as long as a render takes — up to ~20 minutes for six shots.
                    await self._status(emitter, f"Waiting for the GPU… {self._fmt_dur(waited)}")
            if waited:
                await self._status(emitter, "", done=True)
            async for tok in inner:
                yield tok
        finally:
            # Release BEFORE closing `inner`: the lock is the contended resource and must come back
            # even if the underlying HTTP stream misbehaves on teardown.
            if not held and acq is not None:
                # Cancelled while an acquire was in flight. Wait for it — bounded by the 1 s poll —
                # so a lock the worker won on the way out is given back rather than stranded.
                try:
                    held = await asyncio.shield(acq)
                except Exception:
                    held = bool(acq.done() and not acq.cancelled() and acq.exception() is None
                                and acq.result())
            if held:
                _gpu_lock_release()
            try:
                await inner.aclose()
            except Exception:
                pass

    # ---------- content parsing ----------
    def _last_user(self, messages):
        """(text, reference_image_b64_or_None) from the last user message."""
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                return c.strip(), None
            if isinstance(c, list):
                text, img = "", None
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text = part.get("text", "")
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            img = url.split(",", 1)[1]
                return (text or "").strip(), img
            return "", None
        return "", None

    def _ollama_messages(self, messages):
        """Convert OpenAI-format (incl. multimodal) messages to Ollama format (content str + images[])."""
        out = []
        for m in messages:
            role = m.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                out.append({"role": role, "content": self._scrub(c)})
            elif isinstance(c, list):
                text, imgs = "", []
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text += part.get("text", "")
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            imgs.append(url.split(",", 1)[1])
                msg = {"role": role, "content": text}
                if imgs:
                    msg["images"] = imgs
                out.append(msg)
        return out

    # ---------- routing ----------
    _STILL_NOUNS = (r"(?:picture|pictures|image|images|photo|photos|pic|pics|drawing|painting|"
                    r"illustration|portrait|poster|art|artwork|wallpaper|logo)")

    # Shared intent guards (used by the image/video/edit routers) so a question or an
    # acknowledgement about media never gets hijacked into a ~2-minute generation job.
    _SMALLTALK = re.compile(
        r"^(nice|cool|thanks|thank|great|awesome|perfect|love|lovely|beautiful|amazing|good|"
        r"ok|okay|k|lol+|haha+|wow|hmm+|nvm|never\s?mind|yes|yeah|yep|yup|no|nope|nah|sure|"
        r"cheers|wonderful|gorgeous|stunning|fantastic|excellent|brilliant)\b", re.I)
    _QUESTION = re.compile(
        r"^(what'?s?|why|how|who|whom|whose|where|when|which|is|are|am|was|were|do|does|did|"
        r"can|could|would|should|will|have|has|had|may|might|tell\s+me|explain|describe|list|"
        r"suggest|recommend|caption|analy[sz]e|identify|read|translate|compare|define|"
        r"summari[sz]e|write|compose|draft|i\s+(think|feel|wonder|need\s+to\s+know))\b", re.I)
    # 'draw a conclusion', 'paint a picture of how it works' … figurative, not image generation.
    _FIGURATIVE = re.compile(
        r"\b(draw|paint|sketch)\s+(?:\w+\s+){0,3}"
        r"(parallel|conclusion|comparison|distinction|attention|inspiration|line)", re.I)

    def _is_smalltalk(self, t):
        return bool(self._SMALLTALK.match((t or "").strip()))

    def _is_question(self, t):
        t = (t or "").strip()
        return t.endswith("?") or bool(self._QUESTION.match(t))

    def _strip_still_style(self, t):
        """'animated picture', 'cartoon image', 'animated movie poster' … name a STYLE of still
        image (cartoon look), not motion — blank the style word so the video detectors don't
        fire on it. Allows up to ~2 words between the style word and the still-noun. 'animated
        gif/clip/video', 'an animation of X' and 'animate this' still route to video."""
        return re.sub(
            rf"\b(?:animated|animation[\s-]style|cartoon(?:[\s-]style)?|anime[\s-]style)\s+"
            rf"(?:\w+\s+){{0,2}}({self._STILL_NOUNS})\b", r"\1", t)

    # Explicit render prefixes. These exist so the default-deny predicates below can be strict:
    # anything they reject can still be forced with two keystrokes.
    _MEDIA_SLASH_IMG = re.compile(r"^\s*/(?:img|image|draw)\b", re.I)
    _MEDIA_SLASH_VID = re.compile(r"^\s*/(?:vid|video|animate)\b", re.I)
    # A drawing verb in IMPERATIVE position — optionally behind a politeness or request preamble.
    _IMPERATIVE_DRAW = re.compile(
        r"^\s*(?:(?:please|pls|hey|ok|okay|now)[,\s]+)*"
        r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
        r"(?:go\s+ahead\s+and\s+)?"
        r"(draw|sketch|paint|illustrate|render)\b", re.I)

    def _is_video_request(self, t):
        raw = t.lower()
        t = self._strip_still_style(raw)
        # collocations that carry a video-noun but are never motion requests
        t = re.sub(r"\b(video\s?games?|clip\s?art|music\s+videos?)\b", " ", t)
        # A STRONG generation verb + a video-noun object → a request even if phrased as a question.
        if re.search(r"\b(make|create|generate|render|produce|animate)\b"
                     r".{0,25}\b(clip|video|animation|gif|footage|moving image)\b", t):
            return True
        # "show me" / "give me" are DEICTIC, not generative — they only mean "render one" when the
        # object is indefinite. "show me a clip of a dog" asks for a render; "show me the footage
        # from the meeting notes" points at something that already exists. The determiner is the
        # whole difference, so the definite case must not reach the renderer.
        if re.search(r"\b(show|give|send)\s+me\b(?:(?!\bthe\b).){0,25}?"
                     r"\b(clip|video|animation|gif|footage|moving image)\b", t):
            return True
        if re.search(r"\banimate\s+(this|it|that|the|my|him|her|them)\b", t):
            return True
        if re.search(r"\bbring\b[\w\s]{0,20}?\bto\s+life\b", t):
            return True
        # Explicit escape hatch for terse phrasing the default-deny rule below would reject.
        if self._MEDIA_SLASH_VID.match(raw):
            return True
        # An imperative motion instruction ("make it move") is a request; a bare motion NOUN is not.
        if re.search(r"^\s*(?:please\s+|pls\s+)?make\s+(?:it|this|that)\s+move\b", t):
            return True
        # DEFAULT DENY. There used to be a fallback here that returned True on a bare
        # \b(video|animation|footage|moving image)\b anywhere in the message, guarded only against
        # questions and small-talk. Declarative sentences sailed straight through it: measured, 8 of
        # 10 ordinary sentences started a render — "I watched a great video about sourdough
        # yesterday" queued a Wan job, and so did "the video card in this machine is a 3090". Each
        # false render costs 1-5 GPU-minutes holding _GEN_LOCK. Mentioning a video is not asking for
        # one; ask with a verb, or use /vid.
        return False

    def _wants_new_video(self, t):
        """Explicit FRESH video request ('create/make a video of …') — always a new clip,
        even if the chat already has one."""
        return bool(re.search(
            r"\b(create|make|generate|render|produce|give me|show me|another|new)\b.{0,25}\b(video|clip|animation|gif|footage)\b",
            self._strip_still_style(t.lower())))

    def _merge_video_prompt(self, prev, change):
        """Fold a requested change into the previous video prompt using the big chat model.
        (gemma3:1b just ECHOED the original verbatim → same prompt + same seed → ComfyUI's graph
        cache returned the identical cached video, so 'change the background' visibly did nothing.)
        Guarded: if the model returns the original unchanged (or junk), fall back to appending the
        change so the conditioning ALWAYS differs from the previous clip."""
        try:
            r = requests.post(f"{self.ollama}/api/generate", json={
                "model": self.chat_model,
                "system": ("You revise prompts for a text-to-video model. Rewrite the ORIGINAL prompt "
                           "so the REQUESTED CHANGE is fully applied: replace every detail the change "
                           "supersedes (e.g. 'change the background to a city' replaces the beach, "
                           "sand, waves and horizon with streets and buildings everywhere they are "
                           "mentioned), and keep everything the change does not affect — subject, "
                           "action, camera, lighting style. Output ONLY the revised prompt text — "
                           "no preamble, no quotes."),
                "prompt": f"ORIGINAL: {prev}\nREQUESTED CHANGE: {change}\nREVISED PROMPT:",
                "stream": False, "think": False, "keep_alive": 0,
                "options": {"temperature": 0.3, "num_predict": 250}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"').strip()
            if out and len(out) < 900 and out.strip().lower() != (prev or "").strip().lower():
                return out
        except Exception:
            pass
        return f"{prev}. {change}."

    # Background-task intent. DEFAULT-DENY like the media predicates: mentioning a monitor is not
    # asking for one ("what's a good price tracker?" is a question, "I've been watching the price"
    # is conversation). A standing job needs either the explicit /task prefix, a management verb
    # aimed at existing jobs, or an imperative monitoring verb PLUS evidence of recurrence — a
    # schedule word, a duration, or an alert condition. One regex alone does not commit.
    _BG_SLASH = re.compile(r"^\s*/tasks?\b", re.I)  # '/tasks' too — the plural fell through to chat
    # One-shot research/agent prefix, word-bounded. startswith("/agent") also captured "/agenda
    # review monday", and the anchored strip then mangled it to "a review monday" before shipping
    # it to hermes as a research question nobody asked.
    _BG_ONESHOT = re.compile(r"^\s*/(research|agent)\b\s*", re.I)
    # The manage-verb object is a NAMED task noun, not any gerund in range: "stop tracking me" and
    # "cancel my job application" both routed to the agent consent-free (manage verbs skip the
    # confirm gate). So bare 'tracking/monitoring' is rejected when a person is the object, and
    # 'job' is rejected when it heads a non-task noun phrase.
    #
    # The list/what-are arm requires a possessive or a task qualifier before the noun. Without it,
    # moving _BG_MANAGE ahead of the question deny-list turned every "what are ... jobs/monitors"
    # trivia question ("what are the biggest jobs in tech?") into a consent-free hermes delegation
    # — the reorder is only safe because this arm cannot match general-knowledge phrasing.
    _BG_MANAGE = re.compile(
        r"^\s*(?:please\s+)?(?:(?:list|show|what\s+(?:are|is)|what's)\b.{0,20}?\b"
        r"(?:my\s+(?:background\s+|scheduled\s+|monitoring\s+)?"
        r"|(?:the\s+)?(?:background|scheduled|monitoring|active|running|cron)\s+)"
        # SINGULAR TOO. "list all my task" was plural-only and fell through to the chat model,
        # which reached for the code interpreter and listed the upload directory instead.
        #
        # The noun must END the request. Allowing a singular noun mid-sentence immediately claimed
        # "show me my monitor resolution settings" and "list all my task list app ideas" — there
        # the word is a modifier on something else entirely, not the object of the request.
        r"(?:tasks?|monitors?|jobs?|watch(?:es)?)\b"
        r"(?=\s*[?.!]*\s*$|\s+(?:right\s+now|currently|again|please)\b)"
        r"|(?:cancel|stop|pause|resume|remove|delete)\b.{0,40}?\b(?:"
        r"(?:task|monitor|watch)(?:es|s)?\b"
        r"|jobs?\b(?!\s+(?:app(?:lication)?s?|offers?|interviews?|postings?|listings?"
        r"|search(?:es)?|hunts?|markets?)\b)"
        r"|(?:monitoring|tracking)\b(?!\s+(?:me|us|him|her|them)\b)"
        r"))", re.I)
    # Asking what is scheduled, phrased as people actually phrase it. _BG_MANAGE needs a literal
    # task NOUN behind a possessive ("my tasks"); none of these have one — "things you are
    # tracking" puts the noun in a verb and the object in a preposition — so they all reached the
    # CHAT model, which answered with an invented list of monitors the user never created.
    #
    # Only safe because the deterministic path answers these: a false positive costs a local table
    # render, not a 23 s eviction. Gated on MANAGE_DETERMINISTIC for exactly that reason.
    #
    # The object guard on the verb-phrase arms is load-bearing. Without it "what are you watching
    # on netflix", "what are you monitoring in the lab" and "what are you tracking in your fitness
    # app" all fired. Measured: 16/16 of the target phrasings, 0/42 of an ordinary-chat corpus.
    _BG_LNOUN = r"(?:tasks?|monitors?|jobs?|watches|watchers?|trackers?|alerts?|reminders?|automations?)"
    _BG_LVERB = r"(?:tracking|monitoring|watching|keeping an eye on)"
    _BG_LOBJ = (r"(?=\s*[?.!]*\s*$|\s+for\s+(?:me|us)\b|\s+right\s+now\b|\s+currently\b"
                r"|\s+at\s+the\s+moment\b)")
    # The noun must END the request (or the clause). Without this, "list all the tracks on that
    # album", "list the jobs at that company" and "show me the tasks in my jira board" all matched
    # — there the noun belongs to something else. '\s+or\b' lets a compound question through:
    # "are you tracking anything for me? or list all the trackers" is one ask, not two.
    _BG_LEND = (r"(?=\s*[?.!,]*\s*$|\s+(?:right\s+now|currently|again|please|for\s+me)\b"
                r"|\s*[?.!]\s|\s+or\b)")
    _BG_LIST = re.compile(
        r"^\s*(?:please\s+|hey\s+|so\s+)?(?:can you\s+|could you\s+|will you\s+)?"
        r"(?:"
        r"(?:what|show me what|tell me what)\s+(?:are\s+you|you(?:'re| are)|youre)\s+"
        r"(?:currently\s+|right now\s+)?" + _BG_LVERB + _BG_LOBJ +
        r"|(?:show|list|tell)\s+me\s+(?:all\s+|everything\s+)?(?:the\s+)?(?:things?\s+)?"
        r"(?:that\s+)?you(?:'re| are|re)?\s*(?:currently\s+)?" + _BG_LVERB + _BG_LOBJ +
        # Yes/no form. "are you tracking anything for me?" is the same question as "what are you
        # tracking", and it reached the chat model, which answered about conversation context.
        # 'anything|something|any X' is required so "are you tracking the election results" —
        # a question about the world, not about the scheduler — stays chat.
        r"|are\s+you\s+(?:currently\s+)?" + _BG_LVERB + r"\s+(?:anything|something|any\s+\w+)\b"
        # Bare imperative with no possessive: "list all the trackers".
        r"|(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:the|my)\s+" + _BG_LNOUN + _BG_LEND +
        r"|do\s+i\s+have\s+any\s+" + _BG_LNOUN + r"\b"
        r"|am\s+i\s+(?:currently\s+)?(?:tracking|monitoring|watching)\s+anything\b"
        r"|what\s+" + _BG_LNOUN + r"\s+(?:do\s+i\s+have|are\s+(?:there|running|scheduled|active))\b"
        r"|(?:is\s+)?anything\s+(?:running|scheduled|active)\s+(?:in\s+the\s+background|right now)\b"
        r")", re.I)

    # ---------- managing a specific job by reference ----------
    # The verb decides the operation; the rest of the message is the reference. 'restart' is
    # deliberately ABSENT — it reads as run-now, which re-arms and then deletes a one-shot, so it
    # stays with the agent rather than being silently absorbed by resume.
    _MANAGE_VERB = re.compile(
        # "also"/"and then" open a follow-up instruction, not a new subject — "also cancel b" was
        # falling through to the agent for want of one word.
        r"^\s*(?:please\s+|pls\s+|hey\s+|ok(?:ay)?[,\s]+|now\s+|also\s+|and\s+(?:also\s+|then\s+)?"
        r"|then\s+)*"
        r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:go\s+ahead\s+and\s+)?"
        # A leading "yes"/"ok" is agreement with the previous turn, not a different request —
        # "yes cancel this" was falling through to the agent for want of these five words.
        r"(?:(?:yes|yeah|yep|ok(?:ay)?|sure)[,\s]+)?"
        r"(?P<verb>cancel|delete|remove|stop|end|kill|get\s+rid\s+of|unsubscribe\s+from"
        r"|pause|hold|disable|suspend|turn\s+off"
        r"|resume|unpause|re-?enable|re-?activate|turn\s+back\s+on)\b", re.I)
    _MANAGE_OPS = {"cancel": "cancel", "delete": "cancel", "remove": "cancel", "stop": "cancel",
                   "end": "cancel", "kill": "cancel", "get rid of": "cancel",
                   "unsubscribe from": "cancel",
                   "pause": "pause", "hold": "pause", "disable": "pause", "suspend": "pause",
                   "turn off": "pause",
                   "resume": "resume", "unpause": "resume", "reenable": "resume",
                   "re-enable": "resume", "reactivate": "resume", "re-activate": "resume",
                   "turn back on": "resume"}
    _ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
                      "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
    _ORDINAL_RE = re.compile(
        r"\b(?:(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
        r"|(\d{1,2})(?:st|nd|rd|th)?"
        r"|#\s*(\d{1,2})"
        r"|(?:number|no\.?|item|entry|task|job|row)\s*(\d{1,2}))\b", re.I)
    # Words that carry no identity. 'such' is here on purpose: "cancel such and such tracking" is a
    # PLACEHOLDER, not a name, so it must reduce to zero tokens and be treated as a bare reference.
    _REF_STOP = frozenset("""the a an my our your this that those these it its one ones thing
        things please for me us and or of on in to now right currently task tasks job jobs monitor
        monitors monitoring watch watches watching watcher tracker trackers tracking alert alerts
        reminder reminders cron crons automation automations background scheduled active running
        such is are was were do does did i you he she they them there here what which""".split())
    # "All of them" is a real request, not an ambiguity. Refusing it and offering a single-choice
    # menu ("say a, b, or give me an id") is a dead end — the user has to cancel one, wait, then
    # find the next one in a renumbered list. So bulk RESOLVES, and safety comes from the
    # confirmation naming every job it is about to delete.
    _REF_BULK = re.compile(
        r"\b(?:all|everything|every\s+one|everyone|each\s+of\s+them|both|the\s+lot|"
        r"all\s+of\s+(?:them|the|my)|any\s+of\s+them)\b", re.I)
    # Exclusion still refuses: "everything except the rtx one" is a set the user described by what
    # is NOT in it, and getting that wrong deletes the one thing they wanted to keep.
    _REF_QUANTIFIER = re.compile(r"\b(?:the\s+rest)\b", re.I)
    # Filler around a bare handle. "b too" and "also b" mean the same thing as "b", and losing them
    # is what made a follow-up cancel fall through to a no-match.
    _REF_FILLER = re.compile(r"\b(?:too|also|as\s+well|please|then|next|now|one)\b", re.I)
    # Multi-select: "a and b", "1, 2", "the first and the third".
    _REF_JOINER = re.compile(r"\s*(?:,|&|\+|\band\b)\s*", re.I)
    _REF_NEGATION = re.compile(
        r"\b(?:not|isn'?t|aren'?t|except|other\s+than|besides|apart\s+from|rather\s+than"
        r"|instead\s+of|but\s+the|the\s+other)\b", re.I)

    # Rendered-list state: ordinal N maps to ids[N-1], never recomputed from a fresh fetch.
    _JOBS_MARK_RE = re.compile(r"<!--bg-jobs:([A-Za-z0-9+/=]*)-->")
    # An armed destructive op, or an open disambiguation.
    _CONFIRM_MARK_RE = re.compile(r"<!--bg-confirm:([A-Za-z0-9+/=]*)-->")
    # Deleting is irreversible (hermes rmtree's the job's output directory), so a bare "ok" or
    # "sure" is NOT enough to trigger one — those are acknowledgements, not decisions. The
    # permissive set stays for the reversible pause downgrade below.
    # A confirmation is a bare affirmative. 'delete' on its own is deliberately NOT here: with a
    # cancel armed on one job, "delete the job <other id>" matched it and deleted the ARMED job
    # instead of the named one — a fresh instruction read as an answer to a question about
    # something else. Only the pronoun forms ("delete it") refer back to what was asked.
    _CONFIRM_YES = re.compile(
        r"^\s*(?:yes|yeah|yep|yup|do it|delete it|cancel it|confirm(?:ed)?|"
        r"yes please|go ahead and delete)\b[\s\S]{0,40}$", re.I)
    _CONFIRM_ALT = re.compile(
        r"^\s*(?:just\s+)?(?:pause|pause it|pause instead|switch it off|turn it off|disable it)\b",
        re.I)
    # 'stop' is deliberately absent: on a confirm turn it is ambiguous between "stop asking" and
    # "stop the job", so it falls through to the abandoned branch rather than guessing.
    _CONFIRM_NO = re.compile(
        r"^\s*(?:n|no|nope|nah|don'?t|do not|wait|never\s?mind|nvm|leave it|keep it|"
        r"forget it|cancel that|no thanks?)\b", re.I)

    _BG_VERB = re.compile(
        r"^\s*(?:please\s+|can you\s+|could you\s+)?"
        r"(?:monitor|track|watch|keep an eye on|keep track of|alert me|notify me|remind me|ping me"
        # "create alert to search online for X" — the verb alone would swallow media requests
        # ("make a picture"), so the arm requires the alert/monitor noun to follow it.
        # The noun must END the phrase or take a complement (for/to/on/about/when/if) — "create
        # an alert" schedules, "create an alert dialog in react" builds an artifact and must not.
        r"|(?:create|set up|add|make)\s+(?:an?\s+)?(?:new\s+)?(?:price\s+|fare\s+|stock\s+)?"
        r"(?:(?:alert|monitor|tracker)s?|watch(?:es)?)"
        r"(?=\s*(?:$|[,.!:;]|(?:for|to|on|about|when|if)\b)))\b", re.I)
    _BG_RECURRENCE = re.compile(
        # "minute|min" in that order so "minutes" is taken whole; the trailing s? then also
        # covers the live phrasing "every 5 mins", which used to fall through to the chat model.
        r"\b(?:every\s+(?:\d+\s+)?(?:minute|min|hour|hr|day|week|morning|evening|night)s?"
        r"|hourly|daily|weekly|nightly"
        # No minutes here on purpose: "keep an eye on the oven for 20 mins" is talk, not a task.
        # A bounded fast check reaches us via its interval ("check every 5 mins") instead.
        r"|for\s+(?:the\s+)?(?:next\s+)?\d+\s+(?:hour|hr|day|week|month)s?"
        r"|for\s+(?:a|two|three|the next few)\s+(?:hour|day|week|month)s?"
        r"|until\s+(?:it|the|price)"
        r"|(?:when|if|once)\s+(?:it|(?:the\s+)?(?:price|value|fare|cost)|it's|stock)\b.{0,30}"
        r"\b(?:drops?|falls?|changes?|"
        r"rises?|goes\s+(?:below|above|down|up)|hits|reaches|back in stock|available"
        # "is under $150" states a threshold as readily as "drops below" — but only with a
        # NUMBER attached; "under review", "more than I can afford" are prose, not thresholds.
        r"|is\s+(?:under|below|above|over|less\s+than|more\s+than)\s+\$?\d"
        r"|(?:under|below)\s+\$?\d)"
        r"|in\s+\d+\s+(?:minute|min|hour|hr|day|week)s?\b)", re.I)
    _BG_QUESTION = re.compile(
        r"^\s*(?:how|what|which|why|is there|are there|do you know|can i|should i)\b", re.I)

    # Every hermes reply carries this marker. HTML comments do not render in the chat, so it is
    # invisible to the user but survives into the message history the next turn receives — which is
    # how a follow-up ("yes, reenable it") knows it belongs to the task conversation rather than to
    # the general chat model.
    _BG_MARK = "<!--bg-task-->"
    # Short conversational follow-ups. These are only honoured when the PREVIOUS assistant turn was
    # a hermes reply; on their own they are ordinary chat. Live failure that motivated this: after
    # the agent asked "re-enable this one, or create new?", the user answered "yes reenable" — which
    # matched no bg predicate, went to the chat model, and got a confident hallucinated confirmation
    # citing the real job id it had read from the transcript.
    _BG_FOLLOWUP = re.compile(
        r"^\s*(?:yes|yeah|yep|ok(?:ay)?|sure|please|do it|go ahead|sounds good|"
        r"(?:re-?)?enable|(?:re-?)?activate|resume|restart|re-?run|run it|"
        r"the first|the second|that one|this one|both|neither|new one|a new one|"
        r"cancel|stop|pause|remove|delete|no)\b[\s\S]{0,80}$", re.I)

    # A change to an EXISTING job, not a request for a new one — "change/adjust the alert to
    # 15 mins", not "create an alert". Distinct from _MANAGE_VERB (cancel/pause/resume take no
    # new value) and from _BG_VERB (which describes what to watch, not how to change one already
    # running). The gap between verb and target is bounded so an unrelated "change" and "schedule"
    # far apart in a long message do not pair up; "the" is not required — "change alert timing"
    # is just as much an edit as "change the alert timing".
    _EDIT_VERB = re.compile(
        r"\breschedule\b|"
        r"\b(?:change|adjust|update|modify|switch)\b.{0,40}?"
        r"\b(?:the\s+)?(?:alert|schedule|frequency|interval|timing|cadence|check(?:s|ing)?)\b",
        re.I)
    # A task that should TEXT the user needs a number on file. Without this check the job is
    # created, runs, fires, and the alert is skipped with "no phone for 'ohmz'" in a log nobody
    # reads — the user believes they are being watched and hears nothing.
    _WANTS_ALERT = re.compile(
        r"\b(?:text|sms|message)\s+me\b|\bnotify\s+me\b|\balert\s+me\b|\bping\s+me\b|"
        r"\blet\s+me\s+know\b|\btell\s+me\s+(?:when|if|as soon as)\b|"
        r"\bsend\s+(?:me\s+)?a\s+(?:text|sms|message)\b|\b(?:text|sms)\s+(?:alert|me)\b",
        re.I)
    # Carries the pending request across the "what is your number?" turn. Base64 so the original
    # wording (which may contain quotes, newlines or --) cannot break the HTML comment or leak into
    # the rendered chat.
    _PHONE_MARK_RE = re.compile(r"<!--bg-need-phone:([A-Za-z0-9+/=]*)-->")
    _PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
    _PHONE_DECLINE = re.compile(
        r"^\s*(?:no|nope|skip|later|don'?t|do not|email only|just email|no thanks?)\b", re.I)

    # What kind of thing is being watched, decided from the user's own words. Rules first because
    # they are instant, testable and right on the phrasings people actually use; anything they do
    # not recognise is left to the agent, which picks from the same list at creation time. The
    # model never supplies a NUMBER — only a category — so the hallucination surface stays closed.
    _KIND_RULES = [
        # out_of_stock FIRST, and it is the added phrasings that make the order load-bearing:
        # "no longer in stock" and "not in stock" both contain "in stock", so back_in_stock's
        # \bin stock\b used to claim them and a sell-out watch was classified as a restock watch.
        # "sells out" was matched by neither rule and fell through to price_drop.
        ("out_of_stock",  r"\bout of stock\b|\bsold out\b|\bsells?\s+out\b|\bruns out\b|"
                          r"\bno longer (?:in stock|available)\b|\bnot in stock\b"),
        ("back_in_stock", r"\bback in stock\b|\bin stock\b|\brestock|\bavailable again\b"),
        # Plurals matter here and the singular-only version was a live miss: a job created on
        # 2026-08-07 for "Toronto to Vancouver flightS" classified as price_drop, because
        # \bflight\b cannot match "flights". Everything fare-specific then stayed switched off.
        ("fare",          r"\bfares?\b|\bflights?\b|\bairfares?\b|\bticket prices?\b|"
                          r"\bround.?trips?\b"),
        ("inventory",     r"\binventory\b|\bhow many\b|\bunits? left\b|\bstock level\b|\bquantity\b"),
        ("availability",  r"\bappointment\b|\breservation\b|\bslot\b|\bbooking\b|\bavailability\b"),
        ("price_rise",    r"\b(?:goes?|rise|rises|climbs?|above|over|exceeds?)\s+(?:above|over|past)?\s*\$?\d"),
        ("price_drop",    r"\bprice\b|\bcheaper\b|\bdiscount\b|\bdeal\b|\bon sale\b|"
                          r"\b(?:below|under|less than|drops? to|drops? below)\b"),
    ]

    @classmethod
    def _guess_kind(cls, text):
        """The alert kind implied by a request, or None to let the agent decide."""
        t = (text or "").lower()
        for kind, pat in cls._KIND_RULES:
            if re.search(pat, t):
                return kind
        return None

    # ================= flight requests =================
    # Airports resolvable by name. Curated, not complete, ON PURPOSE: an unknown place ASKS, and
    # asking costs one turn, whereas guessing puts someone on a plane to the wrong city. Metro codes
    # are preferred where they exist (YTO, NYC, LON, PAR) because every fare site accepts them and
    # somebody saying "Toronto" usually means any Toronto airport, not Pearson specifically.
    #
    # Inline rather than a pipes/shared/ sidecar: deploy_pipe.py's SIDECARS is an explicit dict, so a
    # sidecar would mean a new registration, a new byte-check in test_deployed.py, and a new
    # silent-staleness mode — which that file's own comment calls "the reason a stale copy would never
    # surface on its own". This ships atomically with the pipe.
    _IATA = {
        # Canada
        "toronto": ("YTO", "Toronto"), "pearson": ("YYZ", "Toronto Pearson"),
        "billy bishop": ("YTZ", "Toronto Billy Bishop"), "vancouver": ("YVR", "Vancouver"),
        "montreal": ("YUL", "Montreal"), "calgary": ("YYC", "Calgary"),
        "edmonton": ("YEG", "Edmonton"), "ottawa": ("YOW", "Ottawa"),
        "winnipeg": ("YWG", "Winnipeg"), "halifax": ("YHZ", "Halifax"),
        "quebec city": ("YQB", "Quebec City"), "victoria": ("YYJ", "Victoria"),
        "saskatoon": ("YXE", "Saskatoon"), "regina": ("YQR", "Regina"),
        "st johns": ("YYT", "St John's"), "kelowna": ("YLW", "Kelowna"),
        "abbotsford": ("YXX", "Abbotsford"), "hamilton": ("YHM", "Hamilton ON"),
        # United States
        "new york": ("NYC", "New York"), "jfk": ("JFK", "New York JFK"),
        "newark": ("EWR", "Newark"), "laguardia": ("LGA", "New York LaGuardia"),
        "los angeles": ("LAX", "Los Angeles"), "san francisco": ("SFO", "San Francisco"),
        "chicago": ("CHI", "Chicago"), "boston": ("BOS", "Boston"),
        "seattle": ("SEA", "Seattle"), "miami": ("MIA", "Miami"),
        "orlando": ("MCO", "Orlando"), "las vegas": ("LAS", "Las Vegas"),
        "denver": ("DEN", "Denver"), "atlanta": ("ATL", "Atlanta"),
        "dallas": ("DFW", "Dallas"), "houston": ("IAH", "Houston"),
        "phoenix": ("PHX", "Phoenix"), "washington": ("WAS", "Washington DC"),
        "philadelphia": ("PHL", "Philadelphia"), "san diego": ("SAN", "San Diego"),
        "honolulu": ("HNL", "Honolulu"), "detroit": ("DTW", "Detroit"),
        "minneapolis": ("MSP", "Minneapolis"), "austin": ("AUS", "Austin"),
        # Europe
        "london": ("LON", "London"), "heathrow": ("LHR", "London Heathrow"),
        "gatwick": ("LGW", "London Gatwick"), "paris": ("PAR", "Paris"),
        "amsterdam": ("AMS", "Amsterdam"), "frankfurt": ("FRA", "Frankfurt"),
        "munich": ("MUC", "Munich"), "berlin": ("BER", "Berlin"),
        "madrid": ("MAD", "Madrid"), "barcelona": ("BCN", "Barcelona"),
        "lisbon": ("LIS", "Lisbon"), "porto": ("OPO", "Porto"),
        "rome": ("ROM", "Rome"), "milan": ("MIL", "Milan"),
        "venice": ("VCE", "Venice"), "zurich": ("ZRH", "Zurich"),
        "geneva": ("GVA", "Geneva"), "vienna": ("VIE", "Vienna"),
        "prague": ("PRG", "Prague"), "budapest": ("BUD", "Budapest"),
        "warsaw": ("WAW", "Warsaw"), "copenhagen": ("CPH", "Copenhagen"),
        "stockholm": ("STO", "Stockholm"), "oslo": ("OSL", "Oslo"),
        "helsinki": ("HEL", "Helsinki"), "dublin": ("DUB", "Dublin"),
        "edinburgh": ("EDI", "Edinburgh"), "manchester": ("MAN", "Manchester"),
        "reykjavik": ("KEF", "Reykjavik"), "athens": ("ATH", "Athens"),
        "istanbul": ("IST", "Istanbul"), "brussels": ("BRU", "Brussels"),
        # Asia, Middle East, Africa
        "tokyo": ("TYO", "Tokyo"), "narita": ("NRT", "Tokyo Narita"),
        "haneda": ("HND", "Tokyo Haneda"), "osaka": ("OSA", "Osaka"),
        "seoul": ("SEL", "Seoul"), "beijing": ("BJS", "Beijing"),
        "shanghai": ("SHA", "Shanghai"), "hong kong": ("HKG", "Hong Kong"),
        "taipei": ("TPE", "Taipei"), "singapore": ("SIN", "Singapore"),
        "bangkok": ("BKK", "Bangkok"), "kuala lumpur": ("KUL", "Kuala Lumpur"),
        "jakarta": ("CGK", "Jakarta"), "manila": ("MNL", "Manila"),
        "delhi": ("DEL", "Delhi"), "new delhi": ("DEL", "Delhi"),
        "mumbai": ("BOM", "Mumbai"), "bangalore": ("BLR", "Bangalore"),
        "chennai": ("MAA", "Chennai"), "hyderabad": ("HYD", "Hyderabad"),
        "karachi": ("KHI", "Karachi"), "lahore": ("LHE", "Lahore"),
        "islamabad": ("ISB", "Islamabad"), "dhaka": ("DAC", "Dhaka"),
        "colombo": ("CMB", "Colombo"), "kathmandu": ("KTM", "Kathmandu"),
        "dubai": ("DXB", "Dubai"), "abu dhabi": ("AUH", "Abu Dhabi"),
        "doha": ("DOH", "Doha"), "riyadh": ("RUH", "Riyadh"),
        "jeddah": ("JED", "Jeddah"), "tel aviv": ("TLV", "Tel Aviv"),
        "cairo": ("CAI", "Cairo"), "nairobi": ("NBO", "Nairobi"),
        "johannesburg": ("JNB", "Johannesburg"), "cape town": ("CPT", "Cape Town"),
        "lagos": ("LOS", "Lagos"), "casablanca": ("CMN", "Casablanca"),
        "addis ababa": ("ADD", "Addis Ababa"),
        # Oceania, Latin America
        "sydney": ("SYD", "Sydney"), "melbourne": ("MEL", "Melbourne"),
        "brisbane": ("BNE", "Brisbane"), "perth": ("PER", "Perth"),
        "auckland": ("AKL", "Auckland"), "mexico city": ("MEX", "Mexico City"),
        "cancun": ("CUN", "Cancun"), "sao paulo": ("SAO", "Sao Paulo"),
        "rio": ("RIO", "Rio de Janeiro"), "rio de janeiro": ("RIO", "Rio de Janeiro"),
        "buenos aires": ("BUE", "Buenos Aires"), "santiago": ("SCL", "Santiago"),
        "lima": ("LIM", "Lima"), "bogota": ("BOG", "Bogota"),
        "havana": ("HAV", "Havana"), "san juan": ("SJU", "San Juan"),
        "punta cana": ("PUJ", "Punta Cana"), "montego bay": ("MBJ", "Montego Bay"),
    }
    _CODE_NAME = {c: n for c, n in _IATA.values()}

    # --- intent. DEFAULT DENY, and the deny arms run FIRST, because "my flight was delayed" carries
    # every positive token a real request does. Same discipline as _is_image_request (:868-877).
    _FLIGHT_FIGURATIVE = re.compile(
        r"\bflights?\s+of\s+(?:stairs?|fancy|steps?|imagination)\b"
        r"|\b(?:flight|aviation)\s+(?:simulator|sim|school|attendant|crew|deck|recorder|path|risk"
        r"|plan|log|academy|training)\b"
        r"|\bin[\s-]flight\s+(?:entertainment|meal|wifi|service)\b"
        r"|\b(?:bus|train|taxi|uber|lyft|transit|subway|metro|cab|ferry|toll)\s+fares?\b"
        r"|\btook\s+flight\b|\bpowered\s+flight\b|\bflight\s+of\s+the\b"
        r"|\bhow\s+(?:did|does|do)\s+(?:you|i|we|they|it)\s+fare\b|\bfare\s+thee\s+well\b", re.I)
    _FLIGHT_PAST = re.compile(
        r"\b(?:my|our|his|her|their|the)\s+flight\s+(?:was|is|got|has|had|arrives?|arrived"
        r"|lands?|landed|leaves?|left|departs?|took|boards?|gets?\s+in)\b"
        r"|\bi\s+(?:flew|took\s+a\s+flight|watched|saw|read|missed|already\s+booked|just\s+booked)\b"
        r"|\bflights?\s+(?:i|we|they)\s+(?:booked|took|missed)\b", re.I)
    # Asking ABOUT air travel rather than for a flight. Narrower than _BG_QUESTION, because
    # "how much is a flight to reykjavik in feb" IS a request wearing a question mark. Manage verbs
    # are denied here so "stop watching flights to vancouver" reaches the scheduler path.
    _FLIGHT_META = re.compile(
        r"^\s*(?:how|why)\s+(?:do|does|are|is|come)\b"
        r"|^\s*is\s+it\s+(?:cheap(?:er)?|better|worth|smart|ok|possible|true|safe)\b"
        r"|^\s*should\s+i\b|^\s*(?:do|does)\s+(?:flight|airline|fare|price)s?\b"
        r"|^\s*wh(?:at|ich)\s+(?:airlines?|carriers?|planes?|aircraft|airports?)\b"
        r"|^\s*wh(?:at|en)\s+is\s+the\s+(?:best|cheapest|worst)\s+(?:time|day|month|season)\b"
        r"|^\s*(?:cancel|delete|remove|stop|pause|resume|end|kill|disable|unpause|turn\s+off)\b",
        re.I)
    # Signal 1: this is about air travel. Bare \bfares?\b is included because _KIND_RULES already
    # uses it for the fare kind and the two must agree; its false friends are denied above.
    _FLIGHT_NOUN = re.compile(
        r"\bflights?\b|\bairfares?\b|\bair\s?fares?\b|\bfares?\b|\bplane\s+tickets?\b"
        r"|\bairline\s+tickets?\b|\bround[\s-]?trips?\b|\bred[\s-]?eyes?\b"
        # "a one way to calgary" and "cheapest nonstop from montreal" name air travel with no other
        # noun in the sentence. Both are safe here only because a route AND a request frame are still
        # required: "one way or another" and "she talked nonstop" name no place, and "it's a one way
        # street to Rome" matches no imperative. Pinned by negatives in tests/test_flight_intent.py.
        r"|\bone[\s-]?way\b|\bnon[\s-]?stops?\b|\blayovers?\b"
        r"|\bfly(?:ing)?\s+(?:from|to|out)\b", re.I)
    # An IATA pair is a noun and a route in one token. Case is the precision signal, so this runs on
    # the ORIGINAL text; a lowercase pair needs a hard separator and must be in the table.
    _FLIGHT_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:->|→|–|—|-|to|/)\s*([A-Z]{3})\b")
    _FLIGHT_PAIR_LC = re.compile(r"\b([a-z]{3})\s*(?:->|→|/|-)\s*([a-z]{3})\b")
    # Signal 2: a request frame, anchored. Position is the discriminator, as in _IMPERATIVE_DRAW.
    _FLIGHT_ASK = re.compile(
        r"^\s*(?:please\s+|pls\s+|hey\s+|ok(?:ay)?[,\s]+|so\s+|and\s+|also\s+)*"
        r"(?:(?:can|could|will|would)\s+you\s+(?:please\s+)?)?"
        r"(?:find|search|look|get|show|give|book|price|check|compare|watch|track|monitor"
        r"|keep\s+an\s+eye\s+on|alert\s+me|notify\s+me|text\s+me|ping\s+me|email\s+me"
        r"|tell\s+me\s+(?:when|if)|let\s+me\s+know\s+(?:when|if)"
        r"|i\s+(?:want|need|am\s+looking|wanna|would\s+like)|looking\s+for|set\s+up|add)\b", re.I)
    _FLIGHT_SUPER = re.compile(
        r"\b(?:cheap(?:est|er)?|best|lowest|good|affordable|deals?\s+on)\s+(?:\w+\s+){0,2}"
        r"(?:flights?|fares?|airfares?|tickets?|deals?)\b"
        r"|\b(?:flights?|fares?|tickets?)\s+(?:under|below)\s+\$?\d"
        r"|\bhow\s+much\s+(?:is|are|would|will)\b[^?]{0,30}\b(?:flights?|fares?|tickets?)\b", re.I)
    # Signal 3: somewhere to go. This is what separates a request from a question about pricing in
    # general — "is it cheaper to book flights on a tuesday" has the noun and a superlative and names
    # no destination.
    _FL_STOP = (r"(?:book|buy|fly|flying|get|find|save|be|do|go|going|make|watch|track|see|check"
                r"|know|my|the|a|an|it|there|that|this|and|or|me|us|now|then)")
    _FLIGHT_PLACE = re.compile(rf"\b(?:to|from|into)\s+(?!{_FL_STOP}\b)([A-Za-z][A-Za-z.'\- ]{{2,}})",
                               re.I)

    _FLIGHT_CLASSIFY_PROMPT = (
        "Classify the user's message. Answer with ONE word, nothing else.\n"
        "Answer FLIGHT if they want you to find, price, book or watch an air fare or plane ticket "
        "for a trip they have not taken yet.\n"
        "Answer OTHER for anything else, including a flight they already took or booked, questions "
        "about airlines or airports, how ticket pricing works, and any other kind of price or stock "
        "watching.\n\n"
        "Examples:\n"
        "flights to tokyo -> FLIGHT\n"
        "my flight was delayed three hours -> OTHER\n"
        "yyz yvr september -> FLIGHT\n"
        "what airline flies to osaka -> OTHER\n"
        "round trip lisbon october -> FLIGHT\n"
        "how do flight prices work -> OTHER\n"
        "airfare tokyo march under 900 -> FLIGHT\n"
        "track the price of the rtx 5090 -> OTHER\n\n"
        "Message: {msg}\nAnswer:")

    def _flight_deny(self, text):
        t = text or ""
        return bool(self._FLIGHT_FIGURATIVE.search(t) or self._FLIGHT_PAST.search(t)
                    or self._FLIGHT_META.search(t))

    def _flight_pair(self, text):
        m = self._FLIGHT_PAIR.search(text or "")
        if m:
            return m.group(1).upper(), m.group(2).upper()
        m = self._FLIGHT_PAIR_LC.search((text or "").lower())
        if m:
            a, b = m.group(1).upper(), m.group(2).upper()
            if a in self._CODE_NAME and b in self._CODE_NAME:
                return a, b
        return None

    def _classify_flight(self, text):
        """The 1B tier for the ambiguous band. Same contract as _classify_code, deliberately.

        Any failure returns False so the turn falls back to ordinary routing — a routing helper must
        never be able to break the chat path. Every call writes a job:'classifier' row carrying
        domain='flight', which is what keeps the two consult streams separable in route_metrics.
        """
        t0 = time.monotonic()
        try:
            r = requests.post(
                f"{self.ollama}/api/chat",
                json={"model": ROUTE_CLASSIFIER_MODEL, "stream": False, "think": False,
                      "options": {"temperature": 0, "num_predict": 4},
                      "messages": [{"role": "user",
                                    "content": self._FLIGHT_CLASSIFY_PROMPT.format(msg=text[:600])}]},
                timeout=ROUTE_CLASSIFIER_TIMEOUT)
            latency = round((time.monotonic() - t0) * 1000)
            if r.status_code != 200:
                self._metric(job="classifier", domain="flight", ok=False,
                             error=f"http_{r.status_code}", latency_ms=latency)
                return False
            verdict = ((r.json().get("message") or {}).get("content") or "").strip().upper()
            self._metric(job="classifier", domain="flight", ok=True, verdict=verdict[:12],
                         latency_ms=latency)
            return verdict.startswith("FLIGHT")
        except Exception as e:
            self._metric(job="classifier", domain="flight", ok=False,
                         error=type(e).__name__, latency_ms=round((time.monotonic() - t0) * 1000))
            return False

    # Travel between two places that is explicitly NOT flying. This is the whole ambiguity in a
    # route with no flight noun — "from Toronto to Vancouver under $1000" is a fare unless the
    # message says otherwise — so naming the exceptions is what lets the route band be decided here
    # instead of by a model. Kept to modes and carriers that are unambiguous in any sentence:
    # 'package' and 'cargo' are deliberately absent because a holiday package is a flight ask, and
    # 'ship a package' is already caught by 'ship'.
    _FL_OTHER_MODE = re.compile(
        r"\b(?:driv(?:e|es|ing)|road\s?trip|car\s+rental|rent\s+a\s+car|mileage"
        r"|train|rail|via\s?rail|amtrak|bus|coach|greyhound|megabus"
        r"|ferry|cruise|boat|sail(?:ing)?"
        r"|ship(?:s|ping|ment)?|courier|freight|fedex|purolator"
        r"|mov(?:e|es|ing|ers)|u-?haul"
        r"|hotel|airbnb|hostel)\b", re.I)

    def _fl_route(self, text):
        """True when the text names a resolvable origin AND destination — a ROUTE, with no flight
        noun required. Deterministic: both ends must be in _IATA, so this asserts "these are two
        airports" and never "this is about flying", which is the classifier's call.

        Same-airport pairs are rejected. "from toronto to toronto" is a typo or a joke, and either
        way sending it down the flight path costs a question about dates for a trip nobody is taking.
        """
        o, d, _unknown = self._fl_places(text)
        return bool(o and d and o[0] != d[0])

    def _is_flight_request(self, text):
        """(tier, rule) when this turn asks for a flight fare, else (None, None)."""
        if not FLIGHT_ROUTE or not text:
            return None, None
        if self._flight_deny(text):
            return None, None
        pair = self._flight_pair(text)
        framed = bool(self._FLIGHT_ASK.match(text) or self._FLIGHT_SUPER.search(text))
        if pair or self._FLIGHT_NOUN.search(text):
            placed = bool(pair or self._FLIGHT_PLACE.search(text))
            if framed and placed:
                return 1, "flight_strong"
        # No noun and no pair — the ROUTE band. A flight ask does not have to name a flight: "track
        # price from Toronto to Vancouver, text me under 1000" is what a person actually types, and
        # it is what created job ba2a91e18def on 2026-08-08 — a fare watch with no itinerary in it,
        # which would have refused itself on all 8 runs under a confirmation card that read like a
        # working watch. It matched no arm here: the frame matched, the places matched, and the word
        # "flights" was the one thing missing. tests/test_flight_intent.py already carried the
        # near-identical POSITIVE case one word away ("track Toronto to Vancouver flights under
        # $600"), which is how close this sat to being caught.
        #
        # DECIDED HERE, NOT BY THE CLASSIFIER, and that is a measurement rather than a preference.
        # This band was first routed to the 1B tier, on the theory that a bare route is ambiguous.
        # Measured against gemma3:1b on 2026-08-08: 7 of 11, and it answered OTHER for the live
        # message above, for "watch the price toronto to vancouver, alert me under 900" and for
        # "monitor prices from montreal to lisbon in october" — its own prompt says OTHER covers
        # "any other kind of price watching", which is exactly what a route price ask looks like. A
        # reworded prompt reached 9 of 11 and started leaking a moving quote. So the ambiguity is
        # named instead: _fl_route proves two AIRPORTS deterministically, _FLIGHT_ASK proves a
        # request, and _FL_OTHER_MODE removes the sentences that say they are about some other way
        # of travelling. What is left is a fare ask, and the cost of being wrong is one question
        # about dates — against a failure mode that silently schedules a watch that cannot work.
        #
        # Tier 2, so this stream stays separable from both the strong tier and the classifier in
        # route_metrics. `framed` short-circuits first: _fl_route scans the whole airport table
        # (~0.3 ms), and an anchored request verb keeps that off every message in every chat.
        elif framed and self._fl_route(text) and not self._FL_OTHER_MODE.search(text):
            return 2, "flight_route"
        else:
            return None, None
        # HINT band. Suppressed on developer vocabulary: "build me a flight search API" has the noun,
        # no frame, and belongs to the coder tier further down the cascade.
        if not FLIGHT_CLASSIFIER or self._CODE_HINT.search(text):
            return None, None
        if self._classify_flight(text):
            return 3, "flight_classifier"
        return None, None

    # --- slots. Everything here is deterministic. The model never supplies a date, an airport or a
    # number, only ever a category — the same closed-hallucination-surface rule _KIND_RULES states
    # at :1095. A guessed itinerary is watched forever and the user finds out at the airport.
    _FL_MON = {m: i for i, m in enumerate(
        ["", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
        if m}
    _FL_MONWORD = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
                   r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
    _FL_ISO = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
    _FL_MD = re.compile(rf"\b({_FL_MONWORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
    _FL_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_FL_MONWORD})\b", re.I)
    # Day names spelled out, with NO trailing [a-z]* — that wildcard made "next month" match `mon`
    # and then swallow the "th", so "next month" resolved to a specific next-Monday date and the
    # form completed on a departure the user never gave. A guessed date is the one failure this
    # parser exists to prevent (:1391), and it was reachable from two very ordinary words.
    _FL_NEXTDOW = re.compile(r"\b(?:next|this)\s+(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?"
                             r"|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.I)
    _FL_INN = re.compile(r"\bin\s+(\d{1,2})\s+(day|week|month)s?\b", re.I)
    # A part-month is a RANGE; a bare month is a MONTH. Ordering matters and is load-bearing:
    # "first week of september" must be claimed here before the bare-month arm sees "september".
    _FL_PARTMON = re.compile(rf"\b(early|mid(?:dle)?|late|first\s+week\s+of|last\s+week\s+of"
                             rf"|second\s+week\s+of|third\s+week\s+of|beginning\s+of|end\s+of)\s+"
                             rf"({_FL_MONWORD})\b", re.I)
    # A month needs SOMETHING in front of it, because half the month names are ordinary English:
    # "may" is a modal, "march" is a verb, "august" is an adjective. The original set was
    # prepositions only, which meant the form could invite "a month like March" and then fail to
    # read the answer: "leaving October and returning nov" parsed to nothing at all, the turn fell
    # through to the agent, and the agent replied about flying to Astana and searching Amazon.ca
    # (reported live 2026-08-09). Departure and return CUES are the missing half of the same idea —
    # "leaving October" is exactly as unambiguous as "in October", and it is what people type when
    # asked when they want to go.
    _FL_MONCUE = (r"in|during|sometime\s+in|for|around"
                  r"|leav(?:e|ing)|depart(?:ing|s)?|fly(?:ing)?\s+out|head(?:ing)?\s+out|go(?:ing)?"
                  r"|out|return(?:ing|s)?|back|coming\s+back|home")
    _FL_BAREMON = re.compile(rf"\b(?:{_FL_MONCUE})\s+(?:in\s+|on\s+|of\s+)?({_FL_MONWORD})\b"
                             rf"(?!\s*\.?\s*\d)", re.I)
    # A month standing completely alone, allowed ONLY while the flight form is open (see
    # _fl_find_dates' `bare`). There the question was literally "when?", so "October" is an answer
    # and not a modal verb — the ambiguity that forces a cue everywhere else does not exist.
    _FL_MONALONE = re.compile(rf"\b({_FL_MONWORD})\b(?!\s*\.?\s*\d)", re.I)
    _FL_ONEWAY = re.compile(r"\bone[\s-]?way\b|\bno\s+return\b|\bnot\s+coming\s+back\b"
                            r"|\bsingle\s+ticket\b|\bjust\s+going\b", re.I)
    _FL_TRIPLEN = re.compile(r"\bfor\s+(?:(\d{1,2})|a|one|two|three|four)\s+(day|week|month|night)s?\b"
                             r"|\b(\d{1,2})\s+nights?\b|\b(long\s+weekend)\b", re.I)
    _FL_SEASON = re.compile(r"\b(spring|summer|fall|autumn|winter|holidays?|christmas|new\s+year)\b",
                            re.I)
    _FL_TARGET = re.compile(r"\b(?:under|below|less\s+than|at\s+most|max(?:imum)?|budget\s+of"
                            r"|no\s+more\s+than|cheaper\s+than)\s*\$?\s*([\d,]+(?:\.\d{2})?)\b", re.I)
    # The verbs that make a flight ask a WATCH rather than a lookup. Deliberately narrower than
    # _FLIGHT_ASK, which also matches find/show/search — those are lookups.
    _FL_WATCHY = re.compile(r"\b(?:track|watch|monitor|keep\s+an\s+eye|alert\s+me|notify\s+me"
                            r"|text\s+me|email\s+me|ping\s+me|let\s+me\s+know|tell\s+me\s+(?:when|if)"
                            r"|set\s+up\s+(?:a\s+|an\s+)?(?:alert|watch|tracker))\b", re.I)
    # A North-American phone number given inline. _norm_phone is the validator; this only finds
    # the candidate token.
    _FL_PHONE_INLINE = re.compile(r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")
    # How often the user asked to be checked, verbatim.
    # "That answer was close, but change this." Distinguishes a correction the answer invited from
    # an unrelated sentence that merely contains a month-shaped word.
    _FL_CHANGE = re.compile(r"\b(?:actually|instead|rather|change|make\s+it|switch|move\s+it"
                            r"|how\s+about|what\s+about|can\s+we\s+do|let'?s\s+do|different"
                            r"|no,?\s+(?:make|do|try)|update\s+it)\b", re.I)
    _FL_UNITS = r"m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?"
    # The bound is optional AND its lead-in is: people write "every 15 mins next 2 hours" as often
    # as "...for the next 2 hours". Requiring for|over captured only half the phrase, which is the
    # half that matters least.
    _FL_CADENCE = re.compile(rf"\bevery\s+\d{{1,4}}\s*(?:{_FL_UNITS})\b"
                             rf"(?:\s*,?\s*(?:for\s+|over\s+)?(?:the\s+)?(?:next\s+)"
                             rf"\d{{1,3}}\s*(?:{_FL_UNITS})\b)?"
                             rf"|\b(?:hourly|daily|weekly|twice\s+a\s+day)\b", re.I)
    _FL_WORDNUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}
    _FL_DOW = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
               "fri": 4, "sat": 5, "sun": 6}

    @classmethod
    def _fl_month_spec(cls, mon, today, part=None):
        """A month name -> a month or range spec, resolved to the NEXT occurrence.

        "in March" asked in August 2026 means March 2027, not a date four months past. A month that
        is partly elapsed keeps only its remaining days; one fully past rolls to next year.
        """
        mi = cls._FL_MON[mon[:3].lower()]
        year = today.year if (mi > today.month or (mi == today.month)) else today.year + 1
        last = calendar.monthrange(year, mi)[1]
        if not part:
            lo = max(datetime.date(year, mi, 1), today)
            return {"kind": "month", "month": f"{year:04d}-{mi:02d}",
                    "from": lo.isoformat(), "to": datetime.date(year, mi, last).isoformat()}
        p = part.lower()
        if p.startswith(("early", "beginning", "first")):
            a, b = 1, 7
        elif p.startswith(("mid", "second", "third")):
            a, b = 8 if p.startswith(("mid", "second")) else 15, 14 if p.startswith(("mid", "second")) else 21
        else:                                       # late / last week / end of
            a, b = max(1, last - 6), last
        lo = max(datetime.date(year, mi, a), today)
        return {"kind": "range", "month": f"{year:04d}-{mi:02d}",
                "from": lo.isoformat(), "to": datetime.date(year, mi, b).isoformat()}

    @classmethod
    def _fl_find_dates(cls, text, today, bare=False):
        """[spec, ...] in the order they appear. Exact dates first, then ranges, then bare months.

        Each arm removes what it consumed, so "Sep 3 back Sep 10" cannot also be read as a bare
        September, and "first week of September" cannot be re-read as the whole month.

        `bare` adds a final arm for a month name with nothing in front of it, and is passed only
        when the flight form is open and waiting on an answer to "when?". It is off by default
        because "may" and "march" are ordinary words in free text.
        """
        t = text or ""
        found = []

        def take(pat, fn):
            nonlocal t
            out = []
            for m in pat.finditer(t):
                spec = fn(m)
                if spec:
                    out.append((m.start(), spec))
            for m in reversed(list(pat.finditer(t))):
                t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]
            found.extend(out)

        def _exact(y, mo, d):
            try:
                dt = datetime.date(int(y), int(mo), int(d))
            except ValueError:
                return None
            return {"kind": "exact", "date": dt.isoformat()}

        take(cls._FL_ISO, lambda m: _exact(m.group(1), m.group(2), m.group(3)))

        def _monthday(mon, day):
            """A month/day with no year -> the next occurrence of it. 'Dec 20 returning Jan 5' has to
            roll the January over, or the return lands eleven months before the departure."""
            mi = cls._FL_MON[mon[:3].lower()]
            d = int(day)
            y = today.year if (mi, d) >= (today.month, today.day) else today.year + 1
            return _exact(y, mi, d)

        take(cls._FL_MD, lambda m: _monthday(m.group(1), m.group(2)))   # "Sep 15"
        take(cls._FL_DM, lambda m: _monthday(m.group(2), m.group(1)))   # "15 Sep"

        def _dow(m):
            want = cls._FL_DOW.get(m.group(1)[:4].lower(), cls._FL_DOW.get(m.group(1)[:3].lower()))
            if want is None:
                return None
            ahead = (want - today.weekday()) % 7 or 7
            return {"kind": "exact", "date": (today + datetime.timedelta(days=ahead)).isoformat()}
        take(cls._FL_NEXTDOW, _dow)

        def _inn(m):
            n, unit = int(m.group(1)), m.group(2).lower()
            days = n * {"day": 1, "week": 7, "month": 30}[unit]
            return {"kind": "exact", "date": (today + datetime.timedelta(days=days)).isoformat()}
        take(cls._FL_INN, _inn)

        take(cls._FL_PARTMON, lambda m: cls._fl_month_spec(m.group(2), today, part=m.group(1)))
        take(cls._FL_BAREMON, lambda m: cls._fl_month_spec(m.group(1), today))
        # LAST, and only inside the open form: every cued and dated reading above has already been
        # consumed, so this can only pick up a month that nothing else claimed.
        if bare:
            take(cls._FL_MONALONE, lambda m: cls._fl_month_spec(m.group(1), today))
        return [s for _o, s in sorted(found, key=lambda p: p[0])]

    @classmethod
    def _fl_trip_days(cls, text):
        m = cls._FL_TRIPLEN.search(text or "")
        if not m:
            return None
        if m.group(4):                                   # "long weekend"
            return 3
        if m.group(3):                                   # "10 nights"
            return int(m.group(3))
        n = int(m.group(1)) if m.group(1) else cls._FL_WORDNUM.get(
            (m.group(0).split()[1] if len(m.group(0).split()) > 1 else "a"), 1)
        return n * {"day": 1, "night": 1, "week": 7, "month": 30}[m.group(2).lower()]

    def _fl_scan_places(self, low):
        """[(start, end, code, display)] for every airport name in the text, non-overlapping.

        Longest name first so "toronto pearson" beats "toronto", then sorted back into DOCUMENT
        order — which is the part that matters, and the part the first version got wrong.
        """
        hits, taken = [], [False] * len(low)
        for name in sorted(self._IATA, key=len, reverse=True):
            for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
                if any(taken[m.start():m.end()]):
                    continue
                for i in range(m.start(), m.end()):
                    taken[i] = True
                code, disp = self._IATA[name]
                hits.append((m.start(), m.end(), code, disp))
        return sorted(hits)

    def _fl_places(self, text):
        """(origin, dest, unknown_fragments). Either place may be None.

        POSITIONAL, not fragment-matching, and that is the fix for two live bugs. Matching inside a
        "from ..." capture read "from toronto to vancouver" as origin=Vancouver, because the capture
        swallowed the whole tail and longest-name-first found "vancouver" before "toronto". And
        keying only off prepositions missed a bare "toronto to karachi" entirely, since nothing says
        "from". Scanning for every airport and then reading the separators between them handles both,
        and handles "YYZ to YVR" and "montreal to porto" with the same rule.
        """
        pair = self._flight_pair(text)
        if pair:
            a, b = pair
            return ((a, self._CODE_NAME.get(a, a)), (b, self._CODE_NAME.get(b, b)), [])
        low = (text or "").lower()
        hits = self._fl_scan_places(low)
        origin = dest = None
        for st, _en, code, disp in hits:
            pre = low[max(0, st - 8):st]
            if re.search(r"\bfrom\s+$", pre) and not origin:
                origin = (code, disp)
            elif re.search(r"\b(?:to|into)\s+$", pre) and not dest:
                dest = (code, disp)
        # A bare "A to B" with no "from": the separator between two adjacent places IS the direction.
        if len(hits) >= 2 and not (origin and dest):
            for a, b in zip(hits, hits[1:]):
                if re.fullmatch(r"\s*(?:to|-|–|—|→|>|until)\s*", low[a[1]:b[0]]):
                    origin = origin or (a[2], a[3])
                    dest = (b[2], b[3])
                    break
        # A named place that resolved to nothing is worth asking about rather than ignoring.
        unknown = []
        for m in re.finditer(r"\b(?:to|from|into)\s+([a-z][a-z.'\- ]{2,}?)"
                             r"(?=\s*(?:$|[,.]|\b(?:on|in|for|under|below|and|back|returning"
                             r"|departing|leaving|between|around|next|this|by|before|after|to)\b))",
                             low):
            frag = m.group(1).strip(" .,-'")
            if frag and not any(s <= m.start(1) < e for s, e, _c, _d in hits) \
                    and frag not in unknown and len(frag) > 2:
                unknown.append(frag)
        return origin, dest, unknown

    def _flight_slots(self, text, prev=None, today=None, bare=False):
        """The itinerary this turn describes, merged onto any draft already in flight.

        `bare` is passed only when the form is open and the outstanding question is "when?" — see
        _fl_find_dates.
        """
        today = today or datetime.date.today()
        s = dict(prev or {})
        o, d, unknown = self._fl_places(text)
        if o:
            s["origin"] = list(o)
        if d:
            s["dest"] = list(d)
        if unknown:
            s["unknown_places"] = unknown
        dates = self._fl_find_dates(text, today, bare=bare)
        if dates:
            s["depart"] = dates[0]
            if len(dates) > 1:
                s["ret"] = dates[1]
            elif dates[0]["kind"] in ("month", "range") and "ret" not in s:
                # A same-window round trip is overwhelmingly the common case. Defaulted rather than
                # asked, and STATED in the reply so one word corrects it.
                s["ret"] = dict(dates[0])
                s["ret_defaulted"] = True
        if self._FL_ONEWAY.search(text or ""):
            s["one_way"] = True
            s.pop("ret", None)
            s.pop("ret_defaulted", None)
        n = self._fl_trip_days(text)
        if n:
            s["trip_days"] = n
        m = self._FL_TARGET.search(text or "")
        if m:
            s["target"] = float(m.group(1).replace(",", ""))
        # Captured so the schedule is built from the user's words: "check every 15 mins next 2
        # hours" was once dropped on the floor, and the job that eventually appeared ran once a
        # day for a week instead.
        m = self._FL_CADENCE.search(text or "")
        if m:
            s["cadence"] = re.sub(r"\s+", " ", m.group(0)).strip()
        # Tracking intent, sticky across turns: "track ... text me" in turn 1 followed by dates in
        # turn 2 is still a tracking ask. This is what decides search-only vs search-and-watch.
        if self._FL_WATCHY.search(text or ""):
            s["wants_watch"] = True
        # An inline number ("notify me on text at 5145579764") is a slot like any other. Validated
        # by the same rule the phone gate uses; saved only at watch creation, never on parse.
        m = self._FL_PHONE_INLINE.search(text or "")
        if m:
            e164 = self._norm_phone(m.group(0))
            if e164:
                s["phone"] = e164
        if self._FL_SEASON.search(text or "") and not dates:
            s["season_only"] = True
        return s

    @staticmethod
    def _flight_missing(s):
        """Which slots still block an answer. Target is optional; a contact is not needed at all,
        because nothing is scheduled — there is nothing to be notified about."""
        need = []
        if not s.get("origin"):
            need.append("origin")
        if not s.get("dest"):
            need.append("destination")
        if not s.get("depart"):
            need.append("dates")
        return need

    # --- rendering. The slot table is deliberately VISIBLE, and it is also the recovery mechanism:
    # _flight_draft_from_reply reads it back out of the transcript when the in-memory draft is lost
    # to a pipe reload. State the user can see is state that survives, which is the opposite of the
    # HTML-comment approach _marks() records as having failed.
    _FL_TBL_O = re.compile(r"\|\s*From\s*\|\s*\*\*([A-Z]{3})\*\*\s*([^|]*)\|")
    _FL_TBL_D = re.compile(r"\|\s*To\s*\|\s*\*\*([A-Z]{3})\*\*\s*([^|]*)\|")
    _FL_TBL_DEP = re.compile(r"\|\s*Depart\s*\|\s*\*\*([^*]+)\*\*")
    _FL_TBL_RET = re.compile(r"\|\s*Return\s*\|\s*\*\*([^*]+)\*\*")

    @staticmethod
    def _fl_show(spec):
        """A date spec as a person would say it."""
        if not spec:
            return None
        if spec["kind"] == "exact":
            d = datetime.date.fromisoformat(spec["date"])
            return d.strftime("%a %-d %b %Y") if os.name != "nt" else d.strftime("%a %d %b %Y")
        lo = datetime.date.fromisoformat(spec["from"])
        hi = datetime.date.fromisoformat(spec["to"])
        if spec["kind"] == "month":
            return f"any time in {lo.strftime('%B %Y')}"
        return f"{lo.day}–{hi.day} {lo.strftime('%B %Y')}"

    def _gflights_url(self, s):
        """A Google Flights deep link for these slots. Its ?q= form takes natural language, which is
        why a month works here at all — and it is the one place a user can act on this today."""
        o = (s.get("origin") or ["", ""])[0]
        d = (s.get("dest") or ["", ""])[0]
        dep, ret = s.get("depart"), s.get("ret")
        lead = "One way flights" if s.get("one_way") or not ret else "Flights"
        q = f"{lead} from {o} to {d}"
        if dep and dep["kind"] == "exact":
            q += f" on {dep['date']}"
            if ret and ret.get("kind") == "exact":
                q += f" through {ret['date']}"
        elif dep:
            lo = datetime.date.fromisoformat(dep["from"])
            q += f" in {lo.strftime('%B %Y')}"
        return ("https://www.google.com/travel/flights?q="
                + urllib.parse.quote(q) + "&curr=CAD&hl=en-CA")

    def _flight_table(self, s):
        rows = []
        for label, key in (("From", "origin"), ("To", "dest")):
            v = s.get(key)
            rows.append(f"| {label} | **{v[0]}** {v[1]} |" if v
                        else f"| {label} | ❓ *need this* |")
        rows.append(f"| Depart | **{self._fl_show(s.get('depart'))}** |" if s.get("depart")
                    else "| Depart | ❓ *need this* |")
        if s.get("one_way"):
            rows.append("| Return | *one way* |")
        else:
            r = self._fl_show(s.get("ret"))
            rows.append(f"| Return | **{r}**{' *(assumed same window — say so if not)*' if s.get('ret_defaulted') else ''} |"
                        if r else "| Return | ❓ *need this — or say **one way*** |")
        if s.get("target"):
            rows.append(f"| Budget | under **${s['target']:,.0f}** |")
        if s.get("trip_days"):
            rows.append(f"| Trip length | about **{s['trip_days']} days** |")
        return "| | |\n|---|---|\n" + "\n".join(rows)

    def _flight_draft_from_reply(self, messages):
        """Recover a draft from the last assistant turn's visible table, or None."""
        prev = next((m.get("content") or "" for m in reversed(messages or [])
                     if m.get("role") == "assistant"), "")
        if "✈️" not in prev:
            return None
        s = {}
        for pat, key in ((self._FL_TBL_O, "origin"), (self._FL_TBL_D, "dest")):
            m = pat.search(prev)
            if m:
                s[key] = [m.group(1), m.group(2).strip()]
        return s or None

    def _flight_ask(self, s, cid, turns, unreadable=None):
        """Ask for everything still missing, in ONE message. With three or four slots, one-at-a-time
        is four round trips and four chances to lose the thread; most real asks already carry two.

        `unreadable` is the user's previous reply when it looked like an answer this parser could
        not read. Quoting it back matters: without it the form re-renders unchanged and reads as if
        the user had said nothing, which invites them to retype the same words.
        """
        need = self._flight_missing(s)
        if cid:
            self._lru(self._flight_draft, cid, {"t": time.time(), "turns": turns, "slots": s})
        asks, hints = [], []
        if "origin" in need:
            asks.append("**where you're flying from**")
        if "destination" in need:
            asks.append("**where you're going**")
        if "dates" in need:
            asks.append("**when**")
            hints.append("A month like *March* is fine — I'll search the whole thing.")
        unknown = s.get("unknown_places") or []
        note = ("\n\n*" + " ".join(hints) + "*") if hints else ""
        if unknown:
            note = (f"\n\nI don't know **{unknown[0]}** as an airport — give me a bigger nearby city "
                    f"or its 3-letter code.")
        if s.get("season_only"):
            note += ("\n\nAlso, *“the fall”* is three months and no site can search all of it at "
                     "once — pick a month and I'll search the whole thing.")
        lead = "✈️ **Flight search**"
        if unreadable:
            lead = (f"✈️ **I couldn't read the dates in “{self._md_cell(unreadable, 60)}”** — that's "
                    f"my parser's fault, not yours.")
            note += ("\n\nThese all work: **October**, *in October*, *Oct 15*, "
                     "*Oct 15 returning Nov 3*, *first week of October*.")
        return (f"{lead}\n\n{self._flight_table(s)}\n\n"
                f"Tell me {' and '.join(asks)}, and I'll pull it up.{note}\n\n"
                f"*Say “never mind” to drop this.*")

    # "Yes, do that" said to the answer above. Anchored affirmatives plus explicit set-it-up verbs.
    _FL_SETALERT = re.compile(
        r"^\s*(?:yes|yeah|yep|yup|ok(?:ay)?|sure|please|do\s+it|go\s+ahead|sounds?\s+good)\b"
        r"|\bset\s+(?:it\s+|the\s+|an?\s+)*alert"
        r"|\b(?:set|create|make|add|start|schedule)\s+(?:it|the|an?|this|that)?\s*"
        r"(?:up\b|alert|watch|monitor|tracker)"
        r"|\b(?:track|watch|monitor)\s+(?:it|this|that)\b", re.I)

    # "Build it anyway." Checked BEFORE the plain affirmative, because "yes, schedule it anyway"
    # contains both and the more specific reading is the one the user typed.
    _FL_ANYWAY = re.compile(r"\banyway\b|\bregardless\b|\beven\s+(?:so|if|though)\b"
                            r"|\b(?:create|make|schedule|build|set\s+up)\s+(?:me\s+)?"
                            r"(?:the|a|it)?\s*(?:hermes\s+)?(?:cron\s+)?job\b"
                            r"|\bjust\s+(?:do|make|create|schedule)\s+it\b", re.I)
    _FL_UNIT_MIN = {"m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
                    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
                    "d": 1440, "day": 1440, "days": 1440, "w": 10080, "week": 10080, "weeks": 10080}
    _FL_CAD_PARTS = re.compile(r"every\s+(\d{1,4})\s*([a-z]+)"
                               r"(?:\s*,?\s*(?:for\s+|over\s+)?(?:the\s+)?(?:next\s+)"
                               r"(\d{1,3})\s*([a-z]+))?", re.I)

    @classmethod
    def _fl_horizon_days(cls, s, today=None):
        """Days from today until this itinerary's departure stops mattering.

        Exact date -> that date; a month/range window -> the window's END, because a fare for
        "any time in October" is still worth watching until October is over. Clamped to [1, 120]:
        a passed date still gets one run (which will say so), and a far-future trip does not
        become a four-year job.
        """
        today = today or datetime.date.today()
        dep = s.get("depart") or {}
        iso = dep.get("date") or dep.get("to")
        if not iso:
            return 14
        try:
            days = (datetime.date.fromisoformat(iso) - today).days
        except Exception:
            return 14
        return max(1, min(120, days))

    @classmethod
    def _fl_schedule(cls, cadence, horizon_days=14):
        """('every 15m', 8) for "every 15 mins next 2 hours"; default = daily until departure.

        Deterministic, and that is the whole point: the ONE time a schedule was left to the agent
        it read "every 15 mins next 2 hours" as every 1440m for 7 days (job e6df1739a275). The
        user's own words are already parsed here, so nothing needs to infer them again.

        `horizon_days` bounds every UNBOUNDED cadence — the user's own bound ("next 2 hours")
        always wins, but "every 6 hours" with no end now runs until the departure passes rather
        than for an arbitrary week, and no cadence at all means one check a day until then
        (the user's chosen default, 2026-08-09). Capped at FLIGHTCLAW_WATCH_MAX_RUNS so
        "every 15 mins" against a trip four months out cannot become five thousand runs.
        """
        def until(mins):
            return max(1, min(FLIGHTCLAW_WATCH_MAX_RUNS, (horizon_days * 1440) // mins))
        # _FL_CADENCE captures these, so _fl_schedule has to understand them or the confirmation
        # says "hourly" and the job runs six-hourly — the same class of mismatch this exists to fix.
        word = {"hourly": ("every 1h", 60), "daily": ("every 1d", 1440),
                "weekly": ("every 7d", 10080), "twice a day": ("every 12h", 720)}.get(
            re.sub(r"\s+", " ", (cadence or "").strip().lower()))
        if word:
            return word[0], until(word[1]), False
        m = cls._FL_CAD_PARTS.search(cadence or "")
        if not m:
            return "every 1d", until(1440), True
        n, unit = int(m.group(1)), (m.group(2) or "").lower()
        per = cls._FL_UNIT_MIN.get(unit)
        if not per or n < 1:
            return "every 1d", until(1440), True
        mins = max(1, n * per)
        sched = (f"every {mins}m" if mins < 60 else
                 f"every {mins // 60}h" if mins < 1440 and mins % 60 == 0 else
                 f"every {mins // 1440}d" if mins % 1440 == 0 else f"every {mins}m")
        repeat = None
        if m.group(3) and m.group(4):
            bound = int(m.group(3)) * (cls._FL_UNIT_MIN.get((m.group(4) or "").lower()) or 0)
            if bound:
                repeat = max(1, bound // mins)
        return sched, (repeat if repeat else until(mins)), False

    # ---------------------------------------------------------------- FlightClaw client
    #
    # The fare engine (docs/FLIGHTCLAW.md): an always-on MCP server on the host loopback, speaking
    # MCP streamable HTTP. Three POSTs per call (initialize -> initialized -> tools/call) — worth
    # it for statelessness: no session to leak across chat turns, nothing to reconnect after the
    # service restarts. Responses arrive as SSE data: lines; the last one is the answer.

    @staticmethod
    async def _fc_rpc(http, method, params, session_id=None, rpc_id=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if session_id:
            headers["mcp-session-id"] = session_id
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if rpc_id is not None:
            body["id"] = rpc_id
        async with http.post(FLIGHTCLAW_URL, json=body, headers=headers) as r:
            sid = r.headers.get("mcp-session-id") or session_id
            ctype = r.headers.get("content-type", "")
            raw = await r.text()
        if "text/event-stream" in ctype:
            msgs = [json.loads(ln[5:].strip()) for ln in raw.splitlines()
                    if ln.startswith("data:")]
            return (msgs[-1] if msgs else None), sid
        return (json.loads(raw) if raw.strip() else None), sid

    async def _fc_call(self, tool, arguments, timeout=FLIGHTCLAW_SEARCH_TIMEOUT_S):
        """One FlightClaw tool call. Returns the tool's text; raises on any failure —
        every caller turns that into an honest sentence, never into a fabricated fare."""
        tmo = aiohttp.ClientTimeout(total=timeout + 20, sock_connect=5, sock_read=timeout)
        async with aiohttp.ClientSession(timeout=tmo) as http:
            msg, sid = await self._fc_rpc(http, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "auto_assistant", "version": "1.0"}}, rpc_id=1)
            await self._fc_rpc(http, "notifications/initialized", {}, session_id=sid)
            msg, _ = await self._fc_rpc(http, "tools/call",
                                        {"name": tool, "arguments": arguments},
                                        session_id=sid, rpc_id=2)
        if msg and msg.get("error"):
            raise RuntimeError(str(msg["error"])[:200])
        content = ((msg or {}).get("result") or {}).get("content") or []
        if not content:
            raise RuntimeError("empty MCP response")
        return content[0].get("text", "")

    # The slot parser deliberately reads bare city names as METRO codes ("toronto" -> YTO, any
    # Toronto airport) and specific names as airports ("pearson" -> YYZ). Google's q= text search
    # accepts both; fli's Airport enum accepts only real airports — measured 2026-08-09, 15 of the
    # 129 codes in _IATA are metro codes fli rejects. Translated at the ENGINE boundary, not in
    # the table, so the user-facing semantics ("any Toronto airport") stay intact everywhere else.
    # Every target verified present in fli's enum the same day.
    _FC_METRO = {"YTO": "YYZ", "NYC": "JFK", "LON": "LHR", "PAR": "CDG", "TYO": "NRT",
                 "OSA": "KIX", "SEL": "ICN", "BJS": "PEK", "CHI": "ORD", "WAS": "IAD",
                 "MIL": "MXP", "ROM": "FCO", "STO": "ARN", "RIO": "GIG", "BUE": "EZE"}

    @classmethod
    def _fc_code(cls, code):
        return cls._FC_METRO.get(code, code)

    # search_dates emits "  2026-10-14 -> 2026-11-14: C$322" (round trip) or
    # "  2026-10-15: C$338" (one way), sorted cheapest-first by flightclaw itself.
    _FC_DATE_LINE = re.compile(r"^\s{2}(\d{4}-\d{2}-\d{2})"
                               r"(?:\s*->\s*(\d{4}-\d{2}-\d{2}))?:\s*(\S.*)$", re.M)
    _FC_BOOK_LINE = re.compile(r"^\s*Book:\s*(https://\S+)\s*$", re.M)
    _FC_NO_FARES = re.compile(r"\bNo (?:prices|flights) found\b", re.I)

    def _fc_search_plan(self, s):
        """(tool, arguments) for these slots — the deterministic slots→API mapping.

        Exact dates -> search_flights (real options with airlines and booking links). A month or
        range window -> search_dates (one calendar-grid query for the whole window), with the trip
        length taken from the user's own words or, failing that, the gap between the two windows —
        stated in the reply, because the chosen dates are then dates the user never typed.
        """
        o, d = self._fc_code(s["origin"][0]), self._fc_code(s["dest"][0])
        dep, ret = s.get("depart") or {}, s.get("ret") or {}
        if dep.get("kind") == "exact":
            # sort_by=CHEAPEST, not FlightClaw's own default (BEST — Google's relevance ranking,
            # which favours shorter/more-convenient itineraries over price). Left at the default,
            # the options shown here could all be pricier than the fare search_dates just called
            # "the cheapest in your window" one turn earlier — measured live: BEST showed a
            # direct $873 option while the $703 fare search_dates found (a one-stop TAP Air
            # Portugal routing) never appeared in the top 3 at all. The whole point of a fare
            # WATCH is price, not convenience, so cheapest-first is correct here regardless of
            # whether this call follows a date-resolution step or the user gave exact dates
            # directly.
            args = {"origin": o, "destination": d, "date": dep["date"], "results": 3,
                    "sort_by": "CHEAPEST"}
            if not s.get("one_way") and ret.get("kind") == "exact":
                args["return_date"] = ret["date"]
            return "search_flights", args
        args = {"origin": o, "destination": d,
                "from_date": dep.get("from"), "to_date": dep.get("to")}
        if not s.get("one_way"):
            if s.get("trip_days"):
                args["trip_duration"] = int(s["trip_days"])
            elif ret.get("from") and dep.get("from"):
                gap = (datetime.date.fromisoformat(ret["from"])
                       - datetime.date.fromisoformat(dep["from"])).days
                if gap > 0:
                    args["trip_duration"] = gap
        return "search_dates", args

    async def _fc_resolve_dates(self, s):
        """Month/range windows -> the cheapest CONCRETE dates in them, via one search_dates call.

        Returns (s, note) where s carries exact-date specs a fare can bind to. The note names the
        chosen dates as CHOSEN — a fare for "any time in October" is really a fare for the pair
        this found, and saying so is what lets one word correct it. Raises on an unreachable
        engine or an empty grid; callers say so rather than schedule on a guess.
        """
        dep = s.get("depart") or {}
        if dep.get("kind") == "exact":
            return s, ""
        tool, args = self._fc_search_plan(s)
        text = await self._fc_call(tool, args)
        if self._FC_NO_FARES.search(text):
            raise RuntimeError(f"no fares found between {args.get('from_date')} and "
                               f"{args.get('to_date')}")
        m = self._FC_DATE_LINE.search(text)
        if not m:
            raise RuntimeError("the date grid came back in a shape I could not read")
        s = dict(s)
        s["depart"] = {"kind": "exact", "date": m.group(1)}
        if m.group(2):
            s["ret"] = {"kind": "exact", "date": m.group(2)}
            s.pop("ret_defaulted", None)
        note = (f"**{m.group(1)}"
                + (f" → {m.group(2)}" if m.group(2) else "")
                + f"** was the cheapest in your window ({m.group(3).strip()}) — these are chosen "
                  f"dates, not typed ones; give exact dates to pin different ones.")
        return s, note

    def _fc_render_options(self, text, limit=3):
        """search_flights output as chat markdown: fares in a code block (its alignment is the
        formatting), booking links pulled out underneath as real links — a 500-char tfs URL
        inline is unreadable, and the link is the single most useful thing on the page.

        Split on the "Option N:" boundaries themselves, not on ruler lines: the CLI prints
        ===== rulers between options and the MCP tool prints none (measured live 2026-08-09 —
        splitting on rulers rendered an empty block against the real service). Rulers, where
        present, are stripped first so both shapes parse identically.
        """
        text = re.sub(r"^\s*={10,}\s*$", "", text or "", flags=re.M)
        blocks = [b.strip() for b in re.split(r"\n(?=Option \d)", text) if b.strip()]
        head = blocks[0] if blocks and not blocks[0].startswith("Option") else ""
        options = [b for b in blocks if b.startswith("Option")][:limit]
        links = []
        shown = []
        for b in options:
            urls = self._FC_BOOK_LINE.findall(b)
            if urls:
                links.append(urls[0])
            shown.append(self._FC_BOOK_LINE.sub("", b).strip())
        out = "```\n" + (head + "\n\n" if head else "") + "\n\n".join(shown) + "\n```"
        if links:
            out += "\n" + " · ".join(f"[Book option {i + 1}]({u})"
                                      for i, u in enumerate(links))
        return out

    # ---------------------------------------------------------------- the watch, one turn
    @classmethod
    def _fc_route_id(cls, s):
        """FlightClaw's own id formula (tracking.py:63) — duplicated so the cron command can be
        built without a second round trip, pinned by tests on both sides."""
        rid = (f"{cls._fc_code(s['origin'][0])}-{cls._fc_code(s['dest'][0])}"
               f"-{s['depart']['date']}")
        if not s.get("one_way") and (s.get("ret") or {}).get("date"):
            rid += f"-RT-{s['ret']['date']}"
        return rid

    def _fc_watch_cmd(self, route_id, state, handle, name, sched, target, origin_name=None,
                      dest_name=None):
        """The vetted command a hermes job runs. Every value parsed, none inferred — the two fare
        jobs an agent authored carried no dates at all and a --monitor whose spaces broke argparse.

        origin_name/dest_name ride along as their own flags rather than making the watcher
        re-derive a city from the code: this pipe already resolved "YOW" -> "Ottawa" once, at the
        moment the slots were parsed, and flightclaw_watch.py has no code->name table of its own
        to keep in sync with this one — the vetted command is the one place both sides agree.
        """
        import shlex
        args = ["--route-id", route_id, "--state", state, "--alert-to", handle]
        if target:
            args += ["--below", f"{target:.0f}"]
        if origin_name:
            args += ["--origin-name", origin_name]
        if dest_name:
            args += ["--dest-name", dest_name]
        args += ["--monitor", name, "--schedule", sched]
        return ("Run this terminal command and print its output verbatim as your entire response. "
                "Add nothing.\n"
                + "python3 /home/ohmz/ai-stack/scripts/flightclaw_watch.py "
                + " ".join(shlex.quote(a) for a in args))

    async def _fc_make_watch(self, s, cid, handle="user"):
        """Create the whole watch in one turn: FlightClaw tracks the fare, hermes schedules the
        checks, and the confirmation states everything it just did — schedule, horizon, channels.

        Retry-safe by construction: track_flight is idempotent on FlightClaw's side ("Already
        tracking"), so a failed job POST can be retried with the same words and nothing doubles.
        """
        try:
            s, note = await self._fc_resolve_dates(s)
        except Exception as e:
            return (f"⚠️ **Nothing was scheduled.** I could not pin dates inside your window — "
                    f"{str(e)[:160]}. Say *track it* to retry, or give exact dates.")
        # An inline number ("text me at 514...") is saved BEFORE the watch exists, so the first
        # alert has somewhere to go. Never overwrites a number already on file.
        if s.get("phone") and not (self._contact(handle) or {}).get("phone"):
            self._save_phone(handle, s["phone"])
        # Display names, not codes — s['origin']/s['dest'] are [code, name] pairs the slot parser
        # already resolved ("YOW" -> "Ottawa"), and this string becomes the job's own title AND
        # the subscribed-confirmation headline. _flight_table and the live-search banner already
        # show the name; this was the one place in the file still showing the code instead,
        # producing confirmations that read "YTO→YOW fare watch" rather than "Toronto → Ottawa".
        name = (f"{s['origin'][1]} → {s['dest'][1]} fare watch"
                + (f" under ${s['target']:,.0f}" if s.get("target") else ""))[:80]
        route_id = self._fc_route_id(s)
        track_args = {"origin": self._fc_code(s["origin"][0]),
                      "destination": self._fc_code(s["dest"][0]),
                      "date": s["depart"]["date"]}
        if not s.get("one_way") and (s.get("ret") or {}).get("date"):
            track_args["return_date"] = s["ret"]["date"]
        if s.get("target"):
            track_args["target_price"] = float(s["target"])
        try:
            tracked_reply = await self._fc_call("track_flight", track_args)
        except Exception as e:
            return (f"⚠️ **Nothing was scheduled.** The fare engine did not accept the tracking "
                    f"request ({str(e)[:140]}). Say *track it* to retry.")
        horizon = self._fl_horizon_days(s)
        sched, repeat, defaulted = self._fl_schedule(s.get("cadence"), horizon_days=horizon)
        slug = re.sub(r"[^a-z0-9]+", "-", f"fc-{route_id}".lower()).strip("-")
        prompt = self._fc_watch_cmd(route_id, slug, handle, name, sched, s.get("target"),
                                    origin_name=s["origin"][1], dest_name=s["dest"][1])
        status, data, err = await asyncio.to_thread(
            self._hermes_api, "POST", "/api/jobs",
            {"name": name, "schedule": sched, "prompt": prompt, "deliver": "local",
             "repeat": repeat})
        if err:
            reason = self._api_err_text(data) or err
            self._route_metric("flight.job_failed", 0, "flight_watch_error", reason)
            return (f"⚠️ FlightClaw is now tracking **{route_id}** ({self._md_cell(tracked_reply, 60)}), "
                    f"but the check schedule failed: `{reason}`. No checks will run until it "
                    f"exists — say *track it* to retry (nothing will double).")
        job = (data or {}).get("job") or {}
        jid = job.get("id", "?")
        if cid:
            self._flight_draft.pop(cid, None)
        self._stamp_owner([jid], handle, src="flight")
        self._route_metric("flight.watch_created", 0, "flight_watch", "", deterministic=True)
        if jid != "?":
            # Everything a confirmation needs is already a local variable here — no prompt to
            # parse, unlike Path A. depart_found/ret_found reuse the SAME fields a fare alert's
            # dates panel reads (alert_templates._details_html): the itinerary the user asked
            # for. Deliberately no "source" — that field means a fare was FOUND on those dates,
            # and nothing has been searched for yet.
            #
            # Deliberately no target/unit either, even when s['target'] is set: `name` is built
            # a few lines up as "{origin}->{dest} fare watch under ${target:,.0f}" whenever a
            # target exists, so the confirmation sentence restating it is not extra information —
            # it is the same three words twice, at the cost of the item name itself. A short-lived
            # regression: adding it here pushed a 137-char SMS to 160+ and truncated the ROUTE to
            # make room for a number the route already carried.
            sub_payload = {"kind": "subscribed", "item": name, "monitor": name, "schedule": sched,
                           "depart_found": s["depart"]["date"]}
            if not s.get("one_way") and (s.get("ret") or {}).get("date"):
                sub_payload["ret_found"] = s["ret"]["date"]
            self._enqueue_subscription(handle, jid, sub_payload)
        # The baseline FlightClaw just recorded ("Tracking YYZ-...: C$338 (F8)") is the first
        # real number of this watch — show it.
        base = self._md_cell(tracked_reply.splitlines()[0] if tracked_reply else "", 90)
        cad = (f"\n\n*You gave no cadence, so this checks once a day until departure — say a "
               f"different one and I'll change it.*" if defaulted else "")
        return (
            f"✅ **Watching it** — job `{jid}`.\n\n{self._flight_table(s)}\n\n"
            + (f"{note}\n\n" if note else "")
            + f"| | |\n|---|---|\n"
            f"| Baseline | {base or '*first check pending*'} |\n"
            f"| Checks | **{sched}**, {repeat} runs (~{horizon} days) |\n"
            f"| First check | **{self._when(job.get('next_run_at')) or 'shortly'}** |\n"
            + (f"| Alert when | under **${s['target']:,.0f}** |\n" if s.get("target") else "")
            + f"\nEvery check logs the real price to **background-tasks**; you get a text and an "
            f"email only when it crosses your line, and never the same price twice."
            f"{self._alert_setup_block(handle)}\n\n"
            f"Say *cancel {jid}* to stop it, or *list my tasks* to see it."
            f"{cad}")

    def _flight_after_answer(self, text, s, cid, handle="user"):
        """The turn AFTER a completed flight answer: "track it" builds the watch, anything else
        routes normally. Exists because "yes so set alert" once fell through to the agent and
        became a job that ran daily, watched nothing, and could not parse (e6df1739a275)."""
        if self._FL_ANYWAY.search(text or "") or self._FL_SETALERT.search(text or ""):
            self._route_metric("flight.watch_confirm", 0, "flight_setalert", text,
                               deterministic=True)

            async def go():
                yield await self._fc_make_watch(s, cid, handle)
            return go()
        return None

    async def _flight_answer_stream(self, s, cid, handle="user"):
        """The answer, with real fares in it: live search, and — when the ask carried tracking
        intent — the watch created in the same turn. One message in, everything running.

        The refusal that used to live here was right about its facts (19 scrapeable sites, 0
        readable — docs/FLIGHT_RECON.md) and is obsolete about the conclusion: FlightClaw reads
        Google Flights' protobuf API, a surface the recon never measured, and returned C$348 from
        this host on the first try (docs/FLIGHTCLAW.md). What survives from the old discipline is
        its one law: no number in this reply is ever invented — a failed search says it failed.
        """
        yield f"✈️ **{s['origin'][1]} → {s['dest'][1]}** — checking live fares…\n\n"
        note = ""
        try:
            dep = s.get("depart") or {}
            if dep.get("kind") == "exact":
                tool, args = self._fc_search_plan(s)
                fares = self._fc_render_options(await self._fc_call(tool, args))
            else:
                s, note = await self._fc_resolve_dates(s)
                tool, args = self._fc_search_plan(s)     # now exact: real options on the pick
                fares = self._fc_render_options(await self._fc_call(tool, args))
        except Exception as e:
            # The draft survives NOT answered: "try again" re-searches instead of trying to build
            # a watch on dates that were never pinned.
            if cid:
                self._lru(self._flight_draft, cid,
                          {"t": time.time(), "turns": 0, "slots": s})
            self._route_metric("flight.search_failed", 0, "flight_engine_error", str(e)[:120])
            yield (f"⚠️ **I couldn't reach live fares** ({str(e)[:140]}). Nothing was scheduled "
                   f"and no number was made up. Try again in a moment — or "
                   f"**[open the search yourself]({self._gflights_url(s)})**.")
            return
        if self._FC_NO_FARES.search(fares):
            if cid:
                self._lru(self._flight_draft, cid,
                          {"t": time.time(), "turns": 0, "slots": s, "answered": True})
            yield (f"{self._flight_table(s)}\n\nGoogle returned **no fares** for this itinerary "
                   f"right now — that happens on far-out or thin routes. "
                   f"**[Check it yourself]({self._gflights_url(s)})**, or try different dates.")
            return
        yield f"{self._flight_table(s)}\n\n" + (f"{note}\n\n" if note else "") + fares + "\n\n"
        if s.get("wants_watch"):
            yield await self._fc_make_watch(s, cid, handle)
            return
        # A pure search. The draft is kept, marked answered, so "track it" works — the gap that
        # once sent that phrase to the scheduler.
        if cid:
            self._lru(self._flight_draft, cid,
                      {"t": time.time(), "turns": 0, "slots": s, "answered": True})
        tgt = f" and text you under **${s['target']:,.0f}**" if s.get("target") else ""
        yield (f"Say **track it** and I'll watch this fare{tgt} — checks on your schedule, "
               f"alerts by text and email. Or *different dates* to change the search.")

    _FL_ABANDON = re.compile(r"^\s*(?:never\s?mind|nvm|forget\s+it|cancel\s+that|drop\s+it|stop"
                             r"|no\s+thanks?|not\s+now|leave\s+it)\b", re.I)
    # "You were trying to give me dates and I could not read them." Deliberately generous — the cost
    # of a false positive is one re-ask inside a form the user is already in, and the cost of a false
    # negative is the turn escaping to a model that invents an itinerary.
    _FL_TRIED_DATES = re.compile(rf"\b(?:{_FL_MONWORD})\b|\d"
                                 r"|\b(?:day|days|week|weeks|month|months|night|nights|weekend"
                                 r"|tomorrow|today|soon|whenever|flexible|anytime|any\s+time)\b",
                                 re.I)

    def _flight_turn(self, cid, text, messages, resume=False, handle="user"):
        """The only method pipe() calls. Returns a reply, or None to route normally.

        Returning None on anything that is not an answer is what keeps a false positive cheap: it
        costs one question and the conversation carries on. Same contract as _phone_reply (:1425).
        """
        draft = self._flight_draft.get(cid) if cid else None
        if draft and (time.time() - draft.get("t", 0)) > FLIGHT_DRAFT_TTL_S:
            self._flight_draft.pop(cid, None)
            draft = None
        prev = (draft or {}).get("slots") or (self._flight_draft_from_reply(messages)
                                             if resume else None)
        turns = (draft or {}).get("turns", 0)

        if resume:
            if self._FL_ABANDON.match(text or ""):
                self._flight_draft.pop(cid, None)
                self._route_metric("flight.abandoned", 0, "flight_abandon", text)
                return self._say("No problem — dropped.")
            if prev is None:
                return None
            # An answered itinerary: "yes, set it up" is about THAT, not a new slot to merge.
            # Checked before the merge so an affirmative cannot be read as an empty answer and
            # dropped into the scheduler.
            if (draft or {}).get("answered"):
                done = self._flight_after_answer(text, prev, cid, handle)
                if done is not None:
                    return done
            # Bare months are readable HERE and nowhere else: the form asked "when?", so a lone
            # "October" is an answer rather than a modal verb. After an ANSWER there is no
            # outstanding question, so a bare month is only taken alongside a change cue —
            # "actually make it december" is a correction the answer invited ("Want a different
            # route or dates? Just say so"), while "may i ask something" is not a date at all.
            answered = bool((draft or {}).get("answered"))
            bare = ("depart" not in prev) or (answered and bool(self._FL_CHANGE.search(text or "")))
            slots = self._flight_slots(text, prev=prev, bare=bare)
            # A reply that fills nothing and answers nothing is the user moving on, not an answer.
            if slots == prev and not self._is_flight_request(text)[0]:
                # ...unless it was plainly an ATTEMPT at dates that no arm could read. Falling
                # through then is what produced the 2026-08-09 report: "leaving October and
                # returning nov" parsed to nothing, the turn reached the agent as a background
                # followup, and the agent answered about Astana and Amazon.ca. A reply carrying a
                # month, a digit or a duration word is the user answering the question — so say it
                # could not be read, and ask again in the same breath. Anything else still returns
                # None, because the user moving on must stay cheap.
                # Only while the form is genuinely OPEN. After an answer nothing is outstanding, so
                # an unreadable message is the user moving on, not a failed attempt at dates —
                # re-asking there would trap them with "I couldn't read the dates" for a sentence
                # that was never about dates.
                if not answered and self._FL_TRIED_DATES.search(text or ""):
                    self._route_metric("flight.slots", 0, "flight_dates_unparsed", text,
                                       n_missing=len(self._flight_missing(prev)))
                    return self._say(self._flight_ask(prev, cid, turns + 1, unreadable=text))
                return None
        else:
            slots = self._flight_slots(text, prev=None)

        need = self._flight_missing(slots)
        if not need:
            self._route_metric("flight.answer", 0 if resume else 1,
                               "flight_form_complete" if resume else "flight_strong", text,
                               deterministic=True)
            return self._flight_answer_stream(slots, cid, handle)
        turns += 1
        if turns > FLIGHT_MAX_TURNS:
            self._flight_draft.pop(cid, None)
            self._route_metric("flight.abandoned", 0, "flight_max_turns", text)
            return self._say(f"I still need {' and '.join(need)} — let's start over when you have "
                             f"them, or search directly on Google Flights.")
        self._route_metric("flight.slots", 0 if resume else 1,
                           "flight_form_reply" if resume else "flight_strong", text,
                           n_missing=len(need), missing=",".join(need), turns=turns)
        return self._say(self._flight_ask(slots, cid, turns))

    @staticmethod
    def _norm_phone(raw, default_country="+1"):
        """-> E.164 or None. Mirrors scripts/alert_transports.normalize_phone; the pipe runs in a
        container and cannot import it, so the rule is duplicated and pinned by tests on both
        sides — a number accepted here and rejected there would fail silently at 3am."""
        if not raw:
            return None
        s = re.sub(r"[^\d+]", "", str(raw))
        digits = re.sub(r"\D", "", s)
        if s.startswith("+"):
            return "+" + digits if 8 <= len(digits) <= 15 else None
        if len(digits) == 10:
            return f"{default_country}{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return None

    @staticmethod
    def _read_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default

    def _contact(self, handle):
        return (self._read_json(ALERT_CONTACTS_FILE, {}) or {}).get(handle) or {}

    @staticmethod
    def _owui_email(handle):
        """The OpenWebUI address whose local part matches this handle.

        Mirrors alert_transports.owui_email, because the delivery side resolves email that way and
        this side must agree. It did not: the confirmation block read only the contacts file and
        told a user with a perfectly good address "no address on file", which reads as "your email
        alerts will not work" about a setup that works fine.
        """
        try:
            db = sqlite3.connect(f"file:{OWUI_DB}?mode=ro", uri=True)
            rows = db.execute("select u.email from user u join auth a on a.id=u.id "
                              "where a.active=1").fetchall()
            db.close()
        except Exception:
            return None
        for (email,) in rows:
            if re.sub(r"[^a-z0-9_-]", "", (email or "").split("@")[0].lower()) == handle:
                return email
        return None

    def _alert_email(self, handle):
        """Where an alert to this handle would actually go — contacts override, OWUI otherwise."""
        return self._contact(handle).get("email") or self._owui_email(handle)

    def _save_phone(self, handle, e164):
        """Persist a number for this handle. Returns True on success.

        Written atomically to the shared file so the host-side transports see it on the very next
        delivery tick — the user should not have to re-schedule the task they just asked for."""
        contacts = self._read_json(ALERT_CONTACTS_FILE, {}) or {}
        entry = dict(contacts.get(handle) or {})
        entry["phone"] = e164
        contacts[handle] = entry
        try:
            tmp = ALERT_CONTACTS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(contacts, f, indent=2)
            os.replace(tmp, ALERT_CONTACTS_FILE)
            return True
        except Exception:
            return False

    # ---------- job ownership ----------
    @staticmethod
    def _read_owners():
        """(owners, err). err is None when the map was read OR is simply absent.

        Deliberately NOT _read_json: that collapses "no file yet" and "file is corrupt" into the
        same empty default, and those must behave differently. An absent map is the ordinary first
        run. A CORRUPT one means ownership is unknown, and a caller that treats unknown as empty
        would tell a user they have no tasks while their monitors keep running — the exact lie the
        rest of this file works to avoid.
        """
        try:
            with open(TASK_OWNERS_FILE) as f:
                d = json.load(f)
            return (d, None) if isinstance(d, dict) else ({}, "unreadable")
        except FileNotFoundError:
            return {}, None
        except Exception:
            return {}, "unreadable"

    @staticmethod
    def _write_owners(owners):
        """Atomic replace, same shape as _save_phone: the host-side delivery watcher reads this
        file on its own schedule and must never see a half-written map."""
        try:
            os.makedirs(os.path.dirname(TASK_OWNERS_FILE), exist_ok=True)
            tmp = TASK_OWNERS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(owners, f, indent=2)
            os.replace(tmp, TASK_OWNERS_FILE)
            return True
        except Exception:
            return False

    @staticmethod
    def _prune_owners(owners, live_ids):
        """Drop records for jobs the scheduler no longer has, once they are old enough that
        delivery has certainly finished with them. Returns the number dropped."""
        now, dead = time.time(), []
        for jid, rec in owners.items():
            if jid not in live_ids and now - float((rec or {}).get("t") or 0) > OWNER_PRUNE_S:
                dead.append(jid)
        for jid in dead:
            owners.pop(jid, None)
        return len(dead)

    def _stamp_owner(self, ids, handle, src="diff", live_ids=None):
        """Record `handle` as the owner of each job id. Never raises.

        MUST stay synchronous and await-free. The pipe runs on one event loop, so a
        read-merge-write with no await inside it cannot interleave with another turn's stamp; move
        any part of this to a thread and concurrent creations start losing each other's records.

        A corrupt map is quarantined rather than merged into: the alternative is refusing to stamp,
        which would leave the job the user just created invisible to them — compounding one failure
        with a second.
        """
        ids = [i for i in (ids or []) if i]
        if not ids:
            return 0
        owners, err = self._read_owners()
        if err:
            try:
                os.replace(TASK_OWNERS_FILE, TASK_OWNERS_FILE + ".corrupt")
            except Exception:
                pass
            owners = {}
            self._metric(job="owner", outcome="corrupt_reset", n=len(ids))
        now = time.time()
        for jid in ids:
            owners[jid] = {"h": handle, "t": now, "src": src}
        if live_ids is not None:
            self._prune_owners(owners, live_ids)
        ok = self._write_owners(owners)
        self._metric(job="owner", outcome="stamped" if ok else "stamp_failed",
                     n=len(ids), src=src, handle=handle)
        return len(ids) if ok else 0

    def _owner_of(self, owners, job):
        return ((owners or {}).get((job or {}).get("id")) or {}).get("h")

    def _enqueue_subscription(self, handle, job_id, payload):
        """Leave a note asking the host to confirm a new monitor by email and text. Never raises.

        The container holds no SMTP/Twilio credentials — see SUBSCRIBE_INBOX_FILE — so the pipe
        cannot send this itself. It can only append to the shared inbox and let
        scripts/hermes_delivery.py pick it up on its next tick (at most 60s later) and hand the
        payload to alert_transports.send_alert exactly the way a price-drop alert is sent, so a
        subscription confirmation gets the SAME cancel link, the same brand template, the same
        retry-on-failure — for free, by looking like one more alert rather than a special case.

        MUST stay synchronous and await-free, exactly like _stamp_owner: the pipe runs on one
        event loop, so a read-append-write with no await inside it cannot interleave with
        ANOTHER TURN's enqueue. That does not cover the other writer, though — the host-side
        watcher (scripts/hermes_delivery.py) reads this same file and clears it every ~60s in a
        SEPARATE process, so an append landing between its read and its clear-write would be
        silently discarded. An flock on a sidecar file closes that window on both sides; the
        watcher takes the identical lock before its own read-modify-write.
        """
        try:
            # dict(payload, to=handle) raises on a non-dict payload — inside the guard, not
            # before it, so "never raises" is actually true rather than true of everything past
            # the first line.
            entry = {"handle": handle, "job_id": job_id, "payload": dict(payload, to=handle),
                     "enqueued_at": time.time()}
            os.makedirs(os.path.dirname(SUBSCRIBE_INBOX_FILE), exist_ok=True)
            with open(SUBSCRIBE_INBOX_FILE + ".lock", "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                pending = self._read_json(SUBSCRIBE_INBOX_FILE, [])
                if not isinstance(pending, list):
                    pending = []
                pending.append(entry)
                tmp = SUBSCRIBE_INBOX_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(pending, f, indent=2)
                os.replace(tmp, SUBSCRIBE_INBOX_FILE)
            self._metric(job="subscribe", outcome="enqueued", handle=handle, job_id=job_id)
        except Exception as e:
            # A failed confirmation must never cost the user the monitor itself — the job already
            # exists in Hermes by the time this runs. Log and move on.
            self._metric(job="subscribe", outcome="enqueue_failed", handle=handle,
                         job_id=job_id, error=str(e)[:160])

    def _enqueue_subscriptions_for(self, jobs, handle):
        """Queue one confirmation per newly-created job, Path A's side of the split with
        _fc_make_watch (Path B, which already holds structured slots and enqueues inline).

        Unlike Path B, nothing here already knows what the job watches — the agent wrote the
        prompt, not this pipe. Best-effort enrichment from the job's own vetted command line; a
        value not found is simply left out, per alert_templates' own rule that every field here
        is optional. Pulled out as its own method (rather than left inline in the streaming
        generator that calls it) so this logic is unit-testable without driving an SSE stream.
        """
        for j in jobs:
            jid = j.get("id")
            if not jid:
                continue
            prompt = j.get("prompt") or ""
            # Collapsed like every other item name that reaches an email (alert_templates.
            # item_label does the same for scraped page titles) — a job NAME is agent-written
            # free text too, and an embedded newline reaches EmailMessage's Subject header
            # unescaped, which the stdlib raises on rather than silently mangling.
            jname = re.sub(r"\s+", " ", (j.get("name") or "your monitor")).strip()
            sub_payload = {"kind": "subscribed", "item": jname, "monitor": jname}
            sched = j.get("schedule_display") or str(j.get("schedule") or "") or None
            if sched:
                sub_payload["schedule"] = sched
            url = self._job_flag_value(prompt, "--url")
            if url:
                sub_payload["url"] = url
            # --below/--above and their direction ("under"/"over" the number), whichever the
            # command actually carries — a price_rise watch is authored with --above (rule
            # :4531), never --below, and showing no target at all for it was the bug.
            below, above = self._job_flag_value(prompt, "--below"), self._job_flag_value(prompt, "--above")
            raw, op = (below, "under") if below is not None else (above, "over")
            if raw is not None:
                try:
                    sub_payload["target"] = float(raw)
                    sub_payload["op"] = op
                    # --mode stock means the number is a COUNT ("fewer than 3 left"), never
                    # money — rule :4568 forbids --unit on a stock watch for exactly that
                    # reason, so fabricating "$" here would be a currency symbol on a count.
                    mode = self._job_flag_value(prompt, "--mode")
                    sub_payload["unit"] = ("" if mode == "stock" else
                                           self._job_flag_value(prompt, "--unit") or "$")
                except ValueError:
                    pass
            self._enqueue_subscription(handle, jid, sub_payload)

    @staticmethod
    def _pretty_phone(e164):
        d = re.sub(r"\D", "", e164 or "")
        if len(d) == 11 and d.startswith("1"):
            return f"+1 {d[1:4]}-{d[4:7]}-{d[7:]}"
        return e164 or "—"

    def _alert_setup_block(self, handle):
        """Exactly how an alert for this user will be delivered, shown when a task is scheduled.

        The user should never have to discover their alert setup by waiting for one to fire (or
        not). Every line here is something they can check at a glance and correct now: a wrong
        number, an address they do not read, a sender their phone will show as unknown.
        """
        prof = self._read_json(ALERT_PROFILE_FILE, {}) or {}
        c = self._contact(handle)
        phone = c.get("phone")
        email = self._alert_email(handle)
        chans = prof.get("channels") or ["sms", "email"]
        rows = []
        if "sms" in chans:
            if phone:
                frm = prof.get("sms_from")
                via = f" · shows as **{frm}**" if frm else ""
                rows.append(f"| 📱 Text | `{self._pretty_phone(phone)}`{via} |")
            else:
                rows.append("| 📱 Text | *no number on file — say “text me at …” to add one* |")
        if "email" in chans:
            rows.append(f"| ✉️ Email | `{email}` |" if email
                        else "| ✉️ Email | *no address on file* |")
        rows.append("| 📋 Every check | logged in **background-tasks** |")
        if not rows:
            return ""
        notes = [
            # The single most common first-week misdiagnosis: a channel post arrives, no text does,
            # and the user concludes the alerting is broken. It is not — the condition simply was
            # not met. Say so before it happens, but say it the way a person would.
            "You'll only get a text when it actually hits your price — every other check just "
            "gets logged.",
        ]
        if phone and prof.get("sms_strips_links", True):
            # Why the text has no link is worth one short clause; the carrier mechanics behind it
            # are not something the reader can act on.
            notes.append("The text won't have a link in it (phone carriers block those), so check "
                         "the email for that.")
        return ("\n\n**How you'll be alerted**\n\n"
                "| | |\n|---|---|\n" + "\n".join(rows) + "\n\n" + " ".join(notes))

    def _phone_prompt(self, handle, pending_request, cid=None):
        """Ask for a number BEFORE scheduling, and carry the request across the turn.

        The request is held in the per-chat store, not in the message: OpenWebUI escapes HTML
        comments, so anything parked in the reply body is shown to the user as base64 noise."""
        if cid:
            self._lru(self._phone_ask, cid, {"t": time.time(), "req": pending_request or ""})
        prof = self._read_json(ALERT_PROFILE_FILE, {}) or {}
        email = self._alert_email(handle)
        alt = (f"Or reply **email only** — alerts still go to `{email}`."
               if email else "Or reply **email only** to skip texts.")
        # Set the expectation about the sender BEFORE the number is handed over. These texts arrive
        # from an email-to-SMS gateway, so the sender shows as an address rather than a number —
        # which reads as spam if it turns up unannounced.
        frm = prof.get("sms_from")
        heads_up = (f"\n\nHeads up: texts arrive from **{frm}**, not a phone number — that's how "
                    f"the free carrier gateway works. Save it as a contact so it doesn't read as "
                    f"spam." if frm else "")
        return (f"📱 **What number should I text?**\n\n"
                f"You asked to be alerted, but there's no number saved for `{handle}` — so the task "
                f"would run, meet your condition, and text nobody.\n\n"
                f"Reply with your mobile number and I'll save it and schedule the task in one go. "
                f"Any of these work:\n"
                f"`5145550123` · `514-555-0123` · `+1 514 555 0123`\n\n"
                f"{alt}{heads_up}")

    @staticmethod
    def _marks(*extra):
        """Nothing. Kept as the single seam where trailing state used to be appended.

        Two rendering theories were tried and both were wrong: an HTML comment inline in a
        paragraph is escaped and shown, and so is the same comment as its own block after a blank
        line. OpenWebUI escapes them wherever they appear, so no invisible payload can be smuggled
        through the message body. State moved to the per-chat stores in __init__ instead; this
        returns the empty string so every former call site stays a no-op rather than being deleted
        and silently re-added by a future edit.
        """
        return ""

    def _mark_bg(self, cid):
        """Record that this chat's last reply was a background-task reply.

        Replaces `_BG_MARK in prev`, which needed the marker to survive in the message text.
        Reading still falls back to the marker so conversations from before this change keep
        working — history written then really does contain it.
        """
        if cid:
            self._bg_turn.pop(cid, None)
            self._bg_turn[cid] = time.time()
            while len(self._bg_turn) > 60:
                self._bg_turn.pop(next(iter(self._bg_turn)))

    def _was_bg_turn(self, cid, messages):
        if cid and (time.time() - self._bg_turn.get(cid, 0)) < PARK_TTL_S:
            return True
        prev = next((m.get("content") or "" for m in reversed(messages or [])
                     if m.get("role") == "assistant"), "")
        return self._BG_MARK in prev          # legacy: history written before the stores existed

    @staticmethod
    async def _say(text):
        yield text

    async def _phone_then_task(self, e164, handle, pending, scoped=False):
        """Save the number, then run the request the user made a turn ago."""
        if not self._save_phone(handle, e164):
            yield (f"⚠️ Couldn't save `{self._pretty_phone(e164)}` — `{ALERT_CONTACTS_FILE}` is "
                   f"not writable. The task was NOT scheduled; alerts would have gone nowhere.")
            return
        yield f"✅ Saved `{self._pretty_phone(e164)}` for texts.\n\n"
        if not pending:
            yield ("Now tell me what to watch and I'll set it up." )
            return
        async for chunk in self._hermes_stream(pending, handle, verify_creation=True,
                                              scoped=scoped):
            yield chunk

    def _phone_reply(self, text, handle, pending, cid=None, scoped=False):
        """Handle the turn AFTER a phone prompt. None => not a phone answer, route normally.

        Consuming the prompt clears the parked request: "no thanks" a turn after the task was
        already scheduled used to re-submit it as a duplicate."""
        t = (text or "").strip()
        if cid and (self._PHONE_DECLINE.match(t) or self._PHONE_RE.search(t)):
            self._phone_ask.pop(cid, None)
        if self._PHONE_DECLINE.match(t):
            if not pending:
                return self._say("No problem — no number saved." )
            return self._hermes_stream(pending, handle, verify_creation=True, scoped=scoped)
        m = self._PHONE_RE.search(t)
        if not m:
            # A short, digit-heavy reply to "what is your number?" IS a phone attempt even when it
            # is too mangled to match the pattern. Letting "12345" fall through sends it to the
            # chat model, which has no idea a number was requested and will answer as if it were
            # small talk. Anything else (a real question, a change of subject) routes normally.
            digits = len(re.sub(r"\D", "", t))
            if not (len(t) <= 40 and digits >= 4 and digits >= len(re.sub(r"\s", "", t)) // 2):
                return None
            attempt = t
        else:
            attempt = m.group(1)
        e164 = self._norm_phone(attempt)
        if not e164:
            # Reject at the point they typed it, not silently at send time three days later. The
            # request stays parked on the pipe (this branch never popped it), so the retry still
            # has something to schedule.
            if cid:
                self._lru(self._phone_ask, cid, {"t": time.time(), "req": pending or ""})
            return self._say(
                f"`{attempt.strip()}` doesn't look like a mobile number I can text — I need "
                f"10 digits (or +country code). Try again, or reply **email only**.")
        return self._phone_then_task(e164, handle, pending, scoped)

    def _pending_phone_request(self, messages, cid=None):
        """The request parked by a previous _phone_prompt, or None.

        Reads the per-chat store first, then falls back to the legacy in-message marker so
        conversations that predate the store still complete their phone flow. The store is cleared
        by _phone_reply, which is what stops a consumed prompt being resurrected.

        The legacy scan covers the last TWO assistant turns, not one: people answer questions out
        of order — phone prompt, "wait, how much does a text cost?", answer, and only THEN the
        number. A single-turn scan had already forgotten the parked request by then, so the bare
        number fell through to the chat model. Two turns is the whole allowance on purpose:
        further back, the prompt has scrolled away and a stray digit string should be ordinary
        chat again.
        """
        d = self._phone_ask.get(cid) if cid else None
        if d:
            return d["req"] if time.time() - d["t"] <= PARK_TTL_S else None
        recent = [m.get("content") or "" for m in reversed(messages or [])
                  if m.get("role") == "assistant"]
        for prev in recent[:2]:
            m = self._PHONE_MARK_RE.search(prev)
            if m:
                try:
                    return base64.b64decode(m.group(1)).decode() or ""
                except Exception:
                    return ""
            # A bg-task reply between us and the prompt means the parked request was already
            # CONSUMED — the number arrived (or was declined) and the task was submitted. Reading
            # past it would resurrect the request: "no thanks" one turn after scheduling matched
            # _PHONE_DECLINE and re-submitted the job, a duplicate the user never asked for. Every
            # consumption path ends in _BG_MARK (hermes stream, decline, re-park), so the marker
            # doubles as the scan's stop sign.
            if self._BG_MARK in prev:
                return None
        return None

    # ---------- cross-turn state for job management ----------
    @staticmethod
    def _lru(store, cid, value, cap=60):
        store.pop(cid, None)          # move-to-end so an active chat is not evicted first
        store[cid] = value
        while len(store) > cap:
            store.pop(next(iter(store)))

    def _park_jobs(self, cid, jobs, handle=""):
        """Remember the list just rendered, so "the second one" means something next turn.

        Returns "" — the reply carries no payload. Ordinal N maps to ids[N-1] and is never
        recomputed, because a job that vanishes between two turns would silently shift every
        number below it.

        The handle rides along so an ordinal cannot be resolved by a different user than the one
        the list was rendered for. Chat ids should already be per-user; this does not depend on it.
        """
        if cid:
            self._lru(self._parked, cid,
                      {"t": time.time(), "h": handle or "",
                       "ids": [j.get("id") for j in jobs[:JOBS_MAX]],
                       "ns": [str(j.get("name") or "")[:40] for j in jobs[:JOBS_MAX]]})
        return ""

    def _parked_jobs(self, cid, messages=None, handle=None):
        """[{i, id, n}] from the most recent rendered list, or [].

        Reads the per-chat store, falling back to the legacy HTML-comment marker so conversations
        that predate the store still resolve ordinals.

        Staleness is not really handled by this clock: jobs vanish with no tombstone when a repeat
        budget is exhausted, so every caller re-fetches /api/jobs and uses this only to map
        ordinal -> id. The TTL is belt-and-braces.

        `handle` refuses a record rendered for someone else. Legacy markers carry no handle and are
        honoured only for admins (handle=None) — they predate ownership entirely.
        """
        d = self._parked.get(cid) if cid else None
        if d and time.time() - d["t"] <= PARK_TTL_S:
            if handle is not None and (d.get("h") or "") != handle:
                return []
            return [{"i": i + 1, "id": jid, "n": (d["ns"][i] if i < len(d["ns"]) else "")}
                    for i, jid in enumerate(d["ids"]) if jid]
        if handle is not None:
            return []
        seen = 0
        for m in reversed(messages or []):
            if m.get("role") != "assistant":
                continue
            seen += 1
            if seen > 2:
                break
            hits = self._JOBS_MARK_RE.findall(m.get("content") or "")
            if not hits:
                continue
            try:
                legacy = json.loads(base64.b64decode(hits[-1]).decode())
                if legacy.get("v") != 1 or time.time() - float(legacy.get("t", 0)) > PARK_TTL_S:
                    return []
                ids, ns = legacy.get("ids") or [], legacy.get("ns") or []
                return [{"i": i + 1, "id": jid, "n": (ns[i] if i < len(ns) else "")}
                        for i, jid in enumerate(ids) if jid]
            except Exception:
                return []
        return []

    def _confirm_park(self, cid, op, job, stage="confirm", handle="", jobs=None):
        """Arm an operation for the NEXT turn. The names and schedules are kept so the yes-turn can
        check nothing changed under us between the question and the answer.

        `jobs` arms the same op over several at once. Stored as a list either way so the consuming
        branch has one shape to handle — a bulk delete is the last place to want two code paths.
        """
        batch = list(jobs) if jobs else ([job] if job else [])
        if cid:
            self._lru(self._armed, cid,
                      {"t": time.time(), "stage": stage, "op": op, "h": handle or "",
                       "ids": [j.get("id") for j in batch],
                       "ns": [str(j.get("name") or "")[:60] for j in batch],
                       "ss": [str(j.get("schedule_display") or j.get("schedule") or "")[:40]
                              for j in batch],
                       # Kept for the single case so existing copy and the legacy marker keep
                       # working unchanged.
                       "id": (job or {}).get("id"),
                       "n": str((job or {}).get("name") or "")[:60],
                       "s": str((job or {}).get("schedule_display")
                                or (job or {}).get("schedule") or "")[:40]})
        return ""

    def _pending_confirm(self, cid, messages=None, handle=None):
        """The armed op, or None. Expiry is REPORTED rather than ignored, so a slow "yes" gets an
        explanation instead of silence.

        A record armed for a different handle is not visible — a "yes" must never land on a
        confirmation somebody else was shown."""
        d = dict(self._armed.get(cid) or {}) if cid else {}
        if d and handle is not None and (d.get("h") or "") != handle:
            return None
        if not d and handle is not None:
            return None
        if not d:
            prev = next((m.get("content") or "" for m in reversed(messages or [])
                         if m.get("role") == "assistant"), "")
            hits = self._CONFIRM_MARK_RE.findall(prev)
            if not hits:
                return None
            try:
                d = json.loads(base64.b64decode(hits[-1]).decode())
            except Exception:
                return None
            if d.get("v") != 1:
                return None
        d["expired"] = (time.time() - float(d.get("t", 0))) > CONFIRM_TTL_S
        return d

    def _disarm(self, cid):
        """Consume the armed op. Called on every terminal branch so a second "yes" cannot replay a
        delete against a chat whose job is already gone."""
        self._armed.pop(cid, None)

    # ---------- resolving "the RTX one" to a real job ----------
    @classmethod
    def _manage_op(cls, text):
        """'cancel' | 'pause' | 'resume' | None, from the leading verb.

        Length-capped like _is_bg_followup: a 300-word message that happens to open with "stop" is
        prose, not an instruction aimed at a job.
        """
        t = (text or "").strip()
        if not t or len(t) > 160:
            return None
        m = cls._MANAGE_VERB.match(t)
        if not m:
            return None
        verb = re.sub(r"\s+", " ", m.group("verb").lower())
        return cls._MANAGE_OPS.get(verb) or cls._MANAGE_OPS.get(verb.replace("-", ""))

    @staticmethod
    def _fold(s):
        """Comparison form. NFKD-normalises rather than deleting non-ASCII, so a job named
        'Café price watch' is still findable by typing 'cafe'."""
        try:
            import unicodedata
            s = unicodedata.normalize("NFKD", str(s or ""))
            s = "".join(c for c in s if not unicodedata.combining(c))
        except Exception:
            s = str(s or "")
        return re.sub(r"\s+", " ", re.sub(r"[^\w$.]+", " ", s.lower())).strip()

    @classmethod
    def _ref_ordinal(cls, phrase):
        """1-based position, or None. Deliberately not a general number parser: a bare number only
        counts when it is the WHOLE reference, so "cancel 2" resolves and "cancel the 60k alert"
        does not."""
        # Strip conversational filler first: "b too" and "also b" name the same row as "b", and
        # dropping them turned a perfectly clear follow-up into "I don't see a task matching".
        p = cls._REF_FILLER.sub(" ", phrase).strip().strip(",.")
        p = re.sub(r"\s+", " ", p).strip()
        if re.fullmatch(r"\d{1,2}", p):
            return int(p)
        # Disambiguation candidates are lettered so a number can never mean two different jobs in
        # one conversation; a bare letter answers that question and nothing else.
        if re.fullmatch(r"[a-j]", p, re.I):
            return ord(p.lower()) - 96
        if re.search(r"\blast\s+one\b|\bthe\s+last\b", p, re.I):
            return -1          # resolved against the parked length by the caller
        m = cls._ORDINAL_RE.search(p)
        if not m:
            return None
        word, bare, hashed, labelled = m.groups()
        if word:
            return cls._ORDINAL_WORDS[word.lower()]
        if hashed or labelled:
            return int(hashed or labelled)
        # A bare digit only counts if nothing else is left of the reference.
        rest = (p[:m.start()] + p[m.end():]).strip()
        if bare and not [t for t in cls._fold(rest).split() if t not in cls._REF_STOP]:
            return int(bare)
        return None

    @classmethod
    def _ref_tokens(cls, phrase):
        return [t for t in cls._fold(phrase).split() if t not in cls._REF_STOP and len(t) > 1]

    def _resolve_ref(self, text, jobs, parked, _split=True):
        """Map the user's words onto the job(s) they meant, or refuse.

        Ordered strategies, and the FIRST stage producing a candidate decides — a weaker signal
        must never override a stronger one. Ambiguity is never broken by a score margin: a
        threshold is a guess, and a wrong guess here deletes the wrong monitor.

        `status="bulk"` means the user named a SET on purpose ("all of them", "a and b"). That is
        answered, not refused: refusing it and offering a one-at-a-time menu made cancelling two
        tasks a four-turn negotiation with a renumbered list in the middle. Safety moves to the
        confirmation, which names every job it is about to delete.
        """
        out = {"status": "none", "job": None, "candidates": [], "strategy": "", "needle": ""}
        by_id = {j.get("id"): j for j in jobs}
        phrase = (text or "").strip()
        m = self._MANAGE_VERB.match(phrase)
        if m:
            phrase = phrase[m.end():]
        phrase = phrase.strip().strip("?!.,").strip()
        out["needle"] = phrase[:60]

        # R0 — an exact id the user typed. If it does not exist, say so; never fall through, they
        # were specific and deserve a straight answer.
        for tok in re.findall(r"\b[0-9a-f]{12}\b", phrase.lower()):
            if tok in by_id:
                return {**out, "status": "one", "job": by_id[tok], "strategy": "id"}
            return {**out, "status": "bad_id", "needle": tok, "strategy": "id"}

        # Exclusion still never resolves: "everything except the rtx one" describes a set by what
        # is missing from it, and getting that wrong deletes the one thing they meant to keep.
        if self._REF_QUANTIFIER.search(phrase) or self._REF_NEGATION.search(phrase):
            return {**out, "status": "many", "candidates": jobs, "strategy": "guarded"}

        # "all of them" / "both" — the whole visible set, which for a scoped user is already only
        # their own jobs.
        if jobs and self._REF_BULK.search(phrase):
            return {**out, "status": "bulk", "candidates": list(jobs), "strategy": "bulk"}

        # "a and b", "1, 2", "the first and the third" — each part must resolve on its own, and to
        # a DIFFERENT job, or this is not a multi-select and the normal ladder should have it.
        if _split:
            parts = [q for q in (x.strip() for x in self._REF_JOINER.split(phrase)) if q]
            if len(parts) > 1:
                picked, seen = [], set()
                for part in parts:
                    r = self._resolve_ref(part, jobs, parked, _split=False)
                    if r["status"] != "one" or not r["job"]:
                        picked = []
                        break
                    jid = r["job"].get("id")
                    if jid not in seen:
                        seen.add(jid)
                        picked.append(r["job"])
                if len(picked) > 1:
                    return {**out, "status": "bulk", "candidates": picked, "strategy": "multi"}

        # R1 — an ordinal against the list we actually rendered.
        n = self._ref_ordinal(phrase)
        if n is not None:
            if not parked:
                return {**out, "status": "need_list", "strategy": "ordinal"}
            if n == -1:
                n = len(parked)
            if not (1 <= n <= len(parked)):
                return {**out, "status": "out_of_range", "needle": str(n), "strategy": "ordinal"}
            p = parked[n - 1]
            if p["id"] in by_id:
                return {**out, "status": "one", "job": by_id[p["id"]], "strategy": "ordinal"}
            return {**out, "status": "gone", "needle": p.get("n") or p["id"], "strategy": "ordinal"}

        tokens = self._ref_tokens(phrase)

        # R2 — an id prefix. Six hex minimum: shorter and ordinary words start colliding.
        for tok in re.findall(r"\b[0-9a-f]{6,11}\b", phrase.lower()):
            hits = [j for j in jobs if str(j.get("id") or "").startswith(tok)]
            if len(hits) == 1:
                return {**out, "status": "one", "job": hits[0], "strategy": "prefix"}
            if len(hits) > 1:
                return {**out, "status": "many", "candidates": hits, "strategy": "prefix"}

        # R3/R4 — name, then name+prompt. Word-boundary matching, not substring containment:
        # 'btc' must not match 'btcusd-adjacent' text by accident.
        def _match(field):
            contiguous, every = [], []
            needle = " ".join(tokens)
            for j in jobs:
                hay = self._fold(field(j))
                if needle and re.search(rf"\b{re.escape(needle)}\b", hay):
                    contiguous.append(j)
                elif tokens and all(re.search(rf"\b{re.escape(t)}\b", hay) for t in tokens):
                    every.append(j)
            return contiguous or every

        if tokens:
            for strat, field in (("name", lambda j: j.get("name")),
                                 ("prompt", lambda j: f"{j.get('name')} {j.get('prompt') or ''}")):
                hits = _match(field)
                if len(hits) == 1:
                    return {**out, "status": "one", "job": hits[0], "strategy": strat}
                if len(hits) > 1:
                    return {**out, "status": "many", "candidates": hits, "strategy": strat}

            # R5 — overlap. Ties are ambiguity, never a margin call.
            scored = [(sum(1 for t in set(tokens)
                           if re.search(rf"\b{re.escape(t)}\b",
                                        self._fold(f"{j.get('name')} {j.get('prompt') or ''}"))), j)
                      for j in jobs]
            best = max([s for s, _ in scored], default=0)
            if best:
                win = [j for s, j in scored if s == best]
                if len(win) == 1:
                    return {**out, "status": "one", "job": win[0], "strategy": "overlap"}
                return {**out, "status": "many", "candidates": win, "strategy": "overlap"}
            return {**out, "status": "none", "strategy": "overlap"}

        # R6 — a bare reference ("cancel it", "cancel such and such tracking"). Only resolves when
        # there is nothing to be ambiguous about.
        if len(jobs) == 1:
            return {**out, "status": "one", "job": jobs[0], "strategy": "solo"}
        if len(parked) == 1 and parked[0]["id"] in by_id:
            return {**out, "status": "one", "job": by_id[parked[0]["id"]], "strategy": "solo"}
        return {**out, "status": "many", "candidates": jobs, "strategy": "bare"}

    def _is_bg_followup(self, text, messages, cid=None):
        """True when this short message continues the previous hermes exchange in THIS chat."""
        if not text or len(text) > 120:
            return False
        return self._was_bg_turn(cid, messages) and bool(self._BG_FOLLOWUP.match(text.strip()))

    def _is_bg_task_request(self, t):
        raw = (t or "").strip().lower()
        if self._BG_SLASH.match(raw):
            return True
        # Manage verbs BEFORE the question deny-list. "what are my scheduled tasks?" is both a
        # question and a management request aimed at existing jobs — and the deny-list used to win,
        # which made the "what are ... tasks" arm of _BG_MANAGE dead code: the turn went to the
        # chat model, which answered with a hallucinated task list. The swap is safe because
        # _BG_MANAGE is anchored — it only fires when the manage verb is the message's opening move.
        if self._BG_MANAGE.match(raw):
            return True
        # BEFORE the question deny-list, and that placement is load-bearing: four of these nine
        # phrasings open with "what" and _BG_QUESTION would kill them. Also `search`, not `match` —
        # the arms are internally anchored, but a leading politeness word must not defeat them.
        if MANAGE_DETERMINISTIC and self._BG_LIST.search(raw):
            return True
        if self._BG_QUESTION.match(raw):
            return False  # asking ABOUT monitoring is chat, whatever else matches
        return bool(self._BG_VERB.match(raw) and self._BG_RECURRENCE.search(raw))

    def _is_image_request(self, t):
        raw = t.lower()
        if self._FIGURATIVE.search(raw):
            return False
        # "make this picture realistic / that photo brighter" is about the EXISTING image —
        # never a fresh generation. Without this, a follow-up whose reference could not be
        # recovered fell through to a from-scratch t2i of the literal follow-up words.
        if re.search(r"\b(this|that|it)\s+(image|picture|photo|pic|drawing|one)\b", raw):
            return False
        # explicit generation verb + an image object → a request even if phrased as a question
        if re.search(r"\b(create|creating|generate|make|design|produce|render|draw|sketch|paint|"
                     r"illustrate|show me|give me|i want|can you make|could you make)\b"
                     r".{0,30}\b(image|images|picture|pictures|"
                     r"photo|photos|pic|drawing|painting|illustration|art|artwork|logo|wallpaper|"
                     r"portrait|render|scene|poster|cartoon|caricature)\b", raw):
            return True
        # Explicit escape hatch for terse phrasing the default-deny rule below would reject.
        if self._MEDIA_SLASH_IMG.match(raw):
            return True
        # An IMPERATIVE drawing verb is a request ("draw a cat", "paint a stormy sea"). The same verb
        # embedded in a sentence is not ("my kid loves to draw", "we should paint the fence").
        # Position is the discriminator, so this is anchored to the start of the message.
        if self._IMPERATIVE_DRAW.match(raw):
            return True
        # DEFAULT DENY. Two fallbacks used to live here: a bare \b(draw|sketch|paint|illustrate)\b
        # anywhere in the message, and a bare '<image-noun> of'. Both were guarded only against
        # questions and small-talk, so declarative sentences went through — "there's a photo of my
        # grandmother on the shelf" and "she wants to illustrate a children's book someday" both
        # started renders. Mentioning a picture is not asking for one.
        return False

    def _is_edit_request(self, t):
        """Edit intent for an image: change/remove/add/replace/bigger/etc.
        (Pure questions about the image have none of these verbs → they go to vision chat.)"""
        t = t.lower()
        # 'give' removed — 'give me <non-visual>' ('give me book recommendations') is a chat request,
        # not an edit; real edits use a concrete verb or reference a visual attribute (see _wants_edit).
        return bool(re.search(
            r"\b(edit|change|replace|remove|delete|erase|swap|add|put|turn|make|"
            r"recolou?r|colou?r|adjust|retouch|modify|update|fix|crop|rotate|blur|bigger|smaller|"
            r"larger|zoom|brighter|darker|more|less|get rid of|without|instead of|into a|to a)\b", t))

    def _chat_id(self, body, meta=None):
        """Stable per-conversation id. OpenWebUI doesn't always put chat_id in the body, and a
        'default' fallback would make the recent-media caches GLOBAL — follow-up edits would then
        grab media from a different conversation. Fall back to hashing the chat's first user
        message, which is stable within a conversation and distinct across them."""
        cid = (body.get("chat_id")
               or (body.get("metadata") or {}).get("chat_id")
               or (meta or {}).get("chat_id"))
        if cid:
            return str(cid)
        for m in body.get("messages", []):
            if m.get("role") == "user":
                c = m.get("content")
                s = c if isinstance(c, str) else json.dumps(c, sort_keys=True)[:500]
                return hashlib.md5(s.encode()).hexdigest()[:16]
        return "default"

    def _extract_b64(self, s):
        if not isinstance(s, str):
            return None
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", s)
        return m.group(1) if m else None

    def _recent_media(self, messages):
        """Most recent media in the chat, scanning backwards.
        Returns ('image', b64) | ('video', (prompt, seed_or_None)) | (None, None).
        Videos carry their prompt+seed in data-p64/data-seed attributes on the <video> tag
        (invisible in the UI, survives restarts); older messages fall back to the 🎬 caption."""
        for m in reversed(messages or []):
            role, c = m.get("role"), m.get("content", "")
            if role == "user" and isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            return "image", url.split(",", 1)[1]
            elif role == "assistant":
                if isinstance(c, str) and "data:video/" in c:
                    v = re.search(r'<video[^>]*data-p64="([A-Za-z0-9+/=]*)"[^>]*data-seed="(\d+)"'
                                  r'(?:[^>]*data-opts="([^"]*)")?', c)
                    if v:
                        try:
                            prompt = base64.b64decode(v.group(1)).decode("utf-8", "ignore")
                        except Exception:
                            prompt = ""
                        return "video", (prompt, int(v.group(2)), self._parse_opts(v.group(3)))
                    cap = re.search(r"\*🎬 (.+?)\*", c)
                    return "video", ((cap.group(1) if cap else ""), None, None)
                # Images: handle every shape OWUI 0.10 delivers — str content, list-of-parts
                # content, and the raw message.output items (pipe replies arrive with
                # content='' on some paths, the markdown living only in output). The old
                # str-only check is why generated images were invisible to the next turn.
                b = (ms.image_from_message(m) if ms else self._extract_b64(c))
                if b:
                    return "image", b
        return None, None

    @staticmethod
    def _opts_attr(opts):
        """Serialize the render options onto the <video> tag ('WxHxL') so a follow-up keeps the
        original clip's resolution/length even across a restart."""
        o = opts or {}
        return f'{o.get("w", V_W)}x{o.get("h", V_H)}x{o.get("length", V_LEN_14B)}'

    def _parse_opts(self, s):
        """Inverse of _opts_attr: 'WxHxL' → opts dict, or None."""
        m = re.match(r"(\d+)x(\d+)x(\d+)", s or "")
        if not m:
            return None
        return {"w": int(m.group(1)), "h": int(m.group(2)), "length": int(m.group(3)), "fast": False}

    def _scrub(self, text):
        """Strip embedded image/video data URIs so we don't feed megabytes of base64 to the chat model."""
        if not isinstance(text, str):
            return text
        # Fast path: the vast majority of turns carry no media — skip the three multi-MB regexes.
        if "data:" not in text and "<video" not in text:
            return text
        text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "[generated image]", text)
        text = re.sub(r"<video[^>]*>.*?</video>", "[generated video]", text, flags=re.S)
        text = re.sub(r"data:(?:image|video)/[^;]+;base64,[A-Za-z0-9+/=]+", "[media]", text)
        return text

    # Visual nouns whose presence signals the message is about the image itself (an edit),
    # not an off-topic chat turn — used as the positive edit test below.
    _VISUAL_NOUN = re.compile(
        r"\b(image|picture|photo|pic|background|foreground|colou?rs?|sky|lighting|"
        r"shadows?|hair|eyes?|face|skin|clothe?s|clothing|shirt|dress|hat|smile|hands?|"
        r"scene|left|right|top|bottom|corner|edges?)\b", re.I)

    def _wants_edit(self, text):
        """Given an image already exists in the chat, decide if this message asks to MODIFY it.
        Chat is the DEFAULT: only a clear edit verb or a visual reference to the image counts as an
        edit, so plain questions / off-topic asks ('write a poem about it') stay in chat instead of
        silently re-running a ~2-minute Qwen edit on a stale image."""
        core = re.sub(r"^\s*(please|hey|okay?|so|and|then|now|also)[,\s]+", "", text.strip(), flags=re.I)
        core = re.sub(r"^\s*(can|could|would|will)\s+(you\s+)?(please\s+|maybe\s+)?", "", core, flags=re.I).strip()
        low = core.lower()
        # small talk / acknowledgment → chat  (checked BEFORE edit verbs)
        if self._is_smalltalk(low):
            return False
        # "have them use chopsticks" / "let her hold it" is an imperative scene direction —
        # _QUESTION's leading 'have' would otherwise classify it as a question → chat, and the
        # user's edit silently never ran.
        if re.match(r"(have|let)\s+(him|her|them|it|the|his|their)\b", low):
            return True
        # question / info request / write-verbs → chat  (translate/write/compose/summarize/draft).
        # A trailing '?' on an imperative edit ("can you make it brighter?") is politeness, not
        # a question — only redirect when it doesn't carry an edit verb.
        if self._QUESTION.match(low):
            return False
        if low.endswith("?") and not self._is_edit_request(low):
            return False
        # explicit edit verb (change/remove/bigger/…) → edit
        if self._is_edit_request(low):
            return True
        # positive edit test: an imperative that references the image or a visual attribute → edit;
        # anything else (a statement/topic that never names the image) → chat
        if self._VISUAL_NOUN.search(low):
            return True
        return False

    def _edit_instruction(self, text):
        """Strip a 'can you edit the picture and …' wrapper, leaving the actual instruction for the editor."""
        t = text.strip()
        t = re.sub(r"^\s*(please\s+)?(can|could|would|will)\s+you\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*please\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*(edit|change|modify|update|fix|retouch|adjust)\s+(this|the|my)\s+(image|picture|photo|pic)\s*(and|to|so(?:\s+that)?|by|:|,)?\s*", "", t, flags=re.I)
        t = re.sub(r"^\s*(in|on|for)\s+(this|the)\s+(image|picture|photo|pic)[,:]?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    # Whole-image restyle targets: (detect_pattern, target_description, negative). Realistic first —
    # "make the cartoon realistic" names both styles and the LAST one is the target the user wants.
    _STYLE_TARGETS = (
        (r"photo[\s-]?realistic|realistic|photoreal|lifelike|real\s+photo(?:graph)?|into\s+a\s+photo(?:graph)?",
         "a photorealistic photograph: real human skin texture and natural proportions, true-to-life "
         "colors and fabrics, real-world lighting and shadows, shot like a DSLR photo",
         "cartoon, illustration, anime, 3D render, CGI, drawing, painting, stylized, plastic skin"),
        (r"animated(?:[\s-]style)?|animation[\s-]style|cartoon(?:ish|[\s-]style)?|pixar|disney|3d\s+animated",
         "a vibrant 3D animated cartoon-style illustration: stylized characters with expressive faces, "
         "clean shapes, rich saturated colors — NOT photorealistic",
         "photorealistic, photograph, real skin texture, film grain"),
        (r"anime|manga",
         "a Japanese anime illustration: clean line art, cel shading, expressive anime faces",
         "photorealistic, photograph, western cartoon"),
        (r"water\s?colou?r(?:\s+painting)?",
         "a soft watercolor painting: visible brush strokes, gentle color washes, paper texture",
         "photograph, 3D render, hard edges"),
        (r"oil\s+painting|painting",
         "a classical painted artwork: rich visible brush strokes, painterly light",
         "photograph, 3D render"),
        (r"(?:pencil\s+)?sketch|line\s?art|charcoal|drawing",
         "a hand-drawn pencil sketch: graphite line work and shading on paper",
         "photograph, color photo, 3D render"),
        (r"pixel\s?art|8[\s-]?bit",
         "retro pixel art with a limited palette and crisp pixel blocks",
         "photograph, smooth gradients"),
    )

    def _style_conversion(self, text):
        """(instruction, negative) when the request is a WHOLE-image style change ('make it
        realistic instead', 'turn this into a cartoon') — else None. The generic edit rewriter
        can't handle these: it ends every instruction with 'keep identity/background/lighting
        unchanged', which fights a global restyle, so we build the instruction directly."""
        if ms:
            return ms.style_conversion(text)
        t = (text or "").lower()
        if re.search(r"\b(video|clip|gif|footage|animate|move|moving|motion)\b", t):
            return None  # motion request, not a still restyle
        if not re.search(r"\b(it|this|that|everything|the\s+(?:whole\s+)?(?:image|picture|photo|pic|"
                         r"scene|cartoon|drawing|painting|sketch|illustration|artwork))\b", t) \
                and not re.search(r"\binstead\b", t):
            return None  # targets a specific object ('make the ball realistic') → normal edit path
        hits = [(m.end(), tgt, neg) for pat, tgt, neg in self._STYLE_TARGETS
                for m in [re.search(rf"(?:{pat})\b", t)] if m]
        if not hits:
            return None
        # last-mentioned style wins ('make the cartoon realistic' → realistic); on a tie
        # ('watercolor painting' ends where 'painting' ends) the more specific earlier entry wins
        _, target, negative = max(hits, key=lambda h: h[0])
        # Subject-AGNOSTIC preservation wording: the old text hard-coded "the same people —
        # ages, genders, ethnicity" into every restyle, handing the editor an instruction
        # about people even when the picture contains a cat.
        return (f"Transform the entire image into {target}. Keep the composition and every "
                f"subject exactly as they are — the same subjects, poses, expressions, clothing "
                f"or markings, colors and framing — changing ONLY the rendering style, applied "
                f"consistently across the whole image.", negative)

    def _style_enrich(self, instruction, msgs, ref_b64=None):
        """Restate the concrete subjects inside a style-conversion instruction — on big style
        jumps the editor drifts identity when the instruction only says 'the same subjects'
        generically (observed: a father came out a different ethnicity on cartoon→photo).
        The rewriter now SEES the image when available, so the subjects it names are the ones
        actually in the picture — a cat stays 'the orange tabby cat', never a guessed person.
        Falls back to the generic instruction."""
        ctx = self._edit_context(msgs)
        if not ctx and not ref_b64:
            return instruction
        try:
            r = requests.post(f"{self.ollama}/api/generate", json={
                "model": self.chat_model,
                "system": ("You tighten image style-conversion instructions. Name every subject "
                           "concretely AS IT ACTUALLY APPEARS in the image/conversation — species "
                           "or kind, colors or markings, and for people their age, gender, "
                           "ethnicity and skin tone — instead of generic wording like 'every "
                           "subject'. NEVER introduce a subject that is not visibly there. Keep "
                           "the conversion command, the style description, and the 'changing ONLY "
                           "the rendering style' ending intact. ONE instruction under 110 words. "
                           "Output ONLY the instruction text."),
                "prompt": f"Conversation:\n{ctx}\n\nInstruction:\n{instruction}\n\nRewritten instruction:",
                **({"images": [ref_b64]} if ref_b64 else {}),
                "stream": False, "think": False, "keep_alive": 0,
                "options": {"temperature": 0.3, "num_predict": 260}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"')
            if out and ("transform" in out.lower() or "convert" in out.lower()) and len(out) < 1200:
                return out
        except Exception:
            pass
        return instruction

    def _edit_boost(self, text):
        """'you barely changed it / still looks young' retry phrasing → push the sampler harder."""
        return bool(re.search(
            r"\b(still|barely|hardly|try again|not enough|no change|"
            r"didn'?t (?:change|work|do|listen)|doesn'?t (?:look|seem)|"
            r"(?:way|much) (?:more|older|younger|bigger|smaller))\b", (text or "").lower()))

    # Fallback rewrite contract, used only when the media_session sidecar is missing or no
    # reference image is available. The shipped path is ms.EDIT_REWRITE_VISION_SYS with the
    # reference image ATTACHED — the official Qwen edit enhancer is a VLM, and a text-only
    # rewriter is exactly what invents subjects. The old prompt's worked examples ('the young
    # woman on the right', ethnicity lists) were copied verbatim by the model onto non-people
    # images; this fallback names its rules without example subjects.
    _EDIT_REWRITE_SYS = (
        "You rewrite a photo-editing request into ONE imperative instruction under 80 words "
        "for the Qwen-Image-Edit model, which sees the photo alongside your instruction. You "
        "are given the conversation so far; rewrite ONLY the last user request. Keep the core "
        "intention unchanged — only make it clearer, concrete and visually feasible. Refer to "
        "subjects the way the conversation describes them; NEVER introduce a subject the "
        "conversation does not mention. Use absolute target states, not relative wording "
        "('a bit older' becomes a concrete age and its visible traits). When changing a "
        "person, restate the traits that must survive (skin tone, hair, clothing, build). "
        "End with what must stay unchanged. Then a second line 'AVOID: ' plus 3-8 "
        "comma-separated visual traits the RESULT must not contain, drawn from the actual "
        "request. Output EXACTLY two lines:\nEDIT: <instruction>\nAVOID: <traits>"
    )

    def _edit_context(self, msgs, limit=8):
        """Compact 'user:/assistant:' transcript (media scrubbed) so the rewriter can resolve
        references like 'the son' from earlier turns. Injected blocks (memory filter, code
        interpreter) are stripped from user lines — OpenWebUI PREPENDS them to the user's
        message, so without stripping the 300-char cap kept the injection and DROPPED the
        user's actual words, feeding the rewriter memories instead of the request."""
        lines = []
        for m in (msgs or [])[-limit:]:
            role, c = m.get("role"), m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if isinstance(c, list):
                t = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                if any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
                    t = (t + " [attached image]").strip()
            else:
                t = str(c or "")
            if role == "user":
                t = self._strip_injected_context(t)
            t = self._scrub(t).strip()
            if t:
                lines.append(f"{role}: {t[:300]}")
        return "\n".join(lines)

    def _comfy_idle(self):
        """True if ComfyUI has no running/pending job — so a /free won't evict another pipe's
        in-flight render (e.g. an Animate/SCAIL job). Fail-open: on error assume idle."""
        try:
            q = requests.get(f"{self.comfy}/queue", timeout=10).json()
            return not q.get("queue_running") and not q.get("queue_pending")
        except Exception:
            return True

    def _vram_free_gib(self):
        """Free VRAM on GPU0 in GiB via ComfyUI /system_stats, or None if unavailable."""
        try:
            d = requests.get(f"{self.comfy}/system_stats", timeout=10).json()
            dev = (d.get("devices") or [{}])[0]
            return float(dev.get("vram_free", 0)) / (1024 ** 3)
        except Exception:
            return None

    def _gpu_contended(self):
        """True only when ComfyUI has a job actually RUNNING. Never raises.

        Deliberately not `not _comfy_idle()`: that also counts a PENDING queue, and it must not.
        The same reasoning as gpuguard's AGENT_MODEL exclusion — reporting contention the user
        cannot act on, or that is really Ollama evicting its own tenant, trains them to ignore the
        status strip, and then it is worth nothing when it matters.

        Used only to TELL the user their reply may be slow. It gates nothing, so a failed probe is
        silent: instrumentation must never be why a chat turn fails.
        """
        try:
            with requests.get(f"{self.comfy}/queue", timeout=3) as r:
                return bool((r.json().get("queue_running") or []))
        except Exception:
            return False

    def _comfy_free(self, need_gib=20.0):
        """Ask ComfyUI to unload its models so the ~20 GB vision model can load for the QA check,
        then BLOCK until VRAM is actually released (the unload is async — a blind 2 s sleep raced it).
        Skips the unload while another ComfyUI job is in flight so we don't evict another pipe's
        render. Returns True if enough VRAM ended up free."""
        if not self._comfy_idle():
            return (self._vram_free_gib() or need_gib) >= need_gib
        try:
            requests.post(f"{self.comfy}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except Exception:
            pass
        for _ in range(20):  # up to ~20 s for the CUDA allocator to actually release
            v = self._vram_free_gib()
            if v is None or v >= need_gib:
                return True
            time.sleep(1)
        return (self._vram_free_gib() or 0) >= need_gib

    _VERIFY_SYS = (
        "You are a strict image QA checker. You get a user's request and the generated image. Check ONLY "
        "hard requirements: person count, each person's apparent age bracket, gender, ethnicity, named "
        "objects/actions/setting — including the key object a named activity implies ('playing basketball' "
        "requires a visible basketball, 'having dinner' requires food on the table) — and, ONLY when the "
        "request names an art style or medium ('animated "
        "picture', 'cartoon', 'anime', 'watercolor' …), that the image is rendered in that style (a "
        "photorealistic photo when an animated/cartoon style was asked for is a FAIL). When no style is "
        "named, ignore style, lighting and quality. Reply with EXACTLY two lines:\n"
        "OK: yes or no\n"
        "FIX: if no — ONE concrete imperative sentence stating what to correct, naming each subject "
        "precisely by appearance and position in the image (e.g. 'Remove the boy in the blue shirt, "
        "second from the right'; 'Redraw the whole scene as a 3D animated cartoon'); if yes — the word none"
    )

    def _verify_image(self, request_text, img_b64):
        """(ok, fix) — the vision model compares the produced image to what was asked.

        Fails open, deliberately: the QA loop is a courtesy and must never be the reason a
        correctly-rendered image is thrown away.

        But it used to fail open INVISIBLY, and by accident. The verdict was scored as the ABSENCE
        of a substring:

            ok = not re.search(r"^\\s*OK:\\s*no\\b", out, re.I | re.M)

        so anything that did not literally contain a line starting `OK: no` counted as a pass —
        `**OK:** no`, a preamble that pushed the verdict past num_predict, or the model answering
        the question correctly in prose ("No, the image does not contain a basketball."). Measured
        on this box: the same model asked a colour question with no format constraint returned
        'The user wants me to identify the dominant color...' and nothing else, inside a 60-token
        budget. Under the old rule that was a PASS.

        Now the verdict must be stated. An unparseable reply is still a pass — the risk posture is
        unchanged — but it is COUNTED, so the pass rate stops being unfalsifiable.
        """
        try:
            out = self._generate(
                {"model": self.vision_model, "system": self._VERIFY_SYS,
                 "prompt": f"Request: {request_text}\nDoes the image satisfy every hard requirement?",
                 "images": [img_b64], "stream": False, "think": False, "keep_alive": 0,
                 "options": {"temperature": 0.1, "num_predict": 220}},
                fmt=_VERIFY_FORMAT, timeout=300).strip()
            # JSON first, regex second, on the SAME raw text: a model that ignored the grammar, or
            # a disabled schema, lands on the old path rather than failing.
            d = _extract_json(out)
            if isinstance(d, dict) and isinstance(d.get("ok"), bool):
                fix = str(d.get("fix") or "").strip()
                return d["ok"], ("" if fix.lower().startswith("none") else fix)
            return self._parse_verdict(out)
        except Exception:
            return True, ""

    def _verify_edit(self, original_ask, instruction, ref_b64, result_b64):
        """(ok, fix) for an EDIT — the checker sees BOTH images (original first, result
        second) and judges against the user's ORIGINAL ask, not the rewritten instruction.

        This is the check the father+son incident sailed through: the old single-image QA
        verified the result against its own (possibly derailed) instruction, so an edit
        that swapped the subjects entirely passed with qa_rounds=0. Judging adherence to
        the original ask AND preservation against the original image catches both a bad
        rewrite and a wrong reference. Falls back to the single-image check without the
        sidecar; fails open on errors, counted like _verify_image."""
        if not ms or not ref_b64:
            return self._verify_image(original_ask or instruction, result_b64)
        try:
            out = self._generate(
                {"model": self.vision_model, "system": ms.VERIFY_EDIT_SYS,
                 "prompt": ms.edit_qa_user_prompt(original_ask, instruction),
                 "images": [ref_b64, result_b64], "stream": False, "think": False,
                 "keep_alive": 0, "options": {"temperature": 0.1, "num_predict": 220}},
                fmt=_VERIFY_FORMAT, timeout=300).strip()
            d = _extract_json(out)
            if isinstance(d, dict) and isinstance(d.get("ok"), bool):
                fix = str(d.get("fix") or "").strip()
                return d["ok"], ("" if fix.lower().startswith("none") else fix)
            return self._parse_verdict(out)
        except Exception:
            return True, ""

    def _generate(self, payload, fmt=None, timeout=180):
        """POST /api/generate, optionally schema-constrained, with a one-shot self-heal.

        If the server rejects the request with something about `format` or `grammar` — a model
        whose template fights the constraint, or a future Ollama that tightens validation — retry
        once WITHOUT the schema and record it. That turns a silent degrade into a recorded,
        self-correcting call, which matters because this stack swaps models.
        """
        body = dict(payload)
        if fmt and STRUCTURED_OUTPUT:
            body["format"] = fmt
        r = requests.post(f"{self.ollama}/api/generate", json=body, timeout=timeout)
        if r.status_code != 200 and "format" in body:
            detail = (r.text or "")[:300].lower()
            if "format" in detail or "grammar" in detail or "schema" in detail:
                self._metric(job="generate", format_rejected=True, detail=detail[:160])
                body.pop("format", None)
                r = requests.post(f"{self.ollama}/api/generate", json=body, timeout=timeout)
        return (r.json().get("response") or "")

    def _parse_verdict(self, out):
        """(ok, fix) from a verifier reply. Split out so tests can drive it without a GPU."""
        # Tolerant of markdown emphasis and a full-width colon, both of which the model emits.
        m = re.search(r"^\s*\**\s*OK\**\s*[:：]\s*\**\s*(yes|no)\b", out, re.I | re.M)
        if m is None:
            self._metric(job="verify", parse="none", raw=(out or "")[:200])
            return True, ""
        ok = m.group(1).lower() == "yes"
        f = re.search(r"^\s*\**\s*FIX\**\s*[:：]\s*\**\s*(.+)$", out, re.I | re.M)
        fix = (f.group(1).strip().strip("*").strip() if f else "")
        return ok, ("" if fix.lower().startswith("none") else fix)

    def _enhance_edit(self, instruction, msgs, ref_b64=None):
        """(explicit_instruction, negative) via the local VLM, which SEES the reference image
        (this tenant ships a real projector). Grounding the rewrite in the actual picture is
        what stops it naming subjects that aren't there — the official Qwen edit enhancer is
        a VLM for exactly this reason. Vague relative asks ('a bit older') under-move the
        identity-preserving editor; explicit absolute target states move it properly.
        Falls back to the original instruction and no negative."""
        ctx = self._edit_context(msgs)
        prompt = (f"Conversation:\n{ctx}\n\nRewrite the last user request: {instruction}"
                  if ctx else f"Request: {instruction}\n\nRewrite this request.")
        rewrite_sys = ms.EDIT_REWRITE_VISION_SYS if (ms and ref_b64) else self._EDIT_REWRITE_SYS
        try:
            out = self._generate(
                {"model": self.chat_model, "system": rewrite_sys, "prompt": prompt,
                 **({"images": [ref_b64]} if ref_b64 else {}),
                 "stream": False, "think": False, "keep_alive": 0,
                 # 220 was too small before any of this: an 80-word instruction is ~110 tokens on
                 # its own, before 3-8 AVOID traits and their separators. A truncated reply loses
                 # the AVOID line first, which is exactly the silent failure below.
                 "options": {"temperature": 0.4, "num_predict": 400}},
                fmt=_EDIT_FORMAT, timeout=180).strip()
            d = _extract_json(out)
            if isinstance(d, dict) and str(d.get("edit") or "").strip():
                av = d.get("avoid")
                # `negative` is consumed downstream as a plain comma string by _build_edit_wf, so
                # joining at this boundary keeps the schema a drop-in.
                avoid_s = ", ".join(str(x).strip() for x in av if str(x).strip()) if isinstance(av, list) else ""
                if not avoid_s:
                    self._metric(job="enhance_edit", avoid_missing=True, via="schema")
                return str(d["edit"]).strip(), avoid_s
            edit = re.search(r"^\s*\**\s*EDIT\**\s*:\s*(.+)$", out, re.I | re.M)
            avoid = re.search(r"^\s*\**\s*AVOID\**\s*:\s*(.+)$", out, re.I | re.M)
            if edit:
                if not avoid:
                    # The edit lands but the negative prompt is silently dropped, so the editor
                    # runs unconstrained and nobody finds out. Count it.
                    self._metric(job="enhance_edit", avoid_missing=True)
                return edit.group(1).strip(), (avoid.group(1).strip() if avoid else "")
        except Exception:
            pass
        return instruction, ""

    def _clean_prompt(self, text):
        t = re.sub(r"^\s*(please\s+)?(can you\s+|could you\s+|i want you to\s+|i'?d like\s+(you to\s+)?)?", "", text.strip(), flags=re.I)
        # article alternation MUST be longest-first ('an' before 'a') — the engine takes the first
        # alternative that lets the (all-optional) rest match, so 'a' would eat 'an' and leave 'n …'
        t = re.sub(r"^\s*(create|generate|make|draw|sketch|paint|illustrate|design|render|produce|animate|show me|give me)\s+(an|a|the|some|me)?\s*(image|picture|photo|pic|drawing|painting|illustration|art|artwork|render|video|clip|animation|gif|footage)?\s*(of|showing|with|:)?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        return requests.post(f"{self.comfy}/upload/image",
                             files={"image": ("ref.png", raw, "image/png")}, timeout=60).json()["name"]

    def _free_vram(self):
        """Unload Ollama models and BLOCK until GPU VRAM is actually released (unload is async).
        Prevents a 22GB Gemma + 18GB Krea 2 collision on the 24GB card. Returns True if the card
        ended up empty; False if a model was still resident after the wait window (an in-flight
        chat pins it) so callers can surface 'GPU busy' rather than blindly OOM."""
        try:
            models = requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", [])
        except Exception:
            models = []
        for m in models:  # per-model try: one wedged unload must not skip the rest
            try:
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
            except Exception:
                pass
        empty = False
        for _ in range(30):  # wait up to ~30s until no models are loaded
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    empty = True
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)  # grace for the CUDA allocator to release
        return empty

    @staticmethod
    def _job_error(entry):
        """Human-readable error if the ComfyUI job failed, else None."""
        st = entry.get("status", {})
        if st.get("status_str") != "error":
            return None
        d = next((m[1] for m in st.get("messages", []) if m[0] == "execution_error"), {})
        return (f"{d.get('exception_type', 'Error')} in {d.get('node_type', '?')} — "
                f"{str(d.get('exception_message', ''))[:200]}")

    @staticmethod
    def _job_exec_secs(entry):
        """How long the job actually ran before erroring, from ComfyUI's own status timestamps.
        Queue wait is excluded, so the number means the same thing on a busy and an idle server."""
        ts = {m[0]: m[1].get("timestamp") for m in entry.get("status", {}).get("messages", [])
              if len(m) > 1 and isinstance(m[1], dict)}
        start, end = ts.get("execution_start"), ts.get("execution_error")
        return (end - start) / 1000.0 if (start and end) else None

    def _gpu_revoked(self, exec_secs):
        """True when an 'OutOfMemory' really means the container can no longer reach the GPU.

        Docker injects /dev/nvidia* through the NVIDIA legacy hook, which writes the device rule
        straight into the container's cgroup — behind systemd's back. Any `systemctl daemon-reload`
        (snapd and unattended-upgrades both trigger one) makes systemd reapply the scope's device
        policy from its own records, which never mentioned the GPU, and every container on the box
        silently loses CUDA. See NVIDIA/nvidia-docker#1730.

        Two signals separate it from a real squeeze, and neither is sufficient alone:
          * a genuine OOM dies *while* weights stream onto the card, so the job runs for seconds
            (13.5 s when this last happened here); a revoked device is refused at the first
            allocation, before any VRAM moves, in well under one second;
          * /system_stats keeps reporting the card as near-empty, because ComfyUI answers that from
            the CUDA context it opened before the revocation.
        Anything ambiguous falls through to the wedged-allocator message, which is the safe side:
        its advice (restart the container) happens to be the cure for both."""
        if exec_secs is None or exec_secs > 3.0:
            return False
        free = self._vram_free_gib()
        return free is not None and free > 8

    def _fetch_node_output(self, entry, node):
        items = entry.get("outputs", {}).get(node, {}).get("images", [])
        if not items:
            return None
        it = items[0]
        return requests.get(f"{self.comfy}/view",
            params={"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": "output"},
            timeout=120).content

    def _comfy_lost_job(self, pid):
        """True if pid is in neither the running nor the pending ComfyUI queue — it vanished
        (crash/restart), so waiting out the full poll budget is pointless. False when unsure."""
        try:
            q = requests.get(f"{self.comfy}/queue", timeout=10).json()
            ids = {str(x[1]) for x in q.get("queue_running", []) + q.get("queue_pending", []) if len(x) > 1}
            return str(pid) not in ids
        except Exception:
            return False

    def _submit_poll(self, wf, out_node, kind, iters, extra_nodes=()):
        """Submit a ComfyUI workflow and poll to completion.
        Returns (data, err, extras): bytes of out_node's first output, an error string, and a
        {node: bytes} dict for extra_nodes (e.g. a last-frame SaveImage for multi-shot handoff).
        Retries ONCE on GPU OOM: OpenWebUI can invoke the 20GB chat LLM (e.g. to generate the
        chat title on a brand-new chat) AFTER our ComfyUI job has started, stealing the VRAM
        out from under the sampler. Freeing again and resubmitting wins the second time."""
        err = None
        for attempt in (1, 2):
            try:
                pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
            except Exception as e:
                return None, f"⚠️ {kind} backend error: {e}", {}
            err = None
            exec_secs = None  # how long the failing job ran — tells a real OOM from a lost GPU
            missing = 0  # consecutive polls where the job is absent from history / the GET failed
            for _ in range(iters):
                try:
                    h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
                except Exception:
                    missing += 1
                    if missing >= 6 and self._comfy_lost_job(pid):
                        return None, f"⚠️ {kind}: ComfyUI is unreachable (crash/restart?).", {}
                    time.sleep(2); continue
                if pid not in h:
                    missing += 1
                    if missing >= 6 and self._comfy_lost_job(pid):
                        return None, f"⚠️ {kind}: ComfyUI lost the job (crash/restart?).", {}
                    time.sleep(2); continue
                missing = 0
                err = self._job_error(h[pid])
                if err:
                    exec_secs = self._job_exec_secs(h[pid])
                    break
                # Fetch the finished output INSIDE the poll's protected region: a transient /view
                # error must re-poll (the render persists in history), not discard a multi-minute job.
                try:
                    data = self._fetch_node_output(h[pid], out_node)
                    if data is None:
                        return None, f"{kind} finished but produced no output.", {}
                    extras = {n: self._fetch_node_output(h[pid], n) for n in extra_nodes}
                    return data, None, extras
                except Exception:
                    time.sleep(2); continue
            if err is None:
                return None, f"⏳ Timed out waiting for the {kind.lower()}.", {}
            if attempt == 1 and ("OutOfMemory" in err or "out of memory" in err.lower()):
                # ComfyUI-side OOM: unload BOTH the ollama models AND ComfyUI's own models before the
                # single retry (the original only freed ollama, so a ComfyUI residual/allocator issue
                # re-failed identically). A revoked GPU is the exception — freeing VRAM cannot give
                # the container its device back, so skip a retry that costs ~50 s of unload waits to
                # fail identically.
                if self._gpu_revoked(exec_secs):
                    break
                self._free_vram()
                self._comfy_free()
                continue
            break
        # An OOM with the card reportedly near-empty is one of two different faults. Name the right
        # one: the remedy is the same but the follow-up is not, and telling someone their allocator
        # is wedged sends them hunting for a ComfyUI bug that isn't there.
        if err and "OutOfMemory" in err:
            free = self._vram_free_gib()
            if self._gpu_revoked(exec_secs):
                return None, (f"⚠️ {kind} failed: ComfyUI's container has lost access to the GPU. CUDA "
                              f"refused the very first allocation (job died in {exec_secs:.2f} s) while "
                              f"the card still reads {free:.0f} GB free — that is a revoked device, not "
                              f"a memory shortage. A `systemctl daemon-reload` on the host strips the "
                              f"GPU out of every container's cgroup (NVIDIA/nvidia-docker#1730). Fix: "
                              f"`docker restart comfyui`, then try again."), {}
            if free is not None and free > 8:
                return None, (f"⚠️ {kind} failed: ComfyUI reported out-of-memory but {free:.0f} GB was "
                              f"free — its GPU allocator is wedged (common after long uptime). Fix: "
                              f"`docker restart comfyui`, then try again."), {}
        return None, f"⚠️ {kind} generation failed: {err}", {}

    # Fallback enhancement contract if the media_session sidecar is missing. The shipped
    # version (ms.T2I_ENHANCE_SYS) follows the RedCraft/Krea-2 guidance: photographic
    # register by default, style words ONLY when the user names one, and NO style examples
    # in the prompt. The old prompt contained a literal cartoon example, and the model
    # copied it — "a cat jumping off a burning building" (no style named) was generated as
    # "a vibrant 3D animated cartoon-style illustration". That example is gone for good.
    _T2I_ENHANCE_FALLBACK = (
        "You expand a user's idea into ONE natural-language prompt for a photorealistic "
        "image model. Keep every subject, count, age, gender, ethnicity, object and action "
        "exactly as stated — never add or substitute subjects. Elaborate ONLY along "
        "photographic axes (setting, composition, lighting, materials, camera and lens). "
        "Use style words (cartoon, anime, painting) ONLY if the user used them. Under 100 "
        "words. Output ONLY the prompt text."
    )

    def _enhance(self, prompt):
        """Expand a short idea into a vivid image prompt via the local LLM. Falls back to the original."""
        sys = ms.T2I_ENHANCE_SYS if ms else self._T2I_ENHANCE_FALLBACK
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 220}}, timeout=180)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

    def _enhance_video(self, prompt):
        """Expand a terse idea ('guy shooting hoops') into a detailed video prompt. Wan needs the
        action spelled out step by step — terse prompts give tiny hoops and balls falling from the
        sky. Uses the big chat model briefly; _free_vram clears it before ComfyUI starts."""
        if len(prompt.split()) > 40:
            return prompt  # already detailed
        sys = ("You write prompts for the Wan text-to-video model. Expand the user's idea into ONE "
               "vivid video prompt: the subject and their appearance, the action described step by "
               "step in the order it happens, the setting, camera angle and movement, lighting and "
               "style. The user's explicit specifications are HARD requirements: every gender, age, "
               "ethnicity, person count and named object MUST be kept exactly and stated explicitly "
               "('a man' stays 'a man' — never a generic 'rider'/'person' the model can recast as "
               "someone else). Be physically precise about sizes, proportions, distances, and how "
               "objects move and interact (e.g. a basketball hoop is 3 m high; the ball leaves the "
               "player's hands, arcs through the air, and drops through the net). Keep it under 90 "
               "words. Output ONLY the prompt text — no preamble, no quotes.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.7, "num_predict": 250}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"')
            return out or prompt
        except Exception:
            return prompt

    def _build_edit_wf(self, instruction, name, seed, cfg, steps, negative, lightning=False):
        # The speed LoRA was previously rejected here on the belief that it made new elements look
        # pasted-on — mismatched lighting and grain. Re-measured 2026-08-01 (EDIT_TIERS above) that
        # premise is backwards: on an "add an object" edit the full 20-step/cfg-4 path REGENERATES
        # more of the photo than it is asked to (1 seed in 3 recomposed the whole frame, wood MAE
        # 30.9) and smooths the source's grain to 84.5%, while the LoRA holds it at ~100%.
        #
        # What the LoRA does cost is real but narrower: slightly waxier micro-texture on the newly
        # synthesized object, and — proven byte-identical output with and without one — a totally
        # INERT negative prompt, because the tier runs cfg 1.0. Callers that depend on the negative
        # must pass lightning=False.
        wf = {
          "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "Qwen-Image-Edit-2509-Q4_K_M.gguf"}},
          "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
          "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
          "v":    {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
          "ld":   {"class_type": "LoadImage", "inputs": {"image": name}},
          "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
          "pos":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": instruction}},
          "neg":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": negative}},
          "enc":  {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}},
          "k":    {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                      "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                      "model": ["cfgn", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["enc", 0]}},
          "d":    {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
          "s":    {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui_edit", "images": ["d", 0]}},
        }
        if lightning:  # speed LoRA slots between the raw unet and ModelSamplingAuraFlow
            wf["lora"] = {"class_type": "LoraLoaderModelOnly",
                          "inputs": {"model": ["u", 0], "lora_name": EDIT_LORA, "strength_model": 1.0}}
            wf["msaf"]["inputs"]["model"] = ["lora", 0]
        return wf

    def _edit_tier(self):
        """The configured tier, falling back to the full path on anything unrecognised."""
        return EDIT_TIERS.get(str(getattr(self.valves, "EDIT_QUALITY", "")).strip().lower(),
                              EDIT_TIERS["best"])

    def _build_t2i_wf(self, prompt, seed):
        # RedCraft (Krea 2 base) text-to-image (8-step; negative is zeroed conditioning, cfg 1).
        # krea2_turbo_fp8_scaled.safetensors stays on disk beside it — swap back to revert.
        # LoRA + size mirror the "Image" pipe's valves so both produce the same subject (F14).
        model_ref = ["u", 0]
        wf = {
          "u":   {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2/redcraft23INT8INT4FP8_30Krea2.safetensors", "weight_dtype": "default"}},
          "c":   {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
          "v":   {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
          "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["c", 0]}},
          "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
          "5":   {"class_type": "EmptySD3LatentImage", "inputs": {"width": IMG_T2I_W, "height": IMG_T2I_H, "batch_size": 1}},
          "8":   {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["v", 0]}},
          "9":   {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if IMG_T2I_LORA.strip():
            wf["lora"] = {"class_type": "LoraLoaderModelOnly",
                          "inputs": {"model": ["u", 0], "lora_name": IMG_T2I_LORA.strip(),
                                     "strength_model": IMG_T2I_LORA_STRENGTH}}
            model_ref = ["lora", 0]
        wf["3"] = {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0,
                    "model": model_ref, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["5", 0]}}
        return wf

    def _metric(self, **fields):
        """Append one JSON line describing a finished media job. Never raises, never blocks a reply.

        Deliberately a flat file rather than a counter: the useful questions are "how often does QA
        correct, and what did that cost" and "which requests does it keep flagging", and both need
        the individual rows. OpenWebUI's data volume is the only writable persistent path the pipe
        has inside its container."""
        if not METRICS_PATH:
            return
        try:
            fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with open(METRICS_PATH, "a") as f:
                f.write(json.dumps(fields, default=str) + "\n")
        except Exception:
            pass  # instrumentation must never cost a user their generation

    def _route_metric(self, route, tier, rule_id, text=None, **extra):
        """One job:'route' row per routed turn — which branch won, at which tier, on which rule.

        This is the answer to a question that used to be unanswerable after the fact: WHICH rule
        routed a given message, and would the message have routed at all without it. Every
        heuristic in this file that guessed has needed measuring and walking back, and until now
        the measuring started only after the incident. Tier vocabulary matches the roadmap:
        0 = explicit (slash/marker/manage-follow-up), 1 = STRONG regex, 3 = the 1B classifier.

        The request text rides along truncated: enough to adjudicate a misroute from the log
        alone, short enough that the file does not become a transcript of every conversation.
        """
        if text:
            extra["request"] = text[:200]
        self._metric(job="route", route=route, tier=tier, rule_id=rule_id, **extra)

    def _gen_image(self, prompt, ref_b64, msgs=None, original=None):
        # `original` is the user's verbatim ask (pre-instruction-stripping) — QA judges
        # against it, never against the rewritten instruction alone: the rewrite may itself
        # be the thing that derailed. Falls back to `prompt` for older callers.
        original = original or prompt
        # Free ComfyUI's VRAM up front so the dolphin/gemma prompt-rewrite helpers below don't load
        # into a card ComfyUI still occupies (~13.7 GB) and run partly on CPU.
        self._comfy_free()
        # Attached image → instruction edit with Qwen-Image-Edit 2509.
        if ref_b64:
            instruction = prompt or "improve the overall quality, keep everything else the same"
            negative = ""
            style = self._style_conversion(prompt) if prompt else None
            # A restyle is defined by what must NOT survive it, and a boost retry exists because the
            # first attempt under-moved — both lean on the negative prompt, which only bites at
            # cfg > 1. So both stay on the full path regardless of the tier valve; the valve steers
            # the ordinary edit, which is the one that costs 152 s and happens most.
            lightning = False
            if style:  # whole-image restyle: purpose-built instruction, hard sampler push
                instruction, negative = style
                # The enricher SEES the image (before _free_vram, model still resident), so
                # the subjects it names are the ones actually in the picture.
                instruction = self._style_enrich(instruction, msgs, ref_b64=ref_b64)
                cfg, steps = 6.0, 24
            else:
                if prompt:  # rewrite BEFORE _free_vram so the model isn't unloaded and reloaded
                    instruction, negative = self._enhance_edit(instruction, msgs, ref_b64=ref_b64)
                if self._edit_boost(prompt):
                    cfg, steps = 6.0, 24
                else:
                    t = self._edit_tier()
                    cfg, steps, lightning = t["cfg"], t["steps"], t["lightning"]
            self._free_vram()
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            wf = self._build_edit_wf(instruction, name, random.randint(0, 2**31), cfg, steps,
                                     negative, lightning)
            t_render = time.time()
            data, err, _ = self._submit_poll(wf, "s", "Edit", 1200)
            if err:
                self._metric(job="edit", ok=False, render_s=round(time.time() - t_render, 1),
                             err=err[:160])
                return err
            render_s = round(time.time() - t_render, 1)
            # Vision QA: judged against the user's ORIGINAL ask, with BOTH images (original
            # first, result second) — a result that satisfied a derailed instruction but
            # swapped the subjects now fails. Up to two harder retries if not.
            qa_note = ""
            qa_rounds, first_fix = 0, None
            t_qa = time.time()
            if IMG_VERIFY and prompt:
                cur = instruction
                for _round in (1, 2):
                    self._comfy_free()
                    ok, fix = self._verify_edit(original, cur, ref_b64,
                                                base64.b64encode(data).decode())
                    if ok:
                        break
                    if not fix:
                        # QA said no and gave nothing to act on. Same control flow as before (we
                        # stop), but it used to be indistinguishable from a pass — the two exits
                        # shared one branch, so a detected-but-unfixable failure was invisible.
                        qa_note = ("\n\n*QA flagged a mismatch but returned no correction — "
                                   "the image is left as generated.*")
                        self._metric(job="edit", qa_unfixable=True, request=(instruction or "")[:160])
                        break
                    first_fix = first_fix or fix
                    cur = f"{cur} IMPORTANT correction: {fix}"
                    wf = self._build_edit_wf(cur, name, random.randint(0, 2**31), 6.0, 24, negative)
                    self._free_vram()
                    data2, err2, _ = self._submit_poll(wf, "s", "Edit", 1200)
                    if data2 is None:
                        qa_note = f"\n\n*⚠️ QA flagged: {fix} — automatic correction failed ({err2})*"
                        break
                    data = data2
                    qa_rounds += 1
            self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
            self._metric(job="edit", ok=True, render_s=render_s,
                         qa_s=round(time.time() - t_qa, 1), qa_rounds=qa_rounds,
                         qa_fix=(first_fix or "")[:200], request=(prompt or "")[:160])
            return f"![{instruction[:50]}](data:image/png;base64,{base64.b64encode(data).decode()}){qa_note}"

        # No image → fresh Krea 2 Turbo text-to-image.
        raw = prompt
        if IMG_ENHANCE and prompt:
            prompt = self._enhance(prompt)
        if IMG_T2I_LORA.strip() and IMG_T2I_TRIGGER.strip():  # LoRA trigger prefix (parity w/ Image pipe)
            prompt = f"{IMG_T2I_TRIGGER.strip()}, {prompt}"
        self._free_vram()
        wf = self._build_t2i_wf(prompt, random.randint(0, 2**31))
        t_render = time.time()
        data, err, _ = self._submit_poll(wf, "9", "Image", 360)
        if err:
            self._metric(job="image", ok=False, render_s=round(time.time() - t_render, 1),
                         err=err[:160])
            return err
        render_s = round(time.time() - t_render, 1)
        # Vision QA against the user's ORIGINAL wording (the ground truth for hard constraints).
        # Up to two correction rounds: a fresh re-roll at cfg 1 usually repeats the mistake
        # (e.g. an extra child), so FIX the produced image with the instruction editor instead —
        # it is precisely good at "remove the extra X / add the missing Y" and keeps the scene.
        qa_note = ""
        qa_rounds, first_fix = 0, None
        t_qa = time.time()
        if IMG_VERIFY and raw:
            for _round in (1, 2):
                self._comfy_free()
                ok, fix = self._verify_image(raw, base64.b64encode(data).decode())
                if ok:
                    break
                if not fix:
                    qa_note = ("\n\n*QA flagged a mismatch but returned no correction — "
                               "the image is left as generated.*")
                    self._metric(job="t2i", qa_unfixable=True, request=(raw or "")[:160])
                    break
                first_fix = first_fix or fix
                try:
                    fix_ref = self._upload(base64.b64encode(data).decode())
                except Exception as e:
                    qa_note = f"\n\n*⚠️ QA flagged: {fix} — auto-correction could not upload ({e})*"
                    break
                self._free_vram()
                wf = self._build_edit_wf(f"{fix} Keep everyone else and the scene exactly the same.",
                                         fix_ref, random.randint(0, 2**31), 4.0, 20, "")
                data2, err2, _ = self._submit_poll(wf, "s", "Edit", 1200)
                if data2 is None:
                    qa_note = f"\n\n*⚠️ QA flagged: {fix} — automatic correction failed ({err2})*"
                    break
                data = data2
                qa_rounds += 1
        self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
        # A correction here costs a full Qwen-Image-Edit pass (~150 s) on top of a ~16 s Krea 2
        # render, so qa_rounds is the field that explains a slow "make me a picture".
        self._metric(job="image", ok=True, render_s=render_s,
                     qa_s=round(time.time() - t_qa, 1), qa_rounds=qa_rounds,
                     qa_fix=(first_fix or "")[:200], request=(raw or "")[:160])
        return f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()}){qa_note}"

    # Standard Wan negative prompt (recommended by the model authors).
    _VID_NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
                "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
                "形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")

    def _wf_video_5b(self, prompt, seed, w=V_W, h=V_H, length=V_LEN_5B):
        """Wan 2.2 TI2V 5B — fast (~90 s) but weak at complex human action. Honors the per-request
        width/height/length (the 5B supports 1280x704) instead of always emitting 832x480x49."""
        return {
          "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
          "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
          "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
          "5": {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["2", 0]}},
          "6": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": w, "height": h, "length": length, "batch_size": 1}},
          "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
          "8": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": V_STEPS_5B, "cfg": 5.0,
                    "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                    "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
          "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
          "save": {"class_type": "SaveWEBM", "inputs": {"images": ["9", 0], "filename_prefix": "owui_vid", "codec": "vp9", "fps": float(V_FPS_5B), "crf": 32.0}},
        }

    def _expert_chain(self, wf, tag, unet, lora, strength, riflex_latent=None):
        """One Wan expert: GGUF unet → Lightning LoRA → SageAttention patch → torch.compile →
        ModelSamplingSD3(shift 5) → optional RifleX RoPE (for >81-frame clips).
        Returns the model ref to feed a sampler. Sage/compile are A14B-ONLY: sage produces
        pure noise on the TI2V 5B and breaks Krea/Qwen — never move these into other graphs."""
        wf[f"u{tag}"] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}}
        src = [f"u{tag}", 0]
        if lora:
            wf[f"l{tag}"] = {"class_type": "LoraLoaderModelOnly",
                             "inputs": {"model": src, "lora_name": lora, "strength_model": strength}}
            src = [f"l{tag}", 0]
        if V_SAGE:
            wf[f"sg{tag}"] = {"class_type": "PathchSageAttentionKJ",  # (sic — upstream typo)
                              "inputs": {"model": src, "sage_attention": "auto", "allow_compile": True}}
            src = [f"sg{tag}", 0]
        if V_COMPILE:
            wf[f"tc{tag}"] = {"class_type": "TorchCompileModel",
                              "inputs": {"model": src, "backend": "inductor"}}
            src = [f"tc{tag}", 0]
        wf[f"m{tag}"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": src, "shift": 5.0}}
        src = [f"m{tag}", 0]
        if riflex_latent is not None:
            wf[f"rx{tag}"] = {"class_type": "ApplyRifleXRoPE_WanVideo",
                              "inputs": {"model": src, "latent": riflex_latent, "k": 6}}
            src = [f"rx{tag}", 0]
        return src

    def _post_chain(self, wf, frames_ref, length, last_frame=False, color_ref=None):
        """Shared tail: optional ColorMatch (multi-shot grading anchor) → RIFE 2x interpolation
        (16→32 fps; the SaveWEBM fps MUST scale with the multiplier) → SaveWEBM; optionally also
        saves the clip's last frame as a PNG for multi-shot handoff."""
        src = frames_ref
        if color_ref is not None:
            wf["cm"] = {"class_type": "ColorMatch",
                        "inputs": {"image_ref": color_ref, "image_target": src, "method": "mkl"}}
            src = ["cm", 0]
        wf["rife"] = {"class_type": "RIFE VFI", "inputs": {
            "ckpt_name": "rife47.pth", "frames": src, "clear_cache_after_n_frames": 10,
            "multiplier": V_RIFE, "fast_mode": False, "ensemble": True, "scale_factor": 1.0,
            "dtype": "float32", "torch_compile": False, "batch_size": 1}}
        wf["save"] = {"class_type": "SaveWEBM", "inputs": {
            "images": ["rife", 0], "filename_prefix": "owui_vid", "codec": "vp9",
            "fps": float(V_FPS_14B * V_RIFE), "crf": 30.0}}
        if last_frame:
            wf["lastf"] = {"class_type": "ImageFromBatch",
                           "inputs": {"image": src, "batch_index": length - 1, "length": 1}}
            wf["savef"] = {"class_type": "SaveImage",
                           "inputs": {"images": ["lastf", 0], "filename_prefix": "owui_lastframe"}}

    def _wf_video_14b(self, prompt, seed, w=V_W, h=V_H, length=V_LEN_14B, last_frame=False):
        """Wan 2.2 T2V A14B two-expert + Lightning 250928 LoRAs, 6 steps with the motion recipe:
        the HIGH stage runs cfg 3 / LoRA 0.8 (recovers base-model motion strength and prompt
        adherence that cfg-1 distillation kills — and makes the negative prompt ACTIVE there);
        the LOW stage runs the standard cfg 1 Lightning finish."""
        wf = {
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "vae":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        }
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["clip", 0]}}
        wf["lat"] = {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": w, "height": h, "length": length, "batch_size": 1}}
        rif = ["lat", 0] if length > V_LEN_14B else None
        mh = self._expert_chain(wf, "h", "wan2.2/Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_T2V_A14B_4step_HIGH_250928.safetensors", V_HIGH_LORA, rif)
        ml = self._expert_chain(wf, "l", "wan2.2/Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_T2V_A14B_4step_LOW_250928.safetensors", 1.0, rif)
        split = V_STEPS_14B // 2
        wf["s1"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "enable", "noise_seed": seed,
                    "steps": V_STEPS_14B, "cfg": V_HIGH_CFG, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 0, "end_at_step": split, "return_with_leftover_noise": "enable",
                    "model": mh, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}}
        wf["s2"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "disable", "noise_seed": seed,
                    "steps": V_STEPS_14B, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": split, "end_at_step": 10000, "return_with_leftover_noise": "disable",
                    "model": ml, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["s1", 0]}}
        wf["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["s2", 0], "vae": ["vae", 0]}}
        self._post_chain(wf, ["dec", 0], length, last_frame=last_frame)
        return wf

    def _wf_video_i2v(self, prompt, seed, w, h, length, start_name, ref_name):
        """Wan 2.2 I2V A14B continuation shot: starts from the previous shot's last frame,
        ColorMatched against the sequence's reference frame (colors drift when chaining —
        known Wan issue). Standard I2V Lightning Seko-V1 recipe: 4 steps (2+2), cfg 1."""
        wf = {
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "vae":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        }
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["clip", 0]}}
        wf["simg"] = {"class_type": "LoadImage", "inputs": {"image": start_name}}
        wf["i2v"] = {"class_type": "WanImageToVideo", "inputs": {
            "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
            "width": w, "height": h, "length": length, "batch_size": 1, "start_image": ["simg", 0]}}
        mh = self._expert_chain(wf, "h", "wan2.2/Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_I2V_A14B_4step_HIGH_SekoV1.safetensors", 1.0)
        ml = self._expert_chain(wf, "l", "wan2.2/Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_I2V_A14B_4step_LOW_SekoV1.safetensors", 1.0)
        wf["s1"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "enable", "noise_seed": seed,
                    "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 0, "end_at_step": 2, "return_with_leftover_noise": "enable",
                    "model": mh, "positive": ["i2v", 0], "negative": ["i2v", 1], "latent_image": ["i2v", 2]}}
        wf["s2"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "disable", "noise_seed": seed,
                    "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 2, "end_at_step": 10000, "return_with_leftover_noise": "disable",
                    "model": ml, "positive": ["i2v", 0], "negative": ["i2v", 1], "latent_image": ["s1", 0]}}
        wf["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["s2", 0], "vae": ["vae", 0]}}
        wf["refimg"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        self._post_chain(wf, ["dec", 0], length, last_frame=True, color_ref=["refimg", 0])
        return wf

    # ---------- video request options / multi-shot ----------
    _LEN_KW = re.compile(r"\b(long(er)?|extend|lengthen)\b(\s+(the\s+)?(video|clip|it))?|"
                         r"\bmake\s+(it|the\s+(video|clip))\s+longer\b|\b[6-9]\s*seconds?\b|"
                         r"\b1[0-9]\s*seconds?\b", re.I)

    def _video_opts(self, text, base=None):
        """Per-request controls parsed from the message: '720p'/'hq' → native 720p,
        'quick/fast/draft video' → 5B fast path, 'longer'/'extend'/'7 seconds' → RifleX 121f.
        When `base` is given (a follow-up on an existing clip) start from it and override ONLY the
        fields the new message explicitly names, so the original clip's resolution/length carry over."""
        t = (text or "").lower()
        o = dict(base) if base else {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}
        if re.search(r"\b(720p|1080p|hq|high[- ]?quality|high[- ]?res(olution)?)\b", t):
            o["w"], o["h"] = V_W_HQ, V_H_HQ
        if re.search(r"\b(quick|fast|draft)\s+(video|clip)\b", t):
            o["fast"] = True
        if self._LEN_KW.search(t):
            o["length"] = V_LEN_LONG
        return o

    def _is_length_only(self, text):
        """True if the message is purely a duration change ('make it longer', 'extend it',
        '10 seconds') with no new visual content — re-render the SAME prompt+seed at the new
        length instead of re-merging (which would perturb the scene)."""
        t = (text or "").lower().strip()
        if not self._LEN_KW.search(t):
            return False
        rest = re.sub(r"\b(make|it|the|this|video|clip|please|a|bit|much|way|can|you|could|would|"
                      r"long(er)?|extend|lengthen|to|now|and|of|seconds?|secs?|s)\b|\d+|[^\w\s]", " ", t)
        return not re.search(r"[a-z]{3,}", rest)

    def _strip_video_directives(self, t):
        t = re.sub(r"\b(in\s+)?(720p|1080p|hq|high[- ]?quality|high[- ]?res(olution)?)\b", "", t, flags=re.I)
        t = re.sub(r"\b(quick|fast|draft)\s+(video|clip)\b", "video", t, flags=re.I)
        return re.sub(r"\s{2,}", " ", t).strip()

    def _wants_multishot(self, t):
        """Return the shot count for long/sequenced requests (0 = single shot).
        Triggers on '15 second video' (>=10 s) or 'X then Y' sequencing."""
        t = (t or "").lower()
        m = re.search(r"\b(\d{1,3})\s*(?:seconds?|secs?)\b", t)
        if m and int(m.group(1)) >= 10:
            return min(V_SHOT_MAX, max(2, round(int(m.group(1)) / 5)))
        if re.search(r"\bthen\b", t):
            return min(V_SHOT_MAX, max(2, len(re.split(r"\bthen\b", t))))
        return 0

    def _plan_shots(self, text, n):
        """Ask the local LLM to direct the request as n consecutive 5-second shots, each written
        as a full video prompt with consistent subjects across shots. Returns list[str] or None."""
        sys = (f"You are a film director planning {n} CONSECUTIVE 5-second shots for a text-to-video "
               "model. Each shot continues exactly where the previous one ended (same subjects, "
               "same look, continuous action). Write each shot as ONE detailed video prompt: subject "
               "and appearance (repeat it in every shot for consistency), the action step by step, "
               "setting, camera, lighting. Under 60 words per shot. "
               f"Return STRICT JSON only: an array of exactly {n} strings.")
        # Two attempts. The first pays the ~23 s cold load; a retry reuses the warm runner and is
        # far cheaper than the alternative, which is silently shipping a 5-second clip when a
        # 15-second sequence was asked for.
        for attempt, temp in ((1, 0.6), (2, 0.2)):
            try:
                out = self._generate(
                    {"model": self.chat_model, "system": sys, "prompt": text, "stream": False,
                     "think": False, "keep_alive": 0,
                     "options": {"temperature": temp, "num_predict": 1400}},
                    fmt=_SHOTS_FORMAT, timeout=300)
                parsed = _extract_json(out)
                if isinstance(parsed, dict):          # a model that wrapped the array in an object
                    parsed = next((v for v in parsed.values() if isinstance(v, list)), None)
                if isinstance(parsed, list):
                    shots = [str(s).strip() for s in parsed if str(s).strip()]
                    if len(shots) >= 2:
                        if len(shots) < n:
                            self._metric(job="plan_shots", short=True, asked=n, got=len(shots))
                        return shots[:min(n, V_SHOT_MAX)]
            except Exception:
                pass
            self._metric(job="plan_shots", parse="none", attempt=attempt)
        return None

    def _concat_webms(self, segs):
        """Join segment webms into one clip with the ffmpeg inside this (OpenWebUI) container.
        Tries stream-copy first (same codec/params), falls back to re-encoding."""
        import os, shutil, subprocess, tempfile
        d = tempfile.mkdtemp(prefix="vidcat_")
        try:
            paths = []
            for i, b in enumerate(segs):
                p = os.path.join(d, f"seg{i}.webm")
                open(p, "wb").write(b)
                paths.append(p)
            lst = os.path.join(d, "list.txt")
            open(lst, "w").write("".join(f"file '{p}'\n" for p in paths))
            out = os.path.join(d, "out.webm")
            r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out],
                               capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                                    "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", out],
                                   capture_output=True, timeout=1800)
                if r.returncode != 0:
                    return None
            return open(out, "rb").read()
        except Exception:
            return None  # e.g. no ffmpeg binary in this environment
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _gen_multishot(self, text, seed, opts, n):
        """Plan → shot 1 (T2V) → shots 2..n (I2V from the previous last frame, ColorMatched to
        shot 1) → ffmpeg concat. Returns the chat reply string, or None to fall back to a
        single-shot generation."""
        self._comfy_free()  # free ComfyUI before the dolphin shot-planning helper loads
        shots = self._plan_shots(text, n)
        if not shots:
            return None
        self._free_vram()
        segs, ref_name, prev_name = [], None, None
        for i, shot in enumerate(shots):
            def build(p):
                if i == 0:
                    return self._wf_video_14b(p, seed, opts["w"], opts["h"], V_LEN_14B, last_frame=True)
                return self._wf_video_i2v(p, seed + i, opts["w"], opts["h"], V_LEN_14B, prev_name, ref_name)
            data, err, extras = self._submit_poll(build(shot), "save", f"Video (shot {i+1}/{len(shots)})", 900,
                                                  extra_nodes=("savef",))
            if err:
                return f"{err}\n\n(multi-shot sequence failed at shot {i+1}/{len(shots)})"
            # Frame QA per shot, BEFORE its last frame seeds the next shot — a wrong shot would
            # otherwise propagate down the whole chain via the I2V handoff. The retried shot
            # replaces both the segment AND the handoff frame. In "anchors" mode only the
            # chain-critical shots (first = identity/style anchor, last = final frame) are verified;
            # intermediate shots inherit shot 1's look via the I2V handoff, so re-running the full
            # Wan stack + gemma4 on every one of them mostly burns time for little gain.
            verify_shot = VID_VERIFY and (VID_VERIFY_MODE == "all"
                                          or i == 0 or i == len(shots) - 1)
            if verify_shot:
                frame = self._video_frame(data)
                if frame:
                    self._comfy_free()
                    ok, fix = self._verify_image(shot, frame)
                    if not ok and fix:
                        self._free_vram()
                        data2, err2, extras2 = self._submit_poll(
                            build(f"{shot} IMPORTANT: {fix}"), "save",
                            f"Video (shot {i+1}/{len(shots)} retry)", 900, extra_nodes=("savef",))
                        if data2 is not None:
                            data, extras = data2, extras2
            segs.append(data)
            fb = extras.get("savef")
            if fb is None:
                return f"⚠️ Shot {i+1} produced no handoff frame for the next shot."
            try:
                prev_name = self._upload(base64.b64encode(fb).decode())
            except Exception as e:
                return f"⚠️ Could not hand shot {i+1}'s last frame to shot {i+2}: {e}"
            if ref_name is None:
                ref_name = prev_name
        out = self._concat_webms(segs)
        if not out:
            return "⚠️ Generated all shots but could not join them into one clip."
        b64 = base64.b64encode(out).decode()
        joined = " || ".join(shots)
        p64 = base64.b64encode(joined.encode()).decode()
        return (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                f'controls loop muted playsinline '
                f'style="max-width:100%;border-radius:8px">\n'
                f'data:video/webm;base64,{b64}\n</video>\n\n'
                f'*🎬 {len(shots)}-shot sequence — {shots[0][:60]}…*')

    def _video_frame(self, webm_bytes):
        """Middle-ish frame of a webm as base64 PNG, via the ffmpeg inside this container.
        Returns None on any failure (QA then just skips)."""
        import os, subprocess, tempfile
        d = tempfile.mkdtemp(prefix="vidqa_")
        try:
            vid = os.path.join(d, "clip.webm")
            open(vid, "wb").write(webm_bytes)
            png = os.path.join(d, "frame.png")
            r = subprocess.run(["ffmpeg", "-y", "-ss", "2", "-i", vid, "-vframes", "1", png],
                               capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(png):  # clip shorter than 2 s → first frame
                r = subprocess.run(["ffmpeg", "-y", "-i", vid, "-vframes", "1", png],
                                   capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.exists(png):
                    return None
            return base64.b64encode(open(png, "rb").read()).decode()
        except Exception:
            return None
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _clip_frames(opts):
        """How many frames this clip will ACTUALLY contain.

        The 5B fast path ignores opts['length'] and renders V_LEN_5B unless 'longer' was asked for.
        Reported separately from opts['length'] the two drifted, and the status line claimed 81f on
        clips that were really 49f — so both the workflow builder and the status line call this.
        """
        if not opts or not opts.get("fast"):
            return (opts or {}).get("length", V_LEN_14B)
        return V_LEN_LONG if opts.get("length", 0) >= V_LEN_LONG else V_LEN_5B

    def _gen_video(self, prompt, seed=None, opts=None, check=None):
        seed = seed if seed is not None else random.randint(0, 2**31)
        opts = opts or {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}

        def build(p):
            if V_QUALITY == "best" and not opts.get("fast"):
                return self._wf_video_14b(p, seed, opts["w"], opts["h"], opts["length"])
            # 5B fast path now honors the parsed resolution; length only stretches on explicit 'longer'
            return self._wf_video_5b(p, seed, opts["w"], opts["h"], self._clip_frames(opts))

        self._free_vram()
        data, err, _ = self._submit_poll(build(prompt), "save", "Video", 900)
        if err:
            return err
        # Frame QA: does the clip match the request (gender, subject, setting)? One corrected
        # retry with the fix appended — same seed, so the scene stays recognizably similar.
        if VID_VERIFY and check:
            frame = self._video_frame(data)
            if frame:
                self._comfy_free()
                ok, fix = self._verify_image(check, frame)
                if not ok and fix:
                    self._free_vram()
                    data2, err2, _ = self._submit_poll(build(f"{prompt} IMPORTANT: {fix}"), "save", "Video", 900)
                    if data2 is not None:
                        data = data2
        b64 = base64.b64encode(data).decode()
        # `video` is NOT a markdown block-level tag, so the opening <video> tag MUST sit alone on
        # its line (CommonMark "type-7" HTML block) for marked to keep the whole element in ONE
        # html token; OpenWebUI then reads the src from the text between the tags
        # (/<video[^>]*>([\s\S]*?)<\/video>/). If content shares the tag's line it splits into
        # inline tokens, the regex misses, and OpenWebUI dumps the raw tag as escaped text.
        # data-p64/data-seed make the message self-describing so a follow-up "change the X"
        # can rebuild this exact clip's prompt+seed even after a restart.
        p64 = base64.b64encode(prompt.encode()).decode()
        return (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                f'controls loop muted playsinline '
                f'style="max-width:100%;border-radius:8px">\n'
                f'data:video/webm;base64,{b64}\n</video>\n\n*🎬 {prompt[:80]}*')

    # What the hermes agent is told when a background task is delegated. This is the contract that
    # keeps unattended jobs deliverable and bounded; the agent writes the actual job prompt, but
    # every job it creates must satisfy these rules.
    # 5d sub-rules, in ship order: i = no-false-confirmation, ii = price_search (no URL),
    # iii = price_watch --mode stock, iv = the next one (a fare extractor is the expected claimant).
    # This whole brief is ONE string literal deployed as one webui.db row, so two branches editing
    # it will conflict; allocate the number here first. tests/test_deployed.py is the backstop.
    _HERMES_BRIEF = (
        "You are the background-task manager for a local OpenWebUI assistant. The user's request "
        "was routed to you because it asks for a standing job (monitoring, scheduled checks, "
        "reminders) or to manage existing ones. Use your cronjob tool.\n"
        "Rules for every job you create:\n"
        "1. The job must be BOUNDED: honour the user's duration (e.g. 'for 2 weeks' => an end "
        "condition or repeat count). If no duration was given, default to 7 days and say so.\n"
        "2. Pick a sensible interval if the user gave none (price checks: every 6 hours).\n"
        "3. The job's prompt must be self-contained. When rule 5d, 5d-ii or 5d-iii applies, its vetted "
        "command IS the entire prompt; otherwise: exact URLs or curl commands to fetch (the "
        "local SearXNG at http://127.0.0.1:8888/search?q=...&format=json is available for "
        "searching), what to extract, and what counts as noteworthy. When fetching a retail page, "
        "use plain urllib WITHOUT a fake browser User-Agent — measured on this host, Amazon "
        "returns the full page to a plain request and serves a robot wall to a spoofed Chrome UA. "
        "Extract prices defensively (search all currency-like matches, never assume one regex "
        "matches) and if extraction fails, say so in the LOG line rather than crashing. "
        "Prefer https:// URLs — the "
        "security scanner blocks plain http:// in terminal commands; for an http-only page the "
        "job must fetch with the execute_code tool (Python urllib) instead of curl.\n"
        "4. State between runs lives in files under ~/.hermes/monitor-state/ — the job reads the "
        "previous value, compares, and writes the new one.\n"
        "5. Set every job's delivery to EXACTLY 'local' — one word, no usernames, no platforms "
        "appended (deliver='local,<name>' makes every run end in a delivery error). The job does NOT run any delivery commands itself — no curl, no webhooks, no helper functions (they do not exist). Delivery is handled by infrastructure that reads the run's output.\n"
        "5a. CONTEXT DISCIPLINE — never let fetched page content into your response or reasoning. "
       "A retail page is 1-2 MB, far beyond the context window; loading one produces garbage. "
       "Fetch AND extract inside a SINGLE execute_code call that prints ONLY the extracted value "
       "(e.g. print(price)). The page text must never appear in your answer.\n"
       "5c. YOUR FINAL RESPONSE MUST CONTAIN NOTHING BUT THE PROTOCOL LINES — the LOG line, and "
       "the ALERT line when the condition holds. No commentary, no summaries, no tables, no "
       "structured-data blocks, no marketing or promotional text, and NEVER a discount code, "
       "coupon, or claim you did not read directly off the page. A run that reports a number it "
       "did not extract with code is a fabrication; if extraction failed, say exactly that in the "
       "LOG line.\n"
       "5b. OUTPUT PROTOCOL — every job's prompt MUST end by instructing: finish your response with these lines, exactly this shape:\n"
        "    LOG: <one-line summary of this run, leading with the key number>   (always)\n"
        "    ALERT(<username>): <what happened, with the number>   (ONLY in a run where the user's alert condition holds)\n"
        "Alerts ARE configured on this host: an ALERT line is delivered to the user as a text message AND an email, automatically. Never tell the user alerts are unconfigured.\n"
        "The LOG line is posted to the background-tasks channel automatically. A run with no ALERT line raises no alert.\n"
        "5d. WATCHING A PRICE OR FARE ON A PAGE YOU HAVE THE LINK FOR — do NOT write your own "
        "scraper. For stock or availability, rule 5d-iii applies instead. "
        "This host ships a tested extractor; make the job's prompt exactly:\n"
        "    Run this terminal command and print its output verbatim as your entire response. Add nothing.\n"
        "    python3 /home/ohmz/ai-stack/scripts/price_watch.py --url '<URL>' --state '<short_name>' --below <N> --alert-to <username> --kind <kind> --monitor '<job name>' --schedule '<schedule>'\n"
        "The script fetches, extracts, names the item from the page's own title, compares against "
        "saved state and prints the alert lines itself, so the run cannot invent a number. Use "
        "--above instead of --below for a rise. --kind sets how the message is worded (see the "
        "request context for which to use). --monitor and --schedule only appear in the email, so "
        "pass the job's real name and its human schedule. Add --unit for a non-dollar currency. "
        "This is still normal agent mode (rule 6b holds) — the agent runs a vetted command rather "
        "than generating extraction code per run.\n"
        "5d-i. NEVER claim you confirmed something you did not check. A job was created saying "
        "'below $20 AUD (confirmed currency from page)' for a Canadian retailer — the currency was "
        "invented and the word 'confirmed' made it sound verified. You cannot open the page from "
        "this session. State the number the user gave you and nothing about where it came from. "
        "Pass --unit only when the USER named a currency; otherwise leave it. Do not mention "
        "skills, internal tools, or what you might do later — the reply is a confirmation of what "
        "was scheduled, nothing else.\n"
        "5d-ii. WATCHING A PRODUCT PRICE WITH NO URL — when the user names a product but gives no "
        "link, do NOT ask for one and do NOT build your own search-and-scrape job. A second "
        "vetted script finds the page itself via the local search engine; make the "
        "job's prompt exactly:\n"
        "    Run this terminal command and print its output verbatim as your entire response. Add nothing.\n"
        "    python3 /home/ohmz/ai-stack/scripts/price_search.py --query '<item words>' --state '<short_name>' --below <N> --alert-to <username> --kind <kind> --monitor '<job name>' --schedule '<schedule>'\n"
        "It searches once, picks the best product page, remembers it, and from then on behaves "
        "exactly like price_watch.py — same flags: --above for a rise, "
        "--unit only when the user named a currency, --require-confidence to refuse "
        "low-confidence alerts. Keep --query to the item's own words — no 'price of', no "
        "quotation marks or apostrophes inside the value, never a URL. If the user DID give a "
        "URL or a page address, rule 5d applies instead, never this one.\n"
        "5d-iv. FLIGHTS AND AIR FARES ARE NEVER YOURS TO SCHEDULE. The assistant's own flight "
        "path handles fare asks before you and creates its jobs directly. If a fare or flight "
        "request reaches you anyway, create NOTHING — no price_search job (--kind fare refuses "
        "itself every run), no scraper — and reply exactly: the flight watcher handles fares; "
        "please re-send the request as its own message. Never invent a fare number.\n"
        "5d-iii. WATCHING STOCK OR AVAILABILITY — 'tell me when it is back in stock', 'text me if "
        "it sells out', 'how many are left', appointment or ticket availability. The SAME vetted "
        "script does this in a different mode; do NOT write your own scraper and do NOT reach for a "
        "price threshold. Make the job's prompt exactly:\n"
        "    Run this terminal command and print its output verbatim as your entire response. Add nothing.\n"
        "    python3 /home/ohmz/ai-stack/scripts/price_watch.py --url '<URL>' --state '<short_name>' --mode stock --kind <kind> --alert-to <username> --monitor '<job name>' --schedule '<schedule>'\n"
        "--mode stock is REQUIRED here. Without it the run compares a PRICE against a threshold and "
        "a back-in-stock watch never fires. --kind is one of back_in_stock, out_of_stock, "
        "inventory, availability. Pass NO --below and NO --above unless the user asked about a "
        "COUNT ('fewer than 3 left') and --kind is inventory — a number on any other stock watch is "
        "a price threshold in disguise and is ignored. No --unit either: a stock reading has no "
        "currency. The script reads the page's OWN availability field, states on every run which "
        "signal it read and how confident it is, waits for an unconfirmed reading to repeat before "
        "it acts, and sends at most one stock alert every six hours so a page that flips in and out "
        "of stock cannot text all day. If a page does not state its availability in a readable way "
        "it reports that it could not read it — it never guesses 'out of stock'. A pre-order is not "
        "a restock and fires nothing. If the user gave no link, ask for one: the no-URL recipe in "
        "rule 5d-ii finds pages by their PRICE and would reject an out-of-stock page.\n"
        "5e. The extractor also reports its OWN failures: a dead URL, a site blocking automated "
        "checks, and a page that still loads but no longer shows a value. Never add your own "
        "error handling or retry logic around it — it already confirms a failure across runs "
        "before telling the user, and alerts once per outage rather than every run.\n"
        "6. Before creating, call cronjob(action='list') and look at STATE, not just names. Only a job that is ACTIVE and still has runs left counts as a duplicate — say so and stop. A job that is completed, exhausted, disabled or has no next run is FINISHED: it will never run again, so create a NEW one instead of pointing at it. Never describe a finished job as 'already running'.\n"
        "6a. Do that check SILENTLY. The list is for you, not for the user: never mention, name, "
        "count or summarise the other jobs you saw. The ONLY existing job you may refer to is one "
        "that is genuinely a duplicate of what was just asked for — and then only to say it is "
        "already running. 'I see an existing X watch, now creating your Y watch' is exactly the "
        "sentence not to write: the user asked about Y and did not ask what else is scheduled.\n"
        "6b. NEVER write your own script into a job and run it with --no-agent. A script YOU "
       "generate has no reasoning to recover when markup shifts, and its bugs fail silently — one "
       "such job computed its ALERT text into a variable it never printed, so the alert could "
       "never fire. Use normal agent mode. (The one exception is the pre-existing, tested "
       "extractors named in rules 5d, 5d-ii and 5d-iii, which the user's operator maintains — never a script you "
       "compose at run time.)\n"
       "6c. SCHEDULE — map the user's words literally. 'every N minutes/hours/days' is RECURRING: "
       "pass 'every Nm' / 'every Nh' / 'every Nd', NOT a bare 'Nm' (which hermes reads as a "
       "ONE-SHOT that runs once and deletes itself). Combine a recurrence with a bound using "
       "repeat: 'every 5 minutes for the next 20 minutes' = schedule 'every 5m' with repeat 4. "
       "Only a genuinely single check ('check once', 'in an hour') may be a one-shot. State the "
       "resulting schedule back to the user in plain words.\n"
       "7. A one-off request ('check once', 'right now') is STILL a job: schedule it as a "
        "one-shot in 1 minute. This session cannot fetch pages itself — do not try to do the "
        "check directly here. A one-shot removes itself after running; that is success, not a "
        "lost job — do not re-query it afterwards.\n"
        "8. Alert conditions are LITERAL and state-based. 'Alert me if it is below X' means: "
        "emit the ALERT line in every run where the value IS below X — including the very "
        "first run. Never add prior-state or transition requirements the user did not ask "
        "for (no 'only if it was previously above X'). The only permitted dampening: skip "
        "the ALERT when the value is identical to the one already alerted last run.\n"        "9. After creating a job, call cronjob(action='list') and copy the REAL job id and next "
        "run time from the tool result into your reply. Describing a job is not creating it — if "
        "the id is not in the list, you did not create it; say so plainly instead of confirming.\n"
        "Reply to the user with plain-language confirmation: what will be checked, how often, "
        "until when, and that results will appear in the background-tasks channel (plus a phone "
        "push if they asked to be alerted). Keep it short."
    )

    def _hermes_key(self):
        try:
            k = open(HERMES_KEY_FILE).read().strip()
            return k or None
        except Exception:
            return None

    @staticmethod
    def _changed_jobs(before, after, owner=None, owners=None):
        """[(job_id, what changed)] for jobs the scheduler already had and has since altered.

        Only fields a user would recognise as "my task changed": how often it runs, how many runs
        are left, and whether it is on. Anything else (next_run_at ticking forward, last_status)
        moves on its own every minute and would report a change on every single turn.

        `owner` restricts the result to that handle's jobs. The snapshots are host-wide, so
        without it a change another user made during this turn would be narrated to this one.
        """
        out = []
        for jid, a in (after or {}).items():
            b = (before or {}).get(jid)
            if not b:
                continue
            if owner is not None and ((owners or {}).get(jid) or {}).get("h") != owner:
                continue
            diffs = []
            if (a.get("schedule_display") or a.get("schedule")) != \
                    (b.get("schedule_display") or b.get("schedule")):
                diffs.append("rescheduled")
            if a.get("repeat") != b.get("repeat"):
                diffs.append("run count changed")
            if bool(a.get("enabled", True)) != bool(b.get("enabled", True)):
                diffs.append("enabled" if a.get("enabled", True) else "disabled")
            if (a.get("state") or "") != (b.get("state") or ""):
                diffs.append(f"now {a.get('state')}")
            # A rename is never innocuous the way next_run_at ticking forward is — a job's name is
            # static unless something explicitly set it, so any diff here IS a real event. Measured
            # live 2026-08-10: an edit turn asked only for a schedule change and the agent silently
            # renamed the job to the user's own typo'd duration phrase ("2 Horus") in the same PATCH
            # — nothing downstream compared names, so nothing ever told the user it happened.
            if (a.get("name") or "") != (b.get("name") or ""):
                diffs.append(f"renamed to '{a.get('name')}'")
            if diffs:
                out.append((jid, ", ".join(diffs)))
        return out

    # ---------- rendering the job table ----------
    @staticmethod
    def _md_cell(s, n=48):
        """A job field, safe to drop into a markdown table cell.

        Job names and error text are ATTACKER-INFLUENCED — they come from whatever the agent was
        told to watch, including page titles and scraped text. So this does more than tidy:
        neutralising '<!--' and '-->' is what stops a job called 'x--><!--bg-confirm:...' from
        forging the very marker that arms a delete. Pipes are escaped so a name cannot add columns,
        newlines collapsed so last_error cannot break the table apart.
        """
        t = re.sub(r"\s+", " ", str(s or "")).strip()
        t = t.replace("`", "'").replace("|", r"\|")
        t = t.replace("<!--", "<! --").replace("-->", "-- >")
        return (t[: n - 1] + "…") if len(t) > n else t

    @staticmethod
    def _when(iso):
        """'in 12 min' / '18 min ago' when that is honest, an absolute stamp when it is not.

        Relative time is a CLAIM about now. A naive timestamp compared against an aware one (or a
        clock-skewed record) produces a confident lie like 'in 3 years', so anything that does not
        parse cleanly, or lands absurdly far away, renders as the raw stamp instead.
        """
        if not iso:
            return ""
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(str(iso))
            if dt.tzinfo is None:      # naive: cannot be compared honestly against an aware now
                raise ValueError("naive")
            delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if abs(delta) > 30 * 86400:
                raise ValueError("implausible")
            ahead, s = delta >= 0, abs(delta)
            if s < 90:
                out = "now" if ahead else "just now"
                return out
            for unit, size in (("min", 60), ("h", 3600), ("d", 86400)):
                if s < size * (60 if unit == "min" else 24 if unit == "h" else 31):
                    n = int(round(s / size))
                    return f"in {n} {unit}" if ahead else f"{n} {unit} ago"
        except Exception:
            pass
        return str(iso)[:16].replace("T", " ")

    @classmethod
    def _job_glyph(cls, j):
        """One character for 'what is this job doing'.

        latest_execution is checked FIRST and is the only way to show an in-flight run: `state` has
        no "running" value, and latest_execution is populated by the list endpoint alone. Bounded
        on freshness — a run 'claimed' three days ago is a wedged record, not a live run.
        """
        ex = j.get("latest_execution") or {}
        if str(ex.get("status") or "").lower() in ("claimed", "running"):
            started = cls._when(ex.get("started_at") or ex.get("claimed_at") or "")
            if "d ago" not in started:
                return "🔄"
        state = str(j.get("state") or "").lower()
        # completed BEFORE the enabled check: a finished job is left enabled=False by hermes, so
        # testing "not enabled" first labelled every exhausted job as merely paused — and the
        # header count (which reads state) then disagreed with the glyph on the same row.
        if state == "completed":
            return "✓"
        if state == "paused" or not j.get("enabled", True):
            return "⏸"
        if state == "error":
            return "⚠️"
        return "▶"

    @classmethod
    def _job_next(cls, j):
        """The 'Next run' cell. A blank next_run_at means different things per state, and saying
        'none' for all of them hides whether a job is off, done, or broken."""
        ex = j.get("latest_execution") or {}
        if str(ex.get("status") or "").lower() in ("claimed", "running"):
            return "running now"
        nxt = j.get("next_run_at")
        if nxt:
            return cls._when(nxt)
        state = str(j.get("state") or "").lower()
        if state == "completed":       # same ordering rule as _job_glyph
            return "— finished"
        if state == "paused" or not j.get("enabled", True):
            return "— paused"
        if state == "error":
            return "— scheduling error"
        return "—"

    @classmethod
    def _job_sched(cls, j):
        sched = cls._md_cell(j.get("schedule_display") or j.get("schedule") or "", 28) or "*unknown*"
        rep = j.get("repeat") or {}
        if isinstance(rep, dict) and isinstance(rep.get("times"), int):
            sched += f" · {rep.get('completed', 0)} of {rep['times']} runs"
        return sched

    def _jobs_table(self, jobs, ordinals=True, owners=None):
        """The markdown table. Rows stay in API order — sorting by state would renumber the list
        between two renders the user is comparing, which is the one thing ordinals cannot survive.

        `owners` adds an Owner column and is passed ONLY on an admin listing: a scoped table
        contains one owner by construction, so the column would be a column of the reader's own
        name.
        """
        own = owners is not None
        rows = ["| # | Task |" + (" Owner |" if own else "") + " Schedule | Next run | Last run | ID |",
                "|---|---|" + ("---|" if own else "") + "---|---|---|---|"]
        for i, j in enumerate(jobs[:JOBS_MAX], 1):
            last = j.get("last_run_at")
            mark = {"ok": "✅", "error": "⚠️"}.get(str(j.get("last_status") or "").lower(), "")
            last_cell = f"{mark} {self._when(last)}".strip() if last else "*never*"
            handle = f"**{i}**" if ordinals else f"**{chr(96 + i)}**"
            owner_cell = (f" {self._md_cell(self._owner_of(owners, j) or '—', 16)} |") if own else ""
            rows.append(f"| {handle} | {self._job_glyph(j)} {self._md_cell(j.get('name'), 44)} "
                        f"|{owner_cell} {self._job_sched(j)} | {self._job_next(j)} | {last_cell} "
                        f"| `{j.get('id') or 'unknown'}` |")
        out = "\n".join(rows)
        if len(jobs) > JOBS_MAX:
            out += f"\n\n*…and {len(jobs) - JOBS_MAX} more — ask for one by id if you need it.*"
        return out

    # ---------- the trip a fare watch is watching ----------
    #
    # A hermes job has NO itinerary field. The whole record is name / prompt / schedule / deliver
    # (~/.hermes/cron/jobs.json), so the only place an itinerary can live is inside the stored
    # command — which is why this reads the prompt back rather than a column. That makes it
    # READ-ONLY and quotable: every value below was typed into the job by whoever created it, and
    # the failure mode is showing nothing, never showing a trip nobody scheduled. Same rule the
    # slot parser keeps at :1391 — a model may supply a category, never a date or an airport.
    _IT_FARE = re.compile(r"--kind[=\s]+'?fare\b")
    _IT_VETTED = re.compile(r"\bprice_(?:search|watch)\.py\b")

    @staticmethod
    def _it_day(v):
        """An ISO date as a person would say it; the raw string when it is not one.

        Never reformats what it could not parse. A --depart the creator wrote as 'next tuesday' is
        shown as 'next tuesday', because guessing which Tuesday is how a watch ends up bound to a
        trip the user never asked for.
        """
        try:
            import datetime
            return datetime.date.fromisoformat(str(v)).strftime("%a %d %b %Y").replace(" 0", " ")
        except Exception:
            return Pipe._md_cell(v, 24)

    def _it_fare_watch(self, prompt):
        """"declared" | "mislabelled" | "" — a fare watch with nowhere to put an itinerary.

        `--kind fare` is the honest label and the easy case. The hard one was measured live on
        2026-08-08: asked for a Toronto→Vancouver price watch, the agent twice built the job with
        `--kind price_drop` while naming it "YYZ→YVR Flight Price Watch". The label is the agent's
        opinion; the ROUTE is not — so a vetted extractor plus a flight word plus two airports the
        parser can actually resolve is read as a fare watch whatever the --kind says.

        All three are required for the mislabelled arm, and the third is what keeps "Microsoft
        Flight Simulator" a product: it names a flight and no route, so it is one.

        The two are told apart because they FAIL DIFFERENTLY, and only one of them is dangerous.
        `--kind fare` refuses at the first run and alerts once (scripts/price_search.py:360), so it
        is inert and merely useless. A mislabelled one never reaches that guard: it runs the ordinary
        product path over a flight search result, which is precisely how a fare monitor texted
        "$358.72, under your $1,000.00 target" at high confidence off a page whose own title read
        "C$ 146+" (docs/TRACKING_ENHANCEMENT.md). Reporting both as "it will refuse" would describe
        the safe failure while the unsafe one is the live risk.
        """
        if self._IT_FARE.search(prompt):
            return "declared"
        if (self._IT_VETTED.search(prompt) and self._FLIGHT_NOUN.search(prompt)
                and self._fl_route(prompt)):
            return "mislabelled"
        return ""

    def _job_trip(self, job):
        """The trip line for one job, or "" when the job is not about a trip at all.

        Three outcomes, and the middle one is why this exists. On 2026-08-08 job ba2a91e18def was
        confirmed to the user as a "Flight price watch" whose only itinerary was the search string
        'Toronto to Vancouver flights' — no dates, because price_search has nowhere to put any. It
        would have refused on all 8 runs (scripts/price_search.py:360) while the confirmation card
        read like a working watch. A fare belongs to one route on one set of dates or it belongs to
        nothing, so a fare watch that cannot name them says so here instead of looking scheduled.
        """
        cls = type(self)
        prompt = job.get("prompt") or ""
        # The FlightClaw shape: the whole itinerary lives inside --route-id, in FlightClaw's own
        # formula (ORIG-DEST-YYYY-MM-DD[-RT-YYYY-MM-DD]) — one token to parse, quoted or not.
        m = re.search(r"flightclaw_watch\.py.*?--route-id[=\s]+'?"
                      r"([A-Z]{3})-([A-Z]{3})-(\d{4}-\d{2}-\d{2})"
                      r"(?:-RT-(\d{4}-\d{2}-\d{2}))?", prompt, re.S)
        if m:
            o, d, dep, ret = m.groups()
            when = f"depart **{cls._it_day(dep)}**"
            when += f" · return **{cls._it_day(ret)}**" if ret else " · **one way**"
            return f"✈️ **{o} → {d}** · {when}"
        # (The flight_watch.py arm lived here until 2026-08-09. That script left with the
        # old scraping stack — FlightClaw watches fares now, parsed above.)
        fare = self._it_fare_watch(prompt)
        if fare == "declared":
            return ("⚠️ **no itinerary** — this fare watch names no origin, destination or dates, "
                    "so every run refuses instead of quoting a fare. A fare exists only for one "
                    "route on one set of dates.")
        if fare == "mislabelled":
            return ("⚠️ **watching a flight as if it were a product** — this job has no dates in "
                    "it, so there is no itinerary for a fare to belong to, and it is not set to "
                    "the fare kind that would refuse. It will read whatever number a flight search "
                    "page shows and compare that against your threshold. Those pages quote "
                    "\"from\" teasers and lists of unrelated trips, so the number it texts you may "
                    "not be bookable — or be a real fare for dates you never asked for.")
        return ""

    def _jobs_notes(self, jobs):
        """Legend, plus the trips and the failures. Both are multi-line and belong BELOW the table:
        in a cell they would blow the columns apart, and they are the fields a user most needs."""
        # Blank line first: markdown needs one to close the table, or the legend is swallowed into
        # it as a malformed row.
        out = ["\n\n▶ active · 🔄 running now · ⏸ paused · ✓ finished · ⚠️ scheduling error"]
        # Trips before failures: this says what the job IS, the failure block says what it did. Both
        # capped at 3 and the remainder COUNTED — a silently truncated list reads as "that is all of
        # them", which is the one thing a listing must never imply.
        trips = [(i, t) for i, t in ((i, self._job_trip(j))
                                     for i, j in enumerate(jobs[:JOBS_MAX], 1)) if t]
        for i, t in trips[:3]:
            out.append(f"\n**{i}** {t}")
        if len(trips) > 3:
            out.append(f"\n*…and {len(trips) - 3} more with a trip attached.*")
        bad = [(i, j) for i, j in enumerate(jobs[:JOBS_MAX], 1)
               if str(j.get("last_status") or "").lower() == "error" and j.get("last_error")]
        for i, j in bad[:3]:
            out.append(f"\n⚠️ **{i}** failed its last run: `{self._md_cell(j.get('last_error'), 180)}`")
        if len(bad) > 3:
            out.append(f"\n*…and {len(bad) - 3} more with failing runs.*")
        return "".join(out)

    def _jobs_error(self, err, op="list", detail=""):
        """Copy for every way the scheduler can fail to answer.

        Every string says some version of "that is not the same as having no tasks". An error that
        reads like an empty list is the worst possible outcome here: the user stops expecting the
        alert they are still owed.
        """
        tail = f" hermes says: {detail}" if detail else ""
        if err == "no_key":
            return (f"⚠️ **I could not read the scheduler** — the hermes-agent key file is missing "
                    f"(`{HERMES_KEY_FILE}`), so I cannot {op} background tasks. That is not the "
                    f"same as having none: whatever is scheduled is still scheduled, I just cannot "
                    f"see it.")
        if err == "unreachable":
            return ("⚠️ **I could not read the scheduler** — hermes's job API on `127.0.0.1:8642` "
                    "did not answer. That is not the same as having no tasks. Start it with "
                    "`systemctl --user start hermes-gateway`, then ask me again.")
        if err == "timeout":
            return ("⚠️ **I could not read the scheduler** — hermes's job API did not answer within "
                    "10 seconds. That is not the same as having no tasks. Try again in a moment.")
        if err == "http_401" or err == "http_403":
            return ("⚠️ **The scheduler rejected my key** — I cannot tell you what is scheduled, "
                    "which is not the same as nothing being scheduled. The staged key no longer "
                    "matches hermes's own.")
        if err == "http_501":
            return ("⚠️ **The scheduler's cron module is not loaded**, so hermes cannot answer "
                    "questions about jobs at all right now.")
        return (f"⚠️ **The scheduler errored** ({err}).{tail} Nothing was changed and I cannot show "
                f"you the list right now.")

    @staticmethod
    def _owners_error():
        """When ownership cannot be read, an ordinary user is told so — not shown an empty list.

        Same discipline as _jobs_error: an error that reads like "you have nothing" is worse than
        an error, because the user stops expecting the alert they are still owed.
        """
        return ("⚠️ **I could not work out which tasks are yours** — the ownership record is "
                "unreadable, so I will not guess. That is not the same as having no tasks: "
                "anything you scheduled is still scheduled and still running. An admin can repair "
                f"`{TASK_OWNERS_FILE}`.")

    def _render_list(self, cid, jobs, lead=None, ordinals=True, park=True, owners=None,
                     scoped=False, handle=""):
        """The full listing reply: header, table, legend/failures, and the state that makes
        ordinals mean something next turn."""
        if not jobs:
            mine = " of yours" if scoped else ""
            only = ("\n\nOnly your own tasks appear here." if scoped else "")
            return (f"**No background tasks{mine} are scheduled.** I checked hermes's scheduler "
                    f"directly — this is what it actually has, not a guess.{only}\n\nAsk for one "
                    f"with e.g. *monitor the RTX 5090 price on newegg every 6 hours*.")
        live = sum(1 for j in jobs if self._job_live(j))
        paused = sum(1 for j in jobs if not j.get("enabled", True)
                     and str(j.get("state") or "").lower() != "completed")
        done = sum(1 for j in jobs if str(j.get("state") or "").lower() == "completed")
        bits = [f"{live} active"] + ([f"{paused} paused"] if paused else []) \
            + ([f"{done} finished"] if done else [])
        head = lead or (f"**{'All' if owners is not None else 'Your'} background tasks** — "
                        f"{', '.join(bits)}")
        # The examples name no job on purpose: a plausible-sounding one ("cancel the btc monitor")
        # reads as though it refers to a row that is actually there.
        tail = ("\n\nSay *pause the second one*, *cancel the first one*, or give me an id. "
                "I read the real scheduler, and I ask before deleting anything.")
        return (f"{head}\n\n{self._jobs_table(jobs, ordinals, owners)}{self._jobs_notes(jobs)}{tail}"
                + (self._park_jobs(cid, jobs, handle) if park else ""))

    def _manage_scope(self, user, handle):
        """Whose jobs this turn may see. None = everyone's (admin); otherwise the owning handle.

        Management used to be admin-only, because hermes has no per-job owner and one shared API
        key sees every job on the host. Ownership now lives in TASK_OWNERS_FILE, stamped at
        creation from the scheduler's own job ids — so an ordinary user gets a real, scoped view
        instead of being pushed onto the agent, which would have shown them everything.

        Caveat worth knowing: the handle is the email local part (see _alert_username), so
        alice@a.com and alice@b.com collide, and every account without an email shares "user".
        That is the same key the alert contacts already use. If it ever matters, key on
        user["id"] instead — the change is confined to _alert_username.
        """
        if str((user or {}).get("role", "")).lower() == "admin" or (handle or "") in TASK_ADMINS:
            return None
        return handle or "user"

    async def _do_manage(self, op, job, parked_name=None, scope=None):
        """Execute one scheduler write and report what the SCHEDULER says afterwards, not what the
        API claimed. Returns the finished reply text."""
        jid = job.get("id") or ""
        name = self._md_cell(job.get("name"), 60) or parked_name or jid
        t0 = time.monotonic()

        def _fin(outcome, text, **extra):
            self._metric(job="manage", op=op, outcome=outcome, job_id=jid,
                         ms=round((time.monotonic() - t0) * 1000), **extra)
            return text

        if not self._JOB_ID_RE.match(jid):
            return _fin("bad_id", f"⚠️ That row has an unusable job id (`{jid}`), so I cannot "
                                  f"{op} it. Ask hermes directly for this one.")
        if scope is not None:
            # Belt and braces: the fetch in _manage_turn already filtered to this owner, so
            # reaching here means the record changed underneath a parked reference. Re-read rather
            # than trusting the map this turn started with, and phrase the refusal so it confirms
            # nothing about a job the user is not allowed to know exists.
            fresh_owners, oerr = self._read_owners()
            if oerr or (fresh_owners.get(jid) or {}).get("h") != scope:
                return _fin("not_owner",
                            f"⚠️ I don't have a job with id `{jid}` among your tasks, so nothing "
                            f"was changed.")
        # Pre-checks that make an API call pointless or misleading.
        state = str(job.get("state") or "").lower()
        if op == "pause" and state == "completed":
            return _fin("already_finished",
                        f"ℹ️ **Nothing to pause** — “{name}” (`{jid}`) has already finished, so it "
                        f"is not going to run again. Say *cancel {name}* if you want the record "
                        f"removed.")
        if op == "resume" and state == "completed":
            return _fin("already_finished",
                        f"ℹ️ **That one is finished, not paused** — “{name}” (`{jid}`) used up its "
                        f"runs. Resuming would not give it any more. Ask me to schedule a new one.")

        path = {"cancel": f"/api/jobs/{jid}",
                "pause": f"/api/jobs/{jid}/pause",
                "resume": f"/api/jobs/{jid}/resume"}[op]
        method = "DELETE" if op == "cancel" else "POST"
        status, data, err = await asyncio.to_thread(self._hermes_api, method, path)

        if err == "http_404":
            if op == "cancel":
                # DELETE is not idempotent server-side, but from the user's point of view a job
                # that is already gone is a completed request, not a failure.
                return _fin("gone", f"✅ **Already gone** — “{name}” (`{jid}`) was no longer in the "
                                    f"scheduler, so there was nothing to cancel. Jobs remove "
                                    f"themselves when they finish their run budget.")
            return _fin("gone", f"⚠️ **That task is gone** — “{name}” (`{jid}`) is no longer in the "
                                f"scheduler; it probably finished and removed itself. Nothing was "
                                f"changed.")
        if err:
            return _fin("api_error", self._jobs_error(err, op, self._api_err_text(data)), err=err)

        # Ground truth: re-read and report what /api/jobs says now.
        jobs, verr = await asyncio.to_thread(self._jobs_list)
        after = {j.get("id"): j for j in jobs}
        if verr:
            return _fin("verify_unavailable",
                        f"✅ hermes accepted the **{op}** for “{name}” (`{jid}`), but I could not "
                        f"re-read the scheduler to confirm it took effect. Ask me to list tasks in "
                        f"a moment to check.")
        if op == "cancel":
            if jid in after:
                return _fin("verify_failed",
                            f"⚠️ hermes returned OK for **cancel** on “{name}” (`{jid}`), but the "
                            f"scheduler still lists it. I am reporting what `/api/jobs` says, not "
                            f"what the API claimed.")
            # No undo exists — DELETE also removes the job's saved output — so the reply carries
            # everything needed to recreate it by hand.
            sched = self._md_cell(job.get("schedule_display") or job.get("schedule"), 40)
            return _fin("done",
                        f"✅ **Cancelled** — “{name}” (`{jid}`) is gone from the scheduler, "
                        f"confirmed by re-reading it.\n\nIf that was a mistake, there is no undo, "
                        f"but this recreates it:\n\n> {name} — {sched}" )
        j2 = after.get(jid) or {}
        want_paused = op == "pause"
        is_paused = (not j2.get("enabled", True)) or str(j2.get("state") or "").lower() == "paused"
        if is_paused != want_paused:
            return _fin("verify_failed",
                        f"⚠️ hermes returned OK for **{op}** on “{name}” (`{jid}`), but the "
                        f"scheduler still shows it as {self._job_glyph(j2)} "
                        f"{j2.get('state') or 'unknown'}. Reporting what `/api/jobs` says.")
        nxt = self._job_next(j2)
        return _fin("done",
                    f"✅ **{'Paused' if want_paused else 'Resumed'}** — “{name}” (`{jid}`)"
                    + (f". Next run {nxt}." if not want_paused else
                       " will not run until you resume it.")
                    )

    async def _do_manage_bulk(self, op, jobs, scope=None):
        """Run one operation across several jobs and report it as one result.

        Deliberately not N copies of _do_manage's prose: eight paragraphs saying the same thing is
        how a real failure in the middle goes unread. One line per job, and anything that did NOT
        work gets its own line with the reason.
        """
        done, failed = [], []
        for j in jobs:
            out = await self._do_manage(op, j, scope=scope)
            name = self._md_cell(j.get("name"), 48)
            # _do_manage words every outcome; the leading glyph is what separates worked from did
            # not, and it is the same set the table uses.
            (done if out.lstrip().startswith(("✅", "ℹ️")) else failed).append((name, j, out))
        verb = {"cancel": "Cancelled", "pause": "Paused", "resume": "Resumed"}[op]
        lines = [f"**{verb} {len(done)} of {len(jobs)} tasks.**" if failed
                 else f"✅ **{verb} {len(done)} task{'s' if len(done) != 1 else ''}.**"]
        for name, j, _ in done:
            lines.append(f"- {name} (`{j.get('id')}`)")
        for name, j, out in failed:
            first = " ".join(out.split())[:160]
            lines.append(f"- ⚠️ **{name}** — not {op}led. {first}")
        if op == "cancel" and done:
            # There is no undo and the saved output goes too, so the recreate details have to be
            # in the same message rather than one list away.
            lines.append("\nNo undo. To recreate them:")
            for name, j, _ in done:
                sched = self._md_cell(j.get("schedule_display") or j.get("schedule"), 40)
                lines.append(f"> {name} — {sched}")
        return "\n".join(lines)

    async def _manage_turn(self, cid, text, parked, rule, pending=None, user=None, handle=""):
        """The whole deterministic manage turn. Returns the reply text, or None to fall through to
        the agent — the sole escape hatch, so this can never be a dead end.

        Owns every route row it could emit: exactly one per turn, by construction.
        """
        if not MANAGE_DETERMINISTIC:
            self._route_metric("task.manage", 1, rule, text,
                               deterministic=False, reason="disabled")
            return None
        t0 = time.monotonic()
        jobs, err = await asyncio.to_thread(self._jobs_list)
        if err:
            # A manage turn the scheduler cannot answer: say so rather than delegating, because
            # the agent would hit the same dead API and cost an eviction to do it.
            self._route_metric("task.list.error", 1, rule, text, err=err,
                               ms=round((time.monotonic() - t0) * 1000))
            return self._jobs_error(err, "list")

        # --- whose jobs is this turn allowed to see? ----------------------------------------
        scope = self._manage_scope(user, handle)
        owners, oerr = self._read_owners()
        total = len(jobs)
        if err is None and not oerr:
            live = {j.get("id") for j in jobs}
            if self._prune_owners(owners, live):
                self._write_owners(owners)
                self._metric(job="owner", outcome="pruned")
        if scope is not None:
            if oerr:
                # FAIL CLOSED. The alternative is rendering an empty list, which reads as "you
                # have nothing scheduled" — the precise lie every error string in _jobs_error
                # exists to prevent — or showing the unfiltered host list, which is the exposure
                # this whole path was built to close.
                self._route_metric("task.list.error", 1, rule, text, err="owners_unreadable",
                                   ms=round((time.monotonic() - t0) * 1000))
                return self._owners_error()
            jobs = [j for j in jobs if self._owner_of(owners, j) == scope]
        # Everything downstream reads `jobs`: the listing, _resolve_ref's candidate set, the
        # disambiguation table and the armed-confirm lookup. Filtering once here is what makes
        # every one of them scoped, including the ones added later.

        pend = pending or {}
        op = self._manage_op(text)

        # --- consuming an armed confirmation -----------------------------------------------
        if pend.get("stage") == "confirm":
            # One shape for one job and for many: the armed record always carries a list.
            jids = list(pend.get("ids") or ([pend.get("id")] if pend.get("id") else []))
            jid = jids[0] if len(jids) == 1 else None
            by_id = {j.get("id"): j for j in jobs}
            # A reply that names a DIFFERENT job is a new instruction, whatever affirmative word it
            # happens to open with. Treating it as an answer would act on the job that was asked
            # about rather than the one just named — the worst available outcome for a delete.
            names_other = any(i != jid for i in
                              re.findall(r"\b[0-9a-f]{12}\b", (text or "").lower()))
            if not names_other and (self._CONFIRM_YES.match(text or "")
                                    or self._CONFIRM_ALT.match(text or "")):
                # Disarm FIRST on every branch below: the armed op is consumed by being answered,
                # so a second "yes" can never replay a delete.
                if pend.get("expired"):
                    self._metric(job="confirm", kind="task_cancel", outcome="expired")
                    self._route_metric("task.manage.abort", 0, "bg_confirm_expired", text)
                    self._disarm(cid)
                    return ("That confirmation is more than 10 minutes old, so I did not act on it. "
                            "Ask me again and I will re-confirm against the current list.")
                present = [i for i in jids if i in by_id]
                if not present:
                    self._metric(job="confirm", kind="task_cancel", outcome="accepted")
                    self._route_metric("task.manage.abort", 0, "bg_confirm_gone", text)
                    self._disarm(cid)
                    gone = self._md_cell((pend.get("ns") or [pend.get("n") or ""])[0], 60)
                    return (f"⚠️ **Already gone** — “{gone}”"
                            + (f" and {len(jids) - 1} other(s)" if len(jids) > 1 else "")
                            + " no longer in the scheduler, so there was nothing to cancel.")
                # --- several at once ------------------------------------------------------
                if len(jids) > 1:
                    names = pend.get("ns") or []
                    changed = [i for n, i in enumerate(jids)
                               if i in by_id
                               and str(by_id[i].get("name") or "")[:60] != (
                                   names[n] if n < len(names) else None)]
                    if changed:
                        self._metric(job="confirm", kind="task_cancel",
                                     outcome="changed_under_us")
                        self._route_metric("task.manage.abort", 0, "bg_confirm_changed", text)
                        self._disarm(cid)
                        return self._render_list(
                            cid, [by_id[i] for i in present], handle=handle,
                            scoped=scope is not None,
                            lead="⚠️ **Something changed since I asked** — I did not cancel "
                                 "anything. Here is where things actually stand; ask again if you "
                                 "still want them gone.")
                    batch = [by_id[i] for i in present]
                    bulk_op = "pause" if self._CONFIRM_ALT.match(text or "") else "cancel"
                    self._metric(job="confirm", kind="task_cancel",
                                 outcome="downgraded" if bulk_op == "pause" else "accepted",
                                 n=len(batch))
                    self._route_metric(f"task.manage.{'downgrade' if bulk_op == 'pause' else 'cancel'}",
                                       0, "bg_confirm_yes", text, op=bulk_op, n=len(batch))
                    self._disarm(cid)
                    missing = len(jids) - len(present)
                    out = await self._do_manage_bulk(bulk_op, batch, scope=scope)
                    if missing:
                        out += (f"\n\n*{missing} of them had already gone from the scheduler.*")
                    return out
                fresh = by_id[jid]
                # Did it change under us between the question and the answer? The armed record
                # carries the fingerprint precisely so a "yes" cannot land on a different job than
                # the one that was described.
                same = (str(fresh.get("name") or "")[:60] == (pend.get("n") or "")
                        and str(fresh.get("schedule_display") or fresh.get("schedule")
                                or "")[:40] == (pend.get("s") or ""))
                if not same:
                    self._metric(job="confirm", kind="task_cancel", outcome="changed_under_us")
                    self._route_metric("task.manage.abort", 0, "bg_confirm_changed", text)
                    self._disarm(cid)
                    return self._render_list(
                        cid, [fresh], handle=handle, scoped=scope is not None,
                        lead="⚠️ **That task changed since I asked** — I did not cancel anything. "
                             "Here it is as it stands now; ask again if you still want it gone.")
                if self._CONFIRM_ALT.match(text or ""):
                    self._metric(job="confirm", kind="task_cancel", outcome="downgraded")
                    self._route_metric("task.manage.downgrade", 0, "bg_confirm_pause", text)
                    self._disarm(cid)
                    return await self._do_manage("pause", fresh, scope=scope)
                self._metric(job="confirm", kind="task_cancel", outcome="accepted")
                self._route_metric("task.manage.cancel", 0, "bg_confirm_yes", text, job_id=jid)
                self._disarm(cid)
                return await self._do_manage("cancel", fresh, pend.get("n"), scope=scope)
            if self._CONFIRM_NO.match(text or ""):
                self._metric(job="confirm", kind="task_cancel", outcome="declined")
                self._route_metric("task.manage.abort", 0, "bg_confirm_no", text)
                self._disarm(cid)
                return "Okay — nothing was cancelled."
            # Anything else: the user moved on from the confirmation. Never act on it.
            self._metric(job="confirm", kind="task_cancel", outcome="abandoned")
            self._disarm(cid)
            # ...but if what they moved on TO is itself a task request, serve it here rather than
            # returning None. Falling through would hand "list my tasks" to the agent, whose job
            # list is the whole host — the one leak this path exists to close.
            raw = (text or "").strip().lower()
            if not (op or self._BG_LIST.search(raw) or self._BG_MANAGE.match(raw)):
                return None
            pend = {}

        # --- an open disambiguation ("which one?") ------------------------------------------
        if pend.get("stage") == "choose":
            op = op or pend.get("op")

        # --- no operation named: this is a listing turn --------------------------------------
        if not op:
            self._route_metric("task.list", 1, rule, text, n_jobs=len(jobs),
                               deterministic=True, scoped=scope is not None,
                               n_hidden=total - len(jobs),
                               ms=round((time.monotonic() - t0) * 1000))
            # The Owner column only makes sense on an unscoped list: a scoped one is a column of
            # the reader's own handle.
            return self._render_list(cid, jobs, owners=owners if scope is None else None,
                                     scoped=scope is not None, handle=handle)

        # --- an operation aimed at a specific job --------------------------------------------
        r = self._resolve_ref(text, jobs, parked)
        st, job = r["status"], r["job"]

        # --- ...or at several of them, named on purpose ---------------------------------------
        if st == "bulk":
            batch = r["candidates"]
            if op == "cancel":
                rows = "\n".join(
                    f"| {self._md_cell(j.get('name'), 52)} | "
                    f"{self._md_cell(j.get('schedule_display') or j.get('schedule'), 24)} | "
                    f"`{j.get('id')}` |" for j in batch)
                self._metric(job="confirm", kind="task_cancel", outcome="asked", n=len(batch))
                self._route_metric("task.manage.confirm", 1, rule, text, op="cancel",
                                   strategy=r["strategy"], n=len(batch))
                # Every job named. A count alone ("delete 4 tasks?") is not something anyone can
                # actually check, and this is the one operation with no undo.
                return (f"⚠️ **Cancel {len(batch)} tasks for good?**\n\n"
                        f"| Task | Schedule | ID |\n|---|---|---|\n{rows}\n\n"
                        "That is everything listed above — deleting removes each job **and its "
                        "saved output**, and there is no undo. Reply **yes** to delete them all, "
                        "**pause** to switch them off instead, or anything else to leave them "
                        "alone." + self._confirm_park(cid, "cancel", None, handle=handle,
                                                      jobs=batch))
            # pause / resume are reversible, so they act now, exactly as they do for one job.
            self._route_metric(f"task.manage.{op}", 1, rule, text, op=op,
                               strategy=r["strategy"], n=len(batch))
            return await self._do_manage_bulk(op, batch, scope=scope)

        if st == "one":
            if op == "cancel":
                sched = self._md_cell(job.get("schedule_display") or job.get("schedule"), 40)
                state = str(job.get("state") or "").lower()
                note = {"paused": " — currently paused, next run: none",
                        "completed": " — already finished, it will not run again"}.get(
                            state, f" — next run {self._job_next(job)}")
                alt = ("\n\nThat verb can mean either — reply **pause** to just switch it off "
                       "instead." if re.match(r"^\s*stop\b", (text or "").strip(), re.I) else
                       "\n\nReply **pause** to keep it and just switch it off instead.")
                self._metric(job="confirm", kind="task_cancel", outcome="asked")
                self._route_metric("task.manage.confirm", 0 if r["strategy"] in
                                   ("ordinal", "id", "prefix") else 1, rule, text,
                                   op="cancel", strategy=r["strategy"], job_id=job.get("id"))
                return ("⚠️ **Cancel this task for good?**\n\n| | |\n|---|---|\n"
                        f"| Task | {self._md_cell(job.get('name'), 60)} |\n"
                        f"| Schedule | {sched}{note} |\n"
                        f"| ID | `{job.get('id')}` |\n\n"
                        "Deleting removes the job **and its saved output**; there is no undo. "
                        "Reply **yes** to delete it, or anything else to leave it alone." + alt
                        + self._confirm_park(cid, "cancel", job, handle=handle))
            self._route_metric(f"task.manage.{op}", 0 if r["strategy"] in
                               ("ordinal", "id", "prefix") else 1, rule, text,
                               op=op, strategy=r["strategy"], job_id=job.get("id"))
            return await self._do_manage(op, job, scope=scope)

        # --- could not resolve: explain and re-render, never guess ---------------------------
        self._route_metric("task.manage.ambiguous" if st == "many" else "task.manage.nomatch",
                           1, rule, text, status=st, strategy=r["strategy"], n_jobs=len(jobs))
        if st == "many":
            cands = r["candidates"]
            # Letters, not numbers, for the candidate list: reusing 1..N here would make "2" mean
            # two different jobs in one conversation.
            # The candidates ARE parked, so "a" / "the first one" resolve against this shortlist
            # rather than the full list they were drawn from.
            return self._render_list(
                cid, cands, ordinals=False, handle=handle, scoped=scope is not None,
                lead=f"**Which one?** {len(cands)} tasks match "
                     f"**“{self._md_cell(r['needle'], 40)}”** — say *a*, *b*, or give me an id. "
                     f"Nothing has been changed.") + self._confirm_park(cid, op, None, stage="choose", handle=handle)
        # "the scheduler has" is true for an admin and misleading for everyone else, who is being
        # shown their own slice of it.
        whose = "you have" if scope is not None else "the scheduler actually has"
        leads = {
            "bad_id": f"I don't have a job with id `{self._md_cell(r['needle'], 20)}` among your "
                      f"tasks. Here's what {whose}:",
            "out_of_range": f"There's no #{self._md_cell(r['needle'], 4)} — the list I showed you "
                            f"has {len(parked)} item(s). Here's the current list:",
            "need_list": "I need to know which one — I haven't shown you a list in this chat yet. "
                         "Here's what's scheduled; then say *cancel the second one*.",
            "gone": f"⚠️ **That one is already gone** — “{self._md_cell(r['needle'], 40)}” is no "
                    f"longer in the scheduler. Nothing was changed; here's what's actually there:",
        }
        return self._render_list(
            cid, jobs, handle=handle, scoped=scope is not None,
            lead=leads.get(st, f"I don't see a task matching "
                               f"**“{self._md_cell(r['needle'], 40)}”** among your tasks. Here's "
                               f"everything {whose} — nothing was changed:"))

    # ---------- the Task control ----------
    @staticmethod
    def _task_mode(metadata):
        """Which signal says the Task control is on, or None.

        Two independent signals, read as an OR. The enabled-filter list is the client's claim that
        the control was on; the stamp is the filter's own record that it actually ran, and so that
        the other modes were actually stood down. A direct API caller can produce the first without
        the second, and that divergence belongs in a metrics row rather than being invisible.

        Missing the mode drops the turn back onto the wording-based guess this control exists to
        replace, while entering it spuriously does what the user pressed a button to ask for — so
        the OR is the right way round.
        """
        md = metadata or {}
        in_ids = TASK_MODE_ID in (md.get("filter_ids") or [])
        stamped = bool(md.get("task_mode"))
        if not (in_ids or stamped):
            return None
        return "both" if (in_ids and stamped) else ("stamp" if stamped else "filter_ids")

    async def _task_mode_turn(self, cid, text, msgs, user, src, ref=None):
        """The whole turn, given that the user asked for the background-task agent.

        The control settles WHETHER to delegate. It does not settle WHAT the request is, so every
        sub-intent below is decided by the same helper that decides it today — this changes
        reachability, never resolution.
        """
        omsgs = self._ollama_messages(msgs)
        handle = self._alert_username(user)
        scoped = self._manage_scope(user, handle) is not None
        raw = (text or "").strip().lower()

        def row(route, rule, **extra):
            # Tier 0 throughout: this is an explicit declaration, the same class of signal as a
            # slash command, not a guess with a confidence.
            self._route_metric(route, 0, rule, text, task_mode=True, src=src, **extra)

        # 1. Explicit one-shot prefixes keep meaning what they mean.
        oneshot = self._BG_ONESHOT.match(text or "")
        if oneshot:
            question = (text or "")[oneshot.end():].strip()
            if not question:
                return self._say("Give me something to look into — e.g. "
                                 "`/research what changed in the Wan 2.2 release notes`.")
            row("agent.oneshot", f"slash_{oneshot.group(1).lower()}")
            self._mark_bg(cid)
            return self._hermes_stream(question, handle, verify_creation=False,
                                       brief=self._RESEARCH_BRIEF, scoped=scoped)

        # 2. A bare number answering our own "what should I text?" question. Checked early because
        #    under this control it would otherwise become a research question ABOUT a phone number.
        pending = self._pending_phone_request(omsgs, cid)
        if pending is not None:
            answered = self._phone_reply(text, handle, pending, cid, scoped=scoped)
            if answered is not None:
                row("task.create", "chip_phone_reply")
                self._mark_bg(cid)
                return answered

        # 2a. A reply to the flight form, BEFORE the manage block for the same load-bearing reason
        #     as the normal path (:6064): _MANAGE_VERB anchors cancel|stop|pause at position 0, and
        #     `referring` below fires whenever a job table was rendered recently — so a bare "stop"
        #     meant to abandon this form would act on a real job instead.
        if FLIGHT_ROUTE and not ref and cid in self._flight_draft:
            done = self._flight_turn(cid, text, self._ollama_messages(msgs),
                                     resume=True, handle=handle)
            if done is not None:
                row("flight.slots", "chip_flight_resume")
                self._mark_bg(cid)
                return done

        # 3. Anything the deterministic path can answer, it should: it reads the scheduler over
        #    HTTP and never loads the agent, so listing stays instant even with the control on.
        pconf = self._pending_confirm(cid, omsgs, handle=handle if scoped else None)
        parked = self._parked_jobs(cid, omsgs, handle=handle if scoped else None)
        mg_manage = bool(self._BG_MANAGE.match(raw))
        mg_list = bool(self._BG_LIST.search(raw))
        answering = bool(pconf and (pconf.get("stage") == "choose"
                                    or self._CONFIRM_YES.match(text or "")
                                    or self._CONFIRM_ALT.match(text or "")
                                    or self._CONFIRM_NO.match(text or "")))
        referring = bool(parked and self._manage_op(text))
        if answering or referring or mg_manage or mg_list:
            rule = ("chip:bg_confirm" if answering else "chip:bg_parked_ref" if referring
                    else "chip:bg_manage_list" if mg_manage else "chip:bg_list_vocab")
            done = await self._manage_turn(cid, text, parked, rule, pending=pconf,
                                           user=user, handle=handle)
            if done is not None:
                self._mark_bg(cid)
                return self._say(done)
            # Declined (the deterministic path is switched off). An ordinary user still must not be
            # handed to the agent for a read-only question — its job list is the whole host.
            if scoped and (mg_manage or mg_list):
                row("task.manage", "chip_scoped_no_fallback", reason="scoped_no_fallback")
                self._mark_bg(cid)
                return self._say(
                    "I can't look up your background tasks right now — task listing is switched "
                    "off on this assistant. An admin can list and change jobs for you in the "
                    "meantime.")

        # 3a. A FLIGHT ask, before the agent gets it. This control means "make this a background
        #     job", and for a fare that is a promise this host cannot keep: measured across all 19
        #     sites the user named, 16 block automated clients, 1 exposes no fetchable URL, 2 are deal
        #     feeds, 0 are readable (docs/FLIGHT_RECON.md). Delegating anyway is not neutral — it
        #     produces exactly the job the user reported on 2026-08-09, 0cf56b8c3afd: a fare watched
        #     as a product, which reads whatever number a flight page shows and texts it as if it
        #     were their fare.
        #
        #     So this is not the control being overridden, it is the control being honoured: the
        #     docstring above says the control settles WHETHER to delegate and never WHAT the
        #     request is, and the same exception already exists for anything the pipe can answer
        #     from the scheduler itself (step 3). A flight ask is the second such case — the pipe
        #     answers it deterministically, with the user's real itinerary and a prefilled link,
        #     and no agent is loaded. What the user loses by coming here is a watch that never
        #     worked; what they get is the dates they asked about and a link that does.
        if FLIGHT_ROUTE:
            ftier, frule = self._is_flight_request(text)
            if ftier:
                done = self._flight_turn(cid, text, omsgs, resume=False, handle=handle)
                if done is not None:
                    # flight_tier, not tier: row() already passes tier positionally as 0 (this is a
                    # declaration, not a guess), so reusing the name is a TypeError.
                    row("flight.ask", f"chip_{frule}", flight_tier=ftier)
                    self._mark_bg(cid)
                    return done

        # 4. Continuing the previous agent turn.
        if self._is_bg_followup(text, omsgs, cid):
            prev = next((m.get("content") or "" for m in reversed(omsgs)
                         if m.get("role") == "assistant"), "")
            sent = (f"Continuing our exchange. You previously said:\n"
                    f"{prev.replace(self._BG_MARK, '')[-1200:]}\n\n"
                    f"The user now replies: {text}\n"
                    f"Act on it against the REAL scheduler state — call "
                    f"cronjob(action='list') first and work from what is actually there.")
            row("task.followup", "chip_followup")
            self._mark_bg(cid)
            return self._hermes_stream(sent, handle, scoped=scoped, verify_creation=False)

        # 4b. Not a new watch — a CHANGE to one that already exists ("change/adjust the alert to
        #     15 mins for 2 hours"). Checked BEFORE step 5's create test, not after: "for 2 hours"
        #     is exactly the kind of phrase _BG_RECURRENCE matches (it has to, for genuine
        #     creation requests like "watch this for 2 hours"), so an edit request that happens to
        #     spell its duration correctly would otherwise be caught by step 5 first and handed
        #     the CREATION brief — which has no idea an existing job is meant and no rule against
        #     making a second one. A verb that names an existing thing ("the alert", "the
        #     schedule") is a more specific signal than mere recurrence wording and has to win.
        #
        #     This also has to be its own step rather than falling to step 6's research brief:
        #     that brief flatly forbids touching cron jobs ("Do NOT create, modify or mention cron
        #     jobs") — correct for an actual one-off question, wrong for an edit request. Live,
        #     2026-08-10, this exact phrasing (with the duration misspelled as "2 Horus", which
        #     _BG_RECURRENCE does NOT match) fell all the way to that research brief, and the
        #     agent ignored the prohibition rather than declining — then edited with no rules at
        #     all: it silently renamed the job to the user's own typo and dropped the "for 2
        #     hours" bound entirely, leaving a 15-minute check running unbounded.
        #
        #     verify_creation=True is what turns on _changed_jobs, so an edit turn now reports a
        #     rename even if the brief below is somehow still violated — ground truth, not the
        #     agent's word, same discipline as every other verified outcome here.
        edit = bool(self._EDIT_VERB.search(raw))
        if edit:
            row("task.edit", "chip_edit")
            self._mark_bg(cid)
            return self._hermes_stream(text, handle, verify_creation=True,
                                       brief=self._EDIT_BRIEF, scoped=scoped)

        # 5. Scheduling, on positive evidence only — but ANY one signal is enough. Outside this
        #    control a monitoring verb must be accompanied by evidence of recurrence, because the
        #    pair is what distinguishes a request from a sentence. Here the user already said which
        #    it is, so the conjunction is what was making "track the item <url> when the price is
        #    under 10" fail: it has the verb, and no schedule word the pattern recognises.
        create = bool(self._BG_SLASH.match(raw) or self._BG_VERB.match(raw)
                      or self._BG_RECURRENCE.search(raw) or self._WANTS_ALERT.search(text or ""))
        if create:
            # The phone gate survives the control. Creating first and asking later leaves a monitor
            # that runs, fires, and texts nobody — pressing a button does not fix that.
            if (self._WANTS_ALERT.search(text or "")
                    and not self._contact(handle).get("phone")):
                row("task.create", "chip_phone_prompt")
                self._mark_bg(cid)
                return self._say(self._phone_prompt(handle, text, cid))
            # The confirmation gate is skipped: the control IS the consent, exactly as /task and
            # /research are ungated. Recorded rather than silent, so the accept/decline stream
            # stays an honest measure of what the guessing path gets wrong.
            self._metric(job="confirm", kind="background task", outcome="skipped_task_mode")
            row("task.create", "chip_create")
            self._mark_bg(cid)
            return self._hermes_stream(text, handle, verify_creation=True, scoped=scoped)

        # 6. No evidence of a schedule: ANSWER it, do not schedule it. The two briefs fail very
        #    differently. A question handed the scheduling brief becomes a job the user has to hunt
        #    down and cancel, and reports a verification failure on top because nothing was
        #    created. A monitoring request handed the research brief gets a real answer plus a
        #    one-line offer to watch it, and the user escalates with a word. One of those is
        #    recoverable in a turn; the other leaves state behind.
        self._metric(job="confirm", kind="background task", outcome="skipped_task_mode")
        row("task.research", "chip_research")
        self._mark_bg(cid)
        return self._hermes_stream(text, handle, verify_creation=False,
                                   brief=self._RESEARCH_BRIEF, scoped=scoped)

    _RESEARCH_BRIEF = (
        "You are answering a ONE-OFF question for the user, using your tools. This is not a "
        "scheduled job.\n"
        "1. Do NOT create, modify or mention cron jobs. Nothing here recurs. If the user actually "
        "wants something watched over time, say so in one line and stop — they will ask for it.\n"
        "2. Do NOT emit LOG:, ALERT(...) or ALERT_DATA: lines. Those belong to scheduled runs and "
        "are parsed by delivery infrastructure; here they would be picked up as a false alert.\n"
        "3. Use your tools to find things out rather than answering from memory, and say which "
        "source each fact came from. If the tools cannot establish something, say that plainly "
        "instead of filling the gap.\n"
        "4. Never state a number, price or date you did not read from a source in this session.\n"
        "5. For a price question, query the local SearXNG "
        "(http://127.0.0.1:8888/search?q=...&format=json) with your web tool, prefer major "
        "retailer pages (amazon.ca, bestbuy.ca, walmart.ca), and answer with the price AND the "
        "link to the page you read it from. Rule 4 still holds: no page read, no number.\n"
        "6. Answer in prose for the user, not as a report to a machine. Be concise."
    )

    _EDIT_BRIEF = (
        "The user wants to CHANGE something about a background job that ALREADY EXISTS — this is "
        "not a request for a new one. Use your cronjob tool.\n"
        "1. First call cronjob(action='list') and find the job the user means. If the "
        "conversation just created or discussed one, that is almost certainly it — use its real "
        "id from the tool result, never one you recall from earlier in the conversation without "
        "re-checking. If more than one job plausibly matches and you cannot tell which, ask which "
        "one instead of guessing.\n"
        "2. cronjob(action='update', ...) is a PARTIAL update: only the fields you include change; "
        "everything else on the job is left exactly as it already was. Include ONLY the fields "
        "the user actually asked to change.\n"
        "3. NEVER include 'name' unless the user explicitly asked to rename the job — 'call it "
        "X' or 'rename it to X', not implied by anything else in the message. A duration or "
        "schedule phrase (even a typo, e.g. 'for 2 Horus' meaning 'for 2 hours') describes "
        "TIMING, not a new name, and must never become one. When in doubt, omit 'name' entirely; "
        "the job keeps the name it already has.\n"
        "4. SCHEDULE AND DURATION — same mapping as creating a job: 'every N minutes/hours/days' "
        "is RECURRING, pass 'every Nm' / 'every Nh' / 'every Nd'. A bound in the same request "
        "('for 2 hours', 'for the next 20 minutes') becomes a repeat count on the SAME update: "
        "'every 15 minutes for 2 hours' = schedule 'every 15m' with repeat 8. Never drop the "
        "bound — leaving a fast schedule to run unbounded is the exact mistake this rule exists "
        "to prevent. If the user gave a new interval but no new duration, leave 'repeat' alone.\n"
        "5. After updating, call cronjob(action='list') again and state back the REAL schedule "
        "and repeat count from that fresh result, not what you intended to set — the same "
        "discipline creating a job uses. Say plainly what changed and, briefly, that nothing else "
        "about the job did.\n"
        "Reply to the user in plain language: what changed, to what, and until when. Keep it "
        "short."
    )

    def _release_chat_tenant(self):
        """Unload the 32768-ctx chat model so the 65536-ctx agent runner has room.

        Targeted rather than _free_vram(): that unloads EVERY model, including a hermes job that
        may be mid-run, and polls for up to 30 s. Here we only need the one tenant that cannot
        co-reside with the agent. Best-effort — Ollama would evict eventually anyway; this just
        makes it happen before the load rather than during it."""
        try:
            requests.post(f"{self.ollama}/api/generate",
                          json={"model": self.chat_model, "keep_alive": 0}, timeout=10)
        except Exception:
            pass

    _JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

    def _hermes_api(self, method, path, body=None, timeout=10):
        """One call against hermes's REST API. Returns (status, data, err).

        status is the HTTP code, 0 on a transport failure, -1 when there is no key. err is None on
        2xx, else one of no_key|unreachable|timeout|bad_json|bad_id|http_<n>. Blocking urllib,
        matching the style this file already uses for hermes — every CALLER wraps it in
        asyncio.to_thread, because the pipe serves every other conversation on the box.

        4xx/5xx bodies are read rather than discarded: hermes puts the actual reason in there
        ("Cannot resume: one-shot time is in the past"), and that reason is user-facing copy.
        """
        key = self._hermes_key()
        if not key:
            return -1, None, "no_key"
        import urllib.request, urllib.error
        url = f"{HERMES_URL.rsplit('/v1', 1)[0]}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": f"Bearer {key}",
                                              **({"Content-Type": "application/json"}
                                                 if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                try:
                    return r.status, (json.loads(raw) if raw else {}), None
                except Exception:
                    return r.status, None, "bad_json"
        except urllib.error.HTTPError as e:
            try:
                parsed = json.loads(e.read() or b"{}")
            except Exception:
                parsed = None
            return e.code, parsed, f"http_{e.code}"
        except TimeoutError:
            return 0, None, "timeout"
        except Exception as e:
            # socket.timeout is an OSError alias on some versions; catch it by name too.
            return 0, None, "timeout" if "timed out" in str(e).lower() else "unreachable"

    @staticmethod
    def _api_err_text(data):
        """hermes's error message, whichever envelope it used.

        Job routes answer {"error": "Job not found"}; the auth and draining paths answer
        {"error": {"message": ...}}. Reading only one shape would print 'None' at the user in
        exactly the cases they most need the reason.
        """
        e = (data or {}).get("error")
        if isinstance(e, dict):
            e = e.get("message") or e.get("code") or ""
        return str(e or "")[:200]

    def _jobs_list(self):
        """(jobs in API order, err). include_disabled=true is REQUIRED — the plain endpoint omits
        completed/exhausted jobs, so a finished job would look like a fabrication."""
        status, data, err = self._hermes_api("GET", "/api/jobs?include_disabled=true")
        if err:
            return [], err
        jobs = (data or {}).get("jobs")
        if not isinstance(jobs, list):
            return [], "bad_json"
        return jobs, None

    def _hermes_jobs(self):
        """{job_id: job} straight from /api/jobs — deterministic ground truth for the delegation
        verifier. None on any error (verification then reports 'could not verify', never a false
        positive). Kept as a dict-or-None wrapper over _jobs_list so that contract is unchanged."""
        jobs, err = self._jobs_list()
        if err:
            return None
        return {j.get("id"): j for j in jobs}

    @staticmethod
    def _job_live(j):
        """Will this job ever run again? The table's glyph and the delegation verifier must never
        disagree about that, so both read this one predicate.

        NOT a presence test: a job that exists but has finished is still 'there' for the purpose of
        cancelling it. Presence is `id in {j['id'] for j in jobs}` and nothing else."""
        return (j.get("enabled", True)
                and (j.get("state") or "").lower() != "completed")

    # ---------------------------------------------------------------- job shape enforcement
    #
    # _HERMES_BRIEF is instructions to a local MoE (hermes-genesis:agent — the same weights as the
    # chat tenant, ~3 B active of 34.7 B), and on 2026-08-07 it ignored three of them
    # at once. Two flight jobs (676e970c59ad, 4df0ab5bed14) were created with deliver='origin' —
    # whose only origin on this host is the api_server, which has no push channel, so EVERY run
    # ended in "Adapter send failed: API server uses HTTP request/response, not send()". Neither
    # prompt asked for a LOG line, so hermes_delivery.py correctly posted them as unverified model
    # output. And both prompts ended in the model's own tool-call framing, stored verbatim:
    #
    #     ...report the cheapest option regardless.</parameter>
    #     <parameter=deliver>
    #     origin
    #
    # Every one of those three is decidable from the stored record without an opinion, and each had
    # already shipped. So they stop being advice and become a check — the same move
    # _hermes_stream's creation verifier already makes against an agent that claims jobs it never
    # created. What is NOT here is anything needing judgement about what the user meant: whether
    # 'every 6 hours, forever' is the duration they asked for is not a machine's call, and a
    # validator that guesses would be the failure it exists to prevent.

    # Tool-call syntax that must never appear inside a stored prompt. Deliberately NARROW — only
    # unambiguous framing tokens, each requiring a literal '<'. Generic tags (<name>, <price>) are
    # excluded on purpose: a job that legitimately discusses markup must not be truncated
    # mid-instruction. The two live cases closed with DIFFERENT tags (</prompt> and </parameter>),
    # which is why this matches a family rather than one string.
    _JOB_MARKUP_RE = re.compile(
        r"</?(?:antml:)?(?:parameter|function|invoke|tool_call|tool_use)[=\s>]|</prompt>", re.I)
    # The vetted extractors print the protocol lines themselves (brief rules 5d / 5d-ii / 5d-iii),
    # so a job whose prompt is one of those commands needs no LOG instruction of its own and must
    # not have one appended — its whole contract is "print this command's output verbatim, add
    # nothing", and appending would make the run add something.
    # flightclaw_watch belongs here for the same reason the others do: it prints its own LOG and
    # ALERT lines, so a job running it needs no protocol instruction and must not be given one —
    # appending would make a run whose entire contract is "print this verbatim, add nothing" add
    # something. Caught by a test asserting the pipe's own generated command passes its own checks.
    _JOB_VETTED_RE = re.compile(r"\b(?:price_(?:watch|search)|flightclaw_watch)\.py\b")
    _JOB_PROTOCOL_TAIL = (
        "\n\nFinish your response with these lines, exactly this shape:\n"
        "LOG: <one-line summary of this run, leading with the key number>\n"
        "ALERT({who}): <what happened, with the number>   (ONLY when the condition above holds)\n"
        "Your response must contain NOTHING ELSE — no commentary, no tables, and never a number you "
        "did not read from the page with code. If extraction failed, say exactly that in the LOG line."
    )
    _JOB_PROMPT_MAX = 5000       # hermes api_server._MAX_PROMPT_LENGTH; a PATCH past it is a 400
    _JOB_PROMPT_MIN = 24         # below this there is no instruction left to keep
    # Repaired without saying so. `deliver` is here because an OMITTED deliver defaults to
    # "origin-or-local" in hermes (tools/cronjob_tools.py:316), and on an api_server session that
    # resolves to origin — so this is a host-level default the brief has to fight, not something the
    # agent authored, and it will need repairing on a good fraction of all creations. Announcing it
    # every time is the pipe narrating its own internals, which the verifier above deliberately
    # refuses to do. The `repaired` metric still counts it, so the rate stays measurable.
    #
    # The prompt defects are NOT here: those change the text of the job the user asked for, and a
    # rewrite the user cannot see is a rewrite they cannot correct. A repair that FAILS always
    # speaks, whatever its code — silence about undelivered alerts is the failure this whole
    # delivery path exists to prevent.
    _JOB_SILENT_FIX = {"deliver"}

    @classmethod
    def _job_defects(cls, job):
        """[(code, what is wrong, what it costs)] for one job record. Empty when well-formed."""
        out = []
        deliver = str(job.get("deliver") or "").strip()
        if deliver != "local":
            out.append(("deliver", f"delivery was `{deliver or '(unset)'}`, not `local`",
                        "its results reach nobody — every run ends in a delivery error"))
        prompt = job.get("prompt") or ""
        # ORDER IS LOAD-BEARING: markup before protocol. _job_patch cuts at the first markup match,
        # so appending the protocol tail first would append it past the cut and then delete it.
        if cls._JOB_MARKUP_RE.search(prompt):
            out.append(("markup", "the prompt carried leaked tool-call markup",
                        "every run replays it as if it were part of the instruction"))
        if "LOG:" not in prompt and not cls._JOB_VETTED_RE.search(prompt):
            out.append(("protocol", "the prompt never asks for a `LOG:` line",
                        "every run posts as unverified model output instead of a measurement"))
        # A vetted command that argparse will reject. Measured live 2026-08-09, job e6df1739a275:
        # `--monitor YTO→YVR fare watch` was written unquoted, so --monitor took 'YTO→YVR' and left
        # 'fare' and 'watch' as positionals. price_search defines no positional arguments, so
        # argparse exits 2 before the script does anything at all — every run fails, with no LOG
        # line, for a reason no other check here looks at. Decidable from the stored record with no
        # opinion, which is the bar this list keeps.
        stray, _flag = cls._job_stray_args(prompt)
        if stray:
            out.append(("argv", f"the command leaves {', '.join(repr(s) for s in stray[:3])} "
                                f"dangling — a flag value with spaces was written unquoted",
                        "argparse rejects it, so every run exits before it checks anything"))
        return out

    # Flags whose value is a free-text phrase, i.e. the ones an unquoted value breaks. Taken from
    # price_watch/price_search's own parsers rather than guessed; a flag missing here simply means
    # its strays are reported and not repaired, which is the safe direction.
    _JOB_TEXT_FLAGS = ("--monitor", "--query", "--schedule", "--state", "--label", "--url",
                       "--alert-to", "--kind", "--unit", "--prefer-domain")

    @classmethod
    def _job_vetted_argv(cls, prompt):
        """argv AFTER the vetted script's own filename, on the one line that invokes it — or None.

        Shared by _job_stray_args and _job_flag_value so both agree on what counts as THE
        command. The slice past the ".py" filename is load-bearing, not cosmetic: without it, a
        flag written anywhere else in the prompt — a decoy ahead of the real invocation, a
        comment, an earlier draft the agent left in — would read as if it were part of the
        vetted command. _job_flag_value shipped without this slice at first and a prompt like
        "note --url http://evil.example ; python3 .../price_watch.py --url https://real --below
        50" returned the DECOY url; _job_stray_args already had the slice and was unaffected.
        """
        line = next((ln for ln in (prompt or "").splitlines()
                     if cls._JOB_VETTED_RE.search(ln)), None)
        if not line:
            return None
        try:
            import shlex
            argv = shlex.split(line.strip())
        except ValueError:
            return None                # unbalanced quotes is a different defect; do not guess
        try:
            return argv[argv.index(next(a for a in argv if a.endswith(".py"))) + 1:]
        except StopIteration:
            return None

    @classmethod
    def _job_stray_args(cls, prompt):
        """(stray tokens, the flag they belong after) for a vetted command, else ([], None).

        Only ever run against the extractors this repo ships, because only there is "no positional
        arguments exist" a fact rather than an assumption.
        """
        argv = cls._job_vetted_argv(prompt)
        if argv is None:
            return [], None
        stray, last, owner, i = [], None, None, 0
        while i < len(argv):
            a = argv[i]
            if a.startswith("--"):
                if "=" not in a and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    last, i = a, i + 2           # flag plus its value
                else:
                    last, i = a, i + 1           # --flag=value, or a bare switch
                continue
            # The flag in flight when the FIRST stray appears is the one whose value lost its
            # quotes. Recording `last` at the end of the loop instead named whatever flag happened
            # to come after the damage.
            if owner is None:
                owner = last
            stray.append(a)
            i += 1
        return stray, owner

    @classmethod
    def _job_flag_value(cls, prompt, flag):
        """The value bound to one flag in a job's own vetted command line — `--flag value` or
        `--flag=value` — or None.

        Only ever looks at argv AFTER the vetted script's filename (_job_vetted_argv), the same
        restriction _job_stray_args already enforced — so text elsewhere in the prompt can never
        be read as this job's own instruction. Used to enrich a subscription confirmation with
        whatever the job was actually told to watch — best-effort, never guessed: a value not
        found here is simply left out of the confirmation rather than invented.
        """
        argv = cls._job_vetted_argv(prompt)
        if not argv:
            return None
        prefix = flag + "="
        for i, a in enumerate(argv):
            if a == flag:
                return (argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--")
                        else None)
            if a.startswith(prefix):
                return a[len(prefix):]
        return None

    @classmethod
    def _job_patch(cls, job, uname):
        """(PATCH body, repaired defects, unrepairable defects) for one job.

        Repair is only ever MECHANICAL: set a field to the one value the brief allows, cut markup at
        its first character, append a fixed block. Nothing here rewrites what the job *does* — a
        prompt this pipe authored would be the pipe guessing at an itinerary or a threshold, which
        is the exact fabrication the flight path (:1728) exists to refuse.
        """
        defects = cls._job_defects(job)
        original = job.get("prompt") or ""
        prompt = original
        fixed, stuck, dead = [], [], False
        for defect in defects:
            code = defect[0]
            if code == "markup":
                body = prompt[:cls._JOB_MARKUP_RE.search(prompt).start()].rstrip()
                # A prompt that is ONLY markup has no job left in it. Truncating to near-nothing
                # would leave a scheduled job that runs and does something arbitrary — worse than
                # one flagged for the user to cancel.
                if len(body) < cls._JOB_PROMPT_MIN:
                    dead = True
                    break
                prompt = body
                fixed.append(defect)
            elif code == "argv":
                # Mechanical and narrow: put the quotes back around the value that lost them, and
                # ONLY when the strays sit directly after a known free-text flag. That is not a
                # guess about intent — argparse says those tokens belong to nothing, and the single
                # reading consistent with the command as written is that they are the tail of the
                # preceding value. Anything else (strays after an unknown flag, or after none at
                # all) is reported instead, because re-joining there would be authoring.
                stray, owner = cls._job_stray_args(prompt)
                fixed_line = None
                if owner in cls._JOB_TEXT_FLAGS and stray:
                    import shlex
                    for ln in prompt.splitlines():
                        if not cls._JOB_VETTED_RE.search(ln):
                            continue
                        argv = shlex.split(ln.strip())
                        k = argv.index(owner)
                        val = " ".join(argv[k + 1:k + 2 + len(stray)])
                        rebuilt = argv[:k + 1] + [val] + argv[k + 2 + len(stray):]
                        fixed_line = " ".join(shlex.quote(t) if (" " in t or not t) else t
                                              for t in rebuilt)
                        # shlex.quote would also quote the interpreter and path; harmless, but keep
                        # the line readable by leaving space-free tokens bare (above).
                        prompt = prompt.replace(ln.strip(), fixed_line)
                        break
                if fixed_line and not cls._job_stray_args(prompt)[0]:
                    fixed.append(defect)
                else:
                    stuck.append(defect)
            elif code == "protocol":
                tail = cls._JOB_PROTOCOL_TAIL.format(who=uname)
                if len(prompt) + len(tail) > cls._JOB_PROMPT_MAX:
                    stuck.append(defect)
                else:
                    prompt += tail
                    fixed.append(defect)
            else:
                fixed.append(defect)
        if dead:
            # Half-fixing a job we are about to tell the user to cancel is worse than not touching
            # it: it writes a prompt nobody authored to a job nobody wants. Report all of it.
            return {}, [], defects
        patch = {"deliver": "local"} if any(d[0] == "deliver" for d in fixed) else {}
        if prompt != original:
            patch["prompt"] = prompt
        return patch, fixed, stuck

    def _enforce_job_shape(self, jobs, uname):
        """Repair what just-created jobs got wrong, and report whatever could not be repaired.

        Returns user-facing text, or "" when every job was already well-formed — silence on a clean
        creation is the same discipline the verifier above keeps: speak only when this check
        DISAGREES with the agent. Blocking (urllib via _hermes_api); callers wrap it in a thread.
        """
        lines = []
        for job in jobs:
            jid = job.get("id") or "?"
            patch, fixed, stuck = self._job_patch(job, uname)
            if patch:
                status, data, err = self._hermes_api("PATCH", f"/api/jobs/{jid}", patch)
                if err:
                    # A repair that did not land must never read as one that did. Fold the whole
                    # attempt into the unrepairable list and name the transport failure.
                    reason = self._api_err_text(data) or err
                    stuck = stuck + [(c, w, f"{cost} (repair failed: {reason})")
                                     for c, w, cost in fixed]
                    fixed = []
            said = [d for d in fixed if d[0] not in self._JOB_SILENT_FIX]
            if said:
                lines.append(f"🔧 **Repaired job `{jid}` before its first run** — "
                             + "; ".join(w for _, w, _ in said)
                             + ". Confirmed against the scheduler, not the agent's word.")
            if stuck:
                lines.append(f"⚠️ **Job `{jid}` was created malformed and I could not fix it** — "
                             + "; ".join(f"{w}, so {cost}" for _, w, cost in stuck)
                             + f". Say *cancel {jid}* to remove it.")
        return ("\n\n" + "\n\n".join(lines)) if lines else ""

    @staticmethod
    def _alert_username(user):
        """OpenWebUI identity -> a short stable handle for addressing alerts. Transport-neutral:
        whatever carries personal alerts (SMS, email, push) is keyed on this, not on the email."""
        u = user or {}
        local = (u.get("email") or "").split("@")[0] or (u.get("name") or "")
        handle = re.sub(r"[^a-z0-9_-]", "", local.lower())
        return handle or "user"

    async def _hermes_stream(self, text, uname="user", verify_creation=False, brief=None,
                             scoped=False):
        """Delegate a background-task request to the local hermes-agent API server.

        A plain HTTP client, deliberately: hermes's API server is an agent runtime that streams
        SSE chunks including inline tool-progress markers, so the user watches the agent work and
        then gets its confirmation — inside the same single chat entry. No second model row, no
        bypass of this pipe.

        `brief` selects the contract: the default cron brief for scheduling, or _RESEARCH_BRIEF for
        one-shot work. They are not interchangeable — handing a research question the cron brief
        would tell the agent to create a job nobody asked for.

        `scoped` says the requester is an ordinary user, not an admin. It does two things: the
        agent is told which job ids are that user's so its duplicate-check cannot describe someone
        else's monitor, and the verdicts below refuse to print details of a job the user does not
        own. `uname` doubles as the ownership key for anything this turn creates.
        """
        key = self._hermes_key()
        if not key:
            yield ("⚠️ Background tasks are configured but the hermes-agent key is missing "
                   f"({HERMES_KEY_FILE}). Is the hermes gateway set up on this host?")
            return
        # Snapshot BEFORE the brief is built: the ctx below names the user's own job ids, which
        # requires knowing what exists. Unconditional now — it used to be taken only when a verdict
        # was wanted, which meant follow-up and /research turns created jobs the pipe never learned
        # the id of, leaving them unowned and so invisible to the person who asked for them.
        before = self._hermes_jobs()
        owners, _oerr = self._read_owners()
        if brief is None:
            brief = self._HERMES_BRIEF
            ctx = (f"Request context: the requesting user is '{uname}'. If this job needs to "
                   f"alert them, the ALERT line's recipient is '{uname}'.")
            kind = self._guess_kind(text)
            # Deterministic pipe-side steering beats hoping the agent notices a rule: a stock watch
            # built as a price watch is the advertised-but-unimplemented bug all over again.
            if kind in ("back_in_stock", "out_of_stock", "inventory", "availability"):
                ctx += (" This is a stock/availability watch, not a price watch: rule 5d-iii "
                        "applies — pass --mode stock and no price threshold.")
            ctx += (f" The request is a '{kind}' watch — pass --kind {kind}."
                    if kind else
                    " Choose --kind yourself from: price_drop, price_rise, back_in_stock, "
                    "out_of_stock, fare, inventory, availability, threshold, change — whichever "
                    "best describes what the user is watching for.")
            # Creation turns only: a follow-up replays a transcript blob and a fallen-through
            # manage turn is about an EXISTING job — telling either to "use the no-URL recipe"
            # steers the agent toward creating something nobody asked for.
            if (verify_creation and kind in ("price_drop", "price_rise", "fare")
                    and not re.search(r"https?://|\bwww\.", text or "", re.I)):
                ctx += (" The request names WHAT to watch but gives NO URL. Prefer the no-URL "
                        "recipe (rule 5d-ii, scripts/price_search.py with --query) over asking "
                        "the user for a link, and never write a search job of your own.")
        else:
            ctx = f"Request context: the requesting user is '{uname}'."
        if scoped:
            # The cron brief tells the agent to list existing jobs and refuse to duplicate an
            # active one. On a shared scheduler that means it would answer "you already have that"
            # while describing a monitor belonging to someone else. Name the user's own ids so the
            # duplicate check has a scope. This is instruction, not enforcement — the hard
            # guarantees are the deterministic scoped path and the verdict filters below.
            mine = [i for i in (before or {}) if (owners.get(i) or {}).get("h") == uname]
            ctx += (" Duplicate-check scope: of the jobs already in the scheduler, ONLY these "
                    "belong to this user: " + (", ".join(sorted(mine)) or "(none)") +
                    ". Every other job belongs to someone else — never name, cite, quote or "
                    "describe one, and never treat one as this user's duplicate.")
        # hermes runs hermes-genesis:agent at num_ctx 65536 while chat holds apex-compact at 32768.
        # Ollama keys runners by model+options, so those are two distinct ~17 GB allocations and
        # only one fits. Releasing the chat tenant first makes the handoff deterministic instead of
        # leaving Ollama to evict under memory pressure mid-load.
        #
        # to_thread because _release_chat_tenant is a blocking requests call: running it inline
        # would stall the event loop, and this pipe serves every other conversation on the box.
        await asyncio.to_thread(self._release_chat_tenant)
        payload = {"model": "hermes-agent", "stream": True,
                   "messages": [{"role": "system", "content": brief + "\n" + ctx},
                                {"role": "user", "content": text}]}
        reply = ""          # accumulated so verification can check ids the agent cites
        after = None
        # Verification class for the metrics stream. Starts at 'incomplete' so a stream that dies
        # without [DONE] — client disconnect, hermes crash mid-answer — is visible as exactly that
        # rather than as a missing row.
        outcome = "incomplete"
        stamped = 0
        repaired = 0        # brief violations found in what the agent just created (:4863)
        t0 = time.monotonic()

        def _attribute(snapshot, src_hint="diff"):
            """Stamp whatever this turn created. Returns the new ids.

            Prefers ids the agent actually CITED in its reply: brief rule 9 makes it print the real
            id, and a cited id is evidence this turn created it. The bare snapshot diff is
            host-wide, so a job another user created in the same seconds would otherwise be
            attributed here.
            """
            nonlocal stamped
            if snapshot is None or before is None:
                return []
            fresh = sorted(set(snapshot) - set(before))
            if not fresh:
                return []
            cited = [i for i in re.findall(r"\b[0-9a-f]{12}\b", reply) if i in fresh]
            pick, src = (cited, "cited") if cited else (fresh, src_hint)
            stamped += self._stamp_owner(pick, uname, src=src, live_ids=set(snapshot))
            return fresh

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=HERMES_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{HERMES_URL}/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {key}"}) as r:
                    if r.status != 200:
                        outcome = f"http_{r.status}"
                        body = (await r.text())[:300]
                        yield f"⚠️ hermes-agent HTTP {r.status}: {body}"
                        return
                    async for line in r.content:
                        line = line.strip()
                        if not line or not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            if verify_creation:
                                # The agent's cronjob write is not instantly visible in /api/jobs —
                                # polling at [DONE] raced it and cried wolf on a job that DID exist.
                                # Retry briefly: a real creation surfaces within a second or two, a
                                # fabricated one never does.
                                # _job_live, not a local copy: the job table and this verifier must
                                # never disagree about what counts as still-going.
                                _runnable = self._job_live
                                new_jobs = None
                                for _ in range(6):
                                    after = self._hermes_jobs()
                                    if after is not None and before is not None:
                                        new_jobs = [j for i, j in after.items()
                                                    if i not in before and _runnable(j)]
                                        if new_jobs:
                                            break
                                    await asyncio.sleep(1)
                                # Stamp ownership before any verdict: everything below reports on
                                # jobs, and a job with no owner is invisible to the person who
                                # just asked for it.
                                _attribute(after)
                                if new_jobs:
                                    # Verified, and deliberately SILENT about it. The check itself
                                    # is load-bearing — the agent has claimed jobs it never
                                    # created — but a success line is the pipe narrating its own
                                    # internals: the agent has already told the user what was
                                    # scheduled and quoted the id. Every verdict below still
                                    # speaks, because each of those is the check DISAGREEING with
                                    # the agent, which is the only part the reader needs.
                                    outcome = "created"
                                    if len(new_jobs) > 1:
                                        # Except this: more jobs exist than the agent described.
                                        yield (f"\n\nℹ️ Note: {len(new_jobs)} tasks were created, "
                                               f"not one. Say *list my tasks* to see them.")
                                    # Verified-to-exist is not verified-to-work. The agent has
                                    # created jobs that could never deliver and whose prompts
                                    # carried its own tool-call markup (:4863). Hold the record to
                                    # the three parts of the brief a machine can check, before the
                                    # first run rather than after it. to_thread per _hermes_api's
                                    # contract — this pipe serves every other chat on the box.
                                    # Counted BEFORE the repair, and NOT gated on whether the repair
                                    # had anything to say. A deliver-only defect is fixed silently
                                    # (:4900), so gating this on `shape` recorded repaired=0 for the
                                    # single most common violation there is — the exact rate the
                                    # column exists to measure, missing precisely where the reply is
                                    # already silent. Measured after the fact: the metric read 0
                                    # while a PATCH had demonstrably gone out.
                                    repaired = sum(len(self._job_defects(j)) for j in new_jobs)
                                    shape = await asyncio.to_thread(
                                        self._enforce_job_shape, new_jobs, uname)
                                    if shape:
                                        # A repaired creation is still a creation: 'created' stays
                                        # countable and malformation gets its own column, rather
                                        # than a new outcome class that silently shrinks the first.
                                        yield shape
                                    # The trip, read back out of the stored command rather than
                                    # taken from the agent's prose. This is the one part of a fare
                                    # watch the reader cannot check any other way — the confirmation
                                    # above is written by the model, and on 2026-08-08 it said
                                    # "Flight price watch set up" over a job with no dates in it.
                                    # Silence on a well-formed creation is still the rule elsewhere;
                                    # this speaks because the dates ARE the deliverable (the same
                                    # argument scripts/alert_templates.py:449 makes for alerts), and
                                    # because their absence is a defect no other check reports.
                                    for trip in (self._job_trip(j) for j in new_jobs):
                                        if trip:
                                            yield f"\n\n{trip}"
                                    yield self._alert_setup_block(uname)
                                    self._enqueue_subscriptions_for(new_jobs, uname)
                                elif new_jobs is not None and self._changed_jobs(
                                        before, after, owner=uname if scoped else None,
                                        owners=owners):
                                    # An UPDATE is not a creation. "change my alert to every 5
                                    # minutes" produced a correctly rescheduled job AND a message
                                    # saying nothing had been created — the loudest possible way to
                                    # report success. Ground truth, not keywords: if a job the
                                    # scheduler already had now has a different schedule, state or
                                    # run budget, something real happened.
                                    outcome = "updated"
                                    jid, what = self._changed_jobs(
                                        before, after, owner=uname if scoped else None,
                                        owners=owners)[0]
                                    j = after[jid]
                                    sched = (j.get("schedule_display")
                                             or str(j.get("schedule", "?")))
                                    yield (f"\n\n✅ **Verified updated**: job `{jid}` is now "
                                           f"{sched} ({what}) — confirmed against the scheduler, "
                                           f"not the agent's word.")
                                elif new_jobs is not None:
                                    # No NEW job is not the same as a fabrication. The agent may
                                    # legitimately have found an existing match and asked what to
                                    # do — that happened live, and calling it a fabrication told
                                    # the user to resend a request that was answered correctly.
                                    # Distinguish by checking any job id the reply cites against
                                    # the scheduler.
                                    cited = [i for i in set(re.findall(r"\b[0-9a-f]{12}\b", reply))
                                             if i in (after or {})]
                                    if scoped:
                                        # Re-read: the stamp above may have just created the record
                                        # this check depends on.
                                        fresh_owners, _ = self._read_owners()
                                        foreign = [i for i in cited
                                                   if (fresh_owners.get(i) or {}).get("h") != uname]
                                        cited = [i for i in cited if i not in foreign]
                                        if foreign and not cited:
                                            # The agent pointed at somebody else's monitor. Say
                                            # that nothing was created WITHOUT confirming what it
                                            # found — no id, no name, no schedule.
                                            outcome = "pointed_foreign"
                                            yield ("\n\nℹ️ The agent referred to an existing job "
                                                   "that is not yours, so I can't show its "
                                                   "details. **No new job was created for you** — "
                                                   "resend the request if you still want this "
                                                   "monitored.")
                                            yield self._marks()
                                            return
                                    live = [i for i in cited if _runnable(after[i])]
                                    if live:
                                        outcome = "pointed_active"
                                        j = after[live[0]]
                                        sched = (j.get("schedule_display")
                                                 or str(j.get("schedule", "?")))
                                        yield (f"\n\nℹ️ **No new job created** — the agent pointed "
                                               f"at an existing ACTIVE job: `{live[0]}` ({sched}). "
                                               f"Nothing was fabricated and it is still scheduled.")
                                    elif cited:
                                        # The failure this catches: the agent finds a FINISHED job
                                        # of the same name, calls it "already running", and creates
                                        # nothing — so the user's monitor silently does not exist.
                                        outcome = "finished_job"
                                        j = after[cited[0]]
                                        yield (f"\n\n⚠️ **Nothing is scheduled**: the agent pointed "
                                               f"at job `{cited[0]}`, which has already FINISHED "
                                               f"({j.get('state', 'completed')}, next run: none). "
                                               f"No new job was created. Reply "
                                               f"**\"create a new one\"** to schedule it properly.")
                                    else:
                                        outcome = "failed"
                                        yield ("\n\n⚠️ **Verification failed**: the agent described "
                                               "a job but the scheduler has NO matching entry — "
                                               "nothing was actually created. Please resend.")
                                else:
                                    outcome = "unverifiable"
                                    yield "\n\n(could not verify job creation — /api/jobs unreachable)"
                            else:
                                # No verdict is wanted (follow-up, /research, list) — but the turn
                                # can still have CREATED something: "yes, create a new one" after a
                                # finished-job verdict travels the follow-up path. Those jobs used
                                # to end up unowned, i.e. invisible to the person who asked for
                                # them. Two short attempts, then give up; the cost on the ordinary
                                # case (nothing created) is one local GET.
                                outcome = "n/a"
                                for attempt in range(2):
                                    after = self._hermes_jobs()
                                    if _attribute(after):
                                        break
                                    if attempt == 0:
                                        await asyncio.sleep(1)
                            return
                        try:
                            d = json.loads(data)
                        except Exception:
                            continue
                        tok = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
                        if tok:
                            reply += tok
                            yield tok
                    # The loop ran out of lines WITHOUT ever seeing "data: [DONE]" — the stream
                    # closed clean (no exception at all) rather than erroring, so nothing above
                    # ever ran the verdict machinery. `outcome` is still its initial "incomplete",
                    # and without this, NOTHING gets appended: whatever the agent had streamed so
                    # far — true or fabricated — stands as the entire reply with no verification
                    # and no warning that there was none. Measured live, 2026-08-10: a chat-only
                    # turn (not even this code path) fabricated a full "✅ Switched to daily
                    # checks" confirmation with zero tool access; this is the same failure mode
                    # ONE LAYER DEEPER — an agent turn that streamed real prose and then the
                    # connection dropped before its tool-verified truth ever got appended.
                    if verify_creation:
                        try:
                            _attribute(self._hermes_jobs(), src_hint="dropped_stream")
                        except Exception:
                            pass
                        yield ("\n\n⚠️ **Unverified**: the connection to hermes-agent closed before "
                               "this could be checked against the real scheduler. Treat the reply "
                               "above as UNCONFIRMED — ask me to list tasks to see what is "
                               "actually scheduled.")
        except asyncio.TimeoutError:
            outcome = "timeout"
            # The advice line below says the job may still have been created — so claim it before
            # saying so, or the user is told to go look for something they will not be able to see.
            try:
                _attribute(self._hermes_jobs(), src_hint="timeout")
            except Exception:
                pass
            yield ("\n\n⏳ hermes-agent did not finish within the window — the job may still have "
                   "been created. Check the background-tasks channel, or ask me to list tasks.")
            # The marker survives the failure ON PURPOSE. Both advice lines above invite a reply
            # ("ask me to list tasks", "start it and retry") — and without the marker that reply
            # matched no predicate and landed in plain chat, exactly when continuity mattered most.
        except aiohttp.ClientConnectorError:
            outcome = "unreachable_gateway"
            yield ("⚠️ hermes-agent is not reachable on 127.0.0.1:8642. "
                   "Start it with: `systemctl --user start hermes-gateway`")
        except aiohttp.ClientError as e:
            # Everything else aiohttp can raise mid-stream (a truncated chunked body, the socket
            # reset partway through) — distinct from ClientConnectorError above, which means the
            # gateway was never reached at all. Without this, one of these left the SAME silent
            # gap the missing-[DONE] case above closes: an uncaught exception here still reaches
            # `finally` (Python guarantees that), but propagates past every yield, so the user
            # sees whatever OpenWebUI does with a raised exception instead of an honest message.
            outcome = "stream_error"
            if verify_creation:
                try:
                    _attribute(self._hermes_jobs(), src_hint="stream_error")
                except Exception:
                    pass
            yield (f"\n\n⚠️ hermes-agent's connection broke mid-reply ({type(e).__name__}). "
                  "Treat anything above as UNCONFIRMED — ask me to list tasks to see what is "
                  "actually scheduled.")
        finally:
            # One row per delegation with the verification CLASS — the ready-made outcome signal
            # ("created" vs "failed" vs "timeout") that until now existed only as chat prose.
            # Sync only. On GeneratorExit (client disconnect) no await is permitted here, which is
            # why attribution happens above rather than in this block — a job created by a turn the
            # user walked away from stays unowned until an admin assigns it.
            self._metric(job="hermes", outcome=outcome, verify=bool(verify_creation),
                         scoped=bool(scoped), stamped=stamped, repaired=repaired,
                         duration_s=round(time.monotonic() - t0, 1))

    def _sampling(self, guard_text):
        """Sampling options for a chat turn, chosen by route.

        Keyed on the guard rather than the model tag because chat, code and vision all resolve to
        the same tag now — the guard is the only thing that still records which branch was taken,
        which is exactly how tests/eval/run_eval.py identifies the coder route on the wire."""
        opts = dict(CODER_OPTIONS if guard_text is self._CODER_GUARD else CHAT_OPTIONS)
        if EVAL_DETERMINISTIC:
            opts.update(temperature=0, seed=EVAL_SEED)
        return opts

    @staticmethod
    def _fit_ctx(messages):
        """A per-request num_ctx sized to the conversation, rounded UP to a power-of-two step.

        OLLAMA_CONTEXT_LENGTH=32768 is allocated for every turn regardless of how short it is,
        and the KV cache is sized from it — so a two-line question reserves the same VRAM as a
        30k-token thread. Sizing per request hands that back on short turns, which is most of
        them, at measured-zero throughput cost (the coder is flat 123-125 tok/s from 4k to 32k).

        ROUND UP ONLY, and never below CTX_FLOOR. Ollama runs with --context-shift, so an
        under-sized window does not error — it silently evicts the front of the conversation, and
        the model answers a question it can no longer fully see. A too-small ctx is therefore far
        worse than a too-large one, which is only wasted VRAM. The 3.2 chars/token estimate is
        deliberately pessimistic (real English is ~4) and the doubling step absorbs the rest.
        """
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        need = int(chars / 3.2) + CTX_HEADROOM
        n = CTX_FLOOR
        while n < need and n < CTX_MAX:
            n *= 2
        return min(n, CTX_MAX)

    async def _achat_stream(self, messages, guard_text=None, keep_system=False, force_model=None):
        """Streamed Ollama chat.

        The default guard is the general-assistant one. Callers override it for coding turns — the
        guard is deliberately NOT shared, because telling a coding model to "NEVER output JSON" would
        break it outright.
        """
        # Two jobs. (1) Keep the model from inventing "dalle"/tool-call JSON: media is routed by the
        # app, not requested by the model. (2) Preserve [id] citation markers verbatim — with legacy
        # function calling, web-search and document context arrive already carrying them, and OWUI
        # only renders clickable source badges if they survive into the reply.
        guard = {"role": "system", "content": guard_text if guard_text is not None else (
            "You are a friendly, concise assistant in a chat app. Image and video generation and "
            "editing are handled automatically by the app, not by you. Always reply in plain, "
            "natural language. NEVER output JSON, tool calls, function calls, or an "
            "\"action\"/\"dalle\"/\"text2im\" object. If the user asks to create or edit a picture or "
            "video, just acknowledge briefly in words.\n"
            "When context from documents or a web search is provided, answer from it, keep any [id] "
            "citation markers exactly as given, and say plainly when the answer is not in the "
            "context rather than guessing.\n"
            "If the app has given you a tag syntax to use — for example a <code_interpreter> block — "
            "follow those instructions and emit the tag raw. Never wrap such a tag in a markdown "
            "code fence; fenced tags are displayed as text instead of being executed.")}
        if keep_system:
            # Preserve OpenWebUI's own system messages (native memory, Adaptive Memory, RAG system
            # context). The historical unconditional strip below is what silently discarded them.
            messages = [guard] + list(messages)
        else:
            messages = [guard] + [m for m in messages if m.get("role") != "system"]

        # Collapse every system message into exactly ONE, guard first.
        #
        # Not cosmetic. Sending two system messages to hermes-genesis:apex-compact makes Ollama fail
        # the whole request with HTTP 400 — "Unable to generate parser for this template" — because
        # that GGUF's embedded chat template cannot handle more than one. Measured: 1 system message
        # works, 2 is a hard 400. keep_system=True produces exactly that shape (our guard + OWUI's
        # memory/context), so every turn carrying memory or a system convention broke.
        #
        # Stock Qwen3.6 tolerates multiple, so this is not a universal requirement — but one system
        # message is what most chat templates actually expect, so merging is the portable shape and
        # removes a whole class of model-specific breakage rather than special-casing one tag.
        sys_parts = [m.get("content") for m in messages
                     if m.get("role") == "system" and m.get("content")]
        if len(sys_parts) > 1:
            messages = ([{"role": "system", "content": "\n\n".join(sys_parts)}]
                        + [m for m in messages if m.get("role") != "system"])
        # Route on the CURRENT turn only: gemma4 (vision) when the LATEST user turn carries an image,
        # else dolphin. Scanning the whole history pinned every later text turn to gemma4 forever
        # after a single image appeared, forcing a needless dolphin<->gemma4 eviction each turn.
        last_user_has_img = next(
            (bool(m.get("images")) for m in reversed(messages) if m.get("role") == "user"), False)
        if force_model:
            model = force_model
            if not last_user_has_img:
                messages = [{k: v for k, v in m.items() if k != "images"} for m in messages]
        elif last_user_has_img:
            model = self.vision_model
        else:
            model = self.chat_model
            # dolphin is text-only — drop any stale image arrays from history so it never sees images[]
            messages = [{k: v for k, v in m.items() if k != "images"} for m in messages]
        try:
            # sock_read (not a fixed total) catches an idle hang without killing a long, actively
            # streaming reply.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{self.ollama}/api/chat",
                                  json={"model": model, "messages": messages,
                                        "stream": True, "think": False,
                                        "options": {**self._sampling(guard_text),
                                                    "num_ctx": self._fit_ctx(messages)}}) as r:
                    if r.status != 200:
                        body = (await r.text())[:300]
                        yield f"⚠️ Ollama HTTP {r.status} from {model}: {body}"
                        return
                    async for line in r.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("error"):
                            yield f"⚠️ Ollama error: {d['error']}"
                            return
                        tok = (d.get("message") or {}).get("content", "")
                        if tok:
                            yield tok
        except Exception as e:
            yield f"⚠️ Chat backend error: {e}"

    # Guard used when a turn is routed to the coder tenant. Note what it does NOT say: the default
    # guard forbids emitting JSON, which would be actively harmful here — JSON is frequently the
    # correct answer to a coding question. It keeps the citation rule, because a coding turn can
    # still carry web-search or document context.
    _CODER_GUARD = (
        "You are an expert programming assistant. Prefer complete, runnable code over fragments, "
        "state the language and any assumptions, and point out real bugs or edge cases you notice. "
        "Use fenced code blocks. Image and video generation are handled by the app, not by you. "
        "When context from documents or a web search is provided, keep any [id] citation markers "
        "exactly as given.")

    # Retained for backwards compatibility only. The manifold collapsed to a single 'auto' entry, so
    # nothing selectable reaches this any more — but a chat saved against the old
    # auto_assistant.knowledge / .coder ids, or a direct API caller using them, still degrades to
    # sensible behaviour instead of erroring.
    _KNOWLEDGE_GUARD = None      # None -> _achat_stream uses the default general-assistant guard

    def _entry_chat_stream(self, entry, messages):
        """Chat path for the non-'auto' manifold entries. No media routing reaches here at all.

        The coder entry loads a large tenant (Phase 8: ~17.7 GB), so it is serialized under the same
        _GEN_LOCK the render pipelines use — that is the whole reason these entries live in this file.
        The lock is acquired on a worker thread (never on the event loop) and released when the
        stream finishes or the client disconnects, matching the _gen_* pattern above.

        Ollama evicts among its OWN models by itself; the lock exists to stop a coder load landing in
        the middle of a ComfyUI render, which Ollama cannot see.
        """
        if entry == "coder":
            guard, model = self._CODER_GUARD, self.coder_model
        else:
            guard, model = self._KNOWLEDGE_GUARD, None

        # Deliberately NOT an async generator: it RETURNS the stream rather than iterating it. Wrapping
        # one generator in another breaks close propagation — aclose() on the outer raises
        # GeneratorExit there, and the inner is only finalised whenever GC gets to it, so a client
        # disconnect would hold _GEN_LOCK (and the GPU) for an unbounded time.
        inner = self._achat_stream(messages, guard_text=guard, keep_system=True, force_model=model)
        return self._locked_stream(inner) if entry == "coder" else inner

    async def _gen_and_cache(self, cid, prompt, ref, msgs=None, original=None):
        def run():
            with _gpu_lock():  # only one VRAM-manipulating pipeline at a time
                return self._gen_image(prompt, ref, msgs, original=original)
        result = await asyncio.to_thread(run)
        b64 = self._extract_b64(result)
        if b64:  # remember the produced image so a later "make it bigger" can edit it (true LRU)
            self._recent.pop(cid, None)  # move-to-end so an active chat isn't evicted first
            self._recent[cid] = b64
            while len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
            if ms:  # write-through to the persistent per-chat store: survives deploys and
                ms.remember_image(cid, b64)  # is visible to the Image/Photoreal pipes too
        return result

    def _cache_video(self, cid, prompt, seed, opts=None):
        self._recent_video.pop(cid, None)  # move-to-end → true LRU (not FIFO)
        self._recent_video[cid] = (prompt, seed, opts)
        while len(self._recent_video) > 30:
            self._recent_video.pop(next(iter(self._recent_video)))

    async def _gen_video_and_cache(self, cid, prompt, seed=None, enhance=False, opts=None):
        seed = seed if seed is not None else random.randint(0, 2**31)

        def run():
            with _gpu_lock():  # only one VRAM-manipulating pipeline at a time
                self._comfy_free()  # free ComfyUI before the dolphin enhance helper loads
                p = self._enhance_video(prompt) if enhance else prompt
                # QA judges against the pre-enhancement wording (the user's ground truth) — the
                # enhanced prompt could itself have dropped a spec.
                return p, self._gen_video(p, seed, opts, check=prompt)

        used_prompt, result = await asyncio.to_thread(run)
        if result.lstrip().startswith("<video"):  # remember so "change the sky" can iterate on it
            self._cache_video(cid, used_prompt, seed, opts)
        return result

    async def _gen_i2v_and_cache(self, cid, img_b64, prompt, opts=None, seed=None):
        """Animate a still (attached or just-generated) with Wan 2.2 I2V — text-directed motion
        FROM the image. Distinct from the Animate pipe (SCAIL motion-transfer off a driving clip)."""
        seed = seed if seed is not None else random.randint(0, 2**31)
        opts = opts or {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}

        def run():
          with _gpu_lock():  # only one VRAM-manipulating pipeline at a time
            self._comfy_free()  # free ComfyUI before the dolphin motion-prompt helper loads
            motion = self._enhance_video(prompt) if prompt else "natural, gentle cinematic motion"
            self._free_vram()
            try:
                name = self._upload(img_b64)
            except Exception as e:
                return None, f"⚠️ Could not upload the image to animate: {e}"
            wf = self._wf_video_i2v(motion, seed, opts["w"], opts["h"],
                                    opts.get("length", V_LEN_14B), name, name)
            data, err, _ = self._submit_poll(wf, "save", "Video", 900)
            self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
            if err:
                return None, err
            b64 = base64.b64encode(data).decode()
            p64 = base64.b64encode(motion.encode()).decode()
            return motion, (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                            f'controls loop muted playsinline style="max-width:100%;border-radius:8px">\n'
                            f'data:video/webm;base64,{b64}\n</video>\n\n*🎬 {(prompt or motion)[:80]}*')

        used_prompt, result = await asyncio.to_thread(run)
        if isinstance(result, str) and result.lstrip().startswith("<video"):
            self._cache_video(cid, used_prompt, seed, opts)
        return result

    async def _gen_multishot_and_cache(self, cid, text, n, opts, seed=None):
        seed = seed if seed is not None else random.randint(0, 2**31)
        def run():
            with _gpu_lock():  # only one VRAM-manipulating pipeline at a time
                return self._gen_multishot(text, seed, opts, n)
        result = await asyncio.to_thread(run)
        if result is None:
            # Shot planning failed → one enhanced clip. SAY SO. This silently turned a requested
            # 15-second sequence into a 5-second clip, and the user had no way to tell that from
            # the model simply deciding one shot was enough. A silent product downgrade is the
            # worst failure mode in this file; the sentence is worth more than the retry above.
            single = await self._gen_video_and_cache(cid, text, seed, enhance=VID_ENHANCE, opts=opts)
            if isinstance(single, str) and self._is_media(single):
                single += (f"\n\n*Could not plan the {n}-shot sequence — this is a single "
                           f"{V_LEN_14B // 16}-second clip instead. Ask again to retry.*")
            return single
        if result.lstrip().startswith("<video"):
            v = re.search(r'data-p64="([A-Za-z0-9+/=]*)"', result)
            try:
                joined = base64.b64decode(v.group(1)).decode("utf-8", "ignore") if v else text
            except Exception:
                joined = text
            self._cache_video(cid, joined, seed, opts)
        return result

    # ---------- native OpenWebUI status line (progress + timing for media) ----------
    async def _status(self, emitter, description, done=False):
        """Emit a native OpenWebUI status event (the grey status strip under the message)."""
        if emitter:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def _confirm_render(self, event_call, kind, detail, expensive=True):
        """Ask before spending minutes of GPU on a render. True = go ahead.

        Fails OPEN on every path that is not an explicit "no": no client attached (the eval
        harness and any direct API caller), the socket disconnected, an unsupported client, a
        malformed answer, or an exception. The gate exists to stop a misrouted request wasting the
        card — it must never be the reason a correctly-routed render fails to happen, and it must
        never hang a caller that has no user behind it.

        OpenWebUI's client renders type "confirmation" from data.title/data.message and returns the
        user's answer through sio.call (frontend handler; backend socket/main.py:1039).
        """
        want = CONFIRM_RENDERS
        if want == "never":
            return True
        # Keyed on measured COST, not on the noun. A Qwen-Image-Edit round is 162 s median (n=26)
        # against 16.3 s for a fresh Krea image, so an edit belongs with video under "video" even
        # though the user would call both "an image".
        gated = want == "all" or expensive
        if event_call is None:
            # Fail-open is the contract, but an UNCOUNTED fail-open silently poisons the gate's
            # accept/decline stream — which is the live false-positive counter the roadmap needs
            # before any heuristic is allowed to relax. Count it, then proceed as before.
            if gated:
                self._metric(job="confirm", kind=kind, outcome="fail_open_no_client")
            return True
        if not gated:
            return True
        try:
            answer = await event_call({
                "type": "confirmation",
                "data": {"title": f"Generate this {kind}?",
                         "message": f"{detail}\n\nThis holds the GPU and pauses chat until it "
                                    f"finishes."},
            })
        except Exception:
            self._metric(job="confirm", kind=kind, outcome="fail_open_error")
            return True
        # sio.call returns {'error': ...} on a dead session; anything non-boolean is "not a no".
        declined = answer is False or (isinstance(answer, dict)
                                       and answer.get("confirmed") is False)
        self._metric(job="confirm", kind=kind, outcome="declined" if declined else "accepted")
        return not declined

    @staticmethod
    def _declined(kind):
        return (f"Okay — no {kind} generated. If you meant that as a question rather than a "
                f"request, just ask it and I'll answer in chat.")

    @staticmethod
    def _fmt_dur(secs):
        s = int(round(secs))
        return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"

    @staticmethod
    def _is_media(result):
        return isinstance(result, str) and (result.startswith("![") or result.lstrip().startswith("<video"))

    async def _tracked(self, emitter, label, coro):
        """Await a generation coroutine while emitting a live elapsed-time status, native-style.
        Returns (result, elapsed_seconds). The heavy work runs in a worker thread, so the 2 s
        ticker keeps updating without blocking it."""
        start = time.monotonic()
        await self._status(emitter, f"{label}…")
        task = asyncio.ensure_future(coro)
        while True:
            try:
                res = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                return res, time.monotonic() - start
            except asyncio.TimeoutError:
                await self._status(emitter, f"{label}… {self._fmt_dur(time.monotonic() - start)}")

    async def _finish(self, emitter, result, verb, elapsed, detail):
        """Collapse the live status to a final 'Generated in 4m 12s · …' line (or clear it on error)."""
        if self._is_media(result):
            await self._status(emitter, f"{verb} in {self._fmt_dur(elapsed)} · {detail}", done=True)
        else:
            await self._status(emitter, "", done=True)  # error text is already in the message body
        return result

    async def pipe(self, body: dict, __metadata__=None, __event_emitter__=None, __event_call__=None,
                   __user__=None, __task__=None):
        emitter = __event_emitter__
        # None whenever nothing can be asked (direct API, eval harness); _confirm_render fails open.
        confirm = __event_call__
        msgs = body.get("messages", [])
        text, ref = self._last_user(msgs)
        # OpenWebUI PREPENDS retrieved file/knowledge/web-search context to the LAST USER message
        # (RAG_SYSTEM_CONTEXT defaults false), so `text` can be a multi-kB document blob. Every
        # routing predicate below reads `text`, and _is_image_request/_is_video_request fire on a
        # bare "picture of"/"draw"/"video" anywhere in it — while the _QUESTION/_SMALLTALK guards
        # use anchored .match() and can never fire because the blob starts with "### Task:". Net
        # effect: attaching a PDF that merely mentions those words launches a multi-minute
        # Krea/Wan render built from the document text. Middleware stashes the user's verbatim
        # words BEFORE that injection (middleware.py:2803), so route on those instead.
        # Chat is unaffected: it uses `msgs`/`omsgs` below, which still carry the full RAG context.
        #
        # This resolution has to happen BEFORE the task guard below, not after. The guard's text
        # fallback matches a leading "### Task:", and OpenWebUI's RAG envelope opens with exactly
        # that — so with web search on, every ordinary turn looked like internal machinery and was
        # answered by the 1 B task model on RAG boilerplate. It replied with the template's own
        # example citation and the router never ran. Testing the guard against the user's real
        # words is what keeps "delete the job 6dc…" a task instruction rather than boilerplate.
        routed = (__metadata__ or {}).get("user_prompt")
        if isinstance(routed, str) and routed.strip():
            # ...but user_prompt is captured AFTER inlet filters run, so strip their blocks too.
            # The emptiness check is on the RAW value: an absent user_prompt must fall back to
            # _last_user (direct-API calls), whereas a prompt that was ENTIRELY injected context
            # must stay empty and route to chat — falling back there would hand the router the very
            # block we just removed.
            text = self._strip_injected_context(routed).strip()
        else:
            # No user_prompt (direct API, older middleware): strip the fallback too, so a RAG-
            # wrapped turn still routes on the question rather than on the envelope.
            text = self._strip_injected_context(text).strip()
        # OpenWebUI background tasks (title / follow-up / tags / web-search decisions) arrive
        # through this pipe whenever the configured task model isn't visible in the model
        # registry. Before this guard each one ran the FULL router — real 14-174 s GPU renders
        # of '### Task:' boilerplate (see media_metrics.jsonl) that also overwrote the chat's
        # last-image cache with junk, which is how "make this picture realistic" got applied
        # to a stranger's photo. Answer them as plain text on the small task model instead.
        if __task__ or (ms.is_task_request(text) if ms else self._is_owui_task(text)):
            # No request text on this row on purpose: it is '### Task:' boilerplate at title/tag
            # frequency. The row itself is the standing invariant — a task_guard row carrying any
            # other tier/rule means the guard stopped being the first check.
            self._route_metric("task_guard", 0, "task_kwarg" if __task__ else "task_prefix")
            return ms.answer_task(self.ollama, msgs) if ms else ""
        # Manifold dispatch. knowledge/coder are chat-only: returning here means NOT ONE media regex
        # runs, so a document that merely mentions "video" cannot start a render on those entries
        # regardless of what the router would have decided. Belt and braces on top of the
        # user_prompt fix above, which protects the 'auto' entry.
        entry = self._entry(body)
        if entry != "auto":
            self._route_metric(f"entry:{entry}", 0, "manifold_entry")
            return self._entry_chat_stream(entry, self._ollama_messages(msgs))
        cid = self._chat_id(body, __metadata__)
        # The Task control (filters/task_mode.py). Placed ABOVE every media branch and the coder
        # tier on purpose: each of those returns unconditionally once it matches, so anything below
        # this line could still swallow the turn — "draw a cat" with the control on has to reach
        # the agent, not the renderer. It stays BELOW the __task__ guard, which must keep winning:
        # OpenWebUI's own title and tag prompts are not the user asking for anything.
        #
        # Requires actual text. A turn that is only an attached image has nothing to delegate, so
        # it falls through to normal routing rather than becoming an empty request.
        tm_src = self._task_mode(__metadata__)
        if BG_TASKS and tm_src and (text or "").strip():
            done = await self._task_mode_turn(cid, text, msgs, __user__, tm_src, ref=ref)
            if done is not None:
                return done
        # What media does this conversation currently revolve around? History first, then the
        # in-memory caches, then the persistent per-chat store — the last one survives deploys
        # and mid-chat model switches (it is shared with the Image and Photoreal pipes).
        kind, media = self._recent_media(msgs)
        if kind is None:  # history scan found nothing → in-memory caches (this conversation only)
            if cid in self._recent_video:
                kind, media = "video", self._recent_video[cid]
            elif cid in self._recent:
                kind, media = "image", self._recent[cid]
            elif ms:
                persisted = ms.recall_image(cid)
                if persisted:
                    kind, media = "image", persisted
        # "make it animated / a cartoon" while an image is on the table is a STYLE EDIT of that
        # image, not a video ("make a video of it / animate this / make it move" still are).
        style_edit = kind == "image" and text and not ref and self._style_conversion(text)
        # Image on the table + a motion request → animate THAT image with Wan I2V (text-directed
        # motion). A freshly attached image always qualifies; a previously-generated image only when
        # the message refers to it ('animate this', 'bring it to life') rather than naming a new scene.
        anim_img = ref or (media if kind == "image" else None)
        refers_to_img = bool(re.search(r"\b(this|it|that|the\s+(image|photo|picture|drawing|pic))\b|"
                                       r"\banimate\b|\bbring\b.*\blife\b", (text or "").lower()))
        if (text and anim_img and not style_edit and (ref or refers_to_img)
                and (self._is_video_request(text) or self._wants_new_video(text))):
            self._route_metric("media:animate", 1, "animate_image", text)
            opts = self._video_opts(text)
            motion = self._strip_video_directives(self._clean_prompt(text))
            if not await self._confirm_render(confirm, "video",
                                              f"Animate the image: “{motion[:120]}”"):
                return self._declined("video")
            result, el = await self._tracked(emitter, "Animating image",
                                             self._gen_i2v_and_cache(cid, anim_img, motion, opts))
            return await self._finish(emitter, result, "Animated", el,
                                      f"Wan 2.2 I2V · {opts['w']}×{opts['h']}")
        # Fresh video: explicit ("create a video of …") or video-flavored wording with no video yet.
        if text and not ref and not style_edit and (self._wants_new_video(text)
                                 or (self._is_video_request(text) and kind != "video")):
            self._route_metric("media:video", 1, "video_request", text)
            opts = self._video_opts(text)
            cleaned = self._strip_video_directives(self._clean_prompt(text))
            n = self._wants_multishot(text)
            if not await self._confirm_render(confirm, "video", f"Generate a video: “{cleaned[:120]}”"):
                return self._declined("video")
            # A multi-shot sequence needs cross-shot consistency (best-quality A14B chain), so it
            # overrides 'fast' rather than being silently dropped to a single 5B clip.
            if n >= 2:
                result, el = await self._tracked(emitter, f"Generating {n}-shot video",
                                                 self._gen_multishot_and_cache(cid, cleaned, n, opts))
                return await self._finish(emitter, result, "Generated", el,
                                          f"{n}-shot · Wan 2.2 A14B · {opts['w']}×{opts['h']}")
            result, el = await self._tracked(emitter, "Generating video",
                                             self._gen_video_and_cache(cid, cleaned, enhance=VID_ENHANCE, opts=opts))
            model = "Wan 2.2 5B" if opts.get("fast") else "Wan 2.2 A14B"
            return await self._finish(emitter, result, "Generated", el,
                                      f"{model} · {opts['w']}×{opts['h']} · {self._clip_frames(opts)}f")
        # Fresh image generation ("create/draw a …") → a brand-new image, even mid-conversation.
        # (unless it's a restyle of the image on the table — "make this picture realistic" — which
        # must fall through to the EDIT path below, not t2i a mangled prompt from scratch)
        if text and not ref and not style_edit and self._is_image_request(text):
            self._route_metric("media:image", 1, "image_request", text)
            cleaned_img = self._clean_prompt(text)
            if not await self._confirm_render(confirm, "image",
                                              f"Generate an image: “{cleaned_img[:120]}”",
                                              expensive=False):   # ~16 s; only gated under "all"
                return self._declined("image")
            result, el = await self._tracked(emitter, "Generating image",
                                             self._gen_and_cache(cid, cleaned_img, None))
            return await self._finish(emitter, result, "Generated", el,
                                      f"RedCraft · {IMG_T2I_W}×{IMG_T2I_H} · 8 steps")
        # Follow-up about the most recent VIDEO → regenerate it with the change folded into the
        # original prompt, SAME seed (keeps the scene recognizably similar). Multi-shot histories
        # (shots joined with ' || ') are re-planned with the change applied.
        if text and not ref and kind == "video" and (self._wants_edit(text) or self._is_length_only(text)):
            self._route_metric("media:video_edit", 1, "video_followup", text)
            prev_prompt, prev_seed, prev_opts = media
            # Start from the original clip's opts; only override what the message explicitly names,
            # so a follow-up keeps the original 720p/length instead of silently resetting to defaults.
            opts = self._video_opts(text, base=prev_opts)
            is_multishot = " || " in (prev_prompt or "")
            # A pure duration change ('make it longer') → re-render the SAME prompt+seed at the new
            # length; merging would perturb the scene for no reason.
            vdetail = f"Wan 2.2 A14B · {opts['w']}×{opts['h']} · {opts['length']}f"
            # Gated once, above both paths: a re-render costs the same minutes as the first one, and
            # a misread follow-up ("that's great, thanks") is exactly the kind of turn that should
            # not silently re-enter the renderer. Asked before _merge_video_prompt so the question
            # names the user's own words rather than a prompt they have not seen yet.
            if not await self._confirm_render(confirm, "video", f"Re-render the video: “{text[:120]}”"):
                return self._declined("video")
            if self._is_length_only(text):
                if is_multishot:
                    n = min(V_SHOT_MAX, max(2, prev_prompt.count(" || ") + 1))
                    base = prev_prompt.replace(" || ", ", then ")
                    coro = self._gen_multishot_and_cache(cid, base, n, opts, seed=prev_seed)
                else:
                    coro = self._gen_video_and_cache(cid, prev_prompt or self._clean_prompt(text),
                                                     prev_seed, opts=opts)
                result, el = await self._tracked(emitter, "Re-rendering video", coro)
                return await self._finish(emitter, result, "Re-rendered", el, vdetail)
            if is_multishot:
                base = prev_prompt.replace(" || ", ", then ")
                merged = await asyncio.to_thread(self._merge_video_prompt, base, text)
                n = min(V_SHOT_MAX, max(2, prev_prompt.count(" || ") + 1))
                coro = self._gen_multishot_and_cache(cid, merged, n, opts, seed=prev_seed)
            else:
                merged = (await asyncio.to_thread(self._merge_video_prompt, prev_prompt, text)
                          if prev_prompt else self._clean_prompt(text))
                coro = self._gen_video_and_cache(cid, merged, prev_seed, opts=opts)
            result, el = await self._tracked(emitter, "Updating video", coro)
            return await self._finish(emitter, result, "Updated", el, vdetail)
        # Editing an image: an attached one, OR the most recent image in this chat — no re-upload
        # needed. Any non-question/non-smalltalk message here is treated as an edit instruction.
        img = ref or (media if kind == "image" else None)
        if text and img and self._wants_edit(text):
            self._route_metric("media:image_edit", 1, "edit_request", text)
            instruction = self._edit_instruction(text)
            # expensive=True: 162 s median, the slowest per-result operation on the box.
            if not await self._confirm_render(confirm, "image edit", f"Edit the image: “{instruction[:120]}”"):
                return self._declined("image edit")
            # `original=text`: QA must judge against the user's verbatim ask, not the
            # stripped/rewritten instruction chain.
            result, el = await self._tracked(emitter, "Editing image",
                                             self._gen_and_cache(cid, instruction, img, msgs,
                                                                 original=text))
            return await self._finish(emitter, result, "Edited", el, "Qwen-Image-Edit")
        # Chat. If the conversation revolves around an image the pipe GENERATED and this turn is a
        # question about it, attach the pixels to the last user message so the vision model (gemma4)
        # actually sees it — otherwise text-only dolphin answers blind (the image was scrubbed).
        omsgs = self._ollama_messages(msgs)
        attached_img = kind == "image" and isinstance(media, str) and not ref
        if attached_img:
            for m in reversed(omsgs):
                if m.get("role") == "user":
                    if media not in (m.get("images") or []):
                        m["images"] = (m.get("images") or []) + [media]
                    break
        # Automatic coder routing. Reached only when every media branch above declined, so a request
        # to *render* something never gets diverted into a text answer. Skipped when the turn carries
        # an image, because that has to go to the vision model — the coder is text-only.
        # `text` here is the clean routing prompt (metadata user_prompt, filter blocks stripped),
        # never the RAG blob, so a document about Python cannot pull the conversation to the coder.
        # Background tasks go to the local hermes-agent. Checked AFTER every media branch (a
        # request to render never becomes a job) and BEFORE coder routing ("track the price and
        # alert me" contains no code but 'script-like' phrasing must not reach the coder either).
        # `text` is the clean routing prompt, so RAG/search context cannot fabricate a job.
        # Deterministic job management, answered from hermes's REST API with no model in the path.
        # Hoisted ABOVE the attached_img gate below on purpose: a job table is text, and gating it
        # the same way as a render would make "list my tasks" unanswerable for the whole life of
        # any chat that once produced an image. Only `ref` (a freshly attached image) suppresses
        # it, because that turn is unambiguously about the picture.
        # A reply to the flight form is handled ABOVE the manage block, not inside the bg block, and
        # the ordering is load-bearing. _MANAGE_VERB anchors cancel|stop|pause|end|kill at position 0,
        # and the `referring` branch below fires whenever a job table was rendered in the last two
        # turns — so a bare "cancel" or "stop" meant to abandon this form would be read as a
        # scheduler reference and act on a real job. The form owns its own abandonment vocabulary,
        # which means it has to be asked first.
        if FLIGHT_ROUTE and not ref and cid in self._flight_draft:
            done = self._flight_turn(cid, text, omsgs, resume=True,
                                     handle=self._alert_username(__user__))
            if done is not None:
                self._mark_bg(cid)
                return done
        if BG_TASKS and MANAGE_DETERMINISTIC and not ref:
            mhandle = self._alert_username(__user__)
            # Non-admins only see state that was rendered FOR them; admins keep reading the legacy
            # in-message markers, which predate ownership and carry no handle.
            mscope = self._manage_scope(__user__, mhandle)
            pconf = self._pending_confirm(cid, omsgs, handle=mhandle if mscope else None)
            parked = self._parked_jobs(cid, omsgs, handle=mhandle if mscope else None)
            raw_l = (text or "").strip().lower()
            mg_manage = bool(self._BG_MANAGE.match(raw_l))
            mg_list = bool(self._BG_LIST.search(raw_l))
            # An armed confirmation only claims the turn when the reply is actually an answer to
            # it; anything else means the user moved on and must route normally.
            answering = bool(pconf and (pconf.get("stage") == "choose"
                                        or self._CONFIRM_YES.match(text or "")
                                        or self._CONFIRM_ALT.match(text or "")
                                        or self._CONFIRM_NO.match(text or "")))
            # A reference is only honoured when a table was rendered in the last two turns, which
            # is what keeps this vocabulary out of ordinary chat: "stop it" mid-conversation
            # cannot reach the scheduler unless the scheduler was just on screen.
            referring = bool(parked and self._manage_op(text))
            if answering or referring or mg_manage or mg_list:
                rule = ("bg_confirm" if answering else "bg_parked_ref" if referring
                        else "bg_manage_list" if mg_manage else "bg_list_vocab")
                done = await self._manage_turn(cid, text, parked, rule, pending=pconf,
                                               user=__user__, handle=mhandle)
                if done is not None:
                    self._mark_bg(cid)
                    return self._say(done)
        if BG_TASKS and not attached_img and not ref:
            handle = self._alert_username(__user__)
            # Non-admin turns tell hermes to scope its duplicate check, and make the pipe refuse to
            # narrate a job the user does not own.
            scoped = self._manage_scope(__user__, handle) is not None
            # One-shot delegation, EXPLICIT ONLY. hermes's chat surface already has web, file,
            # memory, session_search and todo (terminal and code execution are deliberately
            # excluded in config.yaml), so multi-step research is installed and safe — it was just
            # unreachable, because _is_bg_task_request requires RECURRENCE and everything one-shot
            # fell through to plain chat.
            #
            # A slash command rather than a heuristic, on purpose. Every routing tier in this file
            # that guessed has needed measuring and walking back, and the cost here is not a wrong
            # answer: delegating evicts the chat tenant and the user waits ~23 s for the reload.
            # /img and /vid set the precedent. Earn a heuristic with data first.
            # Word-bounded (_BG_ONESHOT), not startswith: "/agenda review monday" used to satisfy
            # startswith("/agent"), and the anchored strip then sent "a review monday" to hermes.
            oneshot = self._BG_ONESHOT.match(text or "")
            if oneshot:
                question = (text or "")[oneshot.end():].strip()
                if not question:
                    return self._say("Give me something to look into — e.g. "
                                     "`/research what changed in the Wan 2.2 release notes`.")
                self._route_metric("agent.oneshot", 0,
                                   f"slash_{oneshot.group(1).lower()}", text)
                self._mark_bg(cid)
                return self._hermes_stream(question, handle, verify_creation=False,
                                           brief=self._RESEARCH_BRIEF, scoped=scoped)
            # A turn that answers "what number should I text?" is handled before anything else —
            # a bare "514-555-0123" matches no task predicate and would otherwise reach the chat
            # model, which would cheerfully claim to have saved it.
            pending = self._pending_phone_request(omsgs, cid)
            if pending is not None:
                answered = self._phone_reply(text, handle, pending, cid, scoped=scoped)
                if answered is not None:
                    self._route_metric("task.create", 0, "phone_reply", text)
                    self._mark_bg(cid)
                    return answered
            # A flight ask is claimed BEFORE _is_bg_task_request, which is the whole point of this
            # path. Measured: 7 of 8 realistic phrasings match neither _BG_VERB+_BG_RECURRENCE nor
            # anything else, and reach the chat model, which invents a fare. The eighth DOES match
            # and builds a price_search --kind fare job that refuses itself. Both are wrong; this
            # takes them instead. Below _BG_ONESHOT so `/research cheapest flights` stays research,
            # and below the phone reply so the two contact flows cannot interleave.
            if FLIGHT_ROUTE:
                ftier, frule = self._is_flight_request(text)
                if ftier:
                    done = self._flight_turn(cid, text, omsgs, resume=False,
                                             handle=self._alert_username(__user__))
                    if done is not None:
                        self._mark_bg(cid)
                        return done
            # An edit to a job that ALREADY EXISTS ("change/adjust the alert to 15 mins"), not a
            # request for a new one. Checked BEFORE _is_bg_task_request below, for the same
            # reason _task_mode_turn's equivalent check runs before its own create test: a
            # duration phrase ("for 2 hours") is exactly the kind of thing _BG_RECURRENCE also
            # matches, so an edit request that spells its duration correctly could otherwise be
            # caught by the CREATION path instead, which has no idea an existing job is meant.
            #
            # Without ANY check here, "change it to checking every day for next 2 weeks" matched
            # no predicate in this whole cascade at all: _BG_VERB requires an opening verb like
            # track/watch/monitor, which this doesn't have, so _is_bg_task_request is unconditionally
            # false regardless of the recurrence wording, and the message fell straight through to
            # the plain CHAT MODEL at the very bottom of pipe() — no tool access, no view of the
            # real scheduler. It fabricated "✅ Switched to daily checks" from the job id and
            # schedule visible earlier in the conversation while the real job sat byte-for-byte
            # unchanged (confirmed live, 2026-08-10: `hermes cron list` still showed the original
            # "every 15m" schedule and "0/8" repeat count afterward). _task_mode_turn's own edit
            # step only fires when the Task control is explicitly on — this is its counterpart for
            # every OTHER turn, which is most of them.
            if self._EDIT_VERB.search((text or "").strip().lower()):
                if not await self._confirm_render(
                        confirm, "background task",
                        f"Change a background task: “{(text or '')[:120]}”"):
                    return self._say(
                        "Okay — nothing changed. If you just wanted an answer rather than a job "
                        "change, ask it directly and I'll answer here.")
                self._route_metric("task.edit", 0, "chip_edit", text, deterministic=False)
                self._mark_bg(cid)
                return self._hermes_stream(text, handle, verify_creation=True,
                                           brief=self._EDIT_BRIEF, scoped=scoped)
            followup = self._is_bg_followup(text, omsgs, cid)
            if followup or self._is_bg_task_request(text):
                raw_l = (text or "").strip().lower()
                is_manage = bool(self._BG_MANAGE.match(raw_l))
                # Reaching here with listing vocabulary means the deterministic path DECLINED
                # (scheduler unreachable, or the feature switched off). It is still a read-only
                # question, so it must be threaded exactly like is_manage below — otherwise a
                # failed list falls into the job-CREATION machinery and asks for a phone number.
                is_list = bool(self._BG_LIST.search(raw_l))
                read_only = is_manage or is_list
                # Rule attribution mirrors _is_bg_task_request's precedence exactly, so the row
                # names the rule that actually won — not merely one that also matches.
                if followup:
                    bg_rule, bg_tier = "bg_followup", 0
                elif self._BG_SLASH.match(raw_l):
                    bg_rule, bg_tier = "bg_slash", 0
                elif is_manage:
                    bg_rule, bg_tier = "bg_manage", 1
                elif is_list:
                    bg_rule, bg_tier = "bg_list", 1
                else:
                    bg_rule, bg_tier = "bg_verb+recurrence", 1
                # For an ordinary user there is NO agent fallback on a read-only turn. The agent's
                # job list is the whole host, so delegating "list my tasks" would show them
                # everyone's — the exact exposure the deterministic path exists to prevent. This
                # deliberately narrows the old "non-admins fall through to today's behaviour"
                # contract: that fallthrough WAS the leak. Admins still fall through.
                if read_only and scoped:
                    self._route_metric("task.manage", bg_tier, bg_rule, text,
                                       deterministic=False, reason="scoped_no_fallback")
                    self._mark_bg(cid)
                    return self._say(
                        "I can't look up your background tasks right now — task listing is "
                        "switched off on this assistant. An admin can list and change jobs for "
                        "you in the meantime.")
                self._route_metric(
                    "task.manage" if read_only else
                    ("task.followup" if followup else "task.create"),
                    bg_tier, bg_rule, text, deterministic=False)
                # Ask for a number BEFORE scheduling anything. Creating the job first would leave
                # a monitor that runs, fires, and texts nobody — the user believing they are
                # covered. Only for genuinely new alerting requests: managing or continuing an
                # existing task must never be interrupted by a form.
                if (not followup and not read_only
                        and self._WANTS_ALERT.search(text or "")
                        and not self._contact(handle).get("phone")):
                    self._mark_bg(cid)
                    return self._say(self._phone_prompt(handle, text, cid))
                # Confirm ONLY a genuinely new, heuristically-detected job. Delegating loads the
                # 65536-ctx agent runner, which cannot co-reside with the 32768-ctx chat tenant —
                # so a false positive costs the user an eviction plus a reload for a job they
                # never asked for. Same fail-open contract as the render gate.
                #
                # Deliberately NOT gated, each for its own reason:
                #   /research, /agent   already explicit user intent (returns above, :3076)
                #   phone-number reply  answered above (:3086) — a bare number matches nothing else
                #   followup            continuing an exchange the user already consented to;
                #                       re-asking on "yes reenable" would be absurd
                #   list/manage verbs   read-only or reversible, and normally answered
                #                       deterministically above — reaching the agent at all means
                #                       the REST path declined (scheduler down, non-admin), and
                #                       gating an explicit "list my tasks" behind a confirmation
                #                       would ask permission to answer a question
                if not followup and not read_only:
                    if not await self._confirm_render(
                            confirm, "background task",
                            f"Schedule a background task: “{(text or '')[:120]}”"):
                        return self._say(
                            "Okay — nothing scheduled. If you just wanted an answer rather than a "
                            "recurring job, ask it directly and I'll answer here.")
                sent = text
                if followup:
                    # "yes reenable" is meaningless alone — hand hermes what it just asked.
                    prev = next((m.get("content") or "" for m in reversed(omsgs)
                                 if m.get("role") == "assistant"), "")
                    sent = (f"Continuing our exchange. You previously said:\n"
                            f"{prev.replace(self._BG_MARK, '')[-1200:]}\n\n"
                            f"The user now replies: {text}\n"
                            f"Act on it against the REAL scheduler state — call "
                            f"cronjob(action='list') first and work from what is actually there.")
                self._mark_bg(cid)
                return self._hermes_stream(sent, handle, scoped=scoped,
                                           verify_creation=not (read_only or followup))
        if not attached_img and not ref and await asyncio.to_thread(self._is_code_request, text):
            code_rule = ("code_strong" if self._CODE_STRONG.search(text or "")
                         else "code_classifier")
            self._route_metric("coder", 1 if code_rule == "code_strong" else 3, code_rule, text)
            # emitter goes IN, so the wait ticks from inside _locked_stream's polling loop. Not
            # wrapped around it — wrapping one async generator in another breaks aclose()
            # propagation and would hold the GPU on a disconnect, which is the bug just fixed.
            return self._locked_stream(self._achat_stream(
                omsgs, guard_text=self._CODER_GUARD, keep_system=AUTO_KEEP_SYSTEM,
                force_model=self.coder_model), emitter=emitter)
        # Plain chat takes no lock and still will not — it contends and works, and blocking it
        # would trade a slow success for a guaranteed wait of up to a full render. It just stops
        # looking hung. One note, not a live strip: chat never calls _status again, so a done=False
        # strip would linger for the whole reply. Nothing at all when there is no client.
        if emitter and await asyncio.to_thread(self._gpu_contended):
            await self._status(emitter, "GPU is rendering — this reply may be slow to start.",
                               done=True)
        self._route_metric("chat:vision" if attached_img else "chat", 0, "fallthrough", text)
        return self._achat_stream(omsgs, keep_system=AUTO_KEEP_SYSTEM)

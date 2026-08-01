"""
title: Assistant (auto)
author: local
version: 0.5.0
required_open_webui_version: 0.5.0
description: One model that decides - chats (with vision), makes a Krea 2 image (text or gentle image-to-image edit), or a Wan video. Non-blocking (async). Never uses the uncensored model.
"""
import asyncio, aiohttp, requests, time, base64, hashlib, os, random, re, json, sqlite3, threading

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
# Alert wiring the pipe can see from inside the container. Both live in the OpenWebUI config
# directory because that is the only path shared with the host, where the transports run:
#   contacts — read AND written here, so a phone number the user types in chat is usable at once
#   profile  — non-secret display facts (sender address, channels), refreshed by the delivery
#              watcher every minute so this can never describe a setup that is no longer true
ALERT_CONTACTS_FILE = os.environ.get("ALERT_CONTACTS",
                                     "/app/backend/data/alerts/contacts.json")
ALERT_PROFILE_FILE = os.environ.get("ALERT_PROFILE", "/app/backend/data/alerts/profile.json")
# Read-only, for resolving a handle to its account email exactly as the delivery side does.
OWUI_DB = os.environ.get("OWUI_DB", "/app/backend/data/webui.db")
VID_ENHANCE = True  # expand terse video ideas ("guy shooting hoops") into detailed prompts — the
                    # single biggest quality lever for Wan; terse prompts produce broken scenes
VID_VERIFY = True   # vision-check a mid frame of the clip against the request; one corrected retry
VID_VERIFY_MODE = "anchors"  # multi-shot QA scope: "anchors" = shot 1 + last shot only | "all" = every shot

# --- automatic coder routing on the 'auto' entry -------------------------------------------------
# Lets 🪄 Assistant hand a coding question to the big coder tenant without the user switching entries.
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
    def __init__(self):
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
        # (OpenWebUI's own title/tag/query task model is gemma4:e2b, configured at the server level —
        # this pipe does not call it directly, so no task_model attribute is kept here.)
        self._recent = {}        # chat_id -> last produced image b64 (for follow-up edits without re-upload)
        self._recent_video = {}  # chat_id -> (prompt, seed) of the last produced video (for follow-up changes)

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
        return [{"id": "auto", "name": "🪄 Assistant"}]

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
        """
        try:
            r = requests.post(
                f"{self.ollama}/api/chat",
                json={"model": ROUTE_CLASSIFIER_MODEL, "stream": False, "think": False,
                      "options": {"temperature": 0, "num_predict": 4},
                      "messages": [{"role": "user",
                                    "content": self._CLASSIFY_PROMPT.format(msg=text[:600])}]},
                timeout=ROUTE_CLASSIFIER_TIMEOUT)
            if r.status_code != 200:
                return False
            verdict = ((r.json().get("message") or {}).get("content") or "").strip().upper()
            return verdict.startswith("CODE")
        except Exception:
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
    _BG_SLASH = re.compile(r"^\s*/task\b", re.I)
    _BG_MANAGE = re.compile(
        r"^\s*(?:please\s+)?(?:(?:list|show|what are)\b.{0,20}\b(?:background|scheduled|monitoring)?"
        r"\s*(?:tasks|monitors|jobs|watches)\b"
        r"|(?:cancel|stop|pause|resume|remove|delete)\b.{0,40}\b(?:task|monitor|monitoring|job|watch|tracking)\b)", re.I)
    _BG_VERB = re.compile(
        r"^\s*(?:please\s+|can you\s+|could you\s+)?"
        r"(?:monitor|track|watch|keep an eye on|keep track of|alert me|notify me|remind me|ping me)\b", re.I)
    _BG_RECURRENCE = re.compile(
        r"\b(?:every\s+(?:\d+\s+)?(?:minute|hour|day|week|morning|evening|night)s?"
        r"|hourly|daily|weekly|nightly"
        r"|for\s+(?:the\s+next\s+)?\d+\s+(?:hour|day|week|month)s?"
        r"|for\s+(?:a|two|three|the next few)\s+(?:hour|day|week|month)s?"
        r"|until\s+(?:it|the|price)"
        r"|(?:when|if|once)\s+(?:it|the price|the value|it's|stock)\b.{0,30}\b(?:drops?|falls?|changes?|"
        r"rises?|goes\s+(?:below|above|down|up)|hits|reaches|back in stock|available)"
        r"|in\s+\d+\s+(?:minute|hour|day|week)s?\b)", re.I)
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
        ("back_in_stock", r"\bback in stock\b|\bin stock\b|\brestock|\bavailable again\b"),
        ("out_of_stock",  r"\bout of stock\b|\bsold out\b|\bruns out\b"),
        ("fare",          r"\bfare\b|\bflight\b|\bairfare\b|\bticket price\b|\bround.?trip\b"),
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
        rows.append("| 📋 Every run | one line in **background-tasks** |")
        if not rows:
            return ""
        notes = [
            # The single most common first-week misdiagnosis: a channel post arrives, no text does,
            # and the user concludes the alerting is broken. It is not — the condition simply was
            # not met. Say so before it happens.
            "Every check posts a line to the channel; only a check that **meets your condition** "
            "texts you.",
        ]
        if phone and prof.get("sms_strips_links", True):
            notes.append("Texts arrive without links — carriers drop any message containing one — "
                         "so the link is in the email.")
        return ("\n\n**How you'll be alerted**\n\n"
                "| | |\n|---|---|\n" + "\n".join(rows) + "\n\n" + " ".join(notes))

    def _phone_prompt(self, handle, pending_request):
        """Ask for a number BEFORE scheduling, and carry the request across the turn."""
        blob = base64.b64encode((pending_request or "").encode()).decode()
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
                f"{alt}{heads_up}\n"
                f"<!--bg-need-phone:{blob}-->")

    @staticmethod
    async def _say(text):
        yield text

    async def _phone_then_task(self, e164, handle, pending):
        """Save the number, then run the request the user made a turn ago."""
        if not self._save_phone(handle, e164):
            yield (f"⚠️ Couldn't save `{self._pretty_phone(e164)}` — `{ALERT_CONTACTS_FILE}` is "
                   f"not writable. The task was NOT scheduled; alerts would have gone nowhere.")
            return
        yield f"✅ Saved `{self._pretty_phone(e164)}` for texts.\n\n"
        if not pending:
            yield ("Now tell me what to watch and I'll set it up." + self._BG_MARK)
            return
        async for chunk in self._hermes_stream(pending, handle, verify_creation=True):
            yield chunk

    def _phone_reply(self, text, handle, pending):
        """Handle the turn AFTER a phone prompt. None => not a phone answer, route normally."""
        t = (text or "").strip()
        if self._PHONE_DECLINE.match(t):
            if not pending:
                return self._say("No problem — no number saved." + self._BG_MARK)
            return self._hermes_stream(pending, handle, verify_creation=True)
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
            # Reject at the point they typed it, not silently at send time three days later.
            return self._say(
                f"`{attempt.strip()}` doesn't look like a mobile number I can text — I need "
                f"10 digits (or +country code). Try again, or reply **email only**.\n"
                f"<!--bg-need-phone:{base64.b64encode((pending or '').encode()).decode()}-->")
        return self._phone_then_task(e164, handle, pending)

    def _pending_phone_request(self, messages):
        """The request parked by a previous _phone_prompt, or None."""
        prev = next((m.get("content") or "" for m in reversed(messages or [])
                     if m.get("role") == "assistant"), "")
        m = self._PHONE_MARK_RE.search(prev)
        if not m:
            return None
        try:
            return base64.b64decode(m.group(1)).decode() or ""
        except Exception:
            return ""

    def _is_bg_followup(self, text, messages):
        """True when this short message continues the previous hermes exchange in THIS chat."""
        if not text or len(text) > 120:
            return False
        prev = next((m.get("content") or "" for m in reversed(messages or [])
                     if m.get("role") == "assistant"), "")
        return self._BG_MARK in prev and bool(self._BG_FOLLOWUP.match(text.strip()))

    def _is_bg_task_request(self, t):
        raw = (t or "").strip().lower()
        if self._BG_SLASH.match(raw):
            return True
        if self._BG_QUESTION.match(raw):
            return False  # asking ABOUT monitoring is chat, whatever else matches
        if self._BG_MANAGE.match(raw):
            return True
        return bool(self._BG_VERB.match(raw) and self._BG_RECURRENCE.search(raw))

    def _is_image_request(self, t):
        raw = t.lower()
        if self._FIGURATIVE.search(raw):
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
            elif role == "assistant" and isinstance(c, str):
                if "data:video/" in c:
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
                b = self._extract_b64(c)
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
        # question / info request / write-verbs → chat  (translate/write/compose/summarize/draft)
        if self._is_question(low):
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
        return (f"Convert this image into {target}. Keep the exact same scene: the same people — same "
                f"count, ages, genders, ethnicity and skin tone — the same poses, expressions, clothing "
                f"and colors, and the same composition, background and framing. Change ONLY the rendering "
                f"style, and apply the new style emphatically to the ENTIRE image.", negative)

    def _style_enrich(self, instruction, msgs):
        """Restate the concrete subjects (from chat context) inside a style-conversion
        instruction — on big style jumps the editor drifts ethnicity/identity when the
        instruction only says 'same ethnicity' generically (observed: Pakistani father came
        out a different ethnicity on cartoon→photo). Falls back to the generic instruction."""
        ctx = self._edit_context(msgs)
        if not ctx:
            return instruction
        try:
            r = requests.post(f"{self.ollama}/api/generate", json={
                "model": self.chat_model,
                "system": ("You tighten image style-conversion instructions. Using the conversation, "
                           "rewrite the instruction so every person is named concretely — age, gender, "
                           "ethnicity and skin tone (e.g. 'the Pakistani father, a South Asian man in "
                           "his 30s with brown skin, and his 10-year-old South Asian son') — instead of "
                           "generic wording like 'the same people'. Keep the conversion command, the "
                           "style description, and the 'Change ONLY the rendering style' ending intact. "
                           "ONE instruction under 110 words. Output ONLY the instruction text."),
                "prompt": f"Conversation:\n{ctx}\n\nInstruction:\n{instruction}\n\nRewritten instruction:",
                "stream": False, "think": False, "keep_alive": 0,
                "options": {"temperature": 0.3, "num_predict": 260}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"')
            if out and "convert" in out.lower() and len(out) < 1200:
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

    _EDIT_REWRITE_SYS = (
        "You rewrite photo-editing requests into instructions for the Qwen-Image-Edit model, which sees "
        "the photo alongside your instruction. You are given the conversation so far; rewrite ONLY the "
        "last user request. Rules: ONE imperative instruction under 80 words. Use absolute, concrete "
        "visual terms for the target state, never relative wording — e.g. \"make the son a bit older, "
        "like 18\" becomes \"Change the boy into an 18-year-old young man: adult height and build, mature "
        "facial features, light stubble, defined jawline.\" Name the subject as it appears in the photo "
        "(\"the boy\", \"the young woman on the right\"), never \"it\" or \"him\". The editor is "
        "conservative and under-applies changes, so state the change emphatically. When transforming a "
        "person, RESTATE the traits that must survive the change — ethnicity and skin tone (take them "
        "from the conversation, e.g. Pakistani/South Asian), hair colour, family resemblance, clothing — "
        "the editor drifts to a generic different-looking person if you don't. For age changes name the "
        "life stage and bracket it: \"a 10-year-old school-age girl — clearly older than a toddler, "
        "clearly younger than a teenager\". End the instruction with what must stay unchanged (identity, "
        "clothing, background, lighting) unless the user asked to change those too. Then output a second "
        "line: \"AVOID: \" plus 3-8 comma-separated visual traits the RESULT must not contain — for age "
        "changes bracket BOTH sides (e.g. for 10 years old: toddler, preschooler, teenager, adult woman) "
        "and add ethnicity-drift terms when ethnicity must be kept (e.g. East Asian features). Output "
        "EXACTLY two lines:\nEDIT: <instruction>\nAVOID: <traits>"
    )

    def _edit_context(self, msgs, limit=8):
        """Compact 'user:/assistant:' transcript (media scrubbed) so the rewriter can resolve
        references like 'the son' from earlier turns."""
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

    def _enhance_edit(self, instruction, msgs):
        """(explicit_instruction, negative) via the local LLM. Vague relative asks ('a bit older')
        under-move the identity-preserving editor; explicit absolute target states move it properly.
        Falls back to the original instruction and no negative."""
        ctx = self._edit_context(msgs)
        prompt = (f"Conversation:\n{ctx}\n\nRewrite the last user request."
                  if ctx else f"Request: {instruction}\n\nRewrite this request.")
        try:
            out = self._generate(
                {"model": self.chat_model, "system": self._EDIT_REWRITE_SYS, "prompt": prompt,
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

    def _enhance(self, prompt):
        """Expand a short idea into a vivid image prompt via the local LLM. Falls back to the original."""
        sys = (
            "You are a prompt engineer for the Krea 2 image model. Rewrite the user's idea "
            "as ONE vivid, richly detailed image prompt: subject, setting, lighting, mood, composition, "
            "style, and camera/lens where useful. The user's explicit specifications are HARD requirements: "
            "every person, count, age, gender, ethnicity, object, relationship AND art style they name "
            "MUST be kept exactly — none added, dropped, or aged up or down. If the user names an art "
            "style or medium ('animated picture', 'cartoon', 'anime', 'watercolor', 'oil painting', "
            "'pixel art' …), OPEN the prompt by stating that style emphatically (e.g. 'A vibrant 3D "
            "animated cartoon-style illustration, stylized characters with expressive faces, NOT "
            "photorealistic') and use that style's vocabulary throughout — no camera or lens language. "
            "Only when no style is named, write it photorealistic with camera/lens detail. Describe EACH "
            "named person as their own clause with concrete age cues (e.g. '10 year old son' becomes "
            "'their 10-year-old son, a school-age boy a head shorter than the adults'; '20 year old "
            "daughter' becomes 'their 20-year-old daughter, a young adult woman'), repeating the "
            "ethnicity for each person, and state the total number of people ('exactly four people'). "
            "When several people are specified keep every face in sharp focus — no shallow depth of "
            "field. Keep it under 100 words. Output ONLY the prompt text — no preamble, no quotes, no lists."
        )
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

    def _build_edit_wf(self, instruction, name, seed, cfg, steps, negative):
        # Full-quality edit (no 4-step speed LoRA): 20 steps, cfg 4 → new elements blend into the
        # scene's lighting/grain instead of looking pasted-on. ~2 min. (Fast path lives in the
        # dedicated "Image" model's EDIT_QUALITY valve.)
        return {
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

    def _build_t2i_wf(self, prompt, seed):
        # Krea 2 Turbo text-to-image (8-step; negative is zeroed conditioning, cfg 1).
        # LoRA + size mirror the "Image" pipe's valves so both produce the same subject (F14).
        model_ref = ["u", 0]
        wf = {
          "u":   {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2/krea2_turbo_fp8_scaled.safetensors", "weight_dtype": "default"}},
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

    def _gen_image(self, prompt, ref_b64, msgs=None):
        # Free ComfyUI's VRAM up front so the dolphin/gemma prompt-rewrite helpers below don't load
        # into a card ComfyUI still occupies (~13.7 GB) and run partly on CPU.
        self._comfy_free()
        # Attached image → instruction edit with Qwen-Image-Edit 2509.
        if ref_b64:
            instruction = prompt or "improve the overall quality, keep everything else the same"
            negative = ""
            style = self._style_conversion(prompt) if prompt else None
            if style:  # whole-image restyle: purpose-built instruction, hard sampler push
                instruction, negative = style
                instruction = self._style_enrich(instruction, msgs)  # before _free_vram (gemma)
                cfg, steps = 6.0, 24
            else:
                if prompt:  # rewrite BEFORE _free_vram so Gemma isn't unloaded and reloaded
                    instruction, negative = self._enhance_edit(instruction, msgs)
                cfg, steps = (6.0, 24) if self._edit_boost(prompt) else (4.0, 20)
            self._free_vram()
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            wf = self._build_edit_wf(instruction, name, random.randint(0, 2**31), cfg, steps, negative)
            t_render = time.time()
            data, err, _ = self._submit_poll(wf, "s", "Edit", 1200)
            if err:
                self._metric(job="edit", ok=False, render_s=round(time.time() - t_render, 1),
                             err=err[:160])
                return err
            render_s = round(time.time() - t_render, 1)
            # Vision QA: did the edit deliver what was asked? Up to two harder retries if not.
            qa_note = ""
            qa_rounds, first_fix = 0, None
            t_qa = time.time()
            if IMG_VERIFY and prompt:
                cur = instruction
                for _round in (1, 2):
                    self._comfy_free()
                    ok, fix = self._verify_image(instruction, base64.b64encode(data).decode())
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
    _HERMES_BRIEF = (
        "You are the background-task manager for a local OpenWebUI assistant. The user's request "
        "was routed to you because it asks for a standing job (monitoring, scheduled checks, "
        "reminders) or to manage existing ones. Use your cronjob tool.\n"
        "Rules for every job you create:\n"
        "1. The job must be BOUNDED: honour the user's duration (e.g. 'for 2 weeks' => an end "
        "condition or repeat count). If no duration was given, default to 7 days and say so.\n"
        "2. Pick a sensible interval if the user gave none (price checks: every 6 hours).\n"
        "3. The job's prompt must be self-contained: exact URLs or curl commands to fetch (the "
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
        "5d. WATCHING A PAGE (price, stock, fare, availability) — do NOT write your own scraper. "
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
        "5e. The extractor also reports its OWN failures: a dead URL, a site blocking automated "
        "checks, and a page that still loads but no longer shows a value. Never add your own "
        "error handling or retry logic around it — it already confirms a failure across runs "
        "before telling the user, and alerts once per outage rather than every run.\n"
        "6. Before creating, call cronjob(action='list') and look at STATE, not just names. Only a job that is ACTIVE and still has runs left counts as a duplicate — say so and stop. A job that is completed, exhausted, disabled or has no next run is FINISHED: it will never run again, so create a NEW one instead of pointing at it. Never describe a finished job as 'already running'.\n"
        "6b. NEVER write your own script into a job and run it with --no-agent. A script YOU "
       "generate has no reasoning to recover when markup shifts, and its bugs fail silently — one "
       "such job computed its ALERT text into a variable it never printed, so the alert could "
       "never fire. Use normal agent mode. (The one exception is the pre-existing, tested "
       "extractor named in rule 5d, which the user's operator maintains — never a script you "
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
    def _changed_jobs(before, after):
        """[(job_id, what changed)] for jobs the scheduler already had and has since altered.

        Only fields a user would recognise as "my task changed": how often it runs, how many runs
        are left, and whether it is on. Anything else (next_run_at ticking forward, last_status)
        moves on its own every minute and would report a change on every single turn.
        """
        out = []
        for jid, a in (after or {}).items():
            b = (before or {}).get(jid)
            if not b:
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
            if diffs:
                out.append((jid, ", ".join(diffs)))
        return out

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
        "5. Answer in prose for the user, not as a report to a machine. Be concise."
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

    def _hermes_jobs(self):
        """Job ids currently scheduled, straight from hermes's /api/jobs — deterministic ground
        truth. None on any error (verification then reports 'could not verify', never a false
        positive)."""
        key = self._hermes_key()
        if not key:
            return None
        try:
            import urllib.request
            # include_disabled=true is REQUIRED: the plain endpoint omits completed/exhausted
            # jobs, so a cited id could not be resolved and a finished job looked like a
            # fabrication. Verification needs the full picture to tell those apart.
            req = urllib.request.Request(
                f"{HERMES_URL.rsplit('/v1', 1)[0]}/api/jobs?include_disabled=true",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return {j.get("id"): j for j in json.load(r).get("jobs", [])}
        except Exception:
            return None

    @staticmethod
    def _alert_username(user):
        """OpenWebUI identity -> a short stable handle for addressing alerts. Transport-neutral:
        whatever carries personal alerts (SMS, email, push) is keyed on this, not on the email."""
        u = user or {}
        local = (u.get("email") or "").split("@")[0] or (u.get("name") or "")
        handle = re.sub(r"[^a-z0-9_-]", "", local.lower())
        return handle or "user"

    async def _hermes_stream(self, text, uname="user", verify_creation=False, brief=None):
        """Delegate a background-task request to the local hermes-agent API server.

        A plain HTTP client, deliberately: hermes's API server is an agent runtime that streams
        SSE chunks including inline tool-progress markers, so the user watches the agent work and
        then gets its confirmation — inside the same single chat entry. No second model row, no
        bypass of this pipe.

        `brief` selects the contract: the default cron brief for scheduling, or _RESEARCH_BRIEF for
        one-shot work. They are not interchangeable — handing a research question the cron brief
        would tell the agent to create a job nobody asked for."""
        key = self._hermes_key()
        if not key:
            yield ("⚠️ Background tasks are configured but the hermes-agent key is missing "
                   f"({HERMES_KEY_FILE}). Is the hermes gateway set up on this host?")
            return
        if brief is None:
            brief = self._HERMES_BRIEF
            ctx = (f"Request context: the requesting user is '{uname}'. If this job needs to "
                   f"alert them, the ALERT line's recipient is '{uname}'.")
            kind = self._guess_kind(text)
            ctx += (f" The request is a '{kind}' watch — pass --kind {kind}."
                    if kind else
                    " Choose --kind yourself from: price_drop, price_rise, back_in_stock, "
                    "out_of_stock, fare, inventory, availability, threshold, change — whichever "
                    "best describes what the user is watching for.")
        else:
            ctx = f"Request context: the requesting user is '{uname}'."
        # hermes runs hermes-genesis:agent at num_ctx 65536 while chat holds apex-compact at 32768.
        # Ollama keys runners by model+options, so those are two distinct ~17 GB allocations and
        # only one fits. Releasing the chat tenant first makes the handoff deterministic instead of
        # leaving Ollama to evict under memory pressure mid-load.
        #
        # to_thread because _release_chat_tenant is a blocking requests call: running it inline
        # would stall the event loop, and this pipe serves every other conversation on the box.
        await asyncio.to_thread(self._release_chat_tenant)
        # Snapshot the scheduler BEFORE delegating, so creation can be verified against ground
        # truth after the stream rather than trusting the agent's narration (it has claimed jobs
        # it never created). None when verification is off — e.g. list/cancel requests.
        before = self._hermes_jobs() if verify_creation else None
        payload = {"model": "hermes-agent", "stream": True,
                   "messages": [{"role": "system", "content": brief + "\n" + ctx},
                                {"role": "user", "content": text}]}
        reply = ""          # accumulated so verification can check ids the agent cites
        after = None
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=HERMES_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{HERMES_URL}/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {key}"}) as r:
                    if r.status != 200:
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
                                def _runnable(j):
                                    return (j.get("enabled", True)
                                            and (j.get("state") or "").lower() != "completed")

                                new_jobs = None
                                for _ in range(6):
                                    after = self._hermes_jobs()
                                    if after is not None and before is not None:
                                        new_jobs = [j for i, j in after.items()
                                                    if i not in before and _runnable(j)]
                                        if new_jobs:
                                            break
                                    await asyncio.sleep(1)
                                if new_jobs:
                                    j = new_jobs[0]
                                    sched = j.get("schedule_display") or str(j.get("schedule", "?"))
                                    yield (f"\n\n✅ **Verified scheduled**: job `{j.get('id')}` "
                                           f"({sched}) — confirmed against the scheduler, not the "
                                           f"agent's word.")
                                    yield self._alert_setup_block(uname)
                                elif new_jobs is not None and self._changed_jobs(before, after):
                                    # An UPDATE is not a creation. "change my alert to every 5
                                    # minutes" produced a correctly rescheduled job AND a message
                                    # saying nothing had been created — the loudest possible way to
                                    # report success. Ground truth, not keywords: if a job the
                                    # scheduler already had now has a different schedule, state or
                                    # run budget, something real happened.
                                    jid, what = self._changed_jobs(before, after)[0]
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
                                    live = [i for i in cited if _runnable(after[i])]
                                    if live:
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
                                        j = after[cited[0]]
                                        yield (f"\n\n⚠️ **Nothing is scheduled**: the agent pointed "
                                               f"at job `{cited[0]}`, which has already FINISHED "
                                               f"({j.get('state', 'completed')}, next run: none). "
                                               f"No new job was created. Reply "
                                               f"**\"create a new one\"** to schedule it properly.")
                                    else:
                                        yield ("\n\n⚠️ **Verification failed**: the agent described "
                                               "a job but the scheduler has NO matching entry — "
                                               "nothing was actually created. Please resend.")
                                else:
                                    yield "\n\n(could not verify job creation — /api/jobs unreachable)"
                            yield self._BG_MARK
                            return
                        try:
                            d = json.loads(data)
                        except Exception:
                            continue
                        tok = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
                        if tok:
                            reply += tok
                            yield tok
        except asyncio.TimeoutError:
            yield ("\n\n⏳ hermes-agent did not finish within the window — the job may still have "
                   "been created. Check the background-tasks channel, or ask me to list tasks.")
        except aiohttp.ClientConnectorError:
            yield ("⚠️ hermes-agent is not reachable on 127.0.0.1:8642. "
                   "Start it with: `systemctl --user start hermes-gateway`")

    def _sampling(self, guard_text):
        """Sampling options for a chat turn, chosen by route.

        Keyed on the guard rather than the model tag because chat, code and vision all resolve to
        the same tag now — the guard is the only thing that still records which branch was taken,
        which is exactly how tests/eval/run_eval.py identifies the coder route on the wire."""
        opts = dict(CODER_OPTIONS if guard_text is self._CODER_GUARD else CHAT_OPTIONS)
        if EVAL_DETERMINISTIC:
            opts.update(temperature=0, seed=EVAL_SEED)
        return opts

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
                                        "options": self._sampling(guard_text)}) as r:
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

    async def _gen_and_cache(self, cid, prompt, ref, msgs=None):
        def run():
            with _gpu_lock():  # only one VRAM-manipulating pipeline at a time
                return self._gen_image(prompt, ref, msgs)
        result = await asyncio.to_thread(run)
        b64 = self._extract_b64(result)
        if b64:  # remember the produced image so a later "make it bigger" can edit it (true LRU)
            self._recent.pop(cid, None)  # move-to-end so an active chat isn't evicted first
            self._recent[cid] = b64
            while len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
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
        if want == "never" or event_call is None:
            return True
        # Keyed on measured COST, not on the noun. A Qwen-Image-Edit round is 162 s median (n=26)
        # against 16.3 s for a fresh Krea image, so an edit belongs with video under "video" even
        # though the user would call both "an image".
        if want != "all" and not expensive:
            return True
        try:
            answer = await event_call({
                "type": "confirmation",
                "data": {"title": f"Generate this {kind}?",
                         "message": f"{detail}\n\nThis holds the GPU and pauses chat until it "
                                    f"finishes."},
            })
        except Exception:
            return True
        # sio.call returns {'error': ...} on a dead session; anything non-boolean is "not a no".
        if answer is False:
            return False
        if isinstance(answer, dict) and answer.get("confirmed") is False:
            return False
        return True

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
                   __user__=None):
        emitter = __event_emitter__
        # None whenever nothing can be asked (direct API, eval harness); _confirm_render fails open.
        confirm = __event_call__
        msgs = body.get("messages", [])
        text, ref = self._last_user(msgs)
        # OpenWebUI PREPENDS retrieved file/knowledge context to the LAST USER message (RAG_SYSTEM_CONTEXT
        # defaults false), so `text` can be a multi-kB document blob. Every routing predicate below reads
        # `text`, and _is_image_request/_is_video_request fire on a bare "picture of"/"draw"/"video"
        # anywhere in it — while the _QUESTION/_SMALLTALK guards use anchored .match() and can never fire
        # because the blob starts with "### Task:". Net effect: attaching a PDF that merely mentions those
        # words launches a multi-minute Krea/Wan render built from the document text. Middleware stashes the
        # user's verbatim words BEFORE that injection (middleware.py:2803), so route on those instead.
        # Chat is unaffected: it uses `msgs`/`omsgs` below, which still carry the full RAG context.
        routed = (__metadata__ or {}).get("user_prompt")
        if isinstance(routed, str) and routed.strip():
            # ...but user_prompt is captured AFTER inlet filters run, so strip their blocks too.
            # The emptiness check is on the RAW value: an absent user_prompt must fall back to
            # _last_user (direct-API calls), whereas a prompt that was ENTIRELY injected context
            # must stay empty and route to chat — falling back there would hand the router the very
            # block we just removed.
            text = self._strip_injected_context(routed).strip()
        # Manifold dispatch. knowledge/coder are chat-only: returning here means NOT ONE media regex
        # runs, so a document that merely mentions "video" cannot start a render on those entries
        # regardless of what the router would have decided. Belt and braces on top of the
        # user_prompt fix above, which protects the 'auto' entry.
        entry = self._entry(body)
        if entry != "auto":
            return self._entry_chat_stream(entry, self._ollama_messages(msgs))
        cid = self._chat_id(body, __metadata__)
        # What media does this conversation currently revolve around?
        kind, media = self._recent_media(msgs)
        if kind is None:  # history scan found nothing → in-memory caches (this conversation only)
            if cid in self._recent_video:
                kind, media = "video", self._recent_video[cid]
            elif cid in self._recent:
                kind, media = "image", self._recent[cid]
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
            cleaned_img = self._clean_prompt(text)
            if not await self._confirm_render(confirm, "image",
                                              f"Generate an image: “{cleaned_img[:120]}”",
                                              expensive=False):   # ~16 s; only gated under "all"
                return self._declined("image")
            result, el = await self._tracked(emitter, "Generating image",
                                             self._gen_and_cache(cid, cleaned_img, None))
            return await self._finish(emitter, result, "Generated", el,
                                      f"Krea 2 · {IMG_T2I_W}×{IMG_T2I_H} · 8 steps")
        # Follow-up about the most recent VIDEO → regenerate it with the change folded into the
        # original prompt, SAME seed (keeps the scene recognizably similar). Multi-shot histories
        # (shots joined with ' || ') are re-planned with the change applied.
        if text and not ref and kind == "video" and (self._wants_edit(text) or self._is_length_only(text)):
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
            instruction = self._edit_instruction(text)
            # expensive=True: 162 s median, the slowest per-result operation on the box.
            if not await self._confirm_render(confirm, "image edit", f"Edit the image: “{instruction[:120]}”"):
                return self._declined("image edit")
            result, el = await self._tracked(emitter, "Editing image",
                                             self._gen_and_cache(cid, instruction, img, msgs))
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
        if BG_TASKS and not attached_img and not ref:
            handle = self._alert_username(__user__)
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
            if (text or "").strip().lower().startswith(("/research", "/agent")):
                question = re.sub(r"^/(research|agent)\s*", "", text.strip(), flags=re.I)
                if not question:
                    return self._say("Give me something to look into — e.g. "
                                     "`/research what changed in the Wan 2.2 release notes`.")
                return self._hermes_stream(question, handle, verify_creation=False,
                                           brief=self._RESEARCH_BRIEF)
            # A turn that answers "what number should I text?" is handled before anything else —
            # a bare "514-555-0123" matches no task predicate and would otherwise reach the chat
            # model, which would cheerfully claim to have saved it.
            pending = self._pending_phone_request(omsgs)
            if pending is not None:
                answered = self._phone_reply(text, handle, pending)
                if answered is not None:
                    return answered
            followup = self._is_bg_followup(text, omsgs)
            if followup or self._is_bg_task_request(text):
                is_manage = bool(self._BG_MANAGE.match((text or "").strip().lower()))
                # Ask for a number BEFORE scheduling anything. Creating the job first would leave
                # a monitor that runs, fires, and texts nobody — the user believing they are
                # covered. Only for genuinely new alerting requests: managing or continuing an
                # existing task must never be interrupted by a form.
                if (not followup and not is_manage
                        and self._WANTS_ALERT.search(text or "")
                        and not self._contact(handle).get("phone")):
                    return self._say(self._phone_prompt(handle, text))
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
                return self._hermes_stream(sent, handle,
                                           verify_creation=not (is_manage or followup))
        if not attached_img and not ref and await asyncio.to_thread(self._is_code_request, text):
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
        return self._achat_stream(omsgs, keep_system=AUTO_KEEP_SYSTEM)

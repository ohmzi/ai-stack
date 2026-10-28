"""
title: Uncensored
author: local
version: 0.7.0
required_open_webui_version: 0.5.0
description: Photorealistic uncensored images (Lustify V10, Krea 2) via local ComfyUI. Edits of an uploaded photo run through FireRed-Image-Edit 1.1 so the subject stays the same person, with the request rewritten from the picture, the result checked against your words, and one corrected retry. A picture Photoreal made itself is re-rendered from its own prompt and seed instead. Follow-ups ("make this picture animated", "remove the hat") keep editing the previous picture, and background-task calls never render. Non-blocking (async); auto-frees GPU VRAM.
"""
import asyncio, base64, hashlib, json, os, random, re, sys, time
import requests
from pydantic import BaseModel, Field

# Shared identity-edit helpers. Source of truth is pipes/shared/identity_edit.py in the repo;
# a copy lives in OpenWebUI's data mount because OWUI execs each Function standalone and a
# pipe cannot import a repo-relative module. If that copy is missing the pipe MUST still
# work — it falls back to the old SDXL img2img path and says so in the status line, rather
# than failing to load and taking the model out of the dropdown entirely.
_SHARED_ERR = ""
try:
    if "/app/backend/data" not in sys.path:
        sys.path.insert(0, "/app/backend/data")
    from identity_edit import (SDXL_EDIT_REWRITE_SYS, build_qwen_edit_wf, metric,
                               parse_rewrite, parse_seed, resolve_tier)
except Exception as _e:                                       # noqa: BLE001 - must never raise
    _SHARED_ERR = str(_e) or "identity_edit import failed"
    build_qwen_edit_wf = None
    SDXL_EDIT_REWRITE_SYS = ""

    def parse_seed(text):
        return (text or ""), None

    def parse_rewrite(out, fallback):
        return fallback, ""

    def resolve_tier(name, tiers=None):
        return {"lightning": False, "steps": 20, "cfg": 4.0}

    def metric(path, **fields):
        pass

# Conversation continuity (task guard, persistent last-image store, reference recovery,
# style-conversion instructions) — same deployment story as identity_edit above, same
# degrade-gracefully contract: without it the pipe behaves like the pre-continuity build.
try:
    import media_session as ms
except Exception:  # noqa: BLE001 - must never raise
    ms = None

NEG = ("cartoon, anime, drawing, painting, illustration, 3d, render, cgi, sketch, "
       "deformed, disfigured, bad anatomy, bad hands, extra fingers, mutated hands, "
       "watermark, signature, text, blurry, lowres, low quality, worst quality")
DENOISE = 0.65  # SDXL img2img strength. Only used on the fallback path — see the note below.
ENHANCE = True  # expand a terse idea into a full Lustify prompt via the local uncensored LLM (txt2img)
METRICS_PATH = os.environ.get("MEDIA_METRICS", "/app/backend/data/media_metrics.jsonl")

# Where a picture came from. Every picture this pipe renders from a prompt (text-to-image or a
# re-render) is recorded per chat as {digest, prompt, seed}. Two things read it:
#   * a follow-up edit of one of those pictures re-renders it from its own prompt and seed with
#     the change folded in, because Lustify follows its own prompt and FireRed (censored) often
#     ignores exactly the edits people ask of this pipe;
#   * the uncensored SDXL img2img engine is allowed ONLY for a recorded picture. An uploaded photo
#     can be a real person who never agreed to it, so uploads always go through FireRed.
# The digest is of the decoded image bytes, so an upload is never mistaken for one of ours unless
# it is byte-for-byte a picture this pipe produced in this chat.
ORIGIN_DIR = os.environ.get("PHOTOREAL_ORIGIN_DIR", "/app/backend/data/photoreal_origin")
_ORIGIN_KEEP = 40      # newest chats kept
_ORIGIN_PER_CHAT = 12  # newest pictures kept per chat

# One corrected retry after a failed check. The Assistant allows two, but a FireRed retry runs the
# 20-step tier (~150 s), and a second one rarely lands what the first missed.
QA_RETRIES = 1

# Property order is load-bearing (llama.cpp emits required properties in schema order, so the
# verdict is committed before the justification). Same shapes as auto_assistant.py.
_VERIFY_FORMAT = {"type": "object",
                  "properties": {"ok": {"type": "boolean"}, "fix": {"type": "string"}},
                  "required": ["ok", "fix"]}
_EDIT_FORMAT = {"type": "object",
                "properties": {"edit": {"type": "string"},
                               "avoid": {"type": "array", "items": {"type": "string"}}},
                "required": ["edit", "avoid"]}

# Lustify V10 is a Krea 2 model whose text encoder is Qwen3-VL, so it reads sentences. The old
# prompt asked for under-60-token tag lists, which was the SDXL/CLIP contract and wasted it.
ENHANCE_SYS = (
    "You write prompts for Lustify V10, a photorealistic image model whose text encoder reads "
    "full sentences. Rewrite the user's idea as ONE natural-language description of the photo: "
    "the subject and their appearance, pose and expression, clothing, the setting, the lighting, "
    "and the camera and lens. The user's words are hard requirements: keep every person count, "
    "adult age, gender, ethnicity, clothing and body detail they give exactly, and never add or "
    "drop a subject. Plain sentences, no tag lists, no (word:1.5) weights. Under 100 words. "
    "Output ONLY the prompt."
)

# Folding a change into the prompt that produced a picture. Re-rendering the same seed with a
# mostly unchanged prompt keeps the composition; the merge must therefore leave every word the
# request does not touch alone.
MERGE_SYS = (
    "You edit the prompt of a photorealistic image model. You get the prompt that produced the "
    "current picture and a change the user wants. Return the whole prompt rewritten so the "
    "picture shows that change. Change only what the request touches and keep every other "
    "subject, detail, setting, lighting and camera phrase exactly as written. State the new "
    "state plainly: describe what is there now, never what was removed or changed. Plain "
    "sentences, no tag lists, no weights. Output ONLY the prompt."
)


def _extract_json(text):
    """The first JSON object in an LLM reply that may wrap it in prose or a fence, else None.
    A trimmed version of auto_assistant._extract_json: Functions deploy as single files, so
    there is nothing to import."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None

# WHY REFERENCE EDITS NO LONGER DEFAULT TO SDXL img2img
#
# They produced a different person in the same pose, and no denoise value fixes that.
# SDXL img2img gives the reference exactly one channel of influence — the initial latent —
# and at denoise 0.65 sampling starts above the sigma where facial identity lives, so the
# face is resampled from Lustify's prior rather than reconstructed. Lowering the denoise
# only trades the edit away: below ~0.35 nothing happens, above ~0.55 it is a stranger, and
# a structural edit needs the high end. One scalar cannot be low where the face is and high
# where the jacket is.
#
# FireRed-Image-Edit 1.1 puts the reference in the CONDITIONING instead of the noise budget,
# so it holds ~100% of the source's grain (measured, auto_assistant.py:180-190). It is also
# a censored model, which is why the SDXL path is kept rather than deleted: EDIT_ENGINE="sdxl"
# uses it, accepting that identity will drift, but only for pictures this pipe made (ORIGIN_DIR).
# An uploaded photo always goes through FireRed.
_ENGINES = ("qwen", "sdxl")


class Pipe:
    class Valves(BaseModel):
        EDIT_ENGINE: str = Field(
            default="qwen",
            description="Engine for reference-image edits. 'qwen' = FireRed-Image-Edit 1.1, keeps "
                        "the subject the same person (~40 s). 'sdxl' = the old Lustify img2img "
                        "path, uncensored, but the face WILL drift (~13 s). 'sdxl' applies only to "
                        "pictures this pipe made; an uploaded photo always uses FireRed.")
        EDIT_QUALITY: str = Field(
            default="balanced",
            description="FireRed tier for the first attempt: best (20 steps/cfg 4, ~150 s, the "
                        "negative prompt works) | balanced (8-step Lightning, ~40 s) | fast (the "
                        "same as balanced: FireRed ships no 4-step LoRA). A retry after a failed "
                        "check always runs best.")
        REWRITE: bool = Field(
            default=True,
            description="Rewrite the edit request from the picture before rendering: a concrete "
                        "instruction naming what changes and what stays for FireRed, a full "
                        "description for the SDXL path. One vision call (~15-30 s).")
        CHECK: bool = Field(
            default=True,
            description="Look at the original and the result together and check the edit against "
                        "the request as you typed it. A miss gets one corrected retry; a miss that "
                        "survives it is reported under the picture.")
        OWN_PICTURES: str = Field(
            default="rerender",
            description="How to change a picture this pipe made: 'rerender' = the same seed with "
                        "the change folded into its prompt (Lustify, follows the prompt) | 'edit' "
                        "= FireRed like an upload.")

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        # Lustify V10 (Krea 2), the direct lineage successor to lustifySDXL — same author, moved
        # from SDXL to Krea 2 for training quality. Swapped 2026-10-04; `lustifySDXL.safetensors`
        # stays on disk for rollback (docs/MODELS.md). It is a Krea 2 DiT, so it loads through the
        # SAME stack the t2i lane uses (UNETLoader + CLIPLoader type=krea2 + the Qwen-Image VAE)
        # rather than CheckpointLoaderSimple, and it runs at Krea 2's own sampler settings
        # (er_sde/simple, cfg 1.0, 8 steps) instead of SDXL's dpmpp_2m/karras at cfg 5.
        self.model = "krea2/lustifyV10Krea2_turbo_fp8.safetensors"
        self.clip = "qwen3vl_4b_fp8_scaled.safetensors"
        self.vae = "qwen_image_vae.safetensors"
        # Prompt enhancement MUST use an uncensored model — gemma4/gemma3 refuse this content and
        # would break the pipe's deliberate isolation from the shared config.
        #
        # Was dolphin-venice:24b. Swapped because this build measured 5/5 compliance on exactly this
        # job (prompt-enhancer-style requests) in tests/bench_models.py, matching dolphin, while also
        # being the tenant every other pipe now uses — so this pipe no longer forces a separate
        # 14.3 GB load that evicts whatever was resident. Stock Qwen3.6 was NOT a viable substitute
        # here: it refused 2 of the same 5 requests.
        #
        # It also reports vision capability with a real projector, which is what lets the edit
        # rewrite below actually LOOK at the source photo instead of guessing at it.
        self.text_model = "hermes-genesis:apex-compact"

    def pipes(self):
        return [{"id": "photo", "name": "Photoreal"}]

    # ---- helpers ----
    def _parse(self, messages):
        """(text, reference_image_b64_or_None, ref_is_current). Text comes from the latest user
        message; if that message carries no image, reuse the most recent upload from the last ~6
        user turns — OpenWebUI keeps an upload attached to its ORIGINAL message, so a follow-up
        turn has no image_url part. ref_is_current says whether the image was attached to THIS
        message: an old upload must not silently hijack a fresh t2i request into img2img, so the
        caller only uses a non-current reference when the text actually asks for a modification."""
        text, img, seen, current = None, None, 0, False
        for m in reversed(messages or []):
            if m.get("role") != "user":
                continue
            seen += 1
            c = m.get("content", "")
            cur_text, cur_img = "", None
            if isinstance(c, str):
                cur_text = c
            elif isinstance(c, list):
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        cur_text = part.get("text", "")
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            cur_img = url.split(",", 1)[1]
            if text is None:            # latest user message defines the prompt text
                text = (cur_text or "").strip()
            if cur_img and img is None:  # most recent image within the window is the reference
                img = cur_img
                current = seen == 1
            if img is not None or seen >= 6:
                break
        return (text or ""), img, current

    # Fresh-request phrasing ("create/draw a …", "another one") that must stay t2i even
    # when a previous image exists. Everything else with edit intent modifies the picture
    # on the table.
    _FRESH = re.compile(
        r"\b(create|generate|draw|sketch|paint|illustrate|design|produce|render)\b"
        r".{0,30}\b(image|picture|photo|pic|portrait|scene|of)\b"
        r"|\b(another|new)\s+(one|image|picture|photo)\b", re.I)
    _EDIT_INTENT = re.compile(
        r"\b(edit|change|replace|remove|delete|erase|swap|add|put|turn|recolou?r|adjust|"
        r"retouch|modify|fix|crop|rotate|blur|bigger|smaller|larger|zoom|brighter|darker|"
        r"get rid of|without|instead of|undress|dress|wearing|naked|nude|topless)\b", re.I)
    _DEICTIC = re.compile(
        r"\b(this|that|it)\s+(image|picture|photo|pic|drawing|one)\b"
        r"|\b(make|turn|keep)\s+(it|this|that|him|her|them)\b", re.I)
    # A change to something IN the picture: "make his t-shirt red", "turn her hair blonde",
    # "make the sky purple". Missed until 2026-10-28, when a live probe sent "make his t-shirt
    # red" as a follow-up and got a brand-new picture of those five words. "make the best picture
    # of a sunset" names a new picture, so a picture noun after "the <word>" does not count.
    _POSSESSIVE = re.compile(
        r"\b(make|turn|give|keep|put|paint|colou?r|dye)\s+(his|her|their|its)\b"
        r"|\bmake\s+the\s+(?!\w+\s+(image|picture|photo|pic|portrait)\b)\w+\s+\w+", re.I)

    def _wants_edit_followup(self, text):
        """A text-only follow-up that modifies the previous picture rather than asking
        for a new one. Style conversions ('make this picture animated') and deictic
        references ('make this guy…') count; fresh 'create a picture of…' does not."""
        t = (text or "").strip()
        if not t:
            return False
        if ms and ms.style_conversion(t):
            return True
        if self._DEICTIC.search(t) or self._POSSESSIVE.search(t):
            return True
        if self._FRESH.search(t):
            return False
        return bool(self._EDIT_INTENT.search(t))

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        r = requests.post(f"{self.comfy}/upload/image",
                          files={"image": ("ref.png", raw, "image/png")}, timeout=60)
        return r.json()["name"]

    def _free_vram(self):
        """Unload Ollama models and BLOCK until VRAM is actually released (unload is async), matching
        the sibling pipes — a fire-and-forget unload let the SDXL job start while dolphin (14 GB) was
        still resident."""
        try:
            models = requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", [])
        except Exception:
            models = []
        for m in models:
            try:
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
            except Exception:
                pass
        for _ in range(30):  # up to ~30 s until nothing is loaded
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)  # grace for the CUDA allocator to release

    def _engine(self, own=True):
        """(engine, reason_it_was_downgraded). Never returns 'qwen' without a builder for it.

        `own` says the reference is a picture this pipe rendered (see ORIGIN_DIR). For anything
        else, an upload above all, the uncensored SDXL path is never used: a chosen 'sdxl' becomes
        FireRed, and a missing FireRed is 'none' (the caller refuses) rather than a quiet fall
        back to img2img on someone's photo."""
        want = str(getattr(self.valves, "EDIT_ENGINE", "qwen")).strip().lower()
        if want not in _ENGINES:
            want = "qwen"
        if not own:
            if build_qwen_edit_wf is None:
                return "none", (_SHARED_ERR or "shared module unavailable")
            if want == "sdxl":
                return "qwen", "uploads always use FireRed"
            return "qwen", ""
        if want == "qwen" and build_qwen_edit_wf is None:
            return "sdxl", (_SHARED_ERR or "shared module unavailable")
        return want, ""

    def _own_mode(self):
        v = str(getattr(self.valves, "OWN_PICTURES", "rerender")).strip().lower()
        return v if v in ("rerender", "edit") else "rerender"

    def _enhance(self, prompt):
        """Expand a terse idea into a full natural-language Lustify prompt via the local
        uncensored LLM. Text-to-image only. Falls back to the raw text on any error."""
        if not ENHANCE or not prompt:
            return prompt
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.text_model, "system": ENHANCE_SYS, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 220}},
                timeout=120)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

    # ---- talking to the vision model ----
    def _llm(self, payload, fmt=None, timeout=180):
        """POST /api/generate, schema-constrained when `fmt` is given. A server that rejects the
        schema gets the same request once without it, as in auto_assistant._generate."""
        body = dict(payload)
        if fmt:
            body["format"] = fmt
        r = requests.post(f"{self.ollama}/api/generate", json=body, timeout=timeout)
        if r.status_code != 200 and "format" in body:
            detail = (r.text or "")[:300].lower()
            if "format" in detail or "grammar" in detail or "schema" in detail:
                body.pop("format", None)
                r = requests.post(f"{self.ollama}/api/generate", json=body, timeout=timeout)
        return (r.json().get("response") or "").strip()

    def _rewrite_edit(self, request, ref_b64):
        """(instruction, avoid) for FireRed, written by a model that SEES the picture.

        The raw request used to go to FireRed as typed. Several asks in one sentence lose some of
        them, relative wording ('a bit darker') under-moves an identity-preserving editor, and
        'it' or 'her' names nothing. This is the Assistant's rewrite (media_session's
        EDIT_REWRITE_VISION_SYS): one concrete instruction naming each subject as it appears,
        absolute target states, and what must stay. Falls back to the request as typed."""
        if not request or not ms or not getattr(ms, "EDIT_REWRITE_VISION_SYS", ""):
            return request, ""
        try:
            out = self._llm(
                {"model": self.text_model, "system": ms.EDIT_REWRITE_VISION_SYS,
                 "prompt": f"Request: {request}\n\nRewrite this request.", "images": [ref_b64],
                 "stream": False, "think": False, "keep_alive": 0,
                 "options": {"temperature": 0.4, "num_predict": 400}},
                fmt=_EDIT_FORMAT, timeout=300)
        except Exception:
            return request, ""
        d = _extract_json(out)
        if isinstance(d, dict) and str(d.get("edit") or "").strip():
            av = d.get("avoid")
            avoid = ", ".join(str(x).strip() for x in av if str(x).strip()) if isinstance(av, list) else ""
            return str(d["edit"]).strip(), avoid
        edit = re.search(r"^\s*\**\s*EDIT\**\s*:\s*(.+)$", out or "", re.I | re.M)
        avoid = re.search(r"^\s*\**\s*AVOID\**\s*:\s*(.+)$", out or "", re.I | re.M)
        if edit:
            return edit.group(1).strip(), (avoid.group(1).strip() if avoid else "")
        return request, ""

    @staticmethod
    def _parse_verdict(out):
        """(ok, fix, parsed) from a checker reply: JSON first, then the OK:/FIX: lines. An
        unreadable reply passes (parsed=False), so the check can never be why a picture is lost."""
        d = _extract_json(out)
        if isinstance(d, dict) and isinstance(d.get("ok"), bool):
            fix = str(d.get("fix") or "").strip()
            return d["ok"], ("" if fix.lower().startswith("none") else fix), True
        m = re.search(r"^\s*\**\s*OK\**\s*[:：]\s*\**\s*(yes|no)\b", out or "", re.I | re.M)
        if m is None:
            return True, "", False
        f = re.search(r"^\s*\**\s*FIX\**\s*[:：]\s*\**\s*(.+)$", out or "", re.I | re.M)
        fix = f.group(1).strip().strip("*").strip() if f else ""
        return m.group(1).lower() == "yes", ("" if fix.lower().startswith("none") else fix), True

    def _verify_edit(self, original, instruction, ref_b64, result_b64):
        """(ok, fix, parsed). The checker sees the original FIRST and the result SECOND and
        judges against the request as the user typed it, not the rewrite, which may itself be
        what went wrong (media_session.VERIFY_EDIT_SYS, the Assistant's checker). Fails open."""
        if not ms or not getattr(ms, "VERIFY_EDIT_SYS", "") or not original:
            return True, "", False
        try:
            out = self._llm(
                {"model": self.text_model, "system": ms.VERIFY_EDIT_SYS,
                 "prompt": ms.edit_qa_user_prompt(original, instruction),
                 "images": [ref_b64, result_b64], "stream": False, "think": False,
                 "keep_alive": 0, "options": {"temperature": 0.1, "num_predict": 220}},
                fmt=_VERIFY_FORMAT, timeout=300)
        except Exception:
            return True, "", False
        return self._parse_verdict(out)

    def _merge_prompt(self, prompt, change):
        """The prompt that produced a picture, rewritten to show `change`. Falls back to the
        prompt with the change appended, which still moves Lustify the right way."""
        fallback = f"{prompt} {change}".strip()
        if not prompt:
            return change
        try:
            out = self._llm(
                {"model": self.text_model, "system": MERGE_SYS,
                 "prompt": f"Prompt of the current picture:\n{prompt}\n\nChange: {change}",
                 "stream": False, "think": False, "keep_alive": 0,
                 "options": {"temperature": 0.3, "num_predict": 300}}, timeout=180)
        except Exception:
            return fallback
        return out.strip().strip('"') or fallback

    # ---- VRAM for the checker ----
    def _comfy_idle(self):
        """True when ComfyUI has nothing running or queued, so a /free cannot evict another
        pipe's render. Fails open."""
        try:
            q = requests.get(f"{self.comfy}/queue", timeout=10).json()
            return not q.get("queue_running") and not q.get("queue_pending")
        except Exception:
            return True

    def _vram_free_gib(self):
        try:
            d = requests.get(f"{self.comfy}/system_stats", timeout=10).json()
            return float((d.get("devices") or [{}])[0].get("vram_free", 0)) / (1024 ** 3)
        except Exception:
            return None

    def _comfy_free(self, need_gib=20.0):
        """Unload ComfyUI's models so the ~18 GB vision model can load for the check, then wait
        until the memory is really free. Leaves another pipe's running render alone."""
        if not self._comfy_idle():
            return
        try:
            requests.post(f"{self.comfy}/free", json={"unload_models": True, "free_memory": True},
                          timeout=30)
        except Exception:
            pass
        for _ in range(20):
            v = self._vram_free_gib()
            if v is None or v >= need_gib:
                return
            time.sleep(1)

    # ---- where a picture came from ----
    @staticmethod
    def _digest(b64):
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raw = str(b64).encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    @staticmethod
    def _origin_path(cid):
        return os.path.join(ORIGIN_DIR, hashlib.sha1(str(cid).encode()).hexdigest()[:24] + ".json")

    def _origin(self, cid, b64):
        """The record of a picture this pipe rendered in this chat, or None. Never raises."""
        if not cid or not b64:
            return None
        try:
            with open(self._origin_path(cid)) as f:
                recs = json.load(f).get("pictures") or []
        except Exception:
            return None
        dg = self._digest(b64)
        for r in reversed(recs):
            if r.get("digest") == dg:
                return r
        return None

    def _remember_origin(self, cid, b64, prompt, seed):
        """Record a picture rendered from `prompt` and `seed`. Never raises."""
        if not cid or not b64 or not prompt or seed is None:
            return
        try:
            os.makedirs(ORIGIN_DIR, exist_ok=True)
            path = self._origin_path(cid)
            try:
                with open(path) as f:
                    recs = json.load(f).get("pictures") or []
            except Exception:
                recs = []
            recs.append({"digest": self._digest(b64), "prompt": prompt, "seed": int(seed),
                         "ts": time.time()})
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"pictures": recs[-_ORIGIN_PER_CHAT:]}, f)
            os.replace(tmp, path)
            files = [(os.path.getmtime(os.path.join(ORIGIN_DIR, n)), n)
                     for n in os.listdir(ORIGIN_DIR) if n.endswith(".json")]
            for _, name in sorted(files)[:-_ORIGIN_KEEP]:
                os.remove(os.path.join(ORIGIN_DIR, name))
        except Exception:
            pass

    def _enhance_edit(self, instruction, ref_b64):
        """(prompt, avoid) for the SDXL edit path — a VISION rewrite.

        This is the fix for the second-largest cause of identity drift. The old code skipped the
        rewriter entirely whenever a reference was attached (`if not ref_b64`), so a raw instruction
        went straight into CLIPTextEncode. SDXL cannot follow instructions; it renders a description
        of a finished image, so "undress this girl, keep her the same" was read as a bag of generic
        subject tags — an active pull toward Lustify's average face, which is exactly the observed
        failure.

        The rewriter SEES the photo (this tenant has vision + a real projector), so it can restate
        the traits that must survive. Generic "keep them the same" wording provably does not work:
        _style_enrich in auto_assistant.py exists only because it failed that way in production.
        """
        if not instruction:
            return instruction, ""
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.text_model, "system": SDXL_EDIT_REWRITE_SYS,
                      "prompt": f"Request: {instruction}\n\nDescribe the photo as it must look after "
                                f"this edit, leading with who the person is.",
                      "images": [ref_b64], "stream": False, "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.4, "num_predict": 400}},
                timeout=300)
            return parse_rewrite((r.json().get("response") or "").strip(), instruction)
        except Exception:
            return instruction, ""

    def _build_t2i_wf(self, prompt, negative, seed, ref_name=None):
        """Lustify V10 (Krea 2). txt2img, or img2img when ref_name is set. Output node is "9".

        THE NEGATIVE IS INERT, and that is a property of the model, not an oversight. Krea 2 is a
        cfg-1.0 model, where guidance is mathematically unable to act on a negative branch, so it
        is built as `ConditioningZeroOut` exactly as the t2i lane builds it — the `negative`
        parameter is accepted and ignored rather than silently dropped, so callers keep one
        signature. (Measured on the SDXL predecessor: same seed with negative="" and a real
        negative produced byte-identical output at cfg 1.0, and differed at cfg 4.0.) Quality is
        steered by the POSITIVE prompt here. `steps` may be raised toward 10-12; cfg stays 1.0.
        """
        wf = {
          "u": {"class_type": "UNETLoader",
                "inputs": {"unet_name": self.model, "weight_dtype": "default"}},
          "c": {"class_type": "CLIPLoader",
                "inputs": {"clip_name": self.clip, "type": "krea2", "device": "default"}},
          "v": {"class_type": "VAELoader", "inputs": {"vae_name": self.vae}},
          "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["c", 0]}},
          # Kept as a real text encode (not ZeroOut) so the AVOID-traits channel still EXISTS in
          # the graph and one config change (a cfg above 1.0, e.g. on the Raw variant) revives it.
          # At cfg 1.0 it has no effect on the result -- see the note above.
          "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["c", 0]}},
          "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0,
                    "model": ["u", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
          "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["v", 0]}},
          "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if ref_name:  # image-to-image: encode the reference as the starting latent
            wf["20"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
            wf["21"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["20", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1}}
            wf["22"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["v", 0]}}
            wf["3"]["inputs"]["latent_image"] = ["22", 0]
            wf["3"]["inputs"]["denoise"] = DENOISE
        else:  # text-to-image
            wf["5"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}}
        return wf

    def _submit_poll(self, wf, out_node):
        """Submit a workflow and poll to completion. Returns (png_bytes, err_or_None).

        out_node is a parameter because the two graphs save from different ids — SDXL from "9",
        Qwen-Image-Edit from "s". This used to be hardcoded to "9", which would have made every
        Qwen render return "produced no image" no matter how well it worked.
        """
        err = None
        for attempt in (1, 2):
            try:
                r = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30)
            except Exception as e:
                return None, f"⚠️ Image backend unreachable: {e}"
            try:
                j = r.json()
            except Exception:
                j = {}
            # ComfyUI reachable but rejected the workflow (HTTP 400 + node_errors, no prompt_id) →
            # surface the actual validation error instead of the misleading 'unreachable'.
            if getattr(r, "status_code", 200) != 200 or "prompt_id" not in j:
                detail = j.get("error") or j.get("node_errors") or getattr(r, "text", "")
                return None, f"⚠️ Image workflow rejected: {str(detail)[:300]}"
            pid = j["prompt_id"]
            err = None
            for _ in range(900):  # ~15 min: SDXL is fast, but Wan video jobs can hold the queue
                try:
                    h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
                except Exception:
                    time.sleep(1); continue
                if pid in h:
                    st = h[pid].get("status", {})
                    if st.get("status_str") == "error":
                        d = next((mm[1] for mm in st.get("messages", []) if mm[0] == "execution_error"), {})
                        err = (f"{d.get('exception_type','Error')} in {d.get('node_type','?')} — "
                               f"{str(d.get('exception_message',''))[:200]}")
                        break
                    imgs = h[pid].get("outputs", {}).get(out_node, {}).get("images", [])
                    if not imgs:
                        return None, f"Job finished but produced no image: {str(st)[:200]}"
                    im = imgs[0]
                    data = requests.get(f"{self.comfy}/view",
                        params={"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": "output"},
                        timeout=30).content
                    return data, None
                time.sleep(1)
            else:
                # Timed out: cancel the still-queued/running job so it doesn't execute later as an
                # orphan (burning GPU and evicting whatever is loaded by then).
                #
                # /queue delete is safe — it names our pid. /interrupt is NOT: it takes no
                # argument and cancels whatever ComfyUI is executing right now. If our job was
                # still PENDING, the delete above already removed it and the interrupt then kills
                # a stranger's render — an Assistant video 20 minutes in, most likely, since that
                # is the longest thing on this box. Only interrupt when the running job is ours.
                try:
                    requests.post(f"{self.comfy}/queue", json={"delete": [pid]}, timeout=10)
                    running = requests.get(f"{self.comfy}/queue", timeout=10).json().get(
                        "queue_running") or []
                    # Entries are [index, prompt_id, prompt, extra_data, outputs].
                    if any(len(e) > 1 and e[1] == pid for e in running):
                        requests.post(f"{self.comfy}/interrupt", timeout=10)
                except Exception:
                    pass
                return None, "⏳ Timed out waiting for the image (cancelled the queued job)."
            if attempt == 1 and err and "OutOfMemory" in err:
                self._free_vram(); continue
            break
        return None, f"⚠️ Image generation failed: {err}"

    def _generate(self, prompt: str, ref_b64, seed=None, cid=None):
        """Returns (markdown_or_error, meta). meta carries what the status line reports, plus
        `origin` ({prompt, seed}) whenever the picture can be drawn again from a prompt.

        Three paths:
          * no reference: text-to-image on Lustify;
          * a picture this pipe made, with OWN_PICTURES=rerender: the same seed with the change
            folded into its own prompt (_rerender);
          * anything else, an upload above all: FireRed with the request rewritten from the
            picture (_edit_firered). The SDXL img2img engine only for this pipe's own pictures.
        Edits are checked against the request as typed, with one corrected retry.

        All LLM work happens BEFORE _free_vram, or we evict the model we are about to call.
        The seed is returned rather than stashed on self: OpenWebUI reuses ONE Pipe instance
        across concurrent chats, so instance state races between users.
        """
        seed_given = seed is not None
        seed = random.randint(0, 2 ** 31 - 1) if seed is None else seed
        meta = {"seed": seed, "engine": "sdxl-t2i", "downgrade": "", "qa_rounds": 0}
        t0 = time.time()

        if ref_b64:
            origin = self._origin(cid, ref_b64)
            own = origin is not None
            meta["own"] = own
            style = ms.style_conversion(prompt) if (ms and prompt) else None
            if own and prompt and not style and origin.get("prompt") and self._own_mode() == "rerender":
                return self._rerender(prompt, ref_b64, origin, seed if seed_given else None, meta, t0)
            engine, downgrade = self._engine(own)
            meta.update(engine=engine, downgrade=downgrade)
            if engine == "none":
                return (f"⚠️ Editing an uploaded photo needs FireRed-Image-Edit, which is not "
                        f"available ({downgrade}). The uncensored img2img path is only for pictures "
                        f"Photoreal made itself."), meta
            if engine == "qwen":
                return self._edit_firered(prompt, ref_b64, style, seed, meta, t0)
            return self._edit_sdxl(prompt, ref_b64, seed, meta, t0)

        instruction = self._enhance(prompt) if prompt else \
            "photorealistic, highly detailed, sharp focus, natural lighting"
        self._free_vram()
        data, err = self._submit_poll(self._build_t2i_wf(instruction, NEG, seed), "9")
        meta["render_s"] = round(time.time() - t0, 1)
        self._metric("photo_t2i", meta, prompt, err)
        if err:
            return err, meta
        meta["origin"] = {"prompt": instruction, "seed": seed}
        return self._md(data, seed, instruction), meta

    def _edit_firered(self, prompt, ref_b64, style, seed, meta, t0):
        """FireRed-Image-Edit 1.1 on the reference, then the check and one corrected retry."""
        tier_name = str(getattr(self.valves, "EDIT_QUALITY", "balanced")).strip().lower()
        tier = resolve_tier(tier_name)
        negative = ""
        self._comfy_free()  # the rewrite and the check load the ~18 GB vision model
        if style:
            # Whole-image restyle ("make this picture animated"): the purpose-built instruction,
            # always on the full tier, because the negative (the style being left behind) only
            # bites at cfg > 1. The pipe's own NEG (which bans cartoon/anime for t2i) is not used,
            # so a requested style is never fought by the pipe's negative prompt.
            instruction, negative = style
            tier, tier_name = resolve_tier("best"), "best"
        elif prompt and getattr(self.valves, "REWRITE", True):
            instruction, negative = self._rewrite_edit(prompt, ref_b64)
        else:
            instruction = prompt or "improve the overall quality, keep everything else the same"
        meta.update(tier=tier_name, rewrote=bool(prompt) and instruction != prompt)
        self._free_vram()
        try:
            ref_name = self._upload(ref_b64)
        except Exception as e:
            return f"⚠️ Could not upload reference image: {e}", meta
        wf = build_qwen_edit_wf(instruction, ref_name, seed, tier["cfg"], tier["steps"],
                                negative=negative, lightning=tier["lightning"])
        data, err = self._submit_poll(wf, "s")
        if err:
            meta["render_s"] = round(time.time() - t0, 1)
            self._metric("photo_edit", meta, prompt, err)
            return err, meta

        def retry(cur, fix):
            # The correction goes on the instruction, and the retry runs the full tier, where the
            # editor follows more closely and the negative acts. A fresh seed: the same one tends
            # to repeat the miss.
            nxt = f"{cur} IMPORTANT correction: {fix}"
            best = resolve_tier("best")
            s = random.randint(0, 2 ** 31 - 1)
            self._free_vram()
            d, e = self._submit_poll(build_qwen_edit_wf(nxt, ref_name, s, best["cfg"], best["steps"],
                                                        negative=negative,
                                                        lightning=best["lightning"]), "s")
            if d is not None:
                meta.update(seed=s, tier="best")
            return nxt, d, e

        data, note = self._checked(prompt, instruction, ref_b64, data, meta, retry)
        meta["render_s"] = round(time.time() - t0, 1)
        self._metric("photo_edit", meta, prompt, None)
        return self._md(data, meta["seed"], instruction) + note, meta

    def _rerender(self, change, ref_b64, origin, seed, meta, t0):
        """A picture this pipe made, drawn again with `change` folded into its own prompt.

        The same seed and a mostly unchanged prompt keep the composition, and Lustify follows its
        prompt, which FireRed often does not do for the edits this pipe gets most (21 of the
        first 29). Checked against the original like an edit; the retry folds the checker's
        correction into the prompt as well and keeps the seed."""
        seed = int(origin.get("seed") or 0) if seed is None else seed
        meta.update(engine="rerender", seed=seed, tier=None)
        self._comfy_free()
        prompt = self._merge_prompt(origin["prompt"], change)
        meta["rewrote"] = True
        self._free_vram()
        data, err = self._submit_poll(self._build_t2i_wf(prompt, NEG, seed), "9")
        if err:
            meta["render_s"] = round(time.time() - t0, 1)
            self._metric("photo_edit", meta, change, err)
            return err, meta

        def retry(cur, fix):
            nxt = self._merge_prompt(cur, fix)
            self._free_vram()
            d, e = self._submit_poll(self._build_t2i_wf(nxt, NEG, seed), "9")
            return nxt, d, e

        data, note, prompt = self._checked(change, prompt, ref_b64, data, meta, retry, keep=True)
        meta["render_s"] = round(time.time() - t0, 1)
        meta["origin"] = {"prompt": prompt, "seed": seed}
        self._metric("photo_edit", meta, change, None)
        return self._md(data, seed, prompt) + note, meta

    def _checked(self, request, cur, ref_b64, data, meta, retry, keep=False):
        """Check `data` against `request`; on a miss call retry(cur, fix) -> (cur, data, err)
        up to QA_RETRIES times. Returns (data, note) or, with keep=True, (data, note, cur).
        The note is shown under the picture whenever a miss survives, so a failed edit is never
        presented as a finished one."""
        note = ""
        if request and getattr(self.valves, "CHECK", True):
            for rnd in range(QA_RETRIES + 1):
                self._comfy_free()
                ok, fix, _ = self._verify_edit(request, cur, ref_b64,
                                               base64.b64encode(data).decode())
                if ok:
                    note = ""
                    break
                meta["qa_fix"] = meta.get("qa_fix") or fix
                if not fix or rnd == QA_RETRIES:
                    note = self._qa_note(fix)
                    break
                nxt, data2, err2 = retry(cur, fix)
                if data2 is None:
                    note = self._qa_note(fix, err2)
                    break
                cur, data = nxt, data2
                meta["qa_rounds"] = meta.get("qa_rounds", 0) + 1
            self._comfy_free()  # idle means the GPU is empty for the next chat turn
        return (data, note, cur) if keep else (data, note)

    @staticmethod
    def _qa_note(fix, err=None):
        if not fix:
            return ("\n\n*The check says this edit missed something but gave no correction, so "
                    "the picture is shown as rendered.*")
        if err:
            return f"\n\n*⚠️ The check says: {fix} The corrected retry failed ({err}).*"
        return f"\n\n*⚠️ After a corrected retry the check still says: {fix}*"

    def _edit_sdxl(self, prompt, ref_b64, seed, meta, t0):
        """The old Lustify img2img edit, for this pipe's own pictures only (see _engine). No check:
        it cannot take a correction, it re-describes the whole picture."""
        instruction = prompt or "photorealistic, highly detailed, sharp focus, natural lighting"
        avoid = ""
        if getattr(self.valves, "REWRITE", True) and prompt:
            instruction, avoid = self._enhance_edit(instruction, ref_b64)
        meta["rewrote"] = bool(avoid)
        self._free_vram()
        try:
            ref_name = self._upload(ref_b64)
        except Exception as e:
            return f"⚠️ Could not upload reference image: {e}", meta
        neg = f"{NEG}, {avoid}" if avoid else NEG
        data, err = self._submit_poll(self._build_t2i_wf(instruction, neg, seed, ref_name=ref_name), "9")
        meta["render_s"] = round(time.time() - t0, 1)
        self._metric("photo_edit", meta, prompt, err)
        if err:
            return err, meta
        return self._md(data, seed, instruction), meta

    @staticmethod
    def _md(data, seed, label):
        # Seed in the alt-text so it survives a page reload and a copy-paste, matching the
        # data-seed= precedent on the video pipe. Brackets and newlines would break the link.
        alt = re.sub(r"\s+", " ", re.sub(r"[\[\]]", " ", label or "")).strip()[:50]
        return f"![seed {seed} · {alt}](data:image/png;base64,{base64.b64encode(data).decode()})"

    @staticmethod
    def _metric(job, meta, request, err):
        metric(METRICS_PATH, job=job, ok=not err, engine=meta.get("engine"), seed=meta.get("seed"),
               tier=meta.get("tier"), own=meta.get("own"), rewrote=meta.get("rewrote"),
               qa_rounds=meta.get("qa_rounds", 0), qa_fix=(meta.get("qa_fix") or "")[:200] or None,
               denoise=DENOISE if meta.get("engine") == "sdxl" else 1.0,
               render_s=meta.get("render_s"), request=(request or "")[:160],
               err=(err or "")[:160] or None)

    # ---------- native OpenWebUI status line (progress + timing) ----------
    async def _status(self, emitter, description, done=False):
        if emitter:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    @staticmethod
    def _fmt_dur(secs):
        s = int(round(secs))
        return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"

    async def _tracked(self, emitter, label, coro):
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
        if isinstance(result, str) and result.startswith("!["):
            await self._status(emitter, f"{verb} in {self._fmt_dur(elapsed)} · {detail}", done=True)
        else:
            await self._status(emitter, "", done=True)
        return result

    @staticmethod
    def _detail(meta, editing):
        """The status line. Naming the engine is not decoration — the two have very different
        identity guarantees, and a silent downgrade would look like the model getting worse."""
        seed = f"seed {meta['seed']}"
        if not editing:
            return f"Lustify V10 Krea 2 · 1024×1024 · 8 steps · {seed}"
        n = meta.get("qa_rounds") or 0
        retried = f" · {n} corrected retr{'y' if n == 1 else 'ies'}" if n else ""
        if meta["engine"] == "rerender":
            return f"Lustify V10 re-render of its own prompt · {seed}{retried}"
        if meta["engine"] == "qwen":
            d = f"FireRed Edit 1.1 · {meta.get('tier', 'balanced')} · {seed}{retried}"
            return d + (f" · {meta['downgrade']}" if meta.get("downgrade") else "")
        warn = " · identity may drift"
        if meta.get("downgrade"):
            warn = " · FireRed unavailable, identity may drift"
        return f"Lustify V10 Krea 2 img2img · d{DENOISE} · 8 steps · {seed}{warn}"

    async def pipe(self, body: dict, __event_emitter__=None, __metadata__=None, __task__=None):
        emitter = __event_emitter__
        msgs = body.get("messages", [])
        text, ref, ref_is_current = self._parse(msgs)
        # OpenWebUI background tasks (title/tags/follow-ups/search decisions) must never
        # render — they used to run real SDXL jobs on task boilerplate through this pipe.
        if ms and ms.is_task_request(text, __task__):
            return ms.answer_task(self.ollama, msgs)
        if not text and not ref:
            return "Type what you'd like me to create (and optionally attach a reference image)."
        cid = ms.chat_id_of(body, __metadata__) if ms else "default"
        wants_edit = self._wants_edit_followup(text)
        # A stale upload from an earlier turn must not hijack a fresh request into img2img.
        if ref is not None and not ref_is_current and not wants_edit:
            ref = None
        # Follow-up modification with no attached image → edit the most recent picture in
        # this chat: history first (any OWUI message shape, generated images included),
        # then the persistent per-chat store (survives deploys and model switches). This
        # is the fix for "make this picture animated" generating an unrelated stranger —
        # the request now EDITS the previous image instead of running a fresh t2i with the
        # follow-up words as the whole prompt.
        if ref is None and text and wants_edit and ms:
            ref = ms.find_recent_image(msgs) or ms.recall_image(cid)
        text, seed = parse_seed(text)
        editing = ref is not None
        label = "Editing image" if editing else "Generating image"
        (result, meta), el = await self._tracked(
            emitter, label, asyncio.to_thread(self._generate, text, ref, seed, cid if ms else None))
        if ms and isinstance(result, str):
            m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", result)
            if m:
                ms.remember_image(cid, m.group(1))
                o = meta.get("origin")
                if o:  # rendered from a prompt: a later edit can re-render it (see ORIGIN_DIR)
                    self._remember_origin(cid, m.group(1), o["prompt"], o["seed"])
        return await self._finish(emitter, result, "Edited" if editing else "Generated", el,
                                  self._detail(meta, editing))

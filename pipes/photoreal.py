"""
title: Uncensored
author: local
version: 0.5.0
required_open_webui_version: 0.5.0
description: Photorealistic uncensored images (Lustify SDXL) via local ComfyUI. Reference-image edits run through Qwen-Image-Edit so the subject stays the same person. Non-blocking (async); auto-frees GPU VRAM.
"""
import asyncio, base64, os, random, re, sys, time
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

NEG = ("cartoon, anime, drawing, painting, illustration, 3d, render, cgi, sketch, "
       "deformed, disfigured, bad anatomy, bad hands, extra fingers, mutated hands, "
       "watermark, signature, text, blurry, lowres, low quality, worst quality")
DENOISE = 0.65  # SDXL img2img strength. Only used on the fallback path — see the note below.
ENHANCE = True  # expand a terse idea into a dense SDXL prompt via the local uncensored LLM (txt2img)
METRICS_PATH = os.environ.get("MEDIA_METRICS", "/app/backend/data/media_metrics.jsonl")

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
# Qwen-Image-Edit 2509 puts the reference in the CONDITIONING instead of the noise budget,
# so it holds ~100% of the source's grain (measured, auto_assistant.py:180-190). It is also
# a censored model, which is why the SDXL path is kept rather than deleted: set
# EDIT_ENGINE="sdxl" for anything Qwen refuses, and accept that identity will drift.
_ENGINES = ("qwen", "sdxl")


class Pipe:
    class Valves(BaseModel):
        EDIT_ENGINE: str = Field(
            default="qwen",
            description="Engine for reference-image edits. 'qwen' = Qwen-Image-Edit 2509, keeps "
                        "the subject the same person (~36 s). 'sdxl' = the old Lustify img2img "
                        "path — uncensored, but the face WILL drift (~13 s).")
        EDIT_QUALITY: str = Field(
            default="balanced",
            description="Qwen tier: best (20 steps/cfg 4, ~152 s, negative prompt works) | "
                        "balanced (8 steps, ~36 s) | fast (4 steps, ~20 s). The negative prompt "
                        "is INERT on balanced and fast — they run cfg 1.0.")
        REWRITE: bool = Field(
            default=True,
            description="Rewrite the edit request before rendering. On the SDXL path this is what "
                        "stops raw instructions being fed to a model that only understands "
                        "descriptions; it costs one vision call (~15-30 s).")

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.model = "lustifySDXL.safetensors"
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
        """(text, reference_image_b64_or_None). Text comes from the latest user message; if that
        message carries no image, reuse the most recent image from the last ~6 user turns — OpenWebUI
        keeps an upload attached to its ORIGINAL message, so a follow-up turn has no image_url part
        and this pipe would otherwise silently drop img2img back to txt2img."""
        text, img, seen = None, None, 0
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
            if img is not None or seen >= 6:
                break
        return (text or ""), img

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

    def _engine(self):
        """(engine, reason_it_was_downgraded). Never returns 'qwen' without a builder for it."""
        want = str(getattr(self.valves, "EDIT_ENGINE", "qwen")).strip().lower()
        if want not in _ENGINES:
            want = "qwen"
        if want == "qwen" and build_qwen_edit_wf is None:
            return "sdxl", (_SHARED_ERR or "shared module unavailable")
        return want, ""

    def _enhance(self, prompt):
        """Expand a terse idea into a dense, comma-separated SDXL prompt via the local uncensored
        LLM. Text-to-image only. Falls back to the raw text on any error."""
        if not ENHANCE or not prompt:
            return prompt
        sys_p = ("You write prompts for the Lustify SDXL photorealistic image model. Rewrite the user's "
                 "idea as ONE dense, comma-separated prompt of concrete visual tags: subject and "
                 "appearance, pose, setting, lighting, camera and lens, photographic style. Keep every "
                 "explicit specification the user gives (person count, adult age, gender, ethnicity, "
                 "clothing/anatomy) exactly. Under 60 tokens. Output ONLY the prompt — no preamble, no quotes.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.text_model, "system": sys_p, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 120}},
                timeout=120)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

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

    def _build_sdxl_wf(self, prompt, negative, seed, ref_name=None):
        """Lustify SDXL. txt2img, or img2img when ref_name is set. Output node is "9"."""
        wf = {
          "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.model}},
          "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
          "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
          "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 30, "cfg": 5.0,
                    "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
                    "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
          "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
          "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if ref_name:  # image-to-image: encode the reference as the starting latent
            wf["20"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
            wf["21"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["20", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1}}
            wf["22"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["4", 2]}}
            wf["3"]["inputs"]["latent_image"] = ["22", 0]
            wf["3"]["inputs"]["denoise"] = DENOISE
        else:  # text-to-image
            wf["5"] = {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}}
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

    def _generate(self, prompt: str, ref_b64, seed=None):
        """Returns (markdown_or_error, meta). meta carries what the status line reports.

        The seed is returned rather than stashed on self: OpenWebUI reuses ONE Pipe instance
        across concurrent chats, so instance state races between users.
        """
        seed = random.randint(0, 2 ** 31 - 1) if seed is None else seed
        engine, downgrade = self._engine() if ref_b64 else ("sdxl", "")
        meta = {"seed": seed, "engine": engine if ref_b64 else "sdxl-t2i", "downgrade": downgrade}
        t0 = time.time()

        # All LLM work happens BEFORE _free_vram, or we evict the model we are about to call.
        avoid = ""
        if ref_b64:
            if engine == "qwen":
                tier = resolve_tier(getattr(self.valves, "EDIT_QUALITY", "balanced"))
                meta["tier"] = str(getattr(self.valves, "EDIT_QUALITY", "balanced")).strip().lower()
                # Qwen takes an imperative instruction natively, so the raw request is already the
                # right shape. No rewrite — the wrong rewrite (SDXL tag soup) would actively hurt.
                instruction = prompt or "improve the overall quality, keep everything else the same"
            else:
                instruction = prompt or "photorealistic, highly detailed, sharp focus, natural lighting"
                if getattr(self.valves, "REWRITE", True) and prompt:
                    instruction, avoid = self._enhance_edit(instruction, ref_b64)
        else:
            instruction = self._enhance(prompt) if prompt else \
                "photorealistic, highly detailed, sharp focus, natural lighting"

        self._free_vram()

        if ref_b64:
            try:
                ref_name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload reference image: {e}", meta
            if engine == "qwen":
                wf = build_qwen_edit_wf(instruction, ref_name, seed, tier["cfg"], tier["steps"],
                                        negative="", lightning=tier["lightning"])
                out_node = "s"
            else:
                neg = f"{NEG}, {avoid}" if avoid else NEG
                wf = self._build_sdxl_wf(instruction, neg, seed, ref_name=ref_name)
                out_node = "9"
        else:
            wf = self._build_sdxl_wf(instruction, NEG, seed)
            out_node = "9"

        data, err = self._submit_poll(wf, out_node)
        meta["render_s"] = round(time.time() - t0, 1)
        metric(METRICS_PATH, job="photo_edit" if ref_b64 else "photo_t2i", ok=not err,
               engine=meta["engine"], seed=seed, tier=meta.get("tier"),
               denoise=DENOISE if (ref_b64 and engine == "sdxl") else 1.0,
               rewrote=bool(avoid), render_s=meta["render_s"], err=(err or "")[:160] or None)
        if err:
            return err, meta
        # Seed in the alt-text so it survives a page reload and a copy-paste, matching the
        # data-seed= precedent on the video pipe.
        return (f"![seed {seed} · {instruction[:50]}]"
                f"(data:image/png;base64,{base64.b64encode(data).decode()})"), meta

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
            return f"Lustify SDXL · 1024×1024 · 30 steps · {seed}"
        if meta["engine"] == "qwen":
            return f"Qwen-Image-Edit · {meta.get('tier', 'balanced')} · {seed}"
        warn = " · identity may drift"
        if meta.get("downgrade"):
            warn = " · Qwen unavailable, identity may drift"
        return f"Lustify SDXL img2img · d{DENOISE} · 30 steps · {seed}{warn}"

    async def pipe(self, body: dict, __event_emitter__=None):
        emitter = __event_emitter__
        text, ref = self._parse(body.get("messages", []))
        if not text and not ref:
            return "Type what you'd like me to create (and optionally attach a reference image)."
        text, seed = parse_seed(text)
        editing = ref is not None
        label = "Editing image" if editing else "Generating image"
        (result, meta), el = await self._tracked(
            emitter, label, asyncio.to_thread(self._generate, text, ref, seed))
        return await self._finish(emitter, result, "Edited" if editing else "Generated", el,
                                  self._detail(meta, editing))

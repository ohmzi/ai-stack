"""
title: Uncensored
author: local
version: 0.4.0
required_open_webui_version: 0.5.0
description: Photorealistic uncensored images (Lustify SDXL) via local ComfyUI. Supports a reference image (image-to-image). Non-blocking (async); auto-frees GPU VRAM.
"""
import asyncio, requests, time, base64, random

NEG = ("cartoon, anime, drawing, painting, illustration, 3d, render, cgi, sketch, "
       "deformed, disfigured, bad anatomy, bad hands, extra fingers, mutated hands, "
       "watermark, signature, text, blurry, lowres, low quality, worst quality")
DENOISE = 0.65  # img2img strength: lower = closer to your reference, higher = more change
ENHANCE = True  # expand a terse idea into a dense SDXL prompt via the local uncensored LLM (txt2img)


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.model = "lustifySDXL.safetensors"
        # Prompt enhancement MUST use the uncensored model — gemma4/gemma3 refuse this content and
        # would break the pipe's deliberate isolation from the shared config.
        self.text_model = "dolphin-venice:24b"

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

    def _enhance(self, prompt):
        """Expand a terse idea into a dense, comma-separated SDXL prompt via dolphin (the uncensored
        LLM). Falls back to the raw text on any error."""
        if not ENHANCE or not prompt:
            return prompt
        sys = ("You write prompts for the Lustify SDXL photorealistic image model. Rewrite the user's "
               "idea as ONE dense, comma-separated prompt of concrete visual tags: subject and "
               "appearance, pose, setting, lighting, camera and lens, photographic style. Keep every "
               "explicit specification the user gives (person count, adult age, gender, ethnicity, "
               "clothing/anatomy) exactly. Under 60 tokens. Output ONLY the prompt — no preamble, no quotes.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.text_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 120}},
                timeout=120)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

    def _generate(self, prompt: str, ref_b64):
        if not ref_b64:  # enhance txt2img prompts (dolphin), BEFORE freeing VRAM; img2img keeps raw text
            prompt = self._enhance(prompt)
        self._free_vram()
        wf = {
          "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.model}},
          "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
          "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["4", 1]}},
          "3": {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": 30, "cfg": 5.0,
                    "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
                    "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
          "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
          "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if ref_b64:  # image-to-image: encode the uploaded reference as the starting latent
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload reference image: {e}"
            wf["20"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            wf["21"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["20", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1}}
            wf["22"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["4", 2]}}
            wf["3"]["inputs"]["latent_image"] = ["22", 0]
            wf["3"]["inputs"]["denoise"] = DENOISE
        else:  # text-to-image
            wf["5"] = {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}}
        err = None
        for attempt in (1, 2):
            try:
                r = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30)
            except Exception as e:
                return f"⚠️ Image backend unreachable: {e}"
            try:
                j = r.json()
            except Exception:
                j = {}
            # ComfyUI reachable but rejected the workflow (HTTP 400 + node_errors, no prompt_id) →
            # surface the actual validation error instead of the misleading 'unreachable'.
            if getattr(r, "status_code", 200) != 200 or "prompt_id" not in j:
                detail = j.get("error") or j.get("node_errors") or getattr(r, "text", "")
                return f"⚠️ Image workflow rejected: {str(detail)[:300]}"
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
                    imgs = h[pid].get("outputs", {}).get("9", {}).get("images", [])
                    if not imgs:
                        return f"Job finished but produced no image: {str(st)[:200]}"
                    im = imgs[0]
                    data = requests.get(f"{self.comfy}/view",
                        params={"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": "output"},
                        timeout=30).content
                    return f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()})"
                time.sleep(1)
            else:
                # Timed out: cancel the still-queued/running job so it doesn't execute later as an
                # orphan (burning GPU and evicting whatever is loaded by then).
                try:
                    requests.post(f"{self.comfy}/queue", json={"delete": [pid]}, timeout=10)
                    requests.post(f"{self.comfy}/interrupt", timeout=10)
                except Exception:
                    pass
                return "⏳ Timed out waiting for the image (cancelled the queued job)."
            if attempt == 1 and err and "OutOfMemory" in err:
                self._free_vram(); continue
            break
        return f"⚠️ Image generation failed: {err}"

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

    async def pipe(self, body: dict, __event_emitter__=None):
        emitter = __event_emitter__
        text, ref = self._parse(body.get("messages", []))
        if not text and not ref:
            return "Type what you'd like me to create (and optionally attach a reference image)."
        if not text:
            text = "photorealistic, highly detailed, sharp focus, natural lighting"
        editing = ref is not None
        label = "Editing image" if editing else "Generating image"
        result, el = await self._tracked(emitter, label, asyncio.to_thread(self._generate, text, ref))
        detail = "Lustify SDXL img2img · 30 steps" if editing else "Lustify SDXL · 1024×1024 · 30 steps"
        return await self._finish(emitter, result, "Edited" if editing else "Generated", el, detail)

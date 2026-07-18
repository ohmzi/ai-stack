"""
title: Krea 2 Image
author: local
version: 1.0.0
required_open_webui_version: 0.5.0
description: Photoreal images with Krea 2 Turbo via local ComfyUI. Optional trained LoRA, a local prompt-enhancer (Gemma), an optional extra refinement pass, and gentle img2img edits (attach an image + describe a small change → it stays ~90% the same). Non-blocking (async); auto-frees GPU VRAM.
"""
import asyncio, requests, time, base64, random, re
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        LORA_FILE: str = Field(
            default="",
            description="LoRA filename under models/loras (e.g. 'krea2/mylora_comfyui.safetensors'). Empty = base Krea 2.",
        )
        LORA_STRENGTH: float = Field(
            default=1.0, description="LoRA strength. Typical 0.6–1.25; lower it if the LoRA overpowers the prompt."
        )
        TRIGGER: str = Field(
            default="",
            description="Trigger word/phrase for your LoRA. Prepended to every prompt when LORA_FILE is set.",
        )
        ENHANCE: bool = Field(
            default=True,
            description="Use the local Gemma model to expand short prompts into richer ones (text-to-image only).",
        )
        EXTRA_PASS: bool = Field(
            default=False, description="Add a second refinement pass at 2x for extra detail (slower)."
        )
        STEPS: int = Field(default=8, description="Sampling steps. Krea 2 Turbo is distilled for 8.")
        EDIT_QUALITY: str = Field(
            default="best",
            description="Instruction-edit quality: 'best' (~2 min, full model, most realistic/blended), "
                        "'balanced' (~40 s, 8-step), or 'fast' (~30 s, 4-step, can look a bit CGI).",
        )
        EDIT_DENOISE: float = Field(
            default=0.30,
            description="(legacy, unused — edits now use Qwen-Image-Edit, not img2img).",
        )
        WIDTH: int = Field(default=1024, description="Default image width for text-to-image.")
        HEIGHT: int = Field(default=1024, description="Default image height for text-to-image.")

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.enhancer_model = "gemma4:31b"
        self._recent = {}  # chat_id -> last produced image b64 (for follow-up edits without re-upload)
        # Krea 2 (text-to-image) model files
        self.unet = "krea2/krea2_turbo_fp8_scaled.safetensors"
        self.clip = "qwen3vl_4b_fp8_scaled.safetensors"
        self.vae = "qwen_image_vae.safetensors"
        # Qwen-Image-Edit 2509 (instruction editing when a reference image is attached).
        # GGUF Q4_K_M so it fits the 24GB card with headroom (fp8 OOM'd on LoRA patching).
        self.edit_unet = "Qwen-Image-Edit-2509-Q4_K_M.gguf"
        self.edit_lora = "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"  # 4-step speedup
        self.edit_clip = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
        self.edit_vae = "qwen_image_vae.safetensors"  # shared with Krea 2

    def pipes(self):
        return [{"id": "krea2", "name": "Image"}]

    # ---------- content parsing ----------
    def _parse(self, messages):
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

    # ---------- conversational continuity ----------
    def _chat_id(self, body):
        return str(body.get("chat_id") or (body.get("metadata") or {}).get("chat_id") or "default")

    def _extract_b64(self, s):
        if not isinstance(s, str):
            return None
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", s)
        return m.group(1) if m else None

    def _find_recent_image(self, messages):
        """Most recent image in the chat — a user upload or a previously generated image embedded in an
        assistant reply (markdown data URI) — so follow-up edits don't need the image re-attached."""
        for m in reversed(messages or []):
            role, c = m.get("role"), m.get("content", "")
            if role == "user" and isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            return url.split(",", 1)[1]
            elif role == "assistant" and isinstance(c, str):
                b = self._extract_b64(c)
                if b:
                    return b
        return None

    def _is_image_request(self, t):
        t = t.lower()
        if re.search(r"\b(draw|sketch|paint|illustrate)\b", t):
            return True
        if re.search(r"\b(create|generate|make|design|produce|render|show me|give me|i want|new (?:image|picture))\b.{0,30}\b(image|images|picture|pictures|photo|photos|pic|drawing|painting|illustration|art|artwork|logo|wallpaper|portrait|scene|poster)\b", t):
            return True
        if re.search(r"\b(image|picture|photo|portrait|drawing|painting|wallpaper|logo) of\b", t):
            return True
        return False

    def _is_edit_request(self, t):
        t = t.lower()
        return bool(re.search(
            r"\b(edit|change|replace|remove|delete|erase|swap|add|put|turn|make|give|"
            r"recolou?r|colou?r|adjust|retouch|modify|update|fix|crop|rotate|blur|bigger|smaller|"
            r"larger|zoom|brighter|darker|more|less|get rid of|without|instead of|into a|to a)\b", t))

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        return requests.post(
            f"{self.comfy}/upload/image",
            files={"image": ("ref.png", raw, "image/png")},
            timeout=60,
        ).json()["name"]

    def _free_vram(self):
        """Unload Ollama models and BLOCK until the GPU VRAM is actually released.

        Ollama's unload is asynchronous, so we must wait — otherwise the ~18GB Krea 2 load
        can collide with a still-resident 22GB Gemma on the 24GB card and OOM.
        """
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(
                    f"{self.ollama}/api/generate",
                    json={"model": m["name"], "keep_alive": 0},
                    timeout=20,
                )
        except Exception:
            pass
        for _ in range(30):  # wait up to ~30s for /api/ps to report no loaded models
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)  # grace for the CUDA allocator to hand memory back to the driver

    def _enhance(self, prompt: str) -> str:
        """Expand a short idea into a vivid image prompt using the local LLM. Falls back to the original."""
        sys = (
            "You are a prompt engineer for the Krea 2 photorealistic image model. Rewrite the user's idea "
            "as ONE vivid, richly detailed image prompt: subject, setting, lighting, mood, composition, "
            "style, and camera/lens where useful. Keep it under 70 words. Do not add people or text that "
            "were not implied. Output ONLY the prompt text — no preamble, no quotes, no lists."
        )
        try:
            r = requests.post(
                f"{self.ollama}/api/generate",
                json={
                    "model": self.enhancer_model,
                    "system": sys,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,  # gemma4 is a thinking model; disable so it answers directly
                    "keep_alive": 0,  # unload right after so VRAM frees for the image model
                    "options": {"temperature": 0.7, "num_predict": 220},
                },
                timeout=180,
            )
            out = (r.json().get("response") or "").strip().strip('"')
            return out or prompt
        except Exception:
            return prompt

    # ---------- workflow ----------
    def _build_wf(self, prompt: str, ref_name, seed: int):
        v = self.valves
        wf = {
            "u": {"class_type": "UNETLoader", "inputs": {"unet_name": self.unet, "weight_dtype": "default"}},
            "c": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.clip, "type": "krea2", "device": "default"}},
            "v": {"class_type": "VAELoader", "inputs": {"vae_name": self.vae}},
            "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["c", 0]}},
            "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
            "k": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed, "steps": v.STEPS, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0,
                    "model": ["u", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0],
                },
            },
            "d": {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
            "s": {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui_krea", "images": ["d", 0]}},
        }
        # Optional LoRA (model-only; Krea LoRAs are applied to the diffusion model)
        model_ref = ["u", 0]
        if v.LORA_FILE.strip():
            wf["lora"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": ["u", 0], "lora_name": v.LORA_FILE.strip(), "strength_model": v.LORA_STRENGTH},
            }
            model_ref = ["lora", 0]
        wf["k"]["inputs"]["model"] = model_ref

        # Latent source: img2img (gentle edit) when a reference is attached, else a blank canvas
        if ref_name:
            wf["ld"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
            wf["sc"] = {
                "class_type": "ImageScaleToTotalPixels",
                "inputs": {"image": ["ld", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1},
            }
            wf["enc"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}}
            wf["k"]["inputs"]["latent_image"] = ["enc", 0]
            wf["k"]["inputs"]["denoise"] = v.EDIT_DENOISE
        else:
            wf["lat"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": v.WIDTH, "height": v.HEIGHT, "batch_size": 1}}

        # Optional extra refinement pass (2x hi-res fix). Skipped in edit mode to keep edits gentle.
        if v.EXTRA_PASS and not ref_name:
            wf["up"] = {
                "class_type": "ImageScaleToTotalPixels",
                "inputs": {"image": ["d", 0], "upscale_method": "lanczos", "megapixels": 2.0, "resolution_steps": 1},
            }
            wf["enc2"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["up", 0], "vae": ["v", 0]}}
            wf["k2"] = {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed, "steps": v.STEPS, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 0.4,
                    "model": model_ref, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["enc2", 0],
                },
            }
            wf["d2"] = {"class_type": "VAEDecode", "inputs": {"samples": ["k2", 0], "vae": ["v", 0]}}
            wf["s"]["inputs"]["images"] = ["d2", 0]
        return wf

    def _edit_instruction(self, text):
        """Strip a 'can you edit the picture and …' wrapper, leaving the actual instruction."""
        t = (text or "").strip()
        t = re.sub(r"^\s*(please\s+)?(can|could|would|will)\s+you\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*please\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*(edit|change|modify|update|fix|retouch|adjust)\s+(this|the|my)\s+(image|picture|photo|pic)\s*(and|to|so(?:\s+that)?|by|:|,)?\s*", "", t, flags=re.I)
        t = re.sub(r"^\s*(in|on|for)\s+(this|the)\s+(image|picture|photo|pic)[,:]?\s*", "", t, flags=re.I)
        return t.strip() or (text or "").strip()

    # Edit quality presets. 'best' drops the speed LoRA and runs the full model → most realistic /
    # best-blended result (new elements match the scene's lighting & grain); slower.
    EDIT_QUALITY = {
        "best":     {"lightning": False, "steps": 20, "cfg": 4.0},
        "balanced": {"lightning": True,  "steps": 8,  "cfg": 1.0},
        "fast":     {"lightning": True,  "steps": 4,  "cfg": 1.0},
    }

    def _build_edit_wf(self, instruction: str, ref_name: str, seed: int, quality: str):
        """Qwen-Image-Edit 2509: follow a text INSTRUCTION on the attached image, changing only what's
        asked and keeping the rest identical. A true instruction editor (img2img cannot do this)."""
        q = self.EDIT_QUALITY.get(quality, self.EDIT_QUALITY["best"])
        wf = {
            "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.edit_unet}},
            "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
            "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.edit_clip, "type": "qwen_image", "device": "default"}},
            "v":    {"class_type": "VAELoader", "inputs": {"vae_name": self.edit_vae}},
            "ld":   {"class_type": "LoadImage", "inputs": {"image": ref_name}},
            "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
            "pos":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": instruction}},
            "neg":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": ""}},
            "enc":  {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}},
            "k":    {"class_type": "KSampler", "inputs": {"seed": seed, "steps": q["steps"], "cfg": q["cfg"], "sampler_name": "euler",
                        "scheduler": "simple", "denoise": 1.0, "model": ["cfgn", 0], "positive": ["pos", 0],
                        "negative": ["neg", 0], "latent_image": ["enc", 0]}},
            "d":    {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
            "s":    {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui_edit", "images": ["d", 0]}},
        }
        if q["lightning"]:  # insert the 4-step speed LoRA between the model and ModelSampling
            wf["lora"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["u", 0], "lora_name": self.edit_lora, "strength_model": 1.0}}
            wf["msaf"]["inputs"]["model"] = ["lora", 0]
        return wf

    def _generate(self, text: str, ref_b64):
        v = self.valves
        editing = ref_b64 is not None
        self._free_vram()

        if editing:
            # Attached image → instruction edit with Qwen-Image-Edit (changes only what you asked).
            try:
                ref_name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            instruction = self._edit_instruction(text) if text else "improve the overall quality, keep everything else the same"
            wf = self._build_edit_wf(instruction, ref_name, random.randint(0, 2**31), v.EDIT_QUALITY)
            cap = instruction[:60]
        else:
            # No image → fresh text-to-image with Krea 2 Turbo (+ enhancer / optional LoRA).
            prompt = text or "high quality, highly detailed, sharp focus"
            if v.ENHANCE and text:
                prompt = self._enhance(prompt)
            if v.LORA_FILE.strip() and v.TRIGGER.strip():
                prompt = f"{v.TRIGGER.strip()}, {prompt}"
            wf = self._build_wf(prompt, None, random.randint(0, 2**31))
            cap = (text or prompt)[:60]

        try:
            pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
        except Exception as e:
            return f"⚠️ Image backend unreachable: {e}"
        for _ in range(900):
            try:
                h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
            except Exception:
                time.sleep(1); continue
            if pid in h:
                imgs = h[pid].get("outputs", {}).get("s", {}).get("images", [])
                if not imgs:
                    return "Job finished but no image was produced."
                im = imgs[0]
                data = requests.get(
                    f"{self.comfy}/view",
                    params={"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": "output"},
                    timeout=60,
                ).content
                return f"![{cap}](data:image/png;base64,{base64.b64encode(data).decode()})"
            time.sleep(1)
        return "⏳ Timed out waiting for the image."

    async def pipe(self, body: dict):
        msgs = body.get("messages", [])
        text, ref = self._parse(msgs)
        if not text and not ref:
            return "Type what you'd like me to draw — or attach an image and describe a change (e.g. 'remove the glasses')."
        cid = self._chat_id(body)
        # Conversational editing: a follow-up with no new upload edits the most recent image from this
        # chat (previously generated or uploaded) — no re-attach needed. A fresh "create/draw a …"
        # request still starts a new image. (This model always outputs an image, so any non-generate
        # follow-up is an edit — robust to phrasing like "have them use chopsticks".)
        if text and not ref and not self._is_image_request(text):
            ref = self._find_recent_image(msgs) or self._recent.get(cid)
        result = await asyncio.to_thread(self._generate, text, ref)
        b64 = self._extract_b64(result)
        if b64:  # remember it so the next "make it bigger" can edit it
            self._recent[cid] = b64
            if len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
        return result

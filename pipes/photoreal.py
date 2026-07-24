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


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.model = "lustifySDXL.safetensors"

    def pipes(self):
        return [{"id": "photo", "name": "Photoreal"}]

    # ---- helpers ----
    def _parse(self, messages):
        """Return (text_prompt, reference_image_base64_or_None) from the last user message."""
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

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        r = requests.post(f"{self.comfy}/upload/image",
                          files={"image": ("ref.png", raw, "image/png")}, timeout=60)
        return r.json()["name"]

    def _free_vram(self):
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
        except Exception:
            pass

    def _generate(self, prompt: str, ref_b64):
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
        try:
            pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
        except Exception as e:
            return f"⚠️ Image backend unreachable: {e}"
        for _ in range(300):
            try:
                h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
            except Exception:
                time.sleep(1); continue
            if pid in h:
                imgs = h[pid].get("outputs", {}).get("9", {}).get("images", [])
                if not imgs:
                    return f"Job finished but produced no image: {str(h[pid].get('status',{}))[:200]}"
                im = imgs[0]
                data = requests.get(f"{self.comfy}/view",
                    params={"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": "output"},
                    timeout=30).content
                return f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()})"
            time.sleep(1)
        return "⏳ Timed out waiting for the image."

    async def pipe(self, body: dict):
        text, ref = self._parse(body.get("messages", []))
        if not text and not ref:
            return "Type what you'd like me to create (and optionally attach a reference image)."
        if not text:
            text = "photorealistic, highly detailed, sharp focus, natural lighting"
        return await asyncio.to_thread(self._generate, text, ref)

"""
title: Assistant (auto)
author: local
version: 0.4.0
required_open_webui_version: 0.5.0
description: One model that decides - chats (with vision), makes a Flux image (text or image-to-image), or a Wan video. Non-blocking (async). Never uses the uncensored model.
"""
import asyncio, aiohttp, requests, time, base64, random, re, json

V_W, V_H, V_LEN, V_STEPS, V_FPS = 832, 480, 49, 20, 16
IMG_DENOISE = 0.80


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.chat_model = "gemma4:31b"

    def pipes(self):
        return [{"id": "auto", "name": "🪄 Assistant (auto chat + image + video)"}]

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
                out.append({"role": role, "content": c})
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
    def _is_video_request(self, t):
        t = t.lower()
        if re.search(r"\b(video|animate|animated|animation|\bclip\b|\bgif\b|footage|moving image|make it move|bring .* to life)\b", t):
            return True
        if re.search(r"\b(make|create|generate|render|produce|show)\b.{0,25}\b(clip|video|animation|gif)\b", t):
            return True
        return False

    def _is_image_request(self, t):
        t = t.lower()
        if re.search(r"\b(draw|sketch|paint|illustrate)\b", t):
            return True
        if re.search(r"\b(create|creating|generate|make|design|produce|render|show me|give me|i want|can you make|could you make)\b.{0,30}\b(image|images|picture|pictures|photo|photos|pic|drawing|painting|illustration|art|artwork|logo|wallpaper|portrait|render|scene|poster)\b", t):
            return True
        if re.search(r"\b(image|picture|photo|portrait|drawing|painting|wallpaper|logo) of\b", t):
            return True
        return False

    def _clean_prompt(self, text):
        t = re.sub(r"^\s*(please\s+)?(can you\s+|could you\s+|i want you to\s+|i'?d like\s+(you to\s+)?)?", "", text.strip(), flags=re.I)
        t = re.sub(r"^\s*(create|generate|make|draw|sketch|paint|illustrate|design|render|produce|animate|show me|give me)\s+(a|an|the|some|me)?\s*(image|picture|photo|pic|drawing|painting|illustration|art|artwork|render|video|clip|animation|gif|footage)?\s*(of|showing|with|:)?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        return requests.post(f"{self.comfy}/upload/image",
                             files={"image": ("ref.png", raw, "image/png")}, timeout=60).json()["name"]

    def _free_vram(self):
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
        except Exception:
            pass

    def _submit_poll(self, wf, out_node, kind, iters):
        try:
            pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
        except Exception as e:
            return None, f"⚠️ {kind} backend error: {e}"
        for _ in range(iters):
            try:
                h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
            except Exception:
                time.sleep(2); continue
            if pid in h:
                items = h[pid].get("outputs", {}).get(out_node, {}).get("images", [])
                if not items:
                    return None, f"{kind} finished but produced no output."
                it = items[0]
                data = requests.get(f"{self.comfy}/view",
                    params={"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": "output"},
                    timeout=60).content
                return data, None
            time.sleep(2)
        return None, f"⏳ Timed out waiting for the {kind.lower()}."

    def _gen_image(self, prompt, ref_b64):
        self._free_vram()
        wf = {
          "4":  {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "flux1-dev-fp8.safetensors"}},
          "6":  {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
          "7":  {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
          "10": {"class_type": "FluxGuidance", "inputs": {"guidance": 3.5, "conditioning": ["6", 0]}},
          "3":  {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": 20, "cfg": 1,
                    "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
                    "model": ["4", 0], "positive": ["10", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
          "8":  {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
          "9":  {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if ref_b64:
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload reference image: {e}"
            wf["20"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            wf["21"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["20", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1}}
            wf["22"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["4", 2]}}
            wf["3"]["inputs"]["latent_image"] = ["22", 0]
            wf["3"]["inputs"]["denoise"] = IMG_DENOISE
        else:
            wf["5"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}}
        data, err = self._submit_poll(wf, "9", "Image", 360)
        return err or f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()})"

    def _gen_video(self, prompt):
        self._free_vram()
        wf = {
          "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.1_t2v_14B_fp8_e4m3fn.safetensors", "weight_dtype": "default"}},
          "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan"}},
          "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
          "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
          "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality, distorted, watermark, text, static", "clip": ["2", 0]}},
          "6": {"class_type": "WanImageToVideo", "inputs": {"positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0], "width": V_W, "height": V_H, "length": V_LEN, "batch_size": 1}},
          "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
          "8": {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": V_STEPS, "cfg": 6.0,
                    "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                    "model": ["7", 0], "positive": ["6", 0], "negative": ["6", 1], "latent_image": ["6", 2]}},
          "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
          "10": {"class_type": "SaveWEBM", "inputs": {"images": ["9", 0], "filename_prefix": "owui_vid", "codec": "vp9", "fps": float(V_FPS), "crf": 32.0}},
        }
        data, err = self._submit_poll(wf, "10", "Video", 800)
        if err:
            return err
        b64 = base64.b64encode(data).decode()
        return (f'<video controls loop muted playsinline style="max-width:100%;border-radius:8px">'
                f'<source src="data:video/webm;base64,{b64}" type="video/webm"></video>\n\n*🎬 {prompt[:80]}*')

    async def _achat_stream(self, messages):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
                async with s.post(f"{self.ollama}/api/chat",
                                  json={"model": self.chat_model, "messages": messages, "stream": True}) as r:
                    async for line in r.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        tok = (d.get("message") or {}).get("content", "")
                        if tok:
                            yield tok
        except Exception as e:
            yield f"⚠️ Chat backend error: {e}"

    async def pipe(self, body: dict):
        msgs = body.get("messages", [])
        text, ref = self._last_user(msgs)
        if text and self._is_video_request(text):
            return await asyncio.to_thread(self._gen_video, self._clean_prompt(text))
        if text and self._is_image_request(text):
            prompt = text if ref else self._clean_prompt(text)
            return await asyncio.to_thread(self._gen_image, prompt, ref)
        # chat (with vision if an image is attached and Gemma supports it)
        return self._achat_stream(self._ollama_messages(msgs))

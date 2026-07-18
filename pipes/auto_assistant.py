"""
title: Assistant (auto)
author: local
version: 0.5.0
required_open_webui_version: 0.5.0
description: One model that decides - chats (with vision), makes a Krea 2 image (text or gentle image-to-image edit), or a Wan video. Non-blocking (async). Never uses the uncensored model.
"""
import asyncio, aiohttp, requests, time, base64, random, re, json

V_W, V_H, V_LEN, V_STEPS, V_FPS = 832, 480, 49, 20, 24  # Wan 2.2 TI2V 5B, ~2 s 480p clip
IMG_DENOISE = 0.30  # gentle Krea 2 img2img edit strength (lower = closer to the attached image)
IMG_ENHANCE = True  # expand short prompts into richer ones via the local LLM (text-to-image only)


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.chat_model = "gemma4:31b"
        self._recent = {}  # chat_id -> last produced image b64 (for follow-up edits without re-upload)

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

    def _is_edit_request(self, t):
        """Edit intent for an image: change/remove/add/replace/bigger/etc.
        (Pure questions about the image have none of these verbs → they go to vision chat.)"""
        t = t.lower()
        return bool(re.search(
            r"\b(edit|change|replace|remove|delete|erase|swap|add|put|turn|make|give|"
            r"recolou?r|colou?r|adjust|retouch|modify|update|fix|crop|rotate|blur|bigger|smaller|"
            r"larger|zoom|brighter|darker|more|less|get rid of|without|instead of|into a|to a)\b", t))

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

    def _scrub(self, text):
        """Strip embedded image/video data URIs so we don't feed megabytes of base64 to the chat model."""
        if not isinstance(text, str):
            return text
        text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "[generated image]", text)
        text = re.sub(r"<video[^>]*>.*?</video>", "[generated video]", text, flags=re.S)
        text = re.sub(r"data:(?:image|video)/[^;]+;base64,[A-Za-z0-9+/=]+", "[media]", text)
        return text

    def _wants_edit(self, text):
        """Given an image already exists in the chat, decide if this message asks to MODIFY it.
        Default is YES (talking about a just-made image = editing it); only clear questions or
        small-talk fall through to chat. Robust to arbitrary phrasing ('have them use chopsticks')."""
        core = re.sub(r"^\s*(please|hey|okay?|so|and|then|now|also)[,\s]+", "", text.strip(), flags=re.I)
        core = re.sub(r"^\s*(can|could|would|will)\s+(you\s+)?(please\s+|maybe\s+)?", "", core, flags=re.I).strip()
        low = core.lower()
        if self._is_edit_request(low):
            return True
        # small talk / acknowledgment → chat
        if re.match(r"^(nice|cool|thanks|thank|great|awesome|perfect|love|lovely|beautiful|amazing|"
                    r"good|ok|okay|lol+|haha+|wow|hmm+|nvm|never\s?mind|yes|yeah|yep|no|nope|sure)\b", low):
            return False
        # question / info request → chat (unless it also contains an edit verb, handled above)
        if low.endswith("?") or re.match(
                r"^(what|why|how|who|whom|whose|where|when|which|is|are|was|were|do|does|did|have\s+you|"
                r"tell\s+me|explain|describe|list|suggest|recommend|caption|analy[sz]e|identify|read|translate|"
                r"i\s+(think|feel|wonder|like|love))\b", low):
            return False
        return True  # an imperative / description of a change → edit

    def _edit_instruction(self, text):
        """Strip a 'can you edit the picture and …' wrapper, leaving the actual instruction for the editor."""
        t = text.strip()
        t = re.sub(r"^\s*(please\s+)?(can|could|would|will)\s+you\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*please\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*(edit|change|modify|update|fix|retouch|adjust)\s+(this|the|my)\s+(image|picture|photo|pic)\s*(and|to|so(?:\s+that)?|by|:|,)?\s*", "", t, flags=re.I)
        t = re.sub(r"^\s*(in|on|for)\s+(this|the)\s+(image|picture|photo|pic)[,:]?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    def _clean_prompt(self, text):
        t = re.sub(r"^\s*(please\s+)?(can you\s+|could you\s+|i want you to\s+|i'?d like\s+(you to\s+)?)?", "", text.strip(), flags=re.I)
        t = re.sub(r"^\s*(create|generate|make|draw|sketch|paint|illustrate|design|render|produce|animate|show me|give me)\s+(a|an|the|some|me)?\s*(image|picture|photo|pic|drawing|painting|illustration|art|artwork|render|video|clip|animation|gif|footage)?\s*(of|showing|with|:)?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        return requests.post(f"{self.comfy}/upload/image",
                             files={"image": ("ref.png", raw, "image/png")}, timeout=60).json()["name"]

    def _free_vram(self):
        """Unload Ollama models and BLOCK until GPU VRAM is actually released (unload is async).
        Prevents a 22GB Gemma + 18GB Krea 2 collision on the 24GB card."""
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
        except Exception:
            pass
        for _ in range(30):  # wait up to ~30s until no models are loaded
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)  # grace for the CUDA allocator to release

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

    def _enhance(self, prompt):
        """Expand a short idea into a vivid image prompt via the local LLM. Falls back to the original."""
        sys = (
            "You are a prompt engineer for the Krea 2 photorealistic image model. Rewrite the user's idea "
            "as ONE vivid, richly detailed image prompt: subject, setting, lighting, mood, composition, "
            "style, and camera/lens where useful. Keep it under 70 words. Do not add people or text that "
            "were not implied. Output ONLY the prompt text — no preamble, no quotes, no lists."
        )
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 220}}, timeout=180)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

    def _gen_image(self, prompt, ref_b64):
        # Attached image → instruction edit with Qwen-Image-Edit 2509 (Lightning 4-step).
        if ref_b64:
            self._free_vram()
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            instruction = prompt or "improve the overall quality, keep everything else the same"
            # Full-quality edit (no 4-step speed LoRA): 20 steps, cfg 4 → new elements blend into the
            # scene's lighting/grain instead of looking pasted-on. ~2 min. (Fast path lives in the
            # dedicated "Image" model's EDIT_QUALITY valve.)
            wf = {
              "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "Qwen-Image-Edit-2509-Q4_K_M.gguf"}},
              "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
              "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
              "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
              "v":    {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
              "ld":   {"class_type": "LoadImage", "inputs": {"image": name}},
              "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
              "pos":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": instruction}},
              "neg":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": ""}},
              "enc":  {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}},
              "k":    {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": 20, "cfg": 4.0,
                          "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                          "model": ["cfgn", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["enc", 0]}},
              "d":    {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
              "s":    {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui_edit", "images": ["d", 0]}},
            }
            data, err = self._submit_poll(wf, "s", "Edit", 1200)
            return err or f"![{instruction[:50]}](data:image/png;base64,{base64.b64encode(data).decode()})"

        # No image → fresh Krea 2 Turbo text-to-image (8-step; negative is zeroed conditioning, cfg 1).
        if IMG_ENHANCE and prompt:
            prompt = self._enhance(prompt)
        self._free_vram()
        wf = {
          "u":   {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2/krea2_turbo_fp8_scaled.safetensors", "weight_dtype": "default"}},
          "c":   {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
          "v":   {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
          "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["c", 0]}},
          "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
          "3":   {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": 8, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0,
                    "model": ["u", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["5", 0]}},
          "5":   {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
          "8":   {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["v", 0]}},
          "9":   {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        data, err = self._submit_poll(wf, "9", "Image", 360)
        return err or f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()})"

    def _gen_video(self, prompt):
        self._free_vram()
        # Wan 2.2 TI2V 5B (text-to-video). Standard Wan negative prompt (recommended by the model).
        neg = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
               "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
               "形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")
        wf = {
          "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
          "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
          "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
          "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["2", 0]}},
          "6": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": V_W, "height": V_H, "length": V_LEN, "batch_size": 1}},
          "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
          "8": {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": V_STEPS, "cfg": 5.0,
                    "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                    "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
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
        # Guard: keep Gemma from inventing "dalle"/tool-call JSON — image work is routed automatically.
        guard = {"role": "system", "content": (
            "You are a friendly, concise assistant in a chat app. Image generation and editing are "
            "handled automatically by the app, not by you. Always reply in plain, natural language. "
            "NEVER output JSON, tool calls, function calls, or an \"action\"/\"dalle\"/\"text2im\" object. "
            "If the user asks to create or edit a picture, just acknowledge briefly in words.")}
        messages = [guard] + [m for m in messages if m.get("role") != "system"]
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

    async def _gen_and_cache(self, cid, prompt, ref):
        result = await asyncio.to_thread(self._gen_image, prompt, ref)
        b64 = self._extract_b64(result)
        if b64:  # remember the produced image so a later "make it bigger" can edit it
            self._recent[cid] = b64
            if len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
        return result

    async def pipe(self, body: dict):
        msgs = body.get("messages", [])
        text, ref = self._last_user(msgs)
        cid = self._chat_id(body)
        if text and self._is_video_request(text):
            return await asyncio.to_thread(self._gen_video, self._clean_prompt(text))
        # Fresh image generation ("create/draw a …") → a brand-new image, even mid-conversation.
        if text and not ref and self._is_image_request(text):
            return await self._gen_and_cache(cid, self._clean_prompt(text), None)
        # Editing: an attached image, OR a follow-up about the most recent image in this chat
        # (previously generated/uploaded) — no re-upload needed. Any non-question/non-smalltalk
        # message here is treated as an edit instruction.
        img = ref or self._find_recent_image(msgs) or self._recent.get(cid)
        if text and img and self._wants_edit(text):
            return await self._gen_and_cache(cid, self._edit_instruction(text), img)
        # chat (with vision if an image is attached and Gemma supports it)
        return self._achat_stream(self._ollama_messages(msgs))

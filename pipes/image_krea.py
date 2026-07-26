"""
title: Krea 2 Image
author: local
version: 1.2.0
required_open_webui_version: 0.5.0
description: Photoreal images with Krea 2 Turbo via local ComfyUI. Optional trained LoRA, a local prompt-enhancer (Gemma), an optional extra refinement pass, and instruction edits via Qwen-Image-Edit (vague follow-ups are rewritten by Gemma into explicit instructions the editor actually follows). Non-blocking (async); auto-frees GPU VRAM.
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
        EDIT_REWRITE: bool = Field(
            default=True,
            description="Use the local Gemma model to rewrite vague edit requests ('make him a bit older') "
                        "into explicit instructions the editor follows much better, plus a negative prompt.",
        )
        VERIFY: bool = Field(
            default=True,
            description="After generating, check the image against your request with the local vision model "
                        "and run up to two correction rounds if it doesn't match (adds ~30-60 s, more when "
                        "a retry triggers).",
        )
        WIDTH: int = Field(default=1024, description="Default image width for text-to-image.")
        HEIGHT: int = Field(default=1024, description="Default image height for text-to-image.")

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        # Mirrors the Assistant pipe, which now uses ONE tenant for everything. The old split
        # (dolphin for text, gemma4:31b for the image QA check) existed to stop a pure text task
        # evicting a resident model to load the 19.9 GB vision model — with a single multimodal
        # tenant there is nothing left to evict, so the split has no purpose.
        #
        # Both point at the same tag on purpose; kept as two attributes so re-splitting later is a
        # one-line change rather than a refactor.
        self.text_model = "hermes-genesis:apex-compact"
        self.vision_model = "hermes-genesis:apex-compact"
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

    # Small-talk / question guards so a 'thanks!' or 'what's in this?' after an image doesn't get
    # silently turned into a ~2-minute Qwen-Image-Edit job (this pipe has no chat/vision path).
    _SMALLTALK = re.compile(
        r"^(nice|cool|thanks|thank|great|awesome|perfect|love|lovely|beautiful|amazing|good|ok|okay|"
        r"k|lol+|haha+|wow|hmm+|nvm|never\s?mind|yes|yeah|yep|yup|no|nope|nah|sure|cheers|wonderful|"
        r"gorgeous|stunning|fantastic|excellent|brilliant)\b", re.I)
    _QUESTION = re.compile(
        r"^(what'?s?|why|how|who|whom|whose|where|when|which|is|are|am|was|were|do|does|did|can|could|"
        r"would|should|will|have|has|had|may|might|tell\s+me|explain|describe|list|suggest|recommend|"
        r"caption|analy[sz]e|identify|read|translate|compare|define|summari[sz]e|write|compose|draft)\b", re.I)

    def _is_smalltalk(self, t):
        return bool(self._SMALLTALK.match((t or "").strip()))

    def _is_question(self, t):
        t = (t or "").strip()
        return t.endswith("?") or bool(self._QUESTION.match(t))

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
            r = requests.post(
                f"{self.ollama}/api/generate",
                json={
                    "model": self.text_model,  # dolphin (text-only) — no vision model needed to expand a prompt
                    "system": sys,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,  # answer directly, no reasoning preamble
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
    def _build_wf(self, prompt: str, seed: int):
        # Text-to-image only. (Instruction edits go through _build_edit_wf with Qwen-Image-Edit;
        # the old Krea img2img reference path was retired, so there is no ref_name branch here.)
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

        # Blank-canvas latent (text-to-image).
        wf["lat"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": v.WIDTH, "height": v.HEIGHT, "batch_size": 1}}

        # Optional extra refinement pass (2x hi-res fix).
        if v.EXTRA_PASS:
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
        """Compact 'user:/assistant:' transcript (media stripped) so the rewriter can resolve
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
            t = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "[generated image]", t)
            t = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[image]", t)
            t = t.strip()
            if t:
                lines.append(f"{role}: {t[:300]}")
        return "\n".join(lines)

    def _comfy_free(self):
        """Ask ComfyUI to unload its models so the 22GB vision model can load for the QA check."""
        try:
            requests.post(f"{self.comfy}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except Exception:
            pass
        time.sleep(2)

    _VERIFY_SYS = (
        "You are a strict image QA checker. You get a user's request and the generated image. Check ONLY "
        "hard requirements: person count, each person's apparent age bracket, gender, ethnicity, named "
        "objects/actions/setting — including the key object a named activity implies ('playing basketball' "
        "requires a visible basketball, 'having dinner' requires food on the table) — and, ONLY when the "
        "request names an art style or medium ('animated picture', 'cartoon', 'anime', 'watercolor' …), "
        "that the image is rendered in that style (a photorealistic photo when an animated/cartoon style "
        "was asked for is a FAIL). When no style is named, ignore style, lighting and quality. Reply with "
        "EXACTLY two lines:\n"
        "OK: yes or no\n"
        "FIX: if no — ONE concrete imperative sentence stating what to correct, naming each subject "
        "precisely by appearance and position in the image (e.g. 'Remove the boy in the blue shirt, "
        "second from the right'; 'Redraw the whole scene as a 3D animated cartoon'); if yes — the word none"
    )

    def _verify_image(self, request_text, img_b64):
        """(ok, fix) — Gemma-vision compares the produced image to what was asked. Fails open."""
        try:
            r = requests.post(
                f"{self.ollama}/api/generate",
                json={"model": self.vision_model, "system": self._VERIFY_SYS,
                      "prompt": f"Request: {request_text}\nDoes the image satisfy every hard requirement?",
                      "images": [img_b64], "stream": False, "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.1, "num_predict": 150}},
                timeout=300,
            )
            out = (r.json().get("response") or "").strip()
            ok = not re.search(r"^\s*OK:\s*no\b", out, re.I | re.M)
            m = re.search(r"^\s*FIX:\s*(.+)$", out, re.I | re.M)
            fix = (m.group(1).strip() if m else "")
            return ok, ("" if fix.lower().startswith("none") else fix)
        except Exception:
            return True, ""

    def _enhance_edit(self, instruction, msgs):
        """(explicit_instruction, negative) via the local LLM. Vague relative asks ('a bit older')
        under-move the identity-preserving editor; explicit absolute target states move it properly.
        Falls back to the original instruction and no negative."""
        ctx = self._edit_context(msgs)
        prompt = (f"Conversation:\n{ctx}\n\nRewrite the last user request."
                  if ctx else f"Request: {instruction}\n\nRewrite this request.")
        try:
            r = requests.post(
                f"{self.ollama}/api/generate",
                json={"model": self.text_model, "system": self._EDIT_REWRITE_SYS, "prompt": prompt,
                      "stream": False, "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.4, "num_predict": 220}},
                timeout=180,
            )
            out = (r.json().get("response") or "").strip()
            edit = re.search(r"^\s*EDIT:\s*(.+)$", out, re.I | re.M)
            avoid = re.search(r"^\s*AVOID:\s*(.+)$", out, re.I | re.M)
            if edit:
                return edit.group(1).strip(), (avoid.group(1).strip() if avoid else "")
        except Exception:
            pass
        return instruction, ""

    # Edit quality presets. 'best' drops the speed LoRA and runs the full model → most realistic /
    # best-blended result (new elements match the scene's lighting & grain); slower.
    EDIT_QUALITY = {
        "best":     {"lightning": False, "steps": 20, "cfg": 4.0},
        "balanced": {"lightning": True,  "steps": 8,  "cfg": 1.0},
        "fast":     {"lightning": True,  "steps": 4,  "cfg": 1.0},
    }

    def _build_edit_wf(self, instruction: str, ref_name: str, seed: int, quality: str,
                       negative: str = "", boost: bool = False):
        """Qwen-Image-Edit 2509: follow a text INSTRUCTION on the attached image, changing only what's
        asked and keeping the rest identical. A true instruction editor (img2img cannot do this).
        negative: traits that must NOT appear (only effective when cfg > 1, i.e. 'best').
        boost: raise cfg/steps when the user says the last edit under-delivered."""
        q = dict(self.EDIT_QUALITY.get(quality, self.EDIT_QUALITY["best"]))
        if boost and q["cfg"] > 1.0:
            q["cfg"], q["steps"] = 6.0, q["steps"] + 4
        wf = {
            "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.edit_unet}},
            "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
            "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.edit_clip, "type": "qwen_image", "device": "default"}},
            "v":    {"class_type": "VAELoader", "inputs": {"vae_name": self.edit_vae}},
            "ld":   {"class_type": "LoadImage", "inputs": {"image": ref_name}},
            "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
            "pos":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": instruction}},
            "neg":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": negative}},
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

    def _generate(self, text: str, ref_b64, msgs=None):
        v = self.valves
        editing = ref_b64 is not None

        # LLM prompt work happens BEFORE _free_vram so Gemma isn't unloaded and reloaded.
        if editing:
            # Attached image → instruction edit with Qwen-Image-Edit (changes only what you asked).
            instruction = self._edit_instruction(text) if text else "improve the overall quality, keep everything else the same"
            negative = ""
            if v.EDIT_REWRITE and text:
                instruction, negative = self._enhance_edit(instruction, msgs)
            self._free_vram()
            try:
                ref_name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            wf = self._build_edit_wf(instruction, ref_name, random.randint(0, 2**31), v.EDIT_QUALITY,
                                     negative=negative, boost=self._edit_boost(text))
            cap = instruction[:60]
        else:
            # No image → fresh text-to-image with Krea 2 Turbo (+ enhancer / optional LoRA).
            prompt = text or "high quality, highly detailed, sharp focus"
            if v.ENHANCE and text:
                prompt = self._enhance(prompt)
            if v.LORA_FILE.strip() and v.TRIGGER.strip():
                prompt = f"{v.TRIGGER.strip()}, {prompt}"
            self._free_vram()
            wf = self._build_wf(prompt, random.randint(0, 2**31))
            cap = (text or prompt)[:60]

        data, err = self._run_wf(wf)
        if err:
            return err
        # Vision QA: does the result actually match what was asked? Up to two correction rounds
        # (models drop hard constraints — person counts, ages — surprisingly often, and a single
        # correction can itself overshoot, e.g. removing two kids instead of one).
        if v.VERIFY and text:
            check = instruction if editing else text  # the goal the result is judged against
            cur = instruction if editing else None    # accumulates corrections for edit retries
            for _round in (1, 2):
                self._comfy_free()
                ok, fix = self._verify_image(check, base64.b64encode(data).decode())
                if ok or not fix:
                    break
                # Correction rebuilds force 'best' quality (cfg≥4, ~24 steps) + boost regardless of the
                # EDIT_QUALITY valve — at 'balanced'/'fast' (cfg 1) the boost is skipped and the
                # negative/AVOID conditioning is mathematically inert, so a retry would re-fail identically.
                if editing:
                    cur = f"{cur} IMPORTANT correction: {fix}"
                    wf = self._build_edit_wf(cur, ref_name, random.randint(0, 2**31),
                                             "best", negative=negative, boost=True)
                else:
                    # A fresh re-roll at cfg 1 usually repeats the mistake (e.g. an extra child) —
                    # instead FIX the produced image with the instruction editor, which is precisely
                    # good at "remove the extra X / add the missing Y" and keeps the scene.
                    try:
                        fix_ref = self._upload(base64.b64encode(data).decode())
                    except Exception:
                        break
                    wf = self._build_edit_wf(f"{fix} Keep everyone else and the scene exactly the same.",
                                             fix_ref, random.randint(0, 2**31), "best", boost=True)
                self._free_vram()
                data2, err2 = self._run_wf(wf)
                if data2 is None:
                    break
                data = data2
        return f"![{cap}](data:image/png;base64,{base64.b64encode(data).decode()})"

    def _run_wf(self, wf):
        """Submit + poll one workflow. Returns (png_bytes, None) or (None, user_facing_error).
        Retries ONCE on GPU OOM: OpenWebUI can invoke the 20GB chat LLM (e.g. to generate the
        chat title on a brand-new chat) AFTER our job has started, stealing the VRAM out from
        under the sampler. Freeing again and resubmitting wins the second time."""
        err = None
        for attempt in (1, 2):
            try:
                pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
            except Exception as e:
                return None, f"⚠️ Image backend unreachable: {e}"
            err = None
            for _ in range(900):
                try:
                    h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
                except Exception:
                    time.sleep(1); continue
                if pid not in h:
                    time.sleep(1); continue
                st = h[pid].get("status", {})
                if st.get("status_str") == "error":
                    d = next((m[1] for m in st.get("messages", []) if m[0] == "execution_error"), {})
                    err = (f"{d.get('exception_type', 'Error')} in {d.get('node_type', '?')} — "
                           f"{str(d.get('exception_message', ''))[:200]}")
                    break
                imgs = h[pid].get("outputs", {}).get("s", {}).get("images", [])
                if not imgs:
                    return None, "Job finished but no image was produced."
                im = imgs[0]
                data = requests.get(
                    f"{self.comfy}/view",
                    params={"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": "output"},
                    timeout=60,
                ).content
                return data, None
            if err is None:
                return None, "⏳ Timed out waiting for the image."
            if attempt == 1 and "OutOfMemory" in err:
                self._free_vram()
                continue
            break
        return None, f"⚠️ Image generation failed: {err}"

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
        """Await the generation while emitting a live elapsed-time status. Returns (result, secs)."""
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
            # A 'thanks!'/'perfect' or a question is NOT an edit — acknowledge/redirect in one line
            # instead of running a Qwen edit on the last image.
            if self._is_smalltalk(text):
                return "🙂 Glad you like it! Tell me any change and I'll edit it, or describe a new image."
            if self._is_question(text):
                return ("I'm the image generator/editor — tell me a change to make (e.g. 'make the sky "
                        "sunset orange') or describe a new image. To chat about a picture, ask the main Assistant.")
            ref = self._find_recent_image(msgs) or self._recent.get(cid)
        editing = ref is not None
        label = "Editing image" if editing else "Generating image"
        result, el = await self._tracked(emitter, label,
                                         asyncio.to_thread(self._generate, text, ref, msgs))
        b64 = self._extract_b64(result)
        if b64:  # remember it so the next "make it bigger" can edit it (LRU)
            self._recent.pop(cid, None)
            self._recent[cid] = b64
            while len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
        v = self.valves
        detail = "Qwen-Image-Edit" if editing else f"Krea 2 · {v.WIDTH}×{v.HEIGHT}"
        return await self._finish(emitter, result, "Edited" if editing else "Generated", el, detail)

"""
title: Animate
author: local
version: 1.0.0
required_open_webui_version: 0.5.0
description: Character animation / motion transfer with SCAIL-2 via local ComfyUI. Attach a character image (full body works best) and name a motion — "dance", "wave", or "walk" — and it animates YOUR character with that motion, keeping its appearance. ~3 min/clip. Non-blocking (async); auto-frees GPU VRAM.
"""
import asyncio, requests, time, base64, random, re
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        WIDTH: int = Field(default=448, description="Output width (multiple of 32). Higher = more VRAM.")
        HEIGHT: int = Field(default=768, description="Output height (multiple of 32).")
        LENGTH: int = Field(default=49, description="Frames (must be 4n+1: 49, 81…). More = longer clip but more VRAM.")
        STEPS: int = Field(default=8, description="Sampling steps (8 with the distill LoRA).")
        DEFAULT_MOTION: str = Field(default="dance", description="Motion used when none is named: dance / wave / walk.")

    def __init__(self):
        self.valves = self.Valves()
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        # SCAIL-2 model stack (GGUF so it fits 24GB with the speed LoRA)
        self.unet = "wan2.1/wan2.1_14B_SCAIL_2-Q4_K_M.gguf"
        self.lora = "wan2.1/lightx2v_I2V_14B_480p_cfg_step_distill_rank128_bf16.safetensors"
        self.clip = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        self.vae = "Wan2_1_VAE_bf16.safetensors"
        self.clip_vision = "clip_vision_h.safetensors"
        # Reference motion clips staged in ComfyUI's input folder
        self.motions = {"dance": "motion_dance.webm", "wave": "motion_wave.webm", "walk": "motion_walk.webm"}

    def pipes(self):
        return [{"id": "scail", "name": "Animate"}]

    def _parse(self, messages):
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
        return requests.post(f"{self.comfy}/upload/image",
                             files={"image": ("character.png", raw, "image/png")}, timeout=60).json()["name"]

    def _free_vram(self):
        """Unload Ollama models and block until GPU VRAM is released (SCAIL is a big 14B model)."""
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
        except Exception:
            pass
        for _ in range(30):
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)

    def _pick_motion(self, text):
        t = (text or "").lower()
        if re.search(r"\b(wave|waving|waves|hello|hi|greet|greeting)\b", t):
            return "wave", self.motions["wave"]
        if re.search(r"\b(walk|walking|walks|march|marching|step|stepping)\b", t):
            return "walk", self.motions["walk"]
        if re.search(r"\b(dance|dancing|dances|dancer|boogie)\b", t):
            return "dance", self.motions["dance"]
        d = self.valves.DEFAULT_MOTION if self.valves.DEFAULT_MOTION in self.motions else "dance"
        return d, self.motions[d]

    def _generate(self, text, img_b64):
        if not img_b64:
            return ("Attach a **character image** (full body works best) and name a motion — "
                    "**dance**, **wave**, or **walk** — and I'll animate your character with it.")
        self._free_vram()
        try:
            char = self._upload(img_b64)
        except Exception as e:
            return f"⚠️ Could not upload the character image: {e}"
        label, motion = self._pick_motion(text)
        v = self.valves
        W, H, LEN = v.WIDTH, v.HEIGHT, v.LENGTH
        negt = "low quality, distorted body, duplicate limbs, extra limbs, deformed, blurry, jpeg artifacts, watermark, text"
        wf = {
            "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.unet}},
            "lora": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["u", 0], "lora_name": self.lora, "strength_model": 1.0}},
            "msaf": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["lora", 0], "shift": 5.0}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.clip, "type": "wan", "device": "default"}},
            "vae":  {"class_type": "VAELoader", "inputs": {"vae_name": self.vae}},
            "cvl":  {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": self.clip_vision}},
            "img":  {"class_type": "LoadImage", "inputs": {"image": char}},
            "rc":   {"class_type": "PixaromaResizeCrop", "inputs": {"image": ["img", 0], "width": W, "height": H}},
            "cve":  {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["cvl", 0], "image": ["rc", 0], "crop": "center"}},
            "lv":   {"class_type": "PixaromaLoadVideo", "inputs": {"video": motion, "max_frames": LEN, "force_fps": 0.0, "skip_first_frames": 0, "custom_width": W, "custom_height": H}},
            "pos":  {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["clip", 0]}},
            "neg":  {"class_type": "CLIPTextEncode", "inputs": {"text": negt, "clip": ["clip", 0]}},
            "scail": {"class_type": "WanSCAILToVideo", "inputs": {
                "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0], "width": W, "height": H,
                "length": LEN, "batch_size": 1, "pose_strength": 1.0, "pose_start": 0.0, "pose_end": 1.0,
                "video_frame_offset": 0, "previous_frame_count": 5,
                "pose_video": ["lv", 0], "reference_image": ["rc", 0], "clip_vision_output": ["cve", 0]}},
            "sched": {"class_type": "BasicScheduler", "inputs": {"model": ["msaf", 0], "scheduler": "simple", "steps": v.STEPS, "denoise": 1.0}},
            "ksel": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "samp": {"class_type": "SamplerCustom", "inputs": {"model": ["msaf", 0], "add_noise": True,
                "noise_seed": random.randint(0, 2**32), "cfg": 1.0, "positive": ["scail", 0], "negative": ["scail", 1],
                "sampler": ["ksel", 0], "sigmas": ["sched", 0], "latent_image": ["scail", 2]}},
            "dec":  {"class_type": "VAEDecode", "inputs": {"samples": ["samp", 0], "vae": ["vae", 0]}},
            "save": {"class_type": "SaveWEBM", "inputs": {"images": ["dec", 0], "filename_prefix": "owui_anim", "codec": "vp9", "fps": 24.0, "crf": 28.0}},
        }
        # Retries ONCE on GPU OOM: OpenWebUI can invoke the 20GB chat LLM (e.g. to generate the
        # chat title on a brand-new chat) AFTER our job has started, stealing the VRAM out from
        # under the sampler. Freeing again and resubmitting wins the second time.
        err = None
        for attempt in (1, 2):
            try:
                pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
            except Exception as e:
                return f"⚠️ Animate backend error: {e}"
            err = None
            for _ in range(600):
                try:
                    h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
                except Exception:
                    time.sleep(2); continue
                if pid not in h:
                    time.sleep(2); continue
                st = h[pid].get("status", {})
                if st.get("status_str") == "error":
                    d = next((m[1] for m in st.get("messages", []) if m[0] == "execution_error"), {})
                    err = (f"{d.get('exception_type', 'Error')} in {d.get('node_type', '?')} — "
                           f"{str(d.get('exception_message', ''))[:200]}")
                    break
                items = h[pid].get("outputs", {}).get("save", {}).get("images", [])
                if not items:
                    return "Animation finished but produced no output."
                it = items[0]
                data = requests.get(f"{self.comfy}/view",
                    params={"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": "output"}, timeout=120).content
                b64 = base64.b64encode(data).decode()
                # `video` is NOT a markdown block-level tag, so the opening <video> tag MUST sit
                # alone on its line (CommonMark "type-7" HTML block) so marked keeps the whole
                # element in ONE html token; OpenWebUI reads src from the text between the tags.
                return (f'<video controls loop autoplay muted playsinline style="max-width:100%;border-radius:8px">\n'
                        f'data:video/webm;base64,{b64}\n</video>\n\n*🎭 {label} animation*')
            if err is None:
                return "⏳ Timed out waiting for the animation."
            if attempt == 1 and "OutOfMemory" in err:
                self._free_vram()
                continue
            break
        return (f"⚠️ Animation failed: {err}\n\n"
                f"(If this isn't a memory error, the character image may not be full-body — try a clear full-body character.)")

    async def pipe(self, body: dict):
        text, img = self._parse(body.get("messages", []))
        return await asyncio.to_thread(self._generate, text, img)

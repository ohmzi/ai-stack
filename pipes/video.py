"""
title: Video
author: local
version: 0.2.0
required_open_webui_version: 0.5.0
description: Text-to-video with Wan 2.2 TI2V 5B via local ComfyUI. Type a description, get a short clip (~90s, 480p). Async so the UI stays responsive; auto-frees GPU VRAM.
"""
import asyncio, requests, time, base64, random

WIDTH, HEIGHT, LENGTH, STEPS, FPS = 832, 480, 49, 20, 24  # 480p, ~2s @24fps (bump WIDTH/HEIGHT to 1280x704 for 720p)


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"

    def pipes(self):
        return [{"id": "wan", "name": "Video"}]

    def _free_vram(self):
        try:
            for m in requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
        except Exception:
            pass

    def _generate(self, prompt: str):
        self._free_vram()
        neg = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
               "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
               "形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")
        wf = {
          "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
          "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
          "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
          "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["2", 0]}},
          "6": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": WIDTH, "height": HEIGHT, "length": LENGTH, "batch_size": 1}},
          "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
          "8": {"class_type": "KSampler", "inputs": {"seed": random.randint(0, 2**31), "steps": STEPS, "cfg": 5.0,
                    "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                    "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
          "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
          "10": {"class_type": "SaveWEBM", "inputs": {"images": ["9", 0], "filename_prefix": "owui_vid", "codec": "vp9", "fps": float(FPS), "crf": 32.0}},
        }
        try:
            pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
        except Exception as e:
            return f"⚠️ Video backend unreachable: {e}"
        # video gen is slow: poll up to ~40 min
        for _ in range(800):
            try:
                h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
            except Exception:
                time.sleep(3); continue
            if pid in h:
                out = h[pid].get("outputs", {}).get("10", {})
                vids = out.get("images", []) or out.get("gifs", [])
                if not vids:
                    return f"The video job finished but produced no output: {str(out)[:200]}"
                v = vids[0]
                data = requests.get(f"{self.comfy}/view",
                    params={"filename": v["filename"], "subfolder": v.get("subfolder", ""), "type": "output"},
                    timeout=60).content
                b64 = base64.b64encode(data).decode()
                return (f'<video controls loop muted playsinline style="max-width:100%;border-radius:8px">'
                        f'<source src="data:video/webm;base64,{b64}" type="video/webm"></video>\n\n'
                        f'*🎬 {prompt[:80]}*')
            time.sleep(3)
        return "⏳ Timed out waiting for the video (over 40 min)."

    def _text(self, messages):
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    return c.strip()
                if isinstance(c, list):
                    return " ".join(p.get("text", "") for p in c
                                    if isinstance(p, dict) and p.get("type") == "text").strip()
        return ""

    async def pipe(self, body: dict):
        prompt = self._text(body.get("messages", []))
        if not prompt:
            return "Type what you'd like a video of (e.g. 'a fox running through snow, cinematic')."
        return await asyncio.to_thread(self._generate, prompt)

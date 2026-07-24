# Character animation / motion transfer (SCAIL-2)

Animate a character **image** using the motion from a reference **video** (SCAIL-2, built on Wan 2.1
14B). The character's appearance is kept; the motion is transferred. Two ways to use it — both set up.

## What's installed

- **Model** (GGUF Q4_K_M so it fits the 24GB card — the fp8 version OOM'd when patching the speed LoRA):
  `models/diffusion_models/wan2.1/wan2.1_14B_SCAIL_2-Q4_K_M.gguf`
- **Speed LoRA**: `models/loras/wan2.1/lightx2v_I2V_14B_480p_cfg_step_distill_rank128_bf16.safetensors`
- **CLIP vision**: `models/clip_vision/clip_vision_h.safetensors`
- **VAE**: `models/vae/Wan2_1_VAE_bf16.safetensors` · **Text encoder**: the shared `umt5_xxl_fp8…`
- The `WanSCAILToVideo` node is **native** in ComfyUI (no extra pack). `UnetLoaderGGUF` comes from the
  ComfyUI-GGUF node (already installed).
- Runs at **448×768, 49 frames, 8 steps ≈ ~3 min/clip** on the RTX 3090 with `--lowvram`.

---

## 1. In OpenWebUI — the **Animate** model (easy)

Pick **Animate** in the model dropdown, **attach a character image** (full body works best), and name a
motion in the message: **dance**, **wave**, or **walk**. It animates your character with that motion.

- The reference clips live in `input/motion_dance.webm`, `motion_wave.webm`, `motion_walk.webm`.
- **Add a motion:** drop a short clip of one clearly-moving person into
  `/volume1/docker/comfyui/input/` named `motion_<name>.webm`, then add `"<name>": "motion_<name>.webm"`
  to the `motions` dict in `pipes/animate_scail.py` (and re-upload the function). Restart ComfyUI so it
  sees the new file.
- Valves (Admin → Functions → Animate → ⚙): `WIDTH/HEIGHT/LENGTH/STEPS/DEFAULT_MOTION`. Raising
  LENGTH/resolution gives longer/bigger clips but uses more VRAM (can OOM).

## 2. In ComfyUI — full workflow, any reference video (powerful)

Open **http://localhost:8188** and load
**`~/Downloads/Ep23 Workflows/Scail 2 Video - Compact (GGUF 24GB).json`** (I adapted your Pixaroma
workflow to the GGUF model + 448×768 so it fits 24GB). Then:

1. In **Load Image**, pick your character image.
2. In **Load Video (Pixaroma)**, upload/choose your reference motion video.
3. Set orientation/size with the Pixaroma Portrait/Landscape node (keep ≈448×768 for 24GB; higher may OOM).
4. Run. The "Long" workflow variant uses the Loop nodes to extend beyond 81 frames.

> The original `Scail 2 Video - Compact.json` (unmodified) points at the fp8 model, which OOMs on 24GB —
> use the **(GGUF 24GB)** copy instead, or swap its "Load Diffusion Model" node for "Unet Loader (GGUF)".

## Tips for good results

- Use a **full-body** character image (head to toe visible), plain background, front-facing.
- The **reference video** should show **one clearly-moving person**, full body, roughly the same
  orientation (portrait for portrait).
- If it OOMs: lower LENGTH (e.g. 49 → 41) or resolution. If motion is weak: raise `pose_strength`.
- Optional speed-up (not installed): **Sage Attention** — faster + lower VRAM, but a finicky build.

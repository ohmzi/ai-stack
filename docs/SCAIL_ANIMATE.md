# Character animation / motion transfer (SCAIL-2)

Animate a character **image** using the motion from a reference **video** (SCAIL-2, built on Wan 2.1
14B). The character's appearance is kept; the motion is transferred. Two ways to use it — both set up.

> **Superseded 2026-08-08.** "Both set up" is no longer true. Way 1 (the OpenWebUI **Animate** model)
> works. Way 2's workflow file is not on this box and was never committed here — see the note at the top
> of section 2.

## What's installed

- **Model** (GGUF Q4_K_M so it fits the 24GB card — the fp8 version OOM'd when patching the speed LoRA):
  `models/diffusion_models/wan2.1/wan2.1_14B_SCAIL_2-Q4_K_M.gguf`
- **Speed LoRA**: `models/loras/wan2.1/lightx2v_I2V_14B_480p_cfg_step_distill_rank128_bf16.safetensors`
- **CLIP vision**: `models/clip_vision/clip_vision_h.safetensors`
- **VAE**: `models/vae/Wan2_1_VAE_bf16.safetensors` · **Text encoder**: the shared `umt5_xxl_fp8…`
- The `WanSCAILToVideo` node is **native** in ComfyUI (no extra pack). Two installed packs under
  `/volume1/docker/comfyui/custom_nodes/` supply the rest of the Animate graph: **ComfyUI-GGUF**
  (`UnetLoaderGGUF`) and **comfyui-pixaroma** (`PixaromaResizeCrop`, submitted at
  `pipes/animate_scail.py:111`, and `PixaromaLoadVideo` at `:113`).
  **Added 2026-08-08:** comfyui-pixaroma was missing from this inventory. Nothing broke — the pack is
  installed here — but a rebuild that trusted this list would lose the whole clip, not one node:
  `PixaromaResizeCrop`'s output feeds both `CLIPVisionEncode` and `WanSCAILToVideo.reference_image`, and
  `PixaromaLoadVideo` feeds `pose_video`, so ComfyUI rejects the entire submitted graph on validation
  and the pipe returns `⚠️ Animate backend error` with nothing rendered.
  (`ls /volume1/docker/comfyui/custom_nodes` also shows ComfyUI-Frame-Interpolation and ComfyUI-KJNodes;
  the Animate graph submits no node from either.)
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

  > **Corrected 2026-08-08.** The three steps above are necessary but not sufficient, and the recipe as
  > written does not work. `_pick_motion` (`pipes/animate_scail.py:79-88`) never looks the message text
  > up in `motions`; it runs three literal keyword regexes and nothing else —
  > `wave|waving|waves|hello|hi|greet|greeting` → wave, `walk|walking|walks|march|marching|step|stepping`
  > → walk, `dance|dancing|dances|dancer|boogie` → dance. Text matching none of the three falls through
  > to the `DEFAULT_MOTION` valve, or to `"dance"` when that valve names a key the dict does not have.
  > So a new `"jump"` entry is unreachable by writing "jump" in the message: with the stock
  > `DEFAULT_MOTION="dance"` the pipe uploads your character, spends the full ~3 min, and hands back the
  > **dance** clip captioned *🎭 dance animation* — no error, no hint that "jump" was ignored, which
  > reads as the model animating badly rather than as a missing branch. To make a new motion selectable
  > from the message you must add a fourth regex branch to `_pick_motion` too. Without that branch the
  > only way to reach the new clip at all is to point `DEFAULT_MOTION` at its key, which then applies to
  > every message that names no motion.
- Valves (Admin → Functions → Animate → ⚙): `WIDTH/HEIGHT/LENGTH/STEPS/DEFAULT_MOTION`. Raising
  LENGTH/resolution gives longer/bigger clips but uses more VRAM (can OOM).

## 2. In ComfyUI — full workflow, any reference video (powerful)

> **Superseded 2026-08-08.** The file this section tells you to load does not exist, so the four steps
> below cannot be started as written — the ComfyUI load dialog opens on nothing to pick. Measured today:
> `ls -la /home/ohmz/Downloads` returns three entries (`.`, `..`, and a `.claude` directory), so the
> `Ep23 Workflows/` directory is gone; `find /home/ohmz -maxdepth 3 -iname "*Scail*"` returns only this
> doc and `pipes/animate_scail.py`, and `find /home/ohmz -maxdepth 4 -iname "*Ep23*"` returns nothing.
> That kills the fallback in the blockquote below as well: the unmodified
> `Scail 2 Video - Compact.json` is missing too, so there is nothing to swap the loader node in. The
> workflow is not in ComfyUI either — `find /volume1/docker/comfyui -iname "*scail*"` returns four paths,
> all of them the Q4_K_M model plus three files in `output/`, and
> `custom_nodes/comfyui-pixaroma/workflows/` contains no scail, wan or video entry. It was never
> committed to this repo either: `git ls-files '*.json'` lists 8 files and none of them contains
> `class_type` or `last_node_id`, so no ComfyUI graph has ever been tracked here.
>
> **To rebuild it:** start from the Pixaroma Ep23 "Scail 2 Video - Compact" workflow, replace its
> **Load Diffusion Model** node with **Unet Loader (GGUF)** pointed at the `wan2.1_14B_SCAIL_2-Q4_K_M.gguf`
> file listed under *What's installed*, and set the size to 448×768 so it fits 24GB. Commit the result
> into this repo rather than leaving it in `~/Downloads`, which is what lost it. Until then, way 1 is the
> only working path, and the `pipes/animate_scail.py` graph is the reference for how the nodes wire up.

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

# Video Quality Roadmap (researched + feasibility-verified 2026-07-18)

> **STATUS 2026-07-18 evening — Tier 1 + most of Tier 2 IMPLEMENTED & tested:**
> sage enabled (scoped, KJ patch node) ✓ · RIFE 2x → 32 fps ✓ · 250928 LoRAs ✓ · 6 steps w/ motion
> recipe (high: cfg 3, LoRA 0.8) ✓ · dead-negative resolved (active again at cfg 3 on high stage) ✓ ·
> torch 2.8 + `--fast fp16_accumulation` (image `comfyui-local:tier2`, rollback `:pre-tier2`) ✓ ·
> 720p/quick/longer keywords ✓ · RifleX 121f wired ✓ · **multi-shot I2V chaining ✓ (15 s knight/dragon
> sequence verified: plan → T2V → I2V×2 w/ last-frame handoff + ColorMatch → ffmpeg concat)**.
> Steady state: 180 s/clip = old wall time but 2.25× the sampling work + 32 fps.
> NOT done: torch.compile (dynamo breaks on --lowvram GGUF Conv3d patching — plumbing wired,
> V_COMPILE=False), **dyno high-noise model (NO GGUF quant exists — verifier over-claimed; the
> fp16 is 28.6 GB, doesn't fit)**, audio (MMAudio/Foley), SeedVR2. Gotchas hit: torch 2.8 needs
> torchaudio 2.8 (undefined symbol crash) and triton/inductor need **gcc in the container**.

Baseline as researched 2026-07-18 (superseded — see STATUS above): Wan 2.2 T2V A14B GGUF Q4 +
Lightning Seko V2.0 (4 steps), 832x480x81f @16fps, ~166 s/clip on the RTX 3090. All items below
were adversarially verified against the actual container/stack (22 confirmed, 1 doubtful). Sources
in the workflow transcript.

> **Superseded 2026-08-08.** That line was written before Tier 1 landed and reads present-tense.
> The engine and the default resolution survive; the LoRA pair, the step count, the saved frame rate
> and the wall time do not. What the pipe actually runs, measured in `pipes/auto_assistant.py`:
> the **250928** Lightning pair (`Wan22_T2V_A14B_4step_HIGH_250928.safetensors` and
> `Wan22_T2V_A14B_4step_LOW_250928.safetensors`, loaded at `:3550-3553` — the `4step` is part of the
> upstream LoRA's name, not our step count), **6 steps** split 3+3 (`V_STEPS_14B = 6`, `:173`) with
> the high expert alone on **cfg 3.0 and LoRA 0.8** (`V_HIGH_CFG = 3.0` `:174`,
> `V_HIGH_LORA = 0.8` `:176`; the low stage stays cfg 1.0), still **832x480x81f** by default
> (`V_W, V_H = 832, 480` `:168`; `V_LEN_14B = 81` `:171`),
> **rendered at 16 fps and written at 32 fps** because RIFE 2x runs before the save
> (`V_RIFE = 2` `:177`; `SaveWEBM fps = float(V_FPS_14B * V_RIFE)` `:3528-3530`), at the
> **~180 s/clip** the STATUS block records. The Seko V2.0 pair is still on disk in
> `/volume1/docker/comfyui/models/loras/wan2.2/` alongside the 250928 pair, but no node loads it.
> Cost of leaving the old line present-tense: an operator tuning quality goes looking for a 4-step
> config the pipe no longer has, and reads a normal 180 s render as a 14 s regression against a
> number that stopped being the baseline in July.

## Tier 1 — free or near-free, do first

1. **Enable SageAttention — it is ALREADY INSTALLED (sageattention 1.0.6 + triton 3.1.0) but never
   enabled.** Scope it to the A14B graph only via ComfyUI-KJNodes `PathchSageAttentionKJ` (typo'd
   node name is real) between LoraLoaderModelOnly→ModelSamplingSD3 on both experts. Do NOT enable
   globally: the TI2V 5B path produces pure noise with sage; Qwen-Image/Krea have black-frame
   reports. Gain: ~15-25% at 480p, ~1.6x at 720p.
2. **RIFE 2x interpolation (16→32 fps)** — Fannovel16/ComfyUI-Frame-Interpolation, `RIFE VFI`
   (rife47.pth ~50MB) between VAEDecode and SaveWEBM, and set SaveWEBM fps=32.0 (must scale
   together!). Biggest perceived smoothness win per minute of effort. Use 2x not 3x (artifacts).
   Fallback for fast-motion warps: FILM VFI (same pack) or kijai GIMM-VFI (needs cupy-cuda12x).
3. **Swap Lightning LoRAs Seko V2.0 → 250928 pair** (3.7GB,
   `lightx2v/Wan2.2-Lightning/Wan2.2-T2V-A14B-4steps-lora-250928/{high,low}_noise_model.safetensors`).
   Community A/B: better colors, less oversaturation, more fine detail. (No "Seko V2.2" exists.)
4. **6 steps instead of 4** (3+3 split) using the speed freed by sage — better motion/anatomy.
   Optional motion recipe: high-expert LoRA strength 0.6-0.8 + cfg 2-3.5 on the high sampler only
   (cfg>1 re-enables the uncond pass = slower; low sampler stays cfg 1).
5. **Delete the Chinese negative from the 14B path** — verified dead weight: samplers.py skips the
   uncond entirely at cfg 1.0. (Still live in the 5B path which runs cfg 5.) If a working negative
   is wanted later: ComfyUI-NAG (`KSamplerAdvanced (NAG)`).
6. **RIFLEx for 7.5 s clips** — KJNodes `ApplyRifleXRoPE_WanVideo` per expert + length 121.
   Without it 121f gives sped-up motion + burnt first frames. `WanContextWindowsManual` is already
   in ComfyUI 0.28.0 for going past that (visible mid-clip drift; use sparingly).

## Tier 2 — medium effort, big payoffs

7. **Native 720p premium mode (1280x704)** — verified 3090 benchmark on this exact workflow class:
   ~8 min standard, **~5 min with SageAttention 2.2**, ~4 min with SpargeAttention. Expose as a
   keyword ("720p"/"hq") rather than default.
8. **torch 2.5.1 → 2.8.x + `--fast fp16_accumulation`** (rebuild comfyui-local, ~3GB wheels) —
   +25% measured on a 3090 (ComfyUI PR #6453); the flag silently no-ops on torch <2.7 (verified in
   model_management.py). 2.8.0 fixes a 2.7.x cuBLASLt crash. Skip fp8_matrix_mult (Ada/Hopper only).
9. **torch.compile on both experts** (after #8 + `git pull` ComfyUI-GGUF): +15-30% once warm;
   1-3 min recompile per restart/resolution-change; set TORCHINDUCTOR_CACHE_DIR to a volume.
   Full stack estimate (1+8+9): 166 s → ~80-100 s at 480p. (That 166 s is the pre-Tier-1 researched
   baseline superseded above. #1 and #8 have since shipped and the steady state is 180 s at 2.25× the
   sampling work, so this projection has not been re-derived against what runs today.)
10. **SageAttention 2.2 source-compile for sm_86** (needs CUDA toolkit in a build stage): extra
    10-20% over the installed Triton 1.0.6 build; the 8→5 min 720p number is sage 2.2.
11. **Fix Lightning slow-motion with the 250928-dyno high-noise model** (full distilled model, GGUF
    Q4_K_M 9.66GB from QuantStack; replaces high expert + high LoRA — delete the lh node). Repo's
    own guidance: motion speed matches the base model.
12. **Audio on clips**: HunyuanVideo-Foley (quality; 12GB VRAM with --enable_offload, runs after the
    Wan job) or MMAudio (~5GB, faster/lighter). Chain as a second ComfyUI job in the pipe.
13. **True multi-shot (15-30 s sequences)**: Wan 2.2 **I2V** A14B GGUF Q4 (19.4GB, same footprint
    as T2V) + I2V Seko-V1 LoRAs → last-frame → next-shot chaining with per-shot prompts. Add
    KJNodes ColorMatch per segment (colors drift when chaining — known Wan issue #172). Free
    draft version today: TI2V 5B `start_image` (input exists in the running node) + ColorMatch.
14. **SeedVR2 3B video upscaler** (numz node, ~7GB fp16, fits 24GB): temporally-consistent 2x for
    when 480p generation speed should stay snappy. batch_size must be 4n+1 (min 5).

## Skip (verified not worth it on this box)

- **Latent hires-fix second pass** — works but loses to native 720p and SeedVR2 on quality/minute.
- **LTX-2 / 2.3** — official min 32GB VRAM; fp8/nvfp4 fast paths bypass Ampere. GGUF fits but slow;
  the "fast on 3090" claims did not verify. Only open model with native synced audio though.
- **HunyuanVideo 1.5** — credible but a sidegrade (better prompt-following, similar overall quality);
  30-40GB for a second engine. Revisit if Wan starts failing specific prompt classes.
- **Wan 2.5/2.6/2.7** — still API-only, no open weights as of 2026-07 (HF org verified).

## Bonus finding

- **Wan-Dancer-14B** dropped 2026-07-17 (Wan-AI org) — a dance/motion model; potentially relevant
  to the SCAIL "Animate" pipe. Unresearched beyond existence.

## OpenWebUI-side (workspace/UX) improvements, no research needed

- Emit progress via `__event_emitter__` in the pipes ("Enhancing prompt… / Sampling 3/6… /
  Interpolating…") instead of a silent spinner for 3-8 minutes.
  > **Partly done — superseded 2026-08-08.** The silent spinner is gone from the Assistant, Image
  > and Photoreal pipes, so the bullet's premise is stale, not just its scope.
  > `pipes/auto_assistant.py:5644`, `pipes/image_krea.py:667` and `pipes/photoreal.py:437` each
  > define a `_tracked()` wrapper that emits a status line on start, re-emits it every 2 s with a
  > live elapsed time (`_fmt_dur(time.monotonic() - start)`), then collapses via `_finish()` to a
  > final line of the form `"<verb> in <duration> · <detail>"` — photoreal's detail names
  > engine/tier/seed (`photoreal.py:456`). Two pieces are still missing:
  >
  > (a) **per-stage granularity**, i.e. the "Sampling 3/6 / Interpolating" part. Nothing reads
  > per-step progress out of ComfyUI: the job loop polls only `/history/{prompt_id}` for completion
  > (`auto_assistant.py:3119`), so the status line can report elapsed time but not which sampler
  > step or node is running.
  >
  > (b) **any emitter at all in the Animate pipe.** `pipes/animate_scail.py:173` is
  > `async def pipe(self, body: dict)` — no `__event_emitter__` parameter, and grep for
  > `__event_emitter__`, `_status` or `_tracked` in that file returns nothing. An Animate request is
  > still a literally silent wait: SCAIL_ANIMATE.md:28 measures ~3 min/clip at 448×768, 49 frames,
  > 8 steps, and for all of it the user sees an unannotated spinner.
- Keyword controls in the auto pipe: "720p"/"hq" → premium res, "quick video" → 5B fast path,
  "longer" → RIFLEx 121f.
- Expose V_QUALITY / steps / resolution as OpenWebUI Valves so they're editable in the UI without
  redeploying.

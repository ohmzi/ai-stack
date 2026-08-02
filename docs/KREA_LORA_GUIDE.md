# Training your own Krea 2 LoRA (fal.ai) and using it locally

This is the one part that isn't automated, because it uses **your** account, card, and images.
It's a one-time ~**$3** step. Everything after you download the file is handled locally by the
Krea LoRA Converter node + the "Image" pipe. Based on the Pixaroma Ep26 method.

---

## 1. Prepare your images (most important step)

- **15–20 images**, good quality, **varied** (different angles, lighting, backgrounds, outfits/poses).
- One clear subject per image. Avoid other people, heavy filters, motion blur, or busy collages.
- Crop out distractions; the subject should be obvious.
- Square-ish or portrait framing is fine — fal.ai trains at **1024**.
- **Consistency matters more than count.** 16 clean, on-subject images beat 30 noisy ones.

Tip by type:
| What the LoRA is | Images | Steps | Notes |
|------------------|--------|-------|-------|
| Real person      | ~16    | **~700**  | Fewer steps → stays flexible, less "burned in" |
| Cartoon character| ~17    | **~1000** | |
| Art style        | ~20    | **~1000** | Show the style on many different subjects |

## 2. Pick a unique trigger word

Make up a rare token so it doesn't collide with normal words, e.g. `pixag1rl26`, `questpaint26`,
`pixabunny26`. You'll type this in every prompt to invoke the LoRA. Write it down.

## 3. Train on fal.ai

> **This guide produces Krea 2 LoRAs, which work in the "Image" pipe and nowhere else.**
> They are architecturally incompatible with Lustify SDXL, so they will not load in
> **Photoreal** — a Krea 2 LoRA cannot give that pipe a consistent recurring character.
> Photoreal has no LoRA loader node at all today; wiring one up needs an SDXL trainer, not
> this one.
>
> Also worth being clear about what a LoRA is for, because the two get conflated constantly:
> a LoRA bakes **one** subject into the model so you can summon them by trigger word in a
> **fresh** generation. It does nothing to keep the person in a photo you hand over for
> editing — that is the edit path's job (see `pipes/shared/identity_edit.py`), and no amount
> of LoRA training will fix an edit that changes who is in the picture.

1. Go to **<https://fal.ai/models/fal-ai/krea-2-trainer>** and sign in (add a little credit; ~$3/run).
2. Upload your images (or a .zip of them).
3. Settings (the creator's proven starting points):
   - **Steps:** 700 (real person) / 1000 (character or style)
   - **Resolution:** 1024
   - **Learning rate:** 0.0001
   - **Trigger word:** the one you chose
   - **Auto-captioning:** On is fine
4. Start training, wait for it to finish, and **download the resulting `.safetensors`** file.

> These are starting points, not fixed rules. If the result is **too weak**, raise steps (or LoRA
> strength later). If it **overtrains** (ignores your prompt, always the same pose), lower steps.

## 4. Hand it to me (local integration)

1. Put the downloaded file in **`/volume1/docker/comfyui/models/loras/krea2/`**
   (e.g. `krea2/pixag1rl26.safetensors`).
2. Tell me the **filename** and your **trigger word**. I will then:
   - Run the **Krea LoRA Converter** (Pixaroma) — fal.ai LoRAs name their layers differently than
     ComfyUI expects, so this makes a `*_comfyui.safetensors` copy that loads correctly.
   - Test a few **LoRA strengths** (≈0.6 → 1.2) and pick the best, per the video's XY-plot method.
   - Set the **Image** model's valves: `LORA_FILE`, `TRIGGER`, `LORA_STRENGTH`.
3. Done — your subject/style now generates locally in OpenWebUI. Use the trigger word in prompts.

## 5. Using it day-to-day

- In OpenWebUI, select **Image** (or just ask the 🪄 Assistant to "draw…").
- Include your **trigger word** in the request for the LoRA look; omit it for plain Krea 2.
- **Gentle edits:** attach a generated image and describe a small change ("same but warmer light,
  add a red scarf"). It re-renders at low strength so ~90% stays the same. Lower the **EDIT_DENOISE**
  valve for even smaller changes, raise it for bigger ones.
- If the LoRA is slightly too strong/weak, adjust **LORA_STRENGTH** instead of retraining.

---

*Manual conversion (reference — I normally do this for you):* open ComfyUI at
<http://localhost:8188>, add the **Krea 2 LoRA Converter** node, pick your fal.ai file, click
**Convert**; the converted copy appears in `models/loras/krea2/` for the normal LoRA loader.

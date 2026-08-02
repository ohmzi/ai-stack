"""Identity-preserving image editing, shared by the image pipes.

WHY THIS FILE EXISTS
--------------------
`photoreal.py` took a reference photo and returned a different person in the same pose.
That is not a tuning problem. SDXL img2img has no channel that carries "who this is":
the reference enters only as an initial latent (VAEEncode -> KSampler.latent_image), and
at denoise 0.65 sampling starts above the sigma where facial identity lives. Identity is
mid/high frequency -- inter-ocular ratios, lid crease, nasion-to-philtrum geometry, skin
micro-texture -- and all of it is below the noise floor at that strength, so it gets
RESAMPLED FROM THE CHECKPOINT'S PRIOR rather than reconstructed from the reference.

Lowering the denoise cannot fix it. A structural edit needs sigma high enough to introduce
new mid-frequency structure, which is the same threshold that destroys identity. One
scalar is being asked to be low where the face is and high where the jacket is:

    <= 0.35   the edit does not land, it becomes a re-render      identity holds
    0.40-0.50 half-applied and inconsistent                       "their sibling"
    >= 0.55   the edit lands                                      a stranger in their pose

Qwen-Image-Edit 2509 does not have this problem, because the reference goes into the
CONDITIONING (TextEncodeQwenImageEditPlus takes image1 on BOTH the positive and negative
encoders) instead of into the noise budget. Measured on this box 2026-08-01, fixed seed,
10 renders (auto_assistant.py:180-190 and UPGRADE_ROADMAP.md 1.3):

                 time    source fidelity (MAE)   grain kept   negative prompt
       best     152 s    16.04 whole / 9.99 wood     84.5%     works
       balanced  36 s     9.36 whole / 2.68 wood    100.8%     INERT
       fast      20 s     9.28 whole / 3.24 wood     98.5%     INERT

That untouched-region MAE of 2.68 and grain of ~100% IS the identity-preservation number.

SCOPE -- deliberately pure functions only
-----------------------------------------
Nothing here does network I/O or touches `self`. The pipes keep their own submit/poll
loops, which differ in their cancel semantics and error surfaces and are not worth
unifying. Three pipes import this, so a fault here breaks all three at once; keeping it
to pure graph-building and string-parsing means it can be exercised completely on the
host with no GPU, no ComfyUI, and no mocking.

DEPLOYMENT
----------
OpenWebUI stores each Function in its database and execs it standalone -- a pipe cannot
import a repo-relative module. This file is therefore ALSO copied to the OpenWebUI data
mount (`/volume1/docker/openwebui/config/`, mounted at `/app/backend/data`, which persists
across container recreate), and the pipes reach it with a guarded sys.path insert. Every
importer must degrade gracefully when the import fails; see photoreal.py's fallback.

Keep this file, `pipes/shared/identity_edit.py`, as the source of truth. Copy it out with
`make deploy-shared` (or by hand); `tests/test_deployed.py` checks the two agree.
"""
import json
import re
import time

__version__ = "1.0.0"

# Instruction-edit sampler tiers. The Lightning LoRA is cfg-distilled, so cfg MUST stay 1.0
# with it: at 2.5 the negative only half-bites and the render costs 68 s, and at 4.0 the
# LoRA plus the uncond pass OOMs the 24 GB card outright (observed). That is why each tier
# carries its own cfg rather than letting a caller pick one.
#
# At cfg 1.0 the negative prompt does NOTHING -- measured, not inferred: the same seed with
# negative="" and negative="steam, smoke, vapour, mist" produced byte-identical output
# (max abs pixel diff 0), while the same pair at cfg 4.0 differed. Any caller that depends
# on its negative must request "best".
EDIT_TIERS = {
    "best":     {"lightning": False, "steps": 20, "cfg": 4.0},
    "balanced": {"lightning": True,  "steps": 8,  "cfg": 1.0},
    "fast":     {"lightning": True,  "steps": 4,  "cfg": 1.0},
}
EDIT_LORA = "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"

# Qwen-Image-Edit 2509 asset names, as they sit in /volume1/docker/comfyui/models.
EDIT_UNET = "Qwen-Image-Edit-2509-Q4_K_M.gguf"
EDIT_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
EDIT_VAE = "qwen_image_vae.safetensors"

SEED_MAX = 2 ** 31 - 1
# "seed 12345", "seed:12345", "--seed=12345". Bounded to 10 digits so a long number in the
# prompt itself ("a crowd of 12345678901 people") cannot be mistaken for a seed.
_SEED_RE = re.compile(r"(?:^|\s)(?:--)?seed\s*[:=]?\s*(\d{1,10})\b", re.I)


def resolve_tier(name, tiers=None):
    """The named tier, falling back to the FULL path on anything unrecognised.

    The direction of this fallback is the whole point. Degrading to 'best' costs time;
    degrading to a lightning tier would silently disable the caller's negative prompt,
    which is a correctness failure nobody would see in the output.
    """
    tiers = tiers or EDIT_TIERS
    return tiers.get(str(name or "").strip().lower(), tiers["best"])


def parse_seed(text):
    """(text_without_the_seed_token, seed_or_None).

    The match MUST be removed from the text: everything downstream feeds the remainder to a
    text encoder, and a leftover "seed 12345" becomes prompt tokens -- which on SDXL is not
    inert, it is five tokens of noise competing with the identity anchors for the first
    chunk, where the pooled embedding comes from.
    """
    if not text:
        return text or "", None
    m = _SEED_RE.search(text)
    if not m:
        return text, None
    seed = int(m.group(1))
    if seed > SEED_MAX:
        return text, None  # not plausibly a seed; leave the text alone
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned, seed


def build_qwen_edit_wf(instruction, ref_name, seed, cfg, steps, negative="", lightning=False,
                       filename_prefix="owui_edit"):
    """Qwen-Image-Edit 2509: follow a text INSTRUCTION on the attached image.

    A true instruction editor -- it changes what was asked and keeps the rest. img2img
    cannot do this at any denoise, because it has nowhere to put "the rest".

    The identity channel is `image1` on BOTH text encoders. Feeding the reference to the
    NEGATIVE encoder as well is not a copy-paste artefact: it is what makes the guidance
    difference meaningful with respect to this specific image rather than to the model's
    unconditional prior.

    denoise is 1.0 and that is correct here -- fidelity comes from the conditioning, not
    from a partially-noised latent. Do not "fix" it downward.

    Output node is "s". Callers must pass that to their poller rather than assuming a
    numeric id (photoreal.py's original loop hardcoded "9", which this graph does not have).
    """
    wf = {
        "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": EDIT_UNET}},
        "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
        "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": EDIT_CLIP, "type": "qwen_image", "device": "default"}},
        "v":    {"class_type": "VAELoader", "inputs": {"vae_name": EDIT_VAE}},
        "ld":   {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
        "pos":  {"class_type": "TextEncodeQwenImageEditPlus",
                 "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0],
                            "prompt": instruction}},
        "neg":  {"class_type": "TextEncodeQwenImageEditPlus",
                 "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0],
                            "prompt": negative}},
        "enc":  {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}},
        "k":    {"class_type": "KSampler",
                 "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler",
                            "scheduler": "simple", "denoise": 1.0, "model": ["cfgn", 0],
                            "positive": ["pos", 0], "negative": ["neg", 0],
                            "latent_image": ["enc", 0]}},
        "d":    {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
        "s":    {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": filename_prefix, "images": ["d", 0]}},
    }
    if lightning:  # speed LoRA slots between the raw unet and ModelSamplingAuraFlow
        wf["lora"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": ["u", 0], "lora_name": EDIT_LORA, "strength_model": 1.0}}
        wf["msaf"]["inputs"]["model"] = ["lora", 0]
    return wf


# The rewrite that makes an SDXL edit prompt survivable.
#
# SDXL has no instruction-following. It reads the positive prompt as a DESCRIPTION OF THE
# TARGET IMAGE, so "undress this / make her smile" tells it the target is about "making"
# and "smiling" with zero identity anchors -- which is worse than an empty prompt, because
# a generic subject noun is an active attractor toward the checkpoint's average face, and
# Lustify is an aesthetic finetune with a narrow and very recognisable one.
#
# Restating concrete traits is the fix, and the evidence is in this repo: _style_enrich
# (auto_assistant.py:1261-1287) exists ONLY because generic "keep them the same" wording
# failed -- the recorded case being a Pakistani father coming out a different ethnicity on
# a cartoon->photo conversion. Generic preservation language does nothing; named traits do.
#
# Ordering is a hard requirement, not a style preference. ComfyUI does not truncate at 77
# tokens -- it starts a new batch and concatenates (comfy/sd1_clip.py:638) -- but there is
# no cross-chunk attention and SDXL's pooled embedding comes from chunk 1. Identity anchors
# must therefore land in the first ~70 tokens; scene and camera can overflow into chunk 2
# where weaker influence is fine.
SDXL_EDIT_REWRITE_SYS = (
    "You convert a photo-editing request into a prompt for Lustify SDXL, which CANNOT follow "
    "instructions -- it only renders a description of a finished image. You are shown the "
    "source photo and the user's request.\n"
    "Write ONE comma-separated description of how the photo should look AFTER the edit. Never "
    "write an instruction: 'make her smile' becomes 'smiling warmly', 'remove the hat' becomes "
    "a description with no hat in it.\n"
    "ORDER MATTERS ABSOLUTELY. Lead with the identity of the person as you actually see them in "
    "the photo, in under 60 tokens: apparent age, gender, ethnicity and skin tone, face shape, "
    "eye colour, eyebrow shape, nose shape, lip shape, hair colour and length and texture and "
    "parting, facial hair, glasses, visible moles or freckles or scars, body build. Be specific "
    "and concrete -- 'South Asian woman, mid-20s, oval face, dark brown almond eyes, thick "
    "straight brows, long wavy near-black hair centre-parted' -- because vague preservation "
    "language has no effect on the model and the face will drift to a generic one. THEN state "
    "the edit. THEN the scene, lighting, camera and lens.\n"
    "Output a second line, 'AVOID: ' plus 6-10 comma-separated traits the result must not have: "
    "the ethnicities and ages it might drift toward, plus 'generic beauty, instagram face, "
    "airbrushed skin, symmetrical model face, different person'.\n"
    "Output EXACTLY two lines:\nDESCRIBE: <description>\nAVOID: <traits>"
)


def parse_rewrite(out, fallback):
    """(description, avoid) from the two-line rewriter reply. Falls back to the raw request.

    Tolerates the markdown bolding small models add unprompted ('**DESCRIBE:** ...'), which
    is the single most common way this contract breaks in practice.
    """
    if not out:
        return fallback, ""
    desc = re.search(r"^\s*\**\s*DESCRIBE\**\s*:\s*(.+)$", out, re.I | re.M)
    avoid = re.search(r"^\s*\**\s*AVOID\**\s*:\s*(.+)$", out, re.I | re.M)
    if not desc:
        return fallback, ""
    return desc.group(1).strip(), (avoid.group(1).strip() if avoid else "")


def metric(path, **fields):
    """Append one JSON line describing a finished media job. Never raises, never blocks a reply.

    Deliberately a flat file rather than counters: the useful questions are "how often does
    this drift, and what did it cost" and "which requests keep failing", and both need the
    individual rows. Read it with tests/media_metrics.py, which reports p50/p90 -- the mean
    of a bimodal render-time distribution describes nothing that ever happens.
    """
    if not path:
        return
    try:
        fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a") as f:
            f.write(json.dumps(fields, default=str) + "\n")
    except Exception:
        pass  # instrumentation must never cost a user their generation

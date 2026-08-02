#!/usr/bin/env python3
"""A reference-image edit must not come back as a different person.

Why this file exists. Photoreal took a photo and an instruction and returned a stranger in the
same pose. Three separate defects, all in the img2img path:

  1. denoise 0.65 over the WHOLE frame. The reference's only influence was the initial latent,
     and at that strength sampling starts above the sigma where facial identity lives, so the
     face was resampled from Lustify's prior instead of reconstructed. No denoise value fixes
     this: below ~0.35 the edit does not land, above ~0.55 the person changes, and a structural
     edit needs the high end. One scalar cannot be low where the face is and high where the
     jacket is.
  2. the prompt rewriter was skipped exactly when a reference was attached (`if not ref_b64`),
     so a raw instruction reached CLIPTextEncode. SDXL cannot follow instructions -- it renders
     a description of a finished image -- so "keep her the same" was read as generic subject
     tags, which is an active pull toward the checkpoint's average face.
  3. nothing carried identity at all. Compare Qwen-Image-Edit, where the reference goes into
     BOTH text encoders as conditioning and holds ~100% of the source grain (measured).

So edits now default to Qwen and SDXL is the uncensored fallback. The checks below pin the
things that would silently undo that -- a hardcoded output node that makes every Qwen render
report "no image", a seed leaking into the prompt, a tier fallback that lands on a silent
no-negative, and a downgrade the status line does not admit to.

Usage:  python3 tests/test_photoreal_edit.py [pipe_path]
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Defaults to the TRACKED source, not pipes/live/, so this file tests the code under review.
# Whether that source is the code OpenWebUI is actually running is a separate question, and
# tests/test_deployed.py is the one that answers it — including the repo -> live/ hop, which
# nothing checked until now.
PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "pipes/photoreal.py")

# The pipe imports identity_edit from OpenWebUI's data mount, which does not exist on the host.
# Point it at the repo copy instead -- that copy is the source of truth, and test_deployed.py
# separately checks the deployed one matches it.
sys.path.insert(0, os.path.join(ROOT, "pipes/shared"))

spec = importlib.util.spec_from_file_location("photoreal_under_test", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.METRICS_PATH = ""  # never write instrumentation from a test run

import identity_edit as shared  # noqa: E402  (must follow the sys.path insert above)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def build(pipe, prompt, ref="Zm9v", seed=None, rewrite=("DESCRIBED PERSON, edit", "drift-traits")):
    """Drive _generate far enough to capture the workflow it would submit, without a GPU."""
    seen = {}
    pipe._free_vram = lambda *a, **k: None
    pipe._upload = lambda *a, **k: "ref.png"
    pipe._enhance = lambda p: f"ENHANCED {p}"
    pipe._enhance_edit = lambda i, r: rewrite
    pipe._submit_poll = lambda wf, out_node: (seen.update(wf=wf, out_node=out_node), (None, "stop"))[1]
    md, meta = pipe._generate(prompt, ref, seed)
    seen.update(meta=meta, md=md)
    return seen


def main():
    src = open(PIPE_PATH, encoding="utf-8").read()

    print("--- the shared module's seed parser ---")
    for text, want_seed, want_text in [
        ("add a hat seed 12345", 12345, "add a hat"),
        ("seed:99 a portrait", 99, "a portrait"),
        ("a portrait --seed=7", 7, "a portrait"),
        ("SEED 42 sunset", 42, "sunset"),
        ("a crowd of 12345678901 people", None, "a crowd of 12345678901 people"),
        ("no seed here", None, "no seed here"),
        ("", None, ""),
    ]:
        got_text, got_seed = shared.parse_seed(text)
        check(f"{text!r} -> seed {want_seed}", got_seed == want_seed, f"got {got_seed}")
        check(f"...and the token is stripped from the text", got_text == want_text, repr(got_text))

    print("--- the tier fallback degrades to full quality, never to a silent no-negative ---")
    for bad in ("nonsense", "", "  ", "BEST!", None):
        check(f"{bad!r} falls back to 'best'", shared.resolve_tier(bad) == shared.EDIT_TIERS["best"])
    check("tier names are case- and whitespace-insensitive",
          shared.resolve_tier(" BaLaNcEd ") == shared.EDIT_TIERS["balanced"])
    for name, t in shared.EDIT_TIERS.items():
        if t["lightning"]:
            check(f"'{name}' pins cfg to 1.0 because it uses the LoRA", t["cfg"] == 1.0, str(t))
    check("'best' keeps a cfg that lets the negative bite", shared.EDIT_TIERS["best"]["cfg"] > 1.0)
    check("the LoRA file is named once, not per call site",
          open(shared.__file__).read().count('"Qwen-Image-Edit-2509-Lightning') == 1)

    print("--- the Qwen graph is what carries identity ---")
    wf = shared.build_qwen_edit_wf("add a hat", "ref.png", 1, 4.0, 20)
    # Both encoders must be the reference-aware class AND actually be handed the reference.
    # Asserting only the image1 wiring is not enough: a plain CLIPTextEncode carrying a stray
    # image1 key validates fine here and silently drops the identity channel at render time.
    for side in ("pos", "neg"):
        check(f"the {side.upper()} encoder is reference-aware",
              wf[side]["class_type"] == "TextEncodeQwenImageEditPlus", wf[side]["class_type"])
        check(f"...and is handed the reference", wf[side]["inputs"]["image1"] == ["sc", 0])
    check("both encoders read the SAME scaled reference",
          wf["pos"]["inputs"]["image1"] == wf["neg"]["inputs"]["image1"] == ["sc", 0])
    check("denoise stays 1.0 (fidelity comes from conditioning, not a partial latent)",
          wf["k"]["inputs"]["denoise"] == 1.0)
    check("it saves from 's', not a numeric id", "s" in wf and "9" not in wf)
    lit = shared.build_qwen_edit_wf("i", "n.png", 1, 1.0, 8, lightning=True)
    check("lightning inserts a LoraLoaderModelOnly", "lora" in lit)
    check("...between the unet and ModelSamplingAuraFlow",
          lit["msaf"]["inputs"]["model"] == ["lora", 0] and lit["lora"]["inputs"]["model"] == ["u", 0])
    check("the full path has no LoRA node at all", "lora" not in wf)

    print("--- the pipe has valves, and edits default to the engine that preserves identity ---")
    check("Pipe declares a Valves model", hasattr(mod.Pipe, "Valves"))
    p = mod.Pipe()
    check("instances carry a valves instance", hasattr(p, "valves"))
    check("edits default to Qwen", p.valves.EDIT_ENGINE == "qwen")
    check("...at the measured-fast tier", p.valves.EDIT_QUALITY == "balanced")
    check("the SDXL rewrite is on by default", p.valves.REWRITE is True)

    print("--- the output node is derived from the graph, not hardcoded ---")
    # The original poller read outputs["9"] unconditionally. The Qwen graph has no node "9", so
    # this would have made every identity-preserving render report "produced no image".
    got = build(mod.Pipe(), "add a red hat")
    check("a Qwen edit polls node 's'", got["out_node"] == "s", repr(got.get("out_node")))
    check("...and really did build the Qwen graph", "TextEncodeQwenImageEditPlus" in
          {n.get("class_type") for n in got["wf"].values()})
    p2 = mod.Pipe(); p2.valves.EDIT_ENGINE = "sdxl"
    got_sdxl = build(p2, "add a red hat")
    check("an SDXL edit polls node '9'", got_sdxl["out_node"] == "9", repr(got_sdxl.get("out_node")))
    got_t2i = build(mod.Pipe(), "a portrait", ref=None)
    check("text-to-image polls node '9'", got_t2i["out_node"] == "9")
    check("_submit_poll takes the node as an argument", "def _submit_poll(self, wf, out_node)" in src)
    check("no surviving hardcoded output-node read",
          '.get("outputs", {}).get("9"' not in src and 'outputs", {}).get("9")' not in src)

    print("--- the seed reaches the sampler and never the prompt ---")
    got = build(mod.Pipe(), "add a red hat", seed=12345)
    check("the sampler gets the requested seed", got["wf"]["k"]["inputs"]["seed"] == 12345)
    check("...and it is reported back", got["meta"]["seed"] == 12345)
    p3 = mod.Pipe(); p3.valves.EDIT_ENGINE = "sdxl"
    got = build(p3, "add a red hat", seed=777)
    check("the SDXL sampler gets it too", got["wf"]["3"]["inputs"]["seed"] == 777)
    text, seed = shared.parse_seed("add a red hat seed 777")
    check("'seed 777' never reaches a text encoder", "777" not in text and "seed" not in text.lower())
    a = build(mod.Pipe(), "x")["wf"]["k"]["inputs"]["seed"]
    b = build(mod.Pipe(), "x")["wf"]["k"]["inputs"]["seed"]
    check("two seedless calls differ", a != b, f"{a} == {b}")
    check("the seed is never stashed on the instance (OWUI shares one Pipe across chats)",
          "self._last_seed" not in src and "self.seed" not in src)

    print("--- the rewriter runs on the SDXL path, which is the one that needs it ---")
    p4 = mod.Pipe(); p4.valves.EDIT_ENGINE = "sdxl"
    got = build(p4, "undress this person", rewrite=("SOUTH ASIAN WOMAN, mid-20s, no top", "white woman, blonde"))
    check("the SDXL edit prompt is the REWRITTEN description",
          got["wf"]["6"]["inputs"]["text"] == "SOUTH ASIAN WOMAN, mid-20s, no top",
          repr(got["wf"]["6"]["inputs"]["text"]))
    check("the raw instruction does not reach CLIP", "undress this person" not in got["wf"]["6"]["inputs"]["text"])
    check("AVOID traits are appended to the negative", "white woman, blonde" in got["wf"]["7"]["inputs"]["text"])
    check("...on top of the base negative, not replacing it", "bad anatomy" in got["wf"]["7"]["inputs"]["text"])
    check("the old `if not ref_b64` rewrite skip is gone", "if not ref_b64:" not in src)
    check("the rewriter is a VISION call (it must see the source to restate traits)",
          '"images": [ref_b64]' in src)
    p5 = mod.Pipe(); p5.valves.EDIT_ENGINE = "sdxl"; p5.valves.REWRITE = False
    got = build(p5, "undress this person")
    check("REWRITE=False is honoured", got["wf"]["6"]["inputs"]["text"] == "undress this person")
    got = build(mod.Pipe(), "undress this person")
    check("Qwen does NOT get the SDXL tag rewrite (it takes instructions natively)",
          got["wf"]["pos"]["inputs"]["prompt"] == "undress this person")

    print("--- denoise: the fallback keeps its ceiling, the real fix does not use one ---")
    check("SDXL img2img denoise is unchanged at 0.65", mod.DENOISE == 0.65)
    check("...and never exceeds it", got_sdxl["wf"]["3"]["inputs"]["denoise"] <= 0.65)
    check("the Qwen path does not touch DENOISE at all", got["wf"]["k"]["inputs"]["denoise"] == 1.0)

    print("--- a downgrade is always admitted to in the status line ---")
    d = mod.Pipe._detail({"seed": 5, "engine": "qwen", "tier": "balanced", "downgrade": ""}, True)
    check("a Qwen edit names the engine and seed", "Qwen-Image-Edit" in d and "seed 5" in d, d)
    d = mod.Pipe._detail({"seed": 5, "engine": "sdxl", "downgrade": ""}, True)
    check("a chosen SDXL edit warns about drift", "drift" in d.lower(), d)
    d = mod.Pipe._detail({"seed": 5, "engine": "sdxl", "downgrade": "no module"}, True)
    check("a FORCED SDXL edit says why", "unavailable" in d.lower() and "drift" in d.lower(), d)
    d = mod.Pipe._detail({"seed": 5, "engine": "sdxl-t2i", "downgrade": ""}, False)
    check("text-to-image reports the seed too", "seed 5" in d, d)

    print("--- the pipe survives the shared module going missing ---")
    # It is copied into OpenWebUI's data volume by hand. If that copy is lost the pipe must fall
    # back to its old behaviour, not vanish from the model dropdown.
    check("the import is guarded", "except Exception as _e:" in src and "_SHARED_ERR" in src)
    check("...with a local parse_seed fallback", src.count("def parse_seed(") == 1)
    check("...and the engine downgrades rather than calling a missing builder",
          'if want == "qwen" and build_qwen_edit_wf is None' in src)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

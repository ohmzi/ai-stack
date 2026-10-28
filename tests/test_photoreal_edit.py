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

Added 2026-10-28, because edits still did not do what was asked: FireRed got the request as
typed, on the 8-step tier, and nothing looked at the result. Now FireRed gets the Assistant's
vision rewrite, every edit is checked against the request as typed with one corrected retry on
the full tier, a picture this pipe made is re-rendered from its own prompt and seed, and the
uncensored SDXL path is never used on an uploaded photo.

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


def build(pipe, prompt, ref="Zm9v", seed=None, rewrite=("DESCRIBED PERSON, edit", "drift-traits"),
          own=None, fire=("FIRERED INSTRUCTION", "avoid-traits"), verdicts=None, render=False):
    """Drive _generate far enough to capture the workflow it would submit, without a GPU.

    Nothing here reaches Ollama or ComfyUI: every network method is stubbed. `own` is the
    origin record the pipe would find for `ref` (None = an upload). With render=True every
    submit succeeds and every workflow is kept in `wfs`, so the check-and-retry loop runs;
    `verdicts` is the checker's answers, in order."""
    seen = {"wfs": [], "verify": []}
    answers = list(verdicts or [])
    pipe._free_vram = lambda *a, **k: None
    pipe._comfy_free = lambda *a, **k: None
    pipe._upload = lambda *a, **k: "ref.png"
    pipe._enhance = lambda p: f"ENHANCED {p}"
    pipe._enhance_edit = lambda i, r: rewrite
    pipe._rewrite_edit = lambda req, r: fire
    pipe._merge_prompt = lambda p, c: f"MERGED[{p}|{c}]"
    pipe._origin = lambda cid, b64: own

    def verify(original, instruction, ref_b64, result_b64):
        seen["verify"].append((original, instruction))
        return answers.pop(0) if answers else (True, "", True)
    pipe._verify_edit = verify

    def submit(wf, out_node):
        seen["wfs"].append(wf)
        seen.setdefault("wf", wf)
        seen.setdefault("out_node", out_node)
        return (b"PNG", None) if render else (None, "stop")
    pipe._submit_poll = submit
    md, meta = pipe._generate(prompt, ref, seed)
    seen.update(meta=meta, md=md)
    return seen


OWN = {"prompt": "ORIGINAL PROMPT", "seed": 4242}


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
          open(shared.__file__).read().count('"FireRed-Image-Edit-1.1-Lightning') == 1)

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
    check("the edit rewrite is on by default", p.valves.REWRITE is True)
    check("the check is on by default", p.valves.CHECK is True)
    check("own pictures are re-rendered by default", p.valves.OWN_PICTURES == "rerender")

    def own_edit(engine="qwen"):
        """A pipe that edits its own pictures like uploads, so the engine valve decides."""
        q = mod.Pipe(); q.valves.EDIT_ENGINE = engine; q.valves.OWN_PICTURES = "edit"
        return q

    print("--- the output node is derived from the graph, not hardcoded ---")
    # The original poller read outputs["9"] unconditionally. The Qwen graph has no node "9", so
    # this would have made every identity-preserving render report "produced no image".
    got = build(mod.Pipe(), "add a red hat")
    check("a Qwen edit polls node 's'", got["out_node"] == "s", repr(got.get("out_node")))
    check("...and really did build the Qwen graph", "TextEncodeQwenImageEditPlus" in
          {n.get("class_type") for n in got["wf"].values()})
    got_sdxl = build(own_edit("sdxl"), "add a red hat", own=OWN)
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
    got = build(own_edit("sdxl"), "add a red hat", seed=777, own=OWN)
    check("the SDXL sampler gets it too", got["wf"]["3"]["inputs"]["seed"] == 777)
    text, seed = shared.parse_seed("add a red hat seed 777")
    check("'seed 777' never reaches a text encoder", "777" not in text and "seed" not in text.lower())
    a = build(mod.Pipe(), "x")["wf"]["k"]["inputs"]["seed"]
    b = build(mod.Pipe(), "x")["wf"]["k"]["inputs"]["seed"]
    check("two seedless calls differ", a != b, f"{a} == {b}")
    check("the seed is never stashed on the instance (OWUI shares one Pipe across chats)",
          "self._last_seed" not in src and "self.seed" not in src)

    print("--- the SDXL path gets a description, FireRed gets a concrete instruction ---")
    ask = "give her a red jacket"
    got = build(own_edit("sdxl"), ask, own=OWN,
                rewrite=("SOUTH ASIAN WOMAN, mid-20s, red jacket", "white woman, blonde"))
    check("the SDXL edit prompt is the REWRITTEN description",
          got["wf"]["6"]["inputs"]["text"] == "SOUTH ASIAN WOMAN, mid-20s, red jacket",
          repr(got["wf"]["6"]["inputs"]["text"]))
    check("the raw instruction does not reach CLIP", ask not in got["wf"]["6"]["inputs"]["text"])
    check("AVOID traits are appended to the negative", "white woman, blonde" in got["wf"]["7"]["inputs"]["text"])
    check("...on top of the base negative, not replacing it", "bad anatomy" in got["wf"]["7"]["inputs"]["text"])
    check("the old `if not ref_b64` rewrite skip is gone", "if not ref_b64:" not in src)
    check("the rewriters are VISION calls (they must see the source)",
          src.count('"images": [ref_b64]') >= 2)
    p5 = own_edit("sdxl"); p5.valves.REWRITE = False
    got = build(p5, ask, own=OWN)
    check("REWRITE=False is honoured on SDXL", got["wf"]["6"]["inputs"]["text"] == ask)
    got = build(mod.Pipe(), ask, rewrite=("SDXL TAG SOUP", ""))
    check("FireRed gets the instruction rewrite, not the SDXL description",
          got["wf"]["pos"]["inputs"]["prompt"] == "FIRERED INSTRUCTION",
          repr(got["wf"]["pos"]["inputs"]["prompt"]))
    check("...and its AVOID traits reach the negative encoder",
          got["wf"]["neg"]["inputs"]["prompt"] == "avoid-traits")
    check("...and the meta says it was rewritten", got["meta"].get("rewrote") is True)
    p6 = mod.Pipe(); p6.valves.REWRITE = False
    got = build(p6, ask)
    check("REWRITE=False sends FireRed the request as typed", got["wf"]["pos"]["inputs"]["prompt"] == ask)
    check("the FireRed rewrite is the Assistant's vision rewrite",
          "ms.EDIT_REWRITE_VISION_SYS" in src)

    print("--- an uploaded photo never reaches the uncensored path ---")
    got = build(own_edit("sdxl"), "add a red hat", own=None)
    check("EDIT_ENGINE=sdxl on an upload still runs FireRed", got["out_node"] == "s", repr(got.get("out_node")))
    check("...and says why", got["meta"]["downgrade"] == "uploads always use FireRed", repr(got["meta"]))
    check("...in the status line too", "uploads always use FireRed" in mod.Pipe._detail(
        {**got["meta"], "tier": "balanced"}, True))
    saved = mod.build_qwen_edit_wf
    try:
        mod.build_qwen_edit_wf = None
        got = build(mod.Pipe(), "add a red hat", own=None)
        check("an upload with FireRed missing is refused, not sent to img2img",
              "wf" not in got and "needs FireRed" in (got["md"] or ""), repr(got["md"])[:120])
        got = build(own_edit("qwen"), "add a red hat", own=OWN)
        check("an OWN picture with FireRed missing still falls back to SDXL",
              got.get("out_node") == "9" and got["meta"]["downgrade"])
    finally:
        mod.build_qwen_edit_wf = saved

    print("--- a picture this pipe made is re-rendered from its own prompt and seed ---")
    got = build(mod.Pipe(), "make the jacket red", own=OWN)
    check("it is a text-to-image graph, not an img2img one",
          got["out_node"] == "9" and "20" not in got["wf"], repr(sorted(got["wf"])))
    check("...on the picture's own seed", got["wf"]["3"]["inputs"]["seed"] == 4242)
    check("...with the change folded into its own prompt",
          got["wf"]["6"]["inputs"]["text"] == "MERGED[ORIGINAL PROMPT|make the jacket red]",
          repr(got["wf"]["6"]["inputs"]["text"]))
    check("...and the status line says so", "re-render" in mod.Pipe._detail(got["meta"], True))
    got = build(mod.Pipe(), "make the jacket red", own=OWN, seed=7)
    check("a seed the user typed still wins", got["wf"]["3"]["inputs"]["seed"] == 7)
    got = build(mod.Pipe(), "make this picture animated", own=OWN)
    check("a whole-picture restyle of an own picture still goes to FireRed",
          got["out_node"] == "s" and got["meta"].get("tier") == "best")
    got = build(own_edit("qwen"), "make the jacket red", own=OWN)
    check("OWN_PICTURES=edit sends an own picture to FireRed", got["out_node"] == "s")
    got = build(mod.Pipe(), "make the jacket red", own=OWN, render=True)
    check("a re-render records its new prompt and seed for the next edit",
          got["meta"].get("origin") == {"prompt": "MERGED[ORIGINAL PROMPT|make the jacket red]",
                                        "seed": 4242}, repr(got["meta"].get("origin")))
    got = build(mod.Pipe(), "a portrait", ref=None, render=True)
    check("text-to-image records its prompt and seed",
          got["meta"].get("origin") == {"prompt": "ENHANCED a portrait", "seed": got["meta"]["seed"]})
    got = build(mod.Pipe(), "make the jacket red", render=True)
    check("a FireRed edit records no origin (it cannot be re-rendered from a prompt)",
          "origin" not in got["meta"])

    print("--- every edit is checked, and a miss gets one corrected retry ---")
    got = build(mod.Pipe(), ask, render=True)
    check("a pass on the first look costs one render", len(got["wfs"]) == 1 and got["meta"]["qa_rounds"] == 0)
    check("...and the checker judged the request as typed, not the rewrite",
          got["verify"] and got["verify"][0][0] == ask, repr(got["verify"]))
    check("...and nothing is added under the picture", got["md"].endswith(")"), got["md"][-40:])
    got = build(mod.Pipe(), ask, render=True,
                verdicts=[(False, "Make the jacket clearly red.", True), (True, "", True)])
    check("a miss renders once more", len(got["wfs"]) == 2, str(len(got["wfs"])))
    second = got["wfs"][-1]
    check("...on the full tier", second["k"]["inputs"]["cfg"] == 4.0 and second["k"]["inputs"]["steps"] == 20
          and "lora" not in second)
    check("...with the correction on the instruction",
          "IMPORTANT correction: Make the jacket clearly red." in second["pos"]["inputs"]["prompt"])
    check("...and it is counted", got["meta"]["qa_rounds"] == 1 and got["meta"]["tier"] == "best")
    check("...and the fixed result carries no warning", "check" not in got["md"].split(")")[-1].lower())
    got = build(mod.Pipe(), ask, render=True,
                verdicts=[(False, "Make the jacket red.", True), (False, "The jacket is still blue.", True)])
    check("only one retry", len(got["wfs"]) == 2)
    check("a miss that survives is reported under the picture",
          "still says: The jacket is still blue." in got["md"], got["md"][-80:])
    got = build(mod.Pipe(), ask, render=True, verdicts=[(False, "", True)])
    check("a miss with no correction is reported, not retried",
          len(got["wfs"]) == 1 and "gave no correction" in got["md"])
    p7 = mod.Pipe(); p7.valves.CHECK = False
    got = build(p7, ask, render=True, verdicts=[(False, "x", True)])
    check("CHECK=False skips the checker", not got["verify"] and len(got["wfs"]) == 1)
    got = build(mod.Pipe(), "make the jacket red", own=OWN, render=True,
                verdicts=[(False, "Make the jacket red.", True), (True, "", True)])
    check("a re-render retry folds the correction into the prompt and keeps the seed",
          len(got["wfs"]) == 2 and got["wfs"][1]["3"]["inputs"]["seed"] == 4242
          and got["wfs"][1]["6"]["inputs"]["text"].endswith("|Make the jacket red.]"))

    print("--- the checker's reply is read strictly but fails open ---")
    pv = mod.Pipe._parse_verdict
    check("JSON verdict", pv('{"ok": false, "fix": "Add the hat."}') == (False, "Add the hat.", True))
    check("markdown OK/FIX lines", pv("**OK:** no\n**FIX:** Add the hat.") == (False, "Add the hat.", True))
    check("'none' is no correction", pv('{"ok": true, "fix": "none"}') == (True, "", True))
    check("an unreadable reply passes, and says it was unreadable",
          pv("The user wants me to look at the colour...") == (True, "", False))

    print("--- where a picture came from is recorded per chat, by its bytes ---")
    import base64 as _b64
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        saved_dir = mod.ORIGIN_DIR
        mod.ORIGIN_DIR = d
        try:
            q = mod.Pipe()
            a = _b64.b64encode(b"picture-a").decode()
            b = _b64.b64encode(b"picture-b").decode()
            q._remember_origin("chat1", a, "PROMPT A", 11)
            r = q._origin("chat1", a)
            check("a recorded picture is found", r and r["prompt"] == "PROMPT A" and r["seed"] == 11)
            check("...only in its own chat", q._origin("chat2", a) is None)
            check("...and an upload with other bytes is not ours", q._origin("chat1", b) is None)
            for i in range(20):
                q._remember_origin("chat1", _b64.b64encode(f"p{i}".encode()).decode(), f"P{i}", i)
            check("each chat keeps only its newest pictures", q._origin("chat1", a) is None)
            check("no chat id records nothing", q._remember_origin(None, a, "x", 1) is None
                  and q._origin(None, a) is None)
        finally:
            mod.ORIGIN_DIR = saved_dir

    print("--- the prompt writer matches the model ---")
    check("Lustify V10 gets sentences, not the SDXL tag list",
          "sentences" in mod.ENHANCE_SYS and "comma-separated" not in mod.ENHANCE_SYS
          and "SDXL" not in mod.ENHANCE_SYS)
    check("a bracket in the label cannot break the image link",
          mod.Pipe._md(b"x", 1, "a [b] c\nd").startswith("![seed 1 · a b c d]("))

    print("--- denoise: the fallback keeps its ceiling, the real fix does not use one ---")
    check("SDXL img2img denoise is unchanged at 0.65", mod.DENOISE == 0.65)
    check("...and never exceeds it", got_sdxl["wf"]["3"]["inputs"]["denoise"] <= 0.65)
    check("the Qwen path does not touch DENOISE at all",
          build(mod.Pipe(), "add a red hat")["wf"]["k"]["inputs"]["denoise"] == 1.0)

    print("--- a downgrade is always admitted to in the status line ---")
    d = mod.Pipe._detail({"seed": 5, "engine": "qwen", "tier": "balanced", "downgrade": ""}, True)
    check("a FireRed edit names the engine and seed", "FireRed" in d and "seed 5" in d, d)
    d = mod.Pipe._detail({"seed": 5, "engine": "qwen", "tier": "best", "downgrade": "", "qa_rounds": 1}, True)
    check("...and a corrected retry", "1 corrected retry" in d, d)
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

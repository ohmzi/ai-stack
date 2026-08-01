#!/usr/bin/env python3
"""The instruction-edit speed tier must never silently disable a negative prompt someone relies on.

Why this file exists. The Lightning LoRA was rejected once already, on the belief that it made new
elements look pasted-on. Re-measured 2026-08-01 across 10 fixed-seed renders that premise turned out
to be backwards -- the full 20-step/cfg-4 path is the one that drifts off the source (1 seed in 3
recomposed the entire frame; grain fell to 84.5% of the original, against ~100% for the LoRA). So the
LoRA is now the default.

But it carries one real, total cost: the tier runs cfg 1.0, and at cfg 1.0 the negative prompt does
NOTHING. That is not a judgement call, it is measured -- the same seed with negative="" and with
negative="steam, smoke, vapour, mist" produced BYTE-IDENTICAL output (max abs pixel diff 0), while
the same pair at cfg 4.0 differed. _enhance_edit produces a negative on every single rewritten edit
(media_metrics.jsonl: 10 edits, zero avoid_missing rows), so "the negative is inert" is a live
condition on the default path, not a corner case.

Three callers genuinely depend on that negative and must therefore stay on the full path whatever
the valve says:
    style conversion   the restyle is DEFINED by what must not survive it
    _edit_boost        "you barely changed it" -- the retry exists because cfg was too low already
    QA correction      the automatic second chance; if it inherited the fast tier the fast tier
                       would have no backstop

That last one is what makes the fast default safe, so it is pinned here rather than left to
convention. cfg is also pinned to 1.0 for both LoRA tiers: at 2.5 the negative only half-bites and
costs 68 s, and at 4.0 the LoRA plus the uncond pass OOMs the 24 GB card outright (observed).

Usage:  python3 tests/test_edit_tiers.py [pipe_path]
"""
import importlib.util
import sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_tiers", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def build(pipe, prompt, style=None, enhanced=("EDIT", "AVOID-TRAITS")):
    """Drive _gen_image far enough to capture the workflow it would submit, without a GPU."""
    seen = {}
    pipe._comfy_free = lambda *a, **k: None
    pipe._free_vram = lambda *a, **k: None
    pipe._upload = lambda *a, **k: "ref.png"
    pipe._style_conversion = lambda p: style
    pipe._style_enrich = lambda i, m=None: i
    pipe._enhance_edit = lambda i, m=None: enhanced
    pipe._metric = lambda **k: None

    real = pipe._build_edit_wf

    def spy(instruction, name, seed, cfg, steps, negative, lightning=False):
        seen.update(cfg=cfg, steps=steps, negative=negative, lightning=lightning)
        return real(instruction, name, seed, cfg, steps, negative, lightning)

    pipe._build_edit_wf = spy
    pipe._submit_poll = lambda *a, **k: (None, "stop", None)   # halt right after the build
    pipe._gen_image(prompt, "Zm9v", None)
    return seen


def main():
    src = open(PIPE_PATH, encoding="utf-8").read()

    print("--- the tier table encodes the two hard constraints ---")
    for name, t in mod.EDIT_TIERS.items():
        if t["lightning"]:
            # cfg > 1 with the LoRA: 2.5 half-works and costs 68 s, 4.0 OOMs the card.
            check(f"'{name}' pins cfg to 1.0 because it uses the LoRA", t["cfg"] == 1.0, str(t))
    check("'best' keeps a cfg that lets the negative bite", mod.EDIT_TIERS["best"]["cfg"] > 1.0)
    check("'best' does not load the speed LoRA", mod.EDIT_TIERS["best"]["lightning"] is False)
    check("the LoRA file is named once, not per call site", src.count('"Qwen-Image-Edit-2509-Lightning') == 1)

    print("--- the pipe finally has Valves, and the tier knob is one of them ---")
    check("Pipe declares a Valves model", hasattr(mod.Pipe, "Valves"))
    p = mod.Pipe()
    check("instances carry a valves instance", hasattr(p, "valves"))
    check("the default is a real tier", p.valves.EDIT_QUALITY in mod.EDIT_TIERS,
          repr(p.valves.EDIT_QUALITY))
    check("the default is the measured-faster one", p.valves.EDIT_QUALITY == "balanced")

    print("--- an unreadable valve degrades to full quality, never to a silent no-negative ---")
    for bad in ("nonsense", "", "  ", "BEST!", None):
        p.valves.EDIT_QUALITY = bad
        check(f"{bad!r} falls back to 'best'", p._edit_tier() == mod.EDIT_TIERS["best"])
    p.valves.EDIT_QUALITY = "BaLaNcEd"
    check("tier names are case- and whitespace-insensitive",
          p._edit_tier() == mod.EDIT_TIERS["balanced"])

    print("--- the LoRA node is wired in only when asked, and rewires the chain when it is ---")
    p.valves.EDIT_QUALITY = "balanced"
    lit = p._build_edit_wf("i", "n.png", 1, 1.0, 8, "", True)
    full = p._build_edit_wf("i", "n.png", 1, 4.0, 20, "neg", False)
    check("lightning inserts a LoraLoaderModelOnly", "lora" in lit)
    check("...between the unet and ModelSamplingAuraFlow",
          lit["msaf"]["inputs"]["model"] == ["lora", 0] and lit["lora"]["inputs"]["model"] == ["u", 0])
    check("...at full strength", lit["lora"]["inputs"]["strength_model"] == 1.0)
    check("the full path has no LoRA node at all", "lora" not in full)
    check("...and samples straight off the unet", full["msaf"]["inputs"]["model"] == ["u", 0])
    check("both tiers still save from the same node id the poller reads", "s" in lit and "s" in full)

    print("--- callers that depend on the negative prompt keep the full path ---")
    p.valves.EDIT_QUALITY = "fast"        # most aggressive tier, to prove the overrides are real
    got = build(mod.Pipe(), "turn this into a watercolour painting",
                style=("Restyle as watercolour.", "photo, 3d render"))
    check("a style conversion ignores the tier valve", got.get("lightning") is False, str(got))
    check("...and keeps cfg above 1 so its negative works", got.get("cfg", 0) > 1.0, str(got))

    p2 = mod.Pipe()
    p2.valves.EDIT_QUALITY = "fast"
    got = build(p2, "you barely changed it, make him much older")
    check("an _edit_boost retry ignores the tier valve", got.get("lightning") is False, str(got))
    check("...and pushes cfg to 6.0", got.get("cfg") == 6.0, str(got))

    p3 = mod.Pipe()
    p3.valves.EDIT_QUALITY = "fast"
    got = build(p3, "add a red hat")
    check("an ordinary edit DOES take the tier", got.get("lightning") is True, str(got))
    check("...with the tier's step count", got.get("steps") == 4, str(got))

    p4 = mod.Pipe()
    p4.valves.EDIT_QUALITY = "best"
    got = build(p4, "add a red hat")
    check("...and honours 'best' when set back", got.get("lightning") is False, str(got))
    check("...at 20 steps / cfg 4", (got.get("steps"), got.get("cfg")) == (20, 4.0), str(got))

    print("--- QA correction is the backstop, so it must never inherit the fast tier ---")
    # Both correction call sites pass 6.0, 24 positionally and omit `lightning`. If either ever
    # grows a lightning argument the fast default loses its safety net silently.
    calls = [ln for ln in src.splitlines() if "_build_edit_wf(" in ln and "def " not in ln]
    check("every correction call site found", len(calls) >= 2, str(len(calls)))
    for ln in calls:
        if "6.0, 24" in ln:
            check("a QA correction call passes no lightning flag", "lightning" not in ln, ln.strip())
    check("the default parameter is the safe one",
          "negative, lightning=False" in src)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

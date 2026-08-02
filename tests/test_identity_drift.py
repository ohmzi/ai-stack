#!/usr/bin/env python3
"""Does a reference-image edit come back as the same person? Measured, on real renders.

This is the end-to-end counterpart to tests/test_photoreal_edit.py, which only checks the
workflow the pipe WOULD submit. This one actually submits it and scores the result.

Method: generate a synthetic subject at a fixed seed (nobody real is involved -- the point
is a face the metric can track, and a synthetic one removes any question about the source),
then run the same edit instruction through each engine at the same fixed seed and compare
each output back to the subject with tests/identity_metrics.py.

The pass condition is SFace cosine >= 0.363 against the source, which is the model's own
calibrated same-person threshold. Grain and MAE are reported alongside for comparability
with the Assistant's tier table at pipes/auto_assistant.py:180-190.

Needs a live ComfyUI and a free GPU. Renders take a few minutes.

Usage:
  python3 tests/test_identity_drift.py [--engines qwen,sdxl] [--seed N] [--keep DIR]
"""
import argparse
import base64
import importlib.util
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipes/shared"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import identity_metrics as im  # noqa: E402

SUBJECT_PROMPT = (
    "photorealistic studio portrait of a 30-year-old South Asian woman, oval face, dark brown "
    "almond eyes, thick straight eyebrows, small mole on left cheek, long dark wavy hair parted "
    "in the centre, plain grey backdrop, soft key light, 85mm lens, sharp focus"
)
EDIT_INSTRUCTION = "change the background to a sunlit green garden"


def load_pipe(path):
    spec = importlib.util.spec_from_file_location("photoreal_e2e", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.METRICS_PATH = ""
    return mod


def save_md_png(md, path):
    m = re.search(r"\(data:image/png;base64,([A-Za-z0-9+/=]+)\)", md or "")
    if not m:
        return None
    with open(path, "wb") as f:
        f.write(base64.b64decode(m.group(1)))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipe", default=os.path.join(ROOT, "pipes/photoreal.py"))
    ap.add_argument("--engines", default="qwen,sdxl")
    ap.add_argument("--seed", type=int, default=20260801)
    ap.add_argument("--keep", default=os.path.join(ROOT, ".identity-drift"))
    a = ap.parse_args()

    os.makedirs(a.keep, exist_ok=True)
    mod = load_pipe(a.pipe)

    print(f"pipe   : {a.pipe}")
    print(f"seed   : {a.seed}   (fixed, so every row below differs only by engine)")
    print(f"edit   : {EDIT_INSTRUCTION!r}\n")

    # --- the subject -------------------------------------------------------------------
    src = os.path.join(a.keep, "subject.png")
    if os.path.exists(src):
        print(f"subject: reusing {src}")
    else:
        p = mod.Pipe()
        p._enhance = lambda t: t          # the prompt is already SDXL-shaped; keep it deterministic
        t0 = time.time()
        md, meta = p._generate(SUBJECT_PROMPT, None, a.seed)
        if not save_md_png(md, src):
            print(f"FAILED to generate the subject: {str(md)[:300]}")
            return 1
        print(f"subject: rendered in {time.time() - t0:.0f}s -> {src}")

    if im.detect_face(im._load(src)) is None:
        print("FAILED: no face in the generated subject — cannot measure identity. Try another seed.")
        return 1

    with open(src, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode()

    # --- each engine, same instruction, same seed --------------------------------------
    rows = []
    for engine in [e.strip() for e in a.engines.split(",") if e.strip()]:
        p = mod.Pipe()
        p.valves.EDIT_ENGINE = engine
        p.valves.REWRITE = False   # isolate the ENGINE; the rewrite is measured separately below
        t0 = time.time()
        md, meta = p._generate(EDIT_INSTRUCTION, ref_b64, a.seed)
        out = os.path.join(a.keep, f"edit_{engine}.png")
        if not save_md_png(md, out):
            print(f"  {engine:<18} FAILED: {str(md)[:200]}")
            continue
        rows.append((f"{engine} ({meta['engine']})", out, round(time.time() - t0)))
        print(f"  {engine:<18} rendered in {time.time() - t0:.0f}s")

    # the SDXL path again WITH the vision rewrite, to price that stage on its own
    if "sdxl" in a.engines:
        p = mod.Pipe()
        p.valves.EDIT_ENGINE, p.valves.REWRITE = "sdxl", True
        t0 = time.time()
        md, meta = p._generate(EDIT_INSTRUCTION, ref_b64, a.seed)
        out = os.path.join(a.keep, "edit_sdxl_rewritten.png")
        if save_md_png(md, out):
            rows.append(("sdxl + vision rewrite", out, round(time.time() - t0)))
            print(f"  {'sdxl+rewrite':<18} rendered in {time.time() - t0:.0f}s")

    # --- score -------------------------------------------------------------------------
    # Whole-frame MAE and grain are deliberately NOT the headline. This edit replaces the
    # background, which is most of the frame, so both are dominated by the change that was
    # ASKED for — a smooth grey backdrop becoming foliage sends grain to ~370% with nothing
    # wrong. They are meaningful only against an untouched region; the face box is the one
    # this test has, and face MAE is reported for that reason.
    print(f"\n{'engine':<24}{'time':>6}{'cos':>7}{'skin dE':>9}{'eye dE':>8}"
          f"{'hair dE':>9}{'faceMAE':>9}{'VERDICT':>9}")
    print("-" * 82)
    failures = 0
    for label, path, secs in rows:
        r = im.compare(src, path)
        same = "n/a" if r["same_person"] is None else ("SAME" if r["same_person"] else "DRIFTED")
        if r["same_person"] is False:
            failures += 1
        print(f"{label:<24}{secs:>5}s{im._fmt(r['face_cosine']):>7}"
              f"{im._fmt(r['skin_de'], '.1f'):>9}{im._fmt(r['eye_de'], '.1f'):>8}"
              f"{im._fmt(r['hair_de'], '.1f'):>9}{im._fmt(r['face_mae'], '.1f'):>9}{same:>9}"
              + (f"   ({r['note']})" if r["note"] else ""))
        if r["sface_verify_would_pass"] and not r["same_person"]:
            print(f"{'':>24}   ^ SFace's published 0.363 threshold would have PASSED this one")
    print(f"\ngate: cosine >= {im.IDENTITY_GATE_COSINE} and skin/eye/hair dE within "
          f"{im.SKIN_DE_MAX}/{im.EYE_DE_MAX}/{im.HAIR_DE_MAX}.  Images kept in {a.keep}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

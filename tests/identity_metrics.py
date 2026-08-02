#!/usr/bin/env python3
"""Objective identity-drift metrics for image edits.

WHY THIS EXISTS
---------------
"It came back as a different person" was, until now, an opinion. Every attempt to fix the
edit path could only be judged by eye, on a random seed, against a memory of the last one.
That is why the seed had to be surfaced first (pipes/photoreal.py) and why this file is the
second half of the same change: fixed seed in, a number out.

The headline metric is a face-embedding cosine distance, which is the only one that
measures the thing actually being complained about. It does NOT need InsightFace,
onnxruntime, or a GPU: OpenCV 4.13 ships FaceDetectorYN (YuNet) and FaceRecognizerSF
(SFace) and reads ONNX through its own DNN module. That matters beyond convenience -- the
onnxruntime/CUDA stack is exactly what risks destabilising the ComfyUI container, and
keeping the measurement host-side means the risky dependency never goes near the renderer.

SFace publishes a calibrated same-person threshold of 0.363 cosine, so this gives an
absolute pass/fail rather than a relative eyeball.

The other two metrics reuse the methodology already established in this repo at
pipes/auto_assistant.py:180-190, so numbers here are comparable with the ones recorded
there for the Assistant's edit tiers:
  untouched-region MAE  did the edit stay in its lane
  grain kept %          high-frequency energy ratio; catches the plastic/smoothed failure
                        that MAE misses (it is what exposed the 'best' tier at 84.5%)

MODELS
------
Two small ONNX files, host-side only, from github.com/opencv/opencv_zoo:
  face_detection_yunet_2023mar.onnx    (228 KB)
  face_recognition_sface_2021dec.onnx  (37 MB)
Default location ~/.cache/identity-metrics, override with IDENTITY_MODEL_DIR.

Usage:
  python3 tests/identity_metrics.py SOURCE.png OUTPUT.png [OUTPUT2.png ...]
"""
import os
import sys

import numpy as np

try:
    import cv2
except Exception as e:  # pragma: no cover - the harness is useless without it
    print(f"opencv is required: {e}", file=sys.stderr)
    raise

MODEL_DIR = os.environ.get("IDENTITY_MODEL_DIR",
                           os.path.expanduser("~/.cache/identity-metrics"))
YUNET = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
SFACE = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")

# SFace's PUBLISHED verification threshold. Documented here because it is what every tutorial
# quotes -- and because it is the wrong number for this job, which cost a measurement to learn.
#
# It is calibrated for face VERIFICATION: "are these two photographs of the same person",
# tuned for a low false-reject rate across real-world lighting, angle and age variation. It
# is therefore extremely permissive about anything that is not facial geometry.
#
# Generative drift is a different failure. Observed on this box 2026-08-01, seed 20260801:
# an SDXL img2img edit returned a face with different ethnicity, green eyes instead of dark
# brown, light-brown highlighted hair instead of near-black, and no mole -- unmistakably a
# different person to any human -- and SFace scored it 0.753, comfortably "same person" by
# this threshold. The embedding encodes geometry and texture; the model had kept the gross
# geometry and changed everything a person actually recognises.
SFACE_VERIFY_COSINE = 0.363

# The gate this harness actually applies. Empirical, from the runs recorded in
# tests/eval/identity_baseline.json: an edit that preserves identity scores ~0.95+, the
# obviously-drifted render above scored 0.753, and a vision-rewritten SDXL render that a
# human reads as the same person scored 0.853. 0.90 separates those cleanly with margin.
#
# Cosine alone is NOT sufficient and must never be the only gate -- see the colour deltas
# below, which are what caught the 0.753 case for what it was.
IDENTITY_GATE_COSINE = 0.90

# CIE Lab delta-E ceilings for the colour channels SFace is blind to. dE76 rule of thumb:
# <2.3 imperceptible, 2-10 perceptible, >10 plainly a different colour.
SKIN_DE_MAX, EYE_DE_MAX, HAIR_DE_MAX = 12.0, 12.0, 14.0


def _load(path):
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


def detect_face(img, detector=None):
    """Largest face as (x, y, w, h) plus the 5-point landmark row SFace needs, or None."""
    h, w = img.shape[:2]
    det = detector or cv2.FaceDetectorYN.create(YUNET, "", (w, h), 0.6, 0.3, 5000)
    det.setInputSize((w, h))
    n, faces = det.detect(img)
    if faces is None or len(faces) == 0:
        return None
    # Largest by area — the subject, not a bystander in the background.
    return max(faces, key=lambda f: f[2] * f[3])


def face_cosine(src_path, out_path):
    """(cosine, note). cosine is None when a face could not be found in one of the images.

    A missing face is reported rather than scored as 0.0: "the detector could not see a
    face" and "the face changed completely" are different findings with different fixes,
    and collapsing them would make a cropped or profile shot look like catastrophic drift.
    """
    a, b = _load(src_path), _load(out_path)
    fa, fb = detect_face(a), detect_face(b)
    if fa is None or fb is None:
        which = "source" if fa is None else "output"
        return None, f"no face detected in {which}"
    rec = cv2.FaceRecognizerSF.create(SFACE, "")
    ea = rec.feature(rec.alignCrop(a, fa)).flatten()
    eb = rec.feature(rec.alignCrop(b, fb)).flatten()
    # Cosine computed directly rather than via rec.match(..., DisType_FR_COSINE): this OpenCV
    # build exposes neither the enum constant nor the DisType class, and the integer that
    # would replace it (0) is an unlabelled magic number in a place where getting it wrong
    # silently swaps in Euclidean distance — same call, same type, completely different scale.
    # Verified identical to rec.match(ea, eb, 0) to 6 dp.
    na, nb = np.linalg.norm(ea), np.linalg.norm(eb)
    if na == 0 or nb == 0:
        return None, "degenerate face embedding"
    return float(np.dot(ea, eb) / (na * nb)), ""


def _as_float_gray(img, shape=None):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    if shape is not None and g.shape != shape:
        g = cv2.resize(g, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
    return g


def whole_mae(src_path, out_path):
    """Mean absolute pixel difference over the whole frame, 0-255."""
    a = _as_float_gray(_load(src_path))
    b = _as_float_gray(_load(out_path), a.shape)
    return float(np.abs(a - b).mean())


def face_mae(src_path, out_path):
    """MAE restricted to the source's face box — crude, free, and stable."""
    a_img, b_img = _load(src_path), _load(out_path)
    f = detect_face(a_img)
    if f is None:
        return None
    x, y, w, h = (int(v) for v in f[:4])
    a = _as_float_gray(a_img)
    b = _as_float_gray(b_img, a.shape)
    x, y = max(0, x), max(0, y)
    w, h = min(w, a.shape[1] - x), min(h, a.shape[0] - y)
    if w <= 0 or h <= 0:
        return None
    return float(np.abs(a[y:y + h, x:x + w] - b[y:y + h, x:x + w]).mean())


def grain_kept(src_path, out_path):
    """High-frequency energy of the output as a % of the source's.

    ~100% means the render preserved the photograph's texture. Well under 100% is the
    'plastic' failure — the image is smoothed toward the model's prior — which MAE does not
    see, because a uniformly smoothed image can still be close in mean absolute terms.
    """
    a = _as_float_gray(_load(src_path))
    b = _as_float_gray(_load(out_path), a.shape)
    hi_a = a - cv2.blur(a, (7, 7))
    hi_b = b - cv2.blur(b, (7, 7))
    if hi_a.std() == 0:
        return None
    return float(100.0 * hi_b.std() / hi_a.std())


def _lab(img):
    """True CIE Lab floats. OpenCV's 8-bit Lab is scaled (L 0-255, a/b offset by 128), so a
    delta-E computed on the raw channels is ~2.55x too large on L and needs the offset removed."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    return lab


def _patch_de(lab_a, lab_b, cx, cy, r):
    """delta-E76 between the mean colour of the same small patch in two images."""
    h, w = lab_a.shape[:2]
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r))
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r))
    if x1 <= x0 or y1 <= y0:
        return None
    ma = lab_a[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    mb = lab_b[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    return float(np.linalg.norm(ma - mb))


def _patch_mean(lab, cx, cy, r):
    h, w = lab.shape[:2]
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r))
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r))
    if x1 <= x0 or y1 <= y0:
        return None
    return lab[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)


def colour_deltas(src_path, out_path):
    """{skin, eye, hair} identity-colour drift, corrected for illumination.

    These exist because the face embedding is nearly blind to them, and they are most of what
    a person means by "that's not them". YuNet returns 5 landmarks after the bounding box --
    right eye, left eye, nose, right mouth corner, left mouth corner -- which is exactly
    enough to sample the eyes without a second model.

    The correction is the whole trick, and a naive absolute delta-E does NOT work: measured on
    this box, a Qwen edit that moved the subject into a sunlit garden scored skin dE 17.2 and
    eye dE 14.4 while being unmistakably the same person, because warm sunlight moves every
    facial colour at once. Absolute colour distance cannot tell "different eyes" from
    "different light".

    So eye and hair are measured RELATIVE TO THE SKIN in the same image: how far the eye sits
    from the cheek in Lab, then how much that offset changed. An illuminant shift moves both
    endpoints together and cancels; brown eyes becoming green does not. Skin itself is scored
    by hue angle only, which survives an intensity/warmth change far better than full Lab
    distance while still moving when ethnicity does.
    """
    out = {"skin": None, "eye": None, "hair": None}
    a_img, b_img = _load(src_path), _load(out_path)
    fa, fb = detect_face(a_img), detect_face(b_img)
    if fa is None or fb is None:
        return out

    def sample(img, f):
        """(skin_lab, [iris_lab...], hair_lab) at THIS image's own landmarks.

        Sampling both images at the SOURCE's coordinates is the obvious implementation and it
        is wrong: an edit that shifts or rescales the face by a few percent then reads eyelid
        or brow instead of iris in the output. Measured -- it scored a correct Qwen edit worse
        on eye colour (30.4) than a render that had genuinely turned the eyes green (11.0),
        i.e. exactly backwards. Every patch below is therefore placed off the face box and
        landmarks detected in the image it is being taken from.
        """
        lab = _lab(img)
        x, y, w, h = (float(v) for v in f[:4])
        nose_x, nose_y = f[8], f[9]
        r = max(4, int(w * 0.06))
        cheeks = [p for p in (_patch_mean(lab, nose_x - w * 0.25, nose_y + h * 0.05, r),
                              _patch_mean(lab, nose_x + w * 0.25, nose_y + h * 0.05, r))
                  if p is not None]
        skin = None if not cheeks else np.mean(cheeks, axis=0)
        er = max(2, int(w * 0.02))
        iris = [p for p in (_patch_mean(lab, f[4], f[5], er), _patch_mean(lab, f[6], f[7], er))
                if p is not None]
        hair = _patch_mean(lab, x + w * 0.5, y - h * 0.18, max(6, int(w * 0.18)))
        return skin, iris, hair

    sa, ia, hra = sample(a_img, fa)
    sb, ib, hrb = sample(b_img, fb)
    if sa is None or sb is None:
        return out

    # Skin: hue-angle shift in degrees. Ethnicity drift rotates it; warmer light mostly does not.
    ha = np.degrees(np.arctan2(sa[2], sa[1]))
    hb = np.degrees(np.arctan2(sb[2], sb[1]))
    out["skin"] = float(abs((hb - ha + 180) % 360 - 180))

    # Eyes and hair: offset from that image's own skin, so an illuminant shift cancels.
    if ia and ib:
        out["eye"] = float(np.linalg.norm((np.mean(ib, axis=0) - sb) - (np.mean(ia, axis=0) - sa)))
    if hra is not None and hrb is not None:
        out["hair"] = float(np.linalg.norm((hrb - sb) - (hra - sa)))
    return out


def compare(src_path, out_path):
    cos, note = face_cosine(src_path, out_path)
    cd = colour_deltas(src_path, out_path)
    geom_ok = None if cos is None else cos >= IDENTITY_GATE_COSINE
    colour_ok = all(v is None or v <= lim for v, lim in
                    ((cd["skin"], SKIN_DE_MAX), (cd["eye"], EYE_DE_MAX), (cd["hair"], HAIR_DE_MAX)))
    return {
        "face_cosine": cos,
        "geometry_ok": geom_ok,
        # Both must hold. Geometry alone passed a render whose ethnicity, eye colour and hair
        # colour had all changed; colour alone would pass a correctly-coloured stranger.
        "same_person": (None if geom_ok is None else (geom_ok and colour_ok)),
        "sface_verify_would_pass": (None if cos is None else cos >= SFACE_VERIFY_COSINE),
        "note": note,
        "skin_de": cd["skin"],
        "eye_de": cd["eye"],
        "hair_de": cd["hair"],
        "face_mae": face_mae(src_path, out_path),
        "whole_mae": whole_mae(src_path, out_path),
        "grain_kept": grain_kept(src_path, out_path),
    }


def _fmt(v, spec=".3f"):
    return "  n/a " if v is None else format(v, spec)


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip().rsplit("Usage:", 1)[-1].strip())
        return 2
    src, outs = argv[1], argv[2:]
    print(f"source: {src}\n")
    print(f"{'output':<30}{'cos':>7}{'skin dE':>9}{'eye dE':>8}{'hair dE':>9}{'VERDICT':>9}")
    print("-" * 72)
    for o in outs:
        r = compare(src, o)
        same = "n/a" if r["same_person"] is None else ("SAME" if r["same_person"] else "DRIFTED")
        print(f"{os.path.basename(o):<30}{_fmt(r['face_cosine']):>7}"
              f"{_fmt(r['skin_de'], '.1f'):>9}{_fmt(r['eye_de'], '.1f'):>8}"
              f"{_fmt(r['hair_de'], '.1f'):>9}{same:>9}"
              + (f"   ({r['note']})" if r["note"] else ""))
    print(f"\ngate: cosine >= {IDENTITY_GATE_COSINE} AND skin dE <= {SKIN_DE_MAX} "
          f"AND eye dE <= {EYE_DE_MAX} AND hair dE <= {HAIR_DE_MAX}")
    print(f"(SFace's published verification threshold of {SFACE_VERIFY_COSINE} is deliberately "
          f"NOT the gate — see the note in this file.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

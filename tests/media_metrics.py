#!/usr/bin/env python3
"""Summarise the media job metrics the assistant pipe writes.

Why this exists. Image latency here is bimodal and the split is invisible from the outside. The same
"red bicycle against a blue door" prompt was measured at 28.2 s, 32.3 s, 43.6 s and 196.9 s across
four runs on 2026-07-28. The 196.9 s run was a 16.5 s Krea 2 render followed by a 154.8 s
Qwen-Image-Edit pass — the vision-QA correction loop firing on an image that then scored 4/4 on
independent VQA. Nothing recorded that it had fired, so "is the QA loop worth its cost" could only
be argued, never answered.

`_metric()` in the pipe now writes one JSON line per finished media job. This reads them and answers
the two questions that matter:

  * how often does QA correct, and what does a correction cost in seconds
  * which requests keep getting flagged (the `qa_fix` text), i.e. is the verifier finding real
    faults or inventing them

Percentiles are reported rather than means: the mean of a bimodal distribution describes nothing
that ever happens. p50 is the experience; p90 is the complaint.

Usage:  python3 tests/media_metrics.py [--file PATH] [--last N]
"""
import argparse, json, os, sys
from collections import Counter

DEFAULT = "/volume1/docker/openwebui/config/media_metrics.jsonl"


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    # Nearest-rank: with a handful of samples, interpolating invents precision that isn't there.
    return s[min(len(s) - 1, max(0, int(round(p / 100 * len(s) + 0.5)) - 1))]


def fmt(v):
    return "-" if v is None else f"{v:.1f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.environ.get("MEDIA_METRICS", DEFAULT))
    ap.add_argument("--last", type=int, default=0, help="only the N most recent jobs")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        print(f"no metrics file at {a.file}")
        print("It appears once the pipe completes a media job with METRICS_PATH set.")
        print("Note the path is inside the open-webui container (/app/backend/data/...); from the")
        print("host that is the mounted config dir. Pass --file to point at it directly.")
        return 2

    rows = []
    for line in open(a.file):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if a.last:
        rows = rows[-a.last:]
    if not rows:
        print("metrics file is empty")
        return 0

    print(f"file : {a.file}")
    print(f"jobs : {len(rows)}   {rows[0].get('ts')} .. {rows[-1].get('ts')}\n")

    for job in sorted({r.get("job", "?") for r in rows}):
        js = [r for r in rows if r.get("job") == job]
        ok = [r for r in js if r.get("ok")]
        failed = len(js) - len(ok)
        renders = [r["render_s"] for r in ok if isinstance(r.get("render_s"), (int, float))]
        corrected = [r for r in ok if (r.get("qa_rounds") or 0) > 0]
        clean = [r for r in ok if not (r.get("qa_rounds") or 0)]

        def total(r):
            return (r.get("render_s") or 0) + (r.get("qa_s") or 0)

        print(f"=== {job}  ({len(js)} jobs, {failed} failed)")
        print(f"  render          p50 {fmt(pct(renders, 50))}   p90 {fmt(pct(renders, 90))}")
        if ok:
            rate = 100 * len(corrected) / len(ok)
            print(f"  QA corrected    {len(corrected)}/{len(ok)} jobs ({rate:.0f}%)"
                  + (f", {sum(r['qa_rounds'] for r in corrected)} rounds total" if corrected else ""))
            t_clean = [total(r) for r in clean]
            t_corr = [total(r) for r in corrected]
            print(f"  total, clean    p50 {fmt(pct(t_clean, 50))}   p90 {fmt(pct(t_clean, 90))}"
                  f"   (n={len(clean)})")
            print(f"  total, corrected p50 {fmt(pct(t_corr, 50))}   p90 {fmt(pct(t_corr, 90))}"
                  f"   (n={len(corrected)})")
            if t_clean and t_corr:
                # The number the gating decision turns on: what a correction actually buys, in
                # seconds, against an image the user might have been happy with.
                print(f"  correction cost  +{pct(t_corr, 50) - pct(t_clean, 50):.0f}s at the median")
        if corrected:
            print("  most common QA complaints:")
            for text, n in Counter(r.get("qa_fix", "")[:90] for r in corrected).most_common(5):
                print(f"    {n:3}x  {text}")
        print()
    return 0


sys.exit(main())

#!/usr/bin/env python3
"""Media intent predicates: do NOT start a GPU render on ordinary conversation.

Why this file exists. The eval suite and test_router both covered media routing, but every single
positive case in them was an explicit imperative ("make a picture of a cat"), and every negative
avoided media vocabulary entirely. That is a systematic blind spot: nothing tested what happens when
someone simply MENTIONS a picture or a video in passing.

Measured against the real Pipe before the fix, 8 of 10 ordinary sentences started a render —
"I watched a great video about sourdough yesterday" queued a Wan job. Each false render costs 1-5
GPU-minutes with _GEN_LOCK held, blocking every other request on the box.

The predicates are therefore DEFAULT-DENY: a render requires either an explicit generation verb near
a media noun, an imperative generation verb, or an explicit /img and /vid prefix. A bare mention is
conversation.

Usage:  python3 tests/test_media_intent.py [pipe_path]
"""
import importlib.util, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_media", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Must render an IMAGE.
IMAGE_YES = [
    "make a picture of a cat",
    "create an image of a mountain at sunset",
    "generate a logo for a coffee shop",
    "draw me a picture of a sunset",
    "draw a cat wearing a hat",
    "sketch a robot holding a flower",
    "paint a stormy sea in oils",
    "illustrate a fox reading a book",
    "can you make an image of a red bicycle",
    "i want a portrait of a wolf",
    "show me a picture of a futuristic city",
    "render a still life with fruit",
    "/img a lighthouse in a storm",
]

# Must render a VIDEO.
VIDEO_YES = [
    "create a video of a dog running",
    "make a short clip of rain on a window",
    "generate an animation of a rocket launching",
    "animate this",
    "animate the picture",
    "bring it to life",
    "/vid a balloon floating away",
]

# Must NOT render anything. Ordinary conversation that merely mentions media.
NEITHER = [
    "I watched a great video about sourdough yesterday",
    "my kid loves to draw",
    "that painting in the hallway is crooked",
    "the animation in that film was gorgeous",
    "can you show me the footage from the meeting notes?",
    "I need to sketch out a plan for the weekend",
    "there's a photo of my grandmother on the shelf",
    "the picture quality on the old TV is terrible",
    "she wants to illustrate a children's book someday",
    "we should paint the fence this summer",
    "what is the capital of Australia?",
    "the video card in this machine is a 3090",
    "he draws a distinction between the two ideas",
    "summarise this document for me",
    "my drawing skills are terrible",
    "explain how animation studios budget a film",
    "I'm painting the spare room beige",
    "send me the photo of the invoice when you can",
    "video games were better in the nineties",
    "that clip art looks dated",
]

results = []


def check(name, got, want):
    results.append((name, got == want, f"expected {want}, got {got}"))


def main():
    print(f"Testing: {PIPE_PATH}\n")
    p = mod.Pipe()

    print("--- must render an IMAGE ---")
    for s in IMAGE_YES:
        got = p._is_image_request(s)
        check(f"IMG  {s[:56]}", got, True)
        print(f"  [{'PASS' if got else 'FAIL'}] {s[:66]}")

    print("\n--- must render a VIDEO ---")
    for s in VIDEO_YES:
        got = p._is_video_request(s) or p._wants_new_video(s)
        check(f"VID  {s[:56]}", got, True)
        print(f"  [{'PASS' if got else 'FAIL'}] {s[:66]}")

    print("\n--- must NOT render (ordinary conversation) ---")
    for s in NEITHER:
        i, v = p._is_image_request(s), p._is_video_request(s)
        got = i or v
        check(f"NONE {s[:56]}", got, False)
        which = ("image" if i else "") + ("video" if v else "")
        print(f"  [{'PASS' if not got else 'FAIL'}] {s[:60]}" + (f"   <-- would render {which}" if got else ""))

    fails = sum(1 for _, ok, _ in results if not ok)
    npos = len(IMAGE_YES) + len(VIDEO_YES)
    print(f"\n{len(results)} checks ({npos} positive, {len(NEITHER)} negative) — "
          f"{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

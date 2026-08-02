#!/usr/bin/env python3
"""Conversation continuity after an image: the follow-up must edit THE picture, and
OpenWebUI background tasks must never render.

Why this file exists. On 2026-08-02 "make this picture realistic" after a cat image
returned a photo of a father and son. media_metrics.jsonl showed the chain: '### Task:'
background prompts (titles/follow-ups/tags/search decisions) were running as real GPU
jobs through the pipes and overwriting the per-chat last-image cache; the user's real
follow-up then edited the junk. Separately, photoreal ran "make this picture animated"
as a fresh t2i (a random stranger), image_krea routed "make this picture realistic" to
a fresh t2i of the literal words, and vision-QA — which judged only the rewritten
instruction against the produced image — passed all of it with qa_rounds=0.

These tests pin the routing and recovery layers of the fix on the host, no GPU needed:
task detection, reference recovery across every OpenWebUI 0.10 message shape, the
persistent per-chat store, style-conversion detection, and the edit/question guards in
all three pipes. The GPU-level behaviour is covered by tests/qa_live.py-style E2E runs.

Usage:  python3 tests/test_continuation.py
"""
import asyncio
import base64
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = load("media_session", "pipes/shared/media_session.py")
krea = load("krea_pipe", "pipes/image_krea.py")
photo = load("photo_pipe", "pipes/photoreal.py")
aa = load("aa_pipe", "pipes/auto_assistant.py")
# On the host the /app/backend/data sidecar import fails and each pipe degrades to its
# fallbacks. The deployed state has the sidecar — inject it so we test THAT wiring.
krea.ms = photo.ms = aa.ms = ms

PNG1 = base64.b64encode(b"\x89PNG-first-image").decode()
PNG2 = base64.b64encode(b"\x89PNG-second-image").decode()

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# ---------------------------------------------------------------- task detection
print("task detection:")
check("'### Task:' prefix is a task", ms.is_task_request("### Task:\nSuggest 3-5 follow-ups"))
check("'###  Task :' variants match", ms.is_task_request("  ###   Task: generate a title"))
check("__task__ kwarg alone is a task", ms.is_task_request("anything", task="title_generation"))
check("ordinary request is NOT a task", not ms.is_task_request("make this picture realistic"))
check("hash-mention is NOT a task", not ms.is_task_request("draw a ### symbol on a wall"))

# ---------------------------------------------------------------- reference recovery
print("reference recovery (OpenWebUI 0.10 message shapes):")
md = f"![cat](data:image/png;base64,{PNG1})"
check("assistant str content", ms.find_recent_image([{"role": "assistant", "content": md}]) == PNG1)
check("assistant list content (text part)",
      ms.find_recent_image([{"role": "assistant",
                             "content": [{"type": "text", "text": md}]}]) == PNG1)
check("assistant output field (content='')",
      ms.find_recent_image([{
          "role": "assistant", "content": "",
          "output": [{"type": "message", "status": "completed",
                      "content": [{"type": "output_text", "text": md}]}]}]) == PNG1)
check("user upload (image_url part)",
      ms.find_recent_image([{"role": "user", "content": [
          {"type": "text", "text": "here"},
          {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG1}"}}]}]) == PNG1)
check("newest message wins: generated image beats older upload",
      ms.find_recent_image([
          {"role": "user", "content": [
              {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG1}"}}]},
          {"role": "assistant", "content": "",
           "output": [{"type": "message",
                       "content": [{"type": "output_text",
                                    "text": f"![x](data:image/png;base64,{PNG2})"}]}]},
          {"role": "user", "content": "make this picture realistic"},
      ]) == PNG2)
check("no image → None", ms.find_recent_image([{"role": "user", "content": "hello"}]) is None)

# ---------------------------------------------------------------- persistent store
print("persistent per-chat store:")
tmp = tempfile.mkdtemp(prefix="media-recent-test-")
ms.RECENT_DIR = tmp
ms.remember_image("chat-1", PNG1)
check("roundtrip", ms.recall_image("chat-1") == PNG1)
ms.remember_image("chat-1", PNG2)
check("newer image replaces older", ms.recall_image("chat-1") == PNG2)
check("other chats unaffected", ms.recall_image("chat-2") is None)
check("no chat_id is a no-op", (ms.remember_image(None, PNG1) or ms.recall_image(None)) is None)

print("chat id:")
check("body chat_id wins", ms.chat_id_of({"chat_id": "abc"}) == "abc")
check("metadata chat_id next", ms.chat_id_of({"metadata": {"chat_id": "m1"}}) == "m1")
first_hash = ms.chat_id_of({"messages": [{"role": "user", "content": "hello world"}]})
check("hash fallback is stable and not 'default'",
      first_hash != "default"
      and first_hash == ms.chat_id_of({"messages": [{"role": "user", "content": "hello world"}]}))

# ---------------------------------------------------------------- style conversion
print("style conversion:")
sc = ms.style_conversion("make this picture realistic")
check("'make this picture realistic' → photoreal restyle", bool(sc) and "photorealistic" in sc[0])
check("restyle instruction is subject-agnostic (no 'people' assumption)",
      bool(sc) and "people" not in sc[0].lower() and "ethnicity" not in sc[0].lower())
sc2 = ms.style_conversion("make this picture animated")
check("'make this picture animated' → cartoon restyle", bool(sc2) and "cartoon" in sc2[0])
check("negative names the style being left behind", bool(sc2) and "photorealistic" in sc2[1])
check("'make the ball realistic' → object edit, not restyle",
      ms.style_conversion("make the ball realistic") is None)
check("'animate this' → motion, not restyle", ms.style_conversion("animate this") is None)
check("'turn it into a watercolor' → watercolor",
      (ms.style_conversion("turn it into a watercolor") or ("",))[0].startswith(
          "Transform the entire image into a soft watercolor"))

# ---------------------------------------------------------------- image_krea routing
print("image_krea routing:")
kp = krea.Pipe()
check("'make this picture realistic' is NOT a fresh image request",
      not kp._is_image_request("make this picture realistic"))
check("'create image of a cat…' IS a fresh image request",
      kp._is_image_request("create image of a cat jumping off the building that is on fire"))
check("'can you make it brighter?' is not a question (edits)",
      not kp._is_question("can you make it brighter?"))
check("'what's in this picture?' is a question",
      kp._is_question("what's in this picture?"))
check("'have them use chopsticks' is not a question (edits)",
      not kp._is_question("have them use chopsticks"))

# ---------------------------------------------------------------- auto_assistant routing
print("auto_assistant routing:")
ap = aa.Pipe()
check("'make this picture realistic' is NOT a fresh image request",
      not ap._is_image_request("make this picture realistic"))
check("'make this picture realistic' is a style edit",
      bool(ap._style_conversion("make this picture realistic")))
check("'have them use chopsticks' wants an edit", ap._wants_edit("have them use chopsticks"))
check("'can you make it brighter?' wants an edit", ap._wants_edit("can you make it brighter?"))
check("'what's in this picture?' stays chat", not ap._wants_edit("what's in this picture?"))
check("'thanks, looks great!' stays chat", not ap._wants_edit("thanks, looks great!"))
check("'write a poem about it' stays chat", not ap._wants_edit("write a poem about it"))
img_output_history = [
    {"role": "user", "content": "create image of a cat jumping off a burning building"},
    {"role": "assistant", "content": "",
     "output": [{"type": "message",
                 "content": [{"type": "output_text",
                              "text": f"![cat](data:image/png;base64,{PNG1})"}]}]},
    {"role": "user", "content": "make this picture realistic"},
]
kind, media = ap._recent_media(img_output_history)
check("_recent_media sees output-field images", kind == "image" and media == PNG1)

# ---------------------------------------------------------------- photoreal routing
print("photoreal routing:")
pp = photo.Pipe()
check("'make this picture animated' is an edit follow-up",
      pp._wants_edit_followup("make this picture animated"))
check("'make this guy in the swimsuit…' is an edit follow-up",
      pp._wants_edit_followup("make this guy in the swimsuit, don't change the face"))
check("'remove the hat' is an edit follow-up", pp._wants_edit_followup("remove the hat"))
check("'create image of a cat…' is NOT an edit follow-up",
      not pp._wants_edit_followup("create image of a cat jumping off a building"))
check("'another one' is NOT an edit follow-up", not pp._wants_edit_followup("another one"))
text, img, current = pp._parse([
    {"role": "user", "content": [
        {"type": "text", "text": "older turn"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG1}"}}]},
    {"role": "user", "content": "a woman on a beach"},
])
check("_parse flags a stale upload as non-current", img == PNG1 and current is False)

# ---------------------------------------------------------------- task guard end-to-end
print("task guard (pipe() short-circuits, no GPU path):")
calls = []
ms_answer, ms.answer_task = ms.answer_task, lambda *a, **k: calls.append(a) or "A Title"
task_body = {"chat_id": "t1", "messages": [
    {"role": "user", "content": "### Task:\nSuggest 3-5 relevant follow-up questions"}]}
r1 = asyncio.run(kp.pipe(dict(task_body)))
r2 = asyncio.run(pp.pipe(dict(task_body)))
r3 = asyncio.run(ap.pipe(dict(task_body)))
r4 = asyncio.run(ap.pipe({"chat_id": "t1", "messages": [
    {"role": "user", "content": "whatever"}]}, __task__="title_generation"))
ms.answer_task = ms_answer
check("image_krea answers task as text", r1 == "A Title")
check("photoreal answers task as text", r2 == "A Title")
check("auto_assistant answers '### Task' text as text", r3 == "A Title")
check("auto_assistant honors the __task__ kwarg", r4 == "A Title")
check("task answers came from the task model, not a render", len(calls) == 4)

print(f"\n{passed + failed} checks — " + ("ALL PASS" if not failed else f"{failed} FAILED"))
sys.exit(1 if failed else 0)

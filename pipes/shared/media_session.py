"""Conversation continuity for the image pipes: task-guarding, reference recovery,
and the prompt contracts that keep follow-up edits anchored to the actual picture.

WHY THIS FILE EXISTS
--------------------
On 2026-08-02 "make this picture realistic" after a cat image produced a photo of a
father and son. media_metrics.jsonl told the whole story: OpenWebUI's background tasks
(title / follow-up / tags / web-search-decision prompts, all starting "### Task:") were
routed into the media pipes as real GPU jobs — dozens of 14-174 s renders of task
boilerplate — and each junk render overwrote the pipe's in-memory last-image cache for
the REAL chat id. The user's next follow-up then edited a junk image instead of the cat.
Three independent weaknesses lined up:

  1. No pipe recognised a task request (OpenWebUI pops `metadata` off the body, so the
     marker only arrives as the `__task__` kwarg — and only if pipe() declares it).
  2. The last-image memory was per-pipe, in-process and 30-entry LRU: wiped by every
     deploy, invisible to sibling pipes, and writable by the junk jobs above.
  3. Reference recovery from history assumed assistant content carries the image
     markdown; OpenWebUI 0.10 stores pipe replies in message.output and rebuilds
     content from it only on some paths, so the scan must handle string content,
     list-of-parts content, AND the raw output field.

This module fixes all three in one place. It is shared the same way identity_edit.py
is: source of truth here, a copy in OpenWebUI's data mount (/app/backend/data), and a
guarded import in each pipe that degrades to the old behaviour when missing.

The prompt texts at the bottom are not house style — they are ported from the official
Qwen-Image "Edit Prompt Enhancer" (QwenLM/Qwen-Image prompt_utils.py), the ImgEdit
benchmark judge rubrics (PKU-YuanGroup/ImgEdit), and the RedCraft creator's own
prompting guidance (civitai model 958009). See docs/IMAGE_CONTINUATION.md.
"""
import base64
import hashlib
import json
import os
import re
import time

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# 1. Background-task detection
# ---------------------------------------------------------------------------
# OpenWebUI marks task requests in body['metadata']['task'] (title_generation,
# follow_up_generation, tags_generation, query_generation, ...) but functions.py POPS
# metadata before the pipe sees the body — the value arrives ONLY as the __task__ kwarg,
# and only when pipe() literally declares a parameter named __task__. The text prefix is
# the belt-and-braces fallback for versions or paths that don't deliver the kwarg.
_TASK_PREFIX = re.compile(r"^\s*#{2,4}\s*Task\s*:", re.I)

# The tiny always-available text model for answering task prompts. This instance's
# admin config already names it as the task model, but OpenWebUI silently falls back to
# the chat's model (i.e. the pipe) whenever the id is missing from the visible model
# registry — which is exactly what happened. Answering in-pipe makes the guard
# self-contained either way.
TASK_LLM = "gemma3:1b"


def is_task_request(text, task=None):
    """True when this invocation is OpenWebUI internal machinery, not the user talking."""
    if task:
        return True
    return bool(_TASK_PREFIX.match(text or ""))


def answer_task(ollama_url, messages, timeout=90):
    """Answer a task prompt as plain text on the small CPU-friendly model.

    Returning the model's raw text keeps titles/tags/follow-ups working (the caller
    parses JSON out of choices[0].message.content); returning '' would make OpenWebUI
    fall back to defaults. Never raises — a failed task answer must not error a title.
    """
    import requests  # deferred: this module must import without requests for pure tests
    msgs = [m for m in (messages or []) if m.get("role") in ("system", "user", "assistant")]
    slim = []
    for m in msgs[-4:]:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
        slim.append({"role": m["role"], "content": str(c or "")[:8000]})
    try:
        r = requests.post(f"{ollama_url}/api/chat",
                          json={"model": TASK_LLM, "messages": slim, "stream": False,
                                "keep_alive": 0, "options": {"temperature": 0.1,
                                                             "num_predict": 400}},
                          timeout=timeout)
        return ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 2. Persistent, cross-pipe last-image store
# ---------------------------------------------------------------------------
# One file per chat under the OpenWebUI data mount (the only writable persistent path
# the pipes share). Survives deploys and pipe reloads, and is visible to Assistant,
# Image and Photoreal alike — switching models mid-chat keeps the picture on the table.
RECENT_DIR = os.environ.get("MEDIA_RECENT_DIR", "/app/backend/data/media_recent")
_RECENT_KEEP = 40  # newest chats kept; one 1024px PNG b64 is ~2 MB, so cap ≈ 80 MB


def _recent_path(chat_id):
    return os.path.join(RECENT_DIR, hashlib.sha1(str(chat_id).encode()).hexdigest()[:24] + ".json")


def remember_image(chat_id, b64, source="generated"):
    """Persist the image the user most recently saw in this chat. Never raises."""
    if not chat_id or not b64:
        return
    try:
        os.makedirs(RECENT_DIR, exist_ok=True)
        with open(_recent_path(chat_id), "w") as f:
            json.dump({"chat_id": str(chat_id), "b64": b64, "source": source,
                       "ts": time.time()}, f)
        entries = [(os.path.getmtime(os.path.join(RECENT_DIR, n)), n)
                   for n in os.listdir(RECENT_DIR) if n.endswith(".json")]
        for _, name in sorted(entries)[:-_RECENT_KEEP]:
            os.remove(os.path.join(RECENT_DIR, name))
    except Exception:
        pass


def recall_image(chat_id):
    """The persisted last image for this chat, or None. Never raises."""
    if not chat_id:
        return None
    try:
        with open(_recent_path(chat_id)) as f:
            return json.load(f).get("b64") or None
    except Exception:
        return None


def chat_id_of(body, metadata=None):
    """Stable per-conversation id, matching auto_assistant's semantics: body/metadata
    chat_id when present, else a hash of the first user message (stable within a
    conversation, distinct across them — never a global 'default' bucket)."""
    cid = ((body or {}).get("chat_id")
           or ((body or {}).get("metadata") or {}).get("chat_id")
           or (metadata or {}).get("chat_id"))
    if cid:
        return str(cid)
    for m in (body or {}).get("messages", []):
        if m.get("role") == "user":
            c = m.get("content")
            s = c if isinstance(c, str) else json.dumps(c, sort_keys=True)[:500]
            return hashlib.md5(s.encode()).hexdigest()[:16]
    return "default"


# ---------------------------------------------------------------------------
# 3. Reference-image recovery from OpenWebUI 0.10 histories
# ---------------------------------------------------------------------------
_DATA_URI = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)")


def _from_text(s):
    if not isinstance(s, str) or "data:image" not in s:
        return None
    m = _DATA_URI.search(s)
    return m.group(1) if m else None


def image_from_message(m):
    """Newest-wins image extraction from ONE message, whatever shape OWUI sent it in:
    str content (markdown data URI), list content (image_url parts AND markdown inside
    text parts), or the raw message.output items (type 'message' → content parts of
    type 'output_text'). Pipe replies live in output when content arrives empty."""
    c = m.get("content", "")
    if isinstance(c, str):
        b = _from_text(c)
        if b:
            return b
    elif isinstance(c, list):
        for part in reversed(c):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and "," in url:
                    return url.split(",", 1)[1]
            if part.get("type") == "text":
                b = _from_text(part.get("text", ""))
                if b:
                    return b
    out = m.get("output")
    if isinstance(out, list):
        for item in reversed(out):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in reversed(item.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    b = _from_text(part.get("text", ""))
                    if b:
                        return b
    return None


def find_recent_image(messages):
    """Most recent image in the chat, strictly newest-message-first across BOTH roles —
    a generated image from the last assistant turn beats a user upload from five turns
    ago (the old upload-first order made edits compound off the original upload)."""
    for m in reversed(messages or []):
        if m.get("role") not in ("user", "assistant"):
            continue
        b = image_from_message(m)
        if b:
            return b
    return None


# ---------------------------------------------------------------------------
# 4. Whole-image style conversion (photo ↔ animated ↔ anime ↔ ...)
# ---------------------------------------------------------------------------
# Detection stays regex; the INSTRUCTION follows the official Qwen-Image edit-enhancer
# formula for style conversion: name the style plus 3-5 concrete visual traits, then
# state what must be preserved — the composition and subjects AS THEY ARE, with no
# assumption about what kind of subjects they are. The old wording hard-coded "the same
# people — ages, genders, ethnicity" into every restyle, which for a cat picture handed
# the editor an instruction about people it then tried to satisfy.
STYLE_TARGETS = (
    (r"photo[\s-]?realistic|realistic|photoreal|lifelike|real\s+photo(?:graph)?|into\s+a\s+photo(?:graph)?",
     "a photorealistic photograph: true-to-life colors and materials, natural skin and "
     "surface textures, real-world lighting and shadows, DSLR photo depth",
     "cartoon, illustration, anime, 3D render, CGI, drawing, painting, stylized"),
    (r"animated(?:[\s-]style)?|animation[\s-]style|cartoon(?:ish|[\s-]style)?|pixar|disney|3d\s+animated",
     "a vibrant 3D animated cartoon-style illustration: stylized shapes, expressive "
     "features, clean outlines, rich saturated colors",
     "photorealistic, photograph, real skin texture, film grain"),
    (r"anime|manga",
     "a Japanese anime illustration: clean line art, cel shading, flat vivid colors",
     "photorealistic, photograph, western cartoon"),
    (r"water\s?colou?r(?:\s+painting)?",
     "a soft watercolor painting: visible brush strokes, gentle color washes, paper texture",
     "photograph, 3D render, hard edges"),
    (r"oil\s+painting|painting",
     "a classical painted artwork: rich visible brush strokes, painterly light",
     "photograph, 3D render"),
    (r"(?:pencil\s+)?sketch|line\s?art|charcoal|drawing",
     "a hand-drawn pencil sketch: graphite line work and shading on paper",
     "photograph, color photo, 3D render"),
    (r"pixel\s?art|8[\s-]?bit",
     "retro pixel art with a limited palette and crisp pixel blocks",
     "photograph, smooth gradients"),
)

_STYLE_MOTION = re.compile(r"\b(video|clip|gif|footage|animate|move|moving|motion)\b")
_STYLE_DEICTIC = re.compile(
    r"\b(it|this|that|everything|the\s+(?:whole\s+)?(?:image|picture|photo|pic|scene|"
    r"cartoon|drawing|painting|sketch|illustration|artwork))\b")


def style_conversion(text):
    """(instruction, negative) when the request is a WHOLE-image style change
    ('make this picture realistic', 'turn it into a cartoon') — else None."""
    t = (text or "").lower()
    if _STYLE_MOTION.search(t):
        return None  # motion request, not a still restyle
    if not _STYLE_DEICTIC.search(t) and not re.search(r"\binstead\b", t):
        return None  # targets a specific object ('make the ball realistic') → normal edit
    hits = [(m.end(), tgt, neg) for pat, tgt, neg in STYLE_TARGETS
            for m in [re.search(rf"(?:{pat})\b", t)] if m]
    if not hits:
        return None
    # Last-mentioned style wins ('make the cartoon realistic' → realistic).
    _, target, negative = max(hits, key=lambda h: h[0])
    return (f"Transform the entire image into {target}. Keep the composition and every "
            f"subject exactly as they are — the same subjects, poses, expressions, "
            f"clothing or markings, colors and framing — changing ONLY the rendering "
            f"style, applied consistently across the whole image.", negative)


# ---------------------------------------------------------------------------
# 5. Prompt contracts (ported from official guidance — see module docstring)
# ---------------------------------------------------------------------------
# Text-to-image enhancement. The failure this replaces: the old prompt contained a
# literal cartoon-style example, and the model copied it — "a cat jumping off a burning
# building" (no style named) came out as "a vibrant 3D animated cartoon-style
# illustration". Rules follow the RedCraft/Krea-2 guidance: natural-language sentences,
# photographic register BY DEFAULT, elaborate only along photographic axes, and adopt a
# style ONLY when the user names one. No style examples appear anywhere in this prompt,
# so there is nothing to copy.
T2I_ENHANCE_SYS = (
    "You expand a user's idea into ONE natural-language prompt for a photorealistic "
    "image model. The user's words are HARD requirements: keep every subject, count, "
    "age, gender, ethnicity, object, action and relationship exactly as stated — never "
    "add, drop or substitute subjects, and never change what kind of thing a subject is. "
    "Elaborate ONLY along photographic axes: setting, composition and framing, lighting "
    "and atmosphere, materials and textures, camera angle, lens and depth of field. "
    "Default to a real photograph: anchor conceptual or imaginative scenes with an "
    "explicit photographic frame (e.g. 'a photograph, shot on a 35mm lens'). Use style "
    "or medium words (cartoon, anime, illustration, painting, render) ONLY if the user "
    "used them — in that exact style, opening the prompt with it. When several people "
    "are specified, describe each as their own clause and keep every face in sharp "
    "focus. Plain sentences, no tag lists, no (word:1.5) weight syntax — weights are "
    "read as literal text. Under 100 words. Output ONLY the prompt text."
)

# Instruction-edit rewriting. Ported from the official Qwen-Image Edit Prompt Enhancer,
# whose rewriter SEES the image — callers must attach the reference image to this call.
# The old text-only prompt carried worked examples full of people ('the young woman on
# the right', ethnicity lists); on non-people images the model copied the examples and
# the editor obligingly drew the people. This version names its rules without example
# subjects, and grounds every noun in what is actually visible.
EDIT_REWRITE_VISION_SYS = (
    "You rewrite a photo-editing request into one clear instruction for an image-editing "
    "model. You can SEE the current image; the conversation may be provided for context. "
    "Rules: ONE imperative instruction, under 80 words. Keep the core intention of the "
    "request unchanged — only make it clearer, concrete and visually feasible. Name each "
    "subject as it actually appears in the image (species, color, position — e.g. 'the "
    "orange tabby cat in mid-leap'), never 'it' or a subject you cannot see. Use "
    "absolute target states, not relative wording ('a bit older' becomes a concrete "
    "age and its visible traits). Anything you add must fit the scene's existing logic "
    "and style; do not invent new subjects or scenery unless the request asks for them. "
    "When changing a person, restate the visible traits that must survive (skin tone, "
    "hair, clothing, build). For a whole-image style change, state the target style "
    "with 3-5 concrete visual traits and keep the subjects and composition as they "
    "are. End with what must stay unchanged. Then a second line 'AVOID: ' listing 3-8 "
    "comma-separated visual traits the RESULT must not contain, drawn from the actual "
    "request (drift directions, the style being left behind). Output EXACTLY two "
    "lines:\nEDIT: <instruction>\nAVOID: <traits>"
)

# Edit verification, after the ImgEdit judge pattern: the checker sees BOTH images in
# order and scores instruction-adherence AND preservation separately, judged against
# the user's ORIGINAL ask — not against the rewritten instruction, which may itself be
# the thing that went wrong. Output stays the pipes' existing OK:/FIX: contract.
VERIFY_EDIT_SYS = (
    "You are a strict image-edit checker. You receive TWO images: the FIRST is the "
    "original, the SECOND is the edited result. You also get the user's original "
    "request and the instruction given to the editor. Judge the SECOND image on two "
    "things, in this order. 1) ADHERENCE: does it deliver what the user's original "
    "request asked for? 2) PRESERVATION: is it recognisably the SAME picture apart "
    "from the requested change — the same subjects (same kind, same count), scene and "
    "composition as the FIRST image? A result that swapped, added or removed subjects "
    "the request never mentioned is a FAIL even if it looks good. For a style-change "
    "request, the style must change but subjects and composition must survive. Ignore "
    "minor color or sharpness differences. Reply with EXACTLY two lines:\n"
    "OK: yes or no\n"
    "FIX: if no — ONE concrete imperative sentence naming what to correct, referring "
    "to subjects as they appear in the FIRST image; if yes — the word none"
)


def edit_qa_user_prompt(original_ask, instruction):
    """The user-turn text that accompanies [reference, result] in the QA call."""
    p = f"User's original request: {original_ask}"
    if instruction and instruction.strip() != (original_ask or "").strip():
        p += f"\nInstruction given to the editor: {instruction}"
    return (p + "\nFirst image = original, second image = edited result. "
                "Does the edited result satisfy the request while staying the same picture?")


def metric(path, **fields):
    """Append one JSON line describing a finished media job. Never raises."""
    if not path:
        return
    try:
        fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a") as f:
            f.write(json.dumps(fields, default=str) + "\n")
    except Exception:
        pass

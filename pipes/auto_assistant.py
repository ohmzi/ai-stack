"""
title: Assistant (auto)
author: local
version: 0.5.0
required_open_webui_version: 0.5.0
description: One model that decides - chats (with vision), makes a Krea 2 image (text or gentle image-to-image edit), or a Wan video. Non-blocking (async). Never uses the uncensored model.
"""
import asyncio, aiohttp, requests, time, base64, hashlib, random, re, json, threading

# Serialize the VRAM-manipulating generation section so two concurrent Assistant invocations
# can't both free/reload models on the single 24 GB card and OOM each other.
_GEN_LOCK = threading.Lock()

V_QUALITY = "best"  # "best" = Wan 2.2 A14B two-expert Lightning (default) | "fast" = TI2V 5B
V_W, V_H = 832, 480            # default 480p
V_W_HQ, V_H_HQ = 1280, 704     # "720p"/"hq" keyword → native 720p (~5 min with sage)
V_LEN_5B, V_STEPS_5B, V_FPS_5B = 49, 20, 24   # 5B fast path
V_LEN_14B, V_FPS_14B = 81, 16                  # A14B path (16 fps native, 5 s)
V_LEN_LONG = 121               # "longer" keyword → 7.5 s (requires the RifleX RoPE patch node)
V_STEPS_14B = 6                # 3 high + 3 low (up from 4 — better motion/anatomy)
V_HIGH_CFG = 3.0               # cfg>1 on the HIGH stage re-enables the negative prompt and
                               # restores motion strength/prompt adherence (Lightning motion recipe)
V_HIGH_LORA = 0.8              # Lightning LoRA strength on the high expert (motion recipe)
V_RIFE = 2                     # RIFE interpolation multiplier: 16 fps → 32 fps output
V_SAGE = True                  # SageAttention patch on the A14B experts (NEVER on the 5B — noise)
V_COMPILE = False              # torch.compile the experts: currently BROKEN with --lowvram GGUF
                               # (dynamo trips on ComfyUI's patched Conv3d: "'Conv3d' has no
                               # attribute '_v'"). Plumbing is wired — flip on when the
                               # ComfyUI/GGUF compile path stabilizes.
V_SHOT_MAX = 6                 # multi-shot: max chained 5 s shots (~30 s)
# Text-to-image parity with the standalone "Image" (Krea 2) pipe — keep these in sync with that
# pipe's LORA_FILE / LORA_STRENGTH / TRIGGER / WIDTH / HEIGHT valves so the same request produces
# the same subject in either. Empty LORA = base Krea 2 (the current default on both).
IMG_T2I_LORA = ""            # e.g. "krea2/mylora_comfyui.safetensors"
IMG_T2I_LORA_STRENGTH = 1.0
IMG_T2I_TRIGGER = ""         # prepended to the prompt when a LoRA is set
IMG_T2I_W, IMG_T2I_H = 1024, 1024
IMG_DENOISE = 0.30  # gentle Krea 2 img2img edit strength (lower = closer to the attached image)
IMG_ENHANCE = True  # expand short prompts into richer ones via the local LLM (text-to-image only)
IMG_VERIFY = True   # vision-check the result against the request; one corrected retry on mismatch
VID_ENHANCE = True  # expand terse video ideas ("guy shooting hoops") into detailed prompts — the
                    # single biggest quality lever for Wan; terse prompts produce broken scenes
VID_VERIFY = True   # vision-check a mid frame of the clip against the request; one corrected retry
VID_VERIFY_MODE = "anchors"  # multi-shot QA scope: "anchors" = shot 1 + last shot only | "all" = every shot


class Pipe:
    def __init__(self):
        self.comfy = "http://localhost:8188"
        self.ollama = "http://localhost:11434"
        self.chat_model = "dolphin-venice:24b"   # text chat + all prompt-rewrite/merge/plan helpers
        self.vision_model = "gemma4:31b"         # dolphin is text-only; gemma does image QA + vision chat
        # (OpenWebUI's own title/tag/query task model is gemma3:1b, configured at the server level —
        # this pipe does not call it directly, so no task_model attribute is kept here.)
        self._recent = {}        # chat_id -> last produced image b64 (for follow-up edits without re-upload)
        self._recent_video = {}  # chat_id -> (prompt, seed) of the last produced video (for follow-up changes)

    def pipes(self):
        return [{"id": "auto", "name": "🪄 Assistant (auto chat + image + video)"}]

    # ---------- content parsing ----------
    def _last_user(self, messages):
        """(text, reference_image_b64_or_None) from the last user message."""
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                return c.strip(), None
            if isinstance(c, list):
                text, img = "", None
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text = part.get("text", "")
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            img = url.split(",", 1)[1]
                return (text or "").strip(), img
            return "", None
        return "", None

    def _ollama_messages(self, messages):
        """Convert OpenAI-format (incl. multimodal) messages to Ollama format (content str + images[])."""
        out = []
        for m in messages:
            role = m.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                out.append({"role": role, "content": self._scrub(c)})
            elif isinstance(c, list):
                text, imgs = "", []
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text += part.get("text", "")
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            imgs.append(url.split(",", 1)[1])
                msg = {"role": role, "content": text}
                if imgs:
                    msg["images"] = imgs
                out.append(msg)
        return out

    # ---------- routing ----------
    _STILL_NOUNS = (r"(?:picture|pictures|image|images|photo|photos|pic|pics|drawing|painting|"
                    r"illustration|portrait|poster|art|artwork|wallpaper|logo)")

    # Shared intent guards (used by the image/video/edit routers) so a question or an
    # acknowledgement about media never gets hijacked into a ~2-minute generation job.
    _SMALLTALK = re.compile(
        r"^(nice|cool|thanks|thank|great|awesome|perfect|love|lovely|beautiful|amazing|good|"
        r"ok|okay|k|lol+|haha+|wow|hmm+|nvm|never\s?mind|yes|yeah|yep|yup|no|nope|nah|sure|"
        r"cheers|wonderful|gorgeous|stunning|fantastic|excellent|brilliant)\b", re.I)
    _QUESTION = re.compile(
        r"^(what'?s?|why|how|who|whom|whose|where|when|which|is|are|am|was|were|do|does|did|"
        r"can|could|would|should|will|have|has|had|may|might|tell\s+me|explain|describe|list|"
        r"suggest|recommend|caption|analy[sz]e|identify|read|translate|compare|define|"
        r"summari[sz]e|write|compose|draft|i\s+(think|feel|wonder|need\s+to\s+know))\b", re.I)
    # 'draw a conclusion', 'paint a picture of how it works' … figurative, not image generation.
    _FIGURATIVE = re.compile(
        r"\b(draw|paint|sketch)\s+(?:\w+\s+){0,3}"
        r"(parallel|conclusion|comparison|distinction|attention|inspiration|line)", re.I)

    def _is_smalltalk(self, t):
        return bool(self._SMALLTALK.match((t or "").strip()))

    def _is_question(self, t):
        t = (t or "").strip()
        return t.endswith("?") or bool(self._QUESTION.match(t))

    def _strip_still_style(self, t):
        """'animated picture', 'cartoon image', 'animated movie poster' … name a STYLE of still
        image (cartoon look), not motion — blank the style word so the video detectors don't
        fire on it. Allows up to ~2 words between the style word and the still-noun. 'animated
        gif/clip/video', 'an animation of X' and 'animate this' still route to video."""
        return re.sub(
            rf"\b(?:animated|animation[\s-]style|cartoon(?:[\s-]style)?|anime[\s-]style)\s+"
            rf"(?:\w+\s+){{0,2}}({self._STILL_NOUNS})\b", r"\1", t)

    def _is_video_request(self, t):
        raw = t.lower()
        t = self._strip_still_style(raw)
        # collocations that carry a video-noun but are never motion requests
        t = re.sub(r"\b(video\s?games?|clip\s?art|music\s+videos?)\b", " ", t)
        # explicit generation verb + a video-noun object → a request even if phrased as a question
        if re.search(r"\b(make|create|generate|render|produce|animate|show me|give me)\b"
                     r".{0,25}\b(clip|video|animation|gif|footage|moving image)\b", t):
            return True
        if re.search(r"\banimate\s+(this|it|that|the|my|him|her|them)\b", t):
            return True
        if re.search(r"\bbring\b[\w\s]{0,20}?\bto\s+life\b", t):
            return True
        # bare motion nouns / idioms → only when the message isn't a question or acknowledgement
        if self._is_question(raw) or self._is_smalltalk(raw):
            return False
        if re.search(r"\b(video|animation|footage|moving image|make it move)\b", t):
            return True
        return False

    def _wants_new_video(self, t):
        """Explicit FRESH video request ('create/make a video of …') — always a new clip,
        even if the chat already has one."""
        return bool(re.search(
            r"\b(create|make|generate|render|produce|give me|show me|another|new)\b.{0,25}\b(video|clip|animation|gif|footage)\b",
            self._strip_still_style(t.lower())))

    def _merge_video_prompt(self, prev, change):
        """Fold a requested change into the previous video prompt using the big chat model.
        (gemma3:1b just ECHOED the original verbatim → same prompt + same seed → ComfyUI's graph
        cache returned the identical cached video, so 'change the background' visibly did nothing.)
        Guarded: if the model returns the original unchanged (or junk), fall back to appending the
        change so the conditioning ALWAYS differs from the previous clip."""
        try:
            r = requests.post(f"{self.ollama}/api/generate", json={
                "model": self.chat_model,
                "system": ("You revise prompts for a text-to-video model. Rewrite the ORIGINAL prompt "
                           "so the REQUESTED CHANGE is fully applied: replace every detail the change "
                           "supersedes (e.g. 'change the background to a city' replaces the beach, "
                           "sand, waves and horizon with streets and buildings everywhere they are "
                           "mentioned), and keep everything the change does not affect — subject, "
                           "action, camera, lighting style. Output ONLY the revised prompt text — "
                           "no preamble, no quotes."),
                "prompt": f"ORIGINAL: {prev}\nREQUESTED CHANGE: {change}\nREVISED PROMPT:",
                "stream": False, "think": False, "keep_alive": 0,
                "options": {"temperature": 0.3, "num_predict": 250}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"').strip()
            if out and len(out) < 900 and out.strip().lower() != (prev or "").strip().lower():
                return out
        except Exception:
            pass
        return f"{prev}. {change}."

    def _is_image_request(self, t):
        raw = t.lower()
        if self._FIGURATIVE.search(raw):
            return False
        # explicit generation verb + an image object → a request even if phrased as a question
        if re.search(r"\b(create|creating|generate|make|design|produce|render|show me|give me|"
                     r"i want|can you make|could you make)\b.{0,30}\b(image|images|picture|pictures|"
                     r"photo|photos|pic|drawing|painting|illustration|art|artwork|logo|wallpaper|"
                     r"portrait|render|scene|poster|cartoon|caricature)\b", raw):
            return True
        # bare draw/paint verbs and the '<noun> of' pattern → only when not a question / small-talk
        # ('what do you paint with?', 'nice drawing of a cat' must NOT trigger t2i)
        if self._is_question(raw) or self._is_smalltalk(raw):
            return False
        if re.search(r"\b(draw|sketch|paint|illustrate)\b", raw):
            return True
        if re.search(r"\b(image|picture|photo|portrait|drawing|painting|wallpaper|logo|cartoon|"
                     r"caricature)\s+of\b", raw):
            return True
        return False

    def _is_edit_request(self, t):
        """Edit intent for an image: change/remove/add/replace/bigger/etc.
        (Pure questions about the image have none of these verbs → they go to vision chat.)"""
        t = t.lower()
        # 'give' removed — 'give me <non-visual>' ('give me book recommendations') is a chat request,
        # not an edit; real edits use a concrete verb or reference a visual attribute (see _wants_edit).
        return bool(re.search(
            r"\b(edit|change|replace|remove|delete|erase|swap|add|put|turn|make|"
            r"recolou?r|colou?r|adjust|retouch|modify|update|fix|crop|rotate|blur|bigger|smaller|"
            r"larger|zoom|brighter|darker|more|less|get rid of|without|instead of|into a|to a)\b", t))

    def _chat_id(self, body, meta=None):
        """Stable per-conversation id. OpenWebUI doesn't always put chat_id in the body, and a
        'default' fallback would make the recent-media caches GLOBAL — follow-up edits would then
        grab media from a different conversation. Fall back to hashing the chat's first user
        message, which is stable within a conversation and distinct across them."""
        cid = (body.get("chat_id")
               or (body.get("metadata") or {}).get("chat_id")
               or (meta or {}).get("chat_id"))
        if cid:
            return str(cid)
        for m in body.get("messages", []):
            if m.get("role") == "user":
                c = m.get("content")
                s = c if isinstance(c, str) else json.dumps(c, sort_keys=True)[:500]
                return hashlib.md5(s.encode()).hexdigest()[:16]
        return "default"

    def _extract_b64(self, s):
        if not isinstance(s, str):
            return None
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", s)
        return m.group(1) if m else None

    def _recent_media(self, messages):
        """Most recent media in the chat, scanning backwards.
        Returns ('image', b64) | ('video', (prompt, seed_or_None)) | (None, None).
        Videos carry their prompt+seed in data-p64/data-seed attributes on the <video> tag
        (invisible in the UI, survives restarts); older messages fall back to the 🎬 caption."""
        for m in reversed(messages or []):
            role, c = m.get("role"), m.get("content", "")
            if role == "user" and isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            return "image", url.split(",", 1)[1]
            elif role == "assistant" and isinstance(c, str):
                if "data:video/" in c:
                    v = re.search(r'<video[^>]*data-p64="([A-Za-z0-9+/=]*)"[^>]*data-seed="(\d+)"'
                                  r'(?:[^>]*data-opts="([^"]*)")?', c)
                    if v:
                        try:
                            prompt = base64.b64decode(v.group(1)).decode("utf-8", "ignore")
                        except Exception:
                            prompt = ""
                        return "video", (prompt, int(v.group(2)), self._parse_opts(v.group(3)))
                    cap = re.search(r"\*🎬 (.+?)\*", c)
                    return "video", ((cap.group(1) if cap else ""), None, None)
                b = self._extract_b64(c)
                if b:
                    return "image", b
        return None, None

    @staticmethod
    def _opts_attr(opts):
        """Serialize the render options onto the <video> tag ('WxHxL') so a follow-up keeps the
        original clip's resolution/length even across a restart."""
        o = opts or {}
        return f'{o.get("w", V_W)}x{o.get("h", V_H)}x{o.get("length", V_LEN_14B)}'

    def _parse_opts(self, s):
        """Inverse of _opts_attr: 'WxHxL' → opts dict, or None."""
        m = re.match(r"(\d+)x(\d+)x(\d+)", s or "")
        if not m:
            return None
        return {"w": int(m.group(1)), "h": int(m.group(2)), "length": int(m.group(3)), "fast": False}

    def _scrub(self, text):
        """Strip embedded image/video data URIs so we don't feed megabytes of base64 to the chat model."""
        if not isinstance(text, str):
            return text
        # Fast path: the vast majority of turns carry no media — skip the three multi-MB regexes.
        if "data:" not in text and "<video" not in text:
            return text
        text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "[generated image]", text)
        text = re.sub(r"<video[^>]*>.*?</video>", "[generated video]", text, flags=re.S)
        text = re.sub(r"data:(?:image|video)/[^;]+;base64,[A-Za-z0-9+/=]+", "[media]", text)
        return text

    # Visual nouns whose presence signals the message is about the image itself (an edit),
    # not an off-topic chat turn — used as the positive edit test below.
    _VISUAL_NOUN = re.compile(
        r"\b(image|picture|photo|pic|background|foreground|colou?rs?|sky|lighting|"
        r"shadows?|hair|eyes?|face|skin|clothe?s|clothing|shirt|dress|hat|smile|hands?|"
        r"scene|left|right|top|bottom|corner|edges?)\b", re.I)

    def _wants_edit(self, text):
        """Given an image already exists in the chat, decide if this message asks to MODIFY it.
        Chat is the DEFAULT: only a clear edit verb or a visual reference to the image counts as an
        edit, so plain questions / off-topic asks ('write a poem about it') stay in chat instead of
        silently re-running a ~2-minute Qwen edit on a stale image."""
        core = re.sub(r"^\s*(please|hey|okay?|so|and|then|now|also)[,\s]+", "", text.strip(), flags=re.I)
        core = re.sub(r"^\s*(can|could|would|will)\s+(you\s+)?(please\s+|maybe\s+)?", "", core, flags=re.I).strip()
        low = core.lower()
        # small talk / acknowledgment → chat  (checked BEFORE edit verbs)
        if self._is_smalltalk(low):
            return False
        # question / info request / write-verbs → chat  (translate/write/compose/summarize/draft)
        if self._is_question(low):
            return False
        # explicit edit verb (change/remove/bigger/…) → edit
        if self._is_edit_request(low):
            return True
        # positive edit test: an imperative that references the image or a visual attribute → edit;
        # anything else (a statement/topic that never names the image) → chat
        if self._VISUAL_NOUN.search(low):
            return True
        return False

    def _edit_instruction(self, text):
        """Strip a 'can you edit the picture and …' wrapper, leaving the actual instruction for the editor."""
        t = text.strip()
        t = re.sub(r"^\s*(please\s+)?(can|could|would|will)\s+you\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*please\s+", "", t, flags=re.I)
        t = re.sub(r"^\s*(edit|change|modify|update|fix|retouch|adjust)\s+(this|the|my)\s+(image|picture|photo|pic)\s*(and|to|so(?:\s+that)?|by|:|,)?\s*", "", t, flags=re.I)
        t = re.sub(r"^\s*(in|on|for)\s+(this|the)\s+(image|picture|photo|pic)[,:]?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    # Whole-image restyle targets: (detect_pattern, target_description, negative). Realistic first —
    # "make the cartoon realistic" names both styles and the LAST one is the target the user wants.
    _STYLE_TARGETS = (
        (r"photo[\s-]?realistic|realistic|photoreal|lifelike|real\s+photo(?:graph)?|into\s+a\s+photo(?:graph)?",
         "a photorealistic photograph: real human skin texture and natural proportions, true-to-life "
         "colors and fabrics, real-world lighting and shadows, shot like a DSLR photo",
         "cartoon, illustration, anime, 3D render, CGI, drawing, painting, stylized, plastic skin"),
        (r"animated(?:[\s-]style)?|animation[\s-]style|cartoon(?:ish|[\s-]style)?|pixar|disney|3d\s+animated",
         "a vibrant 3D animated cartoon-style illustration: stylized characters with expressive faces, "
         "clean shapes, rich saturated colors — NOT photorealistic",
         "photorealistic, photograph, real skin texture, film grain"),
        (r"anime|manga",
         "a Japanese anime illustration: clean line art, cel shading, expressive anime faces",
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

    def _style_conversion(self, text):
        """(instruction, negative) when the request is a WHOLE-image style change ('make it
        realistic instead', 'turn this into a cartoon') — else None. The generic edit rewriter
        can't handle these: it ends every instruction with 'keep identity/background/lighting
        unchanged', which fights a global restyle, so we build the instruction directly."""
        t = (text or "").lower()
        if re.search(r"\b(video|clip|gif|footage|animate|move|moving|motion)\b", t):
            return None  # motion request, not a still restyle
        if not re.search(r"\b(it|this|that|everything|the\s+(?:whole\s+)?(?:image|picture|photo|pic|"
                         r"scene|cartoon|drawing|painting|sketch|illustration|artwork))\b", t) \
                and not re.search(r"\binstead\b", t):
            return None  # targets a specific object ('make the ball realistic') → normal edit path
        hits = [(m.end(), tgt, neg) for pat, tgt, neg in self._STYLE_TARGETS
                for m in [re.search(rf"(?:{pat})\b", t)] if m]
        if not hits:
            return None
        # last-mentioned style wins ('make the cartoon realistic' → realistic); on a tie
        # ('watercolor painting' ends where 'painting' ends) the more specific earlier entry wins
        _, target, negative = max(hits, key=lambda h: h[0])
        return (f"Convert this image into {target}. Keep the exact same scene: the same people — same "
                f"count, ages, genders, ethnicity and skin tone — the same poses, expressions, clothing "
                f"and colors, and the same composition, background and framing. Change ONLY the rendering "
                f"style, and apply the new style emphatically to the ENTIRE image.", negative)

    def _style_enrich(self, instruction, msgs):
        """Restate the concrete subjects (from chat context) inside a style-conversion
        instruction — on big style jumps the editor drifts ethnicity/identity when the
        instruction only says 'same ethnicity' generically (observed: Pakistani father came
        out a different ethnicity on cartoon→photo). Falls back to the generic instruction."""
        ctx = self._edit_context(msgs)
        if not ctx:
            return instruction
        try:
            r = requests.post(f"{self.ollama}/api/generate", json={
                "model": self.chat_model,
                "system": ("You tighten image style-conversion instructions. Using the conversation, "
                           "rewrite the instruction so every person is named concretely — age, gender, "
                           "ethnicity and skin tone (e.g. 'the Pakistani father, a South Asian man in "
                           "his 30s with brown skin, and his 10-year-old South Asian son') — instead of "
                           "generic wording like 'the same people'. Keep the conversion command, the "
                           "style description, and the 'Change ONLY the rendering style' ending intact. "
                           "ONE instruction under 110 words. Output ONLY the instruction text."),
                "prompt": f"Conversation:\n{ctx}\n\nInstruction:\n{instruction}\n\nRewritten instruction:",
                "stream": False, "think": False, "keep_alive": 0,
                "options": {"temperature": 0.3, "num_predict": 260}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"')
            if out and "convert" in out.lower() and len(out) < 1200:
                return out
        except Exception:
            pass
        return instruction

    def _edit_boost(self, text):
        """'you barely changed it / still looks young' retry phrasing → push the sampler harder."""
        return bool(re.search(
            r"\b(still|barely|hardly|try again|not enough|no change|"
            r"didn'?t (?:change|work|do|listen)|doesn'?t (?:look|seem)|"
            r"(?:way|much) (?:more|older|younger|bigger|smaller))\b", (text or "").lower()))

    _EDIT_REWRITE_SYS = (
        "You rewrite photo-editing requests into instructions for the Qwen-Image-Edit model, which sees "
        "the photo alongside your instruction. You are given the conversation so far; rewrite ONLY the "
        "last user request. Rules: ONE imperative instruction under 80 words. Use absolute, concrete "
        "visual terms for the target state, never relative wording — e.g. \"make the son a bit older, "
        "like 18\" becomes \"Change the boy into an 18-year-old young man: adult height and build, mature "
        "facial features, light stubble, defined jawline.\" Name the subject as it appears in the photo "
        "(\"the boy\", \"the young woman on the right\"), never \"it\" or \"him\". The editor is "
        "conservative and under-applies changes, so state the change emphatically. When transforming a "
        "person, RESTATE the traits that must survive the change — ethnicity and skin tone (take them "
        "from the conversation, e.g. Pakistani/South Asian), hair colour, family resemblance, clothing — "
        "the editor drifts to a generic different-looking person if you don't. For age changes name the "
        "life stage and bracket it: \"a 10-year-old school-age girl — clearly older than a toddler, "
        "clearly younger than a teenager\". End the instruction with what must stay unchanged (identity, "
        "clothing, background, lighting) unless the user asked to change those too. Then output a second "
        "line: \"AVOID: \" plus 3-8 comma-separated visual traits the RESULT must not contain — for age "
        "changes bracket BOTH sides (e.g. for 10 years old: toddler, preschooler, teenager, adult woman) "
        "and add ethnicity-drift terms when ethnicity must be kept (e.g. East Asian features). Output "
        "EXACTLY two lines:\nEDIT: <instruction>\nAVOID: <traits>"
    )

    def _edit_context(self, msgs, limit=8):
        """Compact 'user:/assistant:' transcript (media scrubbed) so the rewriter can resolve
        references like 'the son' from earlier turns."""
        lines = []
        for m in (msgs or [])[-limit:]:
            role, c = m.get("role"), m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if isinstance(c, list):
                t = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                if any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
                    t = (t + " [attached image]").strip()
            else:
                t = str(c or "")
            t = self._scrub(t).strip()
            if t:
                lines.append(f"{role}: {t[:300]}")
        return "\n".join(lines)

    def _comfy_idle(self):
        """True if ComfyUI has no running/pending job — so a /free won't evict another pipe's
        in-flight render (e.g. an Animate/SCAIL job). Fail-open: on error assume idle."""
        try:
            q = requests.get(f"{self.comfy}/queue", timeout=10).json()
            return not q.get("queue_running") and not q.get("queue_pending")
        except Exception:
            return True

    def _vram_free_gib(self):
        """Free VRAM on GPU0 in GiB via ComfyUI /system_stats, or None if unavailable."""
        try:
            d = requests.get(f"{self.comfy}/system_stats", timeout=10).json()
            dev = (d.get("devices") or [{}])[0]
            return float(dev.get("vram_free", 0)) / (1024 ** 3)
        except Exception:
            return None

    def _comfy_free(self, need_gib=20.0):
        """Ask ComfyUI to unload its models so the ~20 GB vision model can load for the QA check,
        then BLOCK until VRAM is actually released (the unload is async — a blind 2 s sleep raced it).
        Skips the unload while another ComfyUI job is in flight so we don't evict another pipe's
        render. Returns True if enough VRAM ended up free."""
        if not self._comfy_idle():
            return (self._vram_free_gib() or need_gib) >= need_gib
        try:
            requests.post(f"{self.comfy}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except Exception:
            pass
        for _ in range(20):  # up to ~20 s for the CUDA allocator to actually release
            v = self._vram_free_gib()
            if v is None or v >= need_gib:
                return True
            time.sleep(1)
        return (self._vram_free_gib() or 0) >= need_gib

    _VERIFY_SYS = (
        "You are a strict image QA checker. You get a user's request and the generated image. Check ONLY "
        "hard requirements: person count, each person's apparent age bracket, gender, ethnicity, named "
        "objects/actions/setting — including the key object a named activity implies ('playing basketball' "
        "requires a visible basketball, 'having dinner' requires food on the table) — and, ONLY when the "
        "request names an art style or medium ('animated "
        "picture', 'cartoon', 'anime', 'watercolor' …), that the image is rendered in that style (a "
        "photorealistic photo when an animated/cartoon style was asked for is a FAIL). When no style is "
        "named, ignore style, lighting and quality. Reply with EXACTLY two lines:\n"
        "OK: yes or no\n"
        "FIX: if no — ONE concrete imperative sentence stating what to correct, naming each subject "
        "precisely by appearance and position in the image (e.g. 'Remove the boy in the blue shirt, "
        "second from the right'; 'Redraw the whole scene as a 3D animated cartoon'); if yes — the word none"
    )

    def _verify_image(self, request_text, img_b64):
        """(ok, fix) — Gemma-vision compares the produced image to what was asked. Fails open."""
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.vision_model, "system": self._VERIFY_SYS,
                      "prompt": f"Request: {request_text}\nDoes the image satisfy every hard requirement?",
                      "images": [img_b64], "stream": False, "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.1, "num_predict": 150}}, timeout=300)
            out = (r.json().get("response") or "").strip()
            ok = not re.search(r"^\s*OK:\s*no\b", out, re.I | re.M)
            m = re.search(r"^\s*FIX:\s*(.+)$", out, re.I | re.M)
            fix = (m.group(1).strip() if m else "")
            return ok, ("" if fix.lower().startswith("none") else fix)
        except Exception:
            return True, ""

    def _enhance_edit(self, instruction, msgs):
        """(explicit_instruction, negative) via the local LLM. Vague relative asks ('a bit older')
        under-move the identity-preserving editor; explicit absolute target states move it properly.
        Falls back to the original instruction and no negative."""
        ctx = self._edit_context(msgs)
        prompt = (f"Conversation:\n{ctx}\n\nRewrite the last user request."
                  if ctx else f"Request: {instruction}\n\nRewrite this request.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": self._EDIT_REWRITE_SYS, "prompt": prompt,
                      "stream": False, "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.4, "num_predict": 220}}, timeout=180)
            out = (r.json().get("response") or "").strip()
            edit = re.search(r"^\s*EDIT:\s*(.+)$", out, re.I | re.M)
            avoid = re.search(r"^\s*AVOID:\s*(.+)$", out, re.I | re.M)
            if edit:
                return edit.group(1).strip(), (avoid.group(1).strip() if avoid else "")
        except Exception:
            pass
        return instruction, ""

    def _clean_prompt(self, text):
        t = re.sub(r"^\s*(please\s+)?(can you\s+|could you\s+|i want you to\s+|i'?d like\s+(you to\s+)?)?", "", text.strip(), flags=re.I)
        # article alternation MUST be longest-first ('an' before 'a') — the engine takes the first
        # alternative that lets the (all-optional) rest match, so 'a' would eat 'an' and leave 'n …'
        t = re.sub(r"^\s*(create|generate|make|draw|sketch|paint|illustrate|design|render|produce|animate|show me|give me)\s+(an|a|the|some|me)?\s*(image|picture|photo|pic|drawing|painting|illustration|art|artwork|render|video|clip|animation|gif|footage)?\s*(of|showing|with|:)?\s*", "", t, flags=re.I)
        return t.strip() or text.strip()

    def _upload(self, img_b64):
        raw = base64.b64decode(img_b64)
        return requests.post(f"{self.comfy}/upload/image",
                             files={"image": ("ref.png", raw, "image/png")}, timeout=60).json()["name"]

    def _free_vram(self):
        """Unload Ollama models and BLOCK until GPU VRAM is actually released (unload is async).
        Prevents a 22GB Gemma + 18GB Krea 2 collision on the 24GB card. Returns True if the card
        ended up empty; False if a model was still resident after the wait window (an in-flight
        chat pins it) so callers can surface 'GPU busy' rather than blindly OOM."""
        try:
            models = requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", [])
        except Exception:
            models = []
        for m in models:  # per-model try: one wedged unload must not skip the rest
            try:
                requests.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
            except Exception:
                pass
        empty = False
        for _ in range(30):  # wait up to ~30s until no models are loaded
            try:
                if not requests.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", []):
                    empty = True
                    break
            except Exception:
                break
            time.sleep(1)
        time.sleep(2)  # grace for the CUDA allocator to release
        return empty

    @staticmethod
    def _job_error(entry):
        """Human-readable error if the ComfyUI job failed, else None."""
        st = entry.get("status", {})
        if st.get("status_str") != "error":
            return None
        d = next((m[1] for m in st.get("messages", []) if m[0] == "execution_error"), {})
        return (f"{d.get('exception_type', 'Error')} in {d.get('node_type', '?')} — "
                f"{str(d.get('exception_message', ''))[:200]}")

    def _fetch_node_output(self, entry, node):
        items = entry.get("outputs", {}).get(node, {}).get("images", [])
        if not items:
            return None
        it = items[0]
        return requests.get(f"{self.comfy}/view",
            params={"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": "output"},
            timeout=120).content

    def _comfy_lost_job(self, pid):
        """True if pid is in neither the running nor the pending ComfyUI queue — it vanished
        (crash/restart), so waiting out the full poll budget is pointless. False when unsure."""
        try:
            q = requests.get(f"{self.comfy}/queue", timeout=10).json()
            ids = {str(x[1]) for x in q.get("queue_running", []) + q.get("queue_pending", []) if len(x) > 1}
            return str(pid) not in ids
        except Exception:
            return False

    def _submit_poll(self, wf, out_node, kind, iters, extra_nodes=()):
        """Submit a ComfyUI workflow and poll to completion.
        Returns (data, err, extras): bytes of out_node's first output, an error string, and a
        {node: bytes} dict for extra_nodes (e.g. a last-frame SaveImage for multi-shot handoff).
        Retries ONCE on GPU OOM: OpenWebUI can invoke the 20GB chat LLM (e.g. to generate the
        chat title on a brand-new chat) AFTER our ComfyUI job has started, stealing the VRAM
        out from under the sampler. Freeing again and resubmitting wins the second time."""
        err = None
        for attempt in (1, 2):
            try:
                pid = requests.post(f"{self.comfy}/prompt", json={"prompt": wf}, timeout=30).json()["prompt_id"]
            except Exception as e:
                return None, f"⚠️ {kind} backend error: {e}", {}
            err = None
            missing = 0  # consecutive polls where the job is absent from history / the GET failed
            for _ in range(iters):
                try:
                    h = requests.get(f"{self.comfy}/history/{pid}", timeout=15).json()
                except Exception:
                    missing += 1
                    if missing >= 6 and self._comfy_lost_job(pid):
                        return None, f"⚠️ {kind}: ComfyUI is unreachable (crash/restart?).", {}
                    time.sleep(2); continue
                if pid not in h:
                    missing += 1
                    if missing >= 6 and self._comfy_lost_job(pid):
                        return None, f"⚠️ {kind}: ComfyUI lost the job (crash/restart?).", {}
                    time.sleep(2); continue
                missing = 0
                err = self._job_error(h[pid])
                if err:
                    break
                # Fetch the finished output INSIDE the poll's protected region: a transient /view
                # error must re-poll (the render persists in history), not discard a multi-minute job.
                try:
                    data = self._fetch_node_output(h[pid], out_node)
                    if data is None:
                        return None, f"{kind} finished but produced no output.", {}
                    extras = {n: self._fetch_node_output(h[pid], n) for n in extra_nodes}
                    return data, None, extras
                except Exception:
                    time.sleep(2); continue
            if err is None:
                return None, f"⏳ Timed out waiting for the {kind.lower()}.", {}
            if attempt == 1 and "OutOfMemory" in err:
                self._free_vram()
                continue
            break
        return None, f"⚠️ {kind} generation failed: {err}", {}

    def _enhance(self, prompt):
        """Expand a short idea into a vivid image prompt via the local LLM. Falls back to the original."""
        sys = (
            "You are a prompt engineer for the Krea 2 image model. Rewrite the user's idea "
            "as ONE vivid, richly detailed image prompt: subject, setting, lighting, mood, composition, "
            "style, and camera/lens where useful. The user's explicit specifications are HARD requirements: "
            "every person, count, age, gender, ethnicity, object, relationship AND art style they name "
            "MUST be kept exactly — none added, dropped, or aged up or down. If the user names an art "
            "style or medium ('animated picture', 'cartoon', 'anime', 'watercolor', 'oil painting', "
            "'pixel art' …), OPEN the prompt by stating that style emphatically (e.g. 'A vibrant 3D "
            "animated cartoon-style illustration, stylized characters with expressive faces, NOT "
            "photorealistic') and use that style's vocabulary throughout — no camera or lens language. "
            "Only when no style is named, write it photorealistic with camera/lens detail. Describe EACH "
            "named person as their own clause with concrete age cues (e.g. '10 year old son' becomes "
            "'their 10-year-old son, a school-age boy a head shorter than the adults'; '20 year old "
            "daughter' becomes 'their 20-year-old daughter, a young adult woman'), repeating the "
            "ethnicity for each person, and state the total number of people ('exactly four people'). "
            "When several people are specified keep every face in sharp focus — no shallow depth of "
            "field. Keep it under 100 words. Output ONLY the prompt text — no preamble, no quotes, no lists."
        )
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0, "options": {"temperature": 0.7, "num_predict": 220}}, timeout=180)
            return (r.json().get("response") or "").strip().strip('"') or prompt
        except Exception:
            return prompt

    def _enhance_video(self, prompt):
        """Expand a terse idea ('guy shooting hoops') into a detailed video prompt. Wan needs the
        action spelled out step by step — terse prompts give tiny hoops and balls falling from the
        sky. Uses the big chat model briefly; _free_vram clears it before ComfyUI starts."""
        if len(prompt.split()) > 40:
            return prompt  # already detailed
        sys = ("You write prompts for the Wan text-to-video model. Expand the user's idea into ONE "
               "vivid video prompt: the subject and their appearance, the action described step by "
               "step in the order it happens, the setting, camera angle and movement, lighting and "
               "style. The user's explicit specifications are HARD requirements: every gender, age, "
               "ethnicity, person count and named object MUST be kept exactly and stated explicitly "
               "('a man' stays 'a man' — never a generic 'rider'/'person' the model can recast as "
               "someone else). Be physically precise about sizes, proportions, distances, and how "
               "objects move and interact (e.g. a basketball hoop is 3 m high; the ball leaves the "
               "player's hands, arcs through the air, and drops through the net). Keep it under 90 "
               "words. Output ONLY the prompt text — no preamble, no quotes.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": prompt, "stream": False,
                      "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.7, "num_predict": 250}}, timeout=240)
            out = (r.json().get("response") or "").strip().strip('"')
            return out or prompt
        except Exception:
            return prompt

    def _build_edit_wf(self, instruction, name, seed, cfg, steps, negative):
        # Full-quality edit (no 4-step speed LoRA): 20 steps, cfg 4 → new elements blend into the
        # scene's lighting/grain instead of looking pasted-on. ~2 min. (Fast path lives in the
        # dedicated "Image" model's EDIT_QUALITY valve.)
        return {
          "u":    {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "Qwen-Image-Edit-2509-Q4_K_M.gguf"}},
          "msaf": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["u", 0], "shift": 3.0}},
          "cfgn": {"class_type": "CFGNorm", "inputs": {"model": ["msaf", 0], "strength": 1.0}},
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
          "v":    {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
          "ld":   {"class_type": "LoadImage", "inputs": {"image": name}},
          "sc":   {"class_type": "FluxKontextImageScale", "inputs": {"image": ["ld", 0]}},
          "pos":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": instruction}},
          "neg":  {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "vae": ["v", 0], "image1": ["sc", 0], "prompt": negative}},
          "enc":  {"class_type": "VAEEncode", "inputs": {"pixels": ["sc", 0], "vae": ["v", 0]}},
          "k":    {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                      "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                      "model": ["cfgn", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["enc", 0]}},
          "d":    {"class_type": "VAEDecode", "inputs": {"samples": ["k", 0], "vae": ["v", 0]}},
          "s":    {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui_edit", "images": ["d", 0]}},
        }

    def _build_t2i_wf(self, prompt, seed):
        # Krea 2 Turbo text-to-image (8-step; negative is zeroed conditioning, cfg 1).
        # LoRA + size mirror the "Image" pipe's valves so both produce the same subject (F14).
        model_ref = ["u", 0]
        wf = {
          "u":   {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2/krea2_turbo_fp8_scaled.safetensors", "weight_dtype": "default"}},
          "c":   {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
          "v":   {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
          "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["c", 0]}},
          "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
          "5":   {"class_type": "EmptySD3LatentImage", "inputs": {"width": IMG_T2I_W, "height": IMG_T2I_H, "batch_size": 1}},
          "8":   {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["v", 0]}},
          "9":   {"class_type": "SaveImage", "inputs": {"filename_prefix": "owui", "images": ["8", 0]}},
        }
        if IMG_T2I_LORA.strip():
            wf["lora"] = {"class_type": "LoraLoaderModelOnly",
                          "inputs": {"model": ["u", 0], "lora_name": IMG_T2I_LORA.strip(),
                                     "strength_model": IMG_T2I_LORA_STRENGTH}}
            model_ref = ["lora", 0]
        wf["3"] = {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1.0,
                    "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0,
                    "model": model_ref, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["5", 0]}}
        return wf

    def _gen_image(self, prompt, ref_b64, msgs=None):
        # Free ComfyUI's VRAM up front so the dolphin/gemma prompt-rewrite helpers below don't load
        # into a card ComfyUI still occupies (~13.7 GB) and run partly on CPU.
        self._comfy_free()
        # Attached image → instruction edit with Qwen-Image-Edit 2509.
        if ref_b64:
            instruction = prompt or "improve the overall quality, keep everything else the same"
            negative = ""
            style = self._style_conversion(prompt) if prompt else None
            if style:  # whole-image restyle: purpose-built instruction, hard sampler push
                instruction, negative = style
                instruction = self._style_enrich(instruction, msgs)  # before _free_vram (gemma)
                cfg, steps = 6.0, 24
            else:
                if prompt:  # rewrite BEFORE _free_vram so Gemma isn't unloaded and reloaded
                    instruction, negative = self._enhance_edit(instruction, msgs)
                cfg, steps = (6.0, 24) if self._edit_boost(prompt) else (4.0, 20)
            self._free_vram()
            try:
                name = self._upload(ref_b64)
            except Exception as e:
                return f"⚠️ Could not upload the image to edit: {e}"
            wf = self._build_edit_wf(instruction, name, random.randint(0, 2**31), cfg, steps, negative)
            data, err, _ = self._submit_poll(wf, "s", "Edit", 1200)
            if err:
                return err
            # Vision QA: did the edit deliver what was asked? Up to two harder retries if not.
            qa_note = ""
            if IMG_VERIFY and prompt:
                cur = instruction
                for _round in (1, 2):
                    self._comfy_free()
                    ok, fix = self._verify_image(instruction, base64.b64encode(data).decode())
                    if ok or not fix:
                        break
                    cur = f"{cur} IMPORTANT correction: {fix}"
                    wf = self._build_edit_wf(cur, name, random.randint(0, 2**31), 6.0, 24, negative)
                    self._free_vram()
                    data2, err2, _ = self._submit_poll(wf, "s", "Edit", 1200)
                    if data2 is None:
                        qa_note = f"\n\n*⚠️ QA flagged: {fix} — automatic correction failed ({err2})*"
                        break
                    data = data2
            self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
            return f"![{instruction[:50]}](data:image/png;base64,{base64.b64encode(data).decode()}){qa_note}"

        # No image → fresh Krea 2 Turbo text-to-image.
        raw = prompt
        if IMG_ENHANCE and prompt:
            prompt = self._enhance(prompt)
        if IMG_T2I_LORA.strip() and IMG_T2I_TRIGGER.strip():  # LoRA trigger prefix (parity w/ Image pipe)
            prompt = f"{IMG_T2I_TRIGGER.strip()}, {prompt}"
        self._free_vram()
        wf = self._build_t2i_wf(prompt, random.randint(0, 2**31))
        data, err, _ = self._submit_poll(wf, "9", "Image", 360)
        if err:
            return err
        # Vision QA against the user's ORIGINAL wording (the ground truth for hard constraints).
        # Up to two correction rounds: a fresh re-roll at cfg 1 usually repeats the mistake
        # (e.g. an extra child), so FIX the produced image with the instruction editor instead —
        # it is precisely good at "remove the extra X / add the missing Y" and keeps the scene.
        qa_note = ""
        if IMG_VERIFY and raw:
            for _round in (1, 2):
                self._comfy_free()
                ok, fix = self._verify_image(raw, base64.b64encode(data).decode())
                if ok or not fix:
                    break
                try:
                    fix_ref = self._upload(base64.b64encode(data).decode())
                except Exception as e:
                    qa_note = f"\n\n*⚠️ QA flagged: {fix} — auto-correction could not upload ({e})*"
                    break
                self._free_vram()
                wf = self._build_edit_wf(f"{fix} Keep everyone else and the scene exactly the same.",
                                         fix_ref, random.randint(0, 2**31), 4.0, 20, "")
                data2, err2, _ = self._submit_poll(wf, "s", "Edit", 1200)
                if data2 is None:
                    qa_note = f"\n\n*⚠️ QA flagged: {fix} — automatic correction failed ({err2})*"
                    break
                data = data2
        self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
        return f"![{prompt[:50]}](data:image/png;base64,{base64.b64encode(data).decode()}){qa_note}"

    # Standard Wan negative prompt (recommended by the model authors).
    _VID_NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
                "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
                "形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")

    def _wf_video_5b(self, prompt, seed, w=V_W, h=V_H, length=V_LEN_5B):
        """Wan 2.2 TI2V 5B — fast (~90 s) but weak at complex human action. Honors the per-request
        width/height/length (the 5B supports 1280x704) instead of always emitting 832x480x49."""
        return {
          "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
          "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
          "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
          "5": {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["2", 0]}},
          "6": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": w, "height": h, "length": length, "batch_size": 1}},
          "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
          "8": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": V_STEPS_5B, "cfg": 5.0,
                    "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
                    "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
          "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
          "save": {"class_type": "SaveWEBM", "inputs": {"images": ["9", 0], "filename_prefix": "owui_vid", "codec": "vp9", "fps": float(V_FPS_5B), "crf": 32.0}},
        }

    def _expert_chain(self, wf, tag, unet, lora, strength, riflex_latent=None):
        """One Wan expert: GGUF unet → Lightning LoRA → SageAttention patch → torch.compile →
        ModelSamplingSD3(shift 5) → optional RifleX RoPE (for >81-frame clips).
        Returns the model ref to feed a sampler. Sage/compile are A14B-ONLY: sage produces
        pure noise on the TI2V 5B and breaks Krea/Qwen — never move these into other graphs."""
        wf[f"u{tag}"] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}}
        src = [f"u{tag}", 0]
        if lora:
            wf[f"l{tag}"] = {"class_type": "LoraLoaderModelOnly",
                             "inputs": {"model": src, "lora_name": lora, "strength_model": strength}}
            src = [f"l{tag}", 0]
        if V_SAGE:
            wf[f"sg{tag}"] = {"class_type": "PathchSageAttentionKJ",  # (sic — upstream typo)
                              "inputs": {"model": src, "sage_attention": "auto", "allow_compile": True}}
            src = [f"sg{tag}", 0]
        if V_COMPILE:
            wf[f"tc{tag}"] = {"class_type": "TorchCompileModel",
                              "inputs": {"model": src, "backend": "inductor"}}
            src = [f"tc{tag}", 0]
        wf[f"m{tag}"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": src, "shift": 5.0}}
        src = [f"m{tag}", 0]
        if riflex_latent is not None:
            wf[f"rx{tag}"] = {"class_type": "ApplyRifleXRoPE_WanVideo",
                              "inputs": {"model": src, "latent": riflex_latent, "k": 6}}
            src = [f"rx{tag}", 0]
        return src

    def _post_chain(self, wf, frames_ref, length, last_frame=False, color_ref=None):
        """Shared tail: optional ColorMatch (multi-shot grading anchor) → RIFE 2x interpolation
        (16→32 fps; the SaveWEBM fps MUST scale with the multiplier) → SaveWEBM; optionally also
        saves the clip's last frame as a PNG for multi-shot handoff."""
        src = frames_ref
        if color_ref is not None:
            wf["cm"] = {"class_type": "ColorMatch",
                        "inputs": {"image_ref": color_ref, "image_target": src, "method": "mkl"}}
            src = ["cm", 0]
        wf["rife"] = {"class_type": "RIFE VFI", "inputs": {
            "ckpt_name": "rife47.pth", "frames": src, "clear_cache_after_n_frames": 10,
            "multiplier": V_RIFE, "fast_mode": False, "ensemble": True, "scale_factor": 1.0,
            "dtype": "float32", "torch_compile": False, "batch_size": 1}}
        wf["save"] = {"class_type": "SaveWEBM", "inputs": {
            "images": ["rife", 0], "filename_prefix": "owui_vid", "codec": "vp9",
            "fps": float(V_FPS_14B * V_RIFE), "crf": 30.0}}
        if last_frame:
            wf["lastf"] = {"class_type": "ImageFromBatch",
                           "inputs": {"image": src, "batch_index": length - 1, "length": 1}}
            wf["savef"] = {"class_type": "SaveImage",
                           "inputs": {"images": ["lastf", 0], "filename_prefix": "owui_lastframe"}}

    def _wf_video_14b(self, prompt, seed, w=V_W, h=V_H, length=V_LEN_14B, last_frame=False):
        """Wan 2.2 T2V A14B two-expert + Lightning 250928 LoRAs, 6 steps with the motion recipe:
        the HIGH stage runs cfg 3 / LoRA 0.8 (recovers base-model motion strength and prompt
        adherence that cfg-1 distillation kills — and makes the negative prompt ACTIVE there);
        the LOW stage runs the standard cfg 1 Lightning finish."""
        wf = {
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "vae":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        }
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["clip", 0]}}
        wf["lat"] = {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": w, "height": h, "length": length, "batch_size": 1}}
        rif = ["lat", 0] if length > V_LEN_14B else None
        mh = self._expert_chain(wf, "h", "wan2.2/Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_T2V_A14B_4step_HIGH_250928.safetensors", V_HIGH_LORA, rif)
        ml = self._expert_chain(wf, "l", "wan2.2/Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_T2V_A14B_4step_LOW_250928.safetensors", 1.0, rif)
        split = V_STEPS_14B // 2
        wf["s1"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "enable", "noise_seed": seed,
                    "steps": V_STEPS_14B, "cfg": V_HIGH_CFG, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 0, "end_at_step": split, "return_with_leftover_noise": "enable",
                    "model": mh, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}}
        wf["s2"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "disable", "noise_seed": seed,
                    "steps": V_STEPS_14B, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": split, "end_at_step": 10000, "return_with_leftover_noise": "disable",
                    "model": ml, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["s1", 0]}}
        wf["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["s2", 0], "vae": ["vae", 0]}}
        self._post_chain(wf, ["dec", 0], length, last_frame=last_frame)
        return wf

    def _wf_video_i2v(self, prompt, seed, w, h, length, start_name, ref_name):
        """Wan 2.2 I2V A14B continuation shot: starts from the previous shot's last frame,
        ColorMatched against the sequence's reference frame (colors drift when chaining —
        known Wan issue). Standard I2V Lightning Seko-V1 recipe: 4 steps (2+2), cfg 1."""
        wf = {
          "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
          "vae":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        }
        wf["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
        wf["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": self._VID_NEG, "clip": ["clip", 0]}}
        wf["simg"] = {"class_type": "LoadImage", "inputs": {"image": start_name}}
        wf["i2v"] = {"class_type": "WanImageToVideo", "inputs": {
            "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
            "width": w, "height": h, "length": length, "batch_size": 1, "start_image": ["simg", 0]}}
        mh = self._expert_chain(wf, "h", "wan2.2/Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_I2V_A14B_4step_HIGH_SekoV1.safetensors", 1.0)
        ml = self._expert_chain(wf, "l", "wan2.2/Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf",
                                "wan2.2/Wan22_I2V_A14B_4step_LOW_SekoV1.safetensors", 1.0)
        wf["s1"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "enable", "noise_seed": seed,
                    "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 0, "end_at_step": 2, "return_with_leftover_noise": "enable",
                    "model": mh, "positive": ["i2v", 0], "negative": ["i2v", 1], "latent_image": ["i2v", 2]}}
        wf["s2"] = {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "disable", "noise_seed": seed,
                    "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                    "start_at_step": 2, "end_at_step": 10000, "return_with_leftover_noise": "disable",
                    "model": ml, "positive": ["i2v", 0], "negative": ["i2v", 1], "latent_image": ["s1", 0]}}
        wf["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["s2", 0], "vae": ["vae", 0]}}
        wf["refimg"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        self._post_chain(wf, ["dec", 0], length, last_frame=True, color_ref=["refimg", 0])
        return wf

    # ---------- video request options / multi-shot ----------
    _LEN_KW = re.compile(r"\b(long(er)?|extend|lengthen)\b(\s+(the\s+)?(video|clip|it))?|"
                         r"\bmake\s+(it|the\s+(video|clip))\s+longer\b|\b[6-9]\s*seconds?\b|"
                         r"\b1[0-9]\s*seconds?\b", re.I)

    def _video_opts(self, text, base=None):
        """Per-request controls parsed from the message: '720p'/'hq' → native 720p,
        'quick/fast/draft video' → 5B fast path, 'longer'/'extend'/'7 seconds' → RifleX 121f.
        When `base` is given (a follow-up on an existing clip) start from it and override ONLY the
        fields the new message explicitly names, so the original clip's resolution/length carry over."""
        t = (text or "").lower()
        o = dict(base) if base else {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}
        if re.search(r"\b(720p|1080p|hq|high[- ]?quality|high[- ]?res(olution)?)\b", t):
            o["w"], o["h"] = V_W_HQ, V_H_HQ
        if re.search(r"\b(quick|fast|draft)\s+(video|clip)\b", t):
            o["fast"] = True
        if self._LEN_KW.search(t):
            o["length"] = V_LEN_LONG
        return o

    def _is_length_only(self, text):
        """True if the message is purely a duration change ('make it longer', 'extend it',
        '10 seconds') with no new visual content — re-render the SAME prompt+seed at the new
        length instead of re-merging (which would perturb the scene)."""
        t = (text or "").lower().strip()
        if not self._LEN_KW.search(t):
            return False
        rest = re.sub(r"\b(make|it|the|this|video|clip|please|a|bit|much|way|can|you|could|would|"
                      r"long(er)?|extend|lengthen|to|now|and|of|seconds?|secs?|s)\b|\d+|[^\w\s]", " ", t)
        return not re.search(r"[a-z]{3,}", rest)

    def _strip_video_directives(self, t):
        t = re.sub(r"\b(in\s+)?(720p|1080p|hq|high[- ]?quality|high[- ]?res(olution)?)\b", "", t, flags=re.I)
        t = re.sub(r"\b(quick|fast|draft)\s+(video|clip)\b", "video", t, flags=re.I)
        return re.sub(r"\s{2,}", " ", t).strip()

    def _wants_multishot(self, t):
        """Return the shot count for long/sequenced requests (0 = single shot).
        Triggers on '15 second video' (>=10 s) or 'X then Y' sequencing."""
        t = (t or "").lower()
        m = re.search(r"\b(\d{1,3})\s*(?:seconds?|secs?)\b", t)
        if m and int(m.group(1)) >= 10:
            return min(V_SHOT_MAX, max(2, round(int(m.group(1)) / 5)))
        if re.search(r"\bthen\b", t):
            return min(V_SHOT_MAX, max(2, len(re.split(r"\bthen\b", t))))
        return 0

    def _plan_shots(self, text, n):
        """Ask the local LLM to direct the request as n consecutive 5-second shots, each written
        as a full video prompt with consistent subjects across shots. Returns list[str] or None."""
        sys = (f"You are a film director planning {n} CONSECUTIVE 5-second shots for a text-to-video "
               "model. Each shot continues exactly where the previous one ended (same subjects, "
               "same look, continuous action). Write each shot as ONE detailed video prompt: subject "
               "and appearance (repeat it in every shot for consistency), the action step by step, "
               "setting, camera, lighting. Under 60 words per shot. "
               f"Return STRICT JSON only: an array of exactly {n} strings.")
        try:
            r = requests.post(f"{self.ollama}/api/generate",
                json={"model": self.chat_model, "system": sys, "prompt": text, "stream": False,
                      "think": False, "keep_alive": 0,
                      "options": {"temperature": 0.6, "num_predict": 900}}, timeout=300)
            out = (r.json().get("response") or "")
            m = re.search(r"\[.*\]", out, re.S)
            shots = [str(s).strip() for s in json.loads(m.group(0)) if str(s).strip()]
            if len(shots) >= 2:
                return shots[:V_SHOT_MAX]
        except Exception:
            pass
        return None

    def _concat_webms(self, segs):
        """Join segment webms into one clip with the ffmpeg inside this (OpenWebUI) container.
        Tries stream-copy first (same codec/params), falls back to re-encoding."""
        import os, shutil, subprocess, tempfile
        d = tempfile.mkdtemp(prefix="vidcat_")
        try:
            paths = []
            for i, b in enumerate(segs):
                p = os.path.join(d, f"seg{i}.webm")
                open(p, "wb").write(b)
                paths.append(p)
            lst = os.path.join(d, "list.txt")
            open(lst, "w").write("".join(f"file '{p}'\n" for p in paths))
            out = os.path.join(d, "out.webm")
            r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out],
                               capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                                    "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", out],
                                   capture_output=True, timeout=1800)
                if r.returncode != 0:
                    return None
            return open(out, "rb").read()
        except Exception:
            return None  # e.g. no ffmpeg binary in this environment
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _gen_multishot(self, text, seed, opts, n):
        """Plan → shot 1 (T2V) → shots 2..n (I2V from the previous last frame, ColorMatched to
        shot 1) → ffmpeg concat. Returns the chat reply string, or None to fall back to a
        single-shot generation."""
        self._comfy_free()  # free ComfyUI before the dolphin shot-planning helper loads
        shots = self._plan_shots(text, n)
        if not shots:
            return None
        self._free_vram()
        segs, ref_name, prev_name = [], None, None
        for i, shot in enumerate(shots):
            def build(p):
                if i == 0:
                    return self._wf_video_14b(p, seed, opts["w"], opts["h"], V_LEN_14B, last_frame=True)
                return self._wf_video_i2v(p, seed + i, opts["w"], opts["h"], V_LEN_14B, prev_name, ref_name)
            data, err, extras = self._submit_poll(build(shot), "save", f"Video (shot {i+1}/{len(shots)})", 900,
                                                  extra_nodes=("savef",))
            if err:
                return f"{err}\n\n(multi-shot sequence failed at shot {i+1}/{len(shots)})"
            # Frame QA per shot, BEFORE its last frame seeds the next shot — a wrong shot would
            # otherwise propagate down the whole chain via the I2V handoff. The retried shot
            # replaces both the segment AND the handoff frame. In "anchors" mode only the
            # chain-critical shots (first = identity/style anchor, last = final frame) are verified;
            # intermediate shots inherit shot 1's look via the I2V handoff, so re-running the full
            # Wan stack + gemma4 on every one of them mostly burns time for little gain.
            verify_shot = VID_VERIFY and (VID_VERIFY_MODE == "all"
                                          or i == 0 or i == len(shots) - 1)
            if verify_shot:
                frame = self._video_frame(data)
                if frame:
                    self._comfy_free()
                    ok, fix = self._verify_image(shot, frame)
                    if not ok and fix:
                        self._free_vram()
                        data2, err2, extras2 = self._submit_poll(
                            build(f"{shot} IMPORTANT: {fix}"), "save",
                            f"Video (shot {i+1}/{len(shots)} retry)", 900, extra_nodes=("savef",))
                        if data2 is not None:
                            data, extras = data2, extras2
            segs.append(data)
            fb = extras.get("savef")
            if fb is None:
                return f"⚠️ Shot {i+1} produced no handoff frame for the next shot."
            try:
                prev_name = self._upload(base64.b64encode(fb).decode())
            except Exception as e:
                return f"⚠️ Could not hand shot {i+1}'s last frame to shot {i+2}: {e}"
            if ref_name is None:
                ref_name = prev_name
        out = self._concat_webms(segs)
        if not out:
            return "⚠️ Generated all shots but could not join them into one clip."
        b64 = base64.b64encode(out).decode()
        joined = " || ".join(shots)
        p64 = base64.b64encode(joined.encode()).decode()
        return (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                f'controls loop muted playsinline '
                f'style="max-width:100%;border-radius:8px">\n'
                f'data:video/webm;base64,{b64}\n</video>\n\n'
                f'*🎬 {len(shots)}-shot sequence — {shots[0][:60]}…*')

    def _video_frame(self, webm_bytes):
        """Middle-ish frame of a webm as base64 PNG, via the ffmpeg inside this container.
        Returns None on any failure (QA then just skips)."""
        import os, subprocess, tempfile
        d = tempfile.mkdtemp(prefix="vidqa_")
        try:
            vid = os.path.join(d, "clip.webm")
            open(vid, "wb").write(webm_bytes)
            png = os.path.join(d, "frame.png")
            r = subprocess.run(["ffmpeg", "-y", "-ss", "2", "-i", vid, "-vframes", "1", png],
                               capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(png):  # clip shorter than 2 s → first frame
                r = subprocess.run(["ffmpeg", "-y", "-i", vid, "-vframes", "1", png],
                                   capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.exists(png):
                    return None
            return base64.b64encode(open(png, "rb").read()).decode()
        except Exception:
            return None
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def _gen_video(self, prompt, seed=None, opts=None, check=None):
        seed = seed if seed is not None else random.randint(0, 2**31)
        opts = opts or {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}

        def build(p):
            if V_QUALITY == "best" and not opts.get("fast"):
                return self._wf_video_14b(p, seed, opts["w"], opts["h"], opts["length"])
            # 5B fast path now honors the parsed resolution; length only stretches on explicit 'longer'
            length5 = V_LEN_LONG if opts.get("length", 0) >= V_LEN_LONG else V_LEN_5B
            return self._wf_video_5b(p, seed, opts["w"], opts["h"], length5)

        self._free_vram()
        data, err, _ = self._submit_poll(build(prompt), "save", "Video", 900)
        if err:
            return err
        # Frame QA: does the clip match the request (gender, subject, setting)? One corrected
        # retry with the fix appended — same seed, so the scene stays recognizably similar.
        if VID_VERIFY and check:
            frame = self._video_frame(data)
            if frame:
                self._comfy_free()
                ok, fix = self._verify_image(check, frame)
                if not ok and fix:
                    self._free_vram()
                    data2, err2, _ = self._submit_poll(build(f"{prompt} IMPORTANT: {fix}"), "save", "Video", 900)
                    if data2 is not None:
                        data = data2
        b64 = base64.b64encode(data).decode()
        # `video` is NOT a markdown block-level tag, so the opening <video> tag MUST sit alone on
        # its line (CommonMark "type-7" HTML block) for marked to keep the whole element in ONE
        # html token; OpenWebUI then reads the src from the text between the tags
        # (/<video[^>]*>([\s\S]*?)<\/video>/). If content shares the tag's line it splits into
        # inline tokens, the regex misses, and OpenWebUI dumps the raw tag as escaped text.
        # data-p64/data-seed make the message self-describing so a follow-up "change the X"
        # can rebuild this exact clip's prompt+seed even after a restart.
        p64 = base64.b64encode(prompt.encode()).decode()
        return (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                f'controls loop muted playsinline '
                f'style="max-width:100%;border-radius:8px">\n'
                f'data:video/webm;base64,{b64}\n</video>\n\n*🎬 {prompt[:80]}*')

    async def _achat_stream(self, messages):
        # Guard: keep Gemma from inventing "dalle"/tool-call JSON — image work is routed automatically.
        guard = {"role": "system", "content": (
            "You are a friendly, concise assistant in a chat app. Image generation and editing are "
            "handled automatically by the app, not by you. Always reply in plain, natural language. "
            "NEVER output JSON, tool calls, function calls, or an \"action\"/\"dalle\"/\"text2im\" object. "
            "If the user asks to create or edit a picture, just acknowledge briefly in words.")}
        messages = [guard] + [m for m in messages if m.get("role") != "system"]
        # Route on the CURRENT turn only: gemma4 (vision) when the LATEST user turn carries an image,
        # else dolphin. Scanning the whole history pinned every later text turn to gemma4 forever
        # after a single image appeared, forcing a needless dolphin<->gemma4 eviction each turn.
        last_user_has_img = next(
            (bool(m.get("images")) for m in reversed(messages) if m.get("role") == "user"), False)
        if last_user_has_img:
            model = self.vision_model
        else:
            model = self.chat_model
            # dolphin is text-only — drop any stale image arrays from history so it never sees images[]
            messages = [{k: v for k, v in m.items() if k != "images"} for m in messages]
        try:
            # sock_read (not a fixed total) catches an idle hang without killing a long, actively
            # streaming reply.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{self.ollama}/api/chat",
                                  json={"model": model, "messages": messages,
                                        "stream": True, "think": False}) as r:
                    if r.status != 200:
                        body = (await r.text())[:300]
                        yield f"⚠️ Ollama HTTP {r.status} from {model}: {body}"
                        return
                    async for line in r.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("error"):
                            yield f"⚠️ Ollama error: {d['error']}"
                            return
                        tok = (d.get("message") or {}).get("content", "")
                        if tok:
                            yield tok
        except Exception as e:
            yield f"⚠️ Chat backend error: {e}"

    async def _gen_and_cache(self, cid, prompt, ref, msgs=None):
        def run():
            with _GEN_LOCK:  # only one VRAM-manipulating pipeline at a time
                return self._gen_image(prompt, ref, msgs)
        result = await asyncio.to_thread(run)
        b64 = self._extract_b64(result)
        if b64:  # remember the produced image so a later "make it bigger" can edit it (true LRU)
            self._recent.pop(cid, None)  # move-to-end so an active chat isn't evicted first
            self._recent[cid] = b64
            while len(self._recent) > 30:
                self._recent.pop(next(iter(self._recent)))
        return result

    def _cache_video(self, cid, prompt, seed, opts=None):
        self._recent_video.pop(cid, None)  # move-to-end → true LRU (not FIFO)
        self._recent_video[cid] = (prompt, seed, opts)
        while len(self._recent_video) > 30:
            self._recent_video.pop(next(iter(self._recent_video)))

    async def _gen_video_and_cache(self, cid, prompt, seed=None, enhance=False, opts=None):
        seed = seed if seed is not None else random.randint(0, 2**31)

        def run():
            with _GEN_LOCK:  # only one VRAM-manipulating pipeline at a time
                self._comfy_free()  # free ComfyUI before the dolphin enhance helper loads
                p = self._enhance_video(prompt) if enhance else prompt
                # QA judges against the pre-enhancement wording (the user's ground truth) — the
                # enhanced prompt could itself have dropped a spec.
                return p, self._gen_video(p, seed, opts, check=prompt)

        used_prompt, result = await asyncio.to_thread(run)
        if result.lstrip().startswith("<video"):  # remember so "change the sky" can iterate on it
            self._cache_video(cid, used_prompt, seed, opts)
        return result

    async def _gen_i2v_and_cache(self, cid, img_b64, prompt, opts=None, seed=None):
        """Animate a still (attached or just-generated) with Wan 2.2 I2V — text-directed motion
        FROM the image. Distinct from the Animate pipe (SCAIL motion-transfer off a driving clip)."""
        seed = seed if seed is not None else random.randint(0, 2**31)
        opts = opts or {"w": V_W, "h": V_H, "length": V_LEN_14B, "fast": False}

        def run():
          with _GEN_LOCK:  # only one VRAM-manipulating pipeline at a time
            self._comfy_free()  # free ComfyUI before the dolphin motion-prompt helper loads
            motion = self._enhance_video(prompt) if prompt else "natural, gentle cinematic motion"
            self._free_vram()
            try:
                name = self._upload(img_b64)
            except Exception as e:
                return None, f"⚠️ Could not upload the image to animate: {e}"
            wf = self._wf_video_i2v(motion, seed, opts["w"], opts["h"],
                                    opts.get("length", V_LEN_14B), name, name)
            data, err, _ = self._submit_poll(wf, "save", "Video", 900)
            self._comfy_free()  # idle ⇒ GPU empty for the next chat turn
            if err:
                return None, err
            b64 = base64.b64encode(data).decode()
            p64 = base64.b64encode(motion.encode()).decode()
            return motion, (f'<video data-p64="{p64}" data-seed="{seed}" data-opts="{self._opts_attr(opts)}" '
                            f'controls loop muted playsinline style="max-width:100%;border-radius:8px">\n'
                            f'data:video/webm;base64,{b64}\n</video>\n\n*🎬 {(prompt or motion)[:80]}*')

        used_prompt, result = await asyncio.to_thread(run)
        if isinstance(result, str) and result.lstrip().startswith("<video"):
            self._cache_video(cid, used_prompt, seed, opts)
        return result

    async def _gen_multishot_and_cache(self, cid, text, n, opts, seed=None):
        seed = seed if seed is not None else random.randint(0, 2**31)
        def run():
            with _GEN_LOCK:  # only one VRAM-manipulating pipeline at a time
                return self._gen_multishot(text, seed, opts, n)
        result = await asyncio.to_thread(run)
        if result is None:  # shot planning failed → fall back to one enhanced clip
            return await self._gen_video_and_cache(cid, text, seed, enhance=VID_ENHANCE, opts=opts)
        if result.lstrip().startswith("<video"):
            v = re.search(r'data-p64="([A-Za-z0-9+/=]*)"', result)
            try:
                joined = base64.b64decode(v.group(1)).decode("utf-8", "ignore") if v else text
            except Exception:
                joined = text
            self._cache_video(cid, joined, seed, opts)
        return result

    async def pipe(self, body: dict, __metadata__=None):
        msgs = body.get("messages", [])
        text, ref = self._last_user(msgs)
        cid = self._chat_id(body, __metadata__)
        # What media does this conversation currently revolve around?
        kind, media = self._recent_media(msgs)
        if kind is None:  # history scan found nothing → in-memory caches (this conversation only)
            if cid in self._recent_video:
                kind, media = "video", self._recent_video[cid]
            elif cid in self._recent:
                kind, media = "image", self._recent[cid]
        # "make it animated / a cartoon" while an image is on the table is a STYLE EDIT of that
        # image, not a video ("make a video of it / animate this / make it move" still are).
        style_edit = kind == "image" and text and not ref and self._style_conversion(text)
        # Image on the table + a motion request → animate THAT image with Wan I2V (text-directed
        # motion). A freshly attached image always qualifies; a previously-generated image only when
        # the message refers to it ('animate this', 'bring it to life') rather than naming a new scene.
        anim_img = ref or (media if kind == "image" else None)
        refers_to_img = bool(re.search(r"\b(this|it|that|the\s+(image|photo|picture|drawing|pic))\b|"
                                       r"\banimate\b|\bbring\b.*\blife\b", (text or "").lower()))
        if (text and anim_img and not style_edit and (ref or refers_to_img)
                and (self._is_video_request(text) or self._wants_new_video(text))):
            opts = self._video_opts(text)
            motion = self._strip_video_directives(self._clean_prompt(text))
            return await self._gen_i2v_and_cache(cid, anim_img, motion, opts)
        # Fresh video: explicit ("create a video of …") or video-flavored wording with no video yet.
        if text and not ref and not style_edit and (self._wants_new_video(text)
                                 or (self._is_video_request(text) and kind != "video")):
            opts = self._video_opts(text)
            cleaned = self._strip_video_directives(self._clean_prompt(text))
            n = self._wants_multishot(text)
            # A multi-shot sequence needs cross-shot consistency (best-quality A14B chain), so it
            # overrides 'fast' rather than being silently dropped to a single 5B clip.
            if n >= 2:
                return await self._gen_multishot_and_cache(cid, cleaned, n, opts)
            return await self._gen_video_and_cache(cid, cleaned, enhance=VID_ENHANCE, opts=opts)
        # Fresh image generation ("create/draw a …") → a brand-new image, even mid-conversation.
        # (unless it's a restyle of the image on the table — "make this picture realistic" — which
        # must fall through to the EDIT path below, not t2i a mangled prompt from scratch)
        if text and not ref and not style_edit and self._is_image_request(text):
            return await self._gen_and_cache(cid, self._clean_prompt(text), None)
        # Follow-up about the most recent VIDEO → regenerate it with the change folded into the
        # original prompt, SAME seed (keeps the scene recognizably similar). Multi-shot histories
        # (shots joined with ' || ') are re-planned with the change applied.
        if text and not ref and kind == "video" and (self._wants_edit(text) or self._is_length_only(text)):
            prev_prompt, prev_seed, prev_opts = media
            # Start from the original clip's opts; only override what the message explicitly names,
            # so a follow-up keeps the original 720p/length instead of silently resetting to defaults.
            opts = self._video_opts(text, base=prev_opts)
            is_multishot = " || " in (prev_prompt or "")
            # A pure duration change ('make it longer') → re-render the SAME prompt+seed at the new
            # length; merging would perturb the scene for no reason.
            if self._is_length_only(text):
                if is_multishot:
                    n = min(V_SHOT_MAX, max(2, prev_prompt.count(" || ") + 1))
                    base = prev_prompt.replace(" || ", ", then ")
                    return await self._gen_multishot_and_cache(cid, base, n, opts, seed=prev_seed)
                return await self._gen_video_and_cache(cid, prev_prompt or self._clean_prompt(text),
                                                       prev_seed, opts=opts)
            if is_multishot:
                base = prev_prompt.replace(" || ", ", then ")
                merged = await asyncio.to_thread(self._merge_video_prompt, base, text)
                n = min(V_SHOT_MAX, max(2, prev_prompt.count(" || ") + 1))
                return await self._gen_multishot_and_cache(cid, merged, n, opts, seed=prev_seed)
            merged = (await asyncio.to_thread(self._merge_video_prompt, prev_prompt, text)
                      if prev_prompt else self._clean_prompt(text))
            return await self._gen_video_and_cache(cid, merged, prev_seed, opts=opts)
        # Editing an image: an attached one, OR the most recent image in this chat — no re-upload
        # needed. Any non-question/non-smalltalk message here is treated as an edit instruction.
        img = ref or (media if kind == "image" else None)
        if text and img and self._wants_edit(text):
            return await self._gen_and_cache(cid, self._edit_instruction(text), img, msgs)
        # Chat. If the conversation revolves around an image the pipe GENERATED and this turn is a
        # question about it, attach the pixels to the last user message so the vision model (gemma4)
        # actually sees it — otherwise text-only dolphin answers blind (the image was scrubbed).
        omsgs = self._ollama_messages(msgs)
        if kind == "image" and isinstance(media, str) and not ref:
            for m in reversed(omsgs):
                if m.get("role") == "user":
                    if media not in (m.get("images") or []):
                        m["images"] = (m.get("images") or []) + [media]
                    break
        return self._achat_stream(omsgs)

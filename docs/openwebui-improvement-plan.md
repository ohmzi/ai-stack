# OpenWebUI Stack — Improvement Plan

Living execution plan for the **41 findings** from the 139-agent review of the OpenWebUI dolphin-swap + media-orchestration system. Each item is checked off and annotated as it lands.

- **Report (all 41 findings, detail):** https://claude.ai/code/artifact/5ed5b939-ee9e-43a3-a814-e25dd9ace7f1
- **Started:** 2026-07-24   •   **Severity mix:** 17 major / 18 minor / 6 suggestion / 0 critical
- **Progress:** 41 / 41 complete

## How code is deployed here (important)

The **live pipe code lives in the OpenWebUI SQLite DB** (`function.content`), *not* the on-disk `~/ai-stack/pipes/*.py` files — those were a stale Jul-19 copy from before the dolphin swap. Working model:

1. Authoritative deployed code was exported to `~/ai-stack/pipes/live/<function_id>.py` (this is now the source of truth).
2. Edit the file in `live/`, then `python3 -m py_compile` it.
3. Push `content` back into the DB `function` row, and update the `config`/`model` tables for config items.
4. `docker restart open-webui` to reload, then check logs for load errors and run routing unit tests.

### Safety / rollback
- DB backup: `~/ai-stack/pipes/backup-2026-07-24/webui.db.bak` and in-container `/app/backend/data/webui.db.bak-2026-07-24`.
- Disk-pipe backup: `~/ai-stack/pipes/backup-2026-07-24/*.py`.
- Rollback: stop container → restore `webui.db.bak-2026-07-24` → start.

---

## Progress at a glance

| Phase | Scope | Findings | Done |
|---|---|---:|---:|
| 1 | Assistant pipe (`auto_assistant`) | 24 | ✅ 24/24 |
| 2 | Krea 2 Image pipe (`image_krea`) | 5 | ✅ 5/5 |
| 3 | Photoreal pipe (`uncensored`) | 5 | ✅ 5/5 |
| 4 | OpenWebUI config & workspace | 6 | ✅ 6/6 |
| 5 | Remove legacy Video pipe + deploy/verify | 1 | ✅ 1/1 |

---

## Phase 1 — Assistant pipe (`auto_assistant`)

_24 findings (11 major). Status: ✅ complete._

- [x] **F01** 🔴 major · `auto_assistant:L252` — _wants_edit checks edit-verbs before its question guard and defaults to True, so plain questions / non-edit imperatives after any image become Qwen edits on a stale image
    - **Problem:** _wants_edit (def L233) runs `if self._is_edit_request(low): return True` (L240-241) BEFORE the small-talk (L243) and question (L247-251) guards, and _is_edit_request (L163-170) matches very common verbs (make/give/add/put/turn/change/'into a'/'to a'). Worse, the fallthrough at L252 is `return True` for ANY imperative/statement not on the small-talk or question allowlist, and the allowlist omits write/compose/summarize/draft/translate. Compounding it, _recent_media (L195) scans the whole history and returns the first image found forever, so `img = ref or (media if kind=='image')` (L1141) keeps kind=='image' for the rest of the chat. Net: once any image exists, non-edit text is silently routed to a Qwen-Image-Edit run on the old image. Same default-True gate is used for the video follow-up branch (L1128).
    - **Fix:** Evaluate the question/small-talk guards BEFORE _is_edit_request; replace the blanket `return True` with a positive edit test (require the message to reference the image or contain a visual noun, else treat as chat); add write/compose/summarize/draft/translate to the chat allowlist; and only treat as edit when ref is freshly attached or the image appeared in the last 1-2 messages.
    - **Status:** ✅ done — _wants_edit now checks small-talk/question guards first, defaults to CHAT, edits only on an edit-verb or visual-noun reference.

- [x] **F02** 🔴 major · `auto_assistant:L1036` — _achat_stream picks the model from the WHOLE history, so plain chat routes to gemma4 after any image was ever attached (violates 'chat must come from dolphin')
    - **Problem:** `model = self.vision_model if any(m.get('images') for m in messages) else self.chat_model` (L1036) scans the entire converted history. OpenWebUI resends prior image_url parts and _ollama_messages re-attaches images to their original user turn, so the instant one image has appeared, every later text-only turn evaluates any(...)==True and is answered by gemma4:31b (19.9 GB thinking model) instead of dolphin-venice:24b. This violates design intent #1 and forces a dolphin<->gemma4 eviction (they can never co-reside in 24 GB) plus a full model-load latency on every turn. This is the incomplete half of the chat/vision split from intent #6.
    - **Fix:** Route on the CURRENT turn only: use vision_model only when the last user message has images (or the last ~N messages), e.g. `model = self.vision_model if (out and out[-1].get('role')=='user' and out[-1].get('images')) else self.chat_model`; strip images from history when routing to dolphin.
    - **Status:** ✅ done — _achat_stream routes on the LAST user turn only (gemma4 iff it has an image); strips stale images from history for dolphin.

- [x] **F03** 🔴 major · `auto_assistant:L1145` — Questions about a pipe-GENERATED image/video are answered blind by text-only dolphin (gemma4 never sees generated media)
    - **Problem:** _achat_stream selects vision_model only when a message carries an images[] array, which _ollama_messages builds only from USER-attached image_url parts. Images the pipe generated live in assistant messages as markdown data-URIs, and _scrub replaces them with the literal '[generated image]'/'[generated video]' before dolphin sees them. So after the pipe generates media, a pure question about it (_wants_edit returns False for questions) falls to the chat path at L1145 with no image attached, any(images) is False, and dolphin-venice (text-only, no vision) answers blind — even though pipe() already holds the bytes (`img = ref or (media if kind=='image')`, L1141) and _video_frame (L966) can extract a video frame. This contradicts design intent #2 (gemma4 handles chat about images).
    - **Fix:** In pipe()'s chat fallthrough, when kind=='image'/'video' and media is available and the message is a question/reference, attach media (or an extracted video frame) to the last user message's images list so L1036 selects gemma4 with the actual pixels.
    - **Status:** ✅ done — Chat fallthrough attaches a pipe-generated image to the last user turn so gemma4 sees it (video Q&A stays text-only).

- [x] **F04** 🔴 major · `auto_assistant:L112` — _is_video_request fires on bare keywords/idioms (video/clip/gif, 'bring .* to life', 'make it move') with no question guard, hijacking plain chat into Wan video generation
    - **Problem:** _is_video_request (L112) branch 1 matches the bare nouns video|animate|animated|animation|clip|gif|footage, and the alternation also includes 'make it move' and a greedy 'bring .* to life' — all common non-generation idioms — with no interrogative/small-talk filter (unlike _wants_edit). The video branch is checked FIRST in pipe() (L1112), and ui.default_models=auto_assistant.auto, so every new chat is routed here; _wants_new_video (L124) is similarly loose (create/make/...within 25 chars of a video noun), and _strip_still_style only strips an adjacent STILL_NOUN so 'animated movie poster' still routes to video.
    - **Fix:** Gate the bare-noun branch behind a question/small-talk filter and require a generation verb + video-noun-as-object; replace greedy 'bring .* to life' with 'bring (this|it|the (image|photo|picture)) to life'; qualify/drop 'make it move'; blacklist collocations 'video game','clip art'; allow ~2 intervening words in _strip_still_style.
    - **Status:** ✅ done — _is_video_request gated by shared question/small-talk guard; 'bring…to life' scoped; 'video game/clip art/music video' blacklisted.

- [x] **F05** 🔴 major · `auto_assistant:L153` — _is_image_request over-matches: bare verbs draw/paint/sketch and the '<noun> of' pattern route figurative chat / questions about artworks into Krea t2i
    - **Problem:** _is_image_request (L153) returns True on any occurrence of \b(draw|sketch|paint|illustrate)\b (L155) with no object requirement, and pattern 3 r'\b(image|picture|photo|portrait|drawing|painting|...) of\b' (L159) matches with no generation verb and no question guard. The fresh-image branch (L1123) runs before the chat fallback, so these phrasings generate a Krea image from the leftover text.
    - **Fix:** Drop paint/draw from the unconditional alternation and fold them into the object-qualified pattern; require a generation verb within ~30 chars for pattern 3; and apply a shared question/interrogative guard before entering the fresh-image branch at L1123.
    - **Status:** ✅ done — _is_image_request: verb+object wins even in questions; bare draw/paint + '<noun> of' gated by question/small-talk + figurative filter.

- [x] **F06** 🔴 major · `auto_assistant:L1112` — Attached / recent image + a video request is mis-routed: attachment -> Qwen still edit, generated image -> unrelated T2V; the Wan I2V graph is never reachable from a user image
    - **Problem:** Both video branches require `not ref` (L1112, L1128). Case A (attachment): 'make a video of this'/'animate this' + image skips the video branches, skips _is_image_request, and lands in the edit branch (L1141-1143) because _wants_edit matches 'make'/defaults True — Qwen-Image-Edit runs the instruction and returns a still. Case B (recent generated image, no attachment): 'animate this' passes _is_video_request, _clean_prompt strips the verb leaving 'this'/'it', and _enhance_video has dolphin hallucinate an arbitrary scene — the image and its prompt are discarded (self._recent stores only b64, not the prompt). A working I2V graph (_wf_video_i2v, L800) exists but is only used for multishot handoff; _strip_still_style's docstring falsely promises 'animate this' routes to video.
    - **Fix:** Before the edit fallthrough add: if (_is_video_request(text) or _wants_new_video(text)) and (ref or kind=='image'), _upload the image and run a single _wf_video_i2v shot with the motion text as prompt (cache the generated image's prompt for the pronoun case); minimal stopgap is a clear message pointing at the Animate pipe.
    - **Status:** ✅ done — New Wan-I2V branch: attached/referenced image + motion request animates that image (distinct from the Animate pipe).

- [x] **F07** 🔴 major · `auto_assistant:L1130` — Follow-up video edits re-derive opts from the change text only, silently dropping the original clip's 720p/length (same seed at new latent dims = a different scene)
    - **Problem:** The follow-up branch (L1128-1138) computes `opts = self._video_opts(text)` from the follow-up message; the <video> tag and _recent_video persist only (prompt, seed), not (w,h,length,fast). So a clip originally requested '720p'/'longer' regenerates at the default 832x480/81f. Reusing prev_seed is the mechanism meant to keep the scene 'recognizably similar', but a different EmptyHunyuanLatentVideo width/height/length yields a different noise tensor shape, so the same seed produces an entirely different composition — the invariant silently breaks for every hq/long original.
    - **Fix:** Persist opts alongside prompt+seed (data-opts='1280x704x81' on the <video> tag, parse in _recent_media, store in _recent_video); in the follow-up branch start from the stored opts and only override fields the new message explicitly names.
    - **Status:** ✅ done — Video opts (WxHxL) persisted on the <video> tag + caches; follow-ups start from them and override only named fields.

- [x] **F08** 🔴 major · `auto_assistant:L396` — _comfy_free uses a blind fixed 2 s sleep with swallowed errors and no release verification before loading the 19.9 GB gemma4 QA model, feeding a fail-open verify
    - **Problem:** _comfy_free (L390-396) POSTs /free with `except: pass`, then time.sleep(2) and returns, with no confirmation ComfyUI released its allocation — unlike _free_vram which polls /api/ps up to ~30 s. It is the only barrier before every gemma4 vision load (_verify_image at L654,683,936,1007). ComfyUI holds ~13.7-18 GB while a Krea/Qwen/Wan model is resident, and CUDA does not always release that within 2 s (and if the POST errors it is swallowed). If gemma4 (19.9 GB) loads into the remaining ~5-10 GB, Ollama offloads layers to CPU (a 150-token QA balloons to minutes / blows the 300 s timeout) and _verify_image fails open (L427 returns (True,'')), silently skipping the QA the whole loop exists to run.
    - **Fix:** Mirror _free_vram: after POST /free, poll GET /system_stats until devices[0].vram_free >= ~21 GiB (or a ~20-30 s cap); on failure skip verify with a visible note ('*QA skipped: could not free GPU*') instead of failing open silently, and stop swallowing the /free POST error.
    - **Status:** ✅ done — _comfy_free polls /system_stats for real VRAM release + skips while ComfyUI is busy (no blind 2 s sleep).

- [x] **F09** 🔴 major · `auto_assistant:L1145` — Chat path never frees ComfyUI VRAM; several generation exits leave Comfy resident, so the next dolphin/gemma4 chat silently runs part-CPU
    - **Problem:** pipe() returns _achat_stream (L1145) with no VRAM preparation. With --disable-smart-memory ComfyUI holds its last stack (~13.7 GB idle observed) until an explicit /free. The happy QA path exits clean, but every QA-retry exit (661,693,940,1011), every verify-skipped path (QA valves off, or the bare-image edit where L651 'if IMG_VERIFY and prompt' is falsy), and any use of image_krea leave ~13-14 GB resident. The next plain chat loads dolphin (14.3 GB) into ~10 GB free -> Ollama splits layers to CPU, dropping ~53 tok/s to ~5-15 for the whole keep_alive residency; the vision-chat branch (gemma4, 19.9 GB) is far worse (low single-digit tok/s, minutes per reply). The dolphin->ComfyUI direction is safe (every submit is gated by _free_vram); the ComfyUI->ollama direction is the unguarded one.
    - **Fix:** Call self._comfy_free() once at the end of _gen_image/_gen_video/_gen_multishot so 'idle' always means GPU empty; and/or add a cheap _ensure_gpu guard in pipe() before returning _achat_stream (read /system_stats, _comfy_free only if vram_free below the model need).
    - **Status:** ✅ done — _comfy_free added at the end of every image/video/i2v path so idle ⇒ GPU empty for the next chat.

- [x] **F10** 🔴 major · `auto_assistant:L470` — No serialization across concurrent invocations; _free_vram cannot evict a model with an in-flight request and proceeds silently after its 30 s bail-out; chat keep_alive pin is implicit
    - **Problem:** Each pipe() spawns its own to_thread that independently calls _free_vram (which unloads ALL Ollama models, L466-467) and submits ComfyUI jobs — no mutex. If two requests overlap, one thread's _free_vram can evict a model the other just loaded for enhance/verify, and Ollama honors keep_alive:0 only after in-flight requests finish, so while a chat streams (up to 900 s) the unload is a no-op, the 30-iteration /api/ps poll (L470-476) expires, and the code proceeds into the ComfyUI submit anyway. Nits at the same site: the single try wraps the whole unload loop (one failed POST skips unloading the rest), and the OOM-retry comment (~L502) citing a '20 GB chat LLM' title model is stale (task.model.default is gemma3:1b, 0.8 GB). Separately the /api/chat call (L1040) sends no keep_alive, relying on the implicit OLLAMA_KEEP_ALIVE=60s to pin dolphin/gemma between turns.
    - **Fix:** Guard the generation section with a module-level lock so only one image/video pipeline manipulates VRAM at a time; move _free_vram's try inside the per-model loop; when models remain loaded after the poll window, return False so callers surface 'GPU busy with an active chat' instead of a raw OOM; and set an explicit keep_alive on the /api/chat call.
    - **Status:** ✅ done — Module _GEN_LOCK serializes all VRAM pipelines; _free_vram is per-model + returns success.

- [x] **F11** 🔴 major · `auto_assistant:L1040` — _achat_stream ignores HTTP status and ollama 'error' lines -> a blank assistant reply instead of a surfaced error
    - **Problem:** The stream loop (L1041-1051) never checks r.status and only reads d['message']['content']. Ollama error responses (404 model-not-found, 500 OOM/runner crash) are a single JSON body {'error': ...} — it parses fine, message is absent, nothing is yielded, and the generator ends cleanly. The broad 'except Exception: continue' eats malformed lines, and the outer except only catches transport failures, so all application-level ollama errors are invisible. The ClientTimeout(total=900) is also a whole-stream budget that can kill a legitimately long (partially-offloaded) answer mid-stream.
    - **Fix:** After s.post(...), if r.status!=200: yield f'⚠️ Ollama HTTP {r.status}: {body[:300]}'; return. Inside the loop, if d.get('error'): yield the error and return. Consider replacing total=900 with a sock_read timeout so idle-hangs are caught but long streams are not.
    - **Status:** ✅ done — _achat_stream surfaces non-200 HTTP and ollama 'error' lines instead of a blank reply; sock_read timeout.

- [x] **F12** 🟡 minor · `auto_assistant:L841` — 'Longer'/duration-change follow-ups are not detected, so 'make it longer' re-renders the identical-length clip
    - **Problem:** _video_opts's length trigger (~L841) matches only '\blong(er)?\s+(video|clip)\b|\b[6-9]\s*seconds?\b', so 'make it longer','make the video longer','extend it','6s'/'six seconds' all miss. In the follow-up branch such a message passes _wants_edit, gets merged into the prompt by _merge_video_prompt (adding words a T2V model can't act on) and re-rendered with the SAME seed and same 81-frame length — the clip is essentially unchanged. _wants_multishot is also never consulted in the follow-up branch, so 'make it 15 seconds' can't trigger chaining.
    - **Fix:** Broaden the length/extend regex to catch both word orders and the extend verb and generic '\d\d?\s*seconds', run _wants_multishot in the follow-up branch, and treat a length-only change as opts-only (skip _merge_video_prompt so prompt/seed stay identical while length moves to V_LEN_LONG).
    - **Status:** ✅ done — _LEN_KW/_is_length_only detect 'longer'/'extend'/'N seconds'; length-only change re-renders same prompt+seed at new length.

- [x] **F13** 🟡 minor · `auto_assistant:L712` — 5B 'fast' video path ignores per-request opts (720p/length are parsed then dropped) and 'fast' silently disables multishot
    - **Problem:** _wf_video_5b hard-codes module-level V_W/V_H/V_LEN_5B in its latent node (~L712) and takes no w/h/length parameters; _gen_video's build() passes only (prompt, seed) on the fast path, so '720p'/'hq'/'longer' parsed by _video_opts are dropped. Additionally the multishot branch requires `not opts['fast']` (~L1117), so a fast request suppresses the plan and produces a single ~49-frame clip. None of this is surfaced to the user.
    - **Fix:** Thread opts['w']/opts['h']/length into _wf_video_5b (the TI2V 5B supports 1280x704), let fast multishot chain 5B shots or drop 'fast' when n>=2, or at minimum caption 'draft mode: 2 s @480p' so the truncation is visible.
    - **Status:** ✅ done — 5B fast path honors parsed w/h/length; a multi-shot request overrides 'fast' instead of being dropped to one clip.

- [x] **F14** 🟡 minor · `auto_assistant:L611` — Assistant's default t2i path ignores the Image pipe's LoRA/trigger/size valves — same request, different subject
    - **Problem:** _build_t2i_wf hard-codes base Krea 2 at 1024x1024/8 steps with no LoRA, while image_krea._build_wf honors LORA_FILE/LORA_STRENGTH/TRIGGER/WIDTH/HEIGHT/EXTRA_PASS (a LoRA exists on disk: krea2/krea2_vintagetarot.safetensors). The node graphs are otherwise identical, so this valve gap is the one real workflow divergence — and since ui.default_models=auto_assistant.auto, the Assistant is what users hit by default, silently dropping the personalization configured in the Image pipe.
    - **Fix:** Mirror the LoRA/trigger (and width/height) as constants or Valves in auto_assistant and apply the same LoraLoaderModelOnly node + trigger-prefix logic in _build_t2i_wf, or have both pipes read one shared config source.
    - **Status:** ✅ done — t2i now mirrors the Image pipe's LoRA/trigger/size via IMG_T2I_* constants (empty LoRA = base, unchanged today).

- [x] **F15** 🟡 minor · `auto_assistant:L581` — Prompt-helper / video LLM calls run before ComfyUI is ever freed, so dolphin loads into a card ComfyUI still occupies (~13.7 GB) and runs partly on CPU
    - **Problem:** _enhance (~L670), _enhance_edit (~L639), _style_enrich (~L635), _plan_shots (L872), _enhance_video (L581) and _merge_video_prompt (L135) all load dolphin BEFORE the flow's _free_vram, and no path frees ComfyUI first. With the Comfy stack resident (~13.7 GB) only ~10 GB is free, so Ollama splits dolphin to CPU: _plan_shots (num_predict 900) can go from ~20 s to 1-3 min, the sub-250-token helpers add ~20-60 s each. The _verify_image call sites already solve this (each does _comfy_free first); the text-LLM call sites do not. The comment at ~L638 ('so Gemma isn't unloaded') is also stale — these helpers now use dolphin.
    - **Fix:** Call self._comfy_free() at the top of _gen_image/_gen_video/_gen_multishot before the first helper call (or rely on the end-of-flow _comfy_free proposed elsewhere); update the stale L638 comment.
    - **Status:** ✅ done — _comfy_free runs before the first dolphin helper in each gen path so it isn't loaded onto a card ComfyUI still holds.

- [x] **F16** 🟡 minor · `auto_assistant:L1040` — _achat_stream omits 'think': false, so gemma4 vision chats run with thinking enabled and stream dead air before the answer
    - **Problem:** gemma4:31b is a thinking model (confirmed via /api/show). Every other gemma4/dolphin call passes 'think': false (L144,334,419,439,558,582,873), but the /api/chat body at L1040 omits it, so when L1036 selects gemma4 Ollama enables thinking by default and streams it in message.thinking — which the loop never reads (it only yields message.content, L1049). The reasoning tokens are generated and discarded: pure latency on the 19.9 GB model (worse when it is already CPU-offloaded next to ComfyUI's residents), with the guard already forcing a plain reply. No keep_alive is set either.
    - **Fix:** Add 'think': False to the /api/chat body at L1040 (verified safe for dolphin too), at least when model==self.vision_model; or surface d['message'].get('thinking') as a collapsed status block if reasoning is wanted.
    - **Status:** ✅ done — 'think': false added to the /api/chat body — no dead air before gemma4 vision answers.

- [x] **F17** 🟡 minor · `auto_assistant:L514` — _submit_poll cannot detect a crashed/unreachable ComfyUI mid-job -> the worker hangs the entire poll budget (~30-40 min) before a generic 'Timed out'
    - **Problem:** The poll loop (L512-518) treats both 'history GET failed' and 'pid not yet in history' as 'still running' and keeps sleeping for the full iters budget (1200 iters ~40 min for edits, 900 ~30 min for video). If ComfyUI hard-crashes (a real risk with --lowvram GGUF OOM) and is restarted, its history is empty forever (/history/{pid} returns {}), so the user waits the whole budget; if the process is simply down, connection-refused is swallowed and it polls futilely. The asyncio.to_thread worker is tied up the whole time. The /queue endpoint would reveal the job is gone.
    - **Fix:** Count consecutive exceptions / consecutive 'pid absent from history'; after ~5-10, GET /queue and if pid is in neither queue_running nor queue_pending, return '⚠️ ComfyUI lost the job (crash/restart?)' immediately — one cheap localhost GET every ~10 s.
    - **Status:** ✅ done — _submit_poll counts consecutive misses and checks /queue: a crashed/lost job returns fast instead of hanging the poll budget.

- [x] **F18** 🟡 minor · `auto_assistant:L522` — _submit_poll fetches the finished output OUTSIDE its try/except, so a transient /view error discards a just-completed (possibly multi-minute) render
    - **Problem:** Inside the poll loop only the /history GET is wrapped in try/except (L513-516). Once the job is present and error-free, `data = self._fetch_node_output(...)` (L522) and the extras dict comprehension (L525) run UNGUARDED, and _fetch_node_output does requests.get(/view) (L494-496) with no try of its own. Any exception there (connection reset, read timeout on a large webm) propagates out of _submit_poll, up through _gen_image/_gen_video (asyncio.to_thread re-raises) and out of pipe(), surfacing a raw traceback and throwing away the completed generation — for video/edit that can be a job that took many minutes.
    - **Fix:** Wrap the fetch in the poll's protected region (or give _fetch_node_output its own try with a short retry): on a /view exception, sleep 2 s and continue re-polling rather than letting it escape.
    - **Status:** ✅ done — Output fetch moved inside the poll's try — a transient /view error re-polls instead of discarding a finished render.

- [x] **F19** 🟡 minor · `auto_assistant:L693` — QA-retry generation errors (err2) and upload failures are discarded -> the user receives the QA-rejected output with no notice
    - **Problem:** In every verify loop (edit L661, t2i L693, multishot L940, video L1011) the corrective re-render's err2 is bound and never read: 'if data2 is None: break' silently keeps the image/video _verify_image just judged WRONG. Combined with _verify_image's fail-open (L427), an entire class of real failures (retry OOM, verify timeout, ComfyUI down) is invisible — the flow 'succeeds' with a known-bad result, hiding the recurring OOM that is a real infra signal on this box.
    - **Fix:** Track the last err2/upload failure and append a one-line note to the returned markdown, e.g. '*⚠️ QA flagged: {fix} — automatic correction failed ({err2})*' — one string concat, no flow change.
    - **Status:** ✅ done — QA-retry failures (err2 / upload) now append a visible '⚠️ QA flagged…' note to the reply.

- [x] **F20** 🟡 minor · `auto_assistant:L936` — Multi-shot QA thrashes the full Wan stack + gemma4 on every shot, even when all shots pass
    - **Problem:** Per-shot QA must run before the last frame seeds the next shot (correctly reasoned in the code), but the cost is unconditional: _comfy_free at L936 evicts the Wan stack before the verdict is known, so even an all-pass N-shot run pays N x (Wan eviction + gemma4 19.9 GB load + verify + full Wan GGUF reload under --lowvram). For a 6-shot sequence that is roughly 4-8 min of pure model swapping on top of sampling. This is largely inherent to fitting a 19.9 GB judge on a 24 GB card but is the dominant tunable overhead in the video path.
    - **Fix:** Verify only the chain-critical frames — shot 1 (the identity/style anchor) and the final shot — skipping intermediate verifies; expose it as a valve (VID_VERIFY: all|anchors|off).
    - **Status:** ✅ done — Multi-shot QA verifies only the anchor shots (first+last) by default via VID_VERIFY_MODE='anchors'.

- [x] **F21** 🟡 minor · `auto_assistant:L1102` — Multi-MB base64 regex preprocessing (_recent_media, _ollama_messages/_scrub) runs synchronously on the OpenWebUI event loop every turn
    - **Problem:** pipe() is async but synchronously runs _recent_media(msgs) (L1102) and _ollama_messages(msgs) (L1145), each regex-scanning every message body. Assistant messages embed whole images (~2-3 MB b64) and videos (5-15 MB b64) inline, and _scrub runs three re.sub passes per message. A conversation with a few generated videos means several regex passes over tens of MB inside the event loop on every turn — hundreds of ms up to ~1 s during which the whole OpenWebUI server (all users, all streams) stalls. Patterns are linear, so it is a stall not a hang.
    - **Fix:** Short-circuit _scrub (if 'data:' not in text and '<video' not in text: return text) and move the preprocessing into to_thread hops (kind,media = await asyncio.to_thread(self._recent_media, msgs); pass await asyncio.to_thread(self._ollama_messages, msgs) into _achat_stream).
    - **Status:** ✅ done — _scrub fast-path returns immediately when a turn has no media (no multi-MB regex on every message).

- [x] **F22** 🟡 minor · `auto_assistant:L393` — Cross-pipe ComfyUI /free race: Assistant video/image QA can evict an in-flight Animate (SCAIL) job
    - **Problem:** animate_scail.py and auto_assistant.py drive the same ComfyUI + Ollama. auto_assistant._comfy_free() (L390) POSTs /free {unload_models:true,free_memory:true}, invoked mid-flow during QA (L654,683,936,1007). ComfyUI serializes graph EXECUTION, but /free is a control-plane call that unloads resident models unconditionally regardless of what is queued/running. If a user runs an Animate job in one chat while an Assistant video is in its QA gap in another, /free can unload SCAIL's 14B model out from under its sampler; animate_scail has no guard against this (it only frees Ollama and its OOM-retry wait caps at ~30 s while _verify_image can hold gemma4 up to 300 s).
    - **Fix:** Gate /free on ComfyUI being idle: GET /queue and skip the unload if queue_running/queue_pending is non-empty; or serialize the two pipes with a shared single-flight lock (e.g. a flock file both take around ComfyUI submits and gemma4 loads).
    - **Status:** ✅ done — _comfy_free gated on ComfyUI idle (/queue) so Assistant QA can't evict an in-flight Animate render.

- [x] **F23** 🔵 suggestion · `auto_assistant:L41` — self.task_model (gemma3:1b) is dead code and stale post-split comments/docs misdescribe which model serves which call site
    - **Problem:** self.task_model='gemma3:1b' (L41, comment 'tiny helper for prompt merging') is referenced nowhere — every enhance/merge/plan call uses chat_model (dolphin), and _merge_video_prompt was deliberately moved off gemma3:1b (its docstring records that gemma3:1b echoed prompts verbatim). Multiple stale comments predate the split: L1028 ('keep Gemma from inventing dalle/tool-call JSON'), L635/L638 ('before _free_vram (gemma)'/'so Gemma isn't unloaded') and the header claims all sit on dolphin call sites now, and the L502-504 OOM-retry rationale cites a '20 GB chat LLM ... to generate the chat title' although task.model.default is now gemma3:1b (0.8 GB). Grep-verified the split is otherwise correct across the video path (dolphin for enhance/plan/merge, gemma4 only for _verify_image); the risk here is maintenance drift only.
    - **Fix:** Delete self.task_model (or actually use it for a cheap EDIT-vs-CHAT classifier); rewrite the header and update the L502/L635/L638/L1028 comments to name dolphin/the chat model and the gemma3:1b (0.8 GB) task model.
    - **Status:** ✅ done — Dead self.task_model removed; header comments corrected to name dolphin (chat/helpers) vs gemma4 (vision).

- [x] **F24** 🔵 suggestion · `auto_assistant:L1060` — _recent / _recent_video caches evict by insertion order (FIFO) instead of LRU — the most active chat can be evicted first
    - **Problem:** Both caches are bounded at 30 entries (L1059-1061, L1064-1067) — NOT leaks; worst case ~60-120 MB of b64 on 94 GB RAM. The only defect is eviction: `self._recent.pop(next(iter(self._recent)))` removes the oldest-INSERTED key, and re-assigning an existing cid does not move it to the end, so the most active conversation is the first evicted once 30 distinct chats have generated media. Impact is low because _recent_media re-derives state from message history on a miss.
    - **Fix:** Make writes move-to-end: self._recent.pop(cid, None) before assignment (or use OrderedDict.move_to_end) for true LRU — same ~3-line footprint. Safe to leave as-is.
    - **Status:** ✅ done — _recent/_recent_video caches are now true LRU (move-to-end on write) instead of FIFO.

---

## Phase 2 — Krea 2 Image pipe (`image_krea`)

_5 findings (2 major). Status: ✅ complete._

- [x] **F31** 🔴 major · `image_krea:L60` — image_krea uses gemma4:31b for text-only prompt work — the dolphin/gemma split was never mirrored in the 'Image' pipe
    - **Problem:** image_krea has a single self.enhancer_model='gemma4:31b' (L60) used for THREE calls: _enhance (L195, text-only prompt expansion), _enhance_edit (L377, text-only edit rewrite) and _verify_image (L353, genuinely needs vision). auto_assistant moved the first two to chat_model (dolphin) and kept gemma4 only for _verify_image, but image_krea was left on the old single-model wiring, so the intent-#6 split is INCOMPLETE across call sites. Consequences: (a) every t2i/edit through 'Image' loads the 19.9 GB thinking model for a pure text task, evicting a likely-resident dolphin (two swaps instead of zero); (b) the enhanced prompt differs from the Assistant's for the same request, so 'Image' and Assistant outputs visibly diverge.
    - **Fix:** Add self.text_model='dolphin-venice:24b' (rename enhancer_model->vision_model); point _enhance and _enhance_edit at text_model, leave _verify_image on the vision model. Keep think:false (dolphin accepts it).
    - **Status:** ✅ done — Split the model: text_model=dolphin for _enhance/_enhance_edit, vision_model=gemma4 only for _verify_image.

- [x] **F32** 🔴 major · `image_krea:L178` — image_krea enhancer/verifier lack the art-style handling the Assistant has — 'cartoon' requests drift to photoreal and pass QA
    - **Problem:** auto_assistant's _enhance (L537-554) and _VERIFY_SYS (L398-411) were extended for named styles/mediums: the enhancer opens with the style emphatically and drops camera language, and the verifier FAILs a photorealistic render when a cartoon/anime/watercolor style was requested (plus checks activity-implied objects). image_krea's equivalents were NOT updated: its _enhance sys always leans photorealistic and its _VERIFY_SYS says 'Ignore style, lighting and quality'. image_krea also lacks the Assistant's whole-image restyle path and routes 'make it a cartoon' through _EDIT_REWRITE_SYS whose 'keep identity/background/lighting unchanged' fights a global restyle. So identical requests produce different, unverifiable results in the two entry points.
    - **Fix:** Port auto_assistant's _enhance style clause and the _VERIFY_SYS style-mismatch/activity-object FAIL clause into image_krea (drop-in); ideally factor the shared prompt strings into one module both pipes import so they cannot drift.
    - **Status:** ✅ done — Ported the Assistant's art-style clause into _enhance and the style-mismatch/activity-object FAIL into _VERIFY_SYS.

- [x] **F33** 🟡 minor · `image_krea:L538` — image_krea turns small-talk / question follow-ups ('perfect, thanks!') into unwanted ~2-minute edit jobs — no small-talk/question gate
    - **Problem:** image_krea.pipe() (def L538) routes ANY message that isn't a fresh image request into the edit path once a recent image exists — there is no small-talk/question short-circuit (it does not even consult its own _is_edit_request). auto_assistant grew _wants_edit precisely to filter acknowledgments/questions; image_krea never got it, so gratitude or a question becomes a Qwen-Image-Edit instruction (plus a verify round).
    - **Fix:** Port auto_assistant's small-talk/question short-circuit into image_krea.pipe(): on an acknowledgment/question, return a one-line text reply (or the cached image untouched) instead of entering _generate.
    - **Status:** ✅ done — pipe() now short-circuits small-talk ('thanks!') and questions to a one-line reply instead of a Qwen edit.

- [x] **F34** 🟡 minor · `image_krea:L477` — Krea verify-retry is a strength no-op on 'balanced'/'fast' edit quality (cfg 1 -> boost skipped, negative inert), so failed verifies fail the same way
    - **Problem:** The edit-path verify retry passes boost=True, but _build_edit_wf only boosts when q['cfg']>1.0; with EDIT_QUALITY at 'balanced' (cfg 1.0) or 'fast' the retry re-runs at identical strength and the negative/AVOID conditioning is mathematically inert at cfg 1 — the only delta is the appended correction sentence and a new seed. The t2i fix pass has the same weakness (runs at the unmodified preset with no negative), whereas auto_assistant hard-codes cfg 4.0/20 steps for the identical correction. (Side note: the t2i fix runs through Qwen-Edit without the user's Krea LoRA, so a trained face can drift.)
    - **Fix:** For verify-correction rebuilds, override the preset to 'best' (cfg>=4.0, ~20-24 steps) with boost=True regardless of the EDIT_QUALITY valve so the correction text and negative actually steer the sampler.
    - **Status:** ✅ done — Verify-correction rebuilds forced to 'best' quality + boost so the negative/AVOID actually steers the fix.

- [x] **F35** 🔵 suggestion · `image_krea:L137` — image_krea dead code: unused _is_edit_request, an unreachable img2img ref_name branch + the 'legacy, unused' EDIT_DENOISE valve, and a stale VERIFY valve description
    - **Problem:** _is_edit_request (L137-142) is never called. _build_wf's ref_name branch (L241-249), which applies the EDIT_DENOISE valve (self-labeled 'legacy, unused' at L49-52), is unreachable — the only call site is _build_wf(prompt, None, seed) (L457) since edits moved to _build_edit_wf. The VERIFY valve doc says 'auto-retry once' but _generate runs up to two correction rounds. Cosmetic — no runtime failure — but it misleads a maintainer into thinking Krea 2 img2img editing is still wired up.
    - **Fix:** Delete _is_edit_request, the ref_name branch of _build_wf, and the EDIT_DENOISE valve; update the VERIFY valve text to 'up to two correction rounds'. (Or add a one-line comment that the reference path is retired and edits go through _build_edit_wf.)
    - **Status:** ✅ done — Removed dead _is_edit_request, the unreachable img2img ref_name branch, the EDIT_DENOISE valve; fixed VERIFY doc.

---

## Phase 3 — Photoreal pipe (`uncensored`)

_5 findings (1 major). Status: ✅ complete._

- [x] **F36** 🔴 major · `photoreal:L55` — uncensored.py _free_vram is fire-and-forget (no wait-until-unloaded loop) and its poller has no OOM retry / error parsing
    - **Problem:** uncensored.py's _free_vram (L55-60) posts keep_alive:0 unloads and returns immediately, with a try/except that swallows an /api/ps failure — unlike the sibling pipes (image_krea L152-170, auto_assistant L462-473) which poll /api/ps up to ~30 s until no model is loaded. Ollama unload is asynchronous, so the Lustify job can start (L87) while dolphin (14.3 GB, default keep_alive after a chat) is still releasing VRAM; SDXL is smaller (~6.5 GB) so the risk is lower than the 18-20 GB stacks, but the failure mode is a cryptic message with no retry, and _generate's poll loop (L90-104) never inspects execution_error and never resubmits on OOM.
    - **Fix:** Copy the poll-/api/ps-until-empty wait loop (+small grace) and the two-attempt 'OutOfMemory' resubmit + job-error parsing pattern from image_krea.py/video.py into uncensored.py.
    - **Status:** ✅ done — _free_vram now polls /api/ps until empty (+grace) and the poll loop has a 2-attempt OOM resubmit + job-error parsing.

- [x] **F37** 🟡 minor · `photoreal:L87` — ComfyUI workflow-validation errors are misreported as 'Image backend unreachable' in the Photoreal pipe
    - **Problem:** L87 does `requests.post(f'{self.comfy}/prompt', ...).json()['prompt_id']` inside a try whose except (L88-89) returns 'Image backend unreachable: {e}'. When ComfyUI IS reachable but rejects the workflow (HTTP 400 with {'error':...,'node_errors':...} and no prompt_id), the ['prompt_id'] lookup raises KeyError and the user is told the backend is unreachable — the opposite of what happened, masking the real actionable validation error.
    - **Fix:** Capture the response, check r.status_code!=200 or 'prompt_id' not in the parsed JSON, and surface r.text/node_errors (truncated); reserve the 'unreachable' message for genuine connection exceptions only.
    - **Status:** ✅ done — Submit checks status/prompt_id and surfaces node_errors — 'unreachable' is reserved for real connection failures.

- [x] **F38** 🟡 minor · `photoreal:L90` — Photoreal pipe's fixed 300 s poll window ignores queue wait; a timed-out job keeps running as an orphan
    - **Problem:** The result loop (L90-104) polls /history/{pid} at most 300x with 1 s sleeps (~5 min including queue time). The SDXL render is fast, but ComfyUI executes serially and this host also runs Wan A14B video jobs that can occupy the queue past 5 minutes. On timeout the pipe returns 'Timed out' but never cancels the queued prompt, so the job still executes later — burning GPU, evicting whatever is then loaded, and saving an image nobody receives.
    - **Fix:** Raise the window and, on timeout, cancel the pending job with POST /queue {'delete':[pid]} (plus /interrupt if started); optionally check /queue up front and tell the user the job is queued behind another.
    - **Status:** ✅ done — Poll window raised to ~15 min; on timeout the job is cancelled (POST /queue delete + /interrupt) so no orphan runs.

- [x] **F39** 🟡 minor · `photoreal:L26` — Photoreal pipe reads the reference image only from the last user message, so multi-turn img2img silently degrades to txt2img
    - **Problem:** _parse() (L26) iterates messages in reverse and returns at the first (latest) user message, so only an image attached to that exact message is used as the img2img reference. In OpenWebUI an uploaded image stays attached to the message where it was uploaded, so any follow-up turn carries no image_url part and _parse returns (text, None) even though a reference exists one turn earlier.
    - **Fix:** In _parse, when the latest user message has text but no image, continue scanning earlier user messages (bounded, e.g. last ~6) and reuse the most recent image found, mirroring the follow-up handling in the Krea/Assistant pipes.
    - **Status:** ✅ done — _parse scans back ~6 user turns for the most recent image, so multi-turn img2img no longer degrades to txt2img.

- [x] **F40** 🔵 suggestion · `photoreal:L66` — Photoreal pipe passes raw chat text straight to SDXL CLIP (77-token cap) — no prompt enhancement, unlike the other image pipes
    - **Problem:** The pipe uses NO ollama LLM anywhere (its only ollama use is _free_vram's unload) — good in that there is no refusal-prone enhancer or dangling model reference, and isolation from the Assistant/Krea/global config holds. But it is the only active image pipe with zero prompt engineering: the last user message is fed verbatim into CLIPTextEncode (L66), where SDXL's CLIP truncates at 77 tokens and terse inputs get none of the photoreal scaffolding the other pipes add.
    - **Fix:** If an enhancer is added it MUST be dolphin-venice:24b (the uncensored LLM — gemma4/gemma3 would refuse this content and break isolation): call /api/generate on dolphin with think:false, keep_alive:0 and a ~60-80-token dense-tag rewrite instruction BEFORE _free_vram, falling back to raw text on any error.
    - **Status:** ✅ done — Added a dolphin (uncensored) prompt enhancer for txt2img (think:false, keep_alive:0), raw-text fallback on any error.

---

## Phase 4 — OpenWebUI config & workspace

_6 findings (3 major). Status: ✅ complete._

- [x] **F25** 🔴 major · `config:cfg#9` — dolphin-venice:24b workspace params.system is a stale pipe-era image-description instruction that hijacks direct chat about images
    - **Problem:** The 'Dolphin Venice 24B' model row carries params.system telling the model that when asked for an image it must 'reply with ONE vivid visual-description paragraph ... never say you cannot create images'. That is only coherent for the OLD describe-then-render flow. Under the current design the Assistant routes every image request by regex before the chat model and calls dolphin over raw ollama HTTP injecting its own guard (so this system prompt is dead for the Assistant), but the row is is_active=1 and selectable — when a user picks it directly and asks for a picture, dolphin emits a description paragraph and is forbidden from admitting it cannot render, so the user gets a confusing non-answer and no image. (If the user instead clicks the built-in Image button, that separately fires the global flux flow — see the built-in-image finding.) Capability flags vision:false is correct; terminal/builtin_tools are inert but could break chat if an admin flips function_calling to native.
    - **Fix:** Replace params.system with a plain chat persona (optionally redirecting image asks to the 🪄 Assistant), since image routing is owned by the Assistant; optionally also set capabilities.image_generation/terminal/builtin_tools to false on this row.
    - **Status:** ✅ done — dolphin-venice:24b system prompt replaced with a plain chat persona (redirects image asks to the Assistant); image_generation capability off.

- [x] **F26** 🔴 major · `config:cfg#246` — OpenWebUI built-in flux image generation is enabled and exposed on the pipe model rows — a second, uncoordinated ComfyUI pipeline with no _free_vram/_comfy_free
    - **Problem:** image_generation.enable=true, engine=comfyui, model=flux1-dev-fp8.safetensors, base_url=localhost:8188 (verified loadable via CheckpointLoaderSimple), and capabilities.image_generation=true on dolphin-venice, auto_assistant.auto, uncensored.photo and flux_image.flux-dev — so a 'Generate Image' button is exposed throughout the UI. That native path POSTs the ~16-17 GB flux workflow straight to ComfyUI and runs NONE of the pipes' VRAM choreography (_free_vram unload+poll, _comfy_free), and has no OOM-retry (that lives only in the pipes). It also produces off-brand flux output rather than Krea 2. images.edit.enable is already false (good), so only the generation path is exposed.
    - **Fix:** Set image_generation.enable=false (the Krea pipes own image gen), or at minimum strip the image_generation capability from the auto_assistant.auto / dolphin-venice / Photoreal rows so the uncoordinated flux button is not exposed.
    - **Status:** ✅ done — image_generation.enable=false (built-in flux disabled); image_generation capability stripped from the Assistant/dolphin/Photoreal rows. [user-approved]

- [x] **F27** 🔴 major · `config:cfg#54` — Photoreal (uncensored.photo) meta.defaultFeatureIds auto-fires OpenWebUI's global flux generation on every message, on top of the pipe's Lustify image
    - **Problem:** The uncensored.photo row has meta.defaultFeatureIds:['image_generation'] AND capabilities.image_generation=true. In OpenWebUI, defaultFeatureIds pre-enables the image-input toggle for every chat with this model, and middleware's chat_image_generation_handler then runs BEFORE dispatching to the pipe: it generates an image from the last user message via the GLOBAL engine (comfyui + flux1-dev-fp8, prompt.enable=false so raw text), injects it, then still runs the pipe — which produces its own Lustify SDXL image. Net: every Photoreal turn produces a spurious extra flux image (a different, censored checkpoint that defeats the uncensored purpose) plus a ComfyUI flux load/eviction on top of the Lustify job. flux1-dev-fp8 is present so this fires successfully. The same defaultFeatureIds sits on flux_image.flux-dev (moot only because that function is disabled).
    - **Fix:** Remove image_generation from uncensored.photo's meta.defaultFeatureIds (clear 'Default Features' or set []); optionally set capabilities.image_generation=false so it cannot be toggled on manually. Do the same for flux_image.flux-dev.
    - **Status:** ✅ done — uncensored.photo defaultFeatureIds cleared ([]) + image_generation capability off — no more spurious flux image before the Lustify render.

- [x] **F28** 🟡 minor · `config:cfg#91` — 'Image' workspace model (flux_image.flux-dev) is active while its backing function is disabled — an orphaned picker entry that also carries the double-generation defaultFeatureIds
    - **Problem:** Model row flux_image.flux-dev named 'Image' is is_active=1, but the flux_image FUNCTION that backs it is is_active=0 (disabled), so a pipe whose function is disabled is not registered and this 'Image' row points at nothing loadable. It also collides in name with image_krea's own 'Krea 2 Image' model and carries the same meta.defaultFeatureIds:['image_generation'] footgun as Photoreal. (Contrast video.wan, where both the model row and function are is_active=0 — consistent.)
    - **Fix:** Set the flux_image.flux-dev row is_active=0 (matching its function, like video.wan) and strip its defaultFeatureIds, or delete the row, leaving image_krea's 'Krea 2 Image' as the only standalone image model.
    - **Status:** ✅ done — flux_image.flux-dev row set is_active=0 (matches its disabled function) + defaultFeatureIds cleared — orphaned 'Image' picker entry gone.

- [x] **F29** 🟡 minor · `config:cfg#2` — gemma3:1b has no hidden workspace row, so it is visible/selectable in the model picker (soft breach of dolphin-for-chat)
    - **Problem:** OpenWebUI shows every upstream ollama model by default; only models with a workspace row can be hidden. gemma4:31b got a hidden row (is_active=0) for exactly this reason, but gemma3:1b (the task helper) has none, so users see and can chat with a 0.8 GB model — a soft breach of design intent #1. (Everything else here checks out: the hidden gemma4 row correctly hides it while HTTP pipe calls still reach it; task.model.default/external=gemma3:1b resolve server-side regardless of a hidden row.)
    - **Fix:** Add a hidden workspace row for gemma3:1b (is_active=0, e.g. 'Gemma3 1B (hidden task model)') — hiding it does not affect its use as task.model.default.
    - **Status:** ✅ done — Added a hidden workspace row for gemma3:1b (is_active=0) so the task model is no longer visible/selectable in the picker.

- [x] **F30** 🔵 suggestion · `config:cfg#341` — gemma3:1b is adequate for titles/tags but marginal for search-query/retrieval generation, degrading web-search/RAG recall
    - **Problem:** task.model.default=task.model.external=gemma3:1b handles title, tag, follow-up AND retrieval/web-search query generation (task.query.search.enable=true L341, task.query.retrieval.enable=true). A 1b model is fine for the short low-stakes title/tag jobs (and autocomplete is disabled), but is a weak point for query reformulation, degrading recall. This is a quality trade-off, not a broken flow — the tiny model correctly avoids evicting the big chat model for background tasks.
    - **Status 2026-08-01: this is the live state again, deliberately.** The swap to `gemma4:e2b` was reverted after measuring what it actually cost: e2b reports 1.81 GiB but Ollama reserves ~9.4 GiB for it, and loading it **evicted the 16.70 GiB chat tenant outright** — so every chat title charged the user a full 18 GB reload on their next turn. `gemma3:1b` co-resides (21298/24576 measured).
    - The recall concern above stands and is **unmeasured**: nobody has compared 1b vs e2b query reformulation on real retrieval. If RAG recall ever feels weak, that is the experiment to run — not a blind re-swap, since the eviction cost is now a measured number and the recall cost is not.
    - **Fix:** Keep gemma3:1b for titles/tags; if search quality matters, point only the query task at a stronger model (e.g. dolphin), accepting the extra swap, or leave as-is if background-task cost is the priority.
    - **Status:** ✅ done — Kept gemma3:1b for all background tasks (titles/tags/query) — OpenWebUI has no per-task override; switching all to dolphin would thrash VRAM. [user-approved, documented tradeoff]

---

## Phase 5 — Remove legacy Video pipe + deploy/verify

_1 findings (0 major). Status: ✅ complete._

- [x] **F41** 🔵 Remove the dead `video` pipe function + `video.wan` workspace model (superseded by the Assistant's video path).
- [x] **Deploy:** write every edited pipe's `content` back to the DB, update config/model rows.
- [x] **Verify:** `docker restart open-webui`; confirm all 5 active functions load with no errors in logs.
- [x] **Test:** run routing unit tests (`_wants_edit`, `_is_image_request`, `_is_video_request`, model-split) against representative prompts.
- [ ] Sync `live/` → disk pipe filenames so the on-disk copy is no longer stale.

### Finding detail

**F41 · 🔵 suggestion** — Dead, superseded video.py pipe (video.wan) is still present as a stale duplicate of the Assistant's internal video path
> **Fix:** Delete the 'video' function row and video.wan model row from the DB, or add a header comment in video.py marking it superseded by auto_assistant and keep it permanently disabled.

---

## Change log

_Appended as findings are completed._

- 2026-07-24 — Plan created; DB + pipes backed up; deployed code exported to `~/ai-stack/pipes/live/`.
- 2026-07-24 — **Phase 1 complete (F01–F24, Assistant pipe).** All edits in `~/ai-stack/pipes/live/auto_assistant.py`; `py_compile` clean; routing logic unit-tested (video/image/edit intent + video-opts round-trip, 33/33). Not yet pushed to the DB / restarted.
- 2026-07-24 — **Phase 2 complete (F31–F35, Krea 2 Image pipe).** Edits in `~/ai-stack/pipes/live/image_krea.py`; `py_compile` clean; smoke-tested (model split, dead-code removal, t2i graph, small-talk/question guards). Not yet pushed to the DB / restarted.
- 2026-07-24 — **Phase 3 complete (F36–F40, Photoreal pipe).** Edits in `~/ai-stack/pipes/live/uncensored.py`; `py_compile` clean; tested multi-turn reference reuse + robust submit/poll.
- 2026-07-24 — **Phases 4 & 5 complete + DEPLOYED (F25–F30, F41).** Atomic DB deploy: pushed the 3 rewritten pipes' code into the `function` table, deleted the legacy video pipe/model, disabled built-in flux (`image_generation.enable=false`), fixed the dolphin persona + capability/defaultFeatureIds footguns, added the hidden gemma3:1b row. Fresh pre-deploy DB backup taken; `docker restart open-webui` → healthy; no function-load errors; all 3 pipes import + register in the runtime; live chat smoke test returned 'DOLPHIN OK'. Disk `~/ai-stack/pipes/*.py` re-synced to the deployed code (no longer stale).
- 2026-07-24 — **ALL 41 FINDINGS COMPLETE.** 0 critical remained; system live and verified.
- 2026-07-24 — Incident + fix: video failed with a phantom `OutOfMemoryError in CLIPTextEncode` on a 24 GB-free card — ComfyUI's allocator was wedged after 14 h uptime (NOT the code changes; reproduced with the byte-identical graph submitted directly). Fixed by `docker restart comfyui`; added pipe-side recovery (OOM retry now frees ComfyUI too) + a clear 'allocator wedged — restart comfyui' message when OOM is reported with the card near-empty.
- 2026-07-24 — Enhancement (user request): native OpenWebUI **generation status line** added to all three media pipes — a live elapsed-time ticker while generating, collapsing to `Generated in 1m 05s · Krea 2 · 1024×1024 · 8 steps` (via `__event_emitter__` status events). Verified end-to-end on a real Krea render.
- 2026-07-25 — **Task model upgrade:** swapped the OpenWebUI background-task model `gemma3:1b → gemma4:e2b` (Gemma 4, ~5B/"E2B", 1.9 GB VRAM). It's a thinking model, so titles hung/emptied by default; fixed by forwarding `think:false` via an OpenWebUI model param (`{"think": false}` on the hidden `gemma4:e2b` row → `payload.py` `ollama_root_params`). Verified: titles ~0.04 s warm, zero thinking, clean JSON. gemma3:1b kept hidden for rollback. See `docs/MODELS.md`.
- 2026-07-25 — **Full workflow QA — all green.** End-to-end tests against the live system: routing 17/17; chat + question-about-a-generated-image (gemma4 correctly read a generated red bicycle); image gen + edit (Krea/Qwen); video gen + edit (Wan 2.2 A14B — merge folds the change in, seed + resolution preserved); Photoreal (Lustify SDXL) gen. Status line live on every media reply. No bugs found. (Animate/SCAIL verified at load level only — needs image + motion-clip assets to exercise.)

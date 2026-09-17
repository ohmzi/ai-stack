# The `deepseek` harness — one Claude Code session, several backends

## 1. What this is

Claude Code reads `ANTHROPIC_BASE_URL` **once, at launch**. There is no documented way to change
it inside a session, so a single session can only ever talk to one backend: pick the DeepSeek cloud
API and you pay for every turn, pick the local Qwen and you get a smaller model for every turn.
Restarting to switch is the only alternative, and it throws away the conversation.

This harness removes that constraint. It runs a small relay on `127.0.0.1:8788` and points Claude
Code at *it* instead of at a backend. The relay chooses an upstream from the **model name in the
request body**, so Claude Code's own `/model` command becomes the backend switch — live, mid-turn,
no restart.

The reason this is cheap is that both upstreams already speak the **Anthropic Messages API
natively**: the DeepSeek cloud API at `/anthropic`, and Ollama 0.34.1 at `/v1/messages`. Nothing
needs translating. The relay is a pass-through: it reads the model name, picks a provider, and
copies the bytes.

```
harness/deepseek/
  router.py                        the relay — Python stdlib only, no dependencies
  router.json                      providers, glob routing rules, context sizing
  deepseek                         the launcher wrapper (bash)
  install.sh                       symlinks these into ~/.config/deepseek + ~/.local/bin
  models/qwen38-coder-128k.Modelfile   the 128K-context build of the coder model
```

## 2. Architecture

```
        ┌───────────────────────────────┐
        │  Claude Code (claude)         │
        │  ANTHROPIC_BASE_URL=          │   /model opus|sonnet|haiku rewrites the
        │    http://127.0.0.1:8788      │   model field of the next request
        └───────────────┬───────────────┘
                        │  Anthropic Messages API (JSON, SSE for streams)
                        ▼
        ┌───────────────────────────────┐
        │  deepseek-router  :8788       │   reads payload["model"],
        │  (ThreadingHTTPServer)        │   first glob match in router.json wins,
        │                               │   then a byte-for-byte relay
        └───────┬────────────────┬──────┘
                │                │
   auth: passthrough│            │auth: dummy  (authorization: Bearer ollama)
   (client's key,   │            │
    forwarded)      ▼            ▼
        ┌──────────────────┐  ┌──────────────────────────┐
        │ DeepSeek cloud   │  │ Ollama 127.0.0.1:11434   │
        │ api.deepseek.com │  │ qwen38-coder:q4-128k     │
        │ /anthropic       │  │ gemma3:1b  …             │
        └──────────────────┘  └──────────────────────────┘
             paid, big window        free, 24 GB RTX 3090
```

The relay is deliberately thin. It parses the body to read `model` (which decides the upstream) and
`max_tokens` (for the log line) and to apply the per-provider shims in §5, then re-serialises and
forwards it. Headers are copied verbatim except for the
hop-by-hop set (`connection`, `transfer-encoding`, `host`, `content-length`, …) — and
`accept-encoding`, which is dropped so the body stays an uncompressed byte copy. Responses are
relayed both ways: a known `content-length` is streamed in 64 KiB chunks, and a response without
one (every SSE stream) is re-chunked as it arrives using `read1()` — `read()` would block until a
never-ending stream completed.

**Auth is the interesting part, and it is the reason no secret is in this repo.** A provider's
`auth` field has two values:

- `"passthrough"` (the cloud provider) — the client's own `authorization` / `x-api-key` headers are
  forwarded upstream untouched. The router never holds a key, so there is nothing to leak and
  nothing to keep in sync. The key lives only in the user's shell environment.
- `"dummy"` (the local provider) — credentials are replaced with `authorization: Bearer ollama` and
  `x-api-key` is dropped, which is what a local Ollama server expects.

Two behaviours are worth knowing because they are what make a *relay* look like a *backend* to
Claude Code:

- `HEAD /api/hello` is answered `200` locally (Claude Code warms the connection with it).
- `GET /v1/models` is synthesised from the config, not proxied. Note what it lists: only the
  glob-free model names, so the local provider's six exact tags appear and **none of the cloud
  names do** — every cloud pattern (`deepseek-flash*`, `deepseek*`) contains a wildcard and is
  filtered out. The listing is therefore not the routing table; `deepseek --routes` is.

Client disconnects are routine rather than errors: `handle()` swallows
`ConnectionResetError`/`BrokenPipeError`, and a stream that dies mid-flight logs
`client hung up mid-stream` instead of dumping a traceback over the log.

## 3. Usage

The launcher runs `claude` with the environment rewired **for its own process only**. Plain
`claude` still goes straight to the DeepSeek cloud API, unchanged — so the two never fight over
`~/.bashrc`.

```bash
deepseek                     # DeepSeek cloud, default; window capped at the local one
deepseek --local             # local Qwen 3.8 27B on the RTX 3090
deepseek --cloud             # cloud, no cap — full cloud window
deepseek --model NAME        # any routable model name
deepseek --status, -s        # router health, token presence, routing table
deepseek --routes [MODEL]    # which upstream a model resolves to
deepseek --restart           # bounce the router
deepseek --help, -h          # the header of the script itself

deepseek --local -p "summarise this repo"   # anything else goes to claude verbatim
deepseek --continue
```

The router is started **on demand** — there is no systemd unit on this host, so `start_router()`
`nohup`s `python3 router.py` behind `~/.cache/deepseek-router.log` and polls `/v1/models` every
0.25 s for up to 10 s. (If a user unit named `deepseek-router.service` *does* exist it is preferred,
and `--restart` bounces that instead. `--status` reports router health either way, plus which
backends the three aliases point at and whether a cloud token was found.) A failed start does
not fail silently — it points at the log and at `journalctl --user -u deepseek-router.service`.

Host and port are read from `router.json` with `jq`, falling back to `127.0.0.1 8788` if `jq` is
missing — which is why `install.sh` warns when it is absent.

Inside a session, `/model` is the switch. The launcher binds the three aliases through
`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, and each default is overridable by an environment
variable:

| `/model` alias | Model name sent | Upstream | Env override |
|---|---|---|---|
| `opus` | `deepseek-flash[1m]` | DeepSeek cloud | `DEEPSEEK_CLOUD_MODEL` |
| `sonnet` | `qwen38-coder:q4-128k` | local Ollama | `DEEPSEEK_LOCAL_MODEL` |
| `haiku` | `gemma3:1b` | local Ollama | `DEEPSEEK_FAST_MODEL` |

`deepseek --model NAME` sets the *starting* model the same way; `/model` then moves from there.

### Why an unknown model name is safe

Providers are matched **in config order**, first glob wins, and the `local` provider's list ends
with `"*"`. Any name that matches nothing else — a typo, a model Claude Code invents, a new tag
nobody wired up — lands on the local backend. It cannot reach the paid API by accident. The cloud
provider's own patterns are the four `deepseek*` forms above it, so the only way to bill DeepSeek is
to name a `deepseek…` model.

A request with **no** `model` field at all takes `default_provider`, which is `local`.

## 4. The context-window arithmetic: 131072 real becomes 98304 advertised

This is the part that took measuring, and the part a future reader is most likely to get wrong.

The backend's context window covers **prompt *and* completion together**. Claude Code asks for
`max_tokens=32000` on every request. So handing Claude Code the raw window means a full prompt
leaves nothing to generate into, and the request comes back `400`. The relay therefore advertises
`context_window - output_reserve`:

```
   real window (num_ctx)        131072     what the model was built with
   output_reserve               -32768     ≥ the 32000 max_tokens Claude Code asks for
   ────────────────────────────────────
   advertised to Claude Code     98304     CLAUDE_CODE_MAX_CONTEXT_TOKENS
```

The reserve is deliberately 32768 rather than 32000: 768 tokens of slack costs nothing and covers
a request that asks for slightly more than the usual. Both numbers live in `router.json`
(`context_window`, `output_reserve`) and are surfaced by
`python3 router.py --window qwen38-coder:q4-128k` → `98304`.

### Why the stock 32K tag is unusable here

Claude Code's **baseline** request — system prompt plus every tool schema, before the conversation
starts — measures roughly **25K tokens**. Two measurements pin that down:

- advertising a window of `24576` made Claude Code refuse outright with **"Prompt is too long"**;
- a real turn against the stock `32768` window failed at **33142 tokens**, which Ollama answered
  with `400 request (33142 tokens) exceeds the available context size (32768 tokens)`.

A 32K window is spent before the conversation begins, so `qwen38-coder:q4` (the stock tag,
`PARAMETER num_ctx 32768`) is **not a usable Claude Code backend**. It still serves OpenCode, which
sends a lean prompt — see `docs/MODELS.md` for its VRAM row. `models/qwen38-coder-128k.Modelfile`
exists solely to produce a tag whose window Claude Code can actually work in.

### Why 128K fits on a 24 GB card, and 256K would not

Measured on the RTX 3090 (24 GB), 2026-09-17:

```
   ollama ps   ->  21 GB, 100% GPU, CONTEXT 131072
   nvidia-smi  ->  21857 MiB resident
```

The whole 128K window sits in VRAM with **no CPU offload**, so the larger window costs accuracy
nothing. The KV cache is why it fits: with `OLLAMA_KV_CACHE_TYPE=q8_0` it works out to roughly
**40 KB per token**, not the ~128 KB an fp16 estimate predicts. That is a 3× headroom, and it is
what makes 131072 the comfortable ceiling on a 24 GB card rather than an aspiration. 256K would
not fit and would start spilling.

Only `num_ctx` is overridden in the Modelfile. `FROM qwen38-coder:q4` inherits that tag's template
and sampling parameters (temperature 0.7, top_k 20, top_p 0.8, presence_penalty 1,
repeat_penalty 1), so the two tags answer with identical style and differ **only** in how much they
can hold.

### Two things that are per-provider, not per-model

Both are real and both can bite:

- **The advertised window is computed from the provider, not the model.** `usable_window()` returns
  `context_window - output_reserve` for whichever provider matched. Since `context_window: 131072`
  is a property of the `local` provider, **every** model routed to `local` advertises 98304 —
  including `qwen38-coder:q4`, whose real window is 32768. This is not a problem in the launcher's
  default configuration (both aliases point at the 128K tag), but it means the router does not
  police window size. If you route Claude Code at a smaller tag, the 400 comes from Ollama, not
  from the router. Check with `python3 router.py --route MODEL`.
- **Token counting is estimated, not measured.** Ollama has no `count_tokens` endpoint (it answers
  with a plain-text 404 that Claude Code cannot parse), so when a provider sets
  `supports_count_tokens: false` the relay synthesises `{"input_tokens": ceil(chars/4)}` over the
  whole `messages` + `system` + `tools` payload, counting keys as well as values. Prose on this
  model measures ~4.5 chars/token and JSON tool schemas are denser, so dividing by 4 lands
  **slightly high on purpose**: an undercount lets the prompt grow past the real window and Ollama
  answers with a hard 400, whereas an overcount just triggers auto-compact a little early. Erring
  high is the cheap direction. (The `estimate_tokens` docstring still claims "~3 chars/token rather
  than the usual ~4"; the code divides by 4 — the docstring is stale, the comment beside the
  division is the accurate one.)

## 5. The local-backend shims

Every entry here exists because something failed and the failure is not obvious from the error you
get. This is the section to read when a local model returns a `500` or a `400`.

### 5.1 Hoisting mid-conversation `role: "system"` messages

**Failure it prevents:** `System message must be at the beginning`, surfaced to Claude Code as an
opaque **HTTP 500**.

Claude Code injects `role: "system"` messages part-way through a conversation, routinely. Qwen's
chat template — like most llama.cpp templates — refuses them, raising
`Jinja Exception: System message must be at the beginning` inside Ollama, which has no useful way
to report that so it returns a 500 with no explanation.

The Anthropic API has a proper home for that text: the top-level `system` field. With
`normalize_system: true` on a provider, `normalize_messages()` walks the message list, pulls out
every `system` turn after index 0, and appends its text to `payload["system"]` — handling a string
`system`, a block-list `system`, and no `system` at all. Block content is flattened to text via
`text_of()`. The rewrite logs `hoisted mid-conversation system message(s) for local` each time it
fires, so the log tells you whether this is happening in your traffic (on this stack: essentially
every request).

Note this is **opt-in per provider**. The cloud API accepts mid-conversation system turns as-is, so
the `deepseek` provider leaves them alone — fewer moving parts on the leg that does not need help.

### 5.2 Forcing `thinking: {"type": "disabled"}`

**Failure it prevents:** a `400` on local models, and reasoning tokens silently eating the caller's
`max_tokens`.

Claude Code sends `thinking: {"type": "adaptive"}` to model names it does not recognise as current
Claude models — which is every name in this table, since they are all local tags. A local
llama.cpp/Ollama backend answers adaptive thinking with a `400`.

A provider's `inject` block **forces** fields, deliberately overriding whatever the client sent.
For `local` that is `{"thinking": {"type": "disabled"}}`. Disabling it sidesteps the 400 *and*
stops reasoning tokens consuming the `max_tokens` the caller asked for.

### 5.3 Synthesising `/v1/messages/count_tokens`

**Failure it prevents:** Claude Code cannot account for context at all against a backend that
404s the endpoint.

Ollama returns a plain-text 404 for `count_tokens`, in a shape Claude Code cannot read. When a
provider has `supports_count_tokens: false`, the relay intercepts the path and answers
`{"input_tokens": ...}` itself. The estimate is `ceil(chars/4)` over the payload — deliberately an
overestimate, for the reason in §4: it drives auto-compact slightly early rather than letting the
prompt overflow into a hard 400.

### 5.4 Answering an unreachable upstream in Anthropic's error shape

**Failure it prevents:** a confusing parse failure instead of a legible error.

If the upstream connection throws — Ollama stopped, the cloud unreachable — the relay returns
**502** with a body Claude Code already knows how to render:

```json
{"type": "error",
 "error": {"type": "api_error",
           "message": "deepseek-router: local unreachable: …"}}
```

The provider's name is in the message, so "the router is fine but Ollama is down" and "the cloud is
unreachable" are distinguishable from the client.

### 5.5 The idle-timeout override (launcher-side)

**Failure it prevents:** a local turn dying while the model loads.

A 27B model can legitimately pause for minutes between chunks while it loads into VRAM. The
launcher sets `API_FORCE_IDLE_TIMEOUT=0` for non-`--cloud` sessions, and the relay's own upstream
timeout is a generous 900 s (`DEEPSEEK_ROUTER_TIMEOUT` overrides it).

## 6. Mounting it on a new machine

**Prerequisites:** `python3` (the relay is stdlib-only — nothing to `pip install`), `jq` (the
launcher reads `router.json` with it), `curl`, `claude` on `PATH`, and Ollama. Ollama 0.34.1 is
what this was built and measured against.

```bash
git clone <this repo> ai-stack
cd ai-stack/harness/deepseek
./install.sh
```

`install.sh` symlinks — it does **not** copy, so editing the checkout changes what runs and
`git status` shows drift instead of hiding it:

```
~/.config/deepseek/router.py    -> harness/deepseek/router.py
~/.config/deepseek/router.json  -> harness/deepseek/router.json
~/.local/bin/deepseek           -> harness/deepseek/deepseek
```

`~/.config/deepseek` is created mode `700`. `--uninstall` removes the three symlinks and leaves
`~/.config/deepseek` in place (so `secrets.env`, if you keep one there, survives). The router then
starts on demand on the first `deepseek` run; verify with `deepseek --status`.

### The one manual prerequisite: the API key

`install.sh` installs **no credential, on purpose.** Because the router forwards the client's own
credential to the cloud upstream (`auth: "passthrough"`), the key never has to be duplicated into
this repo or into `router.json`. It belongs in the shell environment — this stack keeps it in
`~/.bashrc` alongside the ambient `ANTHROPIC_BASE_URL` that plain `claude` uses.

The launcher resolves the token in this order: `DEEPSEEK_API_TOKEN`, then `ANTHROPIC_AUTH_TOKEN`,
after sourcing `~/.config/deepseek/secrets.env` if that file exists. The `secrets.env` path is an
alternative for people who prefer a file; note that it is **sourced after the environment is
already exported**, so a variable set there wins over an ambient one of the same name — the
comment above that code reads the precedence the other way round ("real env > secrets.env"). It
matters only if both places set the *same* variable; on this host `secrets.env` does not exist and
the token arrives from the shell. With no token at all the launcher still starts, exporting the
placeholder `router-local` — the local leg ignores credentials entirely, so a local-only session
works without one, and only the cloud leg fails.

### Building the Ollama tag

The launcher's `sonnet` alias has nothing to talk to until the 128K tag exists. The tag builds
`FROM qwen38-coder:q4`, so that base tag must be present first:

```bash
ollama pull qwen38-coder:q4                      # base tag (num_ctx 32768); import it if it is
                                                 # not the registry copy this stack uses
cd ai-stack/harness/deepseek
ollama create qwen38-coder:q4-128k -f models/qwen38-coder-128k.Modelfile
```

Two things the Modelfile does **not** do, both of which the VRAM figures in §4 depend on:

- **The q8_0 KV cache is a server-side setting, not a Modelfile parameter.** The Modelfile contains
  only `FROM` and `PARAMETER num_ctx 131072`. On this host the Ollama service runs with
  `OLLAMA_KV_CACHE_TYPE=q8_0` (and `OLLAMA_FLASH_ATTENTION=1`); without those, the ~40 KB/token
  figure does not apply and 128K will not fit in 24 GB the way it does here.
- `OLLAMA_CONTEXT_LENGTH` is the global default and does not need changing — the per-model
  `num_ctx` overrides it for this tag.

Confirm the result the same way it was measured:

```bash
ollama ps        # expect 21 GB, 100% GPU, CONTEXT 131072
nvidia-smi       # expect ~21857 MiB resident
```

> **State of this host, 2026-09-17.** The three deployed files under `~/.config/deepseek` and
> `~/.local/bin` are currently **regular files, not the symlinks `install.sh` creates** — distinct
> inodes, link count 1 — while being byte-identical to the checkout at the time of writing. They
> were put there by a copy rather than by `install.sh`. The practical consequence is the one
> `install.sh`'s own header warns about: an edit to `harness/deepseek/router.py` will not reach the
> running router until the files are re-installed or re-copied. Running `./install.sh` fixes it and
> leaves the files identical in content.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `400 … request (NNNNN tokens) exceeds the available context size (32768 tokens)` from Ollama | The model actually in use was built at 32768 (`qwen38-coder:q4`), but a full Claude Code turn — ~25K baseline plus conversation — runs past it. The router does not police this: the advertised window comes from the provider (`local` → 98304), not from the model's real `num_ctx`. | Route the session at `qwen38-coder:q4-128k` instead, or build a tag with a bigger `num_ctx`. `python3 router.py --route MODEL` shows the upstream; `--window MODEL` shows what gets advertised. |
| `Prompt is too long` — Claude Code refuses *before* sending anything | The advertised window is smaller than the ~25K baseline request. Measured: advertising 24576 is enough to trigger it. | Raise `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, but not above the backend's usable window. The launcher already sets it to the local window (98304) by default — a session started some other way may not have it. |
| `500` with no useful body, or `System message must be at the beginning` in Ollama's log | Claude Code injected a `role: "system"` message mid-conversation and Qwen's chat template refused it. | Ensure the matched provider has `normalize_system: true` (it is set for `local`). §5.1. The router logs `hoisted mid-conversation system message(s)` when it fires — if that line is absent, the request never reached the shim. |
| The model answers nothing / a `400` right after `/model` to a local name | Claude Code sent `thinking: {"type": "adaptive"}` to a name it does not recognise. | Covered by the `inject` block on `local` (§5.2). If you add a provider, add it there too. |
| Claude Code shows no context usage, or auto-compact never fires on a local model | The backend 404s `count_tokens` in a shape Claude Code cannot parse. | Set `supports_count_tokens: false` for that provider so the relay synthesises the count (§5.3). |
| `deepseek: missing /home/USER/.config/deepseek/router.json` (or `router.py`) | `install.sh` has not been run on this machine, or `--uninstall` removed the links. | Re-run `./install.sh`. |
| `router did not come up`, `deepseek --status` says `DOWN` | A bad `router.json`, a stale process holding port 8788, or a Python error at import. | `deepseek --restart`; then read `~/.cache/deepseek-router.log` (or `journalctl --user -u deepseek-router.service` if a unit exists). Port and host come from `router.json`'s `listen` block — a mismatch between it and anything else pointing at the router shows up here. |
| Every request returns `502 … unreachable` | Ollama is not running, or `base_url` in `router.json` is wrong for the leg that matched. | Start Ollama; confirm the port. The 502 body names the provider (§5.4), which tells you which leg failed. |
| Cloud turns fail with an auth error, local turns are fine | No credential in the environment — the router passes the *client's* token through, so it has nothing to forward. | `deepseek --status` prints `cloud … (auth: present\|MISSING)`. Export the key in `~/.bashrc` (or put it in `~/.config/deepseek/secrets.env`). §6. |
| A local turn hangs, then dies mid-stream; the router logs `client hung up mid-stream` | A 27B model can pause for minutes between chunks while it loads into VRAM, and an idle timeout kills the connection first. | Use the launcher, which sets `API_FORCE_IDLE_TIMEOUT=0` for non-`--cloud` sessions (§5.5). The relay's own upstream timeout is 900 s. |
| A model name you invented silently produces local answers | It hit the `local` provider's `"*"` catch-all — by design, so a typo can never bill the paid API. | `deepseek --routes MODEL` to see where a name lands; `GET /v1/models` is not the routing table (it lists only glob-free names, so no cloud names appear). |
| A long turn in a `deepseek --cloud` session overflows after switching to a local model | `--cloud` opts out of the local window cap, so the session was launched with the cloud's full window. | Start such a session with plain `deepseek` (or `--local`) if you intend to switch. The cap exists precisely so a mid-session switch stays safe. |

# Claude Code on this box: two backends

Claude Code runs here in two configurations that share one binary and one set of
credentials. They are easy to confuse, because both end with a `claude` process on
the RTX 3090 box and both can reach the same paid API.

| | Path A — plain `claude` | Path B — `deepseek` |
|---|---|---|
| What you type | `claude` | `deepseek` |
| Where requests go | `api.anthropic.com` (claude.ai login) | `127.0.0.1:8788`, which forwards |
| Backend | the Claude subscription models | DeepSeek cloud **or** local Ollama |
| Switch mid-session | no | yes — `/model` |
| Cost | subscription | billed on cloud routes, free on local ones |
| Wired by | nothing — stock defaults | `harness/deepseek/` in this repo |

Both are deliberate. Path A is the zero-ceremony default — it is what the `claude`
on your PATH has always been, and nothing in this repo changes it. Path B exists to
put DeepSeek and the local 3090 behind the same tool, without giving up the
subscription.

## Path A — plain `claude`, on the subscription

`~/.bashrc` sets no `ANTHROPIC_*` variables at all, so `claude` runs stock: it uses
the claude.ai login and the real Claude models. That is deliberate. The DeepSeek
token used to be exported here; it now lives in `~/.config/deepseek/secrets.env`
(mode 600), where only the harness reads it, so it is not readable by every process
in every shell.

Nothing else may set a backend for Path A either. Both paths end in the same binary
reading the same `~/.claude/settings.json`, so a `model` key there is inherited by
the other path — which is why the harness names its model on the command line
(`deepseek` → `claude --model`) and leaves plain `claude` to the subscription
default. If plain `claude` ever starts asking for `deepseek-flash`, an errant
`/model` in a `deepseek` session wrote that key — delete it again.

The DeepSeek cloud API is reached only through Path B.

One note that costs time to learn:

- **`[1m]` is stripped before the request goes out.** Claude Code accepts the suffix
  as a context-window hint and sends `deepseek-flash`. Anything matching on the model
  name must therefore match a *glob* (`deepseek*`), never the literal string — the
  router does exactly that, which is why Path B works.

## Path B — `deepseek`, with a backend you can change mid-session

```bash
deepseek                    # DeepSeek cloud (the default) — no local traffic at all
deepseek --local            # local Qwen 3.8 27B on the 3090
deepseek --cloud            # the same as the default; kept for muscle memory
deepseek --model NAME       # any model the router can reach
deepseek --status           # health, the model table, the routing table
deepseek --routes           # which upstream each model resolves to
deepseek --restart          # bounce the relay
```

The alias slots follow whichever backend the session *started* on, and that matters
more than it looks: the slots are where Claude Code sends everything that is not the
main model — background chores, session titles, the auto-mode classifier, subagents.
Left pointed at the local tags in a cloud session they quietly put all of that on the
3090 (measured 2026-09-24: 2.3k calls at a ~29k-token prefill each, GPU pinned at 99%
and 383W while the session model was cloud). So a cloud session binds all three slots
to the cloud model and never touches the local backend; a `--local` session binds them
to the local tags, where `/model` switches live — no restart:

| `/model` | `deepseek` / `--cloud` | `deepseek --local` |
|---|---|---|
| `opus` | `deepseek-flash[1m]` | `deepseek-flash[1m]` — the way back out |
| `sonnet` | `deepseek-flash[1m]` | `qwen38-coder:q4-128k` |
| `haiku` | `deepseek-flash[1m]` | `gemma3:1b` (background chores) |

**Why a relay is needed.** Claude Code reads `ANTHROPIC_BASE_URL` once, at launch,
and has no documented way to switch backends inside a session. Since DeepSeek *and*
Ollama 0.34.1 both speak the Anthropic Messages API natively, a pass-through relay
that picks an upstream by the **model name in the request body** buys that switch for
free: `/model` changes the name, the name picks the backend. Full detail, including
the context arithmetic and the local-backend shims, is in
[harness/deepseek/README.md](../harness/deepseek/README.md).

**The API key is never duplicated.** The router forwards whatever credential the
client sent (`auth: "passthrough"`), so the cloud leg authenticates with the same
token the launcher sourced from `~/.config/deepseek/secrets.env`. Nothing secret
enters this repo — and that one file is deliberately the only place it does live.

## Which model for which job

The point of the harness is that different work wants different models. Local
inference is free and private but slower and smaller; the API is fast and has a 1M
window but bills every token. Pick the side when you launch — a cloud session stays
cloud, and only a `--local` one can be moved between backends mid-session.

| Job | Reach for | Where |
|---|---|---|
| Long-context work, big refactors, whole-repo reasoning | `deepseek` | DeepSeek cloud, 1M window |
| Everyday coding in a repo the 3090 can hold | `deepseek --local` | `qwen38-coder:q4-128k`, 128K, free |
| Anything private — secrets, personal data, contracts | `deepseek --local` | stays on the box, never leaves |
| Session titles, taglines, small background calls | automatic | follows the session's own backend |
| OpenCode's coder route (a different tool entirely) | — | `qwen38-coder:q4`, 32K |

The stock `qwen38-coder:q4` is **not** usable as a Claude Code backend: Claude Code's
baseline request — system prompt plus every tool schema — is ~25K tokens before any
conversation starts, so a 32K window is spent before you type anything. That is why
the harness uses the derived `qwen38-coder:q4-128k` tag. OpenCode sends a lean prompt
and is unaffected, so it still uses the 32K tag. Both tags share the same weights.

## The scripts, and what each one does

Everything below lives in `harness/deepseek/` and is symlinked into place by
`install.sh` — this repo is the source of truth, not a backup copy.

| File | What it is |
|---|---|
| `router.py` | The relay. Stdlib only, no dependencies. Routes by model name, applies the per-provider shims, synthesises `count_tokens` for backends that lack it. |
| `router.json` | Providers, glob routing rules, per-model context windows, the local-only shims. No secrets. |
| `deepseek` | The launcher. Reads `router.json` with `jq`, starts the relay if it is down, sets the environment, `exec`s `claude`. |
| `install.sh` | Symlinks the three files above into `~/.config/deepseek/` and `~/.local/bin/`, then verifies preconditions. `--with-service` also installs the systemd unit; `--uninstall` reverses everything. |
| `models/qwen38-coder-128k.Modelfile` | The derived Ollama tag. `FROM qwen38-coder:q4` + `PARAMETER num_ctx 131072`. |
| `systemd/deepseek-router.service` | Optional: starts the relay at login instead of on demand. |
| `README.md` | The harness reference — architecture, context arithmetic, every shim and the failure it prevents, troubleshooting table. |

`install.sh` is idempotent and ends with a check list, so re-running it on a new
machine reports what is missing (no `jq`, no Ollama tag, no API token) rather than
failing later at request time.

## The skills, rules and hooks layer

`harness/deepseek/` decides *which backend* answers. `harness/claude/` decides *what
the session is told before it starts* — the installed skills, the path-scoped rules,
and the one hook that guards destructive git. It is a separate harness with the same
contract (repo is the source of truth, pieces symlinked out of it):

```bash
cd ai-stack/harness/claude && ./install.sh
```

Five projects are wired in: `mattpocock/skills` (the backbone — 25 skills covering
TDD, diagnosis, planning and review), `obra/superpowers` (the process layer — 15
skills plus a `SessionStart` hook that fires on every session), `blader/humanizer`,
`ayghri/i-have-adhd` (installed but off), and `cloudflare/security-audit-skill`, which
backs the `/pr-ready` pre-PR pipeline and brings a `rules/` policy pairing it with the
built-in `security-review`. The whole set costs **~2,895 tokens always-on**,
which is the number that matters: on Path B the advertised window is 98304 and the
baseline request is already ~25K, so a skill set is spending from roughly 73K, not 98K.

Two `google/artemis` and `affaan-m/ECC` were evaluated and **not** installed, for
reasons recorded in [harness/claude/README.md](../harness/claude/README.md) §6–7.
The short version of each: ECC's `rules/common/` carries no `paths` frontmatter, so
its 10 files would load into every session in every repo at ~4,600 tokens; and
Artemis's five MCP tools would do the same, because **MCP tool search is disabled
whenever `ANTHROPIC_BASE_URL` is a non-first-party host** — the usual "more MCP
servers barely costs anything" assumption does not hold on this box.

That last point is the one to remember before adding any MCP server here.

## Standing up a new machine

```bash
git clone https://github.com/ohmzi/ai-stack && cd ai-stack

# 1. the model tag the harness serves (needs the base tag already present)
ollama create qwen38-coder:q4-128k -f harness/deepseek/models/qwen38-coder-128k.Modelfile

# 2. put the harness in place and find out what is still missing
harness/deepseek/install.sh

# 3. the one prerequisite nothing here can install — see below
echo 'DEEPSEEK_API_TOKEN=sk-…' >> ~/.config/deepseek/secrets.env

deepseek --status
```

### The one manual prerequisite

The API key. `install.sh` will not write it, and nothing in this repo contains it —
by design, since the repo is public and the key is a bearer credential. Put it in
`~/.config/deepseek/secrets.env` (mode 600) yourself; the launcher sources that file,
passes the key through to the cloud upstream, and the cloud route 401s without it.
Local routes are unaffected.

## Things that cost time to find

- **`--status` is not read-only.** It starts the relay if it is down. That is
  convenient interactively and surprising in a script that expected a pure read.
- **The context window is read at launch, not per request.** A `--local` session can
  move to a model with a different window via `/model` and keeps whatever window it
  was launched with, so the launcher advertises the **local** model's window — the
  smaller of the two. An over-large window hands the local model a prompt it answers
  with a hard 400. A cloud session has no local model to overflow and keeps the
  window its model really has. The launcher *unsets* the override for cloud sessions
  rather than merely not setting it, so one launched from a shell that already
  carries a local session's cap (a nested shell, or Claude Code's Bash tool inside a
  `--local` session) does not silently inherit it.
- **Local tags do not share a window.** `qwen38-coder:q4-128k` is 131072;
  `gemma3:1b` carries no `num_ctx` at all and runs at the server's
  `OLLAMA_CONTEXT_LENGTH` (32768). `router.json` pins each one under `model_windows`,
  and the provider default is deliberately the *smallest*, so a tag added later
  cannot be over-advertised.
- **Two diagnostics you will see and can ignore.** `[claude-code:unrecognized_model]`
  is Claude Code noting it does not know the model name; it is non-fatal and appears
  once per model string per process. And when a local request 400s with
  `exceeds the available context size`, that is Ollama, not the relay.
- **Features that turn off on a non-Anthropic host.** Because both paths use a custom
  `ANTHROPIC_BASE_URL`, Claude Code disables Remote Control and server-managed
  settings regardless of what the endpoint forwards. This is not a harness problem
  and cannot be worked around from here.

## How this relates to OpenCode

`opencode` is a separate harness that also runs against this box's models, and it is
configured independently in `~/.config/opencode/opencode.json`. It is the sole
consumer of the 32K `qwen38-coder:q4` tag. The two share the Ollama server and the
GPU, which matters at load time: Ollama keys a runner by model **plus options**, so
each distinct tag is a separate resident model and they cannot all co-exist on one
24 GB card. [docs/MODELS.md](MODELS.md) carries the measured footprints.

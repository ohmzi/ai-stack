# OhmzAI Coding Engine — design

_2026-10-04. One coding engine — the Claude Code harness (`harness/deepseek`) on the local coder —
behind three front doors: the OhmzAI **Code** button, the Hermes coding profile, and the CLI.
Local is the default everywhere; the cloud is reachable only by an explicit, confirmed choice, and
never automatically. Front doors share one policy module so the rule is written once._

Amends the 2026-09-29 OhmzAI Code Agent design (§8): that document's bridge architecture stands,
but its backend default flips from API to local, and its three subsystems stop being independent.

## 1. What was asked

1. **Local coding, not API.** When code is engaged — whether by the deterministic route in default
   mode or by explicitly pressing the Code control — the work runs locally.
2. **Best results first.** The interactive Claude Code harness on a local model is the known-best
   coding experience on this box; the design should put *that* behind all three surfaces rather
   than replace it.
3. **Make the local model reachable.** In the `deepseek` CLI, `/model` offers no way to reach the
   local model today.
4. **Revise all three**, say how they fit together (and whether that is wise), define a fallback,
   and test it.

## 2. Decisions

| Question | Decision |
|---|---|
| The cloud leg | **Local by default; the API only behind an explicit confirmation.** Never inferred from wording. |
| The deterministic code route | **Stays a single-shot local answer**, plus an "open this as a Code session" affordance. No agent is spawned by a guess. |
| Integration | **One shared engine, three front doors.** Not three independent stacks, not one front door. |
| On failure | **Offer the choice** (retry · single-shot local · confirmed cloud) rather than degrading silently or failing hard. |
| The CLI's default | **Cloud stays the launch default**; local becomes reachable from a cloud session in-session. |

## 3. How it works today

One chat entry (`Assistant`) is served by one pipe, `pipes/auto_assistant.py`. The composer's four
controls are client-side: **Internet** (`features.web_search`) and **Code**
(`features.code_interpreter`) are OpenWebUI built-ins; **Task** (`filters/task_mode.py`) and
**Notebook** (`filters/notebook_mode.py`) are this repo's filters. Turn precedence is first-match:

```
1. Notebook ON   → Open Notebook  /api/search/ask            local service
2. Task ON       → Hermes agent                              local model
                   (task_mode.py clears code_interpreter, so Task beats Code)
3. media intent  → ComfyUI image / edit / video              local GPU
                   ("make a picture of a cat" with Code on still renders)
4. CODE          → qwen38-coder:q4  (fixed 32k ctx, GPU lock)
                   single-shot · no tools · no files · no session
5. web_search    → chat + RAG                                local
6. else          → plain chat                                local
```

The Code step (`auto_assistant.py:8481`) is entered by `code_btn` (the client boolean, read by
`_code_mode` at `:6285`) **or** `_is_code_request` (a `_CODE_STRONG` regex, else the `gemma3:1b`
classifier through `_classify_code`). Both entries converge on the same engine —
`self.coder_model = "qwen38-coder:q4"` at `CODER_CTX = 32768` — so today's Code path is already
local, and already single-shot.

Outside the chat:

```
Hermes coding profile → coding_task tool → `deepseek --cloud -p`   CLOUD, no confirmation
harness/deepseek CLI  → Claude Code → relay ─┬─ qwen38-coder:q4-128k   LOCAL
                                             └─ DeepSeek cloud          CLOUD
```

`harness/deepseek/deepseek` launches on the cloud by default (`MODEL="$CLOUD_MODEL"`) and binds
Claude Code's three model aliases through `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`. In a
**cloud** session all three aliases point at the cloud — deliberately, because on 2026-09-24 a
cloud session whose aliases pointed local put every background call (titles, the auto-mode
classifier, chores) on the 3090: 2.3k calls, GPU pinned at 99%, 383 W, while the session model was
cloud. In a **local** session the aliases are `opus`→cloud, `sonnet`→local coder, `haiku`→local
fast model.

`router.py` routes by the model name in the request body, serves `GET /v1/models` (glob-free names
only, so cloud names never appear — the listing is not the routing table), and ends the `local`
provider's list with `"*"` so a typo can never bill the API.

**The three gaps this design closes:**

- The Code button gives a single-shot chat, and the agentic experience behind it is designed but
  unbuilt (`docs/superpowers/specs/2026-09-29-ohmzai-code-agent-design.md`; no `harness/ohmzai-code/`).
- A cloud `deepseek` session cannot reach the local model at all — all three aliases are cloud.
- `coding_task` runs on the cloud by default with no confirmation channel, contradicting "local
  coding, not API".

## 4. Architecture

The engine is the existing harness: the CLI + `router.py` + the local model. One new module holds
the policy; each front door is a thin adapter over it. Only the Code front door is new code.

### 4.1 `harness/deepseek/coding_policy.py` — the shared policy

A pure, I/O-free module that answers three questions. The launcher, the Hermes plugin and the
bridge all read it, so the rule exists once.

1. **Which backend?** `local` by default. `cloud` only when the caller passes an explicit,
   confirmed intent. It never infers from text — the §6 grammar of the 2026-09-29 spec established
   that `use the api to fetch prices` must not read as a switch, and that lesson holds.
2. **Which model, with what window?** One table extending the relay's existing `model_windows`:

   | backend | model | context_window | output_reserve |
   |---|---|---|---|
   | local (default) | `qwen38-coder:q4-128k` | 131072 | 32768 |
   | cloud (confirmed) | `deepseek-flash[1m]` | not pinned here — Claude Code's own window for the cloud model | — |

   A caller asks for a backend and receives `(model, context_window, output_reserve)`; no front
   door re-derives the numbers.
3. **Why did it fail?** A typed reason, not a string: `gpu_busy`, `model_missing`, `harness_error`,
   `context_overflow`, `cancelled`. This is the contract front doors render choices from (§5).

`router.py` keeps routing by model name, unchanged. The policy is *what the front doors ask*;
the relay is *how a name becomes an upstream*.

### 4.2 Front door: the CLI

One symmetrical rule replaces the per-session alias table:

| alias | cloud session | local session | role |
|---|---|---|---|
| `opus` | `deepseek-flash[1m]` | `deepseek-flash[1m]` | always the cloud — the escape upward |
| `sonnet` | **`qwen38-coder:q4-128k`** | `qwen38-coder:q4-128k` | **always the local coder — the coding switch** |
| `haiku` | `deepseek-flash[1m]` | `gemma3:1b` | follows the launch provider; carries background work |

`/model sonnet` means "go local" in either session and `/model opus` means "go cloud". `haiku`
continues to follow the launch provider, so a cloud session's background chores stay off the GPU —
the 2026-09-24 guard is preserved, not traded away.

Two consequences designed in, not discovered:

- **The window.** Claude Code binds its context window once, at launch, from
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. A mid-session switch does not resize it. Binding `sonnet` to
  the 128k tag keeps a switch from a cloud session safe whenever the cloud window is ≤ 131072; a
  session launched wider can `400` on its first local turn, and that surfaces as
  `context_overflow` with a relaunch hint (§5) rather than a mystery.
- **Visibility.** Nothing today shows which backend a session is on. A Claude Code **statusline**
  (plugin hook) renders `local · qwen38-coder` or `cloud · deepseek-flash`, so the state and the
  switch are both discoverable.

`deepseek --local` and `deepseek --cloud` keep working exactly as now.

### 4.3 Front door: the Hermes coding profile

`hermes/plugins/coding_task/__init__.py` currently runs `deepseek --cloud -p <task>`. It becomes a
consumer of `coding_policy`: it asks for the local model and window and runs the local argv. **No
cloud on this surface** — a background tool has no interactive channel on which to confirm, so
"confirmed cloud" is unreachable there by construction.

Everything else about the tool is unchanged: bounded (timeout, output cap), allowlisted roots
(`CODING_TASK_ROOTS`), the denylist, and its exclusion from the chat-facing toolset.

### 4.4 Front door: the OhmzAI Code button

The 2026-09-29 bridge architecture stands — `harness/ohmzai-code/{bridge.py,turn_exec.py}`, the
committed systemd unit, one `auto_assistant` branch, a host-side process because the OpenWebUI
container cannot run Claude Code. Four changes:

- **Default flips to local.** A new Code chat starts local, with `coding_policy` deciding. The
  spec's §6 switch grammar is kept verbatim (its measured false-positive cases are why it exists);
  only its terminal state changes — reaching the cloud requires the explicit click-confirm, never
  a phrasing.
- **Failure returns a typed reason** to the pipe, which renders the §5 choices.
- **The auto route gains a handoff.** The deterministic route keeps its single-shot local answer
  and appends one affordance: *open this as a Code session*, which seeds a Code chat with the
  conversation. Nothing agentic runs unless clicked.
- **Unchanged:** one resumed session per chat, allowlisted workspace (git repos under
  `~/StudioProjects` and `~/src` plus a scratch dir; `~/ai-stack` excluded in v1), admin-only
  pinned by UUID, the OS sandbox, the per-chat lock, streaming, and the GPU-idle gate for local
  turns.

## 5. Failure and fallback

The engine never switches backend on its own. On failure it returns a typed reason and the front
door offers the choice:

| reason | offered |
|---|---|
| `gpu_busy` | wait & retry · single-shot local answer now · cloud (confirmed) |
| `model_missing` | single-shot local · cloud (confirmed) |
| `harness_error` | retry · single-shot local · cloud (confirmed) |
| `context_overflow` | fresh session / relaunch hint · cloud (confirmed) |
| `cancelled` | nothing — the user stopped it |

"Single-shot local answer" is today's Code behaviour (`qwen38-coder:q4`, 32768), so the fallback is
a path that already works. The CLI offers the same choices as printed options; the Hermes tool
reports the reason and stops (no interactive channel).

**What "confirmed" means differs by door**, and it is the door — not a model — that decides:
the CLI, an explicit `/model opus` or `deepseek --cloud`; the Code button, the click-confirm; the
Hermes coding tool, unreachable (§4.3).

## 6. Testing

1. **Unit** — `coding_policy` is pure: its default, its table and its reason mapping get plain unit
   tests. `tests/test_code_commands.py` keeps pinning the §6 grammar.
2. **Suites** — `tests/test_autoroute.py` and `tests/test_router.py` for the pipe branch and the
   handoff affordance; `tests/test_coding_task_plugin.py` updated for the local argv.
3. **Live smoke** (bounded):
   - `deepseek --status` shows the symmetric alias table.
   - `/model sonnet` in a **cloud** session lands on `qwen38-coder:q4-128k`, and `nvidia-smi`
     shows the local runner while the session's cloud turns leave the GPU alone.
   - `coding_task` runs local, with no request to `api.deepseek.com`.
   - Press **Code** → a streamed local agent turn.
   - Hold the GPU (or stop the model) → the choice prompt appears with the right reason.
   - The bridge canary (2026-09-29 §4.10) still proves a real sandboxed turn works.

## 7. Non-goals and risks

- **Not** changing the relay's routing-by-name, the `"*"`-to-local catch-all, or the §6 grammar.
- **Not** giving the Hermes tool a cloud path.
- **Not** making the deterministic route agentic.
- **Risk — alias blast radius.** Binding `sonnet` to local could put traffic on the GPU if
  something besides an explicit `/model sonnet` uses that alias (subagents are the candidate). The
  live smoke must watch `nvidia-smi` during a cloud session; if subagents route local, the fix is a
  per-agent model setting, not abandoning the design.
- **Risk — window mismatch** on a mid-session switch; surfaced as `context_overflow`.
- **Risk — the bridge is the largest new surface** and carries the 2026-09-29 security work
  (OS sandbox + relay gate). It is not simplified here.

## 8. Relationship to the 2026-09-29 spec

That document remains the reference for the bridge: session model, workspace resolution, the
security model, GPU choreography, the canary and its parameter table all stand. This design amends
it in four ways — backend default (API → local), the three subsystems becoming one engine with
three front doors, the typed-failure/fallback contract, and the auto-route handoff — and inherits
its unbuilt status: none of `harness/ohmzai-code/` exists yet.

## 9. Open items

- Whether Claude Code's `/model` picker accepts a typed custom model id; if it does, it is a
  complementary path to `sonnet`, not a replacement for it (the window caveat still applies).
- Whether the Code-button workspace allowlist should keep excluding `~/ai-stack` once the bridge's
  OS sandbox is proven live.

## 10. Delivery

Three pieces, smallest first, each useful on its own and none blocking another — so this lands as
three plans rather than one large change:

1. **Policy + CLI** (§4.1, §4.2) — `coding_policy.py` and the symmetric alias table. Self-contained;
   testable with no new service and no UI.
2. **Hermes** (§4.3) — `coding_task` reads the policy and runs local.
3. **The Code button** (§4.4) — the largest piece and the only new component. It lands last,
   building on the policy the first two have already exercised.

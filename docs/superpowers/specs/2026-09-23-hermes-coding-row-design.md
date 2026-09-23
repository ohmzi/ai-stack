# Hermes coding row — design

_2026-09-23. An admin-only OpenWebUI row that talks to a hermes agent, and can be told to do
coding work through the local `deepseek` harness. Two deliverables, sequenced: upgrade the hermes
runtime first, then build the row._

## 1. What was asked

1. Chat with the hermes agent from OpenWebUI the way models are chatted with, and instruct it to
   do coding tasks using the `deepseek` CLI on this machine.
2. Only admins may talk to it.
3. Hermes gets coding work as a scheduled job too, not only interactively.
4. The hermes runtime is upgraded to the latest version first.

## 2. What was verified before designing

Everything in this table was measured on this box on 2026-09-23, not assumed. It is the reason the
design has the shape it has.

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 1 | The `deepseek` CLI runs headless, with no TTY, from a minimal environment | `env -i HOME=… PATH=… deepseek -p "Reply with exactly: OK"` | **Works** — returned `OK` |
| 2 | Headless Claude Code can actually write files, not just answer | Same, asked to create `hello.txt` in a scratch dir | **Works** — file created, contents `OK` |
| 3 | It works because of a global permission mode, not luck | Read `~/.claude/settings.json` | `permissions.defaultMode: "auto"` |
| 4 | The `deepseek` auth token survives a non-interactive shell | `env -i … deepseek --status` | `auth: present` |
| 5 | …and the documented reason for that is **stale** | Read `docs/CLAUDE_CODE.md:56` and the wrapper's own comment vs. the filesystem | `~/.config/deepseek/secrets.env` **exists** (0600). Both files claim it does not, and that the token lives only in `~/.bashrc`. Corrected by this work. |
| 6 | The backend must support tool calls | Ran the same probe with `--model gemma3:1b` | **Fails**: `does not support tools`. Claude Code cannot be driven by the `haiku` slot. |
| 7 | Cron output and delivery are per-`HERMES_HOME` | Read `scripts/hermes_delivery.py:45,164` | `OUT_DIR` and `JOBS_FILE` are hardcoded to `~/.hermes/…` |
| 8 | The OpenWebUI container reaches the host loopback | `docker inspect open-webui` | `NetworkMode: host` |
| 9 | Hermes supports a second gateway with its own port | `hermes_cli/profiles.py:1629-1635`, `service_manager.py:655-660` | Supported: a profile gets its own `HERMES_HOME`, `config.yaml` and gateway unit; `API_SERVER_PORT` per profile |
| 10 | A plugin can register a tool and a toolset | `hermes_cli/plugins.py:410` (`PluginContext.register_tool`), `model_tools.py:454` | Supported, and `platform_toolsets` can enable it |
| 11 | `approvals.cron_mode` is a separate key from `approvals.mode` | `tools/approval.py:2632` | Yes — cron's `deny` cannot be weakened by changing the interactive mode |
| 12 | The pipe's HTTP timeout is globally 300 s | `pipes/auto_assistant.py:288` | `HERMES_TIMEOUT_S = 300`, shared by every hermes call the pipe makes |
| 13 | Per-profile gateways are **retired** at the upgrade target | `hermes_cli/gateway_multiplex_mode.py`, CLI help | `gateway.multiplex_profiles` defaults on and an explicit `false` is RETIRED; one gateway serves every profile and a second profile is reached at `/p/<name>/…` on the shared listener. **Supersedes the §4.1 shape drafted against v0.19.0.** |
| 14 | Profiles on the shared listener are isolated by key | `gateway/platforms/api_server.py:1394-1438` (tag) | The default key is refused on `/p/coding/…`; the coding key is refused on the unprefixed surface; absent, short or wrong → **401** (identical body, so not a presence oracle). An unknown profile name → 404. |
| 15 | A profile's own config governs its `/p/…` requests | `gateway/run.py:1805-1824`, `:2873-2891`; `api_server.py:2206-2207` (tag) | `platform_toolsets`, `model.*` and the profile `.env` are the coding profile's, and the request is still platform `api_server`. So `platform_toolsets.api_server` in **that** profile is the toolset authority. |
| 16 | `API_SERVER_KEY` alone enrolls the listener | `gateway/config_env.py:308-315` (tag) | Nothing under `gateway/` reads `API_SERVER_ENABLED`; enrolment is gated solely on a usable key (≥16 chars). The docs' env table is stale. |
| 17 | A plugin-registered tool never reaches the approval gate | `hermes_cli/plugins.py:2280-2284` (b), `:1864-1913` (tag) | The gate runs only from a plugin's own `pre_tool_call` hook returning `{"action": "approve"}`. `register_tool` alone is not gated. **Closes §9.1.** |
| 18 | User plugins are **opt-in** | `hermes_cli/plugins.py:1479-1487` | A plugin not named in `plugins.enabled` loads nothing; the live config has no `plugins:` key at all. |
| 19 | `deepseek` needs `HOME`, and `--cloud` is the backend pin | Measured: `env -i HOME=… PATH=… deepseek --cloud -p …` → `OK` | It reads `~/.config/deepseek/` and `~/.claude/`, so `HOME` is required. There is **no `--repo` flag** — it wraps `claude` — so the working directory must be `cwd=`. |
| 20 | The upgrade silently kills cron unless the local provider is fixed | `cron/scheduler_provider.py:415-416` (tag) vs `hermes/plugins/gpuguard/__init__.py:231` | `start()` gained `profile_gate`, `profile_adapters`, `default_profile`. The current override raises `TypeError`, the supervised ticker respawns in a loop, and **no job ever fires** while the API keeps serving and every pipe-side check keeps passing. |
| 21 | The upgrade runs the whole config ladder | live `config.yaml` has no `_config_version` | Missing coerces to 0, so every step up to 45 applies: `platform_toolsets.*` gain `connections`, `cron.model_drift_guard` and `gateway.multiplex_profile_allowlist` are removed, and several unrelated defaults move. |
| 22 | `/v1/responses` is **not** profile-isolated | `gateway/platforms/api_server.py:1195`, `:709-715`; `api_server_openai_routes.py:1126-1147` (tag) | One response store per process, built by the default profile. A caller with the coding key can read default-profile responses **by `conversation` name** and inherit its `session_id`. `/v1/chat/completions` with `X-Hermes-Session-Id` is isolated (separate `state.db`), which is what this design uses. |

Rows 9, 10 and 11 were read in **v0.19.0** source and remain true there, but the plugin, profile and
service-manager machinery was rewritten at the target version: rows 13-16 and 20-21 supersede them
for anything this design builds on.

## 3. The problem this design actually solves

The naive build — add `terminal` to `platform_toolsets.api_server` so the agent can shell out to
`deepseek` — was rejected because that key was assumed **shared**: enabling it would hand a shell on
the host to every Task-mode user, admin or not, reversing the decision recorded at
`pipes/auto_assistant.py:25-26`: *"no terminal, no browser, no code execution on the chat-facing
surface. Chat-reachable text must not be able to shell out."*

**That premise is now wrong, and the design is stronger for it.** Row 15 establishes that
`platform_toolsets.api_server` is read from the *profile's own* config for that profile's requests.
Enabling `terminal` on the `coding` profile would therefore **not** reach the Task path at all —
profile isolation answers the original objection.

The bounded tool is kept anyway, but as a deliberate narrowing rather than a forced workaround: a
fixed-argv `coding_task` is auditable in a way a general shell is not, cannot become a command
through a quoting bug, and gives the admin row exactly one capability instead of everything. The
profile is the boundary; the bounded tool is what makes the boundary worth having. What the
original reasoning should have said is that "shared" was a property of the *default* profile's
surface, not of the key itself.

So the design gives the agent a **bounded tool** instead of a shell, and puts that toolset on a
**separate profile** that only the admin row can reach with a key of its own.


## 4. Architecture

Four new pieces, one per concern.

### 4.1 Hermes profile `coding`, multiplexed on the shared listener

`hermes profile create coding` gives it its own `HERMES_HOME` at `~/.hermes/profiles/coding/` — its
own `config.yaml`, `.env`, `state.db`, cron store and plugins dir. It gets **no gateway unit of its
own**: at the target version that topology is retired (row 13), and `hermes update` can fold one
away without asking and with no rollback.

- Reached at `http://127.0.0.1:8642/p/coding/v1/chat/completions` on the existing listener. No
  second port, no second unit; the primary gateway serves both surfaces.
- The profile `.env` carries its own `API_SERVER_KEY` (≥16 chars, generated by hand — nothing
  generates it) and nothing else about the listener. `API_SERVER_ENABLED` is read by no code under
  `gateway/` (row 16); the key alone is what makes the profile reachable.
- The two keys do not cross (row 14): the Task path's key cannot reach `/p/coding/…`, and the
  coding key cannot reach the unprefixed surface. That is what preserves §3's isolation argument
  now that there is one listener rather than two ports.
- `platform_toolsets.api_server`, in the **coding profile's** config:
  `[web, file, memory, session_search, todo, skills, coding_task]`
  **Absent by intent: `terminal`, `browser`, `shell_exec`, and any code-execution tool.** Row 15 is
  what makes this binding — the profile's own list is the authority for its own `/p/…` requests.
- `platform_toolsets.cron`: `[web, file, memory, todo, skills, coding_task]`
  Deliberately also without `terminal`, so a scheduled coding job goes through the same bounded
  tool as an interactive one. Delivery on this profile is deterministic — jobs emit `LOG:` /
  `ALERT(…)` and infrastructure delivers — so the cron-side `terminal` that the primary gateway
  needs for `curl` is not needed here.
- `cronjob` remains enabled, per the request that coding work can be scheduled. See §6 for the
  delivery work this creates.
- Its own plugins dir (`~/.hermes/profiles/coding/plugins/`), which it does **not** inherit from
  `~/.hermes/plugins/`. Both `gpuguard` and `coding_task` are symlinked into it; `profile create`
  does not create that directory, so it is made by hand.
- `cron.provider: gpuguard` in its config. One caveat, from the same topology: the provider object
  is resolved **once per process, from the launching profile's config**, so when the default
  gateway ticks every home it uses the default's provider. Both name `gpuguard`, so behaviour is
  identical either way — but that is why this is a coincidence to preserve, not a per-profile
  setting to lean on.
- `model:` is the same as the primary — `hermes-genesis:agent` via Ollama. Decided 2026-09-23 over
  a cloud backend: no new credentials, and gpuguard already gates that runner. The consequence is
  that the pipe must do the same chat-tenant handoff `auto_assistant` does, because two ~17 GB
  runners cannot co-reside on the 3090.


### 4.2 Plugin `hermes/plugins/coding_task/`

Registers exactly one tool. `gpuguard` is **not** a template to copy: it is loaded as a *cron
provider* by name, not through the plugin scanner, and it has no manifest. A tool-registering
plugin needs a `plugin.yaml` manifest, a `register(ctx)` entry point, **and** to be named in
`plugins.enabled` — user plugins are opt-in and the live config has no `plugins:` key at all
(row 18). Without that last piece the plugin is discovered and then silently not loaded.

```
coding_task(task: str, repo: str = None) -> str
```

The handler signature is `handler(args: dict, **kw) -> str` — the registry passes extra keyword
arguments at every dispatch site, so a handler that accepts only `args` raises `TypeError`. It is
registered `is_async=False`: sync handlers run inline on the dispatching thread, and the api_server
platform runs the whole conversation inside a thread executor, so a minutes-long subprocess cannot
stall the event loop. There is **no `pre_tool_call` hook**, which is what keeps the tool out of the
approval gate (row 17) — adding one that returns `{"action": "approve"}` would make every coding
task block awaiting a human decision nothing in this stack can answer.

Implementation rules, each of which is a test:

- **List argv, never a shell.** `subprocess.run(["deepseek", "--cloud", "-p", task], …)`.
  The task string is never interpolated into a shell, so no quoting bug can become a command.
- **Pinned working directory.** `repo`, when given, must `realpath` to inside an allowlisted root
  (`CODING_TASK_ROOTS`). Anything else is refused, so `../../` cannot escape. This is `cwd=`, not an
  argument: `deepseek` has **no `--repo` flag** (row 19) — it is a wrapper that ends in
  `exec claude --model … "$@"`.
- **Explicit environment, not the gateway's.** The gateway's env comes from a systemd unit that
  deliberately exports almost nothing (`PATH`, `VIRTUAL_ENV`, `HERMES_HOME`). The tool builds a
  known env instead, and it **must include `HOME`**: `deepseek` reads `~/.config/deepseek/router.json`
  and `~/.claude/settings.json`, and dies at startup without it (row 19). Credentials are not
  inherited — `deepseek` sources `~/.config/deepseek/secrets.env` itself, which is what makes a
  minimal env sufficient (row 19, measured).
- **Pinned backend.** `--cloud` explicitly, so the task can never be answered by a local model.
  `--local` is never passed: it loads `qwen38-coder:q4-128k` at ~21.8 GB, which **cannot co-reside**
  with the 18.3 GB chat tenant and costs an eviction per task.

- **Own timeout, output cap, and honest failure.** Default timeout **360 s**, overridable with
  `CODING_TASK_TIMEOUT_S`. The default is not arbitrary and not what this design first proposed:
  hermes caps a *concurrent* batch of tool calls at 420 s
  (`agent/tool_executor.py:99`, `_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`), and a coding task the model
  batches alongside another call is killed at that ceiling regardless of what the tool was
  configured to wait for. A single call is dispatched sequentially and is uncapped, but the plugin
  cannot assume it will always be called alone. 360 s stays under it, and a task that genuinely
  needs longer is a deliberate one-line change rather than a silent 420 s failure. The gateway's
  inactivity window is 1800 s and is not the binding constraint.
  Default output cap 32 KiB per stream, which also keeps the result inside hermes's 100,000-char
  per-result budget — past that it spills to a file and the agent sees a 1,500-char preview.
  Returns exit code, captured stdout/stderr and duration. A non-zero exit, a missing binary and a
  timeout are three distinct reported outcomes, never a silent empty string.
- **The allowlist root is `/home/ohmz`**, decided deliberately on 2026-09-23 rather than left as a
  default. `CODING_TASK_ROOTS = ["/home/ohmz"]`; when `repo` is null the tool runs in the first
  root. The trade was named before the choice was made and accepted: at this width the tool can
  reach `~/.hermes`, `~/.claude`, `~/.ssh` and `secrets.env`, not just source trees.
- **A denylist still applies inside the root.** `CODING_TASK_DENY` refuses the subtrees where a
  write is catastrophic and never the point of a coding task: `~/.ssh`, `~/.gnupg`,
  `~/.claude`, `~/.hermes`, `~/.config/deepseek`, and anything matching a `secret`-shaped name.
  This follows the precedent hermes itself sets — its hardline blocklist applies "regardless" of
  approval mode — and it costs nothing a legitimate coding task needs. It is a recommendation,
  not a red line: it can be dropped in one line if it ever gets in the way.

### 4.3 Pipe `pipes/hermes_coding.py`

A thin SSE client, modelled on the parts of `auto_assistant.py`'s `_hermes_stream` that already
work, registered in `scripts/deploy_pipe.py`'s `PIPES` map so it inherits the existing deploy,
drift-test (`tests/test_deployed.py`) and `--rollback` machinery.

- **Endpoint:** `http://127.0.0.1:8642/p/coding/v1/chat/completions` — the shared listener, the
  profile path, not a second port. Its own key, read from
  `/app/backend/data/hermes_coding_api_key` (the container's view of a host file), separate from
  `auto_assistant`'s. Its own HTTP timeout, parameterised rather than reusing the shared
  `HERMES_TIMEOUT_S = 300` — raising that globally would loosen hang detection for the Task path for
  no reason. It must exceed the coding task's own timeout plus agent overhead.
- **Only `/v1/chat/completions`.** `/v1/responses` is not profile-isolated (row 22): a caller with
  this key can read the default profile's stored responses by `conversation` name and inherit its
  session id. Nothing here uses that surface.
- **Multi-turn continuity** via `X-Hermes-Session-Id: owui-<chat_id>`, keyed to the OpenWebUI chat
  id. The header means **REPLACE, not append**: the server discards posted history and rebuilds
  context from its own persisted transcript plus the last message in the body. So the pipe sends
  **only the new turn**, not the accumulated conversation — resending history is harmless but
  wasted, and a wrong or empty session degrades to a context-free turn rather than an error.
- **The chat-tenant handoff.** The coding profile's agent runs the same local runner as the
  primary, so the pipe releases the chat tenant before the call exactly as `auto_assistant` does —
  two ~17 GB runners cannot co-reside on the 3090, and leaving Ollama to evict under memory
  pressure mid-load is the non-deterministic version of the same thing.
- Relays hermes's inline tool-progress markers (`event: hermes.tool.progress`, carrying
  `tool`/`emoji`/`label`/`status`), so a multi-minute coding task shows signs of life rather than a
  silent wait. **Unverified:** whether OpenWebUI's pipe runner preserves custom SSE `event:` names
  when it hands bytes to the pipe. If it strips them, tool signalling has to come from
  `/p/coding/api/sessions/{id}/chat/stream` instead, where the event name is the discriminator.
  This is an empirical check at build time, not something reading the source settles.
- **Two deploy gates the plan must satisfy**, both discovered by reading the deploy machinery:
  `deploy_pipe.py` refuses a pipe with no existing `function` row (so the row is created once,
  deliberately), and `tests/test_deployed.py` requires a README roster row whose "Model in the UI"
  cell matches the name the pipe's `pipes()` returns. Separately, the pipe file must never contain
  the literal `from utils`, `from apps`, `from main` or `from config` **anywhere, comments
  included** — OpenWebUI rewrites those on load and deploy refuses the file.


### 4.4 Docs

`docs/HERMES_AGENT.md`, `docs/MODELS.md`, `docs/openwebui-config-snapshot.md`, and a correction to
the stale credential claim in `docs/CLAUDE_CODE.md:56`.

## 5. Security model

### 5.1 Three independent admin gates

| Layer | Mechanism | Defeats |
|---|---|---|
| OpenWebUI | Verified in the running container (0.11.3), not inferred from docs. A pipe-derived model with **no `model` row is admin-only outright** — `utils/models.py` has an explicit `elif user.role == 'admin'` branch commented *"No DB entry means no access control configured yet; only admins can see unconfigured models."* With a row and no grant it is filtered for non-admins by `AccessGrants`. So the deciding act is **not creating a grant**, and being deliberate about whether a `model` row exists at all. Admin visibility itself rests on `BYPASS_ADMIN_ACCESS_CONTROL`, which resolves `True` here via `ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS`. | The UI |
| The pipe | A role check on `__user__` — `role == "admin"`, or handle in `TASK_ADMINS` (`pipes/auto_assistant.py:317`). The handle is the **email local part**, sanitized (`omariqbal97@…` → `omariqbal97`), not the display name. | A direct API call that bypasses the UI |
| Hermes | Row 14: the primary profile's key is refused on `/p/coding/…` and the coding key is refused on the unprefixed surface, both 401. | Anything that holds only the Task path's key — including `auto_assistant` itself |

Any one alone is defeatable; together they cover the paths that exist. The third gate is what
replaces the separate port the design originally relied on.


### 5.2 The residual risk, stated plainly

`coding_task` is effectively **arbitrary code execution as the user, across the home directory**.
`deepseek -p` runs Claude Code under `permissions.defaultMode: "auto"` (§2 row 3), so it writes
files and runs commands without asking.

The root was widened to `/home/ohmz` on request, so be precise about what is left. The fixed argv
still means no shell is ever involved and no argument is interpolated — that is real and it is
tested. The denylist in §4.2 removes the catastrophic targets. But within `~` the tool can
otherwise write anywhere, **including `~/.bashrc` and `~/.config`**, so it is not confined to
source trees and must not be described as confined to them. The timeout and output cap bound cost,
not reach.

This is the accepted cost of the bounded-tool choice, and it is exactly why the row is admin-only,
why the tool is absent from the shared surface the Task path uses, and why `terminal` stays off
everywhere. It should not be described anywhere as sandboxing, because it is not.

### 5.3 What is *not* weakened

- No general shell is enabled on any chat-facing surface, on either profile.
- Cron's dangerous-command mode stays `deny` — it is a separate config key (row 11).
- The default profile's config, toolset, jobs and scheduler are untouched. The only change to the
  shared process is that it now also serves the `/p/coding/…` path, under a key it does not hold
  (row 14).


## 6. The delivery gap, and its fix

This is the largest piece of new integration work, and it is a direct consequence of the
"coding work can be scheduled" requirement.

`scripts/hermes_delivery.py` hardcodes `OUT_DIR = ~/.hermes/cron/output` (`:45`) and
`JOBS_FILE = ~/.hermes/cron/jobs.json` (`:164`). A job created on the `coding` profile writes to
that profile's own `HERMES_HOME` instead. Left alone, such a job would **run, satisfy its
condition, and deliver nothing** — the silent-loss shape this stack has now been bitten by
repeatedly (SMS dropping links; alerts to a domain with no MX record).

Fix: make the delivery watcher take a list of hermes roots rather than one hardcoded path, and scan
each on the same tick. The split is narrower than it first looks, and that is the whole design:

- **Per-root:** only the output glob (`<root>/cron/output/<job>/*.md`) and `JOBS_FILE`
  (`<root>/cron/jobs.json`), the latter merged across roots for name and schedule lookup.
- **Shared, one file each:** `.delivered.json` — its keys are absolute paths, so a single file
  serves every root and keeps the process-once guarantee — the alert queue `.alerts.json`, which
  must stay single because the one-lock mutual exclusion the whole tick relies on is asserted
  directly (`tests/test_hermes_delivery.py:436`), plus the ledger, the cancel tombstones and
  `.job_names.json`, all already keyed by job id.
- **The early-return is evaluated once, over the union of roots.** A `return` inside a per-root loop
  is precisely the silent-loss bug this work exists to prevent. It gets its own test rather than a
  comment.

One consequence worth naming:

- **Ownership.** A coding-profile job has no entry in `job_owners.json`, which the pipe writes
  only for jobs it creates on the primary gateway. An unowned job falls back to the shared
  background-tasks webhook, which `HERMES_AGENT.md` already says *should be restricted to
  admins*. Since the coding row is admin-only, that fallback is coherent and no stamping work is
  needed — but it is a property to verify, not assume: if the shared channel is ever opened to
  non-admins, coding-job results would leak into it.

The GPU story also simplifies. Where the design assumed two gateways each running `gpuguard` and
racing on the same probe, one gateway process now ticks every profile home, so there is one
scheduler and one gate — and because that gate is box-wide (it probes Ollama and ComfyUI, not a
profile), it is also the correct scope. The narrow concurrent-dispatch risk is gone rather than
merely reduced.


## 7. Sequencing

### Step 1 — upgrade hermes v0.19.0 (b6729ba9) → v0.21.4 (v2026.9.21)

That upgrade-first sequencing has now earned itself: the profile/port machinery this design was
drafted against (rows 9-10) **does not exist at the target** (row 13), and the upgrade carries a
hard break that fails silently (row 20). Both are absorbed here rather than discovered later.

1. **Snapshot.** Copy `~/.hermes/{config.yaml,.env,cron/jobs.json,alert_transports.env}` and record
   `git rev-parse HEAD`, so rollback restores measured state rather than a remembered one. Record
   the config's **hash**, not just a copy: row 21 says the migration will rewrite it, and the diff
   is the only way to know exactly what moved. The checkout is a squashed import pinned by tag
   (`main` is ahead 1 / behind 1 against `origin/main`), so there are no local edits to preserve —
   but that also means the tag checkout is the whole change.
2. **Stop** `hermes-gateway` and `hermes-delivery.timer` for the window, so no tick runs against a
   half-upgraded checkout.
3. **Fix the local cron provider before restarting anything.** `hermes/plugins/gpuguard/__init__.py`
   overrides `InProcessCronScheduler.start`, which gained `profile_gate`, `profile_adapters` and
   `default_profile` at the target. The current signature forwards a fixed list, so every ticker
   spawn raises `TypeError`, the supervised thread is respawned in a loop, and **no job ever fires
   again while the API keeps serving and every pipe-side check keeps passing** (row 20). Fix it by
   accepting and forwarding `**kwargs` rather than naming the three new parameters, so the next
   parameter upstream adds does not repeat this. This is the single highest-risk item in the upgrade
   and it is why the ordering below puts "a real job fires" before anything cosmetic.
4. **Upgrade** in `~/.hermes/hermes-agent`: `git fetch --tags`, checkout the `v2026.9.21` tag,
   re-sync the venv. `hermes update` is avoided deliberately — it can run the multiplex migration
   and the config ladder as side effects, and the pinning has been manual all along.
5. **Migrate config**, then diff against the snapshot. Expect the whole ladder to apply: the live
   config has no `_config_version` key, which coerces to 0, so every step up to 45 runs (row 21) —
   `platform_toolsets.*` gain `connections`, `cron.model_drift_guard` and
   `gateway.multiplex_profile_allowlist` are removed, and several unrelated defaults move.
   `cron.provider` and `approvals` are untouched. A migration that renames a key this stack depends
   on is silent breakage, which is what the diff is for.
6. **Re-verify the pinned contracts** — the six the pipe names by hand. All six were checked against
   the tag and are intact (`_MAX_PROMPT_LENGTH` 5000, the PATCH whitelist, the SSE chunk and
   `hermes.tool.progress` shapes, the `/api/jobs` envelope, `cron.provider` loading, and the
   `can_dispatch` seam itself), so these are confirmations on the live box rather than expected
   fixes. Two behaviour changes to expect: the omitted-`deliver` default now resolves to `"local"`,
   which makes the pipe's deliver-repair **dead code and its `repaired` counter read 0** — retire
   both deliberately rather than leave a metric that lies — and a dangerous command on an api_server
   session is now denied instantly instead of blocking for the 300 s approval timeout.
7. **Run the repo's hermes suites**, then the checks that actually matter, in this order: `hermes
   cron status` shows a live ticker heartbeat, **a real job fires** and its `next_run_at` advances,
   one `/p/coding/v1/chat/completions` round trip, and one alert through to the ledger. Check
   `platform_toolsets.api_server` still contains `cronjob` and `.cron` still contains `terminal`.
8. Re-link `gpuguard` into the profile's plugin dir, restart both units.

**Rollback:** `git checkout b6729ba9`, re-sync, restore the config snapshot, restart. Verify with
the same suites, not by inspection — step 3's failure mode is precisely one that inspection misses.

### Step 2 — profile, plugin and pipe

Build after the upgrade, against the API that will actually serve it.


## 8. Testing

- `tests/test_coding_task_plugin.py` — argv construction (asserting no shell, that the task string
  is passed through as data even when it contains shell metacharacters, and that the binary is
  invoked with `--cloud`), the `repo` allowlist including traversal attempts, the `cwd` handoff, a
  missing binary, non-zero exit, timeout, and the output cap.
- `tests/test_hermes_coding_pipe.py` — a non-admin is refused, routing targets
  `:8642/p/coding/v1/chat/completions` with the profile's own key, only the new turn is sent under
  `X-Hermes-Session-Id`, error surfaces (missing key, unreachable, HTTP error), and SSE parsing.
- `tests/test_hermes_delivery.py` — extended for a second hermes root, with the early-return
  evaluated over the union. Two things it must also do that it currently does not:
  - **The suite is not side-effect-free today, and that is a bug to fix in this pass.** It rewrites
    live files: `job_facts()` writes the real `~/.hermes/cron/output/.job_names.json` during the
    early `main()` calls, and every non-dry-run tick calls `publish_profile()`, which rewrites the
    live `alerts/profile.json` the Assistant pipe reads to describe alert delivery. A test run
    currently leaves test-shaped artifacts in live config. The fix is to isolate every root-derived
    global before the first `main()` call, and to assert no live path is written.
  - Its own count is **100 checks, not the 70 `HERMES_AGENT.md:16` claims** — doc drift to correct
    rather than repeat.
- `tests/test_deployed.py` — drift coverage for the new pipe, via the `PIPES` map, plus the README
  roster row that the same test requires.

The strongest check is also the cheapest, and it asserts the isolation is **real** rather than
intended, which matters now that both surfaces share one port:

- `GET /p/coding/v1/toolsets` **lists `coding_task` and does not list `terminal`** — the coding
  profile's toolset is what it claims to be.
- The same request with the **primary** key, and `/v1/toolsets` with the **coding** key, are both
  **401** — the rows 14 boundary holds in practice, not just in the source.


## 9. Open items

All five items this section originally listed are now **closed**, four of them by reading the tag's
source rather than v0.19.0's:

1. **Approval gate for a plugin tool** — closed (row 17). It is reachable only from a plugin's own
   `pre_tool_call` hook, so this plugin never enters it. The originally proposed mitigation
   (`approvals.mode: off`) would in fact **not** have worked on v0.19.0, where only yolo bypassed
   the gate; it would have worked at the tag. Neither is needed.
2. **Does the `coding` profile need cron disabled?** — resolved: it needs nothing done to it. A
   profile gets its own cron store by construction, and no config key disables the ticker — a
   profile's cron is disabled only by not running a process that ticks its home. Under the target
   topology one gateway process ticks **every** profile home, so scheduling works with no extra
   unit, which is part of why §4.1 has no second gateway.
3. **`is_async`** — resolved: `False`. Sync handlers run inline on the dispatching thread and the
   api_server platform runs the conversation in a thread executor, so a minutes-long subprocess is
   safe. `is_async=True` would in fact require the handler to *return a coroutine*, so the
   originally-considered value would have crashed at first call.
4. **`X-Hermes-Session-Id` append or replace** — resolved: **replace**. The server discards posted
   history and rebuilds from its own transcript plus the last body message, so the pipe sends only
   the new turn.
5. **What the upgrade changes** — enumerated (rows 20 and 21), including the silent cron break that
   would otherwise have been found in production.

Still genuinely open, and each is a build-time observation rather than something reading can settle:

- **Whether OpenWebUI preserves the pipe's custom SSE `event:` names** (§4.3). If it strips them,
  tool-progress signalling has to move to the session-stream endpoint.
- **Whether the config ladder's `connections` addition** measurably changes the local MoE's
  behaviour (§7 step 5). A larger tool surface is more prompt weight and more room to go off-script;
  worth watching after the upgrade rather than assuming either way.
- **`/v1/responses` is not profile-isolated** (row 22). This design avoids the surface entirely, so
  it is a recorded boundary rather than a blocker — but it is the kind of thing to re-read before
  anything else is built on `/p/…`.


## 10. Rejected alternatives

- **Enable `terminal` on the shared `platform_toolsets.api_server`.** Fastest, and the chain is then
  proven end to end — but it hands a shell to every Task-mode user and reverses the documented
  decision at `pipes/auto_assistant.py:25-26`. Rejected on the explicit "only admins" requirement.
- **Route coding work only through the existing cron path**, which already has `terminal`.
  Zero new surface and reuses tested machinery, but it is not a conversation: fire-and-forget, with
  results landing in a channel. The scheduling half of this design keeps the *capability*; the row
  is what makes it a conversation.
- **Unhide `hermes-genesis:agent` and grant admins.** That is the raw Ollama tag — no agent runtime,
  no tools, no sessions. It is not the agent, and it would not have delivered the request.

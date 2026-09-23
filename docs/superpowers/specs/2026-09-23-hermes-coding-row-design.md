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

## 3. The problem this design actually solves

The naive build — add `terminal` to `platform_toolsets.api_server` so the agent can shell out to
`deepseek` — was rejected, because `platform_toolsets.api_server` is a **single shared key** and the
same surface serves the ordinary-user Task path. Enabling it there hands a shell on the host to
every Task-mode user, admin or not, and reverses the decision recorded at
`pipes/auto_assistant.py:25-26`: *"no terminal, no browser, no code execution on the chat-facing
surface. Chat-reachable text must not be able to shell out."*

The design therefore gives the agent a **bounded tool** instead of a shell, and puts it on a
**separate gateway** that only the admin row can reach.

## 4. Architecture

Four new pieces, one per concern.

### 4.1 Hermes profile `coding`

`hermes profile create coding` — own `HERMES_HOME`, own `config.yaml`, and its own gateway systemd
unit (the CLI registers that service itself; no unit is hand-written).

- Binds `API_SERVER_PORT=8643` with its own `platforms.api_server.extra.key`. The primary gateway
  keeps 8642 and is untouched.
- `platform_toolsets.api_server`:
  `[web, file, memory, session_search, todo, skills, coding_task]`
  **Absent by intent: `terminal`, `browser`, `shell_exec`, and any code-execution tool.**
- `platform_toolsets.cron`: `[web, file, memory, todo, skills, coding_task]`
  Deliberately also without `terminal`, so a scheduled coding job goes through the same bounded
  tool as an interactive one. Delivery on this profile is deterministic — jobs emit `LOG:` /
  `ALERT(…)` and infrastructure delivers — so the cron-side `terminal` that the primary gateway
  needs for `curl` is not needed here.
- `cronjob` remains enabled, per the request that coding work can be scheduled. See §6 for the
  delivery work this creates.
- `cron.provider: gpuguard`, with `hermes/plugins/gpuguard/` symlinked into the profile's plugin
  dir, so this second scheduler is GPU-gated like the first.

### 4.2 Plugin `hermes/plugins/coding_task/`

Registers exactly one tool. `gpuguard` is **not** a template to copy: it is loaded as a *cron
provider* by name, not through the plugin scanner, and it has no manifest. A tool-registering
plugin needs both a `plugin.yaml` manifest **and** a `register(ctx)` entry point
(`hermes_cli/plugins.py:19`).

```
coding_task(task: str, repo: str = null) -> str
```

Implementation rules, each of which is a test:

- **List argv, never a shell.** `subprocess.run(["deepseek", "--model", MODEL, "-p", task], …)`.
  The task string is never interpolated into a shell, so no quoting bug can become a command.
- **Pinned working directory.** `repo`, when given, must `realpath` to inside an allowlisted root
  (`CODING_TASK_ROOTS`). Anything else is refused, so `../../` cannot escape.
- **Explicit environment, not the gateway's.** The gateway's env comes from a systemd unit that
  deliberately exports almost nothing (`PATH`, `VIRTUAL_ENV`, `HERMES_HOME`). The tool builds a
  known env instead. Verified safe: `deepseek` resolves its own credentials from
  `~/.config/deepseek/secrets.env`, so it does not depend on inheriting anything (§2 rows 4-5).
- **Pinned backend.** `--model` is explicitly the cloud model. `--local` is opt-in only, because
  it loads `qwen38-coder:q4-128k` at ~21.8 GB, which **cannot co-reside** with the 18.3 GB chat
  tenant and costs an eviction per task.
- **Own timeout, output cap, and honest failure.** Default timeout 900 s, overridable by env.
  Default output cap 32 KiB per stream, so a runaway build log cannot fill the agent's context.
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
drift-test (`tests/test_deployed.py`) and `--rollback` machinery. It points at `:8643`, not `:8642`.

- Multi-turn continuity via `X-Hermes-Session-Id`, keyed to the OpenWebUI chat id, so a
  conversation is a conversation rather than a sequence of unrelated one-shots.
- **Its own HTTP timeout**, parameterised — not the shared `HERMES_TIMEOUT_S = 300`. Raising that
  globally would loosen hang detection for the Task path for no reason.
- Relays hermes's inline tool-progress markers, so a multi-minute coding task shows signs of life
  rather than a silent wait.

### 4.4 Docs

`docs/HERMES_AGENT.md`, `docs/MODELS.md`, `docs/openwebui-config-snapshot.md`, and a correction to
the stale credential claim in `docs/CLAUDE_CODE.md:56`.

## 5. Security model

### 5.1 Two independent admin gates

| Layer | Mechanism | Defeats |
|---|---|---|
| OpenWebUI | The function's access control. 0.11.3 is deny-by-default, so the row is invisible unless a grant exists — we simply never create one for non-admins. | The UI |
| The pipe | A role check on `__user__` — `role == "admin"`, or handle in `TASK_ADMINS` (`pipes/auto_assistant.py:317`). | A direct API call that bypasses the UI |

Either alone is defeatable; both together are not, for the two paths that exist.

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

- No general shell is enabled on any chat-facing surface, on either gateway.
- Cron's dangerous-command mode stays `deny` — it is a separate config key (§2 row 11).
- The primary gateway's config, toolset, jobs and scheduler are untouched.

## 6. The delivery gap, and its fix

This is the largest piece of new integration work, and it is a direct consequence of the
"coding work can be scheduled" requirement.

`scripts/hermes_delivery.py` hardcodes `OUT_DIR = ~/.hermes/cron/output` (`:45`) and
`JOBS_FILE = ~/.hermes/cron/jobs.json` (`:164`). A job created on the `coding` profile writes to
that profile's own `HERMES_HOME` instead. Left alone, such a job would **run, satisfy its
condition, and deliver nothing** — the silent-loss shape this stack has now been bitten by
repeatedly (SMS dropping links; alerts to a domain with no MX record).

Fix: make the delivery watcher take a list of hermes roots rather than one hardcoded path, and scan
each on the same tick. The existing LOG/ALERT parsing, retry queue, ledger and cancel tombstones
then apply unchanged, because they are already keyed per job id.

Two consequences worth naming:

- **Ownership.** A coding-profile job has no entry in `job_owners.json`, which the pipe writes
  only for jobs it creates on the primary gateway. An unowned job falls back to the shared
  background-tasks webhook, which `HERMES_AGENT.md` already says *should be restricted to
  admins*. Since the coding row is admin-only, that fallback is coherent and no stamping work is
  needed — but it is a property to verify, not assume: if the shared channel is ever opened to
  non-admins, coding-job results would leak into it.
- **Two schedulers, one GPU.** Both gateways get `gpuguard`, and both probe the same Ollama and
  ComfyUI, so they see the same residency. They do not coordinate with each other, so two ticks in
  the same window could dispatch concurrently. This is a narrow risk that the shared probe
  materially reduces, and it is recorded rather than solved.

## 7. Sequencing

### Step 1 — upgrade hermes v0.19.0 (b6729ba9) → v0.21.4 (v2026.9.21)

First, per §2 rows 9-10: the plugin API and the profile/port machinery this design rests on were
read in **v0.19.0 source**, and upstream merged ~660 PRs across the two minor versions. Building
against the version being left is how the work is earned twice.

1. **Snapshot.** Copy `~/.hermes/{config.yaml,.env,cron/jobs.json,alert_transports.env}` and record
   `git rev-parse HEAD`, so rollback restores measured state rather than a remembered one.
2. **Stop** `hermes-gateway` and `hermes-delivery.timer` for the window, so no tick runs against a
   half-upgraded checkout.
3. **Upgrade** in `~/.hermes/hermes-agent`: `git fetch --tags`, checkout the `v2026.9.21` tag,
   re-sync the venv. `hermes update` is avoided deliberately — the existing doc says not to run it
   casually and the pinning has been manual all along.
4. **Migrate config**, then diff the result against the snapshot. `platform_toolsets`, `cron.provider`
   and `approvals` are the keys this stack depends on; a migration that renames one is silent breakage.
5. **Re-verify the pinned contracts.** These are the internals the pipe names by hand and they are
   the likeliest casualties: the omitted-`deliver` default (`tools/cronjob_tools.py:316`),
   `api_server._MAX_PROMPT_LENGTH`, the job `PATCH` whitelist, the SSE chunk and tool-progress
   marker shape, the `/api/jobs` response shape, and the `InProcessCronScheduler.can_dispatch` seam
   `gpuguard` subclasses.
6. **Run the repo's hermes suites**, then one bounded job end-to-end, one `/v1/chat/completions`
   round trip, and one alert through to the ledger.
7. Re-link `gpuguard` if the profile/plugin layout changed, restart both units.

**Rollback:** `git checkout b6729ba9`, re-sync, restore the config snapshot, restart. Verify with
the same suites, not by inspection.

### Step 2 — profile, plugin and pipe

Build after the upgrade, against the API that will actually serve it.

## 8. Testing

- `tests/test_coding_task_plugin.py` — argv construction (asserting no shell and that a task string
  containing shell metacharacters is passed through as data), the `repo` allowlist including
  traversal attempts, the timeout path, the output cap, non-zero exit, and a missing binary.
- `tests/test_hermes_coding_pipe.py` — a non-admin is refused, routing targets `:8643`, error
  surfaces (missing key, unreachable, HTTP error), and SSE parsing.
- `tests/test_hermes_delivery.py` — extended for a second hermes root, keeping the existing
  70 checks green.
- `tests/test_deployed.py` — drift coverage for the new pipe, via the `PIPES` map.

The strongest check is also the cheapest, and it asserts the isolation is **real** rather than
intended: `GET :8643/v1/toolsets` must list `coding_task` and must **not** list `terminal`.

## 9. Open items to settle at build time

Recorded honestly as unresolved, each with the reason it cannot be settled by reading v0.19.0:

1. **Is a plugin-registered tool subject to the approval gate?** `tools/approval.py` intercepts
   dangerous *shell* commands. Whether a first-class registered tool passes through the same gate
   is unknown. If it does, the `coding` profile sets its own `approvals.mode` — safe, because
   `approvals.cron_mode` is a separate key.
2. **Does the `coding` profile need `cron` explicitly disabled?** It should have its own scheduler
   (§4.1); whether the profile machinery enables one by default is unverified.
3. **Is `is_async=True` required** for a tool that blocks for minutes, or does the registry run
   sync handlers off the loop? Getting this wrong would stall the agent.
4. **Does `X-Hermes-Session-Id` append to a persisted session, or expect only the new turn?**
   Sending the full history under a session id could duplicate context. Must be read in the
   api_server source before the pipe is written.
5. Anything the v0.21.4 upgrade changes, which by construction cannot be enumerated yet.

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

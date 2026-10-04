# OhmzAI Code Agent — design

_2026-09-29. Pressing **Code** in the pinned admin's chat hands the turn to a host bridge that runs
one bounded, headless Claude Code turn per message on an allowlisted workspace, against the existing
deepseek relay, resuming the same session every turn. The backend (DeepSeek API or the local coder)
is chosen only by a pure function of the user's words. The route is direct, not through Hermes.
Supersedes the interactive half of the 2026-09-23 Hermes coding row._

## 1. What was asked

1. **Web stays the default mode.** Nothing about Internet-by-default changes.
2. **Pressing Code uses the local `deepseek` harness CLI** — Claude Code on this machine, driven
   through the deepseek relay — instead of today's single-shot coder chat.
3. **Decide Hermes versus direct.** Decided: direct (§12, §16).
4. **Continuous back-and-forth.** A follow-up ("now make it handle tabs") continues the same agent
   session with its history and its edits, not a fresh run.
5. **Deterministic backend choice.** "use local model" and "use API model" switch the backend by
   rule, never by a model's guess.
6. **Admin-only, an agent on a workspace.** It reads, edits and runs code in a real project folder.

The owner answered every open question on 2026-09-29. They are decisions, not options:

| Question | Decision | Where |
|---|---|---|
| Default backend for a new Code chat | **API** | §6 |
| Leaving local for API | **Keep the full history (resume) after an explicit click-confirm**; `/api fresh` starts clean with a handoff note | §5, §6 |
| Workspaces | **Git repos directly under `~/StudioProjects` and `~/src`, plus a per-chat scratch dir; `~/ai-stack` excluded in v1** | §7 |
| Package installs | **None in v1.** Use what is installed; network fully denied | §7, §10 |
| Hours for local | **No hour limit.** Local is allowed any time the GPU has been idle of other big models for 10 minutes | §9 |
| Coder keep-alive after a local turn | **Ollama's default, 60 s.** The bridge sends no `keep_alive` | §9 |
| Launcher `CLAUDE_CODE_ENTRYPOINT` bug | **Fixed 2026-09-29**; the owner was told to rotate the claude.ai session | §10.2 |
| The Hermes `coding` profile and `coding_task` | **Kept, but `coding_task` is unwired in phase 0.** Not deleted | §12 |
| Accounts | **The admin account only, pinned by UUID.** Not the role=user gmail account | §10.3 |

## 2. What was verified before designing

Everything in these tables was measured or read on this box, not assumed: rows 1-43 on 2026-09-29
by the research pass, rows 44-50 on 2026-09-30 by the review pass. Line numbers in *this*
document's prose were refreshed against the working tree on 2026-09-30 (branch `personal-pipeline`,
with uncommitted edits). They are **advisory**: the working tree keeps moving, so every edit point
is also named by a symbol or a neighbouring statement, and the symbol wins when the two disagree.
Rows cite the evidence the research recorded.

**Claude Code 2.1.285 (headless)**

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 1 | The CLI has the flags a per-turn bridge needs | `claude --version`, `claude --help`; SDK `subprocess_cli.py:611` | 2.1.285 has `-p`, `--output-format stream-json` (with `--verbose`), `--session-id`, `--resume`, `--model`, `--permission-mode` (incl. `dontAsk`), `--tools`, `--settings`, `--strict-mcp-config`, `--restricted`, `--permission-prompts none`. **`--max-turns` is hidden**: absent from `--help`, passed by the SDK. |
| 2 | Continuity comes from resuming the transcript | Probe t1/t2 (session `1ea7f626…`), then a stub replay of `--resume` | Turn 1 `--session-id` 8.7 s; turn 2 `--resume` 1.68 s and recalled PAPAYA. The resumed request carried 10 messages with PAPAYA in them and not in the system prompt. Process start to `init` is 0.34-0.43 s, so a spawn per turn is cheap. |
| 3 | Session-id reuse and cwd | Stub runs; a resume from `probe/othercwd` | Reusing an id with `--session-id` exits 1 before any request (`already in use`). `--resume` is **not** limited to the original cwd: it resolves from elsewhere and injects `# Environment update - Primary working directory: X (was Y)`. |
| 4 | The stream-json contract | `probe/summ.py` over t1-t3 | `system/init` (session_id, cwd, model, permissionMode, tools…); one `assistant` event per content block; `user` events with tool_result; `system/api_retry`; `system/status` `compacting`; a final `result` (subtype, is_error, api_error_status, usage, …). |
| 5 | The failure shape lies about success | `probe/turn1.jsonl` | A 401 is retried 10 times (delays up to 33.9 s), 196.7 s in all, and the result says **subtype `success` with `is_error: true`**, `api_error_status: 401`. |
| 6 | Cost fields mean nothing on this relay | Three-turn probe | `total_cost_usd` is cumulative, `costBasis: unknown`; $0.17 for a one-word turn. |
| 7 | A smaller window compacts **before** the user's turn | Stub, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=30000`, `--resume` | `system/status compacting` on the new model first; if compaction fails, `Prompt is too long · automatic compaction failed`. `--autocompact` accepts only 100k-1M, above the local window. |
| 8 | A cloud history can be resumed on the local tag | Stub capture of `--resume --model qwen38-coder:q4-128k`, then `router.normalize_messages` offline | Thinking blocks stripped, a model note appended; the router hoists system messages and disables thinking (estimate 21,956 tokens). **Ollama itself was not called.** |
| 9 | Headless inherits the user's setup; `CLAUDE_CONFIG_DIR` isolates it | `init` under the default vs an isolated config dir | Default: 4 plugins, a SessionStart hook, git-guardrails, `settings.local.json` allows, auto-memory (told to remember a word, the model chose to write `memory/papaya.md`). Isolated: none of it. With the Claude Desktop env, claude.ai connectors loaded 104 tools including Gmail send; scrubbed, 23 tools and no MCP. |
| 10 | A permission mode is not a sandbox, and `auto` is the default | `t3.jsonl`; `~/.claude/settings.json` | `dontAsk` with `--allowedTools Read` still ran `Bash pwd`. With no flag the mode is `auto`, whose classifier the docs list as alias-slot traffic (`gemma3:1b` in a local session; which slot exactly was not verified) — never Claude. |
| 11 | The launcher's contract | `harness/deepseek/deepseek` | Reads only `$1` for `--local`/`--cloud`. Local: window 98304 (131072 − 32768), `API_FORCE_IDLE_TIMEOUT=0`. Cloud: every slot `deepseek-flash[1m]`. Env is fixed at launch, so a backend change is a respawn. |
| 12 | The relay serves sessions in parallel | Throwaway router, upstream sleeping 2 s | 3 concurrent POSTs finished in 2.00 s each. |
| 13 | A long-lived per-chat process also works | `probe/persist.py` | `--input-format stream-json` stays alive between results and exits 0 ~0.3 s after stdin closes. |
| 14 | The Agent SDK | `pip show`; the unpacked wheel | Not installed; the wheel is 102 MB and bundles its own CLI 2.1.285; it sets `CLAUDE_CODE_ENTRYPOINT=sdk-py`; its one gain over the CLI is `can_use_tool`. |

**OpenWebUI 0.11.3**

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 15 | Code is one client-supplied boolean | `compose/openwebui/fork/gen/01_mode_buttons.py:81-88`; `Chat.svelte` `getFeatures()` | `setMode('code')` sets `codeInterpreterEnabled` → `features.code_interpreter`. No filter, no stamp. Every user may set it (`user.permissions.features.code_interpreter=true`). |
| 16 | OWUI acts on that flag server side | `middleware.py:2691-2708`, `:4631-4644`, `:6045-6214`; live DB | It appends the ~2.2 kB Code Interpreter/Pyodide prompt to the user message, and when the pipe's output contains `<code_interpreter>` it runs it in the browser and calls the pipe again, up to 5 times. |
| 17 | Today's Code route is not an agent | `pipes/auto_assistant.py` coder branch, `CODER_CTX`, `coder_model` | Single-shot `qwen38-coder:q4` at 32768 ctx under the GPU lock; no tools, workspace or session; skipped whenever the chat holds an image; "create a video player component in React" routes to video. |
| 18 | Web is already the default mode | Live DB `meta.defaultFeatureIds=['web_search']`; `Chat.svelte` `setDefaults` | New chats start on Internet and each chat remembers its own mode in `chat.chat_mode`. **Nothing has to change to keep web the default.** |
| 19 | A turn is a background task | `main.py:1814-1893`; `tasks.py:131-262` | No timeout between OWUI and a pipe generator; status events are persisted; **Stop cancels the task** (CancelledError in the pipe); **closing the tab does not**. |
| 20 | OWUI's own tasks come through the pipe | `auto_assistant.py:7828` (the `if __task__ or …` guard); `media_session.answer_task` | Title, tag and follow-up tasks hit the `__task__` guard and are answered on `gemma3:1b`. |
| 21 | The pipe cannot run Claude Code itself | `docker exec open-webui` | Root in a container with no `claude`, `deepseek` or `node`, one mount (the config volume), host loopback reachable (`:8788` → 200). **A host-side process is required.** |
| 22 | Admin gating has to be in the pipe, and `TASK_ADMINS` is unsafe | Live DB; `auto_assistant.py:320` (`TASK_ADMINS =`) | Every user shares the one Assistant row. `TASK_ADMINS` (email local parts) matches the owner's **role=user** gmail account and **not** the admin account. |

**Host, GPU and relay**

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 23 | The GPU lock is one file inside and outside the container | `stat` on host vs container; re-`stat` 2026-09-30 | `/volume1/docker/openwebui/config/.gpu.lock`, same dev/inode. Changed on 2026-09-29 23:34 from 0666 `root:root` to **0660 `root:ohmz`**; host `ohmz` flocks it through the group. |
| 24 | Only part of the GPU work takes that lock | grep; function table | The coder route and the four `auto_assistant` media pipelines do. Plain chat, `image_krea`, `photoreal`, `animate_scail`, Adaptive Memory, title helpers and hermes cron do not. |
| 25 | Ollama's configuration | `systemctl cat ollama`; journal | 0.34.1; `NUM_PARALLEL 1`, `MAX_LOADED_MODELS 3`, **`KEEP_ALIVE 60s`**, `CONTEXT_LENGTH 32768`. The router injects no `keep_alive`. |
| 26 | VRAM and swap costs | `docs/MODELS.md`; `nvidia-smi` | apex-compact 18285 MiB; 32k coder 16881 MiB; **128k coder 21857 MiB resident**; gemma3:1b 1313 MiB; hermes-genesis:agent ~17 GB (not measured). Chat cold reload 22.7 s. The 128k coder cannot share the card with the chat tenant, the agent tag or the 32k coder; beside gemma3:1b it would sit at ~23.9 GB of a ~24.1 GB ceiling (reasoned, not measured). |
| 27 | gpuguard's rules | `hermes/plugins/gpuguard/__init__.py:80-110`, `:156-233` | Defers cron on ComfyUI activity or an Ollama model ≥ 10 GiB / `hermes-genesis*`; dispatches anyway after 900 s blocked on Ollama alone, releases any block at 3600 s; **never looks at `.gpu.lock`**. One daily job exists. |
| 28 | The relay is unsupervised | `ss`, `ps`, cgroup; `router.py` | pid 38124 inside a GNOME Terminal scope; no `deepseek-router.service` installed; `UPSTREAM_TIMEOUT` 900 s. |
| 29 | Per-turn systemd units are available | `cgroup.controllers`; `loginctl` | cgroup v2 with cpu, memory and pids delegated to `user@1000`; linger on; systemd 255; 94 GB RAM (~30 GB free), 24 cores. |
| 30 | The service and alerting pattern | `~/.config/systemd/user`; working-tree `scripts/stack_watchdog.py` and `scripts/health_alert.py`, re-read 2026-09-30 | `cancel-service`: `Restart=always` plus an `onfailure.conf` → `stack-alert@`. None of the live units is in the repo. The watchdog **confirms a failure over 2 runs, sends a daily REMINDER while an alerted check stays down, and routes by `SEVERITY`** (`health_alert.py`). A check is registered in `CHECKS` (label, where), in `_checkers()`, and optionally in `SEVERITY`. There is no check for `:8788`. |
| 31 | Candidate workspaces | git-dir scan; `df`; re-scanned 2026-09-30 | Directly under the roots: 30 git checkouts (29 in `~/StudioProjects`, 1 in `~/src`). Two of them are worktrees whose `.git` is a file (row 47), so **28 are eligible** (27 + 1). `/`, `/home` and `/volume1` are one filesystem, 153 GB free. |

**Security**

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 32 | `ohmz` is root-equivalent | `id`; `sudo -n -l` | `(ALL) NOPASSWD: ALL` and the `docker` group. "As the user" means root: every container, the tunnel credentials, all of `/volume1`. |
| 33 | Who can reach the private instance | `webui.db`; config rows | 1 admin, 3 users; signup open (lands `pending`); admin JWT valid 4 weeks; no MFA. |
| 34 | Loopback is shared, the volume is not | `docker inspect` | Nine host-network containers reach any `127.0.0.1` port. Only `open-webui` mounts the config volume. The public instance reaches neither. |
| 35 | Claude Code's bwrap sandbox works here | Stub-driven Bash probe | Blocked sudo, the docker socket, loopback, internet egress and `$HOME` writes. **Did not block secret reads** until `filesystem.denyRead`; sandboxed Bash saw `ANTHROPIC_AUTH_TOKEN` until `credentials.envVars` denied it. |
| 36 | `dontAsk` needs same-file allow rules | Probe | Bash, Edit and Write each need an allow rule. A `--settings` file that fails validation is **silently ignored** in `-p`, so allow rules co-located with the sandbox block fail closed. |
| 37 | `--restricted` is mandatory | Workspace `.claude/settings.json` SessionStart hook | Without it the hook ran `sudo -n true` → 0. With it, no hook ran. |
| 38 | What the sandbox lets Bash write | Probe | `.git/hooks`, `.git/config`, `.claude/settings.json`, `.vscode` are write-protected; sources and `.envrc` are writable; `/tmp/claude-1000` is shared by all of uid 1000's sessions. |
| 39 | A hardened user unit contains the whole tree | `systemd-run --user -p NoNewPrivileges=yes -p InaccessiblePaths=/run/docker.sock -p ProtectHome=read-only -p ReadWritePaths=<ws>` | sudo=1, docker=1, `~/.cache` write=1, workspace write=0, and nested bwrap still works. |
| 40 | `~/ai-stack` is live control-plane code | Symlink map | `~/.local/bin/deepseek`, `~/.config/deepseek/router.{py,json}`, the git-guardrails hook and the hermes plugins are symlinks into it. |
| 41 | The Hermes coding path is live, with weaker bounds | `~/.hermes/profiles/coding/config.yaml`; `hermes/plugins/coding_task/__init__.py:134` | `coding_task` is in the `api_server` **and** `cron` toolsets; each call is a fresh `deepseek --cloud -p` with no `--resume`, no `CLAUDE_CONFIG_DIR`, no `--restricted`, no sandbox, cwd defaulting to `/home/ohmz`. Through Hermes with a local coder a turn costs 2-3 model swaps; any turn has a 360 s / 32 KiB cap, and nothing streams. |
| 42 | The 2026-09-29 token leak | Header-capture stub; env bisect; `~/.cache/deepseek-router.log` | §10.2. |
| 43 | `request.client.host` is forgeable | `start.sh:75` (`--forwarded-allow-ips *`) | Not an origin signal. |

**Re-checked 2026-09-30 (review pass)**

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 44 | The `[1m]` suffix never reaches the wire | Probe run with `--model deepseek-flash[1m]` and every slot `[1m]`; captured bodies `cap_c.jsonl.body.1/.2`, `cap_b2.jsonl.body.1/.2` | Every request body carries `"model": "deepseek-flash"`, on path `/v1/messages?beta=true`, with a `tools` array and `max_tokens` 32000. Claude Code strips a trailing `[...]` before sending. |
| 45 | The first `toolchains_ro` list does not fit this host | `ls`, `find` over each candidate | `~/.local/bin` holds a file named `keyring` and `claude` is a symlink into `~/.local/share/claude/versions/2.1.285`; `~/.nvm` has 10 secret-shaped names (npm's bundled `.npmrc` ×5, google-auth-library `credentials*.js`, undici-types `cookies*.d.ts`), `~/.pub-cache` 10 (`cookies*.dart`), the flutter SDK two `*.pem`. `~/flutter` does not exist; the SDK is `~/.local/flutter`. `pytest` is `~/.local/bin/pytest`, its package in `~/.local/lib/python3.12/site-packages`; uv interpreters are in `~/.local/share/uv/python`. |
| 46 | The pipe keeps moving | `auto_assistant.py` modified 2026-09-29 23:41, after the research | Line numbers refreshed in this document; edit points also anchored on symbols. |
| 47 | Two "repos" are worktrees | `[ -f <repo>/.git ]` | `StudioProjects/Tday-list-rows` → `StudioProjects/Tday/.git/worktrees/…`; `StudioProjects/homarr-upstream-v1.59.1` → `StudioProjects/homarr/.git/worktrees/…`. Inside a turn unit that gitdir is not bound. |
| 48 | The unit sandbox primitives coexist in one transient user unit | `systemd-run --user --wait --pipe --collect` with `NoNewPrivileges`, `TemporaryFileSystem=/home/ohmz:ro`, a `BindReadOnlyPaths` and a `BindPaths`, `InaccessiblePaths` on the docker socket, `PrivateTmp`, `PrivateNetwork` | `ls -A /home/ohmz` shows only the bound entry; only `lo` exists; host `127.0.0.1:8788` is refused and binding `127.0.0.1:8788` inside succeeds; a docker-socket connect is `PermissionError` (and succeeds without `InaccessiblePaths`); the rw bind is writable; nested `bwrap --ro-bind / / --unshare-net` works. **Not** run with the real claude binary: that stays with the canary and R2a. |
| 49 | Reading an ancestor's environ depends on yama | `/proc/sys/kernel/yama/ptrace_scope`; a descendant reading its ancestor's `/proc/<pid>/environ` | `ptrace_scope` = 1, under which the read is `PermissionError`. `ohmz` can change it (row 32), so a token in claude's own environment is protected only by a mutable sysctl. §4.8 therefore keeps the real token out of the unit entirely. |
| 50 | Cited precedent for a self-test table | `grep` for `def check()` | `pipes/shared/notebook_resolver.py` has no `check()`/`main()` table and no module defines one. §4.4 no longer cites it. |

**Not in these tables, and where each is settled instead:**

- *Reported by the design revision but not in the verified set:* the exact environment of a
  transient unit; that `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` are honoured by
  2.1.285; the sandbox primitives of row 48 around a **real** claude turn. → canary (§4.10), R2a,
  M1, R1. `IPAddressDeny=` was reported to have no effect in user units; nothing here depends on it
  either way.
- *Reported by the adversarial review, not re-verified:* that the Assistant row runs Adaptive Memory
  on an Ollama model in inlet and outlet every turn; that Socket.IO's allowed origins derive from
  `CORS_ALLOW_ORIGIN`; that `OnFailure=` fires for `Restart=always` units on systemd 255; that
  `metadata.user_prompt` is captured after the Code Interpreter suffix. → R3's `/api/ps` trace, R5,
  `StartLimit*` plus the alert cooldown, and a fail-closed strip (§4.1) with a pinned-marker test.
- *Marked unverified by the research itself:* the cross-subdomain CSRF chain (its pieces are
  confirmed — Origin reflected with credentials, the `token` cookie accepted, `SameSite=lax` — the
  whole chain was not run) → phase 0 CORS and R5; the 20-minute contention narrative → R3, R4 and
  §10.5; OWUI not preventing concurrent turns on one chat → the bridge's per-chat lock, regardless.
- *Research unknowns the design depends on:* Ollama accepting a resumed cloud history, the 128k
  coder's cold load and throughput, early stdin EOF, `--restricted` versus `git commit`, test runs
  with read-only toolchains and no network → R2a and R3.

## 3. The problem this design actually solves

Today's Code button (rows 15-17) is a chat with a coding model, not an agent: one Ollama call on a
32k runner, no tools, no files, no memory of the last turn, and it silently loses the turn to the
video renderer when the request says "video player". The half-built Hermes coding row was meant to
fix that, and row 41 shows it cannot deliver what was asked: every call is a fresh Claude Code
session, so "now optimise it" re-reads the files and the continuity lives only in a second LLM's
summary; with a local coder each turn costs two to three swaps; nothing streams; and it is already live with bounds
weaker than anything proposed here, behind an LLM hop that has a web toolset.

The stakes were also understated. The 2026-09-23 spec called `coding_task` "arbitrary code
execution as the user". Row 32 makes that **root**. So the boundary cannot be the model's judgement,
a permission mode (row 10), or a list of secrets to hide: the adversarial review found credentials
no denylist had named — `~/.codex/auth.json`, browser cookie stores, `~/.local/share/keyrings`,
`~/.vnc/passwd`, service `.env` files — and in API mode anything the agent can read can be sent to
DeepSeek without any network access of its own.

What this design solves, then: **a continuous, streaming Claude Code session per chat, on an
allowlisted workspace, whose backend is a pure function of the user's words, where the OS decides
what a turn can reach and a gate in the bridge decides what can leave — so "local" cannot egress by
construction rather than by policy.**

Four hazards the review found in the first draft are designed out rather than patched: a grammar
that read "use the API to fetch prices" as a switch (§6); a lost store that silently resumed a local
session on DeepSeek (§5); per-turn model loads nobody had counted — Adaptive Memory and OWUI's own
title, tag and follow-up tasks — that would evict the local coder on every turn (§9); and a denylist
filesystem that left those credentials readable (§7).

## 4. Architecture

No fork change, one pipe branch, one filter guard, one host service, one committed unit.

```
browser ── Code pressed (features.code_interpreter) ──▶ OpenWebUI (container, root)
                                                         │  Adaptive Memory: skipped for Code-agent turns
                                                         ▼
                                   pipes/auto_assistant.py — Code-agent branch
                                                         │  POST /v1/turn, Bearer, over a Unix socket
                                                         ▼  /volume1/docker/openwebui/config/code-bridge/bridge.sock
                                   ohmzai-code-bridge.service  (host, user ohmz)
                                     grammar · records · locks · GPU arbiter · relay gate · canary · audit
                                                         │  systemd-run --user --pipe --wait   (one unit per turn)
                                                         ▼
                          ohmzai-code-<chat_id>-<seq>.service    tmpfs home · PrivateNetwork · NoNewPrivileges
                            turn_exec.py ── fork ─▶ forwarder 127.0.0.1:8788 (inside the netns)
                                         └─ execve ▶ claude -p   (bwrap sandbox around every Bash call)
                                                         │  <state>/run/<unit>/relay.sock (bind-mounted)
                                                         ▼
                                   relay gate (in the bridge): path + model allowlist per backend;
                                                         │  injects the real DeepSeek token (API turns)
                                                         ▼
                                   deepseek-router.service 127.0.0.1:8788
                                     deepseek*  ─▶ api.deepseek.com        everything else ─▶ Ollama :11434
```

`<state>` is `~/.local/state/ohmzai-code` throughout.

### 4.1 Pipe branch — `pipes/auto_assistant.py`

Gates the turn, forwards clean text, relays the confirm dialog, keeps a small stamp map of agent
chats, renders the stream, and answers OWUI's tasks for agent chats without a model. It holds **no
session state**: the stamp map is a display cache the bridge's `done` events refill, never an input
to the bridge.

- **Placement.** After the Notebook branch (the `nb_src = self._notebook_mode(__metadata__)` block,
  ending near `:7886`) and before the media scan (`kind, media = self._recent_media(msgs)`, `:7890`):
  below the `__task__` guard (the `if __task__ or …` statement, `:7828`) so OWUI's tasks never
  start a turn, and above media, background and coder routing so the turn is claimed (row 17). It
  applies to the `auto` entry, the only one `pipes()` exposes (`:644-645`).
- **Condition.** `code_btn` (`code_btn = self._code_mode(__metadata__)`, `:7821`) **and**
  `_code_agent_ok`, which requires all of: valve `CODE_AGENT` (default `False`);
  `__user__['role'] == 'admin'`; `__user__['id']` in valve `CODE_AGENT_USER_IDS` (UUIDs; the admin
  account only); and a real chat id read from `__metadata__['chat_id']` — not
  `temporary:`/`local:`, and never the md5 fallback `_chat_id` produces. The `_is_code_request`
  classifier never reaches the agent.
  - Fails role, pin or valve → today's routing, unchanged.
  - Pinned admin in a temporary chat → `Code agent needs a saved chat.` Nothing runs.
- **Attachments.** Only files on the **current** user message refuse the turn: an entry of
  `__metadata__['files']` whose id is also in the last user message's `files`, or an image part in
  the last user message → `Attachments aren't passed to the Code agent yet; paste the content.`
  Nothing runs. The other entries of `__metadata__['files']` are chat-level files (`chatFiles`);
  they never reach the agent anyway (only clean text is forwarded), so they do not block it, and
  the first agent turn in such a chat adds one line: `Chat-level files aren't visible to the Code
  agent.`
- **Forwarded text.** `metadata.user_prompt` → `_strip_injected_context` (`:703`), which cuts the
  `#### Code Interpreter` suffix OWUI appends in legacy function-calling mode and the
  `User Memories (` prefix block. This stripping is the **primary** guard (row 16), and it **fails
  closed**: if the stripped text still contains a pinned injection signature — the Code Interpreter
  prompt's opening heading or its `<code_interpreter` instruction line, copied verbatim from OWUI
  0.11.3, or the RAG envelope's `### Task:`…`</context>` shape — the turn is refused with `Code
  agent input format changed; paused pending review.` and an audit row, never forwarded. The
  signatures are pinned in suite 6, and `tests/test_deployed.py` asserts the running container's
  Code Interpreter prompt still begins with the pinned heading, so an OWUI upgrade that renames it
  fails a test instead of silently forwarding 2.2 kB of instructions as task text.
- **Message ids.** `user_msg_id = metadata.user_message.id`, `parent_id =
  metadata.user_message.parentId`, `assistant_msg_id = metadata.message_id`.
- **Stamp map.** `/app/backend/data/code_agent_chats.json` is a map `{chat_id: {ws, backend,
  title_words}}`. On the first agent turn, before the POST, the pipe adds the chat with
  `title_words` = the first 6 words of the clean text and `ws`/`backend` null; every `{t: done}`
  event then updates `ws` and `backend`. Every read-modify-write holds an `fcntl.flock` on the
  sidecar `code_agent_chats.json.lock` and ends in tmp + `os.replace`, the pattern the pipe already
  uses for `SUBSCRIBE_INBOX_FILE` (`:2779-2782`), because the pipe runs concurrent async tasks. The
  filter guard and the task guard read it.
- **Task-guard addition.** Inside the `__task__` guard, before `answer_task`: compute the chat id
  from `__metadata__` (the guard runs before `cid = self._chat_id(…)` is set at `:7846`); if it is
  in the stamp map, answer without a model from its entry, keyed on the task name
  (`media_session.py:44-45`): `title_generation` → `{"title": "<ws> · <title_words>"}`;
  `tags_generation` → `{"tags": ["code", "<ws>"]}`; `follow_up_generation` → `{"follow_ups":
  ["<opposite-backend phrase>", "/status"]}`; any other task → `""` (OWUI falls back to its
  defaults). A null `ws` renders as `scratch`. The opposite-backend phrase is `use API model` when
  the entry's `backend` is `local` and `use local model` otherwise.
- **Transport.** aiohttp `UnixConnector` to the bridge socket; `POST /v1/turn` with
  `Authorization: Bearer <key>` read from `/app/backend/data/code_bridge_key`;
  `ClientTimeout(total=None, sock_connect=5, sock_read=60)`; SSE parsed as `_nb_stream` (`:5963`)
  does; the response is closed in `finally`.
- **Confirm relay.** On `{t: confirm}`, call `asyncio.wait_for(__event_call__({"type":
  "confirmation", "data": {"title": …, "message": …}}), 120)` — OWUI's own `__event_call__` has no
  timeout (`WEBSOCKET_EVENT_CALLER_TIMEOUT` unset), and 120 s is the nonce's life. `True` → re-POST
  the same body with `confirm=<nonce>`. The other outcomes send nothing and reply:
  - `False` → `Still on local; nothing was sent.`
  - timed out → `Still on local; nothing was sent. Say "use API model" again to switch.`
  - no `__event_call__` → `Still on local; nothing was sent. To switch without the dialog, send
    /api confirm within 2 minutes.`
- **Output filters.** Escape `<code_interpreter` (row 16: it would run Pyodide and re-call the
  agent); rewrite external markdown images as links (no CSP, so the browser would fetch them); cap
  the rendered reply at 64 KiB with a pointer to the workspace.

### 4.2 Adaptive Memory guard — `filters/adaptive_memory.py`

Keeps a memory model off the GPU on Code-agent turns, keeps coding transcripts out of the memory
store, and keeps memory blocks out of the forwarded text.

- `inlet` (`:9225`) gains a `__metadata__` parameter and returns `body` unchanged when all of:
  `__metadata__['features']['code_interpreter']` is true; `__user__['id']` is in a mirrored valve
  `CODE_AGENT_USER_IDS` (default empty); and a mirrored valve `CODE_AGENT` is true (default
  `False`). Either default makes the guard inert. The filter cannot see whether the pipe will fall
  back to today's route for a given turn, so the two valves are set **together with** the pipe's:
  the filter's `CODE_AGENT` is switched on in the same step as the pipe's (phase 2), and while it is
  off the pinned admin's Code turns keep memory injection exactly as today.
- `outlet` (`:9338`) returns `body` unchanged, scheduling no background memory task, when
  `body['chat_id']` is a key of the stamp map `code_agent_chats.json` (read under its sidecar
  lock, §4.1). A chat is stamped only by a real agent turn, so this needs no valve.
- The file has been hand-pasted because it was never edited (`scripts/deploy_pipe.py:61-64`). This
  edit makes it first-party: it joins `FILTERS` in `deploy_pipe.py`, so it is deployed and
  drift-checked like `task_mode`.

### 4.3 The bridge — `harness/ohmzai-code/bridge.py`

The only component that can start a process as `ohmz`. stdlib `ThreadingHTTPServer` on `AF_UNIX`,
built on `scripts/cancel_service.py`'s skeleton (socket timeout, log redaction, refuse to start
without its secret, `/healthz`).

- **Secret.** The bridge reads its bearer key from `/volume1/docker/openwebui/config/code_bridge_key`
  (0640 `root:ohmz`, so `ohmz` reads it through the group) at start, and refuses to start without
  it. The pipe reads the same file as `/app/backend/data/code_bridge_key`. The key is staged in
  phase 1, because R1 and R2a already call the bridge with it (§14).
- **Socket.** `/volume1/docker/openwebui/config/code-bridge/bridge.sock`; directory 0750, socket
  0660, owned by `ohmz`. Only `open-webui` mounts that volume (row 34). Nothing listens on TCP.
- **`POST /v1/turn`.** Bearer compared with `hmac.compare_digest` (401 otherwise). Body, all
  required except `confirm`: `uid`, `chat_id`, `user_msg_id`, `parent_id` (string or null),
  `assistant_msg_id`, `text` (≤ 64 KiB UTF-8), `confirm`. Unknown fields → 400. **argv, flags, cwd,
  model and permission mode never come from the request.** `uid` is checked again against the
  bridge's own `allowed_uids` (403).
- **Response.** `text/event-stream`, one JSON object per `data:` line, `t` ∈ `status` `{msg}`,
  `text` `{text}`, `tool` `{name, summary}`, `hb` `{elapsed_s}`, `confirm` `{nonce, title,
  message}`, `done` `{spawned, backend, model, ws, ctx, window, tools, files, ms, turn}`, `error`
  `{code, msg}`. Every response ends with exactly one `done` or `error`. One-line replies that
  spawn nothing (switches, near misses, refusals, `/status`, `/ws`) are a `text` event plus a
  `done` with `spawned: false`; `done` always carries the chat's `backend` and `ws` after the
  request, which is what refills the pipe's stamp map. `error` is reserved for bridge faults.
- **`GET /healthz`** (and `HEAD`), no auth: `{ok, canary_ok, local_ok, claude_version,
  units_running, last_canary}`. It leaks nothing a caller could use.
- **Confirm nonces.** Single-use, 120 s, held in memory, bound to (chat, uid, target backend, sid,
  fresh), where `sid` is the target session the dialog described and `fresh` the form that was
  asked for. Consuming one applies the **stored** `fresh`, never one read from the confirming
  message, and marks only that `sid` as confirmed (§5). A `confirm` for a nonce that is unknown,
  expired, already used, or bound to another chat, uid or session → `Nothing to confirm; say "use
  API model" again.` Nothing is spawned and nothing changes. A bridge restart forgets every nonce,
  which lands in the same reply.
- **Concurrency.** Non-blocking locks that refuse, never queue: one turn per chat; one turn per
  workspace across chats; at most 2 API turns (row 12); and one local turn, held by an in-bridge
  local-turn lock so a second chat's local turn is refused at once rather than waiting behind the
  first. The GPU flock (§9) is separate: its up-to-10-minute poll waits only for holders **outside**
  the bridge (media, Task mode), because the in-bridge lock already excludes a second local turn.
- **Preflight, in order:** bearer → schema → kill-switch file `~/.config/ohmzai-code/disabled` →
  `canary_ok`, and the **canary inputs** unchanged since the last canary (the resolved
  `~/.local/bin/claude` target, and a content hash of `turn_exec.py`, `bridge-settings.json`,
  `git-guardrails.py` and the parsed `workspaces.json`; changed → refuse with `Code agent paused:
  re-running self-test` and start the canary) → `uid` → the per-chat lock (it needs only the chat
  id) → `GET http://127.0.0.1:8788/` (fails → `deepseek relay is down`) → load the chat record, whose
  first step is the archive restore (§5) → `commands.parse` (§6) → a request with `confirm` set, or
  a `/api confirm` parse, consumes the nonce, applies the switch and ends here, skipping regenerate/continue/edit detection
  and never spawning → regenerate/continue/edit detection (§5) → resolve the workspace (§7) → the
  per-workspace lock → plan the session (§5) → local only: GPU admission, the in-bridge local-turn
  lock, the flock, and admission again after the flock (§9).
- **Startup order.** Reap leftover `ohmzai-code-*.service` units **first** (`stop`, then
  `reset-failed`), then serve `/healthz`, then run the canary in-process. Turns are refused until
  `canary_ok`.
- **Unit names** are `ohmzai-code-<chat_id>-<seq>`, where `<seq>` is a per-spawn counter the bridge
  persists in `<state>/seq` plus 6 random hex characters — never the logical turn number, so a
  regenerate or continue, which re-drives the same logical turn, cannot reuse the name of a sibling
  that `--collect` has not yet removed. Before each spawn the bridge runs `systemctl --user
  reset-failed <name>` and refuses the turn if the name is still loaded.
- **Stream loop** over the unit's stdout:
  - `system/init`: assert `session_id == sid` and `cwd` equals the record's cwd; otherwise stop the
    unit and fail the turn (row 3).
  - `assistant` text blocks → `{t: text}`; thinking blocks, `system/thinking_tokens` and hook events
    are dropped.
  - `tool_use` → `{t: tool}` with a one-line summary (Read/Edit/Write path; Bash's first 120
    characters) and an audit row. Edit and Write paths feed `changed_files`, so the bridge never runs
    git in a workspace. On a local turn, Read, Glob and Grep paths and Bash text also feed
    `local_files_read` (§5).
  - `system/api_retry` with `error_status` 401 or 403 → stop at once. The gate is part of the
    bridge, so the bridge first checks the gate's own `egress_blocked` counter for this unit: non-zero
    → reply `Blocked by the relay gate: <model> <path>; nothing left the box.`; zero → reply naming
    `~/.config/deepseek/secrets.env` (row 5), since the gate injects that token (§4.8). Any other
    retry → status `API retrying n/10`. A turn ending in an error result gets the same check.
  - `system/status compacting` → status `Compacting context…`.
  - A heartbeat every 10 s; a failed write means the client is gone → `systemctl --user stop <unit>`.
  - **Idle rule.** 300 s with no stream event → stop — except while a `tool_use` is outstanding
    (no matching `tool_result` yet), when nothing is emitted by design (row 4): the idle timer is
    then paused and replaced by a bound of `BASH_MAX_TIMEOUT_MS` + 30 s (630 s) for that tool call,
    and the 300 s rule resumes at the matching `tool_result`. 900 s wall → `RuntimeMaxSec` stops it.
  - `result`: success is `is_error == false` and `api_error_status` null, never `subtype` (row 5).
    `ctx` = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` of the last
    assistant usage.
- **Finish.** Atomic record and meta update (including `last_user_msg_id`/`last_assistant_msg_id`,
  which every reply sets, spawned or not, §5), audit line, locks released, the per-turn run
  directory and env file removed, then `{t: done}`.

### 4.4 Command grammar — `harness/ohmzai-code/commands.py`

Pure and deterministic: no I/O, no clock, no model. `parse(text) -> Parsed(backend, fresh,
confirm, ws, new, status, run_as_is, rest, near_miss)`. It is the only thing that changes a chat's
backend or workspace. Grammar and pinned table in §6; its tests are `tests/test_code_commands.py`
(suite 1).

### 4.5 Chat records — `<state>/chats/<chat_id>.json` and `<state>/meta/<sid>.json`

The durable chat → workspace/session/backend mapping. Specified in §5.

### 4.6 Workspace resolver and config — `~/.config/ohmzai-code/workspaces.json`

Host-shell editable only; no turn can see it. Keys:

| Key | v1 value |
|---|---|
| `roots` | `["/home/ohmz/StudioProjects", "/home/ohmz/src"]` |
| `scratch_root` | `/home/ohmz/ohmzai-code/scratch` |
| `toolchains_ro` | `~/.local/share/claude` (the claude versions; the unit execs the resolved target, §4.7); **named files** from `~/.local/bin`, never the directory (v1: `pytest`; each further binary is added by name after review, row 45); `~/.local/lib/python3.12/site-packages` (where `pytest` lives); `~/.local/share/uv/python`; `~/.nvm`; `~/.cargo`; `~/.rustup`; `~/.pub-cache`; `~/.local/flutter` — each only if it exists. Candidates, confirmed or narrowed by R2a |
| `scan_exceptions` | reviewed exact paths the toolchain secret scan (§7) may pass over, each with a one-line reason; v1 starts empty and R2a fills it |
| `allowed_uids` | exactly one entry: the admin account's OpenWebUI user id |
| `default_backend` | `"api"` |
| `local_enabled` | `false` until phase 3 |
| `local_switch_max_ctx` | the M1 result (§13) |
| `local_autocompact_pct_override` | the M1 result for `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` on local turns; `null` (the v1 default) leaves the variable unset |
| `busy_window_s` | `600` |
| `git_identity` | `{name, email}` for `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, so agent commits are attributable |

Resolution rules are in §7.

### 4.7 Turn runner (in `bridge.py`) and `harness/ohmzai-code/turn_exec.py`

One hardened transient unit per turn. The unit is a sibling under the user manager, not a child of
the bridge's cgroup, so the bridge's own hardening (§4.12) does not constrain what the unit is
granted — which is why the bridge takes nothing but text from the request.

```
systemd-run --user --unit=ohmzai-code-<chat_id>-<seq> --collect --wait --pipe --quiet
  --working-directory=<ws>
  -p NoNewPrivileges=yes
  -p TemporaryFileSystem=/home/ohmz:ro
  -p BindReadOnlyPaths="<toolchains_ro…> /home/ohmz/ai-stack/harness/ohmzai-code/turn_exec.py
                        /home/ohmz/ai-stack/harness/ohmzai-code/bridge-settings.json
                        /home/ohmz/ai-stack/harness/claude/hooks/git-guardrails.py"
  -p BindPaths="<ws> <state>/claude <state>/cache/<wsid> <state>/run/<unit>"
  -p InaccessiblePaths="-/run/docker.sock -/var/run/docker.sock -/volume1 -/etc/cloudflared"
  -p PrivateTmp=yes -p PrivateNetwork=yes
  -p KillMode=control-group -p RuntimeMaxSec=900 -p MemoryMax=8G -p TasksMax=512
  -p EnvironmentFile=<state>/env/<unit>.env
  -- /usr/bin/python3 /home/ohmz/ai-stack/harness/ohmzai-code/turn_exec.py
       --keep <comma-separated key names> --relay <state>/run/<unit>/relay.sock [--git-init]
  -- <claude target> -p --output-format stream-json --verbose --restricted
       --tools Bash,Read,Edit,Write,Glob,Grep
       --settings /home/ohmz/ai-stack/harness/ohmzai-code/bridge-settings.json
       --strict-mcp-config --permission-mode dontAsk --permission-prompts none
       --max-turns 40 --model <tag> (--session-id|--resume) <sid>
```

- Every bind is a **same-path** bind, so the home view is exactly the bind list and nothing else.
  Paths are written literally (`/home/ohmz`), not as `%h`. `<wsid>` is `sha1(realpath(ws))[:12]`.
  `<seq>` is the per-spawn sequence of §4.3.
- `<claude target>` is `realpath(/home/ohmz/.local/bin/claude)` as preflight resolved it (today
  `/home/ohmz/.local/share/claude/versions/2.1.285`, row 45), inside the bound
  `~/.local/share/claude`. The symlink itself is not bound, and the canary runs against the same
  resolved path, so a claude update changes the target and re-runs the canary (§4.3).
- The prompt is written to stdin as plain text and stdin is closed. There is no positional prompt,
  so text starting with `-` can never be read as a flag.
- `turn_exec.py`, in order:
  1. **Environment.** Keep exactly the keys named by `--keep` plus `HOME`, drop everything else the
     user manager injected (`SSH_AUTH_SOCK`, `DBUS_*`, `DISPLAY`, `OLLAMA_HOST`, any
     `CLAUDE_CODE_*`, …), and report the kept key names (names only) on stderr's first line for the
     canary.
  2. **No route out.** A TCP connect to `1.1.1.1:443` and one to `127.0.0.1:4567` must both fail;
     otherwise exit 97.
  3. **Scratch init.** With `--git-init`, run `git init -q` in the (empty, new) scratch workspace.
  4. **Forwarder.** Bind and listen on `127.0.0.1:8788` inside the netns, then **fork**: the child
     closes fds 0-2 and forwards each connection to `--relay`; the parent continues. (A thread would
     not survive `execve`.) `KillMode=control-group` reaps the child when the unit ends.
  5. **`execve`** claude with the kept environment.
- **The per-turn env file** `<state>/env/<unit>.env` (0600) is written before the spawn and removed
  at unit exit. It never holds the DeepSeek token. The gate reads `secrets.env` at every turn, so a
  rotated token needs no restart.

| Key | API turn | Local turn |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:8788` | same |
| `ANTHROPIC_AUTH_TOKEN` | `gate-injected` — a fixed dummy; the relay gate adds the real token (§4.8), so it never enters the unit | `router-local` |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`, `--model` | `deepseek-flash[1m]` | `qwen38-coder:q4-128k` — **all** slots, so no cloud slot and no `gemma3:1b` co-load |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | unset | `98304` (row 11) |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | unset | `workspaces.json` `local_autocompact_pct_override` (§4.6); unset while it is `null` |
| `API_FORCE_IDLE_TIMEOUT` | unset | `0` (as the launcher) |

  Both backends also get: `CLAUDE_CONFIG_DIR=<state>/claude`; `CLAUDE_CODE_TMPDIR=/tmp` (private);
  `XDG_CACHE_HOME`, `npm_config_cache`, `PIP_CACHE_DIR`, `UV_CACHE_DIR` and `XDG_STATE_HOME` under
  `<state>/cache/<wsid>/`; `DISABLE_AUTOUPDATER=1`; `DISABLE_TELEMETRY=1`;
  `DISABLE_ERROR_REPORTING=1`; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`;
  `BASH_MAX_TIMEOUT_MS=600000`; `GIT_AUTHOR_*`/`GIT_COMMITTER_*` from `git_identity` (`~/.gitconfig`
  is not bound); `PATH` built from the existing toolchain `bin` dirs (including `~/.local/bin`, which
  inside the unit holds only the named files) plus `/usr/local/bin:/usr/bin:/bin`;
  `LANG=C.UTF-8`; `TERM=dumb`.
- **The launcher is never called.** A parity test pins the local tag, the 98304 window and the cloud
  model against the launcher's export block, and asserts the one deliberate departure (every local
  slot is the coder).

### 4.8 Relay gate (in `bridge.py`)

The only network path out of a turn unit, and the structural block on cloud egress for local turns.

- Per turn, a Unix listener at `<state>/run/<unit>/relay.sock`, created **before** the spawn and
  removed at unit exit.
- A minimal HTTP/1.1 proxy, **one request per connection**: read the headers and the body, answer
  with `Connection: close`, and close after the response. One request per connection is a security
  property, not only a simplification: the gate inspects every request, and a kept-alive
  connection would let a second request ride behind a checked first one. The cost is a loopback
  connect per request, negligible beside a model call.
- **Body framing.** A `Content-Length` body is read as is. A `Transfer-Encoding: chunked` body is
  de-chunked into a buffer and forwarded with a computed `Content-Length` (row 44 shows only
  Content-Length bodies so far; nothing depends on that holding). Either way the body is capped at
  32 MiB; over the cap → 413 and an audit row.
- Admitted: `POST` to path `/v1/messages` (query string ignored; the wire path is
  `/v1/messages?beta=true`, row 44). The admitted model is the **wire name**: the configured tag
  with any trailing `[...]` suffix removed, computed from the same constant that sets `--model` and
  the `ANTHROPIC_*_MODEL` slots — `deepseek-flash` on API turns (the slots stay `deepseek-flash[1m]`;
  Claude Code strips the suffix before sending, row 44) and `qwen38-coder:q4-128k` on local turns.
  Anything else → 403 with body `{"type":"error","error":{"type":"permission_error","message":"egress_blocked:
  <model> <path>"}}`, an `egress_blocked` audit row, a per-unit counter the stream loop reads
  (§4.3), and a chat warning. R1 establishes that a normal API session needs no other path (§13).
- **Credentials.** The gate removes any client `Authorization` and `x-api-key` header and sets its
  own: on API turns `Authorization: Bearer <DEEPSEEK_API_TOKEN>`, read from
  `~/.config/deepseek/secrets.env` for each turn; on local turns `Authorization: Bearer router-local`
  (the router replaces it for Ollama anyway). The router forwards client auth verbatim to DeepSeek
  (`router.py` `forward`, `auth: passthrough`), so this is the header DeepSeek sees. The real token
  therefore exists only in the bridge: not in claude's environment, the env file, `systemctl --user
  show`, or anything sandboxed Bash could read from `/proc` — independent of yama `ptrace_scope`
  (row 49).
- Admitted requests are forwarded to `127.0.0.1:8788` with the headers above and the response is
  streamed back.
- Each request is logged to the audit: model, path, bytes, status, time to first response byte.
- The canary points the gate at an in-process stub instead of `:8788`.

### 4.9 Agent profile — `harness/ohmzai-code/bridge-settings.json` plus an isolated `CLAUDE_CONFIG_DIR`

Drops the user's plugins, hooks, memory, local allows, `auto` mode and connectors (row 9); supplies
the sandbox that bounds Bash. One file, read-only bound into every unit at its repo path, so a turn
cannot edit it and there is no installed copy to drift. Allow rules and sandbox live in the **same**
file so a schema error fails closed (row 36).

```json
{
  "permissions": { "allow": ["Bash", "Edit", "Write"] },
  "sandbox": {
    "enabled": true, "failIfUnavailable": true, "allowUnsandboxedCommands": false,
    "network": { "allowedDomains": [] },
    "filesystem": { "denyRead": ["/volume1", "/run/docker.sock", "/var/run/docker.sock",
                                 "/etc/cloudflared",
                                 "/home/ohmz/.local/state/ohmzai-code/claude/projects"] },
    "credentials": { "envVars": [ { "name": "ANTHROPIC_AUTH_TOKEN", "mode": "deny" } ] }
  },
  "cleanupPeriodDays": 120,
  "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command",
    "command": "/home/ohmz/ai-stack/harness/claude/hooks/git-guardrails.py", "timeout": 10 } ] } ] }
}
```

- `denyRead` on the transcripts directory stops sandboxed Bash reading **other chats'**
  transcripts — including local-only ones — into an API turn. `--restricted` already confines
  Read/Glob/Grep to the working directory. The rest of `denyRead` is defence in depth: bwrap runs in
  the unit's mount namespace and sees only the allowlisted home.
- `cleanupPeriodDays` 120 sits above the 90-day archive (§5), so Claude Code's own cleanup never
  deletes a transcript the janitor has not archived. Its clock is the file's mtime, and a move keeps
  mtimes, so the archive restore also touches every restored file to now before the spawn (§5);
  otherwise a chat restored after 120 idle days would have its transcript deleted by the very claude
  start meant to resume it. `cleanupPeriodDays: 0` is not used instead: in Claude Code it disables
  transcript persistence, which would end resume altogether.
- The git-guardrails hook is a workflow net against destructive git in the workspace, not a bound
  (row 38's write confinement is the bound).

### 4.10 Selftest canary (in-process, `bridge.py`)

Proves containment before any turn is served, using the exact production wrapper, `turn_exec`,
argv and settings, with the relay gate pointed at a scripted stub Anthropic server and a random
**sentinel** value as the token the gate injects.

- **Must fail** (each a scripted Bash `tool_use` unless noted): `sudo -n true`; a request on
  `/run/docker.sock`; `cat ~/.ssh/known_hosts`; `cat ~/.codex/auth.json`;
  `cat ~/.config/google-chrome/Default/Cookies`; `ls ~/.local/share/keyrings`; `cat ~/.vnc/passwd`;
  `cat ~/immich/.env`; `cat /volume1/docker/openwebui/config/webui.db`;
  `curl http://127.0.0.1:4567/`; `curl -s http://127.0.0.1:8788/` (the in-netns forwarder, the one
  loopback listener that exists inside the unit: reachable by claude, never by sandboxed Bash);
  `curl https://example.com`; `touch ~/.cache/x`;
  `ls <state>/claude/projects`; `touch <state>/claude/probe`; the Read tool on
  `/home/ohmz/ai-stack/harness/ohmzai-code/bridge-settings.json`; a workspace
  `.claude/settings.json` SessionStart hook writing a marker (the marker must not exist).
- **Sentinel:** the stub must receive `Authorization: Bearer <sentinel>` on every API-profile request
  (the gate injects it) and `Bearer gate-injected` must never reach it; and the sentinel must not
  appear in the output of `env`, `cat /proc/self/environ`, `cat /proc/$PPID/environ`, or
  `grep -a -r <sentinel> /proc/*/environ`. With the token held only by the gate (§4.8) the second
  half is belt and braces: under `ptrace_scope` 1 those `/proc` reads fail for ancestors whatever
  they hold (row 49), so they are not what makes the token unreachable.
- **Must succeed exactly:** every existing `toolchains_ro` path passed the secret scan and is bound
  (a refusal fails the canary with the offending file named in `/healthz`'s `last_canary` and the
  journal; a bind is never dropped silently); `ls -A ~` equals the top-level set implied by the
  binds; `turn_exec`'s
  reported key set equals `--keep` ∪ {`HOME`}; Bash's `env` contains none of `SSH_AUTH_SOCK`,
  `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `GPG_AGENT_INFO`,
  `OLLAMA_HOST`, `LIBVIRT_DEFAULT_URI`, `OPENCODE_*`, `ANTHROPIC_AUTH_TOKEN`; `touch ./ok`.
- **Gate:** a local-profile run requesting `deepseek-flash` gets 403; an API-profile run
  requesting the qwen tag gets 403; an API-profile run configured with `deepseek-flash[1m]` is
  admitted (its wire name is `deepseek-flash`).
- **Local check** (only when `local_enabled`): a synthetic session at `local_switch_max_ctx`
  resumed on the local profile produces no `compacting` status. Failure sets `local_ok=false`, which
  refuses local turns only.
- **Triggers:** startup; a change in any canary input — the resolved claude target, or the content
  hash of `turn_exec.py`, `bridge-settings.json`, `git-guardrails.py` and the parsed
  `workspaces.json` (which defines what `ls -A ~` must equal) — checked in preflight (§4.3); daily.
- **On failure:** `canary_ok=false` in `/healthz`; turns refused with `Code agent paused:
  containment self-test failed`; retries at 5 min, doubling to 1 h. The bridge sends no alert itself:
  the watchdog's `check_code_bridge` reads `canary_ok`, and the watchdog's own engine confirms over
  2 runs, alerts, and reminds daily while it stays down (row 30).

### 4.11 GPU arbiter (in `bridge.py`, local turns only)

Admits a local turn only when the GPU is genuinely idle of other tenants, and never unloads
anyone's model. Specified in §9.

### 4.12 The unit — `harness/ohmzai-code/systemd/ohmzai-code-bridge.service`, committed, with `install.sh`

```
[Unit]
Description=OhmzAI Code Agent bridge
Wants=deepseek-router.service
After=deepseek-router.service
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/ai-stack/harness/ohmzai-code/bridge.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectHome=read-only
ReadWritePaths=%h/.local/state/ohmzai-code %h/ohmzai-code/scratch /volume1/docker/openwebui/config/code-bridge
InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock

[Install]
WantedBy=default.target
```

Plus `ohmzai-code-bridge.service.d/onfailure.conf` (`OnFailure=stack-alert@%n.service`), as
`cancel-service` has. No `ExecStartPre`: the canary is in-process. `install.sh` creates the three
writable directories (a missing `ReadWritePaths` entry fails the unit), links the unit, and enables
it. Two of the directories are ordinary `mkdir -p` as `ohmz`; the socket directory is not, because
`/volume1/docker/openwebui/config` is `root:ohmz` 0750, so `install.sh` prints and runs
`sudo install -d -o ohmz -g ohmz -m 0750 /volume1/docker/openwebui/config/code-bridge`. The bridge
key is staged by hand in phase 1, like `hermes_api_key`:
`/volume1/docker/openwebui/config/code_bridge_key`, 0640 `root:ohmz`. The bridge reads it there
(§4.3); the bridge unit's `ProtectHome=read-only` does not cover `/volume1`, and the key is only read.

### 4.13 Small edits elsewhere

- `pipes/auto_assistant.py` `_gpu_lock.__enter__` (`:154-157`): `_gpu_lock_acquire` returns `False`
  at once on a contended flock (the `except BlockingIOError` branch, `:130-134`), so
  `while not _gpu_lock_acquire(1.0): pass` spins. Add
  `time.sleep(1.0)` on that path; media emits `waiting for GPU (local coding session)` where an
  emitter exists.
- `_hermes_stream` (Task mode, `:6777`) takes the flock through `_locked_stream`'s polling (`:823`).
  Side effect, accepted: a media render now waits for an in-flight Task run as well.
- `hermes/plugins/gpuguard/__init__.py`: a held `.gpu.lock` counts as busy (a `LOCK_EX|LOCK_NB` probe,
  released at once).
- `scripts/stack_alert.py`: a 30-minute cooldown per unit. The file has uncommitted edits (alert
  batching); the cooldown is built on top of them. Tested by suite 11.
- `scripts/stack_watchdog.py`: two checks, each registered in all three places row 30 names —
  `CHECKS` (label, where), `_checkers()`, and `SEVERITY` with `down` for both:
  - `code_bridge`: `/healthz` over the socket; ok iff HTTP 200 and `canary_ok`. When
    `systemctl --user is-enabled ohmzai-code-bridge` is not `enabled`, or
    `~/.config/ohmzai-code/disabled` exists, it returns ok with detail `disabled`, so the kill
    switch and the phase 1 rollback (§10.4, §14) do not raise a daily-reminding DOWN. A plain
    `systemctl --user stop` of an enabled unit still alerts: that is the unit being down.
  - `router`: any HTTP answer from `:8788` is alive, the house idiom.
- `scripts/deploy_pipe.py`: `adaptive_memory` joins `FILTERS`.
- `compose/openwebui/run.sh`: `CORS_ALLOW_ORIGIN` (phase 0, §10.3).

### 4.14 Parameters

Every limit, and where its number comes from.

| Parameter | Value | Basis |
|---|---|---|
| Turn wall clock (`RuntimeMaxSec`) | 900 s | Chosen. The relay's 900 s `UPSTREAM_TIMEOUT` (`router.py:48`, used at `:329-332`) is the socket timeout of each upstream connection, so it caps one slow response, not a turn of many requests |
| Turn idle (no stream event) | 300 s, paused while a `tool_use` is outstanding (then bounded by 630 s for that call) | Chosen; the same idle ceiling the pipe gives Hermes (`HERMES_TIMEOUT_S`, `:290`). The pause exists because nothing is emitted between a `tool_use` and its `tool_result` (row 4), so a plain 300 s rule would cap every Bash command at 300 s |
| `--max-turns` | 40 | Chosen |
| `BASH_MAX_TIMEOUT_MS` | 600000 | Chosen, under the wall clock; effective because the idle rule pauses during a tool call |
| Heartbeat | 10 s | Chosen; a threaded HTTP handler notices a dead client only when it writes |
| Pipe `sock_read` / `sock_connect` | 60 s / 5 s | Chosen: six heartbeats; a local Unix socket |
| Request text / rendered reply | 64 KiB / 64 KiB | Chosen |
| `MemoryMax` / `TasksMax` | 8G / 512 | Chosen against 94 GB RAM, ~30 GB free (row 29) |
| Concurrency | 1 per chat, 1 per workspace, ≤ 2 API, 1 local | Relay parallelism (row 12); `NUM_PARALLEL 1` (row 25) |
| Confirm nonce | 120 s, single use | Chosen |
| Big-model threshold | `size_vram` ≥ 10 GiB | gpuguard's rule (`gpuguard/__init__.py:88`) |
| Busy window | 600 s | Owner decision, 2026-09-29 |
| `/api/ps` sampler | every 5 s | Chosen |
| flock poll / status / cap | 1 s / 5 s / 10 min | 1 s matches the pipe's own poll (`_gpu_lock_acquire(1.0)`, `:155`); the rest chosen |
| Local window | 98304 tokens | The launcher (row 11) |
| `local_switch_max_ctx` | M1 trigger − 15,000 | M1 measures the trigger; 15K headroom chosen |
| Handoff reply tail | 4 KiB | Chosen |
| Archive after idle | 90 days | Chosen; `cleanupPeriodDays` 120 keeps Claude Code's cleanup behind it |
| Canary retry | 5 min, doubling to 1 h | Chosen |
| `stack_alert` cooldown | 30 min per unit | Chosen |
| Unit start limit | 5 in 600 s | Chosen |
| Grammar reach | `m.start("term") <= 80` on the normalised line | Chosen; pinned at 80 and 81 (§6) |
| Gate body cap | 32 MiB | Chosen, far above any observed request |
| `local_files_read` cap | 200 paths, then a count | Chosen |

## 5. Session model

**Claude Code's own transcript, resumed every turn, with no long-lived process.** Row 2 proves the
mechanism; row 11 makes spawn-per-turn the natural fit, because a backend switch is a respawn anyway.

- **Key:** one session per `(chat_id, ws, gen)`, `sid = uuid5(NAMESPACE_URL,
  "ohmzai-code:{chat_id}:{ws}:{gen}")`. `ws` is `scratch` or `<root basename>/<repo>` (e.g.
  `StudioProjects/foo`). The first turn uses `--session-id <sid>`, later turns `--resume <sid>`.
  `--continue` is never used (row 3).
- **Pinned cwd.** A session is always spawned in its creation cwd, and `init.cwd` is asserted (§4.3).
- **Isolation.** Transcripts live under `<state>/claude`, so they never appear in the owner's own
  `/resume` list or memory (row 9).

**Records**, both written atomically (tmp + `os.replace`), both invisible to turn units:

- `<state>/chats/<chat_id>.json`: `{uid, backend, backend_source, ws, last_user_msg_id,
  last_assistant_msg_id, last_spawned, last_reply_tail, interrupted, sessions: {<ws>: {sid, gen,
  cwd, ever_local, backend_of_last_turn, api_confirmed_after_local, local_files_read,
  local_files_read_more, ctx, turns, changed_files_last}}}`. `backend_source` ∈ `default`,
  `command`, `slash`, `confirm`, `rebuild`. A corrupt file affects only its chat.
  - `api_confirmed_after_local` is per session: set true only by consuming a confirm nonce that
    names that session's `sid` (§4.3), reset to false by every local turn in that session.
  - `local_files_read` is per session: the deduplicated paths from Read, Glob and Grep `tool_use`
    inputs and the path-like tokens of Bash text on **local** turns, capped at 200 with
    `local_files_read_more` counting the rest. A new session (gen bump) starts empty. It is what
    the confirm dialog lists (§6).
- `<state>/meta/<sid>.json`: `{chat_id, uid, ws, gen, ever_local, backend_of_last_turn, created}`,
  written when the session is created and updated every turn.

**Fail-closed rebuild:**

- **Archive restore comes first.** Loading a chat record starts by checking `<state>/archive/` for
  the chat; if it is there, record, meta and transcripts are moved back and every restored file is
  `os.utime`d to now (so Claude Code's 120-day cleanup, which keys on mtime, cannot delete the
  transcript the resume is about to load, §4.9). Only then do the rebuild and lost-state rules below
  run, so an archived chat is never mistaken for a lost one.
- A missing chat record is rebuilt from every `meta/` file with that `chat_id`: each session's ws,
  gen, `ever_local` and `backend_of_last_turn` come back exact; `api_confirmed_after_local` comes
  back **false** and `local_files_read` empty. The chat's `ws` is the most recently updated
  session's, and its `backend` is that session's `backend_of_last_turn`, with `backend_source =
  rebuild`. A rebuilt record therefore never resumes a local session on the API profile without a
  new confirm: `rebuild` counts as not confirmed, and the egress invariant below catches it.
- No record and no meta, but the audit log has rows for this chat → refuse:
  `Session state lost. Say /local or /api to continue.` Answering `/local` starts a new local
  session; answering `/api` goes through the local→API confirm (§6) and then starts a new API
  session. Either way the new session is in the workspace the audit last recorded for the chat, at
  `gen` = (highest audited gen) + 1, seeded with the handoff note without the reply tail. It never
  silently defaults to API.
- The `already in use` self-heal (`--session-id` collides → `--resume`) is used only when a record or
  meta exists.
- **The egress invariant:** a session whose `backend_of_last_turn` is `local` is spawned with the
  API profile only when **its own** `api_confirmed_after_local` is true. It is checked at spawn time
  for the session actually being spawned, whatever the chat-level `backend` or `backend_source`
  says, so switching workspaces, a rebuild or a stale chat flag cannot carry one session's consent
  to another. A planned API spawn that fails it spawns nothing and returns the local→API confirm for
  that session instead (§6; the task is dropped with `Resend your task after switching`). Tested
  directly.

**Retention.** No LRU, no eviction. A daily janitor moves chats idle ≥ 90 days — record, meta and
transcripts together — to `<state>/archive/`. The next turn restores it as above, so a return after
any idle time resumes normally.

**Regenerate, continue, edit** (the double-apply fix):

- **Every bridge reply, spawned or not, sets `last_user_msg_id`, `last_assistant_msg_id` and
  `last_spawned`.** So the message after a near miss, a switch-only reply, `/status`, `/ws` or a
  refusal is not mistaken for an edited older message.
- **A request with `confirm` set skips this detection entirely and never spawns** (§4.3). The pipe's
  confirm re-POST repeats the same `user_msg_id` and `assistant_msg_id`, which would otherwise read
  as a continue and send `[Continue from where you stopped.]` to the API right after the click.
- `user_msg_id == last_user_msg_id` with a new `assistant_msg_id` is a **regenerate**. If the last
  reply spawned a turn, the bridge sends a fixed message instead of replaying: `[The user asked you
  to redo your last answer. Your earlier edits to <changed_files_last> are already on disk; run git
  diff and review before changing anything. Original request: <text>]`. The reply opens `Retrying;
  your earlier edits to X, Y were kept on disk.`
- `assistant_msg_id == last_assistant_msg_id` is a **continue**: `[Continue from where you
  stopped.]`, if the last reply spawned a turn.
- A regenerate or continue of a reply that spawned nothing (`last_spawned` false) is evaluated
  again as a fresh request on the same text; the grammar is deterministic, so it gets the same
  answer.
- `parent_id != last_assistant_msg_id` (an edited older message) runs as a new instruction, with a
  notice that the session is linear and files are not rolled back.

**Interrupted turn.** Stop, an OWUI restart or a lost client marks the record `interrupted` with its
changed files. The next reply opens `Previous turn was stopped; files may be partly edited: <files>`
— a Stop cannot yield into the stopped message (row 19). If the next resume errors on a dangling
`tool_use`, the bridge moves to `gen + 1` with the handoff note.

**Handoff note** (deterministic): `[Handoff: new session in <workspace path>. The previous session's
last reply ended with: <last 4 KiB>. Run git status and git diff before doing anything else.]`,
followed by the user's text when there is any. **When the new session is on the API and the
previous one was local** (`/api fresh`, the lost-state `/api` answer), the reply tail is dropped:
`[Handoff: new session in <workspace path>. Run git status and git diff before doing anything
else.]`. The tail is local-model output that may quote local files, and the dialog promises only a
handoff note.

**`/new`** bumps `gen` for the current workspace. **Only the latest clean user text is ever sent**;
the session carries everything else.

## 6. Backend switch grammar

Only `commands.parse` changes the backend. No model, no classifier, no `--fallback-model`, never an
automatic switch. It runs only on Code-agent turns, on the clean text from §4.1.

**Normalise.** Take the first non-empty line. A line starting with ```` ``` ```` or `>` is never a
command. Apply NFKC, lowercase, collapse whitespace, strip. Voice spellings of "api" become `api`:
`re.sub(r"\ba\.\s?p\.\s?i\.?(?=\W|$)|\ba p i\b", "api", line)`. Normalisation keeps an **index
map** from each normalised character to its offset in the original first line, so a match found on
the normalised line can be cut from the original text.

**Natural-language form** — the noun is required, the pattern is anchored at line start, and the
reach is `m.start("term") <= 80`, measured on the normalised line (after `a.p.i.` has become
`api`):

```
P     = r"(?:(?:ok|okay|so|now|and|then|alright|hey|please|pls)\b[ ,]*)*"
MODAL = r"(?:(?:can|could|would|will) you |let's |let us |i want (?:you )?to |i'd like (?:you )?to )?"
VERB  = r"(?:use|switch (?:back )?to|change to|move to|go (?:back )?to|run on)"
BACK  = r"(?P<b>local|api|cloud)"                 # local → local; api|cloud → api; 'deepseek' is not a synonym
NOUN  = r"(?:model|backend|llm)"
FRESH = r"(?P<fresh> fresh| new session)?"
TERM  = r"(?:\s*$|\s*[.!?,;:—–-]+(?=\s|$)\s*(?:(?:and|then)\s+)?|\s+(?:and|then|to)\s+)"
NL    = rf"^{P}{MODAL}{P}{VERB} (?:the )?{BACK} {NOUN}{FRESH}(?P<term>{TERM})"
```

`rest` = the remainder of the **original** line 1 from the offset the index map gives for
`m.end()`, plus every following original line verbatim. Matching happens on the normalised line;
`rest` never does, so case-sensitive paths, identifiers and code reach the agent unchanged. `and`,
`then` and `to` terminate only because they follow the noun. Punctuation terminates only when
whitespace or the end of the line follows it, so `model.py`, `model.json` and `model-based` are not
switches.

**Slash forms**, word-bounded, first line only, tested in this order:

- `^/(api|cloud) confirm(?=\s|$)` — **tested before the general form**, which would otherwise read
  it as `api` with rest `confirm`. For clients without the dialog: it consumes the chat's pending
  nonce (§4.3) and applies the target and `fresh` stored with it. With no pending nonce, or an
  expired one → `Nothing to confirm; say "use API model" again.`; nothing is spawned. Anything after
  `confirm` is ignored.
- `^/(local|api|cloud)(?: (fresh))?(?=\s|$)` — the rest of the message is `rest`.
- `/run <task>` — runs the task as written: no command parse and no near-miss check.
- `/ws`, `/ws <name>`, `/ws scratch` (§7); `/new` (§5); `/status` (backend, model, workspace,
  ctx/window, turns, `local_enabled`, GPU state).

**Near miss.** No command matched, but `\b(?:local|api|cloud) (?:model|backend|llm)s?\b` appears
anywhere in the message, fences included, after every line gets the same normalisation as line 1. **Nothing is spawned.** Reply: `Not run: this
mentions a backend but isn't a switch command. Still on <backend>. To switch, start with "use local
model" or /local; to run it as written, resend starting with /run.` A false positive costs one
resend with `/run`; phase 2 counts them in the audit.

**Pinned table** (the test table in `tests/test_code_commands.py`, verbatim):

| Input | Parse |
|---|---|
| `use local model` | local, rest empty |
| `Okay, switch to the API model, then add tests` | api, rest `add tests` |
| `use local model to refactor utils.py` | local, rest `refactor utils.py` |
| `Use the A.P.I. model.` | api, rest empty |
| `use the a p i model` | api, rest empty |
| `go back to the local backend` | local, rest empty |
| `can you please use the local llm, and fix the test` | local, rest `fix the test` |
| `/local` · `/cloud` · `/api fresh` | local · api · api + fresh |
| `/api confirm` · `/cloud confirm` | confirm (never api with rest `confirm`) |
| `use local model, then fix FooBar.java` | local, rest `fix FooBar.java` (original case) |
| `use local model fresh` | local + fresh, rest empty |
| `okay, ` ×9 then `ok, so use the local model` (term starts at 80) | local, rest empty |
| `okay, ` ×9 then `ok, now use the local model` (term starts at 81) | none — out of reach, then **near miss** |
| `use the api to fetch the users` | none — runs as a task |
| `now use the api and cache the results` | none — runs as a task |
| `use the api, not the scraper` | none — runs as a task |
| `use the cloud to host it` | none — runs as a task |
| `use local to store the token` | none — runs as a task |
| `switch to local and back up the db first` | none — runs as a task |
| `use deepseek` | none — runs as a task |
| `use localhost:3000 for the API` | none — runs as a task |
| `/locale` · `/apiary` | none — runs as a task |
| `use the folder structure from before` · `use the repo root for config` | none — runs as a task |
| `don't use the local model` | **near miss** |
| `why is the local model slow?` | **near miss** |
| `use the local model class instead of the remote one` | **near miss** |
| `use the local model.py as the reference` | **near miss** |
| `use the local model-based ranker` | **near miss** |
| line 1 `fix the parser`, line 2 `use local model` | **near miss** |
| `use local model` inside a ```` ``` ```` fence | **near miss** |
| `/run use the local model class` | run as written |

**Direction rules** — what a parse does, given the chat's current backend. "Unconfirmed local
history" means the chat's session in the target workspace has `backend_of_last_turn == local` and
`api_confirmed_after_local == false` (§5); it is the same test the egress invariant applies at spawn.

| Current | Parsed | `rest` | Behaviour |
|---|---|---|---|
| api | local | empty | `local_enabled` false → `Local backend is disabled on this host.`, no change. Otherwise switch, one-line reply, nothing spawned (admission is checked on the next task turn) |
| api | local | non-empty | GPU admission (§9). Admitted → switch and run `rest` on local this turn. Refused → **no switch**, nothing runs, admission's reason-specific reply (§9) |
| api | local + `fresh` | any | As the two rows above, but the local session is `gen + 1`, seeded with the handoff note (API→local keeps the reply tail, §5) |
| either | the current backend | any | No-op switch; run `rest`, or reply `Already on <backend>` |
| either | the current backend + `fresh` | any | Same as `/new` (`gen + 1` in the current workspace), then run `rest` if any |
| local | api (incl. `fresh`, and the lost-record case), with unconfirmed local history | any | **Always two steps.** `{t: confirm}` bound to the target session and the `fresh` asked for (§4.3); `rest` is dropped with `Resend your task after switching`. Never carries a task across |
| local | api, no unconfirmed local history (e.g. a switch to local whose next task was refused, so nothing ran locally) | any | One step: switch, reply `Switched to API`, and run `rest` on API if any. Nothing local exists to send, so there is nothing to confirm |
| api | `/ws X` (§7), where X's session has unconfirmed local history | — | The workspace switches and the reply is the local→API confirm for X's session before any turn runs there. Declined → the chat stays on X and on API, and every task in X returns the confirm again (the egress invariant, §5) until the owner confirms, says `use local model`, or sends `/new` |

**The local→API confirm.** Dialog text for a resume: `Switch this chat to API (deepseek-flash on
DeepSeek servers)? The session history (~N tokens, including files read while local: a, b, … and
K more) will be sent.` The list is the target session's `local_files_read` (§5); `~N` is its last
`ctx`. For `fresh`: `Switch this chat to API (deepseek-flash on DeepSeek servers)? A new session
starts; only a handoff note naming the workspace will be sent.` After the click the chat switches
and the reply is `Switched to API: deepseek-flash[1m] on DeepSeek's servers. Resend your task.`
Nothing streams from the cloud before the click.

**Sticky.** `record.backend` per chat survives reloads and devices. A new chat starts on
`default_backend = api`. Every turn shows backend, model, workspace and ctx (§8).

**Mid-session thresholds:**

- **API → local** resumes only when `ctx ≤ local_switch_max_ctx`. Above it, a fresh local session
  (`gen + 1`) with the handoff note, and one line saying why. This keeps the resume below the local
  compaction trigger, which would otherwise summarise the whole history on the 8B model before the
  user's turn (row 7).
- **Local → API:** after the confirm, resume by default; `/api fresh` starts `gen + 1` with the
  handoff note without the reply tail (§5).
- **Local session full:** a local result of `automatic compaction failed` (row 7) → `Local session
  is full: /new or use API model.`
- A directive sent while a turn is running is refused by the per-chat lock.

**Egress is structural, not post-hoc.** A local turn's claude has no network except the relay gate,
and the gate rejects every non-local model (§4.8). `modelUsage` in the result is checked as a
secondary assertion.

## 7. Workspace model

Resolved by the bridge only, from `workspaces.json` (§4.6).

- **Eligible:** git repositories that are direct, non-hidden children of `~/StudioProjects` or
  `~/src`, realpath-contained in their root, **whose `<repo>/.git` is a directory**. Worktrees and
  submodule checkouts (`.git` is a file pointing elsewhere) are listed by `/ws` as ineligible in v1:
  their gitdir lives in another repo that a turn unit does not bind, and the per-workspace lock
  would not serialise two workspaces sharing one `.git` (row 47). Nothing else in v1.
- **Always denied**, checked with `coding_task`'s `_resolve_cwd` discipline (containment before
  existence, realpath): `/home/ohmz` itself, any hidden path component, `/volume1`, `~/.config`,
  `~/.local`, `coding_task`'s denylist (`~/.ssh`, `~/.gnupg`, `~/.claude`, `~/.hermes`,
  `~/.config/deepseek`, secret-shaped names), and **`~/ai-stack`** (row 40).
- **Default:** a per-chat scratch directory `~/ohmzai-code/scratch/<chat_id>/`, created by the bridge
  and `git init`-ed by `turn_exec` on the first turn.
- **Commands are slash-only:** `/ws` lists eligible workspaces; `/ws <name>` is an exact,
  case-insensitive basename match; ambiguous or unknown → the candidates as `<root>/<name>`, which is
  also accepted; `/ws scratch` returns to scratch. There is no natural-language workspace form, so
  "use the folder structure from before" is never a command. Switching keeps each workspace's
  session in the record; returning resumes it, subject to the egress invariant: returning an API
  chat to a workspace whose session has unconfirmed local history asks for the confirm first (§6).
- **Concurrency:** one running turn per workspace, across chats.

**What a turn can see.** The home directory is an empty read-only tmpfs. Only these exist inside it:
the workspace (rw); `<state>/claude` (rw; transcripts denied to Bash, §4.9); `<state>/cache/<wsid>`
(rw; npm, pip and uv caches and XDG cache/state); `<state>/run/<unit>` (the relay socket);
`turn_exec.py`, `bridge-settings.json` and the git-guardrails hook (ro); and the `toolchains_ro` list
(ro) — directories, plus individually named files from `~/.local/bin`, never that whole directory
(row 45). Browser profiles, keyrings, `~/.codex`, `~/.vnc` and service `.env` files are absent without
being listed.

**Toolchain secret scan.** At install and at every config load, each `toolchains_ro` path is scanned
for real credential stores, in two tiers:

- **Anywhere in the tree:** `.netrc`, `.pypirc`, `credentials.toml`, `auth.json`, `id_rsa*`,
  `id_ed25519*`, `.env`, `*.env`.
- **Only outside package trees:** `.npmrc` (checked only at the top two levels of a toolchain
  root, where a user config would sit), and the name patterns `credentials*`, `*cookies*`,
  `keyring*`, `*.pem`. Package trees are `node_modules`, `.pub-cache/hosted`, `registry/src`
  (cargo), `site-packages`, `packages` (the flutter SDK's), and the bundled npm under each
  `~/.nvm/versions/node/*/lib`. Those trees ship library source named `cookies.dart`,
  `credentials.js` or test `*.pem` fixtures (row 45), which are code, not secrets.

A hit on an exact path listed in `workspaces.json` `scan_exceptions` (with its reason) passes. Any
other hit refuses the whole path and **fails the canary** naming the file (§4.10) — it never
silently drops the bind, which would surface later as a missing tool mid-turn. The fix is a
reviewed `scan_exceptions` entry or a narrower bind, e.g. `~/.cargo/bin` and `~/.cargo/registry`
instead of `~/.cargo` if `~/.cargo/credentials.toml` exists.

**Network is fully denied in v1**, so tests must use installed dependencies and already-resolved
packages. R2a runs the real unit against one node, one python and one flutter repo from
`~/StudioProjects` before phase 2 (§13).

**Attachments** are refused with a notice (§4.1); pasted code works.

## 8. Streaming UX

- **Turn start:** `Code · API (deepseek-flash[1m]) · StudioProjects/foo · turn 5`, or
  `Code · local (qwen38-coder 128k) · …` followed by `Waiting for the GPU…` / `Loading local coder…`.
- **Live status lines,** persisted with the message (row 19): one per `tool_use` (`Reading
  src/parser.py`, `Editing …`, `Running: pytest -q …`); `API retrying n/10`; `Compacting context…`;
  `Still working… 3:10` every 30 s; `local coder evicted by other GPU work; reloading`.
- **Body:** assistant text streams per content block. No token-level streaming in v1. Thinking is
  never shown.
- **Footer:** `— local · qwen38-coder 128k · foo · 7 tools · 2 files edited · 1m 48s · ctx 41k/98k`.
  API turns show `ctx 41k` with no window. Tokens, never dollars (row 6). Edited files are listed by
  path.
- **Follow-up chip:** the opposite-backend phrase and `/status`, served by the task guard with no
  model (§4.1).
- **Leaving local:** the confirmation dialog (§6).
- **Near miss, attachments, lost state, GPU busy, disabled:** an immediate one-line reply; nothing
  spawned.
- **Stop:** the unit is killed within one heartbeat (10 s); the **next** reply opens with the
  interrupted note (§5).
- **Regenerate / continue:** §5.

## 9. GPU choreography

**API turns never touch the GPU or the lock.** Every slot is cloud and the relay gate rejects
anything else.

**Local turns:**

0. **Admission.** Refuse, with nothing run, when any holds. The reply names the first reason that
   applies:
   - `local_enabled` is false → `Local backend is disabled on this host.`;
   - `local_ok` is false → `Local backend paused: self-test failed.`;
   - any of the conditions below → `GPU in use by other models; nothing ran. Retry later, or say
     "use API model".`:
   - another Ollama model with `size_vram` ≥ 10 GiB is resident now — apex-compact for both OWUI
     instances (public guests reach Ollama through the socat forwarder), hermes-genesis:agent (Task
     mode, cron), the 32k coder;
   - one was seen resident in the last `busy_window_s` = 600 s by the 5 s `/api/ps` sampler;
   - ComfyUI's queue (`http://127.0.0.1:8188/queue`, gpuguard's probe, polled with `/api/ps`) has
     anything running or pending now, or had in the last 600 s;
   - the bridge started less than 600 s ago (no history is not the same as idle).

   There is **no `keep_alive: 0` sweep**: with `KEEP_ALIVE 60s` (row 25) idle tenants leave by
   themselves, and the bridge never kills a warm model that has work in flight.
1. **Local-turn lock, then flock.** The in-bridge local-turn lock (§4.3) is taken non-blocking;
   held by another chat → refused at once with the GPU-busy reply. Then `.gpu.lock` per turn:
   `LOCK_EX|LOCK_NB` every 1 s, a status every 5 s, give up after 10 min. That wait only ever covers
   holders outside the bridge (media, Task mode). After acquiring, **re-run the whole of step 0**
   (every condition, not only the big-model rule: a big model now, one in the busy window,
   ComfyUI's queue now or in the window, bridge uptime, and the two flags), since a render that just released the flock may leave ComfyUI's
   models in VRAM; any failure → release both locks and refuse.
2. **Spawn.** `Loading local coder…` until the first assistant event, then the spill check:
   `size_vram < size` for the coder → status `local coder partly on CPU; expect slow turns` and an
   audit row.
3. **Per-turn loads eliminated:** Adaptive Memory skipped in inlet and outlet (§4.2); title, tag and
   follow-up tasks for stamped chats answered without a model (§4.1); every alias slot is the 128k
   tag, so `gemma3:1b` is never co-loaded; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` against chore
   calls.
4. **Chore traffic** is measured before local ships (R1): with `NUM_PARALLEL 1`, every chore request
   on the coder takes the only slot and discards the main conversation's cached prefix
   (`docs/CLAUDE_CODE.md:57-61` measured 2.3k chore calls at a ~29k-token prefill each in one session
   on 2026-09-24). On local every alias slot is the same coder tag, so the gate cannot tell a chore
   request from a main-loop one by model. **A non-zero R1 chore count therefore blocks phase 3 in
   v1**; a chore stub, with a discriminator taken from R1's captured request bodies, would be a
   follow-up spec (§15).
5. **During the turn,** the sampler watches for the coder disappearing or another big model appearing
   → status line and audit row. **The backend never changes automatically.**
6. **Release** the flock in `finally`. No `keep_alive` is sent: the coder unloads 60 s after its last
   request, so a reply within a minute reuses it.

**Courtesy in both directions:** gpuguard defers cron while the flock is held; Task mode waits on the
flock; `auto_assistant` media waits without spinning (§4.13). Admission's busy window also covers a
cron job gpuguard already dispatched, which it cannot stop (row 27).

**Accepted residual, stated plainly.** Plain chat on either OWUI instance takes no lock, and
`image_krea`, `photoreal` and `animate_scail` unload every Ollama model before rendering (row 24). A
guest's message or one of those renders during a local turn evicts the coder; the guest waits for a
22.7 s cold reload (row 26) and the coder reloads on its next request. Local mode can therefore
degrade the public instance while it runs, which is why it is off by default, opt-in per chat, and
admitted only after 10 idle minutes.

## 10. Security model

### 10.1 The stakes

Row 32: `ohmz` has NOPASSWD sudo and the docker group, so any process "as the user" is root on the
homelab — every container, `/etc/cloudflared`, all of `/volume1`. Row 33: signup is open behind admin
approval and an admin JWT lasts 4 weeks. The security fact behind the CORS change (reflected Origin
with credentials, cookie auth, `SameSite=lax`) is confirmed piecewise but was not run end to end.

### 10.2 The 2026-09-29 token-leak incident, and the fix already applied

During research, a `deepseek --cloud -p` probe run from a shell spawned by Claude Desktop sent the
desktop session's **claude.ai OAuth bearer** (class `sk-ant-oat…`, user-agent `claude-desktop,
agent-sdk/0.3.284`) to `api.deepseek.com`: **11 requests between 16:29:40 and 16:32:55, all 401**
(row 42; `~/.cache/deepseek-router.log`). The cause, bisected against a local header-capture stub, is
one variable: `CLAUDE_CODE_ENTRYPOINT=claude-desktop` makes `claude` authenticate with the host's
OAuth token instead of `ANTHROPIC_AUTH_TOKEN`, and the router forwards client auth verbatim. With a
scrubbed env the header was the `secrets.env` token.

**Fix, applied 2026-09-29:** `harness/deepseek/deepseek` now unsets `CLAUDE_CODE_ENTRYPOINT`,
`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_OAUTH_SCOPES`
before `exec claude` (`:187-189`, with the reason in the comment at `:180-186`), verified with a fake
`claude` on `PATH`. The owner was told to rotate the claude.ai session. The probe's leftover
`~/.claude/projects` and `session-env` directories were checked while this spec was written and are
gone. The fix is
in the working tree alongside the owner's other uncommitted edits to that file; it is committed before
phase 0 exits, so a checkout cannot silently revert it.

**This design does not depend on that fix.** The bridge never calls the launcher, and `turn_exec`
passes claude an exact environment (§4.7, L5), which closes the whole class: nothing the user manager
or a desktop session exports can reach claude. And whatever claude sends as auth, the relay gate
replaces it with the `secrets.env` token (§4.8), so no other credential can reach DeepSeek from a
turn.

### 10.3 Layers

| Layer | Mechanism | Defeats |
|---|---|---|
| **L1 — who can trigger** | Valve `CODE_AGENT` (default off); `role == 'admin'`; `__user__['id']` in the pinned UUID list — the admin account only, **not** the role=user gmail account that `TASK_ADMINS` matches (row 22); an explicit Code press; a real chat id; below the `__task__` guard. CORS narrowed in phase 0 (below) | Non-admins, a stolen non-admin session, OWUI's own tasks, a classifier guess |
| **L2 — who can reach the bridge** | Unix socket in an `ohmz` directory on a volume only `open-webui` mounts; bearer key with `compare_digest`; fixed schema, unknown fields refused; `uid` re-checked against `allowed_uids`; nothing on TCP | The eight other host-network containers (row 34), a direct API call |
| **L3 — OS boundary per turn** | `NoNewPrivileges` (sudo dead); allowlist home (`TemporaryFileSystem` + binds); `InaccessiblePaths` for the docker socket, `/volume1`, `/etc/cloudflared`; `PrivateTmp`; `PrivateNetwork`, so the unsandboxed claude process itself cannot reach Ollama, OWUI, the internet or any telemetry endpoint; `KillMode=control-group`. `IPAddressDeny=` is not used. Proven on every start by the canary and `turn_exec`'s route check, and as one piece by R2a | Root via sudo or docker (row 32), secret reads, escapes by the claude process itself |
| **L4 — the agent** | Isolated `CLAUDE_CONFIG_DIR`; `--restricted` (row 37); no Web, MCP or Task tools, `--strict-mcp-config`; `dontAsk`, never `auto` (row 10); allow and sandbox in one ro-bound file (row 36) | Workspace hooks, inherited plugins and allows, a 1B classifier deciding permissions |
| **L5 — environment** | `turn_exec` keeps exactly the named keys plus `HOME`; the API token is not among them — the unit carries a dummy and the gate injects the real one (§4.8) | The incident class (§10.2), ssh-agent and session-bus sockets, token reads from `/proc` or `systemctl --user show` whatever `ptrace_scope` is (row 49) |
| **L6 — inputs and outputs** | Only the latest clean text goes in; Adaptive Memory skipped; attachments refused; `<code_interpreter` escaped; external images neutralised | Injected context, Pyodide re-call loops, image-fetch exfiltration |
| **L7 — egress** | The relay gate is the only path out, enforces the wire model per backend, and sets the credential itself; a local→API crossing needs a human click that names the session it covers, checked per session at spawn (§5) | Local data reaching DeepSeek by accident, by injection, or by a workspace switch or rebuilt record |

**CORS, phase 0.** Setting only `https://ai.ohmz.cloud` would break Socket.IO for LAN users, who open
`:4567` directly by the owner's decision (`docs/TROUBLESHOOTING.md:453`). So `CORS_ALLOW_ORIGIN`
becomes a semicolon list built from the host's actual names: `https://ai.ohmz.cloud`,
`http://<lan-ip>:4567`, `http://<hostname>:4567`, `http://<hostname>.local:4567`, the Tailscale IP and
MagicDNS name on `:4567`, `http://localhost:4567`, `http://127.0.0.1:4567`. The OWUI access log's
client addresses over 14 days confirm which networks are in use. R5 gates it. If R5 fails, the change
is reverted and a pipe-side check replaces it: agent turns whose `__request__` `Origin` header is
present and not on that list are refused. That fallback is itself gated — whether `__request__`
carries the browser's `Origin` during a chat completion is checked in phase 2 — and if neither works,
`CODE_AGENT` stays off.

### 10.4 Audit and kill switches

- **Audit:** append-only JSONL at `<state>/audit.jsonl`, invisible to turn units. Per turn: ts, uid,
  chat, sid, ws, gen, backend and how it was chosen, confirm events, `egress_blocked`, evictions, each
  `tool_use` (Bash text or paths), gate requests, exit, duration, tokens. **Never file contents or env
  values.**
- **Kill switches, fastest first:** the pipe valve; the flag file `~/.config/ohmzai-code/disabled`
  (checked every turn); `systemctl --user disable --now ohmzai-code-bridge`; per-turn Stop. The flag
  file and `disable` also make `check_code_bridge` report ok with detail `disabled` (§4.13), so a
  deliberate switch-off raises no alert or daily reminder; a plain `stop` leaves the unit enabled
  and is reported down.

### 10.5 The residual risk, stated plainly

- A compromised admin session (a 4-week JWT) can drive the agent — within these bounds.
- Code the agent writes can run **unsandboxed later**, when the owner runs it. The footer lists every
  edited file for that reason.
- In API mode, **everything the agent can read — the workspace, the toolchains, its own transcript —
  may be sent to DeepSeek**. The allowlist exists to make that set small and known, not empty.
- A malicious file in a workspace can steer the agent to read other files in that workspace into an
  API transcript.
- Local mode can slow other users' chat (§9).

It is not described anywhere as a sandbox for what the owner later runs, because it is not one.

### 10.6 What is *not* weakened

- No shell reaches any non-admin surface. Task mode, Notebook and plain chat are unchanged.
- The number of shell paths goes from one with weak bounds (`coding_task`) to one with these bounds.
- Hermes cron's dangerous-command mode is untouched. The fork is untouched.

## 11. Failure modes

| Failure | Handling |
|---|---|
| Bridge down or socket missing | `Code agent is offline; nothing ran.` Never a fallback to the coder. `Restart=always` within the start limit; `OnFailure` with the cooldown; `check_code_bridge` |
| Canary fails (claude update, settings drift, sandbox gone) | Process stays up; `canary_ok=false`; turns refused with `Code agent paused: containment self-test failed`; retries 5 min → 1 h; the watchdog confirms over 2 runs, alerts, and reminds daily (row 30). Leftover units are reaped before the canary runs |
| Relay `:8788` down | Preflight fails fast with `deepseek relay is down`; the bridge never starts the router; `check_router` |
| DeepSeek 401/403 | Stop on the first such retry; if the gate blocked nothing this turn, reply naming `secrets.env` (the token the gate injects); audit row |
| `subtype: success` with `is_error: true` | Judged by `is_error` and `api_error_status` only |
| Chat record missing or corrupt | Rebuilt from meta; with only audit evidence, `Session state lost; say /local or /api` (§5). Only that chat is affected |
| Confirm declined, timed out, or no dialog | `Still on local; nothing was sent.`, with the timed-out and no-dialog variants of §4.1. The nonce expires at 120 s; `/api confirm` works only while one is pending, otherwise `Nothing to confirm; say "use API model" again.` |
| API spawn planned for a session with unconfirmed local history (workspace switch, rebuilt record) | Nothing spawns; the local→API confirm for that session (§5, §6) |
| Near-miss phrasing | Nothing runs; the notice explains `/local`, `/api` and `/run` |
| Gate sees a disallowed model or path | 403 (`permission_error`, `egress_blocked: <model> <path>`) to claude, `egress_blocked` audit row, chat warning `Blocked by the relay gate…` — never the `secrets.env` hint; the turn usually errors; nothing leaves the box |
| Gate body over 32 MiB | 413 and an audit row; a chunked body is de-chunked, not refused |
| Stop, OWUI restart, cancelled task | The next heartbeat write fails within 10 s; the unit is stopped with its whole cgroup; the record is marked interrupted; the next reply opens with the note; resume, or `gen + 1` with the handoff if resume errors |
| Tab closed, phone locked | Nothing is killed; the OWUI task finishes and saves the message (row 19) |
| Hung or runaway turn | 300 s idle (paused during a tool call, which is bounded at 630 s), 900 s wall, 40 steps; then `say continue` |
| Second turn on the same chat or workspace, or a second local turn from another chat | Refused immediately |
| Regenerate or Continue | Detected by message ids; a bridge-authored retry or continue message naming the edited files; the instruction is never replayed blindly |
| GPU busy, local disabled, or local self-test failed | Refused with the reason-specific reply (§9). The bridge never unloads a model |
| Coder evicted mid-turn | Status line and audit row; reload on the next request. Accepted residual (§9) |
| Media request while a local turn holds the flock | Waits without spinning; `waiting for GPU (local coding session)` where an emitter exists |
| API→local above the threshold, or Ollama rejects the resumed payload | Fresh local session with the handoff note, and the reason given |
| Test command needs network or a missing cache | Fails in the sandbox with the tool error shown; no installs in v1 |
| Output flood | 64 KiB cap with a pointer to the workspace |
| Attachments on the current message | Nothing runs; paste the content. Chat-level files do not block (§4.1) |
| Injection signature survives the strip (OWUI format changed) | Nothing runs; `Code agent input format changed; paused pending review.`; audit row |
| Non-admin, unpinned, or valve off | Today's routing, unchanged |
| Pinned admin in a temporary chat | `Code agent needs a saved chat.` |
| Toolchain path holds a credential-shaped file | The canary fails naming the file; turns are refused until the owner narrows the bind or adds a reviewed `scan_exceptions` entry (§7) |
| Workspace is a worktree or submodule checkout | Listed by `/ws` as ineligible in v1 (§7) |

## 12. Relationship to Hermes

**Direct, not through Hermes.** This supersedes the **interactive** half of the 2026-09-23 coding
row; Hermes stays the scheduled and background engine. Row 41 is the reason: no Claude Code
continuity between calls, 2-3 swaps per turn with a local coder (the agent sits idle while
`coding_task` blocks, so the cost is fixed swaps rather than mid-generation thrash), a 360 s / 32 KiB
cap with nothing streamed, an LLM hop with a web toolset in front of a shell, and bounds weaker than
§10's.

**Phase 0 actions:**

- Remove `coding_task` from `platform_toolsets.api_server` and `.cron`, and from `plugins.enabled`, in
  `~/.hermes/profiles/coding/config.yaml` (`:13`, `:14`, `:32`); restart `hermes-gateway`. The
  profile, its `.env` and the plugin code stay on disk. Verified by `GET /p/coding/v1/toolsets` with
  the coding key listing no `coding_task`.
- Remove the staged `hermes_coding_api_key` from `/volume1/docker/openwebui/config/`. The profile's own
  `.env` keeps the key, so re-wiring later is a copy, not a regeneration.

**The 2026-09-23 plan:**

| Task | Status |
|---|---|
| 1 — fix `gpuguard.start()` | Done (`a0d828a`); kept |
| 2 — upgrade hermes to v2026.9.21 | Done (the checkout is at that tag); kept |
| 3 — create the `coding` profile | Done; kept |
| 4 — the `coding_task` plugin | Done (`ac1e67e`); kept, unwired in phase 0 |
| 5 — delivery across two hermes roots | **Cancelled.** Its other half — the delivery suite writing live files (`.job_names.json`, `alerts/profile.json`) — is a pre-existing test bug independent of either design and stays open as its own fix |
| 6 — the pipe | **Cancelled** |
| 7 — create the row and deploy | **Cancelled** |
| 8 — documentation | Folded into phase 4 |
| 9 — end-to-end verification | **Cancelled** |

The 2026-09-23 spec and plan are marked superseded (interactive part) in phase 4.

**Later scheduled coding**, if wanted, would call `/v1/turn` as its own principal — API only, a named
workspace — so there is still one shell path. Not in v1.

## 13. Testing

### Offline suites (no GPU, no network)

1. `tests/test_code_commands.py` — the pinned table in §6 verbatim; noun required; `to`/`and`/`then`
   only after the noun; punctuation terminates only before whitespace or the end of the line; the
   reach pinned at `m.start("term")` 80 (a switch) and 81 (not); `rest` cut from the original text
   through the index map (the `FooBar.java` row); near miss; `/run`; `/api confirm` matched before
   the general slash form; slash word boundaries; slash-only `/ws`; voice spellings.
2. `tests/test_code_bridge.py`, with a fake claude, a `systemd-run` shim and the probe fixtures:
   `--session-id` then `--resume`; the fixed argv; unknown fields refused; the prompt on stdin and
   never in argv; the first 401 aborts; `is_error` beats `subtype`; busy, uid and kill-switch
   refusals; a second local turn from another chat refused at once, not queued; unit names unique
   across a regenerate of the same logical turn. **Confirm:** the local→API crossing returns
   confirm, drops `rest`, is single-use, expires, and is bound to chat + uid + target + sid +
   `fresh`; `/api fresh` then `/api confirm` starts `gen + 1` with the tail-less handoff note, never
   a resume; `/api confirm` with no pending or an expired nonce replies `Nothing to confirm…` and
   spawns nothing; a confirm re-POST with the same message ids is not treated as a continue and
   spawns nothing; the dialog lists `local_files_read` (and the `K more` count past 200).
   **Egress invariant (§5):** directly, per session; the workspace-switch sequence — local turns in
   ws A, `/ws B` while local, `use API model`, click, `/ws A` — returns the confirm for A's session
   and never spawns A on the API profile; a record deleted after a local turn is rebuilt with
   `backend = local`, `backend_source = rebuild`, and then `use API model` returns `{t: confirm}`
   while a plain task runs local; API→local with `rest` runs, and does not switch when admission
   refuses; local→API with no unconfirmed local history switches in one step. **Other:** a near
   miss spawns nothing; the message after a non-spawning reply (near miss, switch, `/status`) gets
   no "session is linear" notice; a regenerate of a non-spawning reply re-evaluates the text; no
   record and no meta with audit evidence refuses and never spawns with the API profile; a corrupt
   record affects one chat; archive and restore, including a chat archived with mtimes set 200 days
   back, which restores with every file touched to now and resumes with the same sid; no eviction
   under 10,000 chats; regenerate, continue and edited-older-message; the threshold at ±1 around
   `local_switch_max_ctx`; the interrupted note; a failed heartbeat stops the unit; a fake claude
   that emits `tool_use`, stays silent 400 s, then emits `tool_result` is **not** stopped, while
   400 s of silence with no tool outstanding is; a 403 retry after a gate refusal names the gate,
   not `secrets.env`; admission's three reason-specific replies; after the flock, admission re-runs
   every condition (a ComfyUI job in the busy window refuses); audit rows carry no contents.
3. `tests/test_code_turn_exec.py` — the kept env is exactly `--keep` ∪ {`HOME`} with
   `SSH_AUTH_SOCK`, `DBUS_SESSION_BUS_ADDRESS`, `OLLAMA_HOST` and `CLAUDE_CODE_ENTRYPOINT` set in the
   parent; exit 97 when a route exists; the forked forwarder round-trips and outlives `execve`.
4. `tests/test_code_relay_gate.py` — local + `deepseek-flash` → 403 and audit; API + the qwen tag
   → 403; a non-`/v1/messages` path → 403; the captured probe body `cap_c.jsonl.body.1` (row 44,
   `"model": "deepseek-flash"`, path `/v1/messages?beta=true`; the body only, no headers, copied into
   `tests/fixtures/`) is admitted on an API turn configured
   as `deepseek-flash[1m]`, pinning the suffix stripping; a chunked body is de-chunked and forwarded
   with a `Content-Length`; a body over 32 MiB → 413; client `Authorization` and `x-api-key` are
   replaced by the injected token (API) or `router-local` (local); the 403 body carries
   `egress_blocked`; one request per connection, with `Connection: close`; streaming passthrough.
5. `tests/test_code_workspaces.py` — `/home/ohmz`, `~/ai-stack`, hidden, symlink escape, ambiguous;
   a repo whose `.git` is a file (a worktree) is ineligible; the toolchain secret scan — a
   `cookies.dart` under `.pub-cache/hosted` and npm's bundled `.npmrc` under `~/.nvm/versions/*/lib`
   pass, a top-level `.npmrc`, a `credentials.toml` or an `id_ed25519` anywhere fail and fail the
   canary, a `scan_exceptions` entry passes; launcher parity.
6. `tests/test_code_agent_pipe.py` — Code-on prompts that today route to media ("create a video player
   component", an ascii diagram) reach the agent, including in a chat with image history; `__task__`,
   Task and Notebook still win; stamped-chat tasks get the deterministic title, tags and follow-ups
   from the stamp map with no Ollama call, and the follow-up phrase follows the backend a `done`
   event recorded; two concurrent stamp writes both survive (the sidecar lock); non-admin, unpinned
   admin, the role=user `TASK_ADMINS` account and temporary chats are excluded; the classifier alone
   never reaches the agent; files on the current message refused, a chat-level file alone does not
   block; confirm `True`/`False`/unavailable/timed out (`asyncio.wait_for`), with the three replies;
   stripping an Adaptive Memory prefix block (with an internal blank line) plus the Code Interpreter
   suffix leaves `use local model` on line 1; the pinned injection signatures (copied verbatim from
   OWUI 0.11.3's Code Interpreter prompt) surviving the strip refuse the turn; the output filters;
   valve off is byte-for-byte today's routing.
7. Adaptive Memory guard tests — inlet skips only for the pinned uid + Code + the filter's
   `CODE_AGENT` on, and is unchanged with either filter valve at its default; outlet skips for
   chats in the stamp map and is unchanged otherwise.
8. `tests/test_admission.py` — another process holds the flock; `_gpu_lock.__enter__` waits with about
   one wakeup per second.
9. `tests/test_gpuguard.py` — a held `.gpu.lock` defers the tick.
10. `tests/test_watchdog.py` — `check_code_bridge` fails on `canary_ok=false` and on no socket, and
    returns ok with detail `disabled` when the unit is not enabled and when the flag file exists;
    `check_router`; both are registered in `CHECKS`, `_checkers()` and `SEVERITY`.
11. `tests/test_stack_alert.py` — the 30-minute per-unit cooldown on top of the existing batching: a
    second failure of the same unit inside 30 minutes sends nothing, a different unit is not
    suppressed, and the same unit alerts again after 30 minutes.

### M1 — the local compaction trigger (offline, stub relay, the installed claude binary)

With the local env (`CLAUDE_CODE_MAX_CONTEXT_TOKENS=98304`, every slot the qwen tag), grow synthetic
sessions against the stub to 50K, 60K, 70K and 80K tokens and resume each; the trigger is the
smallest size whose resume emits `system/status compacting` before the turn, bracketed to 1K by
bisection. Repeat with `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` at candidate values to see whether it moves
the trigger. **Output:** the trigger T, the override value (or "no effect"), and
`local_switch_max_ctx = T − 15,000`, all pinned in the tests and in `workspaces.json` — the override
as `local_autocompact_pct_override` (§4.6), `null` when it has no effect, which leaves the variable
unset. The canary's local check (§4.10) re-proves it on every claude change.

### The canary

In-process, as §4.10 specifies. A phase exit requires `canary_ok` true on the live host.

### Live gates

- **R1 — cloud back-and-forth (phase 1, `curl --unix-socket`).** Three turns in scratch: turn 1
  plants a word, turns 2 and 3 must recall it. **Pass:** all three `is_error: false`, one `session_id`,
  recall correct; every gate request is `deepseek-flash` to `/v1/messages` (the configured
  `deepseek-flash[1m]` minus its suffix, row 44); **zero gate refusals**. A fourth turn has the agent
  Read a file of more than 256 KiB in scratch and answer a question about its end, so the next
  request body exceeds 256 KiB; it must also pass with zero gate refusals, and its framing
  (Content-Length or chunked) is recorded; it too must end `is_error: false`.
  **Measured and recorded:** chore requests per turn = gate requests − distinct assistant
  `message.id`s − `api_retry` events − requests during a `compacting` status, with and without
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
- **R2a — containment and real repos (phase 1, `curl`).** **Pass,** each:
  a real cloud turn under full production hardening uses Read, Edit, Write and Bash successfully in
  scratch; with the prompt written and stdin closed before the first event, the turn still reaches a
  `result`; a turn running `sleep 600` in Bash, with the client disconnected, leaves no unit and no
  `sleep 600` process within 10 s; after `kill -TERM` of the bridge mid-turn and a restart, no
  `ohmzai-code-*` unit exists when `/healthz` first answers; for the three most recently modified node,
  python and flutter repos under `~/StudioProjects`, the agent runs the repo's documented test command
  and the runner reports its own result — a read-only-filesystem, permission or network error on a
  path outside the workspace is a failure, fixed by a narrower `toolchains_ro` bind or a cache
  variable, never by making the home writable. **Recorded, not gating:** whether `git commit`
  succeeds under `--restricted` and the sandbox; if not, commits are the owner's, stated in
  `docs/CLAUDE_CODE.md`.
- **R2b — through OpenWebUI (phase 2).** **Pass,** each: Stop during a long Bash kills the unit within
  10 s; closing the tab mid-turn still saves the reply and footer; a 10-minute turn over
  `https://ai.ohmz.cloud` streams status lines live to the end without a reload; Regenerate and
  Continue produce the bridge-authored messages (audit shows them); a near-miss message spawns
  nothing; an attachment on the message is refused and a chat-level file alone is not; a non-admin
  account pressing Code gets today's route; a Bash command running 400 s (a `sleep 400` test) is not
  stopped by the idle rule.
- **R3 — local (phase 3, GPU otherwise idle).** A scripted three-turn local session in a real python
  repo (add a function and its test, run the tests), turns 2 and 3 sent within 60 s of the previous
  reply. **Pass,** all of: `/api/ps` snapshots every 2 s from turn start to 90 s after each reply list
  **no model other than `qwen38-coder:q4-128k`** (an empty list once the 60 s keep-alive has
  passed is a pass); no turn hits the 300 s idle or 900 s wall; chore requests = 0 per
  turn; the coder stays resident across both gaps; no CPU spill (`size_vram == size`); resuming the R1
  session on local succeeds if its `ctx ≤ local_switch_max_ctx`, otherwise the fresh-session path
  runs and says why; the local→API confirm — `True` switches with no task run, `False` replies `Still on
  local; nothing was sent`, and the gate audit shows zero cloud requests before the click; a session
  grown past the trigger shows `Compacting context…` or the `Local session is full` message.
  **Recorded in `docs/MODELS.md`:** cold load (turn start to first assistant event on a cold GPU),
  prefill seconds per request (gate: request sent to first response byte), tokens per turn.
- **R4 — GPU courtesy (phase 3).** **Pass:** a hermes cron job made due during a local turn is logged
  by gpuguard as deferred and dispatches only after the flock is released; a Task-mode message sent
  during a local turn shows `Waiting for the GPU…` and its hermes call starts only after release; an
  `auto_assistant` image request during a local turn shows the waiting status.
- **R5 — CORS (phase 0).** **Pass:** for each origin in the list, OWUI's Engine.IO polling handshake
  (`/ws/socket.io/?EIO=4&transport=polling`, the path confirmed from the container's `socket/main.py`
  first) with that `Origin` returns 200 and a `sid`; with `Origin: https://evil.example` it does not;
  a real browser on the LAN URL and on the public hostname streams an ordinary chat reply live.

## 14. Rollout

Each phase is independently reversible. The valve is off until phase 2 enables it.

**Phase 0 — prerequisites** (no Code agent yet)

- The launcher fix (§10.2) is committed.
- `coding_task` unwired and the staged coding key removed (§12).
- `deepseek-router.service` installed: stop the unsupervised router (row 28), then
  `harness/deepseek/install.sh --with-service`. Interactive `deepseek` sessions lose the relay for the
  seconds this takes.
- `stack_alert.py` per-unit cooldown, with suite 11.
- CORS list set in `compose/openwebui/run.sh`, container recreated, R5.

*Exit:* the toolsets check shows no `coding_task`; `systemctl --user is-active deepseek-router` is
active and `:8788` answers; suite 11 passes; R5 passes, or R5 failed and the change was
reverted with the pipe-side Origin check recorded as a phase 2 prerequisite.
*Rollback:* restore the three `coding_task` lines and the key copy; `systemctl --user disable --now
deepseek-router` (the launcher falls back to starting the router itself); revert the cooldown; remove
`CORS_ALLOW_ORIGIN` and re-run `run.sh`.

**Phase 1 — the bridge, cloud only, no OpenWebUI**

`harness/ohmzai-code/{bridge.py, commands.py, turn_exec.py, bridge-settings.json,
workspaces.example.json, install.sh, systemd/}` and suites 1-5 and 10; M1; the canary; the relay
gate; stage `code_bridge_key` at `/volume1/docker/openwebui/config/code_bridge_key` (0640
`root:ohmz`), because the bridge will not start without it and R1 and R2a call it with the bearer;
`check_code_bridge` and `check_router`, registered in `CHECKS`, `_checkers()` and `SEVERITY` (here
rather than phase 2, because the canary alerts through the watchdog). `local_enabled=false`.

*Exit:* offline suites green; M1 recorded; `canary_ok` true on the host; R1 and R2a pass.
*Rollback:* `systemctl --user disable --now ohmzai-code-bridge`; `check_code_bridge` then reports ok
with detail `disabled`, so the rollback raises no alert. Nothing else depends on it.

**Phase 2 — pipe branch and Adaptive Memory guard, behind `CODE_AGENT=False`**

Deploy both with `deploy_pipe.py`; point the pipe at `/app/backend/data/code_bridge_key` (the file
staged in phase 1); set `CODE_AGENT_USER_IDS` in the pipe and the filter to the admin UUID; if R5
failed, confirm the pipe-side Origin check works; run R2b; then set `CODE_AGENT=True` in the pipe
**and** the filter in the same step, and use it cloud-only for 7 days. R2b itself runs with the
pipe valve on for the admin only, and the filter's `CODE_AGENT` on for its duration.

*Exit:* suites 6-7 green; R2b passes; 7 calendar days with the valve on; every error row in the audit maps to a row in
§11; near-miss and confirm counts reviewed.
*Rollback:* `CODE_AGENT=False` in the pipe and the filter (instant). The filter's inlet guard needs
both its valves; its outlet guard and the task-guard answers affect only stamped chats.

**Phase 3 — the local backend**

*Preconditions:* the `_gpu_lock` sleep fix, Task mode taking the flock, the gpuguard probe (suites
8-9); R1's chore count at 0 with the nonessential-traffic variable — a non-zero count **blocks phase
3 in v1** (§9, §15); M1 recorded and `local_switch_max_ctx` set.

*Steps:* set `local_enabled=true` (only the pinned admin can reach a local turn: the valve, pin and
`allowed_uids` still gate every request); wait for the canary's local check to report `local_ok`
true — if it does not, set `local_enabled=false` and stop; run R3 and R4; if either fails, set
`local_enabled=false` and stop. Record the R3 numbers in `docs/MODELS.md`.

*Exit:* `local_ok` true, R3 and R4 pass, `local_enabled` left true.
*Rollback:* `local_enabled=false` (read every turn). The pipe and gpuguard changes are harmless on
their own.

**Phase 4 — documentation**

`docs/CLAUDE_CODE.md` (the Code agent, the R2a commit finding); `docs/HERMES_AGENT.md` (the admin-only
exception to "no shell on the chat-facing surface"); `harness/deepseek/README.md` (including the stale
"regular files, not the symlinks" note at `:417`); `docs/MODELS.md` (already written in phase 3); the
2026-09-23 spec and plan marked superseded (interactive part). The fork's Code landing text is left
as it is: it stays true for everyone except the pinned admin.

*Exit:* `tests/test_deployed.py` green; the docs describe the deployed state.

## 15. Open items

Only measurements that reading cannot settle remain, each with the gate that settles it and what its
result decides:

- **The local compaction trigger, and whether `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` moves it** → M1. Sets
  `local_switch_max_ctx` and `workspaces.json` `local_autocompact_pct_override` (§4.6).
- **Chore requests per turn with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`** → R1. Zero, or phase 3
  is blocked in v1 and a follow-up spec designs a chore stub, with its discriminator (for example no
  `tools` array and a small `max_tokens`, or a system-prompt marker) taken from R1's captured bodies
  and pinned by a gate test with captured chore and main-loop bodies.
- **The production containment as one piece around a real claude turn** → the canary and R2a. The
  primitives themselves coexist in one transient user unit (row 48); what remains is `turn_exec`,
  the forwarder, bwrap and a full claude turn inside it. If they cannot coexist, phase 1 does not
  exit; the fallback is a dedicated service uid with no sudo, no docker group and no access to
  `/home/ohmz`, designed separately.
- **Early stdin EOF with plain `-p`** → R2a.
- **Whether `--restricted` permits `git commit`** → R2a; recorded, not gating.
- **The real repos' tests with read-only toolchains and no network** → R2a; confirms or narrows the
  `toolchains_ro` candidates (§4.6), fills `scan_exceptions`, and adjusts the cache variables.
- **Whether a normal API session needs any gate path besides `/v1/messages`, and how large request
  bodies are framed** → R1 (zero refusals, including the >256 KiB turn).
- **Whether `metadata.user_message` carries the current message's `files`** (the attachment rule's
  comparison, §4.1) → R2b's attachment and chat-level-file cases; if it does not, the last user
  message in `body['messages']` is used.
- **The 128k coder under Claude Code:** cold load, prefill, tokens per turn, spill, and Ollama
  accepting a resumed cloud history → R3.
- **Whether OWUI's task invocations carry the chat id, and its outlet body `chat_id`** → R3's `/api/ps`
  trace; a miss shows up as `gemma3:1b` or the memory model in it.
- **Stop latency** — how fast `task.cancel()` reaches a generator parked in an aiohttp read → R2b.
- **Which origins OWUI actually serves, and Socket.IO's handshake from each** → R5; and, only if R5
  fails, whether `__request__` carries `Origin` → phase 2.

## 16. Rejected alternatives

- **Through Hermes** (finish the 2026-09-23 row). The chat would talk to a hermes agent that calls
  `coding_task`. No Claude Code continuity between calls, 2-3 swaps per turn with a local coder, a
  360 s / 32 KiB cap under hermes's 420 s batch ceiling (2026-09-23 spec §4.2), no streaming of Claude Code's progress, and a
  second LLM with a web toolset deciding what reaches a shell (row 41). It also leaves cron with a
  shell path. Hermes keeps what it is good at: scheduled work.
- **The Agent SDK** instead of the CLI. It is not installed, is a 102 MB wheel bundling its own CLI,
  and its one real gain is `can_use_tool`, which needs a live bidirectional process (row 14).
  Everything this design needs is in the CLI (row 1), with no new dependency.
- **A second router instance** (a local-only relay on another port). It duplicates a guarantee this
  design already has: every local slot is the coder, the local token is a dummy, no Task tool can pick
  another model, and the relay gate rejects any other model structurally. One more process to
  supervise, for nothing.
- **A fork change** (make Code a toggle filter that stamps metadata, changing `setMode` and
  regenerating the patch). It adds no security: `selectedFilterIds` is client-supplied exactly like
  `features.code_interpreter` (row 15), and the real gates are the pipe's role and pin checks and the
  bridge. It would cost a fork patch regeneration on every OWUI upgrade.
- **A long-lived process per chat** (row 13). Viable, but it needs a supervisor and idle reaping, does
  not survive a bridge restart, and fixes the environment at launch — so a backend switch is a respawn
  anyway. Spawn-per-turn gets all of that for free at 0.34-0.43 s a turn (row 2).
- **A denylist filesystem** (`ProtectHome=read-only` plus enumerated `InaccessiblePaths` and
  `denyRead`). The first draft's shape. It missed credentials this host really has (§3), and every
  new tool that drops a token in `~` would silently widen it. The allowlist makes absence the default.

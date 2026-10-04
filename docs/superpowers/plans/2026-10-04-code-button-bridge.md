# Code-Button Bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a real Claude Code agent behind the OhmzAI **Code** button — one resumed, sandboxed, local-by-default turn per chat message — without weakening anything the 2026-09-29 security work established.

**Architecture:** A host-side service (`harness/ohmzai-code/`, loopback only) runs one headless Claude Code turn per message on an allowlisted workspace, resuming the session per chat. The pipe cannot run Claude Code itself (the OpenWebUI container has no `claude`, `node` or `deepseek`), so it is a client of this service. Design detail lives in `docs/superpowers/specs/2026-09-29-ohmzai-code-agent-design.md` — this plan sequences it and adds the four amendments from `2026-10-04-ohmzai-coding-engine-design.md`.

**Tech Stack:** Python 3 stdlib + `http.server` (loopback), `systemd-run --user` transient units, the existing `deepseek` harness, the `auto_assistant` pipe.

**Spec:** `docs/superpowers/specs/2026-10-04-ohmzai-coding-engine-design.md` §4.4 (the amendments); `docs/superpowers/specs/2026-09-29-ohmzai-code-agent-design.md` §4–§10 (the architecture this plan builds).

## Global Constraints

- Local by default via `harness/deepseek/coding_policy.py`; the cloud only with a confirmed intent.
- Admin-only, pinned by the admin account **UUID** — not `TASK_ADMINS` (spec row 22: it matches the wrong account).
- The service binds `127.0.0.1` only. Never `0.0.0.0`.
- A turn runs inside a hardened transient user unit: `NoNewPrivileges=yes`, `InaccessiblePaths=/run/docker.sock`, `ProtectHome=read-only`, `ReadWritePaths=<workspace>`, `PrivateTmp`, and the workspace bound rw (spec row 48).
- `--restricted` on every turn (spec row 37). Never read this container's `Config.Cmd`/`Config.Env`.
- Nothing is deployed enabled by default: the bridge ships `mode = "observe"`-equivalent — the pipe's Code branch stays on the single-shot coder until the canary passes on this host.

## Review Focus

- **A chat message reaching the host.** The pipe's request must be authenticated and admin-pinned; a forged or non-admin request must be refused, not queued.
- **The workspace boundary.** A `repo` that realpaths outside the allowlist, or through a symlink, must be refused before any spawn.
- **A workspace's `.claude/settings.json`.** A repo can carry its own hooks; `--restricted` must stop them running (spec row 37 measured that without it, a SessionStart hook ran `sudo -n true`).
- **The GPU.** A local turn started while ComfyUI renders must wait, not evict a render (spec §9).
- **Session leakage.** A resumed turn must never resume a different chat's session, and a missing session id must start fresh rather than pick up another chat's history.

## Phases

The 2026-09-29 spec already sequences the research gates (R1, R2a, R3, M1 …). This plan keeps that order because each gate retires a specific unknown the design depends on (spec §2, "Research unknowns the design depends on").

### Phase A — Pure foundations (no spawn, no service)

- [ ] **A1: `commands.py`** — the §6 backend grammar, from the spec's pinned table. TDD against that table verbatim (`tests/test_code_commands.py`), including the false-positive rows (`use the api to fetch prices` is **not** a switch). No I/O.
- [ ] **A2: `workspaces.py`** — the allowlist resolver. Pure: given the roots and a `repo` string, return the realpath or a typed refusal. Tests cover symlink escape, a worktree `.git` file, the excluded `~/ai-stack`, and the per-chat scratch dir.
- [ ] **A3: `policy_gate.py`** — what may leave. Consumes `coding_policy`; refuses an unconfirmed cloud turn. Tests pin that an unconfirmed cloud request is refused here too, not only in the front door.

**Gate:** `python3 tests/test_code_commands.py && python3 tests/test_code_workspaces.py && python3 tests/test_code_gate.py` — all pass. No process is spawned by Phase A.

### Phase B — The turn runner (privileged; built but not reachable)

- [ ] **B1: `turn_exec.py`** — builds and runs the transient unit for one turn. Tests use a fake `systemd-run` on `PATH` and assert the exact argv (the hardening flags above), as `tests/test_coding_task_plugin.py` asserts the plugin's argv.
- [ ] **B2: R2a** — run one **real** sandboxed turn by hand and record it: sudo blocked, docker socket blocked, `$HOME` read-only, workspace writable, secrets unreadable. This is the gate the 2026-09-29 spec explicitly left unproven (rows 48/49). **Do not proceed past B2 until it passes on this host.**

**Gate:** B2's recorded evidence.

### Phase C — The service and the pipe (reachable)

- [ ] **C1: `bridge.py`** — the loopback HTTP service: admin-pinned auth, per-chat lock, session map, wiring A1–A3 and B1, streaming.
- [ ] **C2: the pipe branch** — `auto_assistant` calls the bridge only when `_code_mode(meta)` is set *and* the caller is the pinned admin; otherwise today's single-shot coder. The auto-route handoff affordance (§4.4) lands here.
- [ ] **C3: the confirm flow** — the §6 grammar's terminal state: cloud requires the click-confirm; `resolve()` from `coding_policy` is the enforcement point.
- [ ] **C4: the failure choices** — the bridge returns the typed reason; the pipe renders retry / single-shot local / confirmed cloud.

**Gate:** the live smoke from the design §6 — press Code, get a streamed local agent turn; hold the GPU, get the choice prompt.

### Phase D — Deploy

- [ ] **D1: the committed unit** `harness/ohmzai-code/systemd/ohmzai-code-bridge.service` + `install.sh`, mirroring `harness/deepseek/install.sh`'s contract (symlink, verify, `--uninstall`).
- [ ] **D2: the canary** (spec §4.10) registered with the stack watchdog (`scripts/stack_watchdog.py` `CHECKS` + `_checkers()`), so a dead bridge is alerted like any other unit.
- [ ] **D3:** flip the pipe's Code branch from the single-shot coder to the bridge **only after** D2 is green for a day.

## Why this plan stops before Phase B unattended

Phases B–D spawn Claude Code as a process that, on this host, is root-equivalent (spec row 32: `sudo -n -l` is `NOPASSWD: ALL`, and `ohmz` is in the `docker` group). The 2026-09-29 review found credentials no denylist had named — `~/.codex/auth.json`, browser cookie stores, `~/.local/share/keyrings`, `~/.vnc/passwd` — and the pipe's message text is the trigger. A half-verified sandbox here is not a bug; it is the whole risk. B2 exists to retire that unknown with evidence **before** anything is reachable, and it is the one step that cannot be done by reading code.

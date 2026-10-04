# Hermes coding row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-only OpenWebUI row that talks to a hermes agent on its own profile, and can be told to do coding work through the local `deepseek` CLI — interactively and as a scheduled job.

**Architecture:** One new hermes profile (`coding`), multiplexed on the existing gateway at `/p/coding/…` with its own API key and its own `platform_toolsets`. The profile's agent gets exactly one custom capability — a bounded `coding_task` tool that runs `deepseek --cloud` with a fixed list argv, never a shell. A thin OpenWebUI pipe reaches that profile; the existing delivery watcher is extended to scan both hermes roots so scheduled coding jobs actually report back.

**Tech Stack:** Python 3.11+ (stdlib only for the new code), hermes-agent v0.21.4 (git-installed at `~/.hermes/hermes-agent`), OpenWebUI 0.11.3 (`ai-stack/open-webui:task-mode`), SQLite, systemd user units, Ollama on one RTX 3090.

**Spec:** `docs/superpowers/specs/2026-09-23-hermes-coding-row-design.md` — read it with this plan. Section references (§) below point into it.

## Global Constraints

- **Python** `>=3.11,<3.14` (unchanged by the upgrade).
- **Tests in this repo are standalone scripts**, run as `python3 tests/test_x.py`, using the `check(label, ok, detail)` helper and exiting non-zero on failure. There is no pytest. Do not introduce it.
- **`pipes/hermes_coding.py` must never contain the literal strings** `from utils`, `from apps`, `from main`, `from config` — anywhere, comments and docstrings included. OpenWebUI's `replace_imports()` rewrites them on load and `scripts/deploy_pipe.py` refuses the file.
- **Never create an `access_grant` row for the coding model.** Absence is what makes it admin-only (§5.1).
- **The admin handle is the email local part**, sanitized: `omariqbal97@…` → `omariqbal97` (`pipes/auto_assistant.py:6696`). Not the display name.
- **`CODING_TASK_ROOTS = ["/home/ohmz"]`**, denylist `CODING_TASK_DENY` as §4.2 lists it. Both env-overridable.
- **Commit message trailer:** every commit ends with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- **Do not run `hermes update`.** It can run the multiplex migration and the config ladder as side effects (§7 step 3).
- New files land in the repo (`/home/ohmz/ai-stack`); nothing is edited in place under `~/.hermes` except the profile created in Task 3 and the plugin symlink.

## Review Focus

Each line is a failure mode the spec implies but no individual task's tests exercise. The owning task carries the test.

1. **The model calls `coding_task` in the same turn as another tool.** hermes caps a *concurrent* batch at 420 s, so the task is killed mid-run at that ceiling no matter what the tool was told to wait. Owning task: 4.
2. **`__user__` is `None`** — a direct API call or an eval harness rather than the UI. The pipe must refuse, not raise, and must not accidentally treat "no user" as "admin". Owning task: 6.
3. **A coding task edits the repo the pipe is deployed from**, so the next deploy ships code the reviewer never saw. The task runs with `cwd` inside `~/ai-stack` by design; the plan documents rather than prevents it. Owning task: 4.
4. **The 5-minute scheduled job fires while an image render holds the GPU.** gpuguard must defer it and keep the job, not lose it. Owning task: 2.
5. **A coding job's output arrives in the shared channel rather than the owner's**, because the coding profile writes no `job_owners.json` entry (§6). Verify it lands where §6 says it will. Owning task: 5.

---

### Task 1: Fix `gpuguard.start()` for the new provider contract

This lands **before** the upgrade and is safe on both revisions: `**kwargs` forwards nothing at v0.19.0 (the gateway passes none of the new parameters) and forwards the three new ones at v0.21.4.

**Files:**
- Modify: `hermes/plugins/gpuguard/__init__.py:231-243`
- Test: `tests/test_gpuguard.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GpuGuardCronScheduler.start(stop_event, *, adapters, loop, interval, can_dispatch, profile_homes, **kwargs)` — forwards every unknown keyword to `super().start()`, so `gateway/run.py` may pass `profile_gate`, `profile_adapters`, `default_profile` (v0.21.4) without a `TypeError`.

- [ ] **Step 1: Write the failing test**

In `tests/test_gpuguard.py`, replace the `fake_start` definition (currently `:94-96`) so it accepts and records arbitrary keywords, and add the new check after the existing composed-gate checks (after `:104`):

```python
        def fake_start(self, stop_event, *, adapters=None, loop=None, interval=60,
                       can_dispatch=None, profile_homes=None, **kwargs):
            captured["gate"] = can_dispatch
            captured["kwargs"] = kwargs
```

```python
        # v2026.9.21 gives InProcessCronScheduler.start three more keyword parameters, and
        # gateway/run.py always passes them. A subclass that names a fixed list raises TypeError on
        # every spawn; the supervised ticker then respawns in a loop and NO JOB EVER FIRES, while
        # the API keeps serving and every pipe-side check keeps passing. Asserted here because the
        # failure is otherwise invisible.
        captured.clear()
        p.start(_Stop(), can_dispatch=lambda: True, profile_gate=lambda: True,
                profile_adapters={"default": object()}, default_profile="coding")
        check("profile_* keywords are forwarded to super().start()",
              captured.get("kwargs", {}).get("default_profile") == "coding"
              and "profile_gate" in captured.get("kwargs", {}),
              repr(captured.get("kwargs")))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 tests/test_gpuguard.py`
Expected: `TypeError: start() got an unexpected keyword argument 'profile_gate'`, and the check `profile_* keywords are forwarded to super().start()` FAILs.

- [ ] **Step 3: Fix the plugin**

In `hermes/plugins/gpuguard/__init__.py`, change the signature and the forwarding call:

```python
    def start(self, stop_event, *, adapters=None, loop=None, interval=60,
              can_dispatch=None, profile_homes=None, **kwargs):
        if can_dispatch is None:
            gate = self._gpu_available
        else:
            def gate():
                return can_dispatch() and self._gpu_available()
        logger.info(
            "gpuguard cron scheduler active (probes: %s, %s; max_defer=%ds hard=%ds)",
            COMFY_QUEUE_URL, OLLAMA_PS_URL, MAX_DEFER_S, HARD_DEFER_S,
        )
        # **kwargs, not a named list: v2026.9.21 added profile_gate/profile_adapters/
        # default_profile here, and naming them would repeat this break the next time upstream adds
        # one. At v0.19.0 the gateway passes none of them, so this forwards nothing and is inert.
        super().start(stop_event, adapters=adapters, loop=loop, interval=interval,
                      can_dispatch=gate, profile_homes=profile_homes, **kwargs)
```

Also extend the module docstring's closing paragraph with one sentence:

```
The `**kwargs` on `start()` is load-bearing: v0.21.4 added `profile_gate`, `profile_adapters` and
`default_profile` to the parent's signature and the gateway always passes them. Naming them here
would work today and break on the next upstream addition.
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 tests/test_gpuguard.py`
Expected: ALL PASS, including the new check. The live ComfyUI/Ollama probes may SKIP; that is by design.

- [ ] **Step 5: Commit**

```bash
cd /home/ohmz/ai-stack
git add hermes/plugins/gpuguard/__init__.py tests/test_gpuguard.py
git commit -m "fix(gpuguard): forward new start() keywords so the ticker survives v0.21.4

InProcessCronScheduler.start gained profile_gate, profile_adapters and
default_profile, and gateway/run.py always passes them. The fixed parameter
list raised TypeError on every spawn; the supervised ticker would have been
respawned in a loop and no cron job would ever have fired again -- while the
API kept serving and every pipe-side check kept passing.

**kwargs rather than the three names, so the next upstream addition does not
repeat this.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: Upgrade hermes to v2026.9.21

Operational, not TDD. Every step is verifiable and step 8 is the one that matters — the failure mode this task exists to avoid is silent.

**Files:**
- Modify: `~/.hermes/config.yaml` (by the migration — never by hand)
- Modify: `~/.hermes/hermes-agent` (git checkout)
- Snapshot: `~/.hermes/upgrade-snapshot-2026-09-23/`

**Interfaces:**
- Consumes: Task 1's fixed plugin.
- Produces: a running gateway at `v2026.9.21`, with `platform_toolsets`, `cron.provider: gpuguard` and the profile machinery all intact.

- [ ] **Step 1: Snapshot measured state**

```bash
cd /home/ohmz/.hermes
mkdir -p upgrade-snapshot-2026-09-23
cp -a config.yaml .env cron/jobs.json alert_transports.env upgrade-snapshot-2026-09-23/ 2>/dev/null
cp -a ~/.hermes/hermes-agent/.git/HEAD upgrade-snapshot-2026-09-23/git-HEAD 2>/dev/null
cd hermes-agent && git rev-parse HEAD > /home/ohmz/.hermes/upgrade-snapshot-2026-09-23/hermes-rev
sha256sum ~/.hermes/config.yaml > /home/ohmz/.hermes/upgrade-snapshot-2026-09-23/config.sha256
ls -la ~/.hermes/upgrade-snapshot-2026-09-23/
```

Expected: the snapshot dir holds `config.yaml`, `.env`, `jobs.json`, `alert_transports.env`, `hermes-rev` (b6729ba90552f11ac1064c3c7dcb7ef20361ef8c) and `config.sha256`.

- [ ] **Step 2: Stop the units for the window**

```bash
systemctl --user stop hermes-gateway.service hermes-delivery.timer
systemctl --user is-active hermes-gateway.service hermes-delivery.timer
```

Expected: both report `inactive`.

- [ ] **Step 3: Check out the tag and re-sync the venv**

```bash
cd /home/ohmz/.hermes/hermes-agent
git fetch --tags
git status --porcelain          # must be empty; the tree is a squashed import with no local edits
git checkout v2026.9.21
git rev-parse HEAD              # d337b736aa1e8ebecfab043842d13e4a2d2f48a3
```

Re-sync the venv the way this host installs it:

```bash
cd /home/ohmz/.hermes/hermes-agent
uv sync 2>&1 | tail -5 || python3 -m venv --help >/dev/null
```

Expected: the three new wheels (`firecrawl-anydoc`, `snowballstemmer`, `pillow-heif`) resolve; Python stays 3.11–3.13.

- [ ] **Step 4: Run the config migration and diff it**

The live config has no `_config_version`, so the whole ladder applies (spec row 21).

```bash
cd /home/ohmz/.hermes/hermes-agent
hermes doctor 2>&1 | tail -20
sha256sum ~/.hermes/config.yaml
diff <(python3 -c "
import yaml,sys
d=yaml.safe_load(open('/home/ohmz/.hermes/upgrade-snapshot-2026-09-23/config.yaml'))
print(yaml.safe_dump({k:d.get(k) for k in ('platform_toolsets','cron','approvals','gateway')}, sort_keys=True))
") <(python3 -c "
import yaml
d=yaml.safe_load(open('/home/ohmz/.hermes/config.yaml'))
print(yaml.safe_dump({k:d.get(k) for k in ('platform_toolsets','cron','approvals','gateway')}, sort_keys=True))
")
```

Expected, exactly: `platform_toolsets.api_server` and `.cron` each gain `connections`; `cron.model_drift_guard` disappears; `gateway.multiplex_profile_allowlist` disappears; `_config_version: 45` appears; **`cron.provider: gpuguard` still present**; `approvals` unchanged. Any other difference is a finding to resolve before continuing.

- [ ] **Step 5: Restart and confirm the ticker survived**

```bash
systemctl --user start hermes-gateway.service hermes-delivery.timer
sleep 20
journalctl --user -u hermes-gateway.service -n 80 --no-pager | grep -iE "unexpected keyword|gpuguard|Cron ticker|traceback" 
hermes cron status
```

Expected: the log names `gpuguard cron scheduler active`; **no** `unexpected keyword argument`; **no** repeated `Cron ticker thread` restart lines; `hermes cron status` shows a live heartbeat. This is Task 1's fix proving itself — if the ticker is respawning, stop and fix it here rather than continuing.

- [ ] **Step 6: Prove a job actually fires** (Review Focus #4)

Create one bounded job and watch it run:

```bash
# v0.21.4: schedule and prompt are POSITIONAL. The v0.19.0 --schedule/--prompt flags are gone and
# the old form exits with "unrecognized arguments". (As run, 2026-09-23; see the ledger.)
hermes cron create --name "upgrade smoke test" "*/2 * * * *" \
  "Reply with exactly: LOG: upgrade smoke test fired" 2>&1 | tail -5
```

Wait for two ticks, then:

```bash
hermes cron list
ls -lt ~/.hermes/cron/output/*/ | head -5
journalctl --user -u hermes-delivery.service -n 40 --no-pager | tail -20
```

Expected: a new `.md` file appears under `~/.hermes/cron/output/<job>/`, the delivery log reports it delivered, and the background-tasks channel receives `🤖 upgrade smoke test: upgrade smoke test fired`. **Delete the job afterwards.**

- [ ] **Step 7: Confirm the contracts the pipe reads**

```bash
HERMES_KEY=$(command grep -oP '(?<=^API_SERVER_KEY=).*' ~/.hermes/.env)
test -n "$HERMES_KEY" || echo "no API_SERVER_KEY in ~/.hermes/.env — stop and find it"
curl -s -H "Authorization: Bearer $HERMES_KEY" http://127.0.0.1:8642/v1/toolsets | head -c 400
curl -s -H "Authorization: Bearer $HERMES_KEY" "http://127.0.0.1:8642/api/jobs?include_disabled=true" | head -c 300
```

Never print the key itself — it authorises the whole Task path.


Expected: both answer with a JSON body (not 401/404/500). `platform_toolsets.api_server` must still contain `cronjob`, and `.cron` must still contain `terminal`.

- [ ] **Step 8: Commit nothing — but record the outcome**

This task changes no tracked files in this repo. Note the two behaviour changes the spec predicts, so later tasks are written against reality: the omitted-`deliver` default is now `"local"`, and a dangerous api_server command is refused instantly rather than after a 300 s block.

---

### Task 3: Create the `coding` profile

**Files:**
- Create: `~/.hermes/profiles/coding/config.yaml`
- Create: `~/.hermes/profiles/coding/.env`
- Create: `~/.hermes/profiles/coding/plugins/` (symlinks)
- Create: the key's container-side copy `/volume1/docker/openwebui/config/hermes_coding_api_key`
  (↔ `/app/backend/data/hermes_coding_api_key`), staged through the container — Step 3.
  Its **host-side record is the profile's own `.env`** (Step 3), which is where it is generated.
  There is deliberately no `~/.hermes/coding_api_key` standalone file: the default profile has no
  such sibling either (its key lives in `~/.hermes/.env` and is merely *staged* for the container),
  and nothing in this plan reads a host-side path.

**Interfaces:**
- Consumes: Task 2's upgraded runtime.
- Produces: a profile reachable at `http://127.0.0.1:8642/p/coding/v1/chat/completions`, authenticated with its own `API_SERVER_KEY`; a key file the pipe (Task 6) reads.

- [ ] **Step 1: Create the profile**

```bash
hermes profile create coding
hermes profile list
ls -la ~/.hermes/profiles/coding/
```

Expected: `hermes profile create` refuses reserved names and creates `~/.hermes/profiles/coding/` with `config.yaml` (seeded with the active profile's `model:` block), `.env`, `SOUL.md`, and the dirs `memories sessions skills skins logs plans workspace cron home`. It creates **no** `plugins/` dir and **no** systemd unit — both are expected.

- [ ] **Step 2: Write the profile config**

Overwrite `~/.hermes/profiles/coding/config.yaml` with exactly this. Absent keys deep-merge from `DEFAULT_CONFIG`, so a minimal file is correct:

```yaml
# The coding profile: an admin-only agent that can do coding work through the local deepseek CLI.
# Reached at /p/coding/... on the shared listener, with its own API_SERVER_KEY. There is no
# gateway unit for this profile -- per-profile gateways are retired at v2026.9.21.
model:
  default: "hermes-genesis:agent"
  provider: "ollama"
  base_url: "http://127.0.0.1:11434/v1"
  context_length: 65536

platform_toolsets:
  # terminal, browser, shell_exec and every code-execution tool are absent BY INTENT.
  # The only way this agent reaches a shell is the bounded coding_task tool (see the plugin).
  api_server: [web, file, memory, session_search, todo, skills, coding_task]
  cron: [web, file, memory, todo, skills, coding_task]

cron:
  # Same provider as the default profile. Note this is resolved once per PROCESS from the launching
  # profile's config, so when the default gateway ticks every home it uses the default's value.
  # Both name gpuguard, so behaviour is identical -- that is a coincidence to preserve, not a
  # per-profile setting to lean on.
  provider: "gpuguard"

plugins:
  # User plugins are opt-in. Without this the plugin is discovered and then silently not loaded.
  enabled: [coding_task]
```

- [ ] **Step 3: Write the profile `.env` with its own key**

`API_SERVER_KEY` alone enrolls the listener — `API_SERVER_ENABLED` is read by nothing under `gateway/` (spec row 16). Generate a key; nothing generates one for you.

```bash
KEY=$(openssl rand -hex 32)
umask 077
printf 'API_SERVER_KEY=%s\n' "$KEY" > ~/.hermes/profiles/coding/.env
chmod 600 ~/.hermes/profiles/coding/.env
test $(wc -c < ~/.hermes/profiles/coding/.env) -gt 16 && echo "key written (not printed)"
```

The same key is what the pipe authenticates with, so it is written where the container reads it
(`/app/backend/data` ↔ the host's `/volume1/docker/openwebui/config`). **Write it through the
container, not from the host**: `config/` is `root:root` and `touch` there fails with
`Permission denied` — the existing `hermes_api_key` is `root:ohmz` for exactly this reason, and
`scripts/deploy_pipe.py` writes its sidecars the same way.

```bash
printf '%s\n' "$KEY" | docker exec -i open-webui sh -c "cat > /app/backend/data/hermes_coding_api_key"
docker exec open-webui chmod 640 /app/backend/data/hermes_coding_api_key
docker exec open-webui ls -la /app/backend/data/hermes_coding_api_key
```

Expected: the file exists, `root:1000`, mode `640` — matching `hermes_api_key` alongside it.


- [ ] **Step 4: Symlink both plugins into the profile's own plugins dir**

A profile does **not** inherit `~/.hermes/plugins/`, and `profile create` does not make the dir.

```bash
mkdir -p ~/.hermes/profiles/coding/plugins
ln -sfn /home/ohmz/ai-stack/hermes/plugins/gpuguard   ~/.hermes/profiles/coding/plugins/gpuguard
ln -sfn /home/ohmz/ai-stack/hermes/plugins/coding_task ~/.hermes/profiles/coding/plugins/coding_task
ls -la ~/.hermes/profiles/coding/plugins/
```

Expected: two symlinks. `coding_task` does not exist yet — Task 4 creates it; the dangling link is deliberate and harmless until then.

- [ ] **Step 5: Restart the gateway and prove the profile is reachable and isolated**

```bash
systemctl --user restart hermes-gateway.service
sleep 15
CODING_KEY=$(command grep -oP '(?<=API_SERVER_KEY=).*' ~/.hermes/profiles/coding/.env)
DEFAULT_KEY=$(command grep -oP '(?<=^API_SERVER_KEY=).*' ~/.hermes/.env)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CODING_KEY" \
  http://127.0.0.1:8642/p/coding/v1/models
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $DEFAULT_KEY" \
  http://127.0.0.1:8642/p/coding/v1/models
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CODING_KEY" \
  http://127.0.0.1:8642/v1/models
```

Expected, and this is spec row 14 holding in practice: `200`, then `401`, then `401`. A `404` on the first means the profile is not being served — check the gateway log. Do not proceed until this is exactly `200 / 401 / 401`.

- [ ] **Step 6: No commit**

Nothing tracked changed. The profile is host state.

---

### Task 4: The `coding_task` plugin

**Files:**
- Create: `hermes/plugins/coding_task/plugin.yaml`
- Create: `hermes/plugins/coding_task/__init__.py`
- Test: `tests/test_coding_task_plugin.py`

**Interfaces:**
- Consumes: `~/.hermes/profiles/coding/plugins/coding_task` (Task 3's symlink).
- Produces: `_handle_coding_task(args: dict, **kw) -> str` and `register(ctx) -> None`, registering tool `coding_task` into toolset `coding_task`, with `check_fn=_deepseek_available`, `is_async=False`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_task_plugin.py`:

```python
#!/usr/bin/env python3
"""The coding_task tool: a bounded capability, not a shell.

Why this file exists. The chat surface must not be able to shell out (auto_assistant.py:25-26).
This tool is the one capability the coding profile gets instead: a FIXED argv that runs the local
`deepseek` CLI. Everything asserted here is a property that turns "we pass a string to a command"
into something auditable:

  * list argv, never shell=True — so no task string can become a command;
  * the argv is exactly ["deepseek", "--cloud", "-p", task] — no operator-supplied flags;
  * `repo` must realpath INSIDE an allowlisted root, so ../../ cannot escape;
  * HOME is passed explicitly, because deepseek dies without it (it reads ~/.config/deepseek);
  * timeout, output cap, non-zero exit and a missing binary are four DISTINCT reported outcomes.

Usage:  python3 tests/test_coding_task_plugin.py
"""
import importlib.util, os, sys

PLUGIN = "/home/ohmz/ai-stack/hermes/plugins/coding_task/__init__.py"

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    spec = importlib.util.spec_from_file_location("coding_task_t", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = {}

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.clear()
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return Done()

    real_run = mod.subprocess.run
    mod.subprocess.run = fake_run
    try:
        mod._handle_coding_task({"task": "add a docstring to foo.py"})
        check("argv is a list", isinstance(calls["argv"], list), repr(calls["argv"]))
        check("argv is exactly deepseek --cloud -p <task>",
              calls["argv"] == ["deepseek", "--cloud", "-p", "add a docstring to foo.py"],
              repr(calls["argv"]))
        check("no shell", calls["kwargs"].get("shell") in (None, False), repr(calls["kwargs"].get("shell")))

        # A task string containing shell metacharacters is DATA, not a command. This is the check
        # that makes the no-shell claim mean something.
        mod._handle_coding_task({"task": "x; rm -rf / # && $(whoami) `id`"})
        check("metacharacters pass through as one argv element",
              calls["argv"][-1] == "x; rm -rf / # && $(whoami) `id`", repr(calls["argv"]))
        check("...and still exactly four elements", len(calls["argv"]) == 4, repr(calls["argv"]))

        # HOME must be explicit: deepseek reads ~/.config/deepseek/router.json and ~/.claude/.
        env = calls["kwargs"].get("env") or {}
        check("HOME is in the tool's environment", env.get("HOME") == "/home/ohmz", repr(env))
        check("PATH is in the tool's environment", "PATH" in env, repr(env))
        check("the gateway's environment is not inherited wholesale",
              "HERMES_HOME" not in env, repr(sorted(env)))

        # The working directory: default root, then an allowlisted repo, then a refusal.
        check("defaults to the first allowlisted root",
              calls["kwargs"].get("cwd") == mod.CODING_TASK_ROOTS[0], repr(calls["kwargs"].get("cwd")))
        mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/ai-stack"})
        check("an allowlisted repo is used as cwd",
              calls["kwargs"].get("cwd") == "/home/ohmz/ai-stack", repr(calls["kwargs"].get("cwd")))
        out = mod._handle_coding_task({"task": "t", "repo": "/etc"})
        check("a repo outside the roots is refused", "outside" in out.lower(), repr(out))
        check("...and nothing was executed", "argv" not in calls or calls["argv"][-1] != "t",
              repr(calls.get("argv")))
        out = mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/ai-stack/../../etc"})
        check("traversal is resolved before the check, not after", "outside" in out.lower(), repr(out))

        # The denylist applies inside an allowed root.
        out = mod._handle_coding_task({"task": "t", "repo": "/home/ohmz/.ssh"})
        check("a denylisted subtree is refused", "refus" in out.lower() or "deny" in out.lower(), repr(out))

        # An empty task is a reported error, not a subprocess with an empty prompt.
        out = mod._handle_coding_task({})
        check("a missing task is reported, not run", "task" in out.lower(), repr(out))

        # Four distinct outcomes, never a silent empty string.
        import subprocess as _sp

        def timeout_run(argv, **kwargs):
            raise _sp.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        mod.subprocess.run = timeout_run
        out = mod._handle_coding_task({"task": "t"})
        check("timeout is its own outcome", "timed out" in out.lower() or "timeout" in out.lower(), repr(out))

        def missing_run(argv, **kwargs):
            raise FileNotFoundError("deepseek")
        mod.subprocess.run = missing_run
        out = mod._handle_coding_task({"task": "t"})
        check("missing binary is its own outcome", "not found" in out.lower(), repr(out))

        def fail_run(argv, **kwargs):
            class R:
                returncode = 2
                stdout = ""
                stderr = "boom"
            return R()
        mod.subprocess.run = fail_run
        out = mod._handle_coding_task({"task": "t"})
        check("non-zero exit is its own outcome", "2" in out and "boom" in out, repr(out))

        # The output cap: a runaway build log must not fill the agent's context.
        def big_run(argv, **kwargs):
            class R:
                returncode = 0
                stdout = "x" * (mod.MAX_OUTPUT_BYTES * 3)
                stderr = ""
            return R()
        mod.subprocess.run = big_run
        out = mod._handle_coding_task({"task": "t"})
        check("stdout is capped", len(out) <= mod.MAX_OUTPUT_BYTES + 200, str(len(out)))
        check("the cap is announced, not silent", "truncat" in out.lower(), repr(out[:200]))
    finally:
        mod.subprocess.run = real_run

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 tests/test_coding_task_plugin.py`
Expected: `FileNotFoundError` / `ModuleNotFoundError` — the plugin does not exist.

- [ ] **Step 3: Write the manifest**

Create `hermes/plugins/coding_task/plugin.yaml`:

```yaml
name: coding_task
version: 1.0.0
description: "One bounded capability: hand a coding task to the local deepseek CLI."
author: ohmz
kind: standalone
provides_tools:
  - coding_task
```

`kind: standalone` and not `backend`: a bundled plugin auto-loads, a user plugin needs
`plugins.enabled`. The manifest's *presence* is what makes the plugin visible at all.

- [ ] **Step 4: Write the plugin**

Create `hermes/plugins/coding_task/__init__.py`:

```python
"""One bounded capability for the coding profile: run a task through the local deepseek CLI.

WHY THIS EXISTS
---------------
The coding agent needs a shell to reach `deepseek`, and the chat-facing surface must not have one
(`pipes/auto_assistant.py:25-26`). So instead of a shell it gets this: a FIXED argv, a pinned working
directory, an explicit environment, a timeout and an output cap. There is no argument a chat message
can supply that changes which program runs.

There is deliberately NO `pre_tool_call` hook. A plugin tool is subject to hermes's approval gate
only when the plugin's own hook returns `{"action": "approve"}`; without one, this tool is never
gated. Adding such a hook would make every coding task block for `approvals.timeout` waiting for a
decision nothing in this stack can answer.

WHAT THIS IS NOT
----------------
This is not a sandbox. `deepseek` runs Claude Code under `permissions.defaultMode: "auto"`, so once
it starts it writes files and runs commands without asking. The fixed argv means no SHELL is
involved and no string is interpolated -- that part is real and tested. The containment is the
`repo` realpath check plus the denylist, and inside an allowed root the reach is the whole root.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

logger = logging.getLogger("plugins.coding_task")

DEEPSEEK_BIN = "deepseek"

# The realpath-checked roots a `repo` argument may name. /home/ohmz by explicit decision
# (2026-09-23); env-overridable so the boundary is testable and adjustable without an edit.
CODING_TASK_ROOTS = [p for p in os.environ.get("CODING_TASK_ROOTS", "/home/ohmz").split(":") if p]

# Subtrees where a write is catastrophic and never the point of a coding task. Follows the
# precedent hermes itself sets: a hardline list that applies regardless of mode. A RECOMMENDATION,
# not a red line -- one line to drop if it ever gets in the way.
CODING_TASK_DENY = [p for p in os.environ.get(
    "CODING_TASK_DENY",
    "/.ssh:/.gnupg:/.claude:/.hermes:/.config/deepseek:/secrets",
).split(":") if p]

# UNDER the 420 s concurrent-batch cap (hermes agent/tool_executor.py). A coding task the model
# batches alongside another tool call is killed at 420 s regardless of this value, so the default
# stays under it. A single call is dispatched sequentially and is uncapped -- but the plugin cannot
# assume it will always be called alone. The gateway's inactivity window is 1800 s and is not the
# binding constraint.
DEFAULT_TIMEOUT_S = int(os.environ.get("CODING_TASK_TIMEOUT_S", "360"))
MAX_OUTPUT_BYTES = int(os.environ.get("CODING_TASK_MAX_OUTPUT_BYTES", str(32 * 1024)))

# Only what deepseek needs. HOME is REQUIRED: it reads ~/.config/deepseek/router.json and
# ~/.claude/settings.json and dies without it. Credentials are not inherited -- the wrapper sources
# ~/.config/deepseek/secrets.env itself, which is what makes a minimal env sufficient.
TOOL_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_CACHE_HOME", "TERM")

CODING_TASK_SCHEMA = {
    "name": "coding_task",
    "description": (
        "Run a coding task through the local deepseek CLI and return its output. Blocks until the "
        "task finishes, which can take several minutes. Use this for any change to code on this "
        "machine: it is the only way to run commands. Give it a self-contained instruction, and "
        "pass `repo` when the work belongs to a specific checkout."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task to perform, as a complete instruction.",
            },
            "repo": {
                "type": "string",
                "description": (
                    "Optional absolute path to run in. Must be inside an allowlisted root. "
                    "Omit to use the default working directory."
                ),
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}


def _deepseek_available() -> bool:
    return shutil.which(DEEPSEEK_BIN) is not None


def _resolve_cwd(repo: str | None) -> tuple[str | None, str | None]:
    """(cwd, error). The boundary the containment argument rests on."""
    if not repo:
        return CODING_TASK_ROOTS[0], None
    try:
        real = os.path.realpath(repo)
    except Exception as e:
        return None, f"could not resolve {repo!r}: {e}"
    if not os.path.isdir(real):
        return None, f"{repo!r} is not a directory"
    in_root = any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep)
                  for r in CODING_TASK_ROOTS)
    if not in_root:
        return None, (f"{repo!r} is outside the allowlisted roots ({', '.join(CODING_TASK_ROOTS)}) "
                      "— refused.")
    for bad in CODING_TASK_DENY:
        banned = os.path.realpath(CODING_TASK_ROOTS[0] + bad) if bad.startswith("/") else bad
        if real == banned or real.startswith(banned + os.sep):
            return None, f"{repo!r} is on the denylist — refused."
    return real, None


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[:MAX_OUTPUT_BYTES] + f"\n…truncated ({len(text)} bytes total)"


def _handle_coding_task(args: dict, **kw) -> str:
    """handler(args, **kwargs) — the registry passes extra keywords at every dispatch site, so the
    **kw is required, not decorative."""
    task = str(args.get("task") or "").strip()
    if not task:
        return "coding_task: a `task` is required."

    cwd, err = _resolve_cwd(args.get("repo"))
    if err:
        return f"coding_task: {err}"

    argv = [DEEPSEEK_BIN, "--cloud", "-p", task]
    env = {k: os.environ[k] for k in TOOL_ENV_KEYS if k in os.environ}
    env.setdefault("HOME", CODING_TASK_ROOTS[0])
    timeout = DEFAULT_TIMEOUT_S

    started = time.monotonic()
    try:
        proc = subprocess.run(          # list argv; never shell=True
            argv, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return (f"coding_task: timed out after {timeout}s — the task was not completed. "
                "Split it, or raise CODING_TASK_TIMEOUT_S.")
    except FileNotFoundError:
        return f"coding_task: {DEEPSEEK_BIN!r} not found on PATH."
    except Exception as e:
        return f"coding_task: could not start {DEEPSEEK_BIN!r}: {type(e).__name__}: {e}"
    elapsed = time.monotonic() - started

    out, errs = _cap(proc.stdout or ""), _cap(proc.stderr or "")
    if proc.returncode != 0:
        return (f"coding_task: {DEEPSEEK_BIN} exited {proc.returncode} after {elapsed:.0f}s "
                f"in {cwd}\n\n--- stdout ---\n{out}\n\n--- stderr ---\n{errs}")
    body = out.strip() or "(no output)"
    return f"coding_task finished in {elapsed:.0f}s (cwd {cwd}):\n\n{body}"


def register(ctx) -> None:
    """Called once by the plugin loader. Registers exactly one tool, and no hooks."""
    ctx.register_tool(
        name="coding_task",
        toolset="coding_task",
        schema=CODING_TASK_SCHEMA,
        handler=_handle_coding_task,
        check_fn=_deepseek_available,
        description="Delegate a coding task to the local deepseek CLI.",
        emoji="🛠️",
        is_async=False,
    )
    logger.info("coding_task plugin registered (roots=%s, timeout=%ss)",
                CODING_TASK_ROOTS, DEFAULT_TIMEOUT_S)
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `python3 tests/test_coding_task_plugin.py`
Expected: ALL PASS. Every `[PASS]`.

- [ ] **Step 6: Prove it loads in hermes, and that the toolset is what the profile sees** (Review Focus #3)

```bash
HERMES_PLUGINS_DEBUG=1 hermes -p coding plugins list 2>&1 | tail -20
```

Expected: `coding_task` listed, enabled. Then:

```bash
CODING_KEY=$(command grep -oP '(?<=API_SERVER_KEY=).*' ~/.hermes/profiles/coding/.env)
curl -s -H "Authorization: Bearer $CODING_KEY" http://127.0.0.1:8642/p/coding/v1/toolsets \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; en=[t['name'] for t in d if t.get('enabled')]; print('enabled:', ', '.join(sorted(en))); print('coding_task enabled:', 'coding_task' in en); print('terminal enabled:', 'terminal' in en)"
```

Expected: `enabled:` exactly `coding_task, file, memory, session_search, skills, todo, web`, then
`coding_task enabled: True`, then `terminal enabled: False`. **This is the check that asserts the
isolation is real rather than intended.** If the gateway was already running when the plugin was
linked, restart it first (`systemctl --user restart hermes-gateway.service`) — plugin discovery
happens at startup.

**Do not go back to a substring test on the whole body.** The endpoint enumerates *every* toolset,
enabled or not, each carrying an `enabled` flag — so `'terminal' in str(d)` is `True` on a correctly
configured box and can never produce the `False` the old form of this step expected. Read the flag,
not the name. (Fixed 2026-09-23 after the implementer hit exactly this on the live gateway and
correctly reported it rather than editing the profile config to make it pass.)

- [ ] **Step 7: Commit**

```bash
cd /home/ohmz/ai-stack
git add hermes/plugins/coding_task/ tests/test_coding_task_plugin.py
git commit -m "feat(hermes): a bounded coding_task tool for the coding profile

One capability instead of a shell: a fixed argv that runs deepseek --cloud, a
realpath-checked working directory, an explicit environment, a timeout under
hermes's 420s concurrent-batch cap, and a capped output.

No pre_tool_call hook, deliberately -- that is what keeps the tool out of the
approval gate, which a chat surface cannot answer.

Tested: argv shape, no shell, metacharacters as data, root containment
including traversal, the denylist, HOME in the env, and four distinct failure
outcomes rather than a silent empty string.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: Delivery across two hermes roots, and stop the suite writing live files

**Files:**
- Modify: `scripts/hermes_delivery.py:45-47,66-67,73,164,167,554-595`
- Modify: `tests/test_hermes_delivery.py`
- Modify: `docs/HERMES_AGENT.md:16`

**Interfaces:**
- Consumes: `~/.hermes/profiles/coding/cron/` (Task 3).
- Produces: `HERMES_ROOTS: list[str]`, `_output_files() -> list[str]`, `_jobs() -> dict` (merged across roots). Everything else keeps its current name and meaning so the existing 100 checks stay valid.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hermes_delivery.py`, before the final tally:

```python
    print("--- a second hermes root is scanned, and nothing is stranded ---")
    # A job created on the `coding` profile writes to THAT profile's hermes home. Left alone it
    # would run, satisfy its condition, and deliver nothing -- the silent-loss shape this whole
    # file exists to prevent.
    import tempfile as _tf7
    root_a, root_b = _tf7.mkdtemp(), _tf7.mkdtemp()
    for r, job, line in ((root_a, "jobA", "from A"), (root_b, "jobB", "from B")):
        os.makedirs(os.path.join(r, "cron", "output", job))
        open(os.path.join(r, "cron", "output", job, "run.md"), "w").write(
            f"## Response\nLOG: {line}\n")
    hd.HERMES_ROOTS = [os.path.join(root_a, "cron", "output"),
                       os.path.join(root_b, "cron", "output")]
    hd.OUT_DIR = hd.HERMES_ROOTS[0]
    hd.STATE = os.path.join(root_a, ".delivered.json")
    hd.ALERT_STATE = os.path.join(root_a, ".alerts.json")
    hd.ALERT_LEDGER = os.path.join(root_a, "ledger.jsonl")
    hd.JOBS_FILE = os.path.join(root_a, "missing.json")
    hd.SUBSCRIBE_INBOX = os.path.join(root_a, "subs.json")
    hd.CANCELLED = os.path.join(root_a, ".cancelled4.json")
    posted = []
    hd.post_channel = lambda summary, job, job_id=None: posted.append(summary) or True
    sys.argv = ["hd"]
    rc = hd.main()
    check("a tick with work in BOTH roots exits cleanly", rc == 0, f"rc={rc}")
    check("a file only in the SECOND root still delivers", "from B" in posted, repr(posted))
    check("...and so does the first", "from A" in posted, repr(posted))
    check("both are recorded as delivered in the ONE shared state file",
          len(json.load(open(hd.STATE))) == 2, repr(json.load(open(hd.STATE))))
    # Re-running must not re-post: the once-only guarantee survives the second root.
    posted.clear()
    hd.main()
    check("a second tick re-posts nothing", posted == [], repr(posted))

    print("--- the suite must not write to LIVE files ---")
    # It does today: job_facts() writes the real ~/.hermes/cron/output/.job_names.json during the
    # early main() calls, and every non-dry-run tick calls publish_profile(), which rewrites the
    # live alerts/profile.json the Assistant pipe reads. A test run currently leaves test-shaped
    # artifacts in live config.
    live = [os.path.expanduser("~/.hermes/cron/output/.job_names.json"),
            "/volume1/docker/openwebui/config/alerts/profile.json"]
    before = {p: (os.path.getmtime(p) if os.path.exists(p) else None) for p in live}
    d3 = _tf7.mkdtemp()
    os.makedirs(os.path.join(d3, "j3"))
    open(os.path.join(d3, "j3", "r.md"), "w").write("## Response\nLOG: isolation check\n")
    hd.HERMES_ROOTS = [d3]
    hd.OUT_DIR = d3
    hd.STATE = os.path.join(d3, ".delivered.json")
    hd.ALERT_STATE = os.path.join(d3, ".alerts.json")
    hd.ALERT_LEDGER = os.path.join(d3, "ledger.jsonl")
    hd.JOBS_FILE = os.path.join(d3, "missing.json")
    hd.NAMES_CACHE = os.path.join(d3, ".job_names.json")
    hd.SUBSCRIBE_INBOX = os.path.join(d3, "subs.json")
    hd.CANCELLED = os.path.join(d3, ".cancelled5.json")
    hd.post_channel = lambda *a, **k: True
    sys.argv = ["hd"]
    hd.main()
    after = {p: (os.path.getmtime(p) if os.path.exists(p) else None) for p in live}
    check("a tick against a temp root never touches the live name cache",
          before[live[0]] == after[live[0]], f"{before[live[0]]} -> {after[live[0]]}")
    check("...nor the live published alert profile",
          before[live[1]] == after[live[1]], f"{before[live[1]]} -> {after[live[1]]}")
```

- [ ] **Step 2: Run and watch them fail**

Run: `python3 tests/test_hermes_delivery.py`
Expected: `AttributeError: module 'hd' has no attribute 'HERMES_ROOTS'`, and — importantly — the last two checks FAIL against today's code even once the attribute exists, because the suite really does write those files.

Also confirm the damage before fixing it, so the fix is measured rather than assumed:

```bash
stat -c '%y %n' ~/.hermes/cron/output/.job_names.json /volume1/docker/openwebui/config/alerts/profile.json
```

- [ ] **Step 3: Make delivery multi-root**

In `scripts/hermes_delivery.py`, replace the constants block at `:45-47` and `:66-67`, `:73`, `:164`, `:167`:

```python
# Every hermes root whose cron output this watcher delivers. The `coding` profile (added
# 2026-09-23) writes to its own HOME, and a job there would otherwise run, satisfy its condition
# and deliver NOTHING -- the silent-loss shape this subsystem keeps being bitten by.
#
# The split is deliberate and narrow:
#   PER-ROOT  : the output glob, and jobs.json (merged for names/schedules)
#   SHARED    : .delivered.json -- its keys are absolute paths, so one file serves every root and
#               keeps the process-once guarantee -- plus the alert queue (which must stay single:
#               one lock guards the whole tick), the ledger, the tombstones and the name cache,
#               all keyed by job id already.
DEFAULT_HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_ROOTS = [p for p in os.environ.get(
    "HERMES_ROOTS",
    f"{DEFAULT_HERMES_HOME}/cron/output:{DEFAULT_HERMES_HOME}/profiles/coding/cron/output",
).split(":") if p]

OUT_DIR = HERMES_ROOTS[0]                      # the primary root; STATE et al. live here
DEFAULT_OUT_DIR = os.path.join(DEFAULT_HERMES_HOME, "cron", "output")
STATE = os.path.join(OUT_DIR, ".delivered.json")
WEBHOOK_FILE = os.path.join(DEFAULT_HERMES_HOME, "owui_webhook_url")
```

and

```python
JOBS_FILES = [os.path.join(os.path.dirname(r.rstrip("/")), "jobs.json") for r in HERMES_ROOTS]
JOBS_FILE = JOBS_FILES[0]                      # kept: tests point this at a temp path

NAMES_CACHE = os.path.join(OUT_DIR, ".job_names.json")
```

`_jobs()` and `job_titles()` currently open `JOBS_FILE` directly. Give both a merged view:

```python
def _jobs_files():
    """The jobs.json of every root, plus JOBS_FILE when a caller has redirected it.

    Tests point JOBS_FILE at a temp file; production leaves it equal to JOBS_FILES[0], so the set
    is deduplicated rather than doubling the primary root.
    """
    return list(dict.fromkeys([JOBS_FILE] + JOBS_FILES))
```

Then in `_jobs()` and `job_titles()`, replace `with open(JOBS_FILE) as f:` with:

```python
    data = {}
    for path in _jobs_files():
        try:
            with open(path) as f:
                loaded = json.load(f)
        except Exception:
            continue
        jobs = loaded if isinstance(loaded, list) else loaded.get("jobs", loaded)
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        for j in jobs:
            if isinstance(j, dict) and j.get("id"):
                data[j["id"]] = j
```

and have both return from `data` exactly as they do today.

- [ ] **Step 4: Scan every root, with the early-return evaluated once**

In `_tick()` at `:590`, replace the single glob and the pending filter:

```python
    # Every root, then ONE early-return over the union. A `return` inside a per-root loop is
    # exactly the silent-loss bug this change exists to prevent: a tick that saw "nothing new" in
    # the primary root would stop before ever looking at the profile's.
    files = sorted(f for root in HERMES_ROOTS for f in glob.glob(os.path.join(root, "*", "*.md")))
    pending = [f for f in files
               if not (state.get(f, {}).get("log") and state.get(f, {}).get("alerts"))]
    if not pending and not any(e.get("status") == "pending" for e in alert_state.values()):
        print("nothing new")
        return 0
```

- [ ] **Step 5: Stop the suite writing live files**

Two writers, both guarded by the same principle — a test must not be able to reach live state:

`job_facts()` at `:186-199` writes `NAMES_CACHE` whenever `live` is non-empty, and `NAMES_CACHE` is a
module constant captured at import — so a test that reassigns `OUT_DIR` to a temp dir leaves the
cache pointing at the real one. Derive it from `OUT_DIR` at call time instead:

```python
def names_cache_path():
    """NAMES_CACHE, unless the caller redirected OUT_DIR (as the tests do).

    NAMES_CACHE used to be a module constant computed from OUT_DIR at import, so a test that
    reassigned OUT_DIR wrote the REAL cache on every run -- which is one of the two ways this suite
    was mutating live state.
    """
    if OUT_DIR != DEFAULT_OUT_DIR:
        return os.path.join(OUT_DIR, ".job_names.json")
    return NAMES_CACHE
```

Use `names_cache_path()` everywhere `NAMES_CACHE` is currently read or written in `job_facts()`.

`publish_profile()` at `:667-672` is the second writer, and it is called on every real tick. Guard it so a redirected run cannot publish:

```python
        try:
            # Publishing is for the LIVE system only: it rewrites the profile the OpenWebUI pipe
            # reads to describe alert delivery. A test that redirects OUT_DIR must never reach it.
            if OUT_DIR == DEFAULT_OUT_DIR:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from alert_transports import publish_profile
                publish_profile()
        except Exception as e:
            print(f"profile publish skipped: {e}", file=sys.stderr)
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `python3 tests/test_hermes_delivery.py`
Expected: ALL PASS — the new checks and all 100 existing ones. Then re-run the `stat` from step 2 and confirm both live mtimes are unchanged.

- [ ] **Step 7: Fix the documented count**

`docs/HERMES_AGENT.md:16` says the suite has 70 checks. It has 100 (now 107). Correct the line rather than leaving a number that has been wrong since the feature grew.

- [ ] **Step 8: Commit**

```bash
cd /home/ohmz/ai-stack
git add scripts/hermes_delivery.py tests/test_hermes_delivery.py docs/HERMES_AGENT.md
git commit -m "feat(delivery): watch every hermes root, and stop the suite writing live files

A job on the coding profile writes to that profile's HOME, so a single
hardcoded OUT_DIR would have let it run, satisfy its condition, and deliver
nothing. Per-root: the output glob and jobs.json. Shared: the delivered-state
file (its keys are absolute paths), the alert queue -- which must stay single,
one lock guards the whole tick -- plus the ledger, tombstones and name cache.

The early-return is evaluated once over the union; a return inside a per-root
loop is the silent-loss bug this change exists to prevent.

Two live-write bugs fixed in the same pass: job_facts() rewrote the real
.job_names.json during early main() calls, and every real tick's
publish_profile() rewrote the live alerts/profile.json the Assistant pipe
reads. A test run left test-shaped artifacts in live config. Both are now
unreachable from a redirected run, and asserted.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: The pipe

**Files:**
- Create: `pipes/hermes_coding.py`
- Test: `tests/test_hermes_coding_pipe.py`
- Modify: `scripts/deploy_pipe.py:53-59` (PIPES map)
- Modify: `tests/test_deployed.py:46-55` (SOURCES map)
- Modify: `README.md` (Pipes table, after `:47`)

**Interfaces:**
- Consumes: Task 3's key file, Task 4's toolset.
- Produces: `Pipes.pipes()` → `[{"id": "coding", "name": "Hermes Coding"}]`; `Pipes.pipe(body, __user__, __event_emitter__, …)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_coding_pipe.py`:

```python
#!/usr/bin/env python3
"""The admin gate and the transport for the hermes coding row.

Why this file exists. This pipe is reachable by anyone who can select the model, and behind it is
an agent that can write files anywhere under /home/ohmz. Two things therefore have to be true and
are asserted here rather than assumed:

  * a non-admin is refused, and __user__=None (a direct API call, an eval harness) is treated as
    NOT admin rather than as "no check applies";
  * the request goes to the coding profile's path with the coding profile's key -- not the
    primary gateway's, which would route to a profile that has no coding_task tool.

Usage:  python3 tests/test_hermes_coding_pipe.py
"""
import asyncio, importlib.util, os, sys

PIPE = "/home/ohmz/ai-stack/pipes/hermes_coding.py"

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def run(coro):
    # A fresh loop, not asyncio.get_event_loop(): the latter is deprecated for this use from 3.12
    # and errors on a bare main thread in newer interpreters.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main():
    spec = importlib.util.spec_from_file_location("hc", PIPE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.Pipe()

    check("declares one selectable entry", isinstance(p.pipes(), list) and p.pipes()[0]["id"] == "coding",
          repr(p.pipes()))
    check("the picker name is the one the README must use",
          p.pipes()[0]["name"] == "Hermes Coding", repr(p.pipes()[0]["name"]))

    body = {"messages": [{"role": "user", "content": "list the files in this repo"}]}

    # __user__ = None first: the failure mode where a missing user is read as "allowed".
    out = run(p.pipe(body, __user__=None))
    check("no user is refused, not allowed", isinstance(out, str) and "admin" in out.lower(), repr(out))

    out = run(p.pipe(body, __user__={"role": "user", "email": "someone@example.com"}))
    check("a non-admin is refused", isinstance(out, str) and "admin" in out.lower(), repr(out))
    check("...and the refusal says who can", "admin" in out.lower(), repr(out))

    # An admin passes the gate and reaches the transport. Stub the call so no network happens.
    seen = {}

    async def fake_stream(text, chat_id, key, session_key):
        seen["text"] = text
        seen["chat_id"] = chat_id
        seen["key"] = key
        yield "hello"

    real = p._hermes_coding_stream
    p._hermes_coding_stream = fake_stream
    try:
        out = run(p.pipe(body, __user__={"role": "admin", "email": "ohmz@example.com"},
                         __metadata__={"chat_id": "abc-123"}))
        check("an admin reaches the transport", seen.get("text") == "list the files in this repo",
              repr(seen))
        check("...with the OpenWebUI chat id as the session key", seen.get("chat_id") == "abc-123",
              repr(seen.get("chat_id")))
        check("...and the profile's own key", seen.get("key") and len(seen["key"]) >= 16,
              repr(bool(seen.get("key"))))
        check("the answer is returned", out == "hello", repr(out))

        # The ADMIN handle, not just the role: TASK_ADMINS carries handles that qualify anyway.
        seen.clear()
        run(p.pipe(body, __user__={"role": "user", "email": "omariqbal97@example.com"},
                   __metadata__={"chat_id": "abc-123"}))
        check("a TASK_ADMINS handle qualifies without the admin role", seen.get("text") is not None,
              repr(seen))
    finally:
        p._hermes_coding_stream = real

    # Routing: the physical request must go to the profile path on the shared listener.
    check("targets the profile path on the shared listener",
          p.hermes_url.endswith("/p/coding/v1"), p.hermes_url)
    check("reads its own key file, not the primary one",
          p.key_file.endswith("hermes_coding_api_key"), p.key_file)

    # SSE parsing is the load-bearing part of the transport: a delta that is skipped loses text,
    # and a custom event name that is misread aborts the turn.
    frames = [
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        b'event: hermes.tool.progress',
        b'data: {"tool":"coding_task","emoji":"\\U0001f6e0\\ufe0f","label":"doing it","status":"running"}',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}',
        b'data: [DONE]',
    ]
    text, progress = mod._parse_frames(frames)
    check("content deltas are concatenated", text == "Hello", repr(text))
    check("tool progress is surfaced, not swallowed", progress and progress[0]["tool"] == "coding_task",
          repr(progress))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
```

- [ ] **Step 2: Run and watch it fail**

Run: `python3 tests/test_hermes_coding_pipe.py`
Expected: `FileNotFoundError: /home/ohmz/ai-stack/pipes/hermes_coding.py`.

- [ ] **Step 3: Write the pipe**

Create `pipes/hermes_coding.py`. **Check every line for the four forbidden literals before saving** — the docstring must say "reads its key" not "read from config".

```python
"""
title: Hermes Coding
author: local
version: 0.1.0
required_open_webui_version: 0.5.0
description: (Admin only) Talk to the hermes coding agent — it can run coding tasks on this machine through the local deepseek CLI. Non-blocking (async).
"""
import asyncio, json, os, re, time

# The coding profile, multiplexed on the shared listener. Its own key: the primary key is refused
# on this path (401), which is what keeps the Task-mode surface and this one apart.
HERMES_URL = "http://127.0.0.1:8642/p/coding/v1"
KEY_FILE = "/app/backend/data/hermes_coding_api_key"
OLLAMA = "http://localhost:11434"
# The 32768-ctx chat tenant. It cannot co-reside with the 65536-ctx agent runner on a 24 GB card,
# so the pipe releases it before delegating rather than letting Ollama evict mid-load.
CHAT_MODEL = "hermes-genesis:apex-compact"
# Must exceed the tool's own timeout (360 s) plus agent overhead, or the pipe gives up first and
# reports a failure while the tool is still working.
HTTP_TIMEOUT_S = 600

# The handles allowed in addition to the admin role. Email LOCAL PART, sanitized -- the same
# derivation the Assistant uses, so the two surfaces agree on who is an admin.
TASK_ADMINS = {h for h in os.environ.get("TASK_ADMINS", "ohmz omariqbal97").lower()
               .replace(",", " ").split() if h}


def _handle(user):
    u = user or {}
    local = (u.get("email") or "").split("@")[0] or (u.get("name") or "")
    return re.sub(r"[^a-z0-9_-]", "", local.lower()) or "user"


def _parse_frames(lines):
    """(text, progress) from one SSE stream.

    Only `data:` lines carry a payload; `event:` names the frame that follows. A frame named
    hermes.tool.progress is progress, not content -- misreading it as content would print the
    agent's tool chatter into the answer.
    """
    text, progress, pending_event = "", [], None
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line:
            continue
        if line.startswith("event:"):
            pending_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            d = json.loads(payload)
        except Exception:
            pending_event = None
            continue
        if pending_event == "hermes.tool.progress":
            progress.append(d)
        else:
            for ch in (d.get("choices") or []):
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    text += piece
        pending_event = None
    return text, progress


class Pipe:
    def __init__(self):
        self.hermes_url = HERMES_URL
        self.key_file = KEY_FILE

    def pipes(self):
        return [{"id": "coding", "name": "Hermes Coding"}]

    def _key(self):
        try:
            with open(self.key_file) as f:
                return f.read().strip()
        except Exception:
            return ""

    def _release_chat_tenant(self):
        """Best-effort unload of the chat tenant so the agent runner has room."""
        try:
            import requests
            requests.post(f"{OLLAMA}/api/generate",
                          json={"model": CHAT_MODEL, "keep_alive": 0}, timeout=10)
        except Exception:
            pass

    def _last_user(self, messages):
        for m in reversed(messages or []):
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c
                                if isinstance(p, dict) and p.get("type") == "text").strip()
        return ""

    async def _hermes_coding_stream(self, text, chat_id, key, session_key):
        import aiohttp
        # Only the NEW turn. X-Hermes-Session-Id REPLACES: the server discards whatever history is
        # posted and rebuilds context from its own transcript for this session id, so resending the
        # conversation would be ignored work.
        payload = {"model": "hermes-agent", "stream": True,
                   "messages": [{"role": "user", "content": text}]}
        headers = {"Authorization": f"Bearer {key}", "X-Hermes-Session-Id": f"owui-{session_key}",
                   "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{self.hermes_url}/chat/completions", json=payload,
                              headers=headers) as r:
                if r.status != 200:
                    body = (await r.text())[:300]
                    yield f"⚠️ hermes coding agent HTTP {r.status}: {body}"
                    return
                buf = []
                async for line in r.content:
                    buf.append(line)
                text_out, _progress = _parse_frames(buf)
                if not text_out:
                    yield "⚠️ the coding agent returned nothing."
                    return
                yield text_out

    async def pipe(self, body, __metadata__=None, __event_emitter__=None, __event_call__=None,
                   __user__=None, __task__=None):
        user = __user__ or {}
        role = str(user.get("role", "")).lower()
        # Deny by default: no user at all is refused, not waved through. A direct API call and an
        # eval harness both arrive without one.
        if role != "admin" and _handle(user) not in TASK_ADMINS:
            return ("⚠️ This row is admin-only. The hermes coding agent can run coding tasks on "
                    "this machine, so it is restricted to administrators.")

        text = self._last_user(body.get("messages", []))
        if not text:
            return "Tell me what to build or change."

        key = self._key()
        if not key:
            return (f"⚠️ The hermes coding key is missing (`{self.key_file}`). Is the coding "
                    "profile set up on this host?")

        chat_id = ((__metadata__ or {}).get("chat_id") or "default")
        await asyncio.to_thread(self._release_chat_tenant)
        out = ""
        async for chunk in self._hermes_coding_stream(text, chat_id, key, chat_id):
            out += chunk
        return out
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 tests/test_hermes_coding_pipe.py`
Expected: ALL PASS.

Then the deploy guard, which is a hard gate rather than a style preference:

```bash
cd /home/ohmz/ai-stack
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from deploy_pipe import check_replace_imports
bad = check_replace_imports(open('pipes/hermes_coding.py').read(), 'pipes/hermes_coding.py')
print('forbidden literals:', bad or 'none')
"
```

Expected: `none`. Any hit must be reworded before the file can ship.

- [ ] **Step 5: Register the pipe for deploy, drift-checking and the README**

In `scripts/deploy_pipe.py`, add to `PIPES`:

```python
    "hermes_coding":  ("pipes/hermes_coding.py",  "pipes/live/hermes_coding.py"),
```

In `tests/test_deployed.py`, add to `SOURCES`:

```python
    "hermes_coding":  "pipes/live/hermes_coding.py",
```

In `README.md`, in the Pipes table after the `auto_assistant` row, add — the name cell must match
`pipes()[0]["name"]` exactly, because `test_deployed.py` compares them:

```markdown
| `hermes_coding` | Hermes Coding | *(admin only)* Talk to the hermes coding agent, which can carry out coding tasks on this machine through the local `deepseek` CLI. Runs on its own hermes profile reached at `/p/coding/…` with its own key, whose toolset has `coding_task` and deliberately **no** `terminal`. Multi-turn continuity per chat. Not selectable by non-admins: no `access_grant` row exists for it, which is what enforces that. |
```

- [ ] **Step 6: Commit**

```bash
cd /home/ohmz/ai-stack
git add pipes/hermes_coding.py tests/test_hermes_coding_pipe.py scripts/deploy_pipe.py tests/test_deployed.py README.md
git commit -m "feat(pipe): an admin-only row that talks to the hermes coding agent

Targets the coding profile on the shared listener with its own key. Deny by
default: a missing __user__ is refused, not waved through, since a direct API
call arrives without one. Sends only the new turn under X-Hermes-Session-Id,
which REPLACES rather than appends, and releases the chat tenant first because
the agent runner cannot co-reside with it on the 3090.

Registered in the PIPES and SOURCES maps so it inherits the existing deploy,
drift-test and rollback machinery, and documented in the README roster that
test_deployed.py compares against.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: Create the row and deploy

**Files:**
- Create: the `function` row in `/volume1/docker/openwebui/config/webui.db`
- Create: `pipes/live/hermes_coding.py` (by the deploy script)

**Interfaces:**
- Consumes: Task 6's pipe file.
- Produces: a live `hermes_coding` function, active, with **no** `access_grant` row.

- [ ] **Step 1: Confirm the row does not exist yet**

`deploy_pipe.py` refuses a pipe whose row is missing, so this is a deliberate one-time creation:

```bash
sudo -n sqlite3 "file:/volume1/docker/openwebui/config/webui.db?mode=ro" \
  "select count(*) from function where id='hermes_coding';"
```

Expected: `0`.

- [ ] **Step 2: Create the row**

Copy the shape of an existing pipe row, taking `user_id` from `auto_assistant` so the row is owned
by the same admin (which is also what makes it visible to admins — §5.1):

```bash
DB=/volume1/docker/openwebui/config/webui.db
sudo -n sqlite3 "$DB" "
insert into function (id, user_id, name, type, content, meta, valves, is_active, is_global,
                      created_at, updated_at)
select 'hermes_coding', user_id, 'hermes_coding', 'pipe', '', '{}', '{}', 1, 0,
       strftime('%s','now'), strftime('%s','now')
from function where id='auto_assistant';"
sudo -n sqlite3 "file:$DB?mode=ro" \
  "select id, type, is_active, user_id, length(content) from function where id='hermes_coding';"
```

Expected: one row, `type=pipe`, `is_active=1`.

- [ ] **Step 3: Deploy it**

```bash
cd /home/ohmz/ai-stack
python3 scripts/deploy_pipe.py hermes_coding --dry-run
python3 scripts/deploy_pipe.py hermes_coding
```

Expected: the dry run preflights clean and reports a manifest version; the real run deploys and
syncs `pipes/live/hermes_coding.py`. `deploy_pipe.py` runs OpenWebUI's own `extract_frontmatter`
and `replace_imports` inside the container, so a file that cannot load is rejected rather than
stored.

- [ ] **Step 4: Verify with the drift test**

```bash
python3 tests/test_deployed.py
```

Expected: the new row matches `pipes/live/hermes_coding.py`, that matches `pipes/hermes_coding.py`,
and the README names it `Hermes Coding`. **Note:** this test also asserts every *pre-existing* row
is byte-current. If it reports failures for pipes this work did not touch, that is pre-existing
drift — report it rather than re-deploying those pipes as a side effect.

- [ ] **Step 5: Prove the admin-only gate** (spec §5.1)

```bash
DB=/volume1/docker/openwebui/config/webui.db
sudo -n sqlite3 "file:$DB?mode=ro" "select count(*) from access_grant where resource_id like 'hermes_coding%';"
```

Expected: `0` — and that zero is the enforcement, not an oversight. Then confirm the row is
visible to the admin account in the picker and **absent** for a non-admin account. If a non-admin
can see it, do not "fix" it by editing the row: find out why the grant list is being ignored.

- [ ] **Step 6: No commit**

The DB row is host state; the pipe file and its maps were committed in Task 6.

---

### Task 8: Documentation

**Files:**
- Modify: `docs/HERMES_AGENT.md`
- Modify: `docs/MODELS.md`
- Modify: `docs/openwebui-config-snapshot.md`
- Modify: `docs/CLAUDE_CODE.md:56`
- Modify: `README.md`

- [ ] **Step 1: `docs/HERMES_AGENT.md`**

Add a section covering: the `coding` profile at `/p/coding/…` and why it has no gateway unit; the
two-key boundary; the profile's toolset with `coding_task` and no `terminal`; the `coding_task`
tool's containment and what it is *not* (not a sandbox — `deepseek` runs under
`defaultMode: "auto"`); the delivery watcher now scanning both roots; and the corrected suite count
from Task 5.

- [ ] **Step 2: `docs/MODELS.md`**

Record the coding profile's agent model (`hermes-genesis:agent`, same as primary) and that it is a
second distinct runner competing for the same card, which is why the pipe releases the chat tenant
and gpuguard continues to gate cron.

- [ ] **Step 3: `docs/openwebui-config-snapshot.md`**

Add the new function row and its picker name, and state plainly that **no** `access_grant` row
exists for it — so the next person to read the file knows the absence is the control.

- [ ] **Step 4: `docs/CLAUDE_CODE.md:56` — the stale credential claim**

The line says the token lives in `~/.bashrc` and nowhere else, and the wrapper's comment says
`secrets.env` does not exist. Both are false: `~/.config/deepseek/secrets.env` exists (0600) and is
what makes auth resolve — which is exactly why a shell with no `~/.bashrc` still reports
`auth: present`. Correct both, and note the precedence the wrapper documents: `secrets.env` is
sourced, so the file **overwrites** an ambient variable of the same name.

- [ ] **Step 5: `README.md`**

Extend the "Standing jobs and alerts" section with one line: coding tasks can be scheduled, and
their results deliver through the same watcher as every other job.

- [ ] **Step 6: Commit**

```bash
cd /home/ohmz/ai-stack
git add docs/HERMES_AGENT.md docs/MODELS.md docs/openwebui-config-snapshot.md docs/CLAUDE_CODE.md README.md
git commit -m "docs(hermes): the coding row, its profile, and the credential claim that was wrong

CLAUDE_CODE.md:56 says the token lives in ~/.bashrc and nowhere else, and the
deepseek wrapper's own comment says secrets.env does not exist. Both are stale:
the file exists, is 0600, and is what makes auth resolve -- which is why a
shell with no ~/.bashrc still reports auth: present.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: End-to-end verification

Nothing here is committed. This is the task that decides whether the feature exists, and every
check is one the spec named.

- [ ] **Step 1: The isolation checks (Review Focus: none — this is §8's "strongest check")**

```bash
CODING_KEY=$(command grep -oP '(?<=API_SERVER_KEY=).*' ~/.hermes/profiles/coding/.env)
DEFAULT_KEY=$(command grep -oP '(?<=^API_SERVER_KEY=).*' ~/.hermes/.env)
echo -n "coding key  -> /p/coding/ : "; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CODING_KEY" http://127.0.0.1:8642/p/coding/v1/toolsets
echo -n "default key -> /p/coding/ : "; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $DEFAULT_KEY" http://127.0.0.1:8642/p/coding/v1/toolsets
echo -n "coding key  -> /v1/       : "; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CODING_KEY" http://127.0.0.1:8642/v1/toolsets
echo "toolsets:"; curl -s -H "Authorization: Bearer $CODING_KEY" http://127.0.0.1:8642/p/coding/v1/toolsets | head -c 300
```

Expected: `200`, `401`, `401`, and a toolset list containing `coding_task` and **not** `terminal`.

- [ ] **Step 2: One interactive coding task, through the row**

In OpenWebUI, as the admin, select **Hermes Coding** and ask for something small and verifiable —
"create `~/ai-stack/.coding-smoke-test` containing the current date". Expected: the agent calls
`coding_task`, the file appears, and the answer reports the outcome. Confirm the file's contents
are real rather than described.

- [ ] **Step 3: Confirm the row is invisible to a non-admin**

Log in as a non-admin account, or query as one:

```bash
DB=/volume1/docker/openwebui/config/webui.db
sudo -n sqlite3 "file:$DB?mode=ro" "select id, is_active from model where id like 'hermes_coding%';"
sudo -n sqlite3 "file:$DB?mode=ro" "select count(*) from access_grant where resource_id like 'hermes_coding%';"
```

Expected: no grant. The picker must not offer the row to that account — if it does, stop and treat
it as a security finding, not a configuration nuisance.

- [ ] **Step 4: One scheduled coding job, delivered** (Review Focus #5)

```bash
hermes -p coding cron create --name "coding smoke test" "*/5 * * * *" \
  "Reply with exactly: LOG: coding profile job fired" 2>&1 | tail -5
```

Wait one cadence, then check that it fired, that the output landed in the profile's own root, and
that the delivery watcher picked it up:

```bash
ls -lt ~/.hermes/profiles/coding/cron/output/*/ | head -5
journalctl --user -u hermes-delivery.service -n 40 --no-pager | tail -20
```

Expected: a new `.md` under the **profile's** output dir, and a delivery log line showing it
delivered. Per §6 the job has no owner entry, so it lands in the shared background-tasks channel —
which is admin-only, which is why that is acceptable. **Delete the job afterwards.**

- [ ] **Step 5: Confirm nothing regressed on the Task path**

The upgrade touched a shared process, so the existing product gets the last check:

```bash
python3 tests/test_deployed.py
python3 tests/test_gpuguard.py
python3 tests/test_hermes_delivery.py
python3 tests/test_coding_task_plugin.py
python3 tests/test_hermes_coding_pipe.py
```

Then, in OpenWebUI as an ordinary user, send one normal chat and one Task-mode job, and confirm both
behave as before. A green suite plus a clean `git status` has read as "shipped" on this box before
when it was not.

---

## Self-review

**Spec coverage.** §4.1 → Task 3. §4.2 → Tasks 4, 7. §4.3 → Tasks 6, 7. §4.4 → Task 8. §5.1 → Tasks
6, 7, 9. §5.2 → Task 4 (the docstring states the residual risk rather than implying sandboxing) and
Task 8. §5.3 → Task 9 step 1. §6 → Task 5. §7 step 1 → Task 2; §7 step 2 → Tasks 3-6. §8 → the test
files in Tasks 4, 5, 6 and the checks in Task 9. §9's three open items: the SSE event-name question
is Task 6 step 4 onward and Task 9 step 2; the `connections` observation is Task 2 step 4; the
`/v1/responses` boundary is honoured by Task 6 step 3 never touching it.

**Placeholders.** None: every step carries the code or the command it needs. The one deliberate
"reword as needed" note is Task 6 step 3's reminder about the four forbidden literals, which is a
constraint on prose rather than an unfinished step.

**Type consistency.** `HERMES_ROOTS` (Task 5) is a list of *output* directories, matching what
`glob` needs; `JOBS_FILES` derives `jobs.json` from their parent by `os.path.dirname`, which is why
the roots carry `/cron/output`. `names_cache_path()` is used by `job_facts()` in place of the
constant. `_handle_coding_task` is the name both the plugin and its test use. `_parse_frames` and
`_hermes_coding_stream` are the names the pipe test stubs. Task 4's `MAX_OUTPUT_BYTES` and
`CODING_TASK_ROOTS` are the names its test asserts against.

**Review Focus coverage.** 1 → Task 4 step 5 (`CODING_TASK_TIMEOUT_S` documented as a ceiling, and
the default under 420 s). 2 → Task 6 step 1 (`__user__=None` refused). 3 → Task 4 step 6 and Task 8
(the risk is documented, and the toolset check proves `terminal` is absent). 4 → Task 2 step 6.
5 → Task 9 step 4.

# Coding Policy + CLI Alias Table — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put one shared coding policy in the deepseek harness and make the local model reachable from a cloud `deepseek` session, so "local by default, cloud only on purpose" is written once.

**Architecture:** A pure module, `harness/deepseek/coding_policy.py`, answers *which backend*, *which model and window*, and *why it failed*. The launcher (`harness/deepseek/deepseek`) reads it for the alias table; the Hermes `coding_task` plugin consumes the local backend it names. `router.py`'s routing-by-name is untouched.

**Tech Stack:** Python 3 (stdlib only), bash, the existing deepseek launcher and relay. This repo's tests are **standalone programs** (`python3 tests/test_x.py`), NOT pytest — each defines `check(name, cond, detail)`, prints `N checks — ALL PASS`, and ends `sys.exit(main())`.

**Spec:** `docs/superpowers/specs/2026-10-04-ohmzai-coding-engine-design.md` §4.1, §4.2, §4.3, §5, §10

## Global Constraints

- Local default model: `qwen38-coder:q4-128k`, `context_window` 131072, `output_reserve` 32768 (copied from `router.json`).
- Cloud model: `deepseek-flash[1m]`.
- A cloud backend is returned **only** when the caller passes `confirmed=True`. The policy never infers from text.
- Failure reasons are exactly: `gpu_busy`, `model_missing`, `harness_error`, `context_overflow`, `cancelled`.
- `haiku` must keep following the launch provider (cloud session stays cloud) — the 2026-09-24 GPU guard.
- No new dependencies. Stdlib only.

## Review Focus

- **An unconfirmed cloud request.** A caller that asks for `cloud` without `confirmed=True` must be refused, not quietly upgraded — the whole "no API by accident" claim rests on it.
- **A cloud session's background traffic.** After the alias change, the *non-main* aliases in a cloud session must still be cloud; if `haiku` drifted to local, titles/classifier/subagents hit the 3090 (the 2026-09-24 incident).
- **The `--model ANYTHING` path.** `deepseek --model hermes-genesis:agent` must still launch, and the launcher must not crash on a name the policy table does not know.
- **An unknown backend string.** `resolve("api")` / `resolve("")` must fail loudly, not fall back to local silently.

---

### Task 1: The `coding_policy` module

**Files:**
- Create: `harness/deepseek/coding_policy.py`
- Test: `tests/test_coding_policy.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DEFAULT_BACKEND` (`"local"`), `LOCAL_MODEL`, `CLOUD_MODEL`, `FailureReason`, `BackendRefused(Exception)`, `resolve(backend=None, confirmed=False) -> dict` with keys `backend`, `model`, `context_window`, `output_reserve`; `reason_from(text) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_policy.py`:

```python
#!/usr/bin/env python3
"""Coding policy: local by default, cloud only on an explicit confirmed ask.

Standalone, like every suite here:  python3 tests/test_coding_policy.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "coding_policy", os.path.join(HERE, "..", "harness", "deepseek", "coding_policy.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"   {detail!r}"))


def main():
    print("--- the default is local ---")
    r = cp.resolve()
    check("no argument resolves to the local backend", r["backend"] == "local", r)
    check("...on the local coder", r["model"] == cp.LOCAL_MODEL, r)
    check("...with the 128k window", r["context_window"] == 131072, r)
    check("...and its reserve", r["output_reserve"] == 32768, r)
    check("DEFAULT_BACKEND is local", cp.DEFAULT_BACKEND == "local", cp.DEFAULT_BACKEND)

    print("\n--- cloud needs a confirmation ---")
    try:
        cp.resolve("cloud")
        check("an unconfirmed cloud ask is refused", False, "no exception")
    except cp.BackendRefused:
        check("an unconfirmed cloud ask is refused", True)
    r = cp.resolve("cloud", confirmed=True)
    check("a confirmed cloud ask resolves", r["backend"] == "cloud" and r["model"] == cp.CLOUD_MODEL, r)
    check("...and carries no local window",
          r["context_window"] is None and r["output_reserve"] is None, r)

    print("\n--- the local ask is never refused ---")
    check("explicit local needs no confirmation", cp.resolve("local")["backend"] == "local")

    print("\n--- unknown input fails loudly ---")
    for bad in ("api", "", "Cloud", "gpu"):
        try:
            cp.resolve(bad)
            check(f"{bad!r} is refused", False, "no exception")
        except cp.BackendRefused:
            check(f"{bad!r} is refused", True)

    print("\n--- the reason vocabulary is exactly the five ---")
    check("FailureReason is the pinned set",
          cp.FailureReason == {"gpu_busy", "model_missing", "harness_error",
                               "context_overflow", "cancelled"}, cp.FailureReason)
    check("a busy-GPU message maps", cp.reason_from("GPU is busy rendering") == "gpu_busy")
    check("a missing model maps", cp.reason_from("model not found") == "model_missing")
    check("an Ollama context error maps",
          cp.reason_from("exceeds the available context size") == "context_overflow")
    check("an unrelated string maps to nothing", cp.reason_from("hello") is None)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_coding_policy.py`
Expected: FAIL — `FileNotFoundError` on `harness/deepseek/coding_policy.py`.

- [ ] **Step 3: Write minimal implementation**

Create `harness/deepseek/coding_policy.py`:

```python
#!/usr/bin/env python3
"""The coding policy: which backend a coding turn uses, which model, and why it failed.

Pure and I/O-free on purpose. The launcher (harness/deepseek/deepseek), the Hermes
coding_task plugin and (later) the Code-button bridge all read this, so "local by
default, cloud only on purpose" is written once instead of three times.

It does NOT route. router.py still maps a model NAME to an upstream; this module
answers what a front door should ASK FOR. The distinction is why an unknown model
name can still never bill the API (router.json's `"*"` catches it) while an
unconfirmed cloud ask here is refused outright.

    python3 -c "import coding_policy as c; print(c.resolve())"
"""
from __future__ import annotations

import re

DEFAULT_BACKEND = "local"

LOCAL_MODEL = "qwen38-coder:q4-128k"
LOCAL_CONTEXT_WINDOW = 131072
LOCAL_OUTPUT_RESERVE = 32768

CLOUD_MODEL = "deepseek-flash[1m]"

# The exact vocabulary a front door renders choices from. A free string would let
# two front doors disagree about what "busy" is called.
FailureReason = {"gpu_busy", "model_missing", "harness_error", "context_overflow", "cancelled"}

_REASONS = (
    ("gpu_busy",         re.compile(r"gpu is busy|gpu busy|render(ing)? in progress", re.I)),
    ("model_missing",    re.compile(r"model not found|no such model|not found on PATH", re.I)),
    ("context_overflow", re.compile(r"exceeds the available context size|context length|too long", re.I)),
    ("cancelled",        re.compile(r"cancel", re.I)),
    ("harness_error",    re.compile(r"traceback|exited [1-9]|harness", re.I)),
)


class BackendRefused(Exception):
    """Raised when a backend is asked for without the intent it requires."""


def resolve(backend: str | None = None, confirmed: bool = False) -> dict:
    """(backend, model, context_window, output_reserve) for a coding turn.

    Defaults to local. `cloud` requires `confirmed=True` — the policy never infers
    intent from wording, and an unanswered or unknown backend is refused rather
    than downgraded, because a silent downgrade is the failure this module exists
    to prevent.
    """
    backend = DEFAULT_BACKEND if backend is None else backend
    if backend == "local":
        return {"backend": "local", "model": LOCAL_MODEL,
                "context_window": LOCAL_CONTEXT_WINDOW, "output_reserve": LOCAL_OUTPUT_RESERVE}
    if backend == "cloud":
        if not confirmed:
            raise BackendRefused("cloud requires an explicit confirmation")
        # No window is pinned here: Claude Code keeps its own for the cloud model,
        # and advertising the local one would truncate the cloud model's context.
        return {"backend": "cloud", "model": CLOUD_MODEL,
                "context_window": None, "output_reserve": None}
    raise BackendRefused(f"unknown backend {backend!r} (expected 'local' or 'cloud')")


def reason_from(text: str | None) -> str | None:
    """Map a failure message to one of FailureReason, or None when it names none.

    None is a real answer: a front door must be able to say "failed, cause unknown"
    rather than guess a reason and offer the wrong choices.
    """
    for reason, rx in _REASONS:
        if text and rx.search(text):
            return reason
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_coding_policy.py`
Expected: `20 checks — ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add harness/deepseek/coding_policy.py tests/test_coding_policy.py
git commit -m "feat(coding): a shared coding policy — local by default, cloud only on confirmation"
```

---

### Task 2: The symmetric alias table in the launcher

**Files:**
- Modify: `harness/deepseek/deepseek` (the `ANTHROPIC_DEFAULT_*_MODEL` block, ~line 168-175)
- Test: `tests/test_coding_policy.py` (extend with a launcher-text check)

**Interfaces:**
- Consumes: `coding_policy.LOCAL_MODEL`, `CLOUD_MODEL`.
- Produces: nothing new — the launcher's exported `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_coding_policy.py`, inside `main()` before the summary:

```python
    print("\n--- the launcher binds the symmetric alias table ---")
    launcher = open(os.path.join(HERE, "..", "harness", "deepseek", "deepseek")).read()
    check("sonnet is the local coder in the cloud branch",
          'export ANTHROPIC_DEFAULT_SONNET_MODEL="$LOCAL_MODEL"' in launcher)
    check("haiku still follows the launch provider (cloud session stays cloud)",
          'export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLOUD_MODEL"' in launcher)
    check("opus is always the cloud",
          'export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLOUD_MODEL"' in launcher)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_coding_policy.py`
Expected: FAIL on "sonnet is the local coder in the cloud branch".

- [ ] **Step 3: Write minimal implementation**

In `harness/deepseek/deepseek`, replace the alias block's `if/else` body:

```bash
# The alias slots are not spare copies of ANTHROPIC_MODEL: they are what Claude Code
# resolves its *other* traffic through -- background chores, session titles, the
# auto-mode classifier, subagents. Left pointing at the local tags in a cloud
# session they silently put all of it on the 3090 (measured 2026-09-24: 2.3k calls
# at a ~29k-token prefill each, GPU pinned at 99%, 383W, while the session model was
# cloud).
#
# SYMMETRIC TABLE (2026-10-04): `sonnet` is ALWAYS the local coder and `opus` is
# ALWAYS the cloud, in either session, so /model means one thing wherever you are:
#   /model sonnet -> local   /model opus -> cloud
# `haiku` deliberately still follows the launch provider: it carries the background
# chores, and in a cloud session those must stay off the GPU. That is the guard the
# 2026-09-24 incident bought.
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLOUD_MODEL"
if [[ "$PROVIDER" == "$CLOUD_PROVIDER" ]]; then
  export ANTHROPIC_DEFAULT_SONNET_MODEL="$LOCAL_MODEL"
  export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLOUD_MODEL"
else
  export ANTHROPIC_DEFAULT_SONNET_MODEL="$LOCAL_MODEL"
  export ANTHROPIC_DEFAULT_HAIKU_MODEL="$FAST_MODEL"
fi
```

(The `export ANTHROPIC_DEFAULT_OPUS_MODEL` line already exists above the block — keep it there and remove the duplicate if the block has one.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_coding_policy.py`
Expected: `23 checks — ALL PASS`

- [ ] **Step 5: Verify the alias table live**

Run: `deepseek --status`
Expected: unchanged table, but confirm no error. Then, in a cloud session: `/model` shows Sonnet; selecting it must reach `qwen38-coder:q4-128k` (`deepseek --routes qwen38-coder:q4-128k` prints the `local` upstream).

- [ ] **Step 6: Commit**

```bash
git add harness/deepseek/deepseek tests/test_coding_policy.py
git commit -m "feat(cli): /model sonnet is the local coder in any session; haiku keeps following the launch provider"
```

---

### Task 3: The Hermes coding tool runs local

**Files:**
- Modify: `hermes/plugins/coding_task/__init__.py` (the `argv` at ~line 134, and the module docstring)
- Test: `tests/test_coding_task_plugin.py`

**Interfaces:**
- Consumes: the `deepseek --local` launcher path.
- Produces: the plugin's argv, now `["deepseek", "--local", "-p", task]`.

- [ ] **Step 1: Write the failing test**

In `tests/test_coding_task_plugin.py`, find the assertion that pins the argv (search `--cloud`) and change it to:

```python
    check("argv is exactly deepseek --local -p <task>",
          calls["argv"] == ["deepseek", "--local", "-p", "t"] or
          calls["argv"] == ["deepseek", "--local", "-p", task_used], repr(calls["argv"]))
    check("no cloud flag reaches a background tool",
          "--cloud" not in calls["argv"], repr(calls["argv"]))
```

(Match the surrounding helper names — the existing test already records `calls["argv"]`; reuse them rather than inventing new ones.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_coding_task_plugin.py`
Expected: FAIL — the argv still carries `--cloud`.

- [ ] **Step 3: Write minimal implementation**

In `hermes/plugins/coding_task/__init__.py`:

```python
    argv = [DEEPSEEK_BIN, "--local", "-p", task]
```

And in the module docstring, replace the cloud sentence with:

```
A background tool has no interactive channel on which to confirm a cloud turn, so it runs
LOCAL always: `deepseek --local`, whose model the harness's coding policy names
(harness/deepseek/coding_policy.py, default qwen38-coder:q4-128k). The cloud is not reachable
from here by construction, not by policy.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_coding_task_plugin.py`
Expected: `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add hermes/plugins/coding_task/__init__.py tests/test_coding_task_plugin.py
git commit -m "feat(hermes): the coding tool runs local; a background tool has no channel to confirm cloud"
```

---

## Self-Review

**Spec coverage:** §4.1 → Task 1. §4.2 → Task 2. §4.3 → Task 3. §5's reason vocabulary → Task 1. §10's delivery order → these three tasks are piece 1 and 2 of three; the Code-button bridge is a separate plan (`2026-10-04-code-button-bridge.md`).

**Placeholder scan:** none — every step carries its code.

**Type consistency:** `resolve()` returns a dict with `backend`/`model`/`context_window`/`output_reserve`, used by Task 1's tests and named identically in the spec §4.1 table. `FailureReason` is the same five strings in Task 1 and spec §5.

**Review Focus → tests:** unconfirmed cloud (Task 1, explicit try/except); cloud-session background traffic (Task 2, the `haiku` text check — plus the live check in Step 5); `--model ANYTHING` (Task 2 Step 5 exercises `deepseek --routes`); unknown backend string (Task 1, the `("api", "", "Cloud", "gpu")` loop).

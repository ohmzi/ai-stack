"""One bounded capability for the coding profile: run a task through the local deepseek CLI.

WHY THIS EXISTS
---------------
The coding agent needs a shell to reach `deepseek`, and the chat-facing surface must not have one
(docs/HERMES_AGENT.md § Config decisions that are deliberate). So instead of a shell it gets this: a
FIXED argv, a pinned working directory, an explicit environment, a timeout and an output cap. There
is no argument a chat message can supply that changes which program runs.

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
    # Containment first, existence second: a path that resolves outside the roots is refused on
    # containment grounds even when it does not exist, so "not a directory" can never be read as
    # an escape hatch for a traversal that merely missed its target.
    in_root = any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep)
                  for r in CODING_TASK_ROOTS)
    if not in_root:
        return None, (f"{repo!r} is outside the allowlisted roots ({', '.join(CODING_TASK_ROOTS)}) "
                      "— refused.")
    if not os.path.isdir(real):
        return None, f"{repo!r} is not a directory"
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

#!/usr/bin/env python3
"""Append the generation-info hunks (ResponseMessage.svelte) to the fork patch.

Restores what OpenWebUI v0.3 had and v0.11 cut back: the info button under a reply showed
`response_token/s` and the rest of Ollama's timing block, and now dumps the raw `message.usage`
object instead — a wall of `key: value` with no figure to read.

Two changes, and the second is why this is worth a patch rather than living with the dump:

  * the token rate moves onto the button FACE, so it is visible without hovering;
  * the tooltip is formatted, and renders whatever the payload actually carries — an Ollama timing
    block for a chat turn, or the MEASURED facts the pipe reports for a render (`method`, `took`)
    and a notebook answer (`answer`, `took`, `citations`, `note`). Those carry no rate at all, by
    design: there are no token counts behind them, and the pipe says so in a `note` field rather
    than leaving a gap that reads as a bug.

Written as one helper plus four anchored edits. The helper goes in the <script> block because
`{@const}` inside the markup would have to recompute it per render and would have to inline the
whole formatter as an expression.

Usage:  python3 06_generation_info.py ../task-mode.patch --append
"""
import os, subprocess, sys

SC = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SC, "ResponseMessage.svelte")
s = open(SRC, encoding="utf-8").read()


def sub(old, new, why, count=1):
    global s
    n = s.count(old)
    assert n == count, f"anchor matched {n}x (expected {count}): {why}\n---\n{old[:180]}"
    s = s.replace(old, new)


# ---------------------------------------------------------------- the formatter
# perSec mirrors the pipe's own arithmetic (Ollama reports NANOseconds) so the button and the
# tooltip cannot disagree about the same numbers. Every line is prefixed with its key, including
# the raw fields, because the tooltip for a render carries `method`/`took` where a chat turn
# carries `eval_count`/`eval_duration` — one rendering path for both, and no key is assumed.
sub(
    "\tconst dispatch = createEventDispatcher();",
    """\tconst dispatch = createEventDispatcher();

\t// --- ai-stack: generation stats under a reply -------------------------------------------------
\t// OpenWebUI v0.3 showed response_token/s here; v0.11 cut the tooltip back to a raw dump of
\t// `message.usage`. Restored, and widened: the Assistant pipe also reports MEASURED facts for
\t// replies that streamed no tokens — a render's pipeline and steps, a notebook answer's source
\t// and citation count — so this renders whatever the payload actually carries rather than
\t// assuming an Ollama timing block. Where there is no rate to derive it says nothing about a
\t// rate, which is the honest answer: those payloads have no token counts behind them.
\tconst generationStats = (usage: any) => {
\t\tif (!usage || typeof usage !== 'object') return { tokenRate: null, lines: [] };
\t\tconst perSec = (count: any, durNs: any) =>
\t\t\ttypeof count === 'number' && typeof durNs === 'number' && count > 0 && durNs > 0
\t\t\t\t? Math.round((count / (durNs / 1e9)) * 10) / 10
\t\t\t\t: null;
\t\tconst tokenRate = perSec(usage.eval_count, usage.eval_duration);
\t\tconst promptRate = perSec(usage.prompt_eval_count, usage.prompt_eval_duration);
\t\tconst lines: string[] = [];
\t\tif (tokenRate !== null) lines.push(`tokens/s:          ${tokenRate}`);
\t\tif (promptRate !== null) lines.push(`prompt tokens/s:   ${promptRate}`);
\t\tfor (const [k, v] of Object.entries(usage)) {
\t\t\tlines.push(`${k}: ${typeof v === 'object' && v !== null ? JSON.stringify(v) : v}`);
\t\t}
\t\treturn { tokenRate, lines };
\t};""",
    "generationStats helper")

# ---------------------------------------------------------------- the tooltip body
# The old expression is a 10-line pipeline that JSON.stringifies and then strips the quoting back
# out line by line. generationStats already produces the finished lines, so this collapses to one.
sub(
    "\t" * 10 + "content={message.usage\n"
    + "\t" * 11 + "? `<pre>${sanitizeResponseContent(\n"
    + "\t" * 13 + "JSON.stringify(message.usage, null, 2)\n"
    + "\t" * 14 + ".replace(/\"([^(\")\"]+)\":/g, '$1:')\n"
    + "\t" * 14 + ".slice(1, -1)\n"
    + "\t" * 14 + ".split('\\n')\n"
    + "\t" * 14 + ".map((line) => line.slice(2))\n"
    + "\t" * 14 + ".map((line) => (line.endsWith(',') ? line.slice(0, -1) : line))\n"
    + "\t" * 14 + ".join('\\n')\n"
    + "\t" * 12 + ")}</pre>`\n"
    + "\t" * 11 + ": ''}",
    "\t" * 10
    + "content={`<pre>${sanitizeResponseContent(generationStats(message.usage).lines.join('\\n'))}</pre>`}",
    "tooltip content")

# ---------------------------------------------------------------- the button face
# `items-center gap-0.5` so the rate sits beside the icon instead of under it. The whitespace-pre-wrap
# is kept: it is what stops the label wrapping mid-number on a narrow column.
sub(
    ": 'hover-reveal'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg "
    "dark:hover:text-white hover:text-black transition whitespace-pre-wrap",
    ": 'hover-reveal'} flex items-center gap-0.5 p-1.5 hover:bg-black/5 "
    "dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition "
    "whitespace-pre-wrap",
    "button class")

# The visible figure. Gated on a real rate: a render or a notebook answer has `usage` but no rate,
# and the button must not claim one.
sub(
    "\t" * 11 + "</svg>\n"
    + "\t" * 10 + "</button>",
    "\t" * 11 + "</svg>\n"
    + "\t" * 11 + "{#if _stats.tokenRate !== null}\n"
    + "\t" * 12 + '<span class="text-xs font-normal">{_stats.tokenRate} tok/s</span>\n'
    + "\t" * 11 + "{/if}\n"
    + "\t" * 10 + "</button>",
    "button label")

# ------------------------------------------------------------------------------- bind it together
# Placed first in the block so the Tooltip below can read it. `{@const}` is only legal as an
# immediate child of a block, which is exactly where this goes.
sub(
    "\t" * 8 + "{#if message.usage}",
    "\t" * 8 + "{#if message.usage}\n"
    + "\t" * 9 + "{@const _stats = generationStats(message.usage)}",
    "stats binding")

out = os.path.join(SC, "ResponseMessage.svelte.new")
open(out, "w", encoding="utf-8").write(s)

rel = "src/lib/components/chat/Messages/ResponseMessage.svelte"
d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", SRC, out],
                   capture_output=True, text=True)
dest = sys.argv[1]
mode = "a" if len(sys.argv) > 2 and sys.argv[2] == "--append" else "w"
with open(dest, mode, encoding="utf-8") as f:
    f.write(d.stdout)
print(f"{'appended to' if mode == 'a' else 'wrote'} {dest}: "
      f"{sum(1 for l in d.stdout.splitlines() if l.startswith('+') and not l.startswith('+++'))} "
      f"added lines")

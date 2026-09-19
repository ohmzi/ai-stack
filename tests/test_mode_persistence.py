#!/usr/bin/env python3
"""The two frontend behaviours the fork patch adds, asserted against the SHIPPED artifact.

Why this file exists. Everything here is generated: `gen/01-06` produce `task-mode.patch`, the
Dockerfile applies it, and the result is compiled into the image. None of that is reachable from a
unit test at runtime — the mode is Svelte component state and the stats button is compiled markup —
so the only place to catch a regression is the patch text itself, which is what actually ships.

Two rules are checked that both fail SILENTLY in the browser, and both were the point of their
change:

  * **A mode restored from the chat is never written into the pending slot.** `MODE_KEY(null)` is
    the one deliberate cross-chat handoff — the mode chosen on the landing page, before a chat
    exists, bounded by `MODE_HANDOFF_MS` so it cannot follow some later, unrelated chat. A
    server-restored mode must stay out of it, or one chat's mode can be adopted by another.
  * **The stats button never claims a rate it does not have.** A render and a notebook answer carry
    `message.usage` with measured facts and no token counts; the figure on the button face is gated
    on a rate being derivable, not on `usage` merely existing.

Also pinned: `applyMode` accepts the object the chat row hands back, not just the string
sessionStorage holds. Getting that wrong is invisible until the server copy exists — which is
exactly when it starts mattering.

Usage:  python3 tests/test_mode_persistence.py [patch_path]
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    ROOT, "compose", "openwebui", "fork", "task-mode.patch")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    text = open(PATCH, encoding="utf-8").read()
    added = "\n".join(l[1:] for l in text.splitlines()
                      if l.startswith("+") and not l.startswith("+++"))

    print("--- the patch covers the files it claims to ---")
    files = re.findall(r"^--- a/(\S+)", text, re.M)
    check("seven files", len(files) == 7, f"{len(files)}: {files}")
    check("includes ResponseMessage.svelte",
          any(f.endswith("Messages/ResponseMessage.svelte") for f in files), repr(files))
    check("includes Chat.svelte", any(f.endswith("chat/Chat.svelte") for f in files), repr(files))

    print("\n--- the mode is written onto the chat, not just the tab ---")
    check("saveMode persists to the chat", "updateChatById(localStorage.token, id, { chat_mode: blob })"
          in added)
    check("...only when there is an id", re.search(r"if \(id\) \{\s*\n\+?\s*updateChatById", added)
          is not None, "no `if (id)` guard found")
    check("...as a partial object, so updated_at is not bumped",
          "chat_mode: blob" in added and "history" not in
          added.split("updateChatById(localStorage.token, id")[1][:120],
          "the write carries history/messages")

    print("\n--- the chat's own mode is read back ---")
    check("restoreMode reads the chat row", "chat?.chat?.chat_mode" in added)
    check("applyMode accepts the stored object, not only the string",
          "typeof raw === 'string' ? JSON.parse(raw) : raw" in added)

    print("\n--- CONTAMINATION: a restored mode must not enter the pending slot ---")
    # The whole of restoreMode, from its signature to the next top-level helper.
    body = added.split("const restoreMode", 1)[-1].split("const clearDraft", 1)[0]
    check("restoreMode found", body and "sessionStorage.getItem" in body, repr(body[:80]))
    # removeItem(MODE_KEY(null)) is the DOCUMENTED consume-and-discard — the pending mode is
    # claimed once and cleared either way, so a stale choice cannot leak into a later chat. Only a
    # setItem into that slot from here would be contamination, which is what this rules out.
    writes = re.findall(r"sessionStorage\.setItem\s*\(\s*MODE_KEY\((\w+)\)", body)
    check("nothing is WRITTEN into the pending slot from the restore path",
          all(key == "id" for key in writes) or writes == [],
          f"setItem to MODE_KEY(null) in restoreMode: {writes}")
    check("...and the pending slot is still consumed exactly once",
          len(re.findall(r"removeItem\s*\(\s*MODE_KEY\(null\)", body)) == 1,
          "the consume-and-discard moved or was duplicated")
    check("...and the server value is never fed into it",
          not re.search(r"MODE_KEY\(null\)[^;]*chat_mode", body, re.S),
          "chat_mode reaches the pending slot")

    print("\n--- the stats button ---")
    check("a rate is computed from the Ollama fields",
          "usage.eval_count" in added and "usage.eval_duration" in added)
    check("the rate is shown on the button face", "{_stats.tokenRate} tok/s" in added)
    check("...and gated on having a rate, not merely on having usage",
          "_stats.tokenRate !== null" in added)
    check("the raw JSON dump is gone",
          "JSON.stringify(message.usage, null, 2)" not in added,
          "the v0.11 dump expression is still present")
    check("the tooltip still renders something for every payload",
          "generationStats(message.usage).lines.join" in added)

    fails = sum(1 for r in results if not r)
    print(f"\n{len(results) - fails}/{len(results)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

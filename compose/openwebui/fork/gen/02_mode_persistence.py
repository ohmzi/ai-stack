#!/usr/bin/env python3
"""Append the mode-persistence hunks (Chat.svelte + MessageInput onModeChange) to the fork patch.

Run after mkpatch.py; writes a combined patch covering both files.
"""
import os, subprocess, sys

SC = os.path.dirname(os.path.abspath(__file__))
EDITS = []


def load(name):
    return open(os.path.join(SC, name), encoding="utf-8").read()


def sub(s, old, new, why):
    n = s.count(old)
    assert n == 1, f"anchor matched {n}x (expected 1): {why}\n---\n{old[:180]}"
    return s.replace(old, new)


# ---------------------------------------------------------------- MessageInput: report the change
mi = load("MessageInput.patched.svelte")
mi = sub(mi,
    """	export let onWebSearchToggle: Function = () => {};""",
    """	export let onWebSearchToggle: Function = () => {};
	// ai-stack: fired only when the USER changes mode, so Chat.svelte can remember the choice
	// without also remembering every incidental reset.
	export let onModeChange: Function = () => {};""",
    "onModeChange prop")
mi = sub(mi,
    """				: (selectedFilterIds ?? []).filter((id) => id !== TASK_FILTER_ID);
		onWebSearchToggle(webSearchEnabled);
	};""",
    """				: (selectedFilterIds ?? []).filter((id) => id !== TASK_FILTER_ID);
		onWebSearchToggle(webSearchEnabled);
		onModeChange();
	};""",
    "onModeChange call")
EDITS.append(("src/lib/components/chat/MessageInput.svelte", "MessageInput.svelte", mi))

# ------------------------------------------------------- Placeholder: forward the callback
# NEW for 0.11.3, and the reason this generator now touches three files instead of two.
#
# 0.10.2 rendered MessageInput directly in both of Chat.svelte's composers. 0.11.3 factors the
# no-messages landing page out into Placeholder, which renders its OWN MessageInput and declares
# its props explicitly (`export let`, no $$restProps). Svelte drops an undeclared prop silently,
# so a callback passed to <Placeholder> goes nowhere unless the component re-declares AND
# forwards it.
#
# That matters because this is the landing page — precisely where a mode gets chosen before the
# chat has an id, which is the case MODE_KEY(null) / MODE_HANDOFF_MS exist to serve. Without
# this, picking Task on the landing page sets the local state but never reaches saveMode, and
# the re-init that follows the first message falls through to setDefaults() and resets it.
ph = load("Placeholder.svelte")
ph = sub(ph,
    """	export let onWebSearchToggle: Function = () => {};""",
    """	export let onWebSearchToggle: Function = () => {};
	// ai-stack: forwarded straight through to MessageInput so a mode chosen on the landing page
	// is remembered exactly like one chosen in the chat composer.
	export let onModeChange: Function = () => {};""",
    "onModeChange prop")
ph = sub(ph,
    """						{askUser}
						{onWebSearchToggle}""",
    """						{askUser}
						{onModeChange}
						{onWebSearchToggle}""",
    "onModeChange forward")
EDITS.append(("src/lib/components/chat/Placeholder.svelte", "Placeholder.svelte", ph))

# ---------------------------------------------------------------- Chat: remember the user's choice
ch = load("Chat.svelte")

ch = sub(ch,
    """	const clearDraft = async (chatId: string | null = null) => {""",
    """	// --- ai-stack: the selected mode belongs to the CHAT, not to the draft ------------------
	// Upstream stores the mode alongside the unsent draft, and clears the draft on submit. So the
	// first message of a chat wipes it, and the re-init that follows (chatIdProp changes, so
	// navigateHandler runs) finds nothing and falls through to setDefaults() — which turns
	// Internet back on and Task off, mid-conversation, without anyone asking. Whatever the user
	// picked has to survive sending a message; only the user gets to change it.
	//
	// Kept under its own key so clearDraft cannot reach it, and written ONLY from the mode
	// buttons' own callback: an incidental reset during init must never be mistaken for a choice.
	const MODE_KEY = (id: string | null = null) => `chat-mode${id ? `-${id}` : ''}`;
	// A mode chosen before the chat had an id is handed over exactly once, and only if it was
	// chosen moments ago — so it follows the message that created the chat, and cannot leak into
	// some unrelated chat opened later.
	const MODE_HANDOFF_MS = 120000;

	const saveMode = (id: string | null = null) => {
		try {
			sessionStorage.setItem(
				MODE_KEY(id),
				JSON.stringify({
					t: Date.now(),
					selectedFilterIds,
					webSearchEnabled,
					codeInterpreterEnabled,
					imageGenerationEnabled
				})
			);
		} catch (e) {}
	};

	const applyMode = (raw: string | null) => {
		if (!raw) return false;
		try {
			const m = JSON.parse(raw);
			selectedFilterIds = m.selectedFilterIds ?? [];
			webSearchEnabled = !!m.webSearchEnabled;
			codeInterpreterEnabled = !!m.codeInterpreterEnabled;
			imageGenerationEnabled = !!m.imageGenerationEnabled;
			return true;
		} catch (e) {
			return false;
		}
	};

	// True when a remembered mode was applied, i.e. the caller must NOT run setDefaults().
	const restoreMode = (id: string | null = null) => {
		try {
			if (applyMode(sessionStorage.getItem(MODE_KEY(id)))) return true;
			if (!id) return false;
			// The chat has just been created: adopt what was chosen while it had no id.
			const pending = sessionStorage.getItem(MODE_KEY(null));
			if (!pending) return false;
			let fresh = false;
			try {
				fresh = Date.now() - (JSON.parse(pending).t ?? 0) < MODE_HANDOFF_MS;
			} catch (e) {}
			sessionStorage.removeItem(MODE_KEY(null));
			if (!fresh || !applyMode(pending)) return false;
			saveMode(id);
			return true;
		} catch (e) {
			return false;
		}
	};

	const clearDraft = async (chatId: string | null = null) => {""",
    "mode helpers")

# navigateHandler: the path that actually caused the reported reset.
#
# RE-DERIVED for 0.11.3. Upstream now routes the draft restore through a named
# `restoreChatInput()` and guards `setDefaults()` behind its failure — structurally the shape
# this hunk has always wanted, so the guard JOINS the condition instead of replacing an
# if/else. The short-circuit is load-bearing: when a draft did restore, the draft's own mode
# is the legitimate one and `restoreMode` must not run a second time over it.
ch = sub(ch,
    """\t\t\tif (!(await restoreChatInput(storageChatInput))) {
\t\t\t\tawait setDefaults();
\t\t\t}

\t\t\tmessageInput?.focus({ preventScroll: true });""",
    """\t\t\tif (!(await restoreChatInput(storageChatInput)) && !restoreMode(chatIdProp)) {
\t\t\t\t// ai-stack: only fall back to the model's defaults when this chat has no mode of
\t\t\t\t// its own. Otherwise sending a message would silently reset it.
\t\t\t\tawait setDefaults();
\t\t\t}

\t\t\tmessageInput?.focus({ preventScroll: true });""",
    "navigateHandler restore")

# onMount init: same rule when a saved chat is opened directly.
ch = sub(ch,
    """\t\t\tif (!chatIdProp) {
\t\t\t\tloading = false;
\t\t\t\tawait tick();""",
    """\t\t\tif (!chatIdProp) {
\t\t\t\t// ai-stack: a brand-new chat starts from the model's defaults (Internet on), so a
\t\t\t\t// mode left over from an earlier new chat must not be inherited here.
\t\t\t\ttry {
\t\t\t\t\tsessionStorage.removeItem(MODE_KEY(null));
\t\t\t\t} catch (e) {}
\t\t\t\tloading = false;
\t\t\t\tawait tick();""",
    "new chat clears pending mode")

ch = sub(ch,
    """\t\t\t\tawait restoreChatInput(storageChatInput);
\t\t\t}

\t\t\tmessageInput?.focus({ preventScroll: true });
\t\t};
\t\tinit();""",
    """\t\t\t\tawait restoreChatInput(storageChatInput);
\t\t\t} else if (chatIdProp) {
\t\t\t\t// ai-stack: opening a saved chat with no unsent draft — keep the mode it was left
\t\t\t\t// in rather than resetting to the model's defaults.
\t\t\t\trestoreMode(chatIdProp);
\t\t\t}

\t\t\tmessageInput?.focus({ preventScroll: true });
\t\t};
\t\tinit();""",
    "onMount restore")

# Wire the callback through to ALL THREE composer hosts, not the two 0.10.2 had. 0.11.3 added
# the third by factoring the landing page out into <Placeholder> — see that file's edits above,
# which are what make this third site actually forward the prop instead of swallowing it.
#
# The replace below already covers every site; this count is the guard that keeps it that way.
# A fourth composer added upstream must fail HERE, in the build, rather than ship as a mode
# control that looks right and quietly forgets what the user picked.
_n = ch.count("onWebSearchToggle={handleWebSearchToggle}")
assert _n == 3, f"expected 3 composer hosts (2 MessageInput + 1 Placeholder), found {_n}"
ch = ch.replace("onWebSearchToggle={handleWebSearchToggle}",
                "onWebSearchToggle={handleWebSearchToggle}\n"
                "\t\t\t\t\t\t\t\t\t\tonModeChange={() => saveMode(chatIdProp)}")

EDITS.append(("src/lib/components/chat/Chat.svelte", "Chat.svelte", ch))

# ---------------------------------------------------------------- emit
parts = []
for rel, orig_name, patched in EDITS:
    orig = os.path.join(SC, "MessageInput.svelte" if orig_name == "MessageInput.svelte" else orig_name)
    new = os.path.join(SC, f"{orig_name}.new")
    open(new, "w", encoding="utf-8").write(patched)
    d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", orig, new],
                       capture_output=True, text=True)
    parts.append(d.stdout)

dest = sys.argv[1]
open(dest, "w", encoding="utf-8").write("".join(parts))
added = sum(1 for l in "".join(parts).splitlines()
            if l.startswith("+") and not l.startswith("+++"))
print(f"wrote {dest}: {added} added lines across {len(EDITS)} files")

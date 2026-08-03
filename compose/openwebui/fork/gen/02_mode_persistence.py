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
ch = sub(ch,
    """			} else {
				await setDefaults();
			}

			const chatInput = document.getElementById('chat-input');
			chatInput?.focus();
		} else {
			await goto('/');
		}
	};""",
    """			} else if (!restoreMode(chatIdProp)) {
				// ai-stack: only fall back to the model's defaults when this chat has no mode of
				// its own. Otherwise sending a message would silently reset it.
				await setDefaults();
			}

			const chatInput = document.getElementById('chat-input');
			chatInput?.focus();
		} else {
			await goto('/');
		}
	};""",
    "navigateHandler restore")

# onMount init: same rule when a saved chat is opened directly.
ch = sub(ch,
    """			if (!chatIdProp) {
				loading = false;
				await tick();""",
    """			if (!chatIdProp) {
				// ai-stack: a brand-new chat starts from the model's defaults (Internet on), so a
				// mode left over from an earlier new chat must not be inherited here.
				try {
					sessionStorage.removeItem(MODE_KEY(null));
				} catch (e) {}
				loading = false;
				await tick();""",
    "new chat clears pending mode")

ch = sub(ch,
    """				try {
					const input = JSON.parse(storageChatInput);

					if (!$temporaryChatEnabled) {
						messageInput?.setText(input.prompt);
						files = input.files;
						selectedToolIds = input.selectedToolIds;
						selectedSkillIds = input.selectedSkillIds ?? [];
						selectedFilterIds = input.selectedFilterIds;
						webSearchEnabled = input.webSearchEnabled;
						imageGenerationEnabled = input.imageGenerationEnabled;
						codeInterpreterEnabled = input.codeInterpreterEnabled;
					}
				} catch (e) {}
			}

			const chatInput = document.getElementById('chat-input');
			chatInput?.focus();
		};
		init();""",
    """				try {
					const input = JSON.parse(storageChatInput);

					if (!$temporaryChatEnabled) {
						messageInput?.setText(input.prompt);
						files = input.files;
						selectedToolIds = input.selectedToolIds;
						selectedSkillIds = input.selectedSkillIds ?? [];
						selectedFilterIds = input.selectedFilterIds;
						webSearchEnabled = input.webSearchEnabled;
						imageGenerationEnabled = input.imageGenerationEnabled;
						codeInterpreterEnabled = input.codeInterpreterEnabled;
					}
				} catch (e) {}
			} else if (chatIdProp) {
				// ai-stack: opening a saved chat with no unsent draft — keep the mode it was left
				// in rather than resetting to the model's defaults.
				restoreMode(chatIdProp);
			}

			const chatInput = document.getElementById('chat-input');
			chatInput?.focus();
		};
		init();""",
    "onMount restore")

# Wire the callback through to BOTH MessageInput instances (the empty-chat one and the active one).
_n = ch.count("onWebSearchToggle={handleWebSearchToggle}")
assert _n == 2, f"expected 2 MessageInput instances, found {_n}"
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

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
    """		setToggle(NOTEBOOK_FILTER_ID, !off && mode === 'notebook');
		onWebSearchToggle(webSearchEnabled);
	};""",
    """		setToggle(NOTEBOOK_FILTER_ID, !off && mode === 'notebook');
		onWebSearchToggle(webSearchEnabled);
		// The mode just resolved, NOT `activeMode` — the reactive statement that recomputes that
		// has not run yet here, so reading it would report the PREVIOUS mode. Clicking the active
		// button clears the mode, hence null.
		onModeChange(off ? null : mode);
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
	export let onModeChange: Function = () => {};

	// --- ai-stack: the landing-page suggestions follow the mode button ----------------------
	// The four buttons are answers to "what should this turn do?", and the fastest way to learn
	// that is for the examples under the composer to change the moment one is pressed. Each mode
	// gets a pool several times larger than the number shown, and the window advances one step
	// per PAGE LOAD, so a returning user meets a different corner of what the mode can do
	// instead of the same four prompts forever.
	let landingMode = null;
	const SUGGESTION_WINDOW = 5;
	// Each entry is { title: [bold line, grey line], content: what clicking inserts }.
	// That shape is NOT optional: Suggestions.svelte renders `prompt.title[0]` / `prompt.title[1]`
	// and otherwise falls back to `prompt.content` plus the literal word "Prompt". A plain STRING
	// therefore renders as chips reading "Prompt" — which is exactly what shipped on 2026-09-18 and
	// what a browser check caught. `content` ends in a space where the prompt is a template the
	// user is meant to finish.
	//
	// Pools are bigger than the window on purpose: the window advances one step per page load, so
	// N prompts give N distinct windows before anything repeats.

	// --- no mode: what the assistant does on its own --------------------------------------
	// This is the one screen where nothing has been declared, so it doubles as the pitch. The
	// first three rotate and are the ordinary things people reach for a chat model for; the two
	// FIXED entries below them exist to teach the thing that is hardest to discover — that this
	// assistant routes code and background work WITHOUT a button, straight from the wording.
	const NO_MODE_ROTATING = [
		{ title: ['Summarise this', 'paste an email, article or notes'], content: 'Summarise this in three bullet points: ' },
		{ title: ['Draft a reply', 'polite, firm or friendly'], content: 'Draft a polite reply to this message: ' },
		{ title: ['Explain it simply', 'a topic, as if I am new to it'], content: 'Explain how ' },
		{ title: ['Brainstorm ideas', 'for a project, gift or name'], content: 'Give me 10 ideas for ' },
		{ title: ['Plan something', 'a trip, a week, a project'], content: 'Plan a 3-day trip to ' },
		{ title: ['Translate this', 'into any language'], content: 'Translate this into ' },
		{ title: ['Change the tone', 'more confident, warmer, shorter'], content: 'Rewrite this to sound more confident: ' },
		{ title: ['Compare my options', 'and say what you would pick'], content: 'Compare these options and tell me which you would pick: ' },
		{ title: ['Draw me a picture', 'describe it and it renders'], content: 'Draw a picture of ' },
		{ title: ['Quiz me', 'one question at a time'], content: 'Quiz me on the basics of ' }
	];
	const NO_MODE_FIXED = [
		{ title: ['Write a script', 'no button needed — code routes itself'], content: 'Write a Python script to ' },
		{ title: ['Set up a monitor', 'no button needed — it schedules itself'], content: 'Monitor the price of ' }
	];
	const NO_MODE_WINDOW = 3; // + the two fixed entries = the five shown

	const MODE_SUGGESTIONS = {
		// Internet: SearXNG-backed search, results injected as context before the model answers.
		web: [
			{ title: ['Latest news', 'on any topic you name'], content: "What's the latest news on " },
			{ title: ['Find research', 'papers and articles on a subject'], content: 'Find recent research about ' },
			{ title: ['Official docs', 'for a library, tool or API'], content: 'Find the official docs for ' },
			{ title: ['Compare prices', 'before buying something'], content: 'Compare current prices for ' },
			{ title: ['What the web says', 'summarised across sources'], content: 'Summarise what the web says about ' },
			{ title: ['What changed', 'on a topic, this year'], content: 'What has changed about ' },
			{ title: ['Release notes', 'for a version or product'], content: 'Look up the release notes for ' },
			{ title: ['Current discussion', 'what people are saying now'], content: 'What are people saying about ' },
			{ title: ['Check a claim', 'against what sources say'], content: 'Is it true that ' },
			{ title: ['Best of', 'top-rated options in a category'], content: 'What are the best ' }
		],
		// Code: routes to the coder model and turns the code interpreter on for the turn.
		code: [
			{ title: ['Refactor this code', 'for readability'], content: "Refactor this code so it's easier to read" },
			{ title: ['Fix the bug', 'in the function I paste'], content: 'Fix the bug in this function' },
			{ title: ['Optimise for speed', 'and explain the trade-offs'], content: 'Optimise this for speed' },
			{ title: ['Explain this code', 'line by line'], content: 'Explain what this code does, line by line' },
			{ title: ['Add tests', 'for this function'], content: 'Add tests for this function' },
			{ title: ['Debug this regex', 'it is not matching what I expect'], content: 'What is wrong with this regular expression?' },
			{ title: ['Write a script', 'that does a task end to end'], content: 'Write a script that ' },
			{ title: ['Port this code', 'to another language'], content: 'Rewrite this in ' },
			{ title: ['Review this diff', 'for mistakes before I commit'], content: 'Review this diff for mistakes: ' },
			{ title: ['Explain this error', 'what it means and how to fix it'], content: 'Explain this error message and how to fix it: ' }
		],
		// Task: the whole turn goes to the background-task agent, which creates standing jobs.
		task: [
			{ title: ['Watch a price', 'and tell me when it moves'], content: 'Monitor the price of ' },
			{ title: ['Threshold alert', 'tell me when it crosses a value'], content: 'Tell me when ' },
			{ title: ['Daily check', 'every morning at 8'], content: 'Check ' },
			{ title: ['Watch and text me', 'whenever something changes'], content: 'Watch ' },
			{ title: ['Weekly reminder', 'every Monday'], content: 'Remind me every Monday to ' },
			{ title: ['Watch a flight', 'and alert me on the fare'], content: 'Find me a cheap flight to ' },
			{ title: ['Show my jobs', 'every monitor currently running'], content: 'Show me my running jobs' },
			{ title: ['Cancel a job', 'stop a monitor that is running'], content: 'Cancel the job ' },
			{ title: ['New listing alert', 'whenever something is posted'], content: 'Tell me when a new ' },
			{ title: ['Recurring summary', 'a report on a schedule'], content: 'Send me a summary of ' }
		],
		// Notebook: answered from Open Notebook, scoped to the notebook named in the message.
		// The complete sentences are phrasings the resolver accepts, so clicking one produces a
		// real answer; the two templates end in a space because they need a notebook name.
		notebook: [
			{ title: ["What's in here?", 'every notebook and its books'], content: 'What kind of books are there?' },
			{ title: ['How do I ask?', 'the phrasings this mode understands'], content: 'What can I ask?' },
			{ title: ['Which notebook?', 'let it suggest the right one'], content: 'Which notebook should I use?' },
			{ title: ['List the notebooks', 'names and source counts'], content: 'List the notebooks' },
			{ title: ['Browse the collection', 'everything available right now'], content: "What's in the notebooks?" },
			{ title: ['Ask about one book', 'name the notebook in your message'], content: 'How do I ask about a specific book?' },
			{ title: ['Show the library', 'all books across notebooks'], content: 'Show me what books you have' },
			{ title: ['Everything it can see', 'the whole Open Notebook'], content: 'What is in open notebook' },
			{ title: ['Books in one notebook', 'type the notebook name after this'], content: 'List the books in ' },
			{ title: ['What one covers', 'name the notebook after this'], content: 'What kind of books are in ' }
		]
	};

	// One counter per mode, advanced once per mount. Deliberately NOT computed in a reactive
	// statement: Placeholder re-renders constantly, and rotating there would shuffle the chips
	// out from under the pointer. The memo below makes re-evaluation idempotent, which is what
	// lets the caller read the same window twice without advancing it.
	const _suggestionStep = (mode, size) => {
		try {
			const key = 'ai-stack-suggestions-' + mode;
			const n = parseInt(localStorage.getItem(key) ?? '0', 10) || 0;
			localStorage.setItem(key, String((n + 1) % size));
			return n % size;
		} catch (e) {
			return 0; // private mode, blocked storage — the pool still works from the top
		}
	};
	const _suggestionStart = {};
	const _window = (pool, count, key) => {
		if (pool.length <= count) return pool;
		if (_suggestionStart[key] === undefined) _suggestionStart[key] = _suggestionStep(key, pool.length);
		const start = _suggestionStart[key];
		return Array.from({ length: count }, (_, i) => pool[(start + i) % pool.length]);
	};
	const rotatedSuggestions = (mode) => {
		if (!mode) {
			// No button pressed: the ordinary prompts plus the two that teach auto-routing.
			return [..._window(NO_MODE_ROTATING, NO_MODE_WINDOW, 'none'), ...NO_MODE_FIXED];
		}
		return _window(MODE_SUGGESTIONS[mode] ?? [], SUGGESTION_WINDOW, mode);
	};
	let suggestionList = [];
	$: suggestionList = rotatedSuggestions(landingMode);
	const handleModeChange = (mode) => {
		landingMode = mode;
		onModeChange(mode);
	};""",
    "onModeChange prop")
ph = sub(ph,
    """						{askUser}
						{onWebSearchToggle}""",
    """						{askUser}
						onModeChange={handleModeChange}
						{onWebSearchToggle}""",
    "onModeChange forward")
ph = sub(ph,
    """					suggestionPrompts={atSelectedModel?.info?.meta?.suggestion_prompts ??
						models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
						$config?.default_prompt_suggestions ??
						[]}""",
    """					suggestionPrompts={suggestionList.length
						? suggestionList
						: (atSelectedModel?.info?.meta?.suggestion_prompts ??
							models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
							$config?.default_prompt_suggestions ??
							[])}""",
    "mode suggestions")
# Guard the SHAPE, not just the presence, of the suggestion pools. Suggestions.svelte renders
# `prompt.title[0]` / `prompt.title[1]`, and falls back to `prompt.content` plus the literal word
# "Prompt" — so a pool written as plain strings renders as four chips reading "Prompt". That
# shipped once (2026-09-18) and was caught in a browser, not by any test. 4 modes x 8 prompts.
assert ph.count("{ title: [") >= 42, (
    f"suggestion pools look wrong: {ph.count('{ title: [')} object entries, expected >= 42. "
    "Entries must be {title: [...], content: ...} — plain strings render as the word \"Prompt\".")
# ------------------------------------------------- Suggestions: room for five, not three
# NEW 2026-09-18, and the reason this generator now touches a fourth file.
#
# 02 owns the landing-page suggestion POOL; that pool is five entries, and the list that renders
# them is a fixed 144px box (`h-36` / `max-h-36`) with `overflow-auto scrollbar-none`. Five chips
# at 48px need 240px, so two were scrolled out of sight behind a HIDDEN scrollbar — measured in a
# browser: 5 in the DOM, 3 fully visible, scrollHeight 240 vs clientHeight 144. Nothing on screen
# suggested the other two existed. Bumping both classes to `h-60` / `max-h-60` (240px) shows all
# five. Changing the pool size again means revisiting these two numbers: they are a pair.
sg = load("Suggestions.svelte")
sg = sub(sg,
    """<div class="h-36 w-full">""",
    """<div class="h-60 w-full">""",
    "suggestions height")
sg = sub(sg,
    """		<div role="list" class="max-h-36 overflow-auto scrollbar-none items-start {className}">""",
    """		<div role="list" class="max-h-60 overflow-auto scrollbar-none items-start {className}">""",
    "suggestions max-height")
EDITS.append(("src/lib/components/chat/Suggestions.svelte", "Suggestions.svelte", sg))
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

#!/usr/bin/env python3
"""Generate compose/openwebui/fork/task-mode.patch from the upstream MessageInput.svelte.

Written as a generator rather than a hand-authored diff so the anchors are checked: every
replacement asserts it matched exactly once, and a rebase that silently applied to the wrong place
fails here instead of in the browser.
"""
import subprocess, sys, os

SC = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SC, "MessageInput.svelte")
orig = open(SRC, encoding="utf-8").read()
s = orig


def sub(old, new, why):
    global s
    n = s.count(old)
    assert n == 1, f"anchor matched {n}x (expected 1): {why}\n---\n{old[:200]}"
    s = s.replace(old, new)


# 1. Mode state + the single mutator. Inserted after upstream's own terminal/code-interpreter
#    exclusivity block, which is the only precedent for one of these turning another off.
sub(
    """	// Disable code interpreter when terminal is active (mutually exclusive)
	$: if ($selectedTerminalId && codeInterpreterEnabled) {
		codeInterpreterEnabled = false;
	}
""",
    """	// Disable code interpreter when terminal is active (mutually exclusive)
	$: if ($selectedTerminalId && codeInterpreterEnabled) {
		codeInterpreterEnabled = false;
	}

	// --- ai-stack: Internet / Code / Task / Notebook as one exclusive control ---------------
	// Upstream treats these as independent switches living in the Integrations dropdown. Here
	// they are always-visible buttons and exactly one can be active, because they are answers to
	// the same question: what should this turn do? Task and Notebook are toggle filters
	// (filters/task_mode.py, filters/notebook_mode.py) rather than built-in features, so they live
	// in selectedFilterIds while the other two are booleans — setMode is what hides that difference.
	//
	// The server enforces the same exclusivity independently (each filter's inlet stands the other
	// modes down). That is deliberate: this layer is a convenience, not the guarantee.
	const TASK_FILTER_ID = 'task_mode';
	const NOTEBOOK_FILTER_ID = 'notebook_mode';
	const MODE_FILTER_IDS = [TASK_FILTER_ID, NOTEBOOK_FILTER_ID];

	let showTaskButton = false;
	$: showTaskButton = (toggleFilters ?? []).some((f) => f.id === TASK_FILTER_ID);

	let taskEnabled = false;
	$: taskEnabled = (selectedFilterIds ?? []).includes(TASK_FILTER_ID);

	let showNotebookButton = false;
	$: showNotebookButton = (toggleFilters ?? []).some((f) => f.id === NOTEBOOK_FILTER_ID);

	let notebookEnabled = false;
	$: notebookEnabled = (selectedFilterIds ?? []).includes(NOTEBOOK_FILTER_ID);

	let activeMode = null;
	$: activeMode = taskEnabled
		? 'task'
		: notebookEnabled
			? 'notebook'
			: codeInterpreterEnabled
				? 'code'
				: webSearchEnabled
					? 'web'
					: null;

	// Extracted so a fourth mode does not need a fourth copy of the array juggling. For the
	// existing Task mode this produces byte-identical arrays to the inline expression it replaces:
	// [...filter(x => x !== TASK), TASK] when on, filter(x => x !== TASK) when off.
	const setToggle = (id, on) => {
		selectedFilterIds = on
			? [...(selectedFilterIds ?? []).filter((x) => x !== id), id]
			: (selectedFilterIds ?? []).filter((x) => x !== id);
	};

	const setMode = (mode) => {
		const off = activeMode === mode; // clicking the active one clears it
		webSearchEnabled = !off && mode === 'web';
		codeInterpreterEnabled = !off && mode === 'code';
		setToggle(TASK_FILTER_ID, !off && mode === 'task');
		setToggle(NOTEBOOK_FILTER_ID, !off && mode === 'notebook');
		onWebSearchToggle(webSearchEnabled);
	};

	const MODE_ON =
		'text-sky-500 dark:text-sky-300 bg-sky-50 hover:bg-sky-100 dark:bg-sky-400/10 dark:hover:bg-sky-600/10 border border-sky-200/40 dark:border-sky-500/20';
	const MODE_OFF =
		'bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border border-transparent';
	const MODE_BASE =
		'shrink-0 px-2 py-[5px] flex gap-1.5 items-center text-xs rounded-full transition-colors duration-300 focus:outline-hidden';
""",
    "mode state",
)

# 2. The three buttons, always visible, ahead of the Integrations trigger.
sub(
    """									<div class="flex flex-1 items-center min-w-0 overflow-x-auto scrollbar-none">
										{#if showWebSearchButton || showImageGenerationButton || showCodeInterpreterButton || showToolsButton || showSkillsButton || (toggleFilters && toggleFilters.length > 0)}
											<IntegrationsMenu""",
    """									<div class="flex flex-1 items-center min-w-0 overflow-x-auto scrollbar-none">
										<!-- ai-stack: the three modes, always visible, exactly one active -->
										{#if showWebSearchButton}
											<Tooltip content={$i18n.t('Search the internet')} placement="top">
												<button
													type="button"
													aria-pressed={webSearchEnabled}
													aria-label={webSearchEnabled
														? $i18n.t('Disable Web Search')
														: $i18n.t('Enable Web Search')}
													on:click|preventDefault={() => setMode('web')}
													class="{MODE_BASE} {webSearchEnabled ? MODE_ON : MODE_OFF}"
												>
													<GlobeAlt className="size-4" strokeWidth="1.75" />
													<span class="hidden sm:block">{$i18n.t('Internet')}</span>
												</button>
											</Tooltip>
										{/if}

										{#if showCodeInterpreterButton}
											<Tooltip content={$i18n.t('Code')} placement="top">
												<button
													type="button"
													aria-pressed={codeInterpreterEnabled}
													aria-label={codeInterpreterEnabled
														? $i18n.t('Disable Code Interpreter')
														: $i18n.t('Enable Code Interpreter')}
													on:click|preventDefault={() => setMode('code')}
													class="{MODE_BASE} {codeInterpreterEnabled ? MODE_ON : MODE_OFF}"
												>
													<Terminal className="size-3.5" strokeWidth="2" />
													<span class="hidden sm:block">{$i18n.t('Code')}</span>
												</button>
											</Tooltip>
										{/if}

										{#if showTaskButton}
											{@const taskFilter = (toggleFilters ?? []).find(
												(f) => f.id === TASK_FILTER_ID
											)}
											<Tooltip content={$i18n.t('Task via Agents')} placement="top">
												<button
													type="button"
													aria-pressed={taskEnabled}
													aria-label={taskEnabled ? $i18n.t('Disable Task') : $i18n.t('Enable Task')}
													on:click|preventDefault={() => setMode('task')}
													class="{MODE_BASE} {taskEnabled ? MODE_ON : MODE_OFF}"
												>
													{#if taskFilter?.icon}
														<img
															src={taskFilter.icon}
															alt={taskFilter?.name ?? 'Task'}
															class="size-3.5 {taskFilter.icon.includes('data:image/svg')
																? 'dark:invert-[80%]'
																: ''}"
														/>
													{:else}
														<Sparkles className="size-4" strokeWidth="1.75" />
													{/if}
													<span class="hidden sm:block">{taskFilter?.name ?? $i18n.t('Task')}</span>
												</button>
											</Tooltip>
										{/if}

										{#if showNotebookButton}
											{@const notebookFilter = (toggleFilters ?? []).find(
												(f) => f.id === NOTEBOOK_FILTER_ID
											)}
											<Tooltip content={notebookFilter?.name ?? $i18n.t('Notebook')} placement="top">
												<button
													type="button"
													aria-pressed={notebookEnabled}
													aria-label={notebookEnabled
														? $i18n.t('Disable Notebook')
														: $i18n.t('Enable Notebook')}
													on:click|preventDefault={() => setMode('notebook')}
													class="{MODE_BASE} {notebookEnabled ? MODE_ON : MODE_OFF}"
												>
													{#if notebookFilter?.icon}
														<img
															src={notebookFilter.icon}
															alt={notebookFilter?.name ?? 'Notebook'}
															class="size-3.5 {notebookFilter.icon.includes('data:image/svg')
																? 'dark:invert-[80%]'
																: ''}"
														/>
													{:else}
														<Sparkles className="size-4" strokeWidth="1.75" />
													{/if}
													<span class="hidden sm:block"
														>{notebookFilter?.name ?? $i18n.t('Notebook')}</span
													>
												</button>
											</Tooltip>
										{/if}

										{#if showImageGenerationButton || showToolsButton || showSkillsButton || (toggleFilters && toggleFilters.filter((f) => !MODE_FILTER_IDS.includes(f.id)).length > 0)}
											<IntegrationsMenu""",
    "mode buttons",
)

# 3. The dropdown keeps everything EXCEPT the three that now have their own buttons — one
#    control per concept, or the two disagree about what is on.
sub(
    """												selectedModels={selectedModelIds}
												{toggleFilters}
												{showWebSearchButton}
												{showImageGenerationButton}
												{showCodeInterpreterButton}""",
    """												selectedModels={selectedModelIds}
												toggleFilters={(toggleFilters ?? []).filter(
													(f) => !MODE_FILTER_IDS.includes(f.id)
												)}
												showWebSearchButton={false}
												{showImageGenerationButton}
												showCodeInterpreterButton={false}""",
    "menu props",
)

# 4. The Task chip would duplicate the Task button.
sub(
    """											{#each selectedFilterIds as filterId (filterId)}
												{@const filter = toggleFilters.find((f) => f.id === filterId)}
												{#if filter}""",
    """											{#each selectedFilterIds as filterId (filterId)}
												{@const filter = toggleFilters.find((f) => f.id === filterId)}
												{#if filter && !((filterId === TASK_FILTER_ID && showTaskButton) || (filterId === NOTEBOOK_FILTER_ID && showNotebookButton))}""",
    "task chip",
)

# 5. ...and so would the web-search and code-interpreter chips. Guarded rather than deleted, so
#    a model where the button is hidden still gets upstream's chip.
#
#    RE-DERIVED for 0.11.3. Upstream 0.10.2 gated these chips on the flag ALONE
#    (`{#if webSearchEnabled}`); 0.11.3 gates them on the flag AND the button's own visibility
#    (`{#if webSearchEnabled && showWebSearchButton}`), because it turned the chip from a passive
#    indicator into an interactive toggle. The intent here is unchanged — the chip must not
#    duplicate a mode button that is already on screen — so the re-derivation is the same
#    expression with upstream's new positive gate NEGATED, not the old gate restored. Restoring
#    `{#if webSearchEnabled}` would render the chip alongside our button and give one concept two
#    controls, which is the thing this hunk exists to prevent.
sub("""											{#if webSearchEnabled && showWebSearchButton}
												<Tooltip content={$i18n.t('Web Search')} placement="top">""",
    """											{#if webSearchEnabled && !showWebSearchButton}
												<Tooltip content={$i18n.t('Web Search')} placement="top">""",
    "web chip")
sub("""											{#if codeInterpreterEnabled && showCodeInterpreterButton}
												<Tooltip content={$i18n.t('Code Interpreter')} placement="top">""",
    """											{#if codeInterpreterEnabled && !showCodeInterpreterButton}
												<Tooltip content={$i18n.t('Code Interpreter')} placement="top">""",
    "code chip")

out = os.path.join(SC, "MessageInput.patched.svelte")
open(out, "w", encoding="utf-8").write(s)

rel = "src/lib/components/chat/MessageInput.svelte"
d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", SRC, out],
                   capture_output=True, text=True)
patch = d.stdout
dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SC, "task-mode.patch")
open(dest, "w", encoding="utf-8").write(patch)
print(f"wrote {dest}: {len(patch.splitlines())} diff lines, "
      f"{sum(1 for l in patch.splitlines() if l.startswith('+') and not l.startswith('+++'))} added")

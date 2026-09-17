#!/usr/bin/env python3
"""Add a Background tasks shortcut to the sidebar's top nav group.

The channel already exists and already appears further down under CHANNELS, but that is where
chat-like things live and it reads as one more room rather than as "where my alerts land". A
shortcut sits with New Chat / Search / Notes / Workspace, which is where someone looks when they
want to go somewhere rather than talk to something.

Deliberately NOT a hardcoded channel id: this resolves the channel by NAME out of the store the
sidebar already loads, so the patch is portable to any box and simply renders nothing when no such
channel exists. A pinned uuid would be wrong on every other install and silently dead here the day
the channel is recreated.

Run after 01 and 02; appends to the same combined patch.
"""
import os, subprocess, sys

SC = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SC, "Sidebar.svelte")
s = open(SRC, encoding="utf-8").read()


def sub(old, new, why, count=1):
    global s
    n = s.count(old)
    assert n == count, f"anchor matched {n}x (expected {count}): {why}\n---\n{old[:180]}"
    s = s.replace(old, new)


# The channel to surface. Named, not id'd — see the module docstring.
sub(
    """	$: pinnedItems = $settings?.pinnedMenuItems ?? DEFAULT_PINNED_ITEMS;""",
    """	$: pinnedItems = $settings?.pinnedMenuItems ?? DEFAULT_PINNED_ITEMS;

	// ai-stack: the channel background-task results are delivered to. Resolved by NAME from the
	// channel list the sidebar already loads, so this works on any install and renders nothing
	// when the channel is absent — a hardcoded id would be wrong everywhere else and would go
	// quietly dead here the day the channel is recreated.
	const TASKS_CHANNEL_NAME = 'background-tasks';
	let tasksChannel = null;
	$: tasksChannel = ($channels ?? []).find((c) => c?.name === TASKS_CHANNEL_NAME) ?? null;""",
    "tasks channel lookup")

# Rendered immediately after the pinned nav group, so it sits with the other "go somewhere" links
# rather than among the chat rooms.
#
# RE-DERIVED for 0.11.3, and this hunk needed more than a moved anchor. Upstream restructured the
# nav group: the pinned items moved into a `#pinned-menu-items-list` wrapper, the models guard
# became `$visiblePinnedModels.length > 0` (it was an inline `($models ?? []).length > 0 && ...`),
# and every sibling row was restyled — rounded-2xl -> rounded-xl, space-x-3 -> space-x-2,
# py-2 -> py-1.5, icons at size-4 with stroke-width 1.5 rather than size-4.5 at 2.
#
# So the markup below is not the 0.10.2 markup re-indented: it is that markup restated in the
# sidebar's new idiom, which is what keeps it looking native instead of conspicuously larger and
# misaligned next to New Chat / Search / Notes. The load-bearing parts are unchanged — the
# `sidebar-background-tasks-button` id (branding/ohmz.css may target it), the name-resolved
# channel href, and the aria-label.
old_close = """{/each}
\t\t\t\t\t\t</div>
\t\t\t\t\t</div>

\t\t\t\t\t{#if $visiblePinnedModels.length > 0"""
new_close = """{/each}
\t\t\t\t\t\t</div>

\t\t\t\t\t\t<!-- ai-stack: alerts and run logs live in a channel; this is the way in -->
\t\t\t\t\t\t{#if tasksChannel && $config?.features?.enable_channels && ($user?.role === 'admin' || ($user?.permissions?.features?.channels ?? true))}
\t\t\t\t\t\t\t<div class="px-1 flex justify-center text-gray-700 dark:text-gray-300">
\t\t\t\t\t\t\t\t<a
\t\t\t\t\t\t\t\t\tid="sidebar-background-tasks-button"
\t\t\t\t\t\t\t\t\tclass="group grow flex items-center space-x-2 rounded-xl px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-900 transition outline-none"
\t\t\t\t\t\t\t\t\thref="/channels/{tasksChannel.id}"
\t\t\t\t\t\t\t\t\ton:click={itemClickHandler}
\t\t\t\t\t\t\t\t\tdraggable="false"
\t\t\t\t\t\t\t\t\taria-label={$i18n.t('Background tasks')}
\t\t\t\t\t\t\t\t>
\t\t\t\t\t\t\t\t\t<div class="self-center flex size-4 shrink-0 items-center justify-center">
\t\t\t\t\t\t\t\t\t\t<svg
\t\t\t\t\t\t\t\t\t\t\txmlns="http://www.w3.org/2000/svg"
\t\t\t\t\t\t\t\t\t\t\tfill="none"
\t\t\t\t\t\t\t\t\t\t\tviewBox="0 0 24 24"
\t\t\t\t\t\t\t\t\t\t\tstroke-width="1.5"
\t\t\t\t\t\t\t\t\t\t\tstroke="currentColor"
\t\t\t\t\t\t\t\t\t\t\tclass="size-4"
\t\t\t\t\t\t\t\t\t\t>
\t\t\t\t\t\t\t\t\t\t\t<path
\t\t\t\t\t\t\t\t\t\t\t\tstroke-linecap="round"
\t\t\t\t\t\t\t\t\t\t\t\tstroke-linejoin="round"
\t\t\t\t\t\t\t\t\t\t\t\td="M14.857 17.082a23.848 23.848 0 0 0 5.454-1.31A8.967 8.967 0 0 1 18 9.75V9A6 6 0 0 0 6 9v.75a8.967 8.967 0 0 1-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 0 1-5.714 0m5.714 0a3 3 0 1 1-5.714 0"
\t\t\t\t\t\t\t\t\t\t\t/>
\t\t\t\t\t\t\t\t\t\t</svg>
\t\t\t\t\t\t\t\t\t</div>

\t\t\t\t\t\t\t\t\t<div class="flex flex-1 self-center translate-y-[0.5px]">
\t\t\t\t\t\t\t\t\t\t<div class=" self-center text-[0.8125rem] leading-5">
\t\t\t\t\t\t\t\t\t\t\t{$i18n.t('Background tasks')}
\t\t\t\t\t\t\t\t\t\t</div>
\t\t\t\t\t\t\t\t\t</div>
\t\t\t\t\t\t\t\t</a>
\t\t\t\t\t\t\t</div>
\t\t\t\t\t\t{/if}
\t\t\t\t\t</div>

\t\t\t\t\t{#if $visiblePinnedModels.length > 0"""
sub(old_close, new_close, "tasks shortcut markup")

out = os.path.join(SC, "Sidebar.svelte.new")
open(out, "w", encoding="utf-8").write(s)

rel = "src/lib/components/layout/Sidebar.svelte"
d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", SRC, out],
                   capture_output=True, text=True)
dest = sys.argv[1]
mode = "a" if len(sys.argv) > 2 and sys.argv[2] == "--append" else "w"
with open(dest, mode, encoding="utf-8") as f:
    f.write(d.stdout)
print(f"{'appended to' if mode == 'a' else 'wrote'} {dest}: "
      f"{sum(1 for l in d.stdout.splitlines() if l.startswith('+') and not l.startswith('+++'))} added")

# Regenerating `task-mode.patch`

The patch is produced by these scripts rather than hand-authored, so every anchor is checked: each
replacement asserts it matched exactly the expected number of times. A rebase that would have
applied a hunk to the wrong place fails here, while it is being made, instead of in the browser
where the symptom is a button that looks right and does nothing.

They edit the **upstream** sources, which are recoverable verbatim from the running image — the
build ships `.js.map` files carrying full `sourcesContent`:

```bash
docker exec open-webui cat /app/build/_app/immutable/chunks/<chunk>.js.map > mi.map
# then pull MessageInput.svelte / Chat.svelte / Sidebar.svelte out of sourcesContent
```

> **Corrected 2026-08-08.** That comment read `MessageInput.svelte / IntegrationsMenu.svelte /
> Chat.svelte` until today, which named the wrong third file. `IntegrationsMenu.svelte` is not
> vendored here and is never patched: `grep '^--- ' ../task-mode.patch` lists exactly three files
> (`MessageInput.svelte`, `Chat.svelte`, `Sidebar.svelte`, at patch lines 1, 175 and 299), and
> `IntegrationsMenu` occurs in the patch once, at line 134, as the name of a component the markup
> already referenced — `01_mode_buttons.py` uses it as a match anchor (lines 87 and 153), nothing
> more. `Sidebar.svelte` is the file `03_tasks_shortcut.py` opens and diffs against; that diff
> contributes **two** hunks to the patch, not one — `@@ -118,6 +118,14 @@` (the channel lookup) and
> `@@ -1227,6 +1235,40 @@` (the nav entry itself), of 14 hunks in `task-mode.patch` overall.
> An operator who followed the old comment after an upstream bump
> extracted a file no script reads and left the vendored `Sidebar.svelte` stale, so `03` diffed the
> new shortcut against the previous release's text. The assertion-based check this file opens with
> does not catch that — 03's anchors were satisfied by the stale copy. What breaks instead is the
> image build: the `Dockerfile` runs `git apply --verbose /tmp/task-mode.patch`, which takes no
> fuzz, so the Sidebar hunks fail on context that moved, and the failure names a file the operator
> never re-extracted.

They expect the upstream `MessageInput.svelte`, `Chat.svelte` and `Sidebar.svelte` beside them
(vendored here so a rebase starts from a known base), and run in order:

```bash
python3 01_mode_buttons.py /tmp/p1.patch      # also writes MessageInput.patched.svelte, which 02 reads
python3 02_mode_persistence.py ../task-mode.patch
python3 03_tasks_shortcut.py ../task-mode.patch --append
```

`01` adds the three exclusive mode buttons. `02` adds the `onModeChange` callback and the
chat-scoped mode memory, emitting the combined patch for those two files. `03` appends the
sidebar shortcut to the background-tasks channel.

Run on 2026-08-08 against a scratch copy of this directory, with the output paths pointed into that
copy, those three commands reproduced the committed `task-mode.patch` byte for byte: `diff` between
the two was empty, 14377 bytes. The recipe above is current.

**Byproducts (recorded 2026-08-08).** The run writes four files here, not the one the `01` comment
used to name on its own: `MessageInput.patched.svelte` (from `01`, and the input `02` loads),
`MessageInput.svelte.new` and `Chat.svelte.new` (from `02`, one per entry in its `EDITS` list), and
`Sidebar.svelte.new` (from `03`). Each is only the patched side of a `diff -u`; `task-mode.patch` is
the artifact, so all four are safe to delete once `03` has finished, and nothing in the build reads
them. `MessageInput.patched.svelte` has to survive between `01` and `02`, which loads it by name.
None of the four is ignored: `git check-ignore -v` exits 1 for all of them and `.gitignore` has no
`*.new` or `*.patched.svelte` entry (both measured 2026-08-08), so regenerating the patch leaves
four untracked files in `git status` — easy to mistake for vendored sources someone forgot to
commit. Adding those two patterns to `.gitignore` is the fix; it is not done — `.gitignore` was
still unchanged when this note was written.

After an upstream bump, re-extract the three sources from the new image's sourcemaps, replace the
vendored copies, and re-run. A hunk that no longer applies shows up as an assertion here, naming
which anchor moved — but only for the copies you actually replaced. A source left stale satisfies
its own assertions and defers the failure to `git apply` in the build, which is the trap the
2026-08-08 correction above describes.

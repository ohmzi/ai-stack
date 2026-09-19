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

> **Simpler, verified 2026-09-17.** You usually do not need the sourcemaps at all. The vendored
> copies are byte-identical to upstream **git** at the `OWUI_REV` the image was built from, so the
> tag itself is an equally good source and needs no running container:
>
> ```bash
> git clone --filter=blob:none --no-checkout https://github.com/open-webui/open-webui.git
> cd open-webui && git show v0.11.3:src/lib/components/chat/Chat.svelte > Chat.svelte
> ```
>
> Measured before the 0.11.3 rebase: all four then-vendored files matched `git show $OWUI_REV:<path>`
> by md5. If a future bump ever disagrees, the sourcemap route above is the tiebreaker — it is what
> the image actually built.

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

> **Extended 2026-08-11.** A fourth file is now vendored: `auth+page.svelte`
> (`src/routes/auth/+page.svelte`), patched by `04_guest_link.py` — which despite its name now
> makes three sign-in-page changes, not one; see its docstring — starting with the public instance's
> "Continue without an account" link (`docs/PUBLIC_INSTANCE.md`). The same trap as above applies to
> it specifically: it is the one vendored file with no counterpart already living in this directory
> from before, so it is the easiest of the four to forget to re-extract after an upstream bump.
> `grep '^--- ' ../task-mode.patch` should list four files after `04` has run.

> **Extended 2026-09-17 (the 0.11.3 rebase).** A **fifth** file is now vendored:
> `Placeholder.svelte` (`src/lib/components/chat/Placeholder.svelte`), patched by `02` — which
> despite its name now makes edits in **four** files, not one. `grep '^--- ' ../task-mode.patch`
> should list **six** files after `04` has run. (Still six after `05` was added on 2026-09-17 —
> that one emits `shell-cache.patch`, a separate artifact. See "`05` is the odd one out" below.)
>
> **Extended 2026-09-18.** A sixth file joined: `Suggestions.svelte`
> (`src/lib/components/chat/Suggestions.svelte`), also patched by `02`. Its list is a fixed 144px
> box with `overflow-auto scrollbar-none`, so five 48px chips meant two were scrolled out of sight
> behind a hidden scrollbar — 5 in the DOM, 3 visible. The patch raises both classes to `h-60` /
> `max-h-60`. If the pool size changes, revisit those two numbers together.
>
> Why it was needed: 0.11.3 factored the no-messages landing page out of `Chat.svelte` into
> `Placeholder`, which renders its own `MessageInput` and declares its props explicitly (`export
> let`, no `$$restProps`). Svelte drops an undeclared prop **silently**, so the `onModeChange`
> callback that `02` wires into every composer host would have reached the landing-page composer
> and gone nowhere — and the landing page is exactly where a mode gets picked before the chat has
> an id, the case `MODE_KEY(null)` / `MODE_HANDOFF_MS` exist to serve. The symptom would have been
> a Task mode chosen on the landing page that quietly reverted on the first message.
>
> `02`'s `_n == 3` assertion is the guard: it counts the composer hosts that carry
> `onWebSearchToggle={handleWebSearchToggle}` and fails the build if upstream adds a fourth, so a
> new composer cannot ship unwired. The 0.10.2 patch asserted `_n == 2` (two `MessageInput`
> instances, no `Placeholder`).

They expect the upstream `MessageInput.svelte`, `Placeholder.svelte`, `Chat.svelte`,
`Sidebar.svelte` and `auth+page.svelte` beside them (vendored here so a rebase starts from a known
base), and run in order:

```bash
python3 01_mode_buttons.py /tmp/p1.patch      # also writes MessageInput.patched.svelte, which 02 reads
python3 02_mode_persistence.py ../task-mode.patch
python3 03_tasks_shortcut.py ../task-mode.patch --append
python3 04_guest_link.py ../task-mode.patch --append
python3 05_shell_cache.py ../shell-cache.patch    # a SECOND artifact — see below
```

`02` also makes the landing page's suggestion chips follow the mode button, and rotates them: each mode
owns a pool of eight prompts and the four shown advance one step per page load, so a
returning user meets a different corner of what the mode can do rather than the same four forever.
Storage is per-mode `localStorage`; a blocked store just starts from the top of the pool.

### The pools, and why the no-mode one looks the way it does

Ten prompts per mode, five shown, one step of rotation per page load — so ten distinct windows
before anything repeats. The mode pools are grounded in what each button does *behind* the UI:

| Mode | What it actually does | Prompts teach |
|---|---|---|
| Internet | SearXNG search; results injected as context before the model answers | news, research, docs, prices, verifying a claim |
| Code | Routes to `qwen38-coder:q4` on its own tenant **and** turns the code interpreter on | refactor, debug, optimise, tests, explain an error |
| Task | The whole turn goes to the hermes agent, which creates standing jobs | price watch, threshold alert, daily check, flight watch, list/cancel jobs |
| Notebook | Answered from Open Notebook, scoped to the notebook named in the message | every "what's in here" phrase, plus two templates needing a notebook name |

**No mode is different, and deliberate.** It is the one screen where nothing has been declared, so
it doubles as the pitch: three rotating prompts of the ordinary kind (summarise, draft, explain,
brainstorm, plan, translate, compare, draw, quiz) **plus two FIXED entries** —

```
Write a script        no button needed — code routes itself
Set up a monitor      no button needed — it schedules itself
```

Those two exist to teach the least discoverable thing about this assistant: `pipe()` routes code
(`_is_code_request`) and background work (`_is_bg_task_request`) from the wording ALONE, with no
button pressed. A user who never presses a mode should still find out that it can write code and
schedule a monitor. The fixed pair guarantees that lesson appears on **every** visit, which a
rotating entry could not.


Each entry must be a `{title: [bold line, grey line], content: …}` OBJECT. `Suggestions.svelte`
renders `prompt.title[0]`/`[1]` and otherwise falls back to `prompt.content` plus the literal
word "Prompt" — so a pool written as plain strings renders as four chips reading *Prompt*. That
shipped on 2026-09-18 and no bundle grep could see it: the strings were all present in the built
JS, only a browser showed how they rendered. `02` now asserts the shape at generation time.

`01` adds the four exclusive mode buttons (Internet / Code / Task / Notebook — the
last two being toggle filters, so it emits both as data-driven blocks off each filter's
own frontmatter; a fifth mode is a copy of one block plus its filter id). `02` adds the `onModeChange` callback and the
chat-scoped mode memory, emitting the combined patch for those two files. `03` appends the
sidebar shortcut to the background-tasks channel. `04` appends the sign-in page's three changes:
the guest link, the brand wordmark span (`branding/ohmz.css` colours the "AI" amber off the back of
it), and the logo moved from the fixed corner into the card.

**`05` is the odd one out, and deliberately so.** It emits its own `shell-cache.patch` and is not
part of `task-mode.patch`, because the two differ in three ways that all argue for separate
artifacts:

- **Different language, different file.** `05` patches `backend/open_webui/main.py` — one class,
  `SPAStaticFiles`. The other four patch compiled Svelte sources.
- **Different point in the build.** `01`-`04` must run before `npm run build`; the result REPLACES
  `/app/build`. `05` edits a Python file that is never built and is LAYERED over the base image's
  own copy. One patch file would imply a sequencing that does not exist.
- **A count that other docs depend on.** `task-mode.patch` is documented here and in
  `docs/STACK_SETUP.md` as touching exactly **six** files, and operators are told to check that
  with `grep '^--- ' ../task-mode.patch`. Folding `main.py` in would make it seven and quietly
  break four statements that are used as a verification step. **`main.py` is intentionally not in
  it** — do not "fix" that.

It also means a conflict in a 4669-line `Chat.svelte` cannot block a one-hunk change to `main.py`,
and a failed `git apply` names the artifact to re-derive.

`05` takes the `"w"` form like `01`/`02` (it is the first writer of its own artifact, so there is
no `--append`), and writes `main.py.new` as its byproduct — already covered by the `*.new` ignore
above.

Run on 2026-08-08 against a scratch copy of this directory, with the output paths pointed into that
copy, the first three commands reproduced the committed `task-mode.patch` byte for byte: `diff`
between the two was empty, 14377 bytes. `04` was added later, on 2026-08-11 — see the extension note
above.

**Byproducts (recorded 2026-08-08).** The run writes four files here, not the one the `01` comment
used to name on its own: `MessageInput.patched.svelte` (from `01`, and the input `02` loads),
`MessageInput.svelte.new` and `Chat.svelte.new` (from `02`, one per entry in its `EDITS` list), and
`Sidebar.svelte.new` (from `03`). Each is only the patched side of a `diff -u`; `task-mode.patch` is
the artifact, so all four are safe to delete once `03` has finished, and nothing in the build reads
them. `MessageInput.patched.svelte` has to survive between `01` and `02`, which loads it by name.
**Fixed 2026-09-17.** None of them was ignored, so regenerating the patch left them sitting in
`git status` looking exactly like vendored sources someone forgot to commit. `.gitignore` now
carries `*.new` and `*.patched.svelte`. Both patterns are deliberately narrow — they do not match
any vendored source, including `Placeholder.svelte`, which is the one file here whose name a
looser pattern (`Placeholder*`, `*.svelte`) would have quietly untracked.

As of the 0.11.3 rebase the run writes **six**, one per file `01`-`04` touches: the four above plus
`Placeholder.svelte.new` (`02` now has four `EDITS` entries) and `auth+page.patched.svelte` (`04`,
which the 2026-08-11 note predates).

After an upstream bump, re-extract the sources from the new image's sourcemaps, replace the
vendored copies, and re-run. A hunk that no longer applies shows up as an assertion here, naming
which anchor moved — but only for the copies you actually replaced. A source left stale satisfies
its own assertions and defers the failure to `git apply` in the build, which is the trap the
2026-08-08 correction above describes.

**`main.py` is a vendored source too, and inherits that trap exactly** (added 2026-09-17). It is
the largest file here at 3043 lines, and — being Python — it has no sourcemaps: re-extract it from
git at the pinned revision, never by hand.

```bash
git show $OWUI_REV:backend/open_webui/main.py > main.py    # OWUI_REV from ../Dockerfile
md5sum main.py        # must be 191d906bd0fcad8d892ea570a49cce96 at v0.11.3
```

Two guards watch it, and they watch different things. The `sha256sum` assertion in
`../Dockerfile` catches `OWUI_REV` and the base digest describing *different revisions* — a
mismatch `git apply` cannot see, because the anchors are identical across revisions and the patch
applies cleanly to the wrong file. The `sub()` count assertions in `05_shell_cache.py` catch
anchors that moved *within* a matching revision, but only when you regenerate. Neither runs if you
bump one and forget the other.

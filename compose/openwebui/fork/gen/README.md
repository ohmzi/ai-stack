# Regenerating `task-mode.patch`

The patch is produced by these scripts rather than hand-authored, so every anchor is checked: each
replacement asserts it matched exactly the expected number of times. A rebase that would have
applied a hunk to the wrong place fails here, while it is being made, instead of in the browser
where the symptom is a button that looks right and does nothing.

They edit the **upstream** sources, which are recoverable verbatim from the running image — the
build ships `.js.map` files carrying full `sourcesContent`:

```bash
docker exec open-webui cat /app/build/_app/immutable/chunks/<chunk>.js.map > mi.map
# then pull MessageInput.svelte / IntegrationsMenu.svelte / Chat.svelte out of sourcesContent
```

Both scripts expect `MessageInput.svelte` and `Chat.svelte` beside them, and run in order:

```bash
python3 01_mode_buttons.py /tmp/p1.patch      # writes MessageInput.patched.svelte as a side effect
python3 02_mode_persistence.py ../task-mode.patch
```

`01` adds the three exclusive mode buttons. `02` adds the `onModeChange` callback and the
chat-scoped mode memory, and emits the combined patch for both files.

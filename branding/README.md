# OhmzAI — Open WebUI skin

Warm-dark shell, one amber accent, Ω mark. Built from the `OhmzAI Brand.dc.html`
brand sheet and verified against Open WebUI **0.10.2**.

```bash
python3 branding/build_assets.py   # render the mark (only after editing it)
./branding/apply.sh                # install into the running container
./branding/apply.sh --revert       # put the stock look back
```

Then hard-refresh the browser (`ctrl-shift-r`) — `custom.css` and the favicons
are cached aggressively.

## What's here

| Path | What it is |
|---|---|
| `ohmz.css` | The theme. Installs as `custom.css`. |
| `assets/` | Rendered marks — favicons, splash, manifest icons. Committed. |
| `fonts/` | Space Grotesk + IBM Plex Mono `woff2`, self-hosted. |
| `build_assets.py` | Regenerates `assets/` from the font outline. |
| `apply.sh` | Idempotent installer / reverter. |

## How the theme works

0.10.2 is Tailwind v4, so the whole chassis is painted from a `--color-gray-*`
ramp that ships **pure neutral** — `oklch(L% 0 0)` at every step. Re-declaring
that ramp with the brand's warm greys rebrands every surface at once. There is
no `!important` selector chase, and nothing breaks when upstream reshuffles its
class names.

The app's own `oled-dark` preset does exactly this (see the inline script in
`index.html`), which is how the token roles were confirmed rather than guessed:

```
gray-950 / gray-900   canvas          gray-300   body text
gray-850              raised surface  gray-400   muted text
gray-800              line / hover    gray-100   headings
```

Three things the ramp can't reach are handled separately:

- **`body`** — its colours are literal hex in the app's stylesheet, not tokens.
- **`#splash-screen`** — painted `#000` by an inline `<style>` that is declared
  *after* `custom.css`, so matching its specificity would lose on source order.
  An extra `body` in the selector outranks it and stops the boot flash.
- **The accent** — stock OWUI spends *three* hues on "active/selected/info":
  `blue` (focus rings, citations), `sky` (composer toggles, the unread dot) and
  `indigo`. The brand allows one, so all three ramps point at amber. Red and
  green are left alone — destructive and success still need to read as
  themselves.

Fonts are self-hosted rather than pulled from Google. This stack runs
`OFFLINE_MODE=true`, `DO_NOT_TRACK=true`, `ANONYMIZED_TELEMETRY=false`; the UI
should not phone `fonts.gstatic.com` on every load.

## The mark

`build_assets.py` pulls the Ω outline straight from Space Grotesk — the brand
text face — pinned to `wght=500`, so the logo and the UI are drawn with the same
pen. It emits the vector favicon from the glyph contour and rasterises the PNGs
from the same source, sized so the ink is ~0.42 of the tile (the brand sheet's
38px type on a 64px tile). Maskable manifest icons get a smaller, full-bleed
treatment so the OS circle-crop doesn't clip the mark.

Re-run it only if you change the mark; `assets/` is committed.

## Where this installs, and why it's a script

`main.py:2567` mounts `/static` from `STATIC_DIR`, which `env.py:240` resolves
to **`/app/backend/open_webui/static`**. It is *not* `/app/build/static` — that
directory also exists, is a leftover of the front-end build, and is served to
nobody. Writing branding there looks like it worked and changes nothing.

That directory lives inside the image. This container's only bind mount is
`/app/backend/data`, so the skin survives `docker restart` but is wiped by
`docker rm` or an image pull. **Re-run `apply.sh` after either.** It is
idempotent, and it stashes the stock files in `.stock-backup` on first run only,
so re-running never overwrites the pristine originals with branded ones.

Brand webfonts go in `$STATIC/ohmz-fonts/`, deliberately not `$STATIC/fonts/` —
that one already holds the Noto family the PDF exporter needs.

## The sign-in screen

The auth route paints its own full-bleed backdrop with a hardcoded
`dark:bg-black`, which is why it stayed pure black while the rest of the app
went warm. It resolves `--color-black`, so the token layer fixes it; the form
itself gets boxed fields, an amber primary button, and an autofill override —
Chrome paints autofilled inputs pale blue over whatever background you give
them, and an inset `box-shadow` is the only thing that reaches them.

Those rules are scoped to the auth container so dialogs and settings forms
elsewhere keep their own styling.

## The app name

`#sidebar-webui-name` is set to the brand lockup in CSS — Ohmz in text, **AI**
in amber — so the sidebar reads *OhmzAI* with no container changes. This is
**cosmetic only**: the DOM text, the page title and the sign-in heading still
render whatever `WEBUI_NAME` is.

For those, `WEBUI_NAME` is read from the environment at import, so it needs the
container **recreated**, not restarted — and this one was created by hand, with
no `com.docker.compose.*` labels, so that means reconstructing its `docker run`.

Worth knowing before you bother: `env.py:842-844` appends `" (Open WebUI)"` to
any `WEBUI_NAME` that isn't the default, so it renders as
**"OhmzAI (Open WebUI)"**. That suffix is upstream's attribution and no env var
suppresses it.

```
WEBUI_NAME=OhmzAI
```

Note that Open WebUI's licence only permits removing its branding for
deployments of 50 users or fewer (or with a commercial agreement). This is a
single-user instance, which is why the lockup above is fine here. The version
footer is left as-is.

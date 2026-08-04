# OhmzAI — Open WebUI skin

Warm-dark shell, one amber accent, Ω mark. Built from the `OhmzAI Brand.dc.html`
brand sheet and verified against Open WebUI **0.10.2**.

```bash
python3 branding/build_assets.py   # render the mark (only after editing it)
./branding/apply.sh                # install into the running container
./branding/apply.sh --revert       # put the stock look back
python3 tests/test_branding.py     # 43 checks; --restart adds the restart case
```

There are **four** surfaces here, not one, and each needs a different lever —
which is most of what this file is about:

| Surface | Lever | Why the others cannot reach it |
|---|---|---|
| Look | `ohmz.css` | — |
| App name in the running UI | `loader.js` rewrites `/api/config` | `WEBUI_NAME` would render "OhmzAI (Open WebUI)" |
| Home-screen shortcut, tab title, icons | `apply.sh` edits `index.html` | a browser fetches the manifest itself, not via `window.fetch` |
| UI copy saying *WebUI* | `i18n_brand.py` | i18n resources arrive by dynamic `import()`, also not via `window.fetch` |

## Caching — read this before debugging a "it didn't apply" report

This cost the most time of anything here, and it is invisible from the server:
the files on disk are correct, the edge just isn't serving them.

`ai.ohmz.cloud` sits behind Cloudflare, which caches `/static/*` for four hours
(`max-age=14400`, `cf-cache-status: HIT`). A client-side `cache: 'no-store'`
does **not** see through it — that only bypasses the *browser* cache and still
hits the edge, so a probe can report the new file while the `<script>` tag runs
an old one. Two layers, disagreeing.

`apply.sh` therefore fingerprints the asset URLs in `index.html` (`?v=<hash>`),
which is cached `DYNAMIC` — never — so a new hash busts both layers at once and
no refresh is needed for the assets. The same stamp is written into the icon
URLs inside `site.webmanifest`, which is also `DYNAMIC`.

**The stamp must cover every file that carries it.** It originally hashed three
(`ohmz.css`, `loader.js`, `favicon.svg`) while eleven URLs carried it, so
changing *the mark* left every icon URL byte-identical. Cloudflare kept serving
its four-hour copy under the unchanged key, and Chrome — whose favicon store is
keyed by icon URL and expires on the order of days, independently of the HTTP
cache — never refetched at all. A revamped logo simply never appeared, on a
shell that looked correctly fingerprinted, over files that were correct on disk
and correct on the wire. `tests/test_branding.py` now recomputes the stamp from
that file list and asserts the shell carries it.

Chrome's favicon store is the one layer a fingerprint reaches only on the *next*
change: an icon it has already cached under a URL stays until that URL moves.
That is fine as long as the stamp moves with the asset, which is the fix above.

The one thing a fingerprint can't bust is the HTML carrying it. Open WebUI
serves `/` with no `cache-control`, so browsers apply *heuristic* freshness —
roughly 10% of the document's age. Against a shell whose `Last-Modified` was
the image build date, that is days. Hence: **one** hard refresh after the first
install. After that the shell's `Last-Modified` is recent, browsers revalidate
it on almost every load, and later changes propagate on their own.

`window.__ohmzLoader` is a sentinel for exactly this — it answers "did the
browser actually run the current file?" in one lookup.

## What's here

| Path | What it is |
|---|---|
| `ohmz.css` | The theme. Installs as `custom.css`. |
| `loader.js` | App-name override for the running app. Installs as `loader.js`. |
| `i18n_brand.py` | Rebrands the UI copy that says *WebUI*. |
| `assets/` | Rendered marks — favicons, splash, manifest icons. Committed. |
| `assets/site.webmanifest` | The PWA manifest — what names a home-screen shortcut. |
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

## Where this installs — TWO directories, and both are mandatory

`main.py:2567` mounts `/static` from `STATIC_DIR`, which `env.py:240` resolves to
**`/app/backend/open_webui/static`**. But that directory is **rebuilt on every container start**:
`config.py:96-115` runs at import, unlinks every top-level file in it, then copies
**`/app/build/static/**/*`** over the top. Directories survive; files do not.

So `apply.sh` writes **both**:

| Path | Role |
|---|---|
| `/app/backend/open_webui/static` | what `/static` serves — write it so the skin is live immediately |
| `/app/build/static` | the source the above is rebuilt from — write it so a restart reproduces the brand |

`/app/build` itself is a third location, but it is not part of that dance — it
is not rebuilt at startup, which is exactly why `index.html` can be edited in
place. Its files are also served from the **site root**, which is where anything
ignoring the `<link>` tags looks for an icon. Stock ships the "OI" `favicon.png`
there and no `.ico` at all, so `/favicon.ico` fell through to the SPA catch-all
and answered `200 text/html`. `apply.sh` writes both.

An earlier revision of this file called `/app/build/static` "a leftover served to nobody". That was
**wrong**, and acting on it broke the skin on 2026-08-01: a plain `docker restart` copied stock
right back over the branded files. The failure is nasty because it is invisible from the server —
`index.html` keeps its `?v=` fingerprints while the assets they point at return **200 OK and zero
bytes**. `tests/test_branding.py` now asserts served *bytes* against the repo files, and with
`--restart` reproduces the regression end to end.

Both directories live inside the image, so `docker rm` or an image pull still wipes them: **re-run
`apply.sh` after either**. It is idempotent, and it stashes the stock files in `.stock-backup` on
first run only, so re-running never overwrites the pristine originals with branded ones.

Brand webfonts go in `$STATIC/ohmz-fonts/`, deliberately not `$STATIC/fonts/` — that one already
holds the Noto family the PDF exporter needs.

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

`loader.js` is where this is solved. `GET /api/config` returns `{"name": ...}`
and is the single source the whole front-end reads for the app name — the
sign-in heading, the sidebar and the document title all derive from it. The
loader wraps `window.fetch`, rewrites that one field, and everything downstream
says **OhmzAI**.

index.html loads `loader.js` with `defer` at line 34, ahead of the SvelteKit
entry at line 120, so the patch is in place before the app's first request. On
anything that isn't `/api/config` — or if the rewrite throws — it hands back the
untouched response.

Two things this beats:

- **`WEBUI_NAME`.** `env.py:842-844` appends `" (Open WebUI)"` to any value that
  isn't the default, so the env var can only ever produce *"OhmzAI (Open
  WebUI)"*. It also needs the container recreated rather than restarted.
  (Historic note: this used to add "and a restart signs everyone out" — no
  longer true since `0f95516` set `WEBUI_SECRET_KEY` in
  `compose/openwebui/run.sh`.)
- **A CSS text swap.** The heading has four variants ("Sign in to X", "Get
  started with X", "Signing in to X", "... with LDAP"). Replacing the string
  would fix one and break three.

`#sidebar-webui-name` still carries a CSS lockup on top, purely so the sidebar
renders the **AI** in amber.

### The name on a phone home screen is a different path

`loader.js` cannot reach it, and this is not a caching problem. A browser
fetches a web app manifest **itself** — not through `window.fetch` — so the
wrapper never sees it. And the manifest the shell asked for was
`/manifest.json`, the **backend route**, which builds its `name` from
`WEBUI_NAME` and therefore always said *Open WebUI*. Meanwhile the branded
`site.webmanifest` was installed into `/static` and referenced by nothing.

So `apply.sh` repoints `<link rel="manifest">` at `/static/site.webmanifest`.
Three further things the stock manifest got wrong, now fixed in ours:

- it declared its 512×512 `logo.png` as `500x500`, and **Chrome drops a manifest
  icon whose declared size does not match the file** — silently, so a correct
  icon and an ignored one look identical. The test checks declared sizes against
  each PNG's IHDR.
- `background_color` was `#343541`, a grey belonging to no theme here.
- it declared no `id`, `scope` or non-maskable icon.

`share_target` is carried over from the stock manifest — without it Android
loses OhmzAI from the system share sheet.

Three more things live only in the shell, where no amount of correct files under
`/static` can reach them:

| In `index.html` | Stock | Why it matters |
|---|---|---|
| `<title>` | `Open WebUI` | what reads the raw HTML — bookmarks, link previews, iOS's add-to-home-screen prefill. `loader.js` only fixes the title once JS has run. |
| `apple-mobile-web-app-title` | absent | what iOS labels a home-screen icon with. |
| `theme-color` | `#171717` | the Android status bar and Chrome-mobile's tab strip. |

The `theme-color` **meta tag is not enough on its own**: the inline anti-FOUC
script `setAttribute`s it from its own hardcoded table a frame later, so the tag
is overwritten on every load. `apply.sh` rewrites the dark entry in that script
too, matched single-quoted — which is exactly and only how the script spells it,
the markup uses double quotes. Light (`#ffffff`), oled-dark (`#000000`) and her
(`#983724`) are deliberately untouched; `#1a1917` is the *dark* canvas
specifically (`--color-gray-900` / `--color-black`).

An already-added home-screen shortcut keeps the name and icon it was created
with — the OS copies both at install time. Remove it and add it again.

### The UI copy that says WebUI

A third path again, and `loader.js` reaches this one no better than it reaches
the manifest. The pending-activation page, *WebUI Settings*, *WebUI URL* and the
webhook hints are **i18n keys compiled into the frontend**, and i18next pulls
its resources with a dynamic `import()` — which does not go through
`window.fetch`, so the wrapper never sees them.

`i18n_brand.py` uses the lever i18next already provides. `en-US/translation.json`
ships every value as `""`; English is the fallback, so what renders **is the
key**. Give a key a non-empty value and that value wins — no source patched, no
key broken for any other locale, and no frontend rebuild.

It finds the chunk through the app's own locale registry
(`"./locales/en-US/translation.json": () => import("./DwGFF-zt.js")`) rather
than by filename, because Vite content-hashes those on every build.

The rewrite is a **rule, not a list**, so a string added upstream is picked up
instead of quietly keeping stock wording:

```
"the WebUI"  -> "OhmzAI"                 "Open WebUI" -> "OhmzAI"
"your WebUI" -> "your OhmzAI instance"    "WebUI"     -> "OhmzAI"
```

Order is load-bearing. The article rules exist because a bare substitution reads
as *"To access the OhmzAI"*, and they must run before `"Open WebUI"` or
*"maintained by the Open WebUI team"* loses its article too. Note that
`"the OhmzAI team"` is a correct result — the tests assert each rule's **output**
rather than the absence of `"the OhmzAI"`, since no cheap pattern separates that
from `"the OhmzAI, please"`.

Idempotent in both directions, because the rule is applied to the current value
when there is one and already-branded text has nothing left to match. `--revert`
restores the chunk byte-for-byte.

Why not a fork rebuild: `compose/openwebui/fork/` is the right tool when
*behaviour* changes. This is product copy — a replacement either matched or it
did not, and the script asserts every occurrence is accounted for — against
which a rebuild is `npm ci && npm run build` plus a container recreate.

`/_app/immutable/` sounds like it would fight this and does not: the build sends
no `cache-control` there, only `etag` + `last-modified`, and Cloudflare reports
`REVALIDATED`. Editing a chunk changes its etag and both layers pick it up. If
upstream ever starts sending `immutable`, this has to rename the chunk instead.

Scope was the owner's call (2026-08-04): **every** occurrence, including the ones
naming the upstream project — version strings, Community links, the funding
notice. Some of those now label an external service with our name; that is
known. The licence permits removing the branding at 50 users or fewer.

Open WebUI's licence permits removing its branding for deployments of 50 users
or fewer (or with a commercial agreement). This is a single-user instance. The
version footer is left as-is.

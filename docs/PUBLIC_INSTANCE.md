# The public instance — chat without an account

`ai.ohmz.cloud` keeps public signup open but requires admin approval
(`ui.default_user_role = "pending"`) — every new visitor waits for the owner. There was no way to
just try the product. `https://aipublic.ohmz.cloud` is that way in: a **second, disposable OpenWebUI
container**, reached from a
"Continue without an account" link on the private instance's sign-in page, that gives every visitor
a throwaway identity, one model, chat only, and nothing else.

**Why a second container and not a flag on the first one.** The private instance carries pipes —
`auto_assistant`, `photoreal`, `animate_scail` — that POST straight to ComfyUI on `localhost:8188`
from inside the container. Those media branches are gated by nothing OpenWebUI knows about: not
`image_generation.enable`, not a model's `capabilities`, not a valve
(`pipes/auto_assistant.py:6837-6886`). "Chat only, no image or video" on that instance would be a UI
convention, not a boundary. It also holds the owner's chats, memories and the background-task
channel. None of that belongs one bad prompt away from an anonymous visitor.

So the public instance runs **stock OpenWebUI with nothing deployed to it** — no pipes, no filters,
no functions — on a network that cannot reach ComfyUI, the hermes gateway, or qdrant at all. "No
image or video generation" here is a fact about the network, not a setting someone could flip back.

## Architecture

```
                    ai.ohmz.cloud ──► cloudflared ──► 0.0.0.0:4567   open-webui (unchanged)
                                                                      └─ pipes, ComfyUI, hermes …

  aipublic.ohmz.cloud ──► cloudflared ──► 0.0.0.0:4568
                                                            │
                                          ┌─────────────────▼──────────────────┐
                                          │ owui-public-gate   (nginx:alpine)  │
                                          │  :80  public  → mints guest id     │
                                          │  :81  admin   → fixed owner id     │◄── 127.0.0.1:4570
                                          └─────────────────┬──────────────────┘   (never tunneled)
                                    network owui-public (172.16.240.0/24, pinned)
                                                            │
                                          ┌─────────────────▼──────────────────┐
                                          │ open-webui-public                  │
                                          │  stock image, no GPU, no pipes     │
                                          │  trusted-header auth, no signup    │
                                          └─────────────────┬──────────────────┘
                                                            │ 172.16.240.1:11435
                                          ┌─────────────────▼──────────────────┐
                                          │ owui-public-ollama (alpine/socat)  │
                                          │  network_mode: host                │
                                          │  → 127.0.0.1:11434  (Ollama only)  │
                                          └────────────────────────────────────┘
```

Compose: [`../compose/public/docker-compose.yml`](../compose/public/docker-compose.yml). Gate
config: [`../compose/public/nginx/`](../compose/public/nginx/).

## How "no login" actually works

OpenWebUI 0.10.2 has a built-in escape hatch from password auth: **trusted-header auth**
(`WEBUI_AUTH_TRUSTED_EMAIL_HEADER`). Set it, and the backend trusts whatever email arrives in that
header instead of a session cookie of its own — auto-creating a user for an email it hasn't seen,
regardless of `ENABLE_SIGNUP`. The frontend's `/auth` page auto-signs-in and never renders a form
when this is on. Verified by reading `routers/auths.py` and `utils/auth.py` in the running image,
not assumed:

- `signin`, on a trusted header: if no user has that email, it calls `signup_handler(...)` — auto
  creation, unconditional. `signup_handler` assigns `ui.default_user_role`, then **promotes the user
  to admin if it's the only user in the table**, no matter how it got created. Bootstrap order below
  exists entirely because of this one line.
- `WEBUI_AUTH_TRUSTED_GROUPS_HEADER`, honored on every signin via `Groups.sync_groups_by_group_names`
  (`models/groups.py:514`): matches an **existing** group by name, adds/removes membership. It does
  not create groups — the `Guests` group has to exist first (bootstrap step 3).
- `get_current_user`: if the trusted header's email doesn't match the presented JWT's own email, a
  hard `401 User mismatch. Please sign in again.` A stale token self-heals through one extra
  `/auth` round trip.

So the entire "no login" mechanism is: **inject a header OpenWebUI already trusts, from somewhere
the visitor cannot control.** That's `owui-public-gate`'s whole job.

### The gate (`compose/public/nginx/guest-gate.conf`)

Two doors, both nginx, both proxying to the same backend:

- **`:80` public** — published `0.0.0.0:4568`, every interface, matching how the private instance's
  `:4567` is exposed; LAN devices can reach it directly, not just through the tunnel. Cloudflare
  access never actually depended on this — cloudflared is a process on this same host and always
  dials `localhost`, so `127.0.0.1`-only was never a barrier to it — this is purely parity with 4567,
  and costs nothing extra since the content behind it is meant to be public anyway. Tunneled as
  `aipublic.ohmz.cloud`. On a cookie-less visit to `/`, mints a random
  32-hex id, sets it as an `HttpOnly; SameSite=Lax` session cookie (no `Max-Age` — closing the
  browser ends the identity, "fresh every visit" by design), and 302s back to itself so the SPA's own
  burst of sub-requests (JS chunks, `/api/config`, the websocket) never fires until that cookie is
  already attached. Every proxied request then gets `X-Ohmz-Guest: guest-<id>@public.ohmz.cloud` via
  `proxy_set_header` — which **replaces** any client-supplied header of the same name. That
  substitution, not the cookie shape check, is the actual security property: a request cannot forge
  its own identity by sending `X-Ohmz-Guest` itself. `Secure` is added conditionally — see
  [The scheme bug](#the-scheme-bug-fixed-worth-remembering) below for why it can't be hardcoded.
- **`:81` admin** (published `127.0.0.1:4570`, **never given a Cloudflare route**) — a fixed owner
  identity (`compose/public/nginx/_admin_proxy.snippet`), no cookie logic at all. Loopback-only:
  reachable from the host or over SSH, not from the internet. This is how the owner administers an
  instance whose login form is permanently off, and how `scripts/purge_public_guests.py`
  authenticates without a password.

**The cookie is a bearer token**, not a proof of who minted it — the shape check
(`^[0-9a-f]{32}$`) exists to stop a malformed value from breaking the email string it's interpolated
into, not to authenticate. Whoever holds a given `ohmzgid` value holds that guest's session. Accepted
deliberately: no file upload, no long-term persistence that matters, session cookies clear at browser
close, and idle accounts are reaped server-side regardless (below).

**A signin-storm rate limit exists because one already happened.** During setup (2026-08-11), one
browser tab fired ~500 `POST /api/v1/auths/signin` in 5 seconds — most likely tangled up with testing
over plain `http://localhost` (a `Secure` cookie's persistence there is less predictable than over a
real Cloudflare-terminated `https://` origin) compounded by a redirect bug that existed at the same
moment (next section). It self-resolved and only produced 8 duplicate guest rows, not hundreds, but
nothing bounded it at the time. `guest-gate.conf` now caps `/api/v1/auths/signin` at `6r/m` with a
burst of 3 — generous for a real visitor signing in once, tight enough that a retry loop of any
origin (client bug, flaky network, deliberate hammering) can't mint more than a handful of accounts.

### The scheme bug (fixed, worth remembering)

Cloudflare Tunnel and a direct hit to this port are, to nginx, the exact same thing: plain HTTP on
the exact same loopback socket, because Cloudflare always terminates TLS at its own edge and connects
to origins over HTTP regardless of what the browser used. Two real bugs came from code that assumed
otherwise, both found the same way — testing locally never surfaced either, and both failed silently
behind Cloudflare in production.

**The redirect.** The bootstrap hop originally read `return 302 /;` — a bare path, which nginx
resolves against **its own** view of the request: `$scheme`. That's always `http` on the hop nginx
actually sees, so the redirect silently downgraded every real visitor to
`http://aipublic.ohmz.cloud/` — a scheme Cloudflare doesn't serve on that hostname, so the hop just
failed. Fixed by hardcoding the target: `return 302 https://$host/;`. If a future edit reintroduces a
bare `return 302 /` or `return 302 $uri`, expect the exact same silent failure.

**The cookie.** `ohmzgid` was unconditionally `Secure`. Over the real `https://` origin a browser
sees, that's correct and necessary; over plain `http://<host>:4568` — which is indistinguishable from
the real hop to nginx, per the paragraph above — a browser can refuse to persist it, and every
subsequent request then mints a *different* random identity, each mismatching the last one's JWT,
each triggering the frontend's `401 → redirect to /auth → sign in again` handler
(`src/routes/+layout.svelte`'s `redirectToAuthAfterUnauthorized`). Reproduced 2026-08-11: ~500
`POST /api/v1/auths/signin` in 5 seconds from one tab, and — separately, on a load that didn't race
as badly — a chat pane stuck on a permanent loading spinner while the sidebar rendered fine, because
the identity churn kept every subsequent authenticated fetch failing while the shell itself needed no
auth to paint. Fixed by making `Secure` conditional on `CF-Connecting-IP` being present — a header
only Cloudflare's edge sets, so its presence is the one signal that's genuinely different between the
two paths (`map $http_cf_connecting_ip $cookie_secure` in `guest-gate.conf`). Real internet-facing
traffic still gets `Secure`; local testing over plain HTTP now also just works.

## What's off, and how

`open-webui-public` runs the **stock**, digest-pinned upstream image — the same digest the private
instance's frontend fork (`compose/openwebui/fork/Dockerfile`) builds `FROM`, not the fork itself.
Deliberately not the fork: its Internet/Code/Task buttons drive web search, the code interpreter and
the `task_mode` filter, all off or entirely absent here, so on this instance they'd be three controls
that render and do nothing.

| Layer | Mechanism |
|---|---|
| Image/video generation | Never structurally possible — ComfyUI is unreachable from this network (see Isolation). `ENABLE_IMAGE_GENERATION=false` etc. are belt-and-braces on top of that, not the actual boundary. |
| Web search, code interpreter, notes, channels, memories, calendar, automations, API keys | `ENABLE_*=false` instance-wide, plus matching `USER_PERMISSIONS_*=false` per-user defaults — the two have drifted independently upstream before, hence both. |
| File upload | `USER_PERMISSIONS_CHAT_FILE_UPLOAD=false` / `_WEB_UPLOAD=false`. `client_max_body_size 1m` in the gate is the real backstop. |
| Sign-up, login form | `ENABLE_SIGNUP=false`, `ENABLE_LOGIN_FORM=false` — `POST /api/v1/auths/signup` is a hard 403 regardless of these; see the router-level check in `test_public_instance.py`. |
| Title/tag/follow-up/autocomplete generation | All `ENABLE_*_GENERATION=false`. No task model is configured for this instance at all — the private instance's own notes (`openwebui-config-snapshot.md`) record that a task model which exists but isn't visible/granted silently falls back to answering with the **chat** model, i.e. a title-generation call would burn a real completion on whatever the guest is talking to. Simplest fix: never ask for one. |
| Admin-UI settings drifting after a restart | `ENABLE_PERSISTENT_CONFIG=false` — every row above is re-read from `docker-compose.yml` on every boot rather than living in a 300 MB sqlite blob, so the posture lives in git. Cost: admin-UI setting changes (other than model visibility / access grants, which are DB tables, not config rows) don't stick across a restart. |

## Isolation

`open-webui-public` is on its own bridge network, `owui-public`, pinned to `172.16.240.0/24`. Pinned
rather than left to Docker because this host already had **15** other docker networks occupying the
entire default pool (`172.17.0.0/16` through `172.31.0.0/16`) when this was built — the only range
left was `172.16.0.0/16`, and Docker's fallback pool after that is `192.168.0.0/16`, which collides
with this host's own LAN (`192.168.40.0/24`). An unpinned `up` would either fail or, worse, succeed
by handing the network a subnet that already routes to real LAN hosts.

That network has no route to `127.0.0.1` on the host, which is where ComfyUI (`:8188`, no auth), the
hermes gateway (`:8642`) and qdrant (`:6333`) all live. The one deliberate hole is Ollama: it's a
*host* systemd unit bound to `127.0.0.1:11434` only, so `owui-public-ollama` (an `alpine/socat`
container on `network_mode: host`) forwards `172.16.240.1:11435` (this bridge's gateway address) to
it and nothing else. `172.16.240.1:11435` needed an explicit `ufw allow from 172.16.240.0/24 to any
port 11435 proto tcp` rule — this host's UFW default-denies incoming, including from bridge networks
to host-bound ports, and Docker's own iptables rules don't override that.

Verify from inside the container:

```bash
docker exec open-webui-public curl -m3 -s -o /dev/null -w '%{http_code}\n' http://172.16.240.1:8188/   # 000
docker exec open-webui-public curl -m3 -s -o /dev/null -w '%{http_code}\n' http://172.16.240.1:8642/   # 000
docker exec open-webui-public curl -m3 -s -o /dev/null -w '%{http_code}\n' http://172.16.240.1:6333/   # 000
docker exec open-webui-public curl -m3 -s http://172.16.240.1:11435/api/tags                           # 200, model list
```

`tests/test_public_instance.py` runs exactly this matrix as its first check.

## Model and identity

One model is visible: `hermes-genesis:apex-compact`, the same model the private instance's `Ω
Assistant` runs on — shared, so this costs **zero extra VRAM** (already resident). It's raw, not
routed through `auto_assistant`; the workspace entry (`model` table row, survives
`ENABLE_PERSISTENT_CONFIG=false` because grants and model rows are separate tables, not config) gives
it a short standalone system prompt instead.

Model visibility in 0.10.2 is deny-by-default: every model is admin-only until an explicit
`access_grant` row exists (the private instance's own memory notes say the same). One row does the
whole job here — `model=hermes-genesis:apex-compact`, `group=Guests`, `permission=read` — and every
other Ollama tag (`gemma3:1b`, `hermes-genesis:agent`, `bge-m3`, …) stays invisible with no hiding
step needed.

## Trimming the chat UI

A guest with no account has no use for several controls OWUI shows by default. Where a real
permission exists, it's off — the compose file's `USER_PERMISSIONS_*` block:

| Removed | Permission |
|---|---|
| Chat-header Controls panel (temperature, system prompt override, …) | `USER_PERMISSIONS_CHAT_CONTROLS=false` |
| Temporary Chat toggle — meaningless when nothing outlives the tab either way | `USER_PERMISSIONS_CHAT_TEMPORARY=false` |
| Sidebar FOLDERS section | `USER_PERMISSIONS_FEATURES_FOLDERS=false` + instance-wide `ENABLE_FOLDERS=false` |
| Sidebar MODELS (pinned-shortcut) section | not a permission — `DEFAULT_PINNED_MODELS` is simply never set; `Sidebar.svelte` only renders it when something is pinned |

Three more have **no permission at all** in 0.10.2 — verified by reading the vendored source
(`src/lib/components/chat/Navbar.svelte`, `src/lib/components/layout/Sidebar/UserMenu.svelte`,
`src/routes/auth/+page.svelte`):

- the "···" chat-context menu (`#chat-context-menu-button`, gated only on the chat having an id)
- the account-avatar menu (`button[aria-label="User menu"]`, gated on nothing — any signed-in
  identity gets it)
- the "Signing in to OhmzAI ⟳" screen `/auth` shows while the trusted-header auto-signin is in
  flight — safe to hide unconditionally on `#auth-page` here, since this instance always has
  `auth_trusted_header=true` and that element's OTHER branch (the real email/password form) never
  renders on it at all

Hidden with CSS instead: [`compose/public/guest-ui.css`](../compose/public/guest-ui.css), installed
by [`compose/public/apply_guest_ui.sh`](../compose/public/apply_guest_ui.sh) — **not** part of
`branding/ohmz.css`, since that file installs onto both instances verbatim and these rules have no
business reaching the private one. Run order:

```bash
OWUI_CONTAINER=open-webui-public ./branding/apply.sh    # shared skin, first
./compose/public/apply_guest_ui.sh                       # then the guest-only trim
```

`apply_guest_ui.sh` **appends** to `custom.css`; `branding/apply.sh` **overwrites** it wholesale. Any
`branding/apply.sh` pass on this container — after a recreate, an image pull, or just a routine
re-apply — silently wipes the appended rules, so `apply_guest_ui.sh` has to run again after every
one. Its idempotency guard is a marker comment, not a content hash — if `guest-ui.css` gains new
rules without the marker text itself changing (as happened 2026-08-11 adding the `#auth-page` rule
above), re-running `apply_guest_ui.sh` on its own sees the marker already present and skips, leaving
the OLD content installed. Run `branding/apply.sh` first whenever `guest-ui.css` changes, same as
after a recreate — that wipes `custom.css` back to just the shared skin, so the guard correctly sees
no marker and appends the current file.

**A fourth thing has no CSS-reachable fix at all**: the same auto-signin also fires OWUI's own
`toast.success("You're now logged in.")` — the exact toast a real user's deliberate signin
triggers, since both paths call the same `setSessionUser` function. There's no prop on `<Toaster>`
to filter one toast by content, and no permission gates it. Suppressed instead from
`branding/loader.js`, in the same hostname-gated branch that sets `window.__ohmzGuestUrl`: a
`MutationObserver` watches for `[data-sonner-toast]` nodes matching `/now logged in/i` and hides
them, disconnecting after 10s so it can never swallow a real, later toast (an error, a rate-limit
notice) that happens to land near the same words. English-only, matching this instance's only
branded locale.

## Bootstrap (fresh instance only)

Order matters because of `signup_handler`'s "user #1 becomes admin" rule — **verified the hard way**:
an isolation-test curl through the public door before the owner ever visited the admin door handed
admin to that request's guest identity instead. Bring the stack up, then hit `:81` before anything
else ever reaches `:80`.

1. `./compose/public/up.sh` — generates `/volume1/docker/openwebui-public/secret.env`
   (`WEBUI_SECRET_KEY`) on first run, then `docker compose -p owui-public up -d`.
2. **Before anyone else touches `:80`**, sign in on the admin door to create the owner account (it's
   user #1, so `signup_handler` promotes it):
   ```bash
   curl -s -X POST http://127.0.0.1:4570/api/v1/auths/signin \
     -H "Content-Type: application/json" -d '{"email":"x","password":"x"}'
   ```
   The email/password body is required by the request schema but never consulted — the admin door's
   `X-Ohmz-Guest` header (`compose/public/nginx/_admin_proxy.snippet`) is the real identity. **Edit
   that file's placeholder to the identity you want before this step**, or you get whatever was left
   in it.
3. Create a group named exactly `Guests` (the name `guest-gate.conf` injects via
   `X-Ohmz-Guest-Groups`):
   ```bash
   curl -s -X POST http://127.0.0.1:4570/api/v1/groups/create \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"Guests","description":"Public, no-account guest sessions"}'
   ```
4. Grant that group read access to the guest model — this is the single `access_grant` row from
   above:
   ```bash
   curl -s -X POST http://127.0.0.1:4570/api/v1/models/model/access/update \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"id":"hermes-genesis:apex-compact","name":"Assistant",
          "access_grants":[{"principal_type":"group","principal_id":"<GUESTS_GROUP_ID>","permission":"read"}]}'
   ```
5. Give it a system prompt (`POST /api/v1/models/model/update` — **must** re-include the same
   `access_grants` array from step 4, or `ModelForm`'s validator rejects the request outright with a
   500 for a missing required field — it is not optional-and-preserved).
6. Put the `Guests` group id into `compose/public/.env` as `GUEST_GROUP_ID` (gitignored — per-install
   state, would be wrong on any other host), then `./compose/public/up.sh --force-recreate` so
   `DEFAULT_GROUP_ID` picks it up.
7. `OWUI_CONTAINER=open-webui-public ./branding/apply.sh && ./compose/public/apply_guest_ui.sh` —
   re-run both after any recreate or image pull, same as the private instance for the first one; see
   [Trimming the chat UI](#trimming-the-chat-ui) for why the second is a separate step.

## The message quota

The gate has always carried `limit_req` on chat, but that is a **rate** limit — 20 requests a
minute, refilled every minute, forever. It stops a burst and does nothing about someone patiently
holding a free GPU all day. `owui-public-quota`
([`compose/public/quota/guest_quota.py`](../compose/public/quota/guest_quota.py)) is the ceiling:
**20 messages per IP per rolling 24 hours**, after which the visitor is asked, in-chat, to sign up.

Both numbers are env vars on the service in `docker-compose.yml` — `GUEST_QUOTA_LIMIT` and
`GUEST_QUOTA_WINDOW_HOURS`. Changing one needs only
`docker compose -p owui-public -f compose/public/docker-compose.yml up -d owui-public-quota`;
nothing else in the stack reads them.

**nginx asks, it doesn't proxy.** The quota service is never in the request path for a chat that is
allowed:

```
POST /api/chat/completions ─► auth_request /_quota_check ─► owui-public-quota /check
                                   204 ─► proxied to open-webui-public as normal
                                   403 ─► error_page @quota_limited ─► /limited
```

So the hot path costs one tiny subrequest, nginx still does all the streaming, and a bug in the
quota service cannot corrupt a chat response — only wrongly allow or wrongly deny. Three details
that make this work and are easy to break:

- The location is an **exact** match (`= /api/chat/completions`), which outranks the `/api/chat/`
  prefix block. `/api/chat/completed` and friends are per-turn bookkeeping calls; counting those
  would burn several quota units per actual message.
- `auth_request` treats **only** 204 and 403 as a verdict. Anything else — service down, an
  unhandled exception — becomes a 500, so the quota **fails closed**: guests see an error rather
  than an unlimited demo. That is the right direction, but it means the service dying takes chat
  with it, which is why the watchdog has a `pubquota` check separate from `pubgate` (`/api/config`
  doesn't go through `auth_request`, so `pubgate` stays green through exactly this failure).
- `error_page 403` can only ever catch nginx's **own** 403 from the subrequest, never one from
  OpenWebUI, because `proxy_intercept_errors` is off by default and upstream errors pass straight
  through. That is what makes it safe to hang the limit notice on a single status code.

**The notice is a chat message, not an error.** `/limited` answers 200 with a valid OpenAI-shaped
SSE stream, so the frontend renders it as an ordinary assistant turn — markdown, a signup link, and
how long until the window lifts — instead of the red toast a bare 403 produces. The parser it has
to satisfy is `src/lib/apis/streaming/index.ts`: `choices[0].delta.content` per `data:` line,
terminated by `data: [DONE]`. This assumes the request was streaming, which on this instance it
always is — title/tag/autocomplete generation are all disabled, so the chat UI is the only caller.

**Keyed on IP, and IPs are not stored.** The `ohmzgid` cookie identifies a session and clearing it
is one click, so a cookie quota would be decorative; IP is the only identifier a casual abuser
doesn't trivially control. The stored key is an HMAC of the address under a secret generated on
first run and kept on the data volume — the table is a set of opaque digests, enough to count
against and useless as a record of who visited. Two accepted consequences: visitors behind one NAT
share a quota, and anyone with an address pool can still cycle it. This is a speed bump for casual
abuse, not a defense against a determined attacker — Cloudflare's own WAF/rate limiting is the
layer for that, and it sits in front.

Note that `$client_ip` is `CF-Connecting-IP` when the request came through Cloudflare and
`$remote_addr` otherwise — and on the direct `:4568` path Docker's NAT masquerades every LAN client
to the bridge gateway, so **local testing shares one bucket**. Only the Cloudflare path
distinguishes real visitors.

Inspecting and resetting, both from the host:

```bash
# who is currently counted (opaque keys, truncated) — no nginx route points here
docker exec owui-public-quota python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9099/stats').read().decode())"

# forgive everyone at once: delete the HMAC secret, every key becomes unreachable
docker exec owui-public-quota rm -f /data/quota_secret && docker restart owui-public-quota
```

## Operating it

**Guest accounts accumulate — the purge job is load-bearing, not optional.** "Fresh every visit"
means every browser session mints a *new* user row and its chats; the database grows with traffic,
never with distinct visitors, and nothing else prunes it.

```bash
python3 scripts/purge_public_guests.py                        # dry run, 24h default cutoff
python3 scripts/purge_public_guests.py --yes                  # actually delete
python3 scripts/purge_public_guests.py --max-age-hours 6 --yes
```

Authenticates through the admin door, deletes through OWUI's own `DELETE /api/v1/users/{id}` (not
sqlite surgery) so chats and sessions actually cascade — that endpoint also independently refuses to
touch the primary admin or the caller's own account, a second guard on top of the script's own
`^guest-[0-9a-f]{32}@public\.ohmz\.cloud$` filter. Schedule it as a systemd timer alongside
`hermes-gateway.service` / `flightclaw.service`.

**Cloudflare Tunnel routing is dashboard-only and unrecorded here.** The tunnel is token-based
(`/etc/systemd/system/cloudflared.service`); there is no local ingress config file — adding or
changing a hostname is a Zero Trust dashboard edit that leaves no trace in this repo. Current
mapping: `aipublic.ohmz.cloud` → `http://localhost:4568`. **`:4570` must never get a route** — it is
the password-free admin door.

A single-level subdomain (`aipublic`, not `ai.public`) is deliberate, not a style choice: Cloudflare's
free Universal SSL certificate only covers one level of subdomain (`*.ohmz.cloud`). A two-level
hostname like `ai.public.ohmz.cloud` fails the TLS handshake at Cloudflare's own edge — before the
request ever reaches this tunnel — unless the zone has Advanced Certificate Manager. Verified the hard
way on 2026-08-11: that was the very first hostname tried here, and every symptom pointed away from
Cloudflare — no origin error, no nginx log entry, nothing reaching this host at all — because the
failure never got past the edge.

**After any recreate or image pull:** re-run `OWUI_CONTAINER=open-webui-public
./branding/apply.sh && ./compose/public/apply_guest_ui.sh` — same reasoning as the private instance
for the first, the served static assets live inside the image; the second because `apply.sh`
overwrites `custom.css` wholesale and would otherwise silently drop the guest-ui trim.

## The login-page link

`compose/openwebui/fork/gen/04_guest_link.py` patches the private instance's sign-in page
(`src/routes/auth/+page.svelte`, vendored as `auth+page.svelte` next to the other three files that
fork already patches) to add "Continue without an account" between the sign-in form and the OAuth
divider. Same generator pattern as `01`–`03`: anchored string replacement, asserts each anchor
matches exactly once, fails the build loudly on a moved anchor rather than shipping a dead link.

The target URL is not hardcoded into the patch. `branding/loader.js` sets
`window.__ohmzGuestUrl = 'https://aipublic.ohmz.cloud'`, guarded on `location.hostname !==
'aipublic.ohmz.cloud'` so the public instance's own branding pass (step 7 above) can never produce a
link back to itself — moot in practice, since the public instance runs the stock image and the patch
that reads that value was never built into it, but the guard costs nothing and removes the
possibility outright. A stock/unbranded build likewise renders nothing, same reasoning
`03_tasks_shortcut.py` gives for resolving its sidebar channel by name instead of by id.

Rebuilding after any change here: `docker build -t ai-stack/open-webui:task-mode
compose/openwebui/fork/`, then `compose/openwebui/run.sh`, then `./branding/apply.sh`.

## Verification

```bash
python3 tests/test_public_instance.py
```

Isolation matrix, guest arrival (cookie mint → auto sign-in → role=`user`, never `admin`, every
trimmed permission actually off — Controls, Temporary Chat, Folders, file upload, image gen, web
search, code interpreter), exactly one model visible, two guests never sharing an identity, a forged
identity header never escalating, `/api/v1/auths/signup` rejected, the admin door signing in as a
fixed separate identity, the signin rate limit engaging under a burst, and — against the admin door —
that `custom.css` is the shared skin plus exactly the guest-ui trim appended, and everything else
branding installs (`loader.js`, favicons, fonts) is byte-identical to the private instance's.

Confirm the private instance is untouched: `python3 tests/test_deployed.py` and
`python3 tests/test_branding.py --restart` should both still pass after any fork rebuild, and the
three Internet/Code/Task buttons should still switch each other off.

## Deliberately out of scope

- No pipes, filters, tools or functions are ever deployed to the public instance —
  `scripts/deploy_pipe.py` keeps pointing only at the private database
  (`/volume1/docker/openwebui/config/webui.db`).
- No changes to the private instance beyond the login-page link and the branding loader.
- Turnstile / WAF tuning beyond the one nginx rate limit — worth revisiting if the endpoint sees real
  public traffic.

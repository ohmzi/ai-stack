# OpenWebUI with the Internet / Code / Task mode buttons.
#
# WHY A FORK AT ALL
# -----------------
# Upstream renders Web Search, Code Interpreter and toggle filters as switches inside an
# "Integrations" dropdown, and shows them in the input row only once they are on. There is no
# concept of "exactly one of these at a time" — its single precedent is a hardcoded reactive
# statement that turns the code interpreter off when a terminal is selected.
#
# The alternative considered and rejected was a script that injects buttons into the running page
# and clicks the real controls for the user. It fails badly: the input row has no stable selector
# (Tailwind classes only), the labels it would have to match are translated strings, and the
# toggle-filter row carries no ARIA at all. Worst of all it fails SILENTLY — an upstream reshuffle
# leaves buttons that look like they work and toggle nothing, which for a control whose whole
# purpose is "this definitely became a background task" is the worst available outcome.
#
# WHAT THIS BUILDS
# ----------------
# The frontend, plus EXACTLY ONE backend file. The patches are:
#
#   task-mode.patch    (gen/01-04)  five Svelte sources — the Internet/Code/Task mode buttons.
#                                   Applied before `npm run build`; the result REPLACES /app/build.
#   shell-cache.patch  (gen/05)     backend/open_webui/main.py — one class, SPAStaticFiles, which
#                                   serves the SPA shell with `Cache-Control: no-cache` so a
#                                   browser can never reuse a shell whose chunks no longer exist
#                                   (the splash-screen hang). Is LAYERED over the base image's copy.
#
# Everything else is grafted onto the official image: the rest of the Python backend, the CUDA
# layers and every dependency stay byte-identical to upstream, and neither patch can reach them.
# The two differ in kind, which is why they are separate artifacts — see gen/05_shell_cache.py.
#
# The base is pinned BY DIGEST, not by the `cuda` tag: a floating tag would silently re-point this
# build at a frontend the patch was never tested against. Moving it is a deliberate edit.
#
# Build:  docker build -t ai-stack/open-webui:task-mode compose/openwebui/fork/
# Run:    compose/openwebui/run.sh   (which already points at that tag)
#
# AFTER AN UPSTREAM BUMP
#   1. Set OWUI_REV and the base digest to the new release, together — they must describe the
#      same build, and the assertion after the second FROM now ENFORCES that rather than trusting
#      it (it did not have to before shell-cache.patch: that patch layers a file over the base's).
#   2. Re-extract gen/main.py from the new revision (`git show $OWUI_REV:backend/open_webui/main.py`)
#      and re-run 05_shell_cache.py, alongside re-running 01-04 for the frontend sources.
#   3. Rebuild. `git apply` fails loudly on conflict; that failure is the signal to re-derive the
#      patch, not something to force past.
#   4. Re-run branding/apply.sh (the static assets live inside the image, as always).
#   5. python3 tests/test_deployed.py and python3 tests/test_branding.py, then confirm the three
#      buttons still switch each other off by hand. test_branding.py is what catches SPAStaticFiles
#      anchors that moved in a way `git apply` happened to accept.

# The revision the pinned base image was built from. OWUI_REV, the digest below, and gen/main.py all
# describe ONE build and MUST move together — a mismatch makes the backend neither revision, and
# the assertion after the second FROM is what turns that from a comment into a build failure.
#
# v0.11.3 (2026-08-31). The jump from 0.10.2 is a REBASE, not a bump: 0.11 rewrote the composer
# and the sidebar this patch edits (Chat.svelte +1608/-392, Sidebar.svelte substantially
# rewritten), and added a fifth file to the patch — Placeholder.svelte, the landing-page composer
# that 0.10.2 did not have. See fork/gen/README.md.
ARG OWUI_REV=2a960a59fe1dbbd35282f0556b3666d81102e781

FROM node:22-alpine AS webbuild
ARG OWUI_REV
# Upstream's package.json allows node >=18.13.0 <=22.x.x — 22 is the top of that range.
ARG BUILD_HASH=ai-stack-task-mode
RUN apk add --no-cache git python3 make g++
WORKDIR /src
RUN git clone --filter=blob:none https://github.com/open-webui/open-webui.git . \
    && git checkout ${OWUI_REV}
COPY task-mode.patch shell-cache.patch /tmp/
# --verbose so a conflict names the hunk. No fuzz and no 3-way: a patch that only ALMOST applies to
# a 2000-line component is exactly the case that should stop the build rather than guess.
RUN git apply --verbose /tmp/task-mode.patch
# The backend half. One file, one class — see shell-cache.patch and gen/05_shell_cache.py. Applied
# here because this stage is the only one with the source tree.
#
# The pristine copy is taken BEFORE the patch and carried into the runtime stage, where it is
# compared against the base image's own main.py. That comparison is the only thing that catches
# OWUI_REV and the base digest drifting apart — see the assertion there.
RUN cp backend/open_webui/main.py /tmp/owui-main.py.orig \
    && git apply --verbose /tmp/shell-cache.patch
ENV APP_BUILD_HASH=${BUILD_HASH}
RUN npm ci && npm run build

# The manifest INDEX digest of ghcr.io/open-webui/open-webui:v0.11.3-cuda. Read it back with:
#   docker buildx imagetools inspect ghcr.io/open-webui/open-webui:v0.11.3-cuda
# — that prints the index digest on the `Digest:` line, which is the value to paste here.
# (NOT `docker inspect <container> --format '{{.Image}}'`, which is the local config id and is not
# resolvable from a registry. NOT the per-platform digest on the `Manifests:` lines either: those
# pin one architecture, and an index digest is what keeps this build multi-arch like its base.)
#
# This SAME digest is the public instance's image — compose/public/docker-compose.yml. The two
# must name one OWUI release, or the fork's frontend and the public backend disagree about API
# shape. Move them together.
FROM ghcr.io/open-webui/open-webui@sha256:f27666b889001e1d2c639ca4af527aad6e466bd1a1ac70cae5ce13dfe35ebae6
# Re-declared: an ARG does not survive a FROM, and the label below and the assertion need it.
ARG OWUI_REV

# THE PIN HAS TO AGREE WITH ITSELF, and until shell-cache.patch nothing checked that it did.
# task-mode.patch only rewrites files this build REPLACES wholesale (/app/build), so a digest that
# did not match OWUI_REV was invisible — the frontend simply came from the clone. shell-cache.patch
# LAYERS a patched main.py over the digest-pinned one, so the two now have to agree about a file
# neither of them owned before. If they drift, the result is a backend that is neither revision,
# and `git apply` cannot catch it: the anchors are identical across revisions, so the patch applies
# cleanly to the wrong file. This is that check.
COPY --from=webbuild /tmp/owui-main.py.orig /tmp/owui-main.py.orig
RUN set -eu; \
    at_rev=$(sha256sum /tmp/owui-main.py.orig | cut -d' ' -f1); \
    in_base=$(sha256sum /app/backend/open_webui/main.py | cut -d' ' -f1); \
    if [ "$at_rev" != "$in_base" ]; then \
      echo "OWUI_REV (${OWUI_REV}) and the base digest describe DIFFERENT builds."; \
      echo "  main.py at that revision: ${at_rev}"; \
      echo "  main.py in this base:     ${in_base}"; \
      echo "They must move together — see AFTER AN UPSTREAM BUMP above."; \
      exit 1; \
    fi; \
    echo "OWUI_REV and the base digest agree on main.py (${at_rev})"; \
    rm -f /tmp/owui-main.py.orig

# The one backend file shell-cache.patch rewrites. Everything else in the image is untouched.
#
# The stale bytecode is deleted rather than left to Python's own mtime/size check. The base image
# ships a main.cpython-311.pyc compiled from the UNPATCHED file; if it ever survived, the container
# would start healthy, /health would pass, no log line would differ, and SPAStaticFiles would be the
# old code — the splash-screen hang intact and nothing to point at.
COPY --from=webbuild /src/backend/open_webui/main.py /app/backend/open_webui/main.py
RUN rm -f /app/backend/open_webui/__pycache__/main.cpython-*.pyc

LABEL ai-stack.owui-rev="${OWUI_REV}" \
      ai-stack.shell-cache="no-cache on the SPA shell (SPAStaticFiles)"

COPY --from=webbuild /src/build /app/build

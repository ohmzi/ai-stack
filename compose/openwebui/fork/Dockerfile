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
# ONLY the frontend. The result is grafted onto the official image, so the Python backend, CUDA
# layers and every dependency stay byte-identical to upstream — the patch cannot affect them.
#
# The base is pinned BY DIGEST, not by the `cuda` tag: a floating tag would silently re-point this
# build at a frontend the patch was never tested against. Moving it is a deliberate edit.
#
# Build:  docker build -t ai-stack/open-webui:task-mode compose/openwebui/fork/
# Run:    compose/openwebui/run.sh   (which already points at that tag)
#
# AFTER AN UPSTREAM BUMP
#   1. Set OWUI_REV and the base digest to the new release, together — they must describe the
#      same build or the frontend and backend disagree.
#   2. Rebuild. `git apply` fails loudly on conflict; that failure is the signal to re-derive the
#      patch, not something to force past.
#   3. Re-run branding/apply.sh (the static assets live inside the image, as always).
#   4. python3 tests/test_deployed.py, then confirm the three buttons still switch each other off.

# The revision the pinned base image was built from. These two MUST move together.
ARG OWUI_REV=ecd48e2f718220a6400ecf49eafd4867a38feb10

FROM node:22-alpine AS webbuild
ARG OWUI_REV
# Upstream's package.json allows node >=18.13.0 <=22.x.x — 22 is the top of that range.
ARG BUILD_HASH=ai-stack-task-mode
RUN apk add --no-cache git python3 make g++
WORKDIR /src
RUN git clone --filter=blob:none https://github.com/open-webui/open-webui.git . \
    && git checkout ${OWUI_REV}
COPY task-mode.patch /tmp/task-mode.patch
# --verbose so a conflict names the hunk. No fuzz and no 3-way: a patch that only ALMOST applies to
# a 2000-line component is exactly the case that should stop the build rather than guess.
RUN git apply --verbose /tmp/task-mode.patch
ENV APP_BUILD_HASH=${BUILD_HASH}
RUN npm ci && npm run build

# The manifest digest of ghcr.io/open-webui/open-webui:cuda as it was on 2026-08-03 — the same
# image the unforked stack was running. Read it back with:
#   docker inspect <image> --format '{{index .RepoDigests 0}}'
# (NOT `docker inspect <container> --format '{{.Image}}'`, which is the local config id and is not
# resolvable from a registry.)
FROM ghcr.io/open-webui/open-webui@sha256:d04b9ec1c9d5a6f8fb978158e5c7208d31beb2f9d1a27c58dd692dd8cee26f1a
# Everything else in the image is untouched.
COPY --from=webbuild /src/build /app/build

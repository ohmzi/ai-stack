#!/usr/bin/env python3
"""Serve the SPA shell with `Cache-Control: no-cache`, so it can never be reused without asking.

THE BUG THIS CLOSES. The shell boots by importing /_app/immutable/entry/{start,app}.<hash>.js and
calling kit.start(), which is what removes #splash-screen. Those filenames are content hashes, so a
rebuild changes them — and a client that reuses a STORED shell asks for chunks that no longer
exist. Every dynamic import rejects, kit.start() never runs, and the splash screen stays up
forever: the reported symptom is "the logo in the middle and nothing happens, unless I clear the
cache and log in again". One action, because clearing site data drops the auth token too.

Starlette sends no cache-control on that response, so browsers fall back to HEURISTIC freshness
(~10% of the document's age, RFC 9111 4.2.2) and may reuse a stored shell for hours without asking.
`no-cache` — not `no-store` — is the fix: it forbids reuse without REVALIDATION while still
allowing the response to be STORED, so the etag/last-modified comparison Starlette already
implements answers with a 304 instead of resending the shell on every load.

Two things the guard has to get right, and both were verified against this image rather than
reasoned about:

  * KEYED ON THE REQUEST, not the response. Starlette's NotModifiedResponse
    (starlette/staticfiles.py:22-36) whitelists only cache-control, content-location, date, etag,
    expires and vary — so a 304 carries NO content-type. A content-type guard would fire on the 200
    and silently skip the 304, and because RFC 9111 4.3.4 keeps the stored headers a 304 does not
    mention, a client that already holds a pre-fix shell would keep revalidating under its OLD
    heuristic policy and never converge — while an unconditional GET showed the header and looked
    correct. That is a green check against a still-broken phone.
  * A request for `/` arrives as '.' — NOT '' and NOT '/'. Verified with a Probe(StaticFiles) under
    TestClient: '/' -> '.', '/index.html' -> 'index.html', '/auth' -> 'auth', '/c/abc' -> 'c/abc'.
    StaticFiles.get_path is normpath(join(*route_path.split('/'))), and "/".split("/") is
    ['', ''] -> normpath('') -> '.'. A guard written as ('', '/', 'index.html') would skip the
    single most important request while the build stayed green.

Its OWN patch, not an --append to task-mode.patch. That one is documented (gen/README.md:36-37,
:52, :57) and relied on (docs/STACK_SETUP.md:135) as "exactly six frontend files", and is applied
BEFORE `npm run build` because it edits compiled Svelte. main.py is a backend file, is never built,
and is COPY'd from the build stage into the runtime stage by the Dockerfile. Keeping them apart also
means a failed `git apply` names the right artifact, and an upstream rebase of a 4669-line
Chat.svelte cannot block a one-hunk Python change.

Run on its own:  python3 05_shell_cache.py ../shell-cache.patch

main.py beside this file is the vendored upstream copy (md5 191d906bd0fcad8d892ea570a49cce96,
byte-identical to upstream git at OWUI_REV — checked against
`git show 2a960a59fe1dbbd35282f0556b3666d81102e781:backend/open_webui/main.py`). Re-extract after a
bump; `git show`, never copy-paste, because main.py is 4-space indented and one re-indented space
changes the diff context and makes `git apply` (no fuzz) fail:

    git show $OWUI_REV:backend/open_webui/main.py > main.py
"""
import os, subprocess, sys

SC = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SC, "main.py")
s = open(SRC, encoding="utf-8").read()


def sub(old, new, why, count=1):
    global s
    n = s.count(old)
    assert n == count, f"anchor matched {n}x (expected {count}): {why}\n---\n{old[:180]}"
    s = s.replace(old, new)


# The anchor is the WHOLE class verbatim (main.py:292-304), not a fragment: the body is being
# rewritten in place, and a partial anchor would let the replacement drift away from the branch it
# is supposed to wrap. In particular the `if path.endswith('.js'): raise ex` row is kept untouched:
# making a missing chunk return the shell with a 200 would be a different, far less legible hang
# (the import() would resolve to HTML and die at parse time instead of 404ing).
OLD = """class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as ex:
            if ex.status_code == 404:
                if path.endswith('.js'):
                    # Return 404 for javascript files
                    raise ex
                else:
                    return await super().get_response('index.html', scope)
            else:
                raise ex"""

NEW = """class SPAStaticFiles(StaticFiles):
    # ai-stack: the one response whose staleness BRICKS the app.
    #
    # The shell boots by importing /_app/immutable/entry/{start,app}.<hash>.js and calling
    # kit.start(), which is what removes #splash-screen. A rebuild changes those filenames, so a
    # client reusing a stored shell requests chunks that no longer exist, the dynamic imports
    # reject, kit.start() never runs, and the splash screen stays up forever. The only way out is
    # clearing site data, which drops the auth token with it.
    #
    # Starlette sends no cache-control here, so browsers fall back to HEURISTIC freshness (~10% of
    # the document's age, RFC 9111 4.2.2) and can reuse a stored shell for days without asking.
    # `no-cache` — not `no-store` — is the fix: it forbids reuse without REVALIDATION while still
    # allowing the response to be STORED, so the etag/last-modified comparison Starlette already
    # implements answers with a 304 instead of resending the shell on every load.
    #
    # KEYED ON THE REQUEST, not on the response, and that is deliberate. Starlette's
    # NotModifiedResponse whitelists only cache-control, content-location, date, etag, expires and
    # vary, so a 304 carries NO content-type — a content-type guard would fire on the 200 and
    # silently skip the 304. Because RFC 9111 4.3.4 keeps the stored headers a 304 does not
    # mention, a client holding a pre-fix shell would then keep revalidating under its OLD
    # heuristic policy and never converge, while an unconditional GET showed the header and looked
    # correct.
    #
    # The path is relative to this mount and normalised, so a request for `/` arrives as '.' —
    # NOT '' and NOT '/'. index.html is listed too because it is directly reachable.
    SHELL_PATHS = frozenset({'.', 'index.html'})

    async def get_response(self, path: str, scope):
        shell = path in self.SHELL_PATHS
        try:
            response = await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as ex:
            if ex.status_code == 404:
                if path.endswith('.js'):
                    # Return 404 for javascript files
                    raise ex
                else:
                    response = await super().get_response('index.html', scope)
                    # The fallback IS the shell: /auth, /c/<id> and every other non-.js 404 answer
                    # with index.html, and each is a URL a browser may hold without ever having
                    # asked for /. Flag it here rather than inferring it from the response.
                    shell = True
            else:
                raise ex

        # `shell` covers the 200s and the 304s; the content-type test is a net under any other
        # text/html entry point a future build may grow. Hashed chunks are text/javascript and are
        # deliberately NOT touched: their filenames change on every build, which is exactly what
        # makes them safe to cache hard, and Cloudflare already gives them max-age=14400.
        if shell or response.headers.get('content-type', '').startswith('text/html'):
            response.headers['Cache-Control'] = 'no-cache'
        return response"""

sub(OLD, NEW, "SPA shell cache header")

out = os.path.join(SC, "main.py.new")
open(out, "w", encoding="utf-8").write(s)

rel = "backend/open_webui/main.py"
d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", SRC, out],
                   capture_output=True, text=True)
dest = sys.argv[1]
with open(dest, "w", encoding="utf-8") as f:
    f.write(d.stdout)
print(f"wrote {dest}: "
      f"{sum(1 for l in d.stdout.splitlines() if l.startswith('+') and not l.startswith('+++'))} added")

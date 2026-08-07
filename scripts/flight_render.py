#!/usr/bin/env python3
"""Render one URL in headless Chromium and print its HTML as one JSON line. Nothing else.

WHY THIS IS A SEPARATE FILE AND NOT A FUNCTION.

Playwright exists on this host under /usr/bin/python3 (3.12) and NOT in the hermes venv (3.11):

    /usr/bin/python3                                   -> playwright 1.49.1  OK
    ~/.hermes/hermes-agent/venv/bin/python             -> ModuleNotFoundError

A cron job's bare `python3` is the venv, so a fare watch running under cron cannot import
playwright no matter how it is written. The split therefore has to be a FILE boundary crossed by
subprocess with the interpreter spelled out, not an `if` inside one module — an `if` would still be
evaluated by the interpreter that cannot satisfy it. Measured; see docs/HERMES_AGENT.md:280-283 and
docs/TRACKING_ENHANCEMENT.md:88-90.

WHY A REAL BROWSER USER-AGENT HERE, WHEN price_watch.py FORBIDS ONE.

price_watch.py:46-54 carries a law: no spoofed User-Agent, because amazon.ca serves the full page
to plain urllib and a 3.8 KB robot wall to a fake Chrome UA. That law is SCOPED TO THE URLLIB PATH
and does not transfer here, for a reason worth stating so nobody "fixes" one by breaking the other:
there, the UA was a LIE — a Python client claiming to be Chrome. Here the client IS Chrome, and the
UA Playwright sends is the true one for the Chromium build actually doing the rendering. Sending a
truthful UA is not the thing that law prohibits. Do not add a hand-written UA string on top; that
would put the lie back.

FAILURE IS LOUD AND TYPED. Exit codes are distinct because the caller words a different LOG line for
each, and "could not read the fare" and "the browser is not installed" are different problems with
different fixes:

    0  ok, JSON on stdout
    3  playwright not importable by THIS interpreter (prints which interpreter)
    4  chromium present but would not launch
    5  the page did not finish inside the deadline
    6  bad usage

There is deliberately NO fallback to a plain fetch on failure. A caller that quietly fell back would
report a "from $199" teaser from static HTML as a fare — the exact 2026-08-07 bug this whole
subsystem exists to avoid.
"""
import argparse
import json
import os
import signal
import sys
import time

# Self-destruct margin: the parent kills the process group at --timeout, so the child aims to be
# dead a little earlier and print a typed error rather than be killed mid-write.
ALARM_MARGIN_S = 5


def _die(code, msg, **extra):
    """One JSON line on stdout, one human line on stderr, then exit with a typed code."""
    print(json.dumps({"ok": False, "error": msg, "code": code, **extra}))
    print(f"flight_render: {msg}", file=sys.stderr)
    sys.exit(code)


def _on_alarm(_sig, _frm):
    _die(5, "the page did not finish rendering before the deadline")


def main():
    ap = argparse.ArgumentParser(description="Render one URL in headless Chromium.")
    ap.add_argument("--url", required=True)
    # The signal that the fares have actually arrived. Fare pages stream results in by XHR after a
    # polling handshake, so "the document loaded" means nothing: a fare read at load time is a
    # partial-result fare. Per-site, from the registry.
    ap.add_argument("--wait-selector", default=None,
                    help="CSS selector to wait for before reading the DOM")
    ap.add_argument("--wait-text", default=None,
                    help="substring to wait for in the page body (alternative to --wait-selector)")
    ap.add_argument("--timeout", type=int, default=45, help="whole-render budget in seconds")
    # Settling time AFTER the selector appears. The first fare to render is frequently not the
    # cheapest — these pages re-sort as more carriers answer.
    ap.add_argument("--settle-ms", type=int, default=2500)
    ap.add_argument("--profile", default=os.path.expanduser("~/.hermes/flight-browser-profile"),
                    help="persistent profile dir; consent cookies survive between runs here")
    ap.add_argument("--locale", default="en-CA")
    ap.add_argument("--timezone", default="America/Toronto")
    ap.add_argument("--max-bytes", type=int, default=2_000_000,
                    help="truncate returned HTML; a caller only ever regex-scans it")
    ap.add_argument("--screenshot", default=None,
                    help="optional PNG path — recon only, never a monitor")
    a = ap.parse_args()

    if a.timeout < 10:
        _die(6, "--timeout below 10s cannot render a fare page")

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(max(1, a.timeout - ALARM_MARGIN_S))

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ModuleNotFoundError:
        _die(3, f"playwright is not importable by {sys.executable} — a cron job's bare python3 is "
                f"the hermes venv, where it is absent. Invoke this file as /usr/bin/python3.",
             interpreter=sys.executable)
        return

    t0 = time.monotonic()
    started = None
    try:
        with sync_playwright() as p:
            try:
                # A PERSISTENT context, not launch()+new_context(). Consent and geo interstitials
                # are the most common reason a render that worked interactively returns an empty
                # shell under automation, and a cookie jar that survives between runs is the
                # cheapest possible defence against them.
                started = p.chromium.launch_persistent_context(
                    a.profile,
                    headless=True,
                    locale=a.locale,
                    timezone_id=a.timezone,
                    viewport={"width": 1440, "height": 900},
                    args=["--disable-dev-shm-usage",   # /dev/shm is small in containers and a
                                                       # crashed renderer looks like a blocked site
                          "--no-sandbox",
                          "--disable-gpu",
                          "--disable-extensions",
                          # Images, fonts and media are pure cost here: nothing this caller does
                          # reads a pixel, and a fare page ships megabytes of carrier logos.
                          "--blink-settings=imagesEnabled=false"],
                )
            except Exception as e:
                _die(4, f"chromium would not launch: {type(e).__name__}: {str(e)[:200]}")
                return

            page = started.new_page()
            # Block by resource type as well as by the launch flag: the flag stops decoding, this
            # stops the request. Measured elsewhere in this repo as the difference between a 400 KB
            # and a 4 MB transfer.
            page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in
                ("image", "media", "font") else route.continue_()))

            deadline_ms = max(5000, (a.timeout - ALARM_MARGIN_S) * 1000)
            waited = "load"
            try:
                page.goto(a.url, wait_until="domcontentloaded", timeout=deadline_ms)
                if a.wait_selector:
                    page.wait_for_selector(a.wait_selector, timeout=deadline_ms)
                    waited = f"selector:{a.wait_selector}"
                elif a.wait_text:
                    page.wait_for_function(
                        "t => document.body && document.body.innerText.includes(t)",
                        arg=a.wait_text, timeout=deadline_ms)
                    waited = f"text:{a.wait_text}"
                if a.settle_ms:
                    page.wait_for_timeout(a.settle_ms)
            except PWTimeout:
                # NOT fatal, and this is a deliberate judgement. The wait target is a per-site guess
                # from recon; the DOM we already have may well carry the fare, and reporting "no
                # fare found in this HTML" is a truthful answer the caller can act on. What must
                # never happen is claiming the wait SUCCEEDED — so the caller is told.
                waited = "TIMEOUT:" + waited

            html = page.content()
            final_url = page.url
            if a.screenshot:
                try:
                    page.screenshot(path=a.screenshot, full_page=False)
                except Exception:
                    pass
            truncated = len(html) > a.max_bytes
            signal.alarm(0)
            print(json.dumps({
                "ok": True,
                "html": html[:a.max_bytes],
                "bytes": len(html),
                "truncated": truncated,
                "final_url": final_url,
                "waited": waited,
                "ms": int((time.monotonic() - t0) * 1000),
                "interpreter": sys.executable,
            }))
    except SystemExit:
        raise
    except Exception as e:
        _die(4, f"render failed: {type(e).__name__}: {str(e)[:200]}")
    finally:
        # Belt and braces. The context manager closes the driver, but an exception between launch
        # and the `with` unwinding has been observed to leave a browser process behind, and a leaked
        # Chromium on a box that also renders video is not a cosmetic problem.
        signal.alarm(0)
        try:
            if started is not None:
                started.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Measure what each flight site actually gives an automated client. A measuring instrument, not a monitor.

RUN THIS UNDER /usr/bin/python3, MANUALLY, AND NEVER FROM CRON. It fetches 19 hostile commercial
hosts; it is meant to be read by a human who then decides what ships.

WHY IT EXISTS. scripts/price_search.py:354 refuses every fare watch, for a measured reason: on
2026-08-07 job 52f821a8d3a2 reported "$358.72, under your $1,000.00 target" at high confidence,
read out of a JSON-LD array of 97 unrelated itineraries on a cheapflights.ca page whose own title
said "C$ 146+". docs/TRACKING_ENHANCEMENT.md settled where the failure was: `{"rows": 10, ...,
"reason": "no_candidate"}` — discovery worked, EXTRACTION was the gap, and "more engines was never
the fix for fares."

So this file answers, per site, with evidence rather than opinion:
  - does a URL carrying the itinerary reach a results page at all, or a robot wall?
  - does the page ECHO the itinerary it was asked for? (a page that does not is about nothing)
  - are the fares on it bound to their own dates, or is it a bare list of numbers?
  - is the number a real fare or a "from $199" teaser?
  - does any of that survive without JavaScript, or is a browser mandatory?
  - and for a month-shaped ask: does the site's own whole-month search work, and do its results
    still carry the dates they belong to?

THE CONTROL FETCH IS THE STRONGEST THING HERE. Every site is fetched twice, for two unrelated date
pairs. A number that is IDENTICAL across both is not a fare — it is a teaser, a cached page, or a
list element that happens to sort first. That turns "is this a teaser?" from a regex opinion into a
measurement, and it is the single check the old search-result path could never run.

WHAT IT DOES NOT DO. It does not write scripts/flight_sites.json (except under --write-registry,
which refuses to downgrade a shipping site without --force). Production policy is not rewritten
unattended from a hostile network; this repo's habit is to verify a roster change against the live
thing and have a human land it. It also never retries: retrying a bot wall is how a 403 becomes a
durable ban, and the job here is to measure, not to succeed.

Usage:
  /usr/bin/python3 scripts/flight_probe.py --stack
  /usr/bin/python3 scripts/flight_probe.py --origin YYZ --dest YVR \
      --depart 2026-09-15 --return 2026-09-22 [--tier plain|browser|both] [--month 2027-03]
  /usr/bin/python3 scripts/flight_probe.py --verdicts-only <probe.json>
  python3 scripts/flight_probe.py --selftest          # offline, zero traffic
"""
import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(modname):
    """The price_search.py:59 idiom: sibling scripts are loaded by path, not installed."""
    path = os.path.join(HERE, modname + ".py")
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pw = _load("price_watch")

REGISTRY = os.path.join(HERE, "flight_sites.json")
RENDERER = os.path.join(HERE, "flight_render.py")
# Spelled out, never "python3". A cron tick's bare python3 is the hermes venv (3.11) where
# playwright is absent; /usr/bin/python3 (3.12) is where it lives. See flight_render.py's docstring.
BROWSER_PY = os.environ.get("FLIGHT_BROWSER_PYTHON", "/usr/bin/python3")

DELAY_S = 20            # between hosts on the plain tier
BROWSER_DELAY_S = 45    # between hosts on the browser tier
JITTER_S = 5
TIMEOUT_S = 30
BROWSER_TIMEOUT_S = 45
ABORT_AFTER_BLOCKED = 4  # consecutive blocks means the IP is being fingerprinted, not that the
                         # next site differs. Stop and say so rather than earning a longer ban.
CONTROL_OFFSET_D = 14    # the control itinerary is this far from the primary one
MAX_SAVE_BYTES = 512_000

# A fare-shaped amount. NOT pw.MONEY, which requires cents ("([0-9][0-9,]*\.[0-9]{2})") — fare
# pages routinely render "$289" with none, and requiring cents would score a working site as empty.
# A currency mark IS required, for the reason web_search.py:91 gives about MONEY_IN_TEXT: without
# one, "rated 4.99 out of 5" and "1,250 reviews" are money.
FARE_MONEY = re.compile(r"(?:C\$|CA\$|US\$|A\$|\$|CAD\s|USD\s|€|£)\s?([0-9][0-9,]{0,6}(?:\.[0-9]{2})?)")
FARE_MIN, FARE_MAX = 30.0, 25_000.0

# Dates as fare pages render them, for the "is this number bound to an itinerary?" measurement.
DATE_NEAR = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*\.?,?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\b",
    re.I)
# How far from a number a date may sit and still plausibly belong to the same itinerary card.
BIND_WINDOW = 400

# Teaser cues. "from $199" is an advertisement, not a bookable fare for the dates asked for.
TEASER_NEAR = re.compile(
    r"\b(?:from|starting(?:\s+at|\s+from)?|as\s+low\s+as|prices?\s+from|fares?\s+from"
    r"|one[-\s]way\s+from|round[-\s]trip\s+from|deals?\s+from|under)\s*$", re.I)
TEASER_BACK = 40   # characters before the number to inspect for a cue

# The robot-wall vocabulary, DELIBERATELY BROADER THAN pw.fetch's. price_watch.py:102 only sniffs
# under 20 000 bytes, which is right for a retail page but wrong here: an Akamai or Incapsula
# interstitial is often several hundred KB and would sail through as a "successful" fetch with no
# fares on it. Both verdicts are recorded separately, because the divergence tells you whether
# pw.fetch itself needs widening.
WALL_SIGNALS = (
    "access to this page has been denied", "pardon our interruption",
    "request unsuccessful. incapsula", "incident id", "cf-challenge",
    "attention required", "checking your browser", "enable javascript and cookies",
    "/sorry/index", "unusual traffic from your computer", "are you a robot",
    "captcha", "not a robot", "automated access", "bot detection", "px-captcha",
    "verify you are a human", "ddos protection by",
    # Measured 2026-08-07. kayak.com, momondo.ca and cheapflights.ca each served a 250-300 KB page
    # titled "What is a bot?" to plain urllib — one wall, three Booking Holdings hosts, which is the
    # owner field earning its place. trip.com answered HTTP 432 with the 17-byte body
    # "whaleguard block". None of these matched the vocabulary above, so all four were scored as
    # ordinary pages that merely had no fare on them.
    "what is a bot", "whaleguard", "press & hold", "press and hold",
    "prove you are human", "human verification", "unusual activity",
)
# A title is a strong wall signal on its own and survives into the saved measurement, which means a
# past run can be re-judged from probe.json without refetching anything.
WALL_TITLES = ("what is a bot", "access denied", "attention required", "just a moment",
               "are you a robot", "captcha", "blocked", "security check", "verify")
# A 2xx response this small is not a page. It is a redirect stub, a WAF body, or a bare SPA shell.
TINY_PAGE_BYTES = 2500
CONSENT_SIGNALS = ("cookie", "consent", "gdpr", "privacy preference", "accept all")

VERDICTS = ("untested", "usable", "usable_with_browser", "blocked", "no_deeplink",
            "unusable_role", "no_fare", "listing", "teaser", "js_only")
SHIPPABLE = ("usable", "usable_with_browser")


# ---------------------------------------------------------------- measurement helpers

def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def fare_candidates(html):
    """[(value, offset)] for every currency-marked, fare-banded number in the page.

    Bounded, not clever. The point is to COUNT and to locate, so the caller can ask whether the
    numbers are bound to dates — not to pick a winner. Picking is flight_watch.py's job, and only
    with a per-site selector a human landed.
    """
    out = []
    for m in FARE_MONEY.finditer(html or ""):
        v = _f(m.group(1))
        if v is not None and FARE_MIN <= v <= FARE_MAX:
            out.append((v, m.start()))
    return out


def teaser_flagged(html, cands):
    """How many candidates sit right after a 'from'/'as low as' cue."""
    n = 0
    for _v, off in cands:
        back = (html or "")[max(0, off - TEASER_BACK):off]
        if TEASER_NEAR.search(back):
            n += 1
    return n


def fares_bound_to_dates(html, cands):
    """([(value, [dates...])], bound_count) — is each number co-located with a date?

    THIS IS THE MEASUREMENT THAT DECIDES WHETHER MONTH MODE CAN WORK AT ALL. In exact-date mode a
    number is bound to the itinerary by the URL. On a whole-month page it is not: the page shows
    dozens of itineraries, so a fare is only usable if the page states which dates it belongs to,
    near enough to be read out together. If nothing here is bound, rungs 1 and 3 of the ladder are
    impossible on this site no matter how well it renders.

    Heuristic and labelled as such: it answers "is the information co-located?", never "here is the
    fare for Mar 8".
    """
    html = html or ""
    pairs, bound = [], 0
    for v, off in cands:
        window = html[max(0, off - BIND_WINDOW):off + BIND_WINDOW]
        dates = list(dict.fromkeys(DATE_NEAR.findall(window)))[:4]
        if dates:
            bound += 1
        pairs.append((v, dates))
    return pairs, bound


def wall_check(html):
    """(is_wall, [signals], pw_fetch_would_see_it)."""
    low = (html or "").lower()
    hits = [s for s in WALL_SIGNALS if s in low]
    narrow = bool(len(html or "") < 20000
                  and re.search(r"captcha|not a robot|automated access", html or "", re.I))
    return bool(hits), hits, narrow


def itinerary_echo(html, origin, dest, depart, ret, date_formats=()):
    """Which components of the requested itinerary the page actually mentions.

    A page that names neither of your dates is not answering your question — cheapflights.ca echoed
    neither, which is exactly why its 97 prices meant nothing.
    """
    h = html or ""
    low = h.lower()
    got = {"origin": origin.lower() in low, "dest": dest.lower() in low}
    for label, d in (("depart", depart), ("ret", ret)):
        if not d:
            got[label] = None
            continue
        forms = {d, d.replace("-", ""), d.replace("-", "/")}
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            forms |= {dt.strftime("%d/%m/%Y"), dt.strftime("%m/%d/%Y"),
                      dt.strftime("%d %b %Y"), dt.strftime("%b %d"), dt.strftime("%-d %b")
                      if os.name != "nt" else dt.strftime("%d %b"),
                      dt.strftime("%y%m%d")}
            for fmt in date_formats:
                forms.add(dt.strftime(fmt))
        except Exception:
            pass
        got[label] = any(f and f.lower() in low for f in forms)
    return got


def month_echo(html, ym):
    """Is the requested month named, in any of the forms a page might use? (month mode)"""
    if not ym:
        return None
    y, m = ym.split("-")
    dt = date(int(y), int(m), 1)
    forms = {ym, ym.replace("-", ""), dt.strftime("%B %Y"), dt.strftime("%b %Y"),
             dt.strftime("%B"), dt.strftime("%b")}
    low = (html or "").lower()
    return any(f.lower() in low for f in forms)


# ---------------------------------------------------------------- the verdict, as a pure function

def verdict(rec, role="itinerary_search", list_is_itinerary=False):
    """(verdict, reason) from a recorded measurement ONLY. No I/O, no clock, no network.

    Pure on purpose: every verdict in a saved probe.json can be recomputed for free with
    --verdicts-only when a rule changes, and the whole decision table is unit-testable offline
    against fixtures. Order is load-bearing and each rung is earned by a real failure mode.
    """
    if role in ("deal_feed", "redirect_only"):
        return "unusable_role", f"role is {role}: it has no per-itinerary fare to read"
    if rec.get("error") and not rec.get("http_status"):
        return "blocked", f"could not be fetched: {rec['error']}"
    if rec.get("robot_wall"):
        sig = ", ".join(rec.get("wall_signals", [])[:3]) or "robot wall"
        return "blocked", f"served an interstitial instead of the page ({sig})"
    title = (rec.get("title") or "").lower()
    if title and any(t in title for t in WALL_TITLES):
        return "blocked", f"the page's own title is a challenge: {rec['title']!r}"
    # ANY 4xx/5xx, not a hand-listed few. trip.com answered 432 — a non-standard code — and fell
    # straight through a `status in (401, 403, 429, 503)` check to be judged as a page.
    if isinstance(rec.get("http_status"), int) and rec["http_status"] >= 400:
        return "blocked", f"HTTP {rec['http_status']}"
    if not rec.get("url"):
        return "no_deeplink", "no URL template carries the itinerary; the fare is behind a form"

    cands0 = rec.get("prices", {})
    # A tiny 2xx body, and a served-shell-with-no-fares, are both "we have not seen the page yet" —
    # NOT "this site has no fare". Ordering matters here and got it wrong: the date-echo gate below
    # ran first and returned no_fare for skyscanner.ca, whose 708-byte response is a bare React
    # shell ('<div id="root">' plus 'You need to enable JavaScript to run this app'). A shell
    # legitimately echoes nothing, so judging it on echo is judging it on the wrong evidence, and it
    # turned "needs the browser tier" into a verdict that reads like a dead end.
    if rec.get("bytes", 0) and rec["bytes"] < TINY_PAGE_BYTES and not cands0.get("distinct"):
        return (("js_only", f"{rec['bytes']} bytes — an app shell, not a rendered page")
                if rec.get("tier") == "plain"
                else ("no_fare", f"only {rec['bytes']} bytes even after rendering"))
    if rec.get("tier") == "plain" and not cands0.get("distinct"):
        return "js_only", "no fare-shaped number in the served HTML; needs a browser to confirm"

    echo = rec.get("itinerary_echoed") or {}
    cands = rec.get("prices", {})
    distinct = cands.get("distinct", 0)

    # A page that names neither date is not about the itinerary asked for, however much money it
    # carries. This is the gate the search-result path structurally could not run.
    dated = [k for k in ("depart", "ret") if echo.get(k)]
    if rec.get("mode") == "month":
        if not rec.get("month_echoed"):
            return "no_fare", "the requested month is not named anywhere on the page"
    elif echo.get("depart") is not None and not dated:
        return "no_fare", "neither requested date appears on the page — it is about no itinerary"

    if distinct == 0:
        if rec.get("tier") == "plain":
            return "js_only", "no fare-shaped number in the served HTML; needs a browser to confirm"
        return "no_fare", "rendered, but no fare-shaped number was present"

    # Teaser tests. The control comparison is the strong one and it is a measurement.
    if rec.get("control", {}).get("identical_to_primary") and distinct <= 3:
        return "teaser", ("the same number is returned for an unrelated date pair, so it is not a "
                          "fare for these dates")
    if cands.get("teaser_flagged", 0) >= max(1, distinct):
        return "teaser", "every number on the page follows a 'from'/'as low as' cue"

    bound = cands.get("bound_to_dates", 0)
    if rec.get("mode") == "month":
        # In month mode there is no URL binding to fall back on: unless the fares carry their own
        # dates, a minimum taken from this page is the 97-price array again.
        if bound == 0:
            return "listing", (f"{distinct} fares and none carries its own dates — a minimum here "
                               f"would be an arbitrary element of a list")
        return ("usable_with_browser" if rec.get("tier") == "browser" else "usable",
                f"{bound} of {distinct} fares carry their own dates inside the requested month")

    if distinct >= pw.LISTING_MIN_PRICES and not list_is_itinerary:
        return "listing", (f"the page offers {distinct} different fares, so none of them is the "
                           f"fare for one itinerary")
    return ("usable_with_browser" if rec.get("tier") == "browser" else "usable",
            f"{distinct} fare-shaped value(s), itinerary echoed ({'+'.join(dated) or 'month'})")


# ---------------------------------------------------------------- URL building

def _slots(origin, dest, depart, ret, adults=1):
    def ymd(d):
        return d.replace("-", "")[2:] if d else ""

    def ym(d):
        return d.replace("-", "")[:6] if d else ""
    return {
        "origin": origin, "dest": dest,
        "origin_lc": origin.lower(), "dest_lc": dest.lower(),
        "depart": depart or "", "ret": ret or "",
        "depart_ymd": ymd(depart), "ret_ymd": ymd(ret),
        "depart_ym": ym(depart), "ret_ym": ym(ret),
        "depart_ym_dash": (depart or "")[:7], "ret_ym_dash": (ret or "")[:7],
        "adults": adults,
    }


def build_url(site, origin, dest, depart, ret, adults=1, mode="exact"):
    """The URL for this site and itinerary, or None when the site cannot express it."""
    key = {"exact": "one_way_template" if not ret else "url_template",
           "month": "month_url_template",
           "range": "range_url_template"}[mode]
    tpl = site.get(key)
    if not tpl:
        return None
    try:
        return tpl.format(**_slots(origin, dest, depart, ret, adults))
    except (KeyError, IndexError) as e:
        return f"__BAD_TEMPLATE__:{e}"


# ---------------------------------------------------------------- fetching

def fetch_plain(url):
    """(html, status, error). pw.HEADERS verbatim — no spoofed UA on the urllib path, ever."""
    req = urllib.request.Request(url, headers=pw.HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read().decode("utf-8", "replace"), r.status, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return body, e.code, f"HTTP {e.code}"
    except Exception as e:
        return "", None, f"{type(e).__name__}: {str(e)[:120]}"


def fetch_browser(url, wait_selector=None, timeout=BROWSER_TIMEOUT_S):
    """(html, final_url, error) via flight_render.py under an explicit interpreter.

    start_new_session + killpg, not p.kill(): kill() reaps the wrapper and ORPHANS Chromium, and a
    leaked browser on a box that also renders video is not cosmetic. The child additionally arms its
    own SIGALRM, so there are two independent stops.
    """
    if not os.path.exists(RENDERER):
        return "", None, f"renderer missing at {RENDERER}"
    cmd = [BROWSER_PY, RENDERER, "--url", url, "--timeout", str(timeout)]
    if wait_selector:
        cmd += ["--wait-selector", wait_selector]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    except FileNotFoundError:
        return "", None, f"interpreter not found: {BROWSER_PY}"
    try:
        out, err = p.communicate(timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            p.kill()
        p.communicate()
        return "", None, "renderer exceeded its deadline and was killed"
    if p.returncode != 0:
        try:
            j = json.loads((out or "").strip().splitlines()[-1])
            return "", None, f"exit {p.returncode}: {j.get('error')}"
        except Exception:
            return "", None, f"exit {p.returncode}: {(err or out or '').strip()[:160]}"
    try:
        j = json.loads(out.strip().splitlines()[-1])
    except Exception:
        return "", None, "renderer produced no JSON"
    return j.get("html", ""), j.get("final_url"), None


def robots_check(host, path):
    """(fetched, status, disallows, rule, crawl_delay).

    Both politeness and a hard ship gate: this repo would not ship a monitor hammering a path the
    site's own robots.txt forbids. A tiny parser rather than urllib.robotparser because we want the
    RULE TEXT recorded, not just a boolean.
    """
    url = f"https://{host}/robots.txt"
    txt, status, err = fetch_plain(url)
    if err and not txt:
        return False, status, None, None, None
    applies, delay, rules = False, None, []
    for line in txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        kl = k.lower()
        if kl == "user-agent":
            applies = v in ("*",)
        elif applies and kl == "disallow" and v:
            rules.append(v)
        elif applies and kl == "crawl-delay":
            delay = _f(v)
    for r in rules:
        if path.startswith(r):
            return True, status, True, f"Disallow: {r}", delay
    return True, status, False, None, delay


def parse_keep_only(text):
    """The engine names under use_default_settings.engines.keep_only, or None.

    A hand parser rather than a regex or pyyaml, for two reasons. The entries carry trailing inline
    comments AND multi-line comment continuations indented past the list item --

        keep_only:
          - google        # best long-tail recall for product and fare pages; also the highest
                          # CAPTCHA risk here, and safe only because...
          - brave         # genuinely independent crawl...

    -- so a naive `(?:\\s*-\\s*\\S+\\s*\\n)+` matches nothing, which is how this silently returned
    None on the first attempt. And pyyaml is not importable under both interpreters this repo has to
    work with, while this file must load identically under /usr/bin/python3 and the hermes venv.
    """
    lines = (text or "").splitlines()
    start = indent = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)keep_only:\s*(?:#.*)?$", ln)
        if m:
            start, indent = i + 1, len(m.group(1))
            break
    if start is None:
        return None
    names = []
    for ln in lines[start:]:
        if not ln.strip() or re.match(r"^\s*#", ln):
            continue                                    # blank, or a comment continuation
        m = re.match(r"^(\s*)-\s*([^\s#]+)", ln)
        if m and len(m.group(1)) > indent:
            names.append(m.group(2))
            continue
        if len(ln) - len(ln.lstrip()) <= indent:
            break                                       # dedented to a sibling key: block over
    return names or None


# ---------------------------------------------------------------- Phase 0: the stack

def probe_stack():
    """Every live assertion about the running system, with its expected value.

    Each of these has cost time in this repo's history by being assumed. A mismatch prints as a
    MISMATCH line and is recorded; nothing here is swallowed.
    """
    out = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # 1. Is google actually on the monitors-only roster? docs/TRACKING_ENHANCEMENT.md:55 records
    #    that it went missing with nothing in the logs saying so, and that the fix is UNVERIFIED.
    # Three-way drift, not just "is google there". The repo has TWO declarations of this roster --
    # web_search.ENGINE_ORDER and settings.yml's keep_only -- and a test pins them to each other.
    # That test passes while BOTH disagree with the running container, which is precisely the hole
    # TRACKING_ENHANCEMENT.md:59 names: "Nothing in that file fails loudly. Verify every roster
    # change against /config." A missing engine and an UNEXPECTED one are different bugs: missing
    # means a fix did not land, unexpected means a removal did not land -- and startpage/qwant were
    # removed for CAPTCHA-ing on sight, so their presence is spending an engine budget on nothing.
    declared_code = declared_yaml = None
    try:
        declared_code = list(_load("web_search").ENGINE_ORDER)
    except Exception:
        pass
    try:
        y = open(os.path.join(HERE, "..", "compose", "searxng-hermes", "settings.yml")).read()
        declared_yaml = parse_keep_only(y)
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:8889/config", timeout=8) as r:
            cfg = json.loads(r.read())
        live = sorted({e.get("name") for e in cfg.get("engines", []) if e.get("name")})
        want = set(declared_code or declared_yaml or [])
        out["searxng_8889"] = {
            "reachable": True,
            "live": live,
            "declared_in_web_search_py": declared_code,
            "declared_in_settings_yml": declared_yaml,
            "missing_from_live": sorted(want - set(live)),
            "unexpected_in_live": sorted(set(live) - want),
            "in_sync": bool(want) and want == set(live),
        }
    except Exception as e:
        out["searxng_8889"] = {"reachable": False, "error": f"{type(e).__name__}: {str(e)[:100]}",
                               "declared_in_web_search_py": declared_code,
                               "declared_in_settings_yml": declared_yaml}

    # 2. What hermes will grant a job. api_server must NOT have terminal or browser.
    key = None
    try:
        for line in open(os.path.expanduser("~/.hermes/.env")):
            if line.startswith("API_SERVER_KEY="):
                key = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass
    if key:
        try:
            req = urllib.request.Request("http://127.0.0.1:8642/v1/toolsets",
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                out["gateway_8642"] = {"reachable": True, "toolsets": json.loads(r.read())}
        except Exception as e:
            out["gateway_8642"] = {"reachable": False,
                                   "error": f"{type(e).__name__}: {str(e)[:100]}"}
    else:
        out["gateway_8642"] = {"reachable": False, "error": "no API_SERVER_KEY readable"}

    # 3/4/5. THE critical one. The asymmetry is the finding: asserting only that /usr/bin/python3
    #        works would let a future venv install make the constraint invisible.
    pwc = {}
    for label, interp in (("usr_bin_python3", BROWSER_PY),
                          ("venv_python",
                           os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python"))):
        try:
            r = subprocess.run([interp, "-c",
                                "import playwright,sys;print(playwright.__version__ if hasattr("
                                "playwright,'__version__') else 'ok');print(sys.version.split()[0])"],
                               capture_output=True, text=True, timeout=30)
            lines = (r.stdout or "").strip().splitlines()
            pwc[label] = {"interpreter": interp, "import_ok": r.returncode == 0,
                          "detail": lines, "stderr": (r.stderr or "").strip()[-160:]}
        except Exception as e:
            pwc[label] = {"interpreter": interp, "import_ok": False, "error": str(e)[:120]}
    # 6. And prove the explicit path defeats a cron job's PATH, which is the whole mitigation.
    try:
        env = dict(os.environ)
        env["PATH"] = os.path.expanduser("~/.hermes/hermes-agent/venv/bin") + ":" + env.get("PATH", "")
        r = subprocess.run([BROWSER_PY, "-c", "import playwright;print('PW OK')"],
                           capture_output=True, text=True, timeout=30, env=env)
        pwc["explicit_path_beats_venv_PATH"] = (r.returncode == 0 and "PW OK" in (r.stdout or ""))
    except Exception as e:
        pwc["explicit_path_beats_venv_PATH"] = f"error: {str(e)[:100]}"
    pwc["browsers"] = sorted(os.listdir(os.path.expanduser("~/.cache/ms-playwright"))) \
        if os.path.isdir(os.path.expanduser("~/.cache/ms-playwright")) else []
    out["playwright"] = pwc

    # 7. Fare jobs currently scheduled, and whether they can be migrated or only deleted.
    jobs = []
    try:
        raw = json.load(open(os.path.expanduser("~/.hermes/cron/jobs.json")))
        items = raw.get("jobs", raw) if isinstance(raw, dict) else raw
        items = list(items.values()) if isinstance(items, dict) else items
        for j in items:
            prompt = j.get("prompt") or ""
            if "price_search" not in prompt and "flight" not in (j.get("name") or "").lower():
                continue
            has_itin = bool(re.search(r"--origin\b", prompt) and re.search(r"--depart\b", prompt))
            jobs.append({
                "id": j.get("id"), "name": j.get("name"),
                "schedule": (j.get("schedule") or {}).get("display"),
                "enabled": j.get("enabled"), "state": j.get("state"),
                "repeat": j.get("repeat"), "last_status": j.get("last_status"),
                "alert_to": (re.search(r"--alert-to\s+(\S+)", prompt) or [None, None])[1],
                "_prompt": prompt,
                "has_itinerary": has_itin,
                "recommendation": "migrate" if has_itin else "delete",
                "why": ("carries origin/dest/dates, so it can be rewritten as a flight_watch job"
                        if has_itin else
                        "no --origin/--dest/--depart to carry over: there is no itinerary to "
                        "migrate, so recreate it from the flight form instead"),
            })
    except Exception as e:
        jobs = [{"error": str(e)[:120]}]
    out["fare_jobs"] = jobs

    # 8. Can an alert to each fare job's recipient actually be delivered? A watch that fires and
    #    reaches nobody is the false negative this subsystem keeps being bitten by.
    try:
        at = _load("alert_transports")
        recips = {}
        for j in jobs:
            h = j.get("alert_to")
            if not h:
                continue
            try:
                email, phone = at.resolve(h)[:2]
            except Exception:
                c = (at.load_contacts() or {}).get(h) or {}
                email, phone = c.get("email") or at.owui_email(h), c.get("phone")
            recips[h] = {"email": bool(email), "phone": bool(phone)}
        out["recipients"] = recips
    except Exception as e:
        out["recipients"] = {"error": str(e)[:120]}

    # 9. Orphaned fare state. A retired job leaves its ~/.hermes/monitor-state/ files behind, and
    #    they are not inert: price_search writes `fare_refused: true` so the refusal only speaks
    #    once, and the watch file carries `alerted_price`. A new watch that happens to reuse one of
    #    these --state names inherits a "already told them" flag and a price it never read. Worth
    #    listing before creating anything, because the names are the obvious ones a person picks
    #    twice (this box already has five).
    orphans = []
    sd = os.path.expanduser("~/.hermes/monitor-state")
    try:
        reg_hosts = json.load(open(REGISTRY))["sites"]
    except Exception:
        reg_hosts = []
    live_states = set()
    for j in jobs:
        m = re.search(r"--state\s+'?([^'\s]+)", (j.get("_prompt") or ""))
        if m:
            live_states.add(m.group(1))
    try:
        for fn in sorted(os.listdir(sd)):
            if not fn.endswith(".json") or ".tmp" in fn:
                continue
            base = fn[:-5]
            if base.split(".")[0] in live_states:
                continue
            try:
                d = json.load(open(os.path.join(sd, fn)))
            except Exception:
                continue
            # Classify by CONTENT, not by name. Matching state-file names with a regex caught
            # 'tipping-the-velvet-price' (a book watch) via [a-z]{3}-[a-z]{3} on "ing-the", which is
            # the kind of heuristic that looks fine until it reports someone's novel as a fare.
            # What actually makes a state file fare-related is what is IN it.
            hay = " ".join(str(d.get(k) or "") for k in ("item", "query", "url", "itin")).lower()
            is_fare = bool(
                d.get("fare_refused") is not None
                or d.get("itin")                      # flight_watch's own itinerary key
                or re.search(r"\bflights?\b|\bfares?\b|airfare|air-?travel", hay)
                or any(h["domain"] in hay for h in reg_hosts)
                # The NAME is a weak but real signal, and content alone missed a live case:
                # 'yxx-yyz-flight.search' holds query "Toronto to Vancouver" with no fare word in
                # it at all. A bounded word match only — the earlier [a-z]{3}-[a-z]{3} arm matched
                # "ing-the" inside 'tipping-the-velvet-price' and reported a novel as a fare.
                or re.search(r"\b(?:fares?|flights?|airfare)\b", base, re.I))
            if not is_fare:
                continue
            orphans.append({
                "state": base,
                "fare_refused": d.get("fare_refused"),
                "alerted_price": d.get("alerted_price"),
                "item": (d.get("item") or "")[:70] or None,
                "url": (d.get("url") or "")[:90] or None,
                "has_itinerary_in_url": bool(re.search(r"\d{4}-\d{2}-\d{2}|\d{6}", d.get("url") or "")),
            })
    except Exception as e:
        orphans = [{"error": str(e)[:120]}]
    out["orphaned_state"] = orphans

    return out


def print_stack(s):
    def mark(ok):
        return "OK  " if ok else "?? "

    print("\n=== Phase 0: the running stack ===")
    sx = s.get("searxng_8889", {})
    if sx.get("reachable"):
        print(f"{mark(sx.get('in_sync'))} searxng :8889 live    = {sx['live']}")
        print(f"     declared (web_search.py) = {sx.get('declared_in_web_search_py')}")
        print(f"     declared (settings.yml)  = {sx.get('declared_in_settings_yml')}")
        miss, extra = sx.get("missing_from_live") or [], sx.get("unexpected_in_live") or []
        if miss:
            print(f"     MISSING from the live container: {miss}")
            print("       -> a roster ADDITION never landed. For google this is the exact failure")
            print("          TRACKING_ENHANCEMENT.md:55 predicted and left unverified.")
        if extra:
            print(f"     UNEXPECTED in the live container: {extra}")
            print("       -> a roster REMOVAL never landed. startpage and qwant were dropped in")
            print("          a1b4558 for CAPTCHA-ing on the very first query from this IP, so every")
            print("          fan-out is still paying for two engines that cannot answer.")
        if miss or extra:
            print("     Both point the same way: the container is running a config the repo no")
            print("     longer contains. a1b4558 also changed the MOUNT (a rw directory -> a ro")
            print("     single file), and a changed mount needs the container RECREATED, not")
            print("     restarted: `docker compose up -d --force-recreate searxng-hermes`.")
            print("     Note this does NOT affect flight fares — flight_watch.py issues no search")
            print("     queries at all, by construction. It affects price_search.py.")
    else:
        print(f"??  searxng :8889 unreachable: {sx.get('error')}")
        print(f"     declared (web_search.py) = {sx.get('declared_in_web_search_py')}")
        print(f"     declared (settings.yml)  = {sx.get('declared_in_settings_yml')}")

    gw = s.get("gateway_8642", {})
    if gw.get("reachable"):
        ts = gw.get("toolsets")
        print(f"OK   hermes :8642 toolsets = {json.dumps(ts)[:200]}")
    else:
        print(f"??  hermes :8642 unreachable: {gw.get('error')}")

    p = s.get("playwright", {})
    a, b = p.get("usr_bin_python3", {}), p.get("venv_python", {})
    print(f"{mark(a.get('import_ok'))} playwright under {a.get('interpreter')}: "
          f"{'importable' if a.get('import_ok') else 'MISSING'} {a.get('detail')}")
    print(f"{mark(not b.get('import_ok'))} playwright under {b.get('interpreter')}: "
          f"{'importable (UNEXPECTED)' if b.get('import_ok') else 'absent, as expected'}")
    print(f"{mark(p.get('explicit_path_beats_venv_PATH') is True)} explicit interpreter defeats a "
          f"venv-first PATH: {p.get('explicit_path_beats_venv_PATH')}")
    print(f"     chromium builds: {p.get('browsers')}")

    # Say the zero out loud. A section that prints nothing is indistinguishable from a section that
    # did not run, and this whole subsystem's recurring failure is silence being read as success.
    fj = s.get("fare_jobs", [])
    if not fj:
        print("OK   fare jobs scheduled: NONE — nothing to migrate or delete")
    for j in fj:
        if j.get("error"):
            print(f"??  fare jobs unreadable: {j['error']}")
            continue
        print(f"     job {j['id']} '{j['name']}' {j['schedule']} enabled={j['enabled']} "
              f"-> {j['recommendation'].upper()}: {j['why']}")

    rec = s.get("recipients") or {}
    if not rec:
        print("     alert recipients: none to check (no fare jobs)")
    for h, r in rec.items():
        if h == "error":
            print(f"??  recipients unresolvable: {r}")
            continue
        print(f"{mark(r['email'] or r['phone'])} alerts to '{h}': "
              f"email={'yes' if r['email'] else 'NO'} phone={'yes' if r['phone'] else 'NO'}")

    orph = s.get("orphaned_state") or []
    if not orph:
        print("OK   orphaned fare state: none")
    else:
        print(f"     orphaned fare state in ~/.hermes/monitor-state ({len(orph)} file(s)) — reusing")
        print("     one of these --state names would inherit its flags:")
        for o in orph:
            if o.get("error"):
                print(f"??     unreadable: {o['error']}")
                continue
            bits = []
            if o.get("fare_refused"):
                bits.append("fare_refused=true (the refusal would stay silent)")
            if o.get("alerted_price") is not None:
                bits.append(f"alerted_price={o['alerted_price']} (it already texted this)")
            if o.get("url") and not o.get("has_itinerary_in_url"):
                bits.append("url carries NO dates")
            print(f"       {o['state']:28s} {'; '.join(bits) or 'no fare flags'}")
            if o.get("item"):
                print(f"         item: {o['item']}")
    print()


# ---------------------------------------------------------------- one site

def probe_site(site, origin, dest, depart, ret, adults, tier, mode, control=None,
               save_dir=None):
    """One site, one tier, one mode. Never retries; records everything it saw."""
    host = site["domain"]
    rec = {"domain": host, "owner": site.get("owner"), "role": site.get("role"),
           "date_flex": site.get("date_flex"), "tier": tier, "mode": mode,
           "requested": {"origin": origin, "dest": dest, "depart": depart, "ret": ret},
           "error": None}

    if site.get("role") in ("deal_feed", "redirect_only"):
        rec["verdict"], rec["reason"] = verdict(rec, role=site["role"])
        return rec

    url = build_url(site, origin, dest, depart, ret, adults, mode=mode)
    if url and url.startswith("__BAD_TEMPLATE__"):
        rec["error"] = f"template placeholder missing: {url.split(':', 1)[1]}"
        rec["verdict"], rec["reason"] = "no_deeplink", rec["error"]
        return rec
    rec["url"] = url
    if not url:
        rec["verdict"], rec["reason"] = verdict(rec, role=site.get("role", "itinerary_search"))
        return rec

    parsed = urllib.parse.urlparse(url)
    fetched, status, disallows, rule, delay = robots_check(parsed.netloc, parsed.path or "/")
    rec["robots"] = {"fetched": fetched, "status": status, "disallows_path": disallows,
                     "rule": rule, "crawl_delay": delay}

    t0 = time.monotonic()
    if tier == "browser":
        extract = site.get("extract") or {}
        html, final_url, err = fetch_browser(url, extract.get("wait_selector"))
        rec["final_url"] = final_url
        status = 200 if html else status
    else:
        html, status, err = fetch_plain(url)
    rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    rec["http_status"] = status
    rec["bytes"] = len(html or "")
    rec["error"] = err

    wall, sigs, narrow = wall_check(html)
    rec["robot_wall"] = wall
    rec["wall_signals"] = sigs
    rec["robot_wall_pw_fetch_would_see"] = narrow
    rec["consent_wall"] = any(s in (html or "").lower() for s in CONSENT_SIGNALS)
    rec["title"] = pw.page_title(html)

    if mode == "month":
        rec["month_echoed"] = month_echo(html, (depart or "")[:7])
    rec["itinerary_echoed"] = itinerary_echo(html, origin, dest, depart, ret)

    cands = fare_candidates(html)
    pairs, bound = fares_bound_to_dates(html, cands)
    vals = [v for v, _ in cands]
    rec["prices"] = {
        "count": len(vals),
        "distinct": len(set(vals)),
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "teaser_flagged": teaser_flagged(html, cands),
        "bound_to_dates": bound,
        "sample_bound": [{"value": v, "dates": d} for v, d in pairs if d][:5],
    }

    if control is not None:
        cvals = [v for v, _ in fare_candidates(control)]
        rec["control"] = {
            "distinct": len(set(cvals)),
            "min": min(cvals) if cvals else None,
            "identical_to_primary": bool(vals and cvals and min(vals) == min(cvals)),
        }

    if save_dir and html:
        os.makedirs(save_dir, exist_ok=True)
        fn = os.path.join(save_dir, f"{host}.{tier}.{mode}.html")
        with open(fn, "w") as f:
            f.write(html[:MAX_SAVE_BYTES])
        rec["saved_html"] = os.path.relpath(fn, os.path.dirname(save_dir.rstrip("/")))

    rec["verdict"], rec["reason"] = verdict(rec, role=site.get("role", "itinerary_search"),
                                           list_is_itinerary=bool(site.get("list_is_itinerary")))
    return rec


# ---------------------------------------------------------------- report

def report_md(run):
    L = ["# Flight site reconnaissance", "",
         f"Probed {run['itinerary']['origin']}→{run['itinerary']['dest']} "
         f"{run['itinerary']['depart']}–{run['itinerary'].get('ret') or 'one-way'} "
         f"({run.get('mode')} mode), {run['started_at']}.", "",
         f"Interpreter: `{run['interpreter']}`. "
         f"{run['summary']['requests_made']} requests, {run['summary']['wall_clock_s']}s.", "",
         "| site | owner | tier | HTTP | bytes | fares | bound | verdict | why |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in run["results"]:
        p = r.get("prices", {})
        L.append(f"| {r['domain']} | {r.get('owner') or ''} | {r.get('tier')} | "
                 f"{r.get('http_status') or '—'} | {r.get('bytes') or 0} | "
                 f"{p.get('distinct') or 0} | {p.get('bound_to_dates') or 0} | "
                 f"**{r['verdict']}** | {r.get('reason', '')} |")
    L += ["", "## By verdict", ""]
    for v, n in sorted(run["summary"]["by_verdict"].items(), key=lambda kv: -kv[1]):
        L.append(f"- **{v}** — {n}")
    ship = run["summary"]["ship_candidates"]
    L += ["", "## Ship candidates", "",
          ("None. The gate says stop: keep the refusal and ship these findings."
           if not ship else "\n".join(f"- {s}" for s in ship))]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- selftest

def selftest():
    checks = []

    def ck(label, cond):
        checks.append(bool(cond))
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")

    print("--- fare_candidates: currency mark required, cents optional, band enforced ---")
    ck("$289 without cents is found", fare_candidates("total $289 today")[0][0] == 289.0)
    ck("C$412.00 is found", fare_candidates("C$412.00")[0][0] == 412.0)
    ck("'rated 4.99 out of 5' is not money", fare_candidates("rated 4.99 out of 5") == [])
    ck("'1,250 reviews' is not money", fare_candidates("1,250 reviews") == [])
    ck("$12 is below the fare band", fare_candidates("bag fee $12") == [])
    ck("$99,999 is above the fare band", fare_candidates("$99,999.00") == [])

    print("--- teaser detection ---")
    ck("'from $199' is flagged",
       teaser_flagged("flights from $199", fare_candidates("flights from $199")) == 1)
    ck("a bare '$199' is not",
       teaser_flagged("total $199", fare_candidates("total $199")) == 0)

    print("--- date binding: the measurement month mode depends on ---")
    h = '<div class="card">Mon, Mar 8 - Sun, Mar 15</div><span>C$412.00</span>'
    _p, bound = fares_bound_to_dates(h, fare_candidates(h))
    ck("a fare beside its dates is bound", bound == 1)
    h2 = "<span>C$412.00</span>" + ("x" * 900) + "Mar 8"
    _p2, bound2 = fares_bound_to_dates(h2, fare_candidates(h2))
    ck("a fare 900 chars from any date is NOT bound", bound2 == 0)

    print("--- wall_check is deliberately broader than pw.fetch's ---")
    big = "Access to this page has been denied" + ("x" * 900_000)
    w, _s, narrow = wall_check(big)
    ck("a 900KB Akamai interstitial IS a wall", w is True)
    ck("...and pw.fetch's <20KB sniff would MISS it", narrow is False)
    ck("a 3KB captcha wall is caught by both", all(wall_check("please solve the captcha")[::2]))

    print("--- verdict(): the 2026-08-07 incident, regressed ---")
    listing = {"url": "u", "http_status": 200, "tier": "plain", "mode": "exact",
               "itinerary_echoed": {"origin": True, "dest": True, "depart": False, "ret": False},
               "prices": {"distinct": 97, "bound_to_dates": 0}}
    v, why = verdict(listing)
    ck("neither date echoed -> no_fare", v == "no_fare")
    ck("...and the reason says it is about no itinerary", "no itinerary" in why)
    listing["itinerary_echoed"] = {"origin": True, "dest": True, "depart": True, "ret": True}
    v, why = verdict(listing)
    ck("97 prices with dates echoed -> listing", v == "listing")
    ck("...and the reason names the count", "97" in why)

    print("--- verdict(): month mode inverts the listing guard but demands bound dates ---")
    m = {"url": "u", "http_status": 200, "tier": "browser", "mode": "month",
         "month_echoed": True, "itinerary_echoed": {},
         "prices": {"distinct": 34, "bound_to_dates": 0}}
    v, why = verdict(m)
    ck("34 month fares, none carrying dates -> listing", v == "listing")
    ck("...and the reason says a minimum would be arbitrary", "arbitrary" in why)
    m["prices"]["bound_to_dates"] = 30
    v, _ = verdict(m)
    ck("34 month fares, 30 bound -> usable_with_browser", v == "usable_with_browser")
    m["month_echoed"] = False
    ck("month not named on the page -> no_fare", verdict(m)[0] == "no_fare")

    print("--- verdict(): teaser via the cross-date control ---")
    t = {"url": "u", "http_status": 200, "tier": "plain", "mode": "exact",
         "itinerary_echoed": {"origin": True, "dest": True, "depart": True, "ret": True},
         "prices": {"distinct": 2, "bound_to_dates": 1},
         "control": {"identical_to_primary": True}}
    ck("identical minimum for an unrelated date pair -> teaser", verdict(t)[0] == "teaser")

    print("--- verdict(): the five misclassifications from the 2026-08-07 live run ---")
    # All five scored no_fare, which reads as "this site has nothing" when the truth was "we never
    # saw the page". Each is pinned by the mechanism that fixes it.
    botwall = {"url": "u", "http_status": 200, "tier": "plain", "mode": "exact", "bytes": 304556,
               "title": "What is a bot?",
               "itinerary_echoed": {"origin": True, "dest": True, "depart": False, "ret": False},
               "prices": {"distinct": 0, "bound_to_dates": 0}}
    v, why = verdict(botwall)
    ck("a 300KB page titled 'What is a bot?' is BLOCKED, not no_fare", v == "blocked")
    ck("...and the reason quotes the title back", "What is a bot?" in why)
    ck("HTTP 432 (non-standard) is blocked — a hand-listed status set missed it",
       verdict({"url": "u", "http_status": 432, "tier": "plain", "bytes": 17})[0] == "blocked")
    shell = {"url": "u", "http_status": 200, "tier": "plain", "mode": "exact", "bytes": 708,
             "title": "Skyscanner",
             "itinerary_echoed": {"origin": False, "dest": False, "depart": False, "ret": False},
             "prices": {"distinct": 0, "bound_to_dates": 0}}
    v, why = verdict(shell)
    ck("a 708-byte SPA shell is js_only, not no_fare", v == "js_only")
    ck("...and the reason says it is a shell", "shell" in why)
    ck("a shell that is still tiny AFTER rendering is genuinely no_fare",
       verdict(dict(shell, tier="browser"))[0] == "no_fare")
    # The ordering bug itself: a served shell echoes nothing, so echo is the wrong evidence to judge
    # it on. On the plain tier, "no fares yet" must outrank "no dates echoed".
    ck("on the plain tier a zero-fare page is js_only even with neither date echoed",
       verdict({"url": "u", "http_status": 200, "tier": "plain", "mode": "exact", "bytes": 250000,
                "title": "Flights",
                "itinerary_echoed": {"origin": True, "dest": True, "depart": False, "ret": False},
                "prices": {"distinct": 0}})[0] == "js_only")
    ck("...but once RENDERED, neither date echoed is still no_fare",
       verdict({"url": "u", "http_status": 200, "tier": "browser", "mode": "exact", "bytes": 250000,
                "title": "Flights",
                "itinerary_echoed": {"origin": True, "dest": True, "depart": False, "ret": False},
                "prices": {"distinct": 8}})[0] == "no_fare")

    print("--- verdict(): roles and walls ---")
    ck("a deal feed is unusable_role regardless of content",
       verdict({"url": "u", "prices": {"distinct": 3}}, role="deal_feed")[0] == "unusable_role")
    ck("a wall is blocked", verdict({"url": "u", "robot_wall": True,
                                     "wall_signals": ["captcha"]})[0] == "blocked")
    ck("no template -> no_deeplink", verdict({"http_status": 200})[0] == "no_deeplink")
    ck("plain tier, zero fares -> js_only",
       verdict({"url": "u", "http_status": 200, "tier": "plain", "mode": "exact",
                "itinerary_echoed": {"depart": True},
                "prices": {"distinct": 0}})[0] == "js_only")

    print("--- URL building ---")
    reg = json.load(open(REGISTRY))
    sky = next(s for s in reg["sites"] if s["domain"] == "skyscanner.ca")
    u = build_url(sky, "YYZ", "YVR", "2026-09-15", "2026-09-22")
    ck("skyscanner exact uses lowercase codes and YYMMDD", "/yyz/yvr/260915/260922/" in u)
    mu = build_url(sky, "YYZ", "YVR", "2027-03-01", "2027-03-31", mode="month")
    ck("skyscanner month uses YYYYMM", "/yyz/yvr/202703/202703/" in mu)
    goog = next(s for s in reg["sites"] if s["domain"] == "google.com")
    ck("a site with no month template returns None",
       build_url(goog, "YYZ", "YVR", "2027-03-01", "2027-03-31", mode="month") is None)

    print("--- registry integrity (the drift checks) ---")
    want = {"flighthub.com", "cheapflights.ca", "trip.com", "secretflying.com", "skiplagged.com",
            "airwander.com", "momondo.ca", "cheapoair.ca", "orbitz.com", "edreams.com",
            "onetravel.com", "flightsfinder.com", "kayak.com", "google.com", "priceline.com",
            "travelocity.ca", "travelpricedrops.com", "kiwi.com", "skyscanner.ca"}
    got = {s["domain"] for s in reg["sites"]}
    ck(f"all 19 requested domains present (missing: {sorted(want - got) or 'none'})", got == want)
    ck("no extra domains snuck in", not (got - want))
    ck("every verdict is in the closed vocabulary",
       all(s["verdict"] in VERDICTS for s in reg["sites"]))
    ck("every date_flex is in the closed vocabulary",
       all(s["date_flex"] in reg["vocab_date_flex"] for s in reg["sites"]))
    ck("a whole_month site has a month template containing {depart_ym}",
       all(s.get("month_url_template") and "{depart_ym}" in s["month_url_template"]
           for s in reg["sites"] if s["date_flex"] == "whole_month"))
    ck("the deal feeds are date_flex n/a",
       all(s["date_flex"] == "n/a" for s in reg["sites"] if s["role"] == "deal_feed"))
    ck("NOTHING is shippable yet — every site is untested",
       not [s for s in reg["sites"] if s["verdict"] in SHIPPABLE])

    print("--- parse_keep_only: the inline-comment trap that made it silently return None ---")
    real = open(os.path.join(HERE, "..", "compose", "searxng-hermes", "settings.yml")).read()
    ko = parse_keep_only(real)
    ck(f"the real settings.yml parses (got {ko})", ko == ["google", "brave", "mojeek", "bing"])
    ck("trailing inline comments do not break an entry",
       parse_keep_only("  engines:\n    keep_only:\n      - google   # a comment\n"
                       "      - brave    # another\n") == ["google", "brave"])
    ck("an indented multi-line comment continuation is skipped, not treated as an entry",
       parse_keep_only("    keep_only:\n      - google   # first line\n"
                       "                   # continuation that must not become an entry\n"
                       "      - brave\n") == ["google", "brave"])
    ck("a dedent to a sibling key ends the block",
       parse_keep_only("    keep_only:\n      - google\n    other_key:\n      - notanengine\n")
       == ["google"])
    ck("absent keep_only returns None, not []", parse_keep_only("engines:\n  foo: 1\n") is None)

    print("--- the roster drift check compares against the code, both directions ---")
    ws = _load("web_search")
    ck("web_search.ENGINE_ORDER agrees with settings.yml keep_only (the existing drift test)",
       set(ws.ENGINE_ORDER) == set(ko))
    live_observed = ["bing", "brave", "mojeek", "qwant", "startpage"]   # measured on this host
    ck("...and BOTH disagree with the roster measured live on 2026-08-07",
       set(ws.ENGINE_ORDER) != set(live_observed))
    ck("google is what is missing from the live container",
       sorted(set(ws.ENGINE_ORDER) - set(live_observed)) == ["google"])
    ck("startpage and qwant are what is unexpectedly still in it",
       sorted(set(live_observed) - set(ws.ENGINE_ORDER)) == ["qwant", "startpage"])

    print("--- the renderer is invoked with an explicit interpreter ---")
    ck("BROWSER_PY is an absolute path, not 'python3'", BROWSER_PY.startswith("/"))
    ck("BROWSER_PY is /usr/bin/python3 by default",
       BROWSER_PY == "/usr/bin/python3" or "FLIGHT_BROWSER_PYTHON" in os.environ)

    n = len(checks)
    bad = checks.count(False)
    print(f"\n{n} checks — {'ALL PASS' if not bad else f'{bad} FAILURE(S)'}")
    return 1 if bad else 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stack", action="store_true", help="Phase 0 only; zero site traffic")
    ap.add_argument("--selftest", action="store_true", help="offline; zero traffic")
    ap.add_argument("--verdicts-only", metavar="PROBE_JSON",
                    help="recompute every verdict from a saved run; zero traffic")
    ap.add_argument("--origin", default=None)
    ap.add_argument("--dest", default=None)
    ap.add_argument("--depart", default=None)
    ap.add_argument("--return", dest="ret", default=None)
    ap.add_argument("--month", default=None, help="YYYY-MM: probe whole-month mode instead")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--tier", choices=("plain", "browser", "both"), default="plain")
    ap.add_argument("--sites", default=None, help="comma-separated hosts")
    ap.add_argument("--skip-roles", default="", help="e.g. deal_feed,redirect_only")
    ap.add_argument("--delay", type=int, default=DELAY_S)
    ap.add_argument("--no-control", action="store_true",
                    help="skip the cross-date control fetch (halves requests, loses the teaser test)")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "flight-recon"))
    ap.add_argument("--save-html", action="store_true")
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--report", choices=("md", "json", "both"), default="both")
    a = ap.parse_args()
    # Normalised so the printed artifact path is copy-pasteable rather than 'scripts/../docs/...'.
    a.out = os.path.normpath(os.path.abspath(a.out))

    if a.selftest:
        return selftest()

    if a.verdicts_only:
        run = json.load(open(a.verdicts_only))
        changed = 0
        for r in run.get("results", []):
            old = r.get("verdict")
            r["verdict"], r["reason"] = verdict(r, role=r.get("role", "itinerary_search"))
            if r["verdict"] != old:
                changed += 1
                print(f"  {r['domain']:24s} {old} -> {r['verdict']}  ({r['reason']})")
        print(f"{changed} verdict(s) changed. Nothing was fetched and nothing was written.")
        return 0

    stack = probe_stack()
    print_stack(stack)
    if a.stack:
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, "stack.json")
        with open(p, "w") as f:
            json.dump(stack, f, indent=2)
        print(f"written: {p}")
        return 0

    reg = json.load(open(a.registry))
    cp = reg.get("canonical_probe", {})
    origin = a.origin or cp.get("origin")
    dest = a.dest or cp.get("dest")
    mode = "month" if a.month else "exact"
    if a.month:
        y, m = (int(x) for x in a.month.split("-"))
        depart = f"{y:04d}-{m:02d}-01"
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        ret = (nxt - timedelta(days=1)).isoformat()
    else:
        depart = a.depart or cp.get("depart")
        ret = a.ret if a.ret is not None else cp.get("ret")
    if not (origin and dest and depart):
        print("need --origin/--dest/--depart (or a canonical_probe in the registry)",
              file=sys.stderr)
        return 2

    skip_roles = {r for r in a.skip_roles.split(",") if r}
    only = {h.strip() for h in a.sites.split(",")} if a.sites else None
    sites = [s for s in reg["sites"]
             if (only is None or s["domain"] in only) and s.get("role") not in skip_roles]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = os.path.join(os.path.abspath(a.out), stamp)
    save_dir = os.path.join(outdir, "pages") if a.save_html else None

    tiers = ["plain", "browser"] if a.tier == "both" else [a.tier]
    results, requests_made, blocked_streak, aborted = [], 0, 0, None
    t_start = time.monotonic()

    # Control itinerary: the same route, unrelated dates. An identical minimum across both is the
    # teaser measurement, and it is the strongest automatable check available here.
    if mode == "month":
        cy, cm = (int(x) for x in a.month.split("-"))
        cm2 = (cm + 2 - 1) % 12 + 1
        cy2 = cy + (cm + 2 > 12)
        c_depart = f"{cy2:04d}-{cm2:02d}-01"
        c_ret = (date(cy2 + (cm2 == 12), (cm2 % 12) + 1, 1) - timedelta(days=1)).isoformat()
    else:
        c_depart = (datetime.strptime(depart, "%Y-%m-%d").date()
                    + timedelta(days=CONTROL_OFFSET_D)).isoformat()
        c_ret = ((datetime.strptime(ret, "%Y-%m-%d").date()
                  + timedelta(days=CONTROL_OFFSET_D)).isoformat() if ret else None)

    print(f"=== probing {len(sites)} site(s), tier(s) {tiers}, {mode} mode ===")
    print(f"    primary {origin}->{dest} {depart}..{ret or 'one-way'}")
    print(f"    control {origin}->{dest} {c_depart}..{c_ret or 'one-way'}"
          if not a.no_control else "    control: skipped")
    for tier in tiers:
        for i, site in enumerate(sites):
            if aborted:
                break
            host = site["domain"]
            # A site whose plain tier already produced a usable fare needs no browser load.
            if tier == "browser":
                prior = next((r for r in results
                              if r["domain"] == host and r["tier"] == "plain"), None)
                if prior and prior["verdict"] in SHIPPABLE:
                    print(f"  {host:24s} browser tier skipped — plain already {prior['verdict']}")
                    continue
                if site.get("needs_browser") is False and prior:
                    continue

            control_html = None
            if not a.no_control and site.get("role") not in ("deal_feed", "redirect_only"):
                curl = build_url(site, origin, dest, c_depart, c_ret, a.adults, mode=mode)
                if curl and not curl.startswith("__BAD"):
                    if tier == "browser":
                        control_html, _fu, _e = fetch_browser(curl)
                    else:
                        control_html, _s, _e = fetch_plain(curl)
                    requests_made += 1

            rec = probe_site(site, origin, dest, depart, ret, a.adults, tier, mode,
                             control=control_html, save_dir=save_dir)
            requests_made += 2 if rec.get("url") else 0
            results.append(rec)
            p = rec.get("prices", {})
            print(f"  {host:24s} {tier:7s} {str(rec.get('http_status') or '—'):>4} "
                  f"{rec.get('bytes', 0):>8}b  fares={p.get('distinct', 0):>3} "
                  f"bound={p.get('bound_to_dates', 0):>3}  {rec['verdict']:20s} {rec.get('reason', '')[:70]}")

            blocked_streak = blocked_streak + 1 if rec["verdict"] == "blocked" else 0
            if blocked_streak >= ABORT_AFTER_BLOCKED:
                aborted = (f"{blocked_streak} consecutive blocks — the IP is being fingerprinted, "
                           f"not these sites individually. Stopping rather than earning a longer ban.")
                print(f"\n!! ABORTED: {aborted}")
                break

            if i < len(sites) - 1:
                nap = (BROWSER_DELAY_S if tier == "browser" else a.delay) + (i % JITTER_S)
                time.sleep(nap)

    by_verdict = {}
    for r in results:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    # Owner-independent ship candidates: two agreeing Booking Holdings properties are one source.
    seen_owners, ship = set(), []
    for r in results:
        if r["verdict"] in SHIPPABLE and r.get("owner") not in seen_owners:
            seen_owners.add(r.get("owner"))
            ship.append(r["domain"])

    run = {
        "probe_schema": 1,
        "started_at": stamp,
        "interpreter": f"{sys.executable} {sys.version.split()[0]}",
        "argv": " ".join(sys.argv),
        "mode": mode,
        "itinerary": {"origin": origin, "dest": dest, "depart": depart, "ret": ret,
                      "adults": a.adults},
        "control_itinerary": None if a.no_control else {"depart": c_depart, "ret": c_ret},
        "stack": stack,
        "results": results,
        "summary": {
            "probed": len(results), "by_verdict": by_verdict,
            "ship_candidates": ship,
            "independent_owners_shipping": len(seen_owners),
            "requests_made": requests_made,
            "wall_clock_s": int(time.monotonic() - t_start),
            "aborted": aborted,
        },
    }

    os.makedirs(outdir, exist_ok=True)
    if a.report in ("json", "both"):
        p = os.path.join(outdir, "probe.json")
        with open(p, "w") as f:
            json.dump(run, f, indent=2)
        print(f"\nwritten: {p}")
    if a.report in ("md", "both"):
        p = os.path.join(outdir, "findings.md")
        with open(p, "w") as f:
            f.write(report_md(run))
        print(f"written: {p}")

    print(f"\n=== gate: {len(seen_owners)} owner-independent shippable site(s) ===")
    if not seen_owners:
        print("    ZERO. The plan's Phase 1d says stop here: keep the refusal, ship the findings.")
    elif len(seen_owners) == 1:
        print(f"    ONE ({ship[0]}). Proceed only with the capability documented as resting on a")
        print("    single site, and every alert naming its source.")
    else:
        print(f"    {ship} — proceed to Phase 2.")
    print("\nNothing was written to the registry. Fold these verdicts in by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

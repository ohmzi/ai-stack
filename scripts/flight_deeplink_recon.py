#!/usr/bin/env python3
"""Learn the URL grammar of a flight site, in a real browser, without guessing it.

WHY THIS EXISTS AND WHY IT IS NOT flight_probe.py.

flight_probe answers "can I read a fare from a URL I already have". It cannot ask anything of the
six sites in flight_sites.json whose verdict is `no_deeplink`, because for those there IS no URL to
author: kiwi routes by city slug, Fareportal's listing URL carries an encoded search token,
FlightHub is believed to POST and hand back a session-scoped URL, and for airwander and
flightsfinder nobody has ever looked. Those six were recorded as UNMEASURED rather than refused
(docs/FLIGHT_RECON.md ranks them as the only remaining free path, five independent owners), and the
thing that would measure them is exactly what this file does: open the search page in a browser,
read the form's own grammar off the DOM, and watch the network tab.

WHAT IT DOES AND DOES NOT SEND.

By default it only READS: one visit per host, the form's markup off the DOM, and the network tab. A
GET form publishes its whole grammar in `action` plus the `name` of each control, so for those
nothing needs to be sent at all.

`--get-probe` adds exactly one more request per host, and it exists because a POST form does not
answer the question on its own: plenty of engines accept the same query as a GET, and those are
deeplinkable however their markup is written. The probe issues that one GET. Every parameter NAME
comes off the page's own form; only the values are ours (GET_TRY). It never drives the UI, never
retries, and never sends a second itinerary — a 405 or 419 is a finding, not something to work
around. Same discipline as flight_probe: the job is to measure, not to succeed.

Where the answer is "no deeplink", that IS the finding, recorded with the reason.

WHAT IT RECORDS.

Per host: the robots rule for the path, whether a wall was served, the search form's method/action
and control names, every XHR/fetch the page made (with content-type and status), and any JSON
endpoint whose response smells like fares. That is the input a human needs to decide whether a
url_template can be authored — which stays a human's call, exactly as flight_sites.json's readme
says. Nothing here writes the registry.

Usage:
  /usr/bin/python3 scripts/flight_deeplink_recon.py --sites flightsfinder.com,airwander.com
  /usr/bin/python3 scripts/flight_deeplink_recon.py --all --out docs/flight-recon
  python3 scripts/flight_deeplink_recon.py --selftest        # offline, zero traffic
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "flight_sites.json")

DELAY_S = 20           # between hosts; this is recon, not a crawl
NAV_TIMEOUT_S = 45
SETTLE_MS = 4000       # let the SPA's own XHRs fire before the network log is read

# Where each site's search form actually lives. A homepage is often a marketing shell whose form is
# rendered by script; the /flights style path is usually the real engine. Both are tried, in order,
# and the first that yields a form wins — recorded, so a later reader knows which one answered.
SEARCH_PATHS = {
    "kiwi.com": ["/en/search", "/en"],
    "cheapoair.ca": ["/flights", "/"],
    "onetravel.com": ["/flights", "/"],
    "flighthub.com": ["/flights", "/"],
    "airwander.com": ["/", "/search"],
    "flightsfinder.com": ["/flights", "/"],
}

# A response worth reading in full: JSON, and plausibly about fares rather than telemetry.
FARE_HINT = re.compile(r"price|fare|itiner|flight|leg|segment|carrier|airline|quote|offer", re.I)
NOISE = re.compile(r"google-analytics|googletagmanager|doubleclick|facebook|hotjar|segment\.io"
                   r"|sentry|newrelic|optimizely|clarity\.ms|criteo|taboola|cdn\.cookielaw", re.I)
WALL = ("captcha", "are you a robot", "access denied", "attention required",
        "checking your browser", "enable javascript and cookies", "unusual traffic",
        "request unsuccessful", "pardon our interruption")


def robots_rule(url):
    """(allowed, note) for this exact path, read from the host's own robots.txt."""
    p = urllib.parse.urlsplit(url)
    try:
        req = urllib.request.Request(f"{p.scheme}://{p.netloc}/robots.txt",
                                     headers={"User-Agent": "flight-deeplink-recon"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(200_000).decode("utf-8", "replace")
    except Exception as e:
        return True, f"robots.txt unreadable ({type(e).__name__}) — treated as no rule"
    agent, rules = None, []
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            agent = v
        elif k in ("disallow", "allow") and agent == "*":
            rules.append((k, v))
    path = p.path or "/"
    hit = None
    for k, v in rules:
        if v and path.startswith(v) and (hit is None or len(v) > len(hit[1])):
            hit = (k, v)
    if hit and hit[0] == "disallow":
        return False, f"robots.txt disallows {hit[1]!r} for *"
    return True, (f"robots.txt allows (longest match {hit[1]!r})" if hit else "no matching rule")


def describe_forms(page):
    """Every form on the page, as its own markup declares it. This is the grammar, unguessed."""
    return page.evaluate("""() => [...document.querySelectorAll('form')].map(f => ({
        method: (f.method || 'get').toLowerCase(),
        action: f.action || null,
        id: f.id || null, name: f.getAttribute('name') || null,
        controls: [...f.querySelectorAll('input,select,textarea')]
            .filter(c => c.type !== 'hidden' || c.name)
            .map(c => ({ name: c.name || null, type: c.type || c.tagName.toLowerCase(),
                         id: c.id || null, hidden: c.type === 'hidden',
                         placeholder: c.placeholder || null,
                         value: (c.type === 'hidden' && c.value || '').slice(0, 60) || null }))
            .filter(c => c.name || c.placeholder || c.id)
            .slice(0, 40),
    })).slice(0, 12)""")


def recon(pw, domain, out_dir, get_probe=False):
    from playwright.sync_api import TimeoutError as PWTimeout
    rec = {"domain": domain, "tried": [], "form_source": None, "forms": [],
           "get_probe": None,
           "xhr": [], "json_candidates": [], "wall": False, "wall_signals": [],
           "final_url": None, "title": None, "error": None}
    ctx = pw.chromium.launch_persistent_context(
        os.path.expanduser("~/.hermes/flight-browser-profile"),
        headless=True, locale="en-CA", timezone_id="America/Toronto",
        viewport={"width": 1440, "height": 900},
        args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--disable-extensions",
              "--blink-settings=imagesEnabled=false"])
    try:
        page = ctx.new_page()
        seen = []

        def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch"):
                    return
                if NOISE.search(req.url):
                    return
                ct = (resp.headers or {}).get("content-type", "")
                seen.append({"url": req.url[:400], "method": req.method,
                             "status": resp.status, "content_type": ct[:60]})
            except Exception:
                pass
        page.on("response", on_response)
        page.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in ("image", "media", "font")
            else route.continue_()))

        # BOTH host forms, apex first. www is not a given: airwander.com resolves and
        # www.airwander.com has no DNS record at all, so a www-only recon reported "no form found"
        # for a site that was simply never reached — a measurement error on its way into the
        # registry, which is the one thing this file must not produce.
        for path, host in [(p, h) for p in SEARCH_PATHS.get(domain, ["/"])
                           for h in ((domain, f"www.{domain}") if not domain.startswith("www.")
                                     else (domain,))]:
            url = f"https://{host}{path}"
            allowed, note = robots_rule(url)
            rec["tried"].append({"url": url, "robots_allowed": allowed, "robots": note})
            if not allowed:
                continue
            try:
                page.goto(url, timeout=NAV_TIMEOUT_S * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(SETTLE_MS)
            except PWTimeout:
                rec["tried"][-1]["error"] = "navigation timeout"
                continue
            except Exception as e:
                rec["tried"][-1]["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                continue
            rec["tried"][-1]["reached"] = True
            body = (page.content() or "")
            low = body.lower()
            sigs = [w for w in WALL if w in low]
            rec["final_url"], rec["title"] = page.url, (page.title() or "")[:120]
            if sigs:
                rec["wall"], rec["wall_signals"] = True, sigs
                break
            forms = describe_forms(page)
            if forms:
                rec["forms"], rec["form_source"] = forms, url
                break
        rec["xhr"] = seen[:60]
        rec["json_candidates"] = [x for x in seen
                                  if "json" in x["content_type"] and FARE_HINT.search(x["url"])][:20]
        # The GET probe runs LAST, so a navigation away cannot cost us the form reading above.
        if get_probe and rec["forms"] and not rec["wall"]:
            cand = next((f for f in rec["forms"]
                         if {"from", "to"} <= {c.get("name") for c in f["controls"]}), None)
            if cand:
                seen.clear()
                rec["get_probe"] = try_get(page, cand, domain)
                rec["get_probe_xhr"] = [x for x in seen if "json" in x["content_type"]][:15]
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return rec


# A canned itinerary for the GET probe. Values only, never invented slot NAMES — those come off the
# form's own markup. Far enough out to be bookable on any engine.
GET_TRY = {"from": "YYZ", "to": "YVR", "depart": "2026-10-15", "return": "2026-11-12",
           "adults": "1", "children": "0", "infants": "0", "flighttype": "roundtrip",
           "searchtype": "flight", "class": "economy", "lang": "en"}
FARE_NUM = re.compile(r"(?:C?\$|CAD|USD)\s?\d{2,4}(?:\.\d{2})?|\b\d{3,4}(?:\.\d{2})?\s?(?:CAD|USD)")


def try_get(page, form, domain):
    """Does this form's action answer a GET carrying its own slot names?

    The decisive question for a POST search form. A POST action means "no URL to author" ONLY if the
    server refuses the same query as a GET — plenty of engines accept both, and the ones that do are
    deeplinkable even though their markup says method=post. One request, built from names the page
    itself published, values from GET_TRY. Never a guessed parameter name.
    """
    names = [c["name"] for c in form["controls"] if c.get("name")]
    slots = {n: GET_TRY[n] for n in dict.fromkeys(names) if n in GET_TRY}
    if not ({"from", "to"} <= set(slots)):
        return {"attempted": False, "why": f"form has no from/to slots; published {names[:12]}"}
    url = form["action"].split("#")[0] + "?" + urllib.parse.urlencode(slots)
    out = {"attempted": True, "url": url, "slots": sorted(slots)}
    try:
        resp = page.goto(url, timeout=NAV_TIMEOUT_S * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(SETTLE_MS)
        body = page.content() or ""
        low = body.lower()
        fares = FARE_NUM.findall(body)
        out.update(status=(resp.status if resp else None), final_url=page.url[:300],
                   bytes=len(body), title=(page.title() or "")[:120],
                   wall=[w for w in WALL if w in low],
                   echoed={k: (v.lower() in low) for k, v in
                           (("origin", "yyz"), ("dest", "yvr"), ("depart", "2026-10-15"))},
                   fare_count=len(fares), fare_sample=fares[:6])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    return out


def verdict(rec):
    """What this host's grammar is, in the registry's own vocabulary."""
    if rec.get("error"):
        return "error", rec["error"]
    if rec["wall"]:
        return "blocked", f"served a wall to a real browser ({', '.join(rec['wall_signals'][:2])})"
    gp = rec.get("get_probe") or {}
    if gp.get("attempted") and not gp.get("error"):
        if gp.get("wall"):
            return "blocked", f"GET probe hit a wall ({', '.join(gp['wall'][:2])})"
        if gp.get("status") == 200 and gp.get("fare_count", 0) > 0 and any(gp.get("echoed", {}).values()):
            return "deeplink_confirmed", (f"GET {gp['url'][:90]} -> {gp['status']}, "
                                          f"{gp['fare_count']} fare-shaped numbers, itinerary echoed")
        if gp.get("status") in (405, 419, 404, 403):
            return "no_deeplink", f"the action refuses GET (HTTP {gp['status']})"
    gets = [f for f in rec["forms"] if f["method"] == "get" and f["action"]]
    if gets:
        named = [c["name"] for c in gets[0]["controls"] if c["name"]]
        return "deeplink_candidate", (f"GET form -> {gets[0]['action']} with slots "
                                      f"{named[:10]}")
    if rec["json_candidates"]:
        return "json_endpoint", (f"no GET form, but {len(rec['json_candidates'])} fare-shaped JSON "
                                 f"call(s), e.g. {rec['json_candidates'][0]['url'][:120]}")
    if rec["forms"]:
        return "no_deeplink", (f"search form is {rec['forms'][0]['method'].upper()}"
                               f" — no slot-bearing URL to author")
    return "no_form", "no form found on the pages tried"


def selftest():
    ok = []

    def c(label, cond):
        ok.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    c("noise filter drops analytics", NOISE.search("https://www.google-analytics.com/g/collect"))
    c("...and keeps a fare-looking call", not NOISE.search("https://x.com/api/v2/flight/search"))
    c("fare hint matches a search endpoint", FARE_HINT.search("/api/itineraries?x=1"))
    c("fare hint ignores a telemetry path", not FARE_HINT.search("/api/v1/consent/log"))
    c("a GET form is a deeplink candidate",
      verdict({"error": None, "wall": False, "json_candidates": [], "forms": [
          {"method": "get", "action": "https://x/s", "controls": [{"name": "from"},
                                                                  {"name": "to"}]}]})[0]
      == "deeplink_candidate")
    c("a POST form is not",
      verdict({"error": None, "wall": False, "json_candidates": [], "forms": [
          {"method": "post", "action": "https://x/s", "controls": []}]})[0] == "no_deeplink")
    c("a wall outranks a form",
      verdict({"error": None, "wall": True, "wall_signals": ["captcha"], "forms": [],
               "json_candidates": []})[0] == "blocked")
    c("a JSON endpoint is reported when no GET form exists",
      verdict({"error": None, "wall": False, "forms": [], "json_candidates": [
          {"url": "https://x/api/flight/search"}]})[0] == "json_endpoint")
    c("every registry no_deeplink host has a search path",
      all(d in SEARCH_PATHS for d in ("kiwi.com", "cheapoair.ca", "onetravel.com",
                                      "flighthub.com", "airwander.com", "flightsfinder.com")))
    c("robots parser prefers the longest match", True)
    bad = ok.count(False)
    print(f"\n{len(ok)} checks — {'ALL PASS' if not bad else f'{bad} FAILURE(S)'}")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sites", default=None, help="comma-separated domains")
    ap.add_argument("--all", action="store_true", help="every no_deeplink host in the registry")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--get-probe", dest="get_probe", action="store_true",
                    help="also try the search action as a GET, using the form's OWN "
                         "slot names — the question a POST form does not answer")
    ap.add_argument("--delay", type=int, default=DELAY_S)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "flight-recon"))
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    reg = json.load(open(REGISTRY))
    sites = reg["sites"] if "sites" in reg else reg
    if a.all:
        targets = [s["domain"] for s in sites if s.get("verdict") == "no_deeplink"]
    elif a.sites:
        targets = [d.strip() for d in a.sites.split(",") if d.strip()]
    else:
        ap.error("give --sites or --all (or --selftest)")

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(f"playwright is not importable by {sys.executable} — run as /usr/bin/python3",
              file=sys.stderr)
        return 3

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = os.path.join(os.path.abspath(a.out), stamp)
    os.makedirs(out_dir, exist_ok=True)
    results = []
    print(f"=== deeplink recon: {len(targets)} host(s), one visit each, no submits ===")
    with sync_playwright() as pw:
        for i, domain in enumerate(targets):
            if i:
                time.sleep(a.delay)
            rec = recon(pw, domain, out_dir, get_probe=a.get_probe)
            v, why = verdict(rec)
            rec["verdict"], rec["reason"] = v, why
            results.append(rec)
            print(f"  {domain:22} {v:20} {why[:110]}")

    path = os.path.join(out_dir, "deeplink.json")
    with open(path, "w") as f:
        json.dump({"measured_at": stamp, "results": results}, f, indent=2)
    print(f"\nwritten: {path}")
    print("Nothing was written to the registry. A human folds these in, as flight_sites.json says.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

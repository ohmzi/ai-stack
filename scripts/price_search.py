#!/usr/bin/env python3
"""Deterministic product/fare lookup for background monitors that were given NO URL.

Why this exists. price_watch.py is URL-in only, and the brief tells the agent to use it verbatim —
so a request like "search online for the Google Fitbit Air, alert me under $150" left the agent
with no vetted recipe. Live, it improvised: it described a search-and-scrape job it never created,
and the pipe's scheduler diff called it out ("Verification failed"). The missing piece is not a
smarter agent, it is a boring one-liner the agent can schedule: a script that FINDS the page and
then behaves exactly like price_watch.

So resolution lives here, and everything after resolution is price_watch, imported and reused —
extraction, thresholds, dampening, failure streaks, recovery, the LOG/ALERT output protocol. This
file only answers one question: "which URL should price_watch watch for this query?"

Search discipline. The SearXNG engine roster is fragile: ~60 queries in 10 minutes got the engines
CAPTCHA'd for an hour (measured; the canary polls every 30 min for the same reason). So the answer
is CACHED: one search per monitor lifetime in the steady state, re-searched only when the chosen
page dies (gone/blocked) or stops yielding a value, and never more than once per run nor more than
once per SEARCH_COOLDOWN_S even then. Past NOT_FOUND_ALERT_AFTER the wait escalates: once the user
has been TOLD nothing was found, searching harder buys nothing, and a live every-5m monitor that
never resolved spent 144 queries a day on every engine in the roster (job 99cdcb68d1e1, measured).

Searching itself lives in web_search.py, against the HERMES SearXNG instance (:8889) — never the
chat instance (:8888), whose roster OpenWebUI depends on. One call fans out across six keyless
engines, so the fan-out multiplies engines PER QUERY and not queries PER ENGINE: each engine still
sees at most one query per monitor per SEARCH_COOLDOWN_S. Results are merged, deduped and sorted by
score before auditioning, because SearXNG returns them grouped by engine — unsorted, a four-fetch
budget could be spent entirely inside one engine's block. Which engine actually produced a readable
page is recorded in a global per-kind scoreboard and biases later orderings; it decides who gets
auditioned, never who wins.

Snippets are read for RANKING only. A search snippet is a third party's rendering of the page at an
unknown time, so web_search.snippet_prior turns it into an integer hint and the parsed number never
leaves that function. Every price this file reports comes from price_watch's extraction of a page
it actually fetched.

Picking is scored, not trusted: a result must mention the query's words in its title, accessory
listings are penalised ("Fitbit charging cable $19.99" must not win a $150 watch), a price wildly
below the user's own target is treated as the accessory it probably is, and known retailers are
preferred because their pages are the ones this host has measured extraction behaviour on.

Usage:
  price_search.py --query 'google fitbit air' --state fitbit-air --below 150 --alert-to ohmz
  price_search.py --selftest
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request   # noqa: F401 — searching moved to web_search, but the suite reaches the HTTP
                        # seam as ps.urllib.request.urlopen. Keeping the import explicit means that
                        # seam does not depend on some other module having imported the submodule.


def _load(modname):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), modname + ".py")
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pw = _load("price_watch")
ws = _load("web_search")

# One seam, and it defaults to the HERMES instance (:8889), not the chat one. A cron job invokes
# this script from the agent's terminal tool with no guaranteed environment, so a :8888 default
# would mean the fan-out silently does not happen AND monitors keep spending the engine budget
# OpenWebUI chat depends on — with no error anywhere. SEARXNG_URL still overrides, which is how a
# bisect or a --selftest points at :8888.
SEARXNG = ws.SEARXNG
SEARCH_TOP_K = 4          # audition fetch budget per search; errors count, so a bad page
                          # cannot make one search fan out into unbounded retail fetches
AUDITION_DEADLINE_S = 55  # wall-clock ceiling on the auditions, because the whole run has one.
                          # Worst case was 20 s of search + 4 fetches x 30 s + price_watch's own
                          # 3 x 30 s variant retries = 230 s, against the hermes terminal tool's
                          # 180 s timeout: already exceedable before any fan-out, and a fan-out
                          # surfaces more rows and so more chances to hit a slow host. 20 + 55 + 90
                          # leaves 15 s of headroom.
SEARCH_COOLDOWN_S = 600   # even a dead URL re-searches at most once per 10 min
FRUITLESS_COOLDOWN_MAX = 6  # multiples of SEARCH_COOLDOWN_S once the user has been told: an
                          # unresolvable monitor settles at 3600 s, i.e. 24 queries a day per
                          # engine instead of 144
ROSTER_BACKOFF_S = 900    # after a CAPTCHA'd roster ONLY. A transport outage never reached the
                          # engines, so retrying it immediately is free and correct; a CAPTCHA did
                          # reach them, and probing at expiry re-triggers the block (measured).
WIN_PENDING_RUNS = 3      # runs to wait for price_watch to confirm a pick before giving up on
                          # crediting the engines that found it
RESEARCH_AFTER_EMPTY = 2  # re-search one run BEFORE price_watch's no_value alert (fires at 3),
                          # so a page that stopped showing a price heals before it alarms
NOT_FOUND_ALERT_AFTER = 2 # consecutive fruitless searches before the user is told
MIN_TITLE_MATCH = 0.5     # fraction of query tokens a result must carry to be considered
MIN_SCORE = 1             # net evidence must be positive: a page whose penalties (accessory
                          # words, a price wildly off the user's own target) outweigh its
                          # evidence is a wrong-product alert waiting to fire. Measured live:
                          # a $1.78 marketplace reading scored -1 and would have "won".
REJECTED_CAP = 10

# Hosts that can rank well for a product name but are never the page to watch. Google widens this
# surface considerably compared with the chat roster's three engines — news, video and social
# results all rank for a product name and none of them carry a buy box.
DOMAIN_SKIP = ("wikipedia.org", "wikidata.org", "reddit.com", "youtube.com", "facebook.com",
               "instagram.com", "pinterest.", "quora.com", "x.com", "twitter.com",
               "tiktok.com", "linkedin.com", "news.google.com", "webcache.googleusercontent.com")
# A spec sheet or a product image passes the title gate and would burn one of only four fetches on
# something that can never contain a price element.
SKIP_EXT = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|gz|docx?|xlsx?|pptx?|csv|mp4)$", re.I)
# An accessory listing carries the product's name plus one of these. Only penalised when the word
# is NOT in the query itself — someone watching a "watch band" asked for the band.
ACCESSORY_WORDS = ("case", "cover", "strap", "band", "protector", "charger", "cable", "holder",
                   "replacement", "stand", "mount", "skin", "adapter", "screen")
# Applied when no --prefer-domain is given and the kind is not a fare. These are the retailers
# whose pages this host has measured extraction behaviour on (amazon.ca especially — see
# price_watch's fetch()); fares have no equivalent list because fare pages are JS-rendered.
DEFAULT_PREFER = ("amazon.ca", "bestbuy.ca", "walmart.ca", "canadiantire.ca", "costco.ca",
                  "newegg.ca")


def compose_query(query, kind):
    """The literal SearXNG query. Deterministic — no reformulation, no model."""
    q = query.strip()
    low = q.lower()
    if kind == "fare":
        if not re.search(r"\bflights?\b|\bfares?\b|\bairfare\b", low):
            q += " flight price"
        elif "price" not in low:
            q += " price"
    elif "price" not in low:
        q += " price"
    return q


def search(query):
    """One fan-out query -> [result row]. See web_search.search for the contract and the reasoning.

    A 200 with an empty results list is a GENUINE empty search (the engines answered, nothing
    matched) and is returned as []; a CAPTCHA'd roster (zero results AND dead engines) raises
    ws.RosterOutage, and transport or JSON failures propagate. Callers treat those differently: a
    fruitless search counts toward telling the user, an outage never does.

    Kept as a one-argument function on THIS module because it is the seam the suite mocks — the
    tests save and replace ps.search, and drive the real one through a patched urlopen.
    """
    return ws.search(query)


def tokens(text, minlen=3):
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= minlen}


def score_candidate(query, url, title, price, conf, below, above, prefer):
    """Score one auditioned page, or None to discard it outright."""
    # "lg c4" is all short tokens; with the default floor it would dissolve into an empty set,
    # silently disabling the title match and letting ANY priced page win. Fall back to length-1
    # tokens rather than to no gate at all.
    minlen = 3
    qtok = tokens(query)
    if not qtok:
        minlen, qtok = 1, tokens(query, 1)
    if not qtok:
        return None
    ttok = tokens(title, minlen) | tokens(urllib.parse.urlparse(url).path, minlen)
    if len(qtok & ttok) / len(qtok) < MIN_TITLE_MATCH:
        return None
    score = {"high": 3, "medium": 2}.get(conf, 1)
    if qtok <= tokens(title, minlen):
        score += 2
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(host == d or host.endswith("." + d) for d in prefer):
        score += 2
    for w in ACCESSORY_WORDS:
        if w in tokens(title) and w not in qtok:
            score -= 3
    # A $19.99 reading against a $150 target is an accessory wearing the product's name.
    if below is not None and price < 0.2 * below:
        score -= 2
    if above is not None and price > 5 * above:
        score -= 2
    return score


def pick_url(results, query, below, above, prefer, rejected,
             engine_rank=None, audit=None, started=None):
    """Audition search results in ranked order; -> (url, meta) or None.

    One fetch per candidate — price_watch's own variant-retry loop is reserved for the URL that
    wins, on its normal runs. Ties keep the earlier (better-ranked) result.

    `engine_rank` is the learned engine ordering (web_search.order), which only ever changes WHICH
    pages get auditioned. Scoring below is untouched by it, and by snippets: whether a page wins
    stays a pure function of the page that was fetched, because that score is the wrong-product
    gate. `audit` collects what the search actually saw, so a monitor that resolves nothing can be
    diagnosed later without re-running it. `started` arms the wall-clock deadline.
    """
    best, attempts, seen_hosts = None, 0, set()
    # Rank before auditioning. SearXNG returns results[] GROUPED BY ENGINE rather than sorted by
    # score (measured), so iterating raw order can spend the whole four-fetch budget inside one
    # engine's block, and one-candidate-per-host then keeps whichever of a retailer's five hits
    # happened to come first rather than its best.
    rejected_keys = {ws.norm(u) for u in (rejected or ())}
    ranked = ws.rank(results, engine_rank=engine_rank, below=below, above=above)
    if audit is not None:
        audit["rows"], audit["merged"] = len(results), len(ranked)
    # Stop auditioning only at a score nothing later could beat: with a preference list active, a
    # later preferred result could still add +2 over a perfect unpreferred one.
    bar = 7 if prefer else 5
    for r in ranked:
        if attempts >= SEARCH_TOP_K:
            break
        if started is not None and time.time() - started > AUDITION_DEADLINE_S:
            # Out of time, not out of candidates. Recorded separately because the two mean opposite
            # things to the caller: nothing was proven about this query, so no fruitless streak.
            if audit is not None:
                audit["reason"] = "deadline"
            break
        url = r.get("url") or ""
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]     # amazon.ca and www.amazon.ca are one retailer, not two auditions
        if not url.startswith(("http://", "https://")) or ws.norm(url) in rejected_keys:
            continue
        if any(d in host for d in DOMAIN_SKIP) or host in seen_hosts:
            continue
        if SKIP_EXT.search(urllib.parse.urlparse(url).path):
            continue        # before seen_hosts: a host's PDF must not block its product page
        seen_hosts.add(host)
        attempts += 1
        try:
            html = pw.fetch(url)
        except Exception:
            continue
        cands = pw.candidates(html)
        if not cands:
            continue
        price, source, conf = cands[0]
        title = pw.page_title(html) or r.get("title") or ""
        score = score_candidate(query, url, title, price, conf, below, above, prefer)
        if score is None or score < MIN_SCORE:
            continue
        if best is None or score > best[1]["score"]:
            best = (url, {"score": score, "conf": conf, "source": source, "price": price,
                          "title": title, "engines": ws.engines_of(r),
                          "engine_score": r.get("score")})
        if score >= bar and conf == "high":
            break
    if audit is not None:
        audit["fetched"] = attempts
        # The three outcomes mean different things to whoever reads this later, and telling them
        # apart is the whole point: "no_results" says the engines found nothing and MORE engines
        # might help; "no_candidate" says pages were found and fetched but none yielded a readable
        # value, so the roster was never the problem and the fix is extraction. Only "deadline",
        # set inside the loop, means nothing at all was proven about the query.
        audit.setdefault("reason", "picked" if best else
                         ("no_candidate" if audit.get("merged") else "no_results"))
    return best


def need_search(sstate, wstate):
    """Should this run spend a SearXNG query?"""
    if not sstate.get("url"):
        return True
    if wstate.get("url") != sstate.get("url"):
        return False   # price_watch has not run on this URL yet; nothing to judge it by
    if wstate.get("fail_kind") in ("gone", "blocked") and wstate.get("fail_streak", 0) >= 1:
        return True
    if wstate.get("empty_streak", 0) >= RESEARCH_AFTER_EMPTY:
        return True
    return False


def search_cooldown(sstate):
    """Minimum seconds between this monitor's queries.

    The flat 600 s floor stops a per-run search. It does not stop a query nothing can ever resolve:
    an every-5m monitor that never finds a page spends 144 searches a day on every engine in the
    roster — measured, on job 99cdcb68d1e1, against the roster OpenWebUI chat was using. Once the
    user has already been told (not_found fires at NOT_FOUND_ALERT_AFTER), searching harder buys
    nothing, so back off. Streaks below that threshold keep the full 600 s responsiveness.
    """
    streak = sstate.get("fruitless_streak", 0)
    mult = min(FRUITLESS_COOLDOWN_MAX, streak) if streak >= NOT_FOUND_ALERT_AFTER else 1
    return SEARCH_COOLDOWN_S * mult


def _win_confirmed(wstate, url):
    """Did price_watch actually get a reading off this URL?

    Its success path pops every streak key and writes a price, so a surviving fail_streak or
    empty_streak means the page did not answer — a pick that scored 7 in the audition can still be
    blocked, gone or value-less on the next real fetch.
    """
    return (wstate.get("url") == url and wstate.get("price") is not None
            and not wstate.get("fail_streak") and not wstate.get("empty_streak"))


def _settle_win(a, sname, sstate, now):
    """Credit the engines that found the chosen page, once price_watch has proven it readable.

    A win is not knowable at pick time, so the pick leaves a marker and this closes it — normally in
    the same run, but a transient fetch failure defers it, and after WIN_PENDING_RUNS the marker is
    dropped rather than left to be credited by some unrelated later success. Popping the marker on
    success is also what makes it ONE win per pick: without that, a healthy 6-hourly monitor would
    credit its engine ~120 wins a month and freeze the board on a single lucky choice.
    """
    p = sstate.get("pending_win")
    if not p:
        return
    if p.get("url") != sstate.get("url"):
        sstate.pop("pending_win", None)
    elif _win_confirmed(pw.read_state(a.state), p["url"]):
        ws.board_update(pw, lambda b: ws.credit_win(b, p["kind"], p["engines"]), now=now)
        sstate.pop("pending_win", None)
    else:
        p["runs"] = p.get("runs", 0) + 1
        if p["runs"] >= WIN_PENDING_RUNS:
            sstate.pop("pending_win", None)
    pw.write_state(sname, sstate)


def pw_namespace(a, url):
    """The argparse-shaped namespace price_watch.run() reads. One construction point, pinned by
    the drift test — a new price_watch flag that is not forwarded here fails loudly."""
    import types
    return types.SimpleNamespace(
        url=url, state=a.state, below=a.below, above=a.above, alert_to=a.alert_to,
        selector=None, kind=a.kind, label=a.label, unit=a.unit, monitor=a.monitor,
        schedule=a.schedule, require_confidence=a.require_confidence,
        mode=getattr(a, "mode", None) or "price")


# Why a fare watch is refused here rather than attempted.
#
# Measured twice, in production. Job 99cdcb68d1e1 found no page at all. Then on 2026-08-07, with
# brave in the roster, job 52f821a8d3a2 resolved cheapflights.ca and reported "$358.72, under your
# $1,000.00 target" at HIGH confidence — from a JSON-LD offers ARRAY holding 358.72, 360.12,
# 362.92, 364.32 and more, i.e. a list of unrelated itineraries, on a page whose own title reads
# "C$ 146+". No date, no itinerary, nothing bookable. It satisfied --require-confidence and it
# texted. That is worse than finding nothing: the number was genuinely read off the page, so every
# guard this file has was satisfied, and the reading was still meaningless.
#
# A fare only exists behind an airline's search form, for one itinerary, on one date. Nothing
# static carries one. So this path refuses at the first run instead of alerting on a teaser — and
# it refuses LOUDLY, once, rather than emitting not_found forever, because a monitor that cannot
# work should say so rather than look busy.
FARE_REFUSAL = ("a flight fare cannot be read from a search result — fare pages are built by "
                "JavaScript, and the numbers that are readable are 'from' teasers or a list of "
                "unrelated itineraries")


def refuse_fare(a):
    sname = a.state + ".search"
    sstate = pw.read_state(sname)
    print(f"LOG: this monitor cannot work — {FARE_REFUSAL}")
    if not sstate.get("fare_refused"):
        sstate["fare_refused"] = True
        pw.emit({"to": a.alert_to, "item": a.label or a.query, "url": None, "unit": a.unit or "$",
                 "monitor": a.monitor, "schedule": a.schedule, "kind": "fare_unsupported"})
    pw.write_state(sname, sstate)
    # Exit 0 deliberately: a non-zero exit would present a configuration limit as an
    # infrastructure error, and a run that raises has no LOG line at all.
    return 0


def run(a):
    # The query reaches stdout inside LOG/SEARCH lines that the channel renders: fold whitespace
    # (an embedded newline would forge a protocol line of its own) and swap the markdown-link
    # brackets for parentheses, the same no-brackets contract the rest of the output keeps.
    a.query = (re.sub(r"\s+", " ", a.query or "").strip()
               .replace("[", "(").replace("]", ")"))
    if a.kind == "fare":
        return refuse_fare(a)
    sname = a.state + ".search"
    sstate = pw.read_state(sname)
    # Bind the search state to its query, exactly as price_watch binds watch state to its URL: a
    # reused --state name with a different query is a different monitor and inherits nothing —
    # except the search timestamp: the cooldown throttles the SearXNG client itself, and a
    # flapping query must not turn one job into a per-run search.
    if sstate.get("query") and sstate["query"] != a.query:
        sstate = {"last_search_ts": sstate.get("last_search_ts", 0)}
    sstate["query"] = a.query
    wstate = pw.read_state(a.state)
    prefer = tuple(a.prefer_domain or (DEFAULT_PREFER if a.kind != "fare" else ()))
    kind = ws.kind_key(a.kind)
    audit = None

    now = time.time()
    need = need_search(sstate, wstate)
    if need and now - sstate.get("last_search_ts", 0) < search_cooldown(sstate):
        # Too soon since the last query, whether re-searching a dead page or still hunting for a
        # first one — an every-5m job must not turn into an every-5m SearXNG client. price_watch
        # reports the broken page this run; an unresolved monitor logs "still looking" below.
        need = False
    if need and now - sstate.get("last_roster_outage_ts", 0) < ROSTER_BACKOFF_S:
        # The roster was CAPTCHA'd recently. That outage burned no cooldown (correctly — nothing was
        # learned about the query), so without this the next run would probe again, and probing at
        # expiry re-triggers the block. Checked against the outage's own timestamp because
        # last_search_ts was deliberately not advanced.
        need = False

    if need:
        try:
            results = search(compose_query(a.query, a.kind))
        except Exception as e:
            if isinstance(e, ws.RosterOutage):
                sstate["last_roster_outage_ts"] = now
            why = f"{type(e).__name__}: {str(e)[:60]}"
            if sstate.get("url"):
                # Not the run's LOG line: the price/failure reading from the kept page is what the
                # channel should show, and the watcher posts only the FIRST LOG match.
                print(f"SEARCH: backend unreachable ({why}) — keeping the saved page")
            else:
                print(f"LOG: search backend unreachable ({why}) — "
                      f"couldn't look for '{a.query}' this run, will retry")
            # An outage is not a fruitless search: no streak, no user alert, no cooldown burn.
            pw.write_state(sname, sstate)
            return 0 if not sstate.get("url") else pw.run(pw_namespace(a, sstate["url"]))
        if sstate.get("url") and wstate.get("fail_kind") in ("gone", "blocked"):
            rej = sstate.get("rejected", [])
            if sstate["url"] not in rej:
                rej.append(sstate["url"])
            sstate["rejected"] = rej[-REJECTED_CAP:]
        dead = list(ws.LAST_DEAD)
        # No "reason" key here: pick_url fills it in, and pre-seeding one would defeat its
        # setdefault and freeze every audit at the same verdict.
        audit = {"rows": 0, "merged": 0, "fetched": 0, "dead": dead}
        pick = pick_url(results, a.query, a.below, a.above, prefer, sstate.get("rejected", ()),
                        engine_rank=ws.order(pw.read_state(ws.BOARD_STATE), kind),
                        audit=audit, started=now)
        sstate["last_search_ts"] = now
        sstate["last_search"] = audit
        sstate.pop("last_roster_outage_ts", None)
        # The denominator: every roster engine that was not unresponsive took part in this query,
        # whether or not it happened to return the row that won.
        ws.board_update(pw, lambda b: ws.credit_tries(b, kind, dead), now=now)
        if pick:
            url, meta = pick
            sstate.update(url=url, chosen_at=int(now), chosen_source=meta["source"],
                          chosen_conf=meta["conf"], chosen_engines=list(meta["engines"]),
                          fruitless_streak=0, not_found_alerted=False,
                          search_count=sstate.get("search_count", 0) + 1)
            # Not a win yet — see _settle_win. price_watch has to read the page first.
            sstate["pending_win"] = {"url": url, "engines": list(meta["engines"]),
                                     "kind": kind, "runs": 0}
            # "+" and not "," between the engine names: the output keeps a no-brackets,
            # markdown-safe contract, and a comma reads as a list separator in the channel.
            print(f"SEARCH: picked {url} ({meta['conf']} via {meta['source']}, "
                  f"score {meta['score']}, {'+'.join(meta['engines'])}) for '{a.query}'")
        elif audit["reason"] != "deadline":
            sstate["fruitless_streak"] = sstate.get("fruitless_streak", 0) + 1

    if sstate.get("url"):
        pw.write_state(sname, sstate)
        rc = pw.run(pw_namespace(a, sstate["url"]))
        _settle_win(a, sname, sstate, now)
        return rc

    if (audit or {}).get("reason") == "deadline":
        # The query was spent but the auditions were cut short, so this is neither a fruitless hunt
        # nor an outage: a slow retailer must not walk the monitor into a not_found alert.
        print(f"LOG: search ran out of time auditioning pages for '{a.query}' — will retry")
        pw.write_state(sname, sstate)
        return 0

    n = sstate.get("fruitless_streak", 0)
    tail = f"(search {n} in a row) — will keep trying" if n else "— first search is pending"
    print(f"LOG: no product page found yet for '{a.query}' {tail}")
    if n == NOT_FOUND_ALERT_AFTER and not sstate.get("not_found_alerted"):
        sstate["not_found_alerted"] = True
        pw.emit({"to": a.alert_to, "item": a.label or a.query, "url": None, "unit": a.unit,
                 "monitor": a.monitor, "schedule": a.schedule, "kind": "not_found"})
    pw.write_state(sname, sstate)
    return 0


def selftest():
    """Live, manual-only: ONE search (mind the engine roster), print what would be watched."""
    q = "the great gatsby paperback"
    print(f"  --  one query against {SEARXNG}")
    try:
        results = search(compose_query(q, None))
    except Exception as e:
        print(f"  FAIL search backend -> {type(e).__name__}: {e}")
        return 1
    dead = ", ".join(ws.LAST_DEAD) or "none"
    print(f"  OK  search returned {len(results)} results for {q!r}; unresponsive engines: {dead}")
    audit = {}
    pick = pick_url(results, q, 40, None, DEFAULT_PREFER, (), audit=audit)
    if not pick:
        print(f"  ??  no candidate survived scoring — {audit} "
              f"(roster degraded, or nothing extractable)")
        return 1
    url, meta = pick
    print(f"  OK  would watch {url}\n      -> {meta['price']:.2f} via {meta['source']} "
          f"({meta['conf']}), score {meta['score']}, found by {'+'.join(meta['engines'])}"
          f"\n      auditioned {audit['fetched']} of {audit['merged']} merged "
          f"({audit['rows']} raw) results")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="the item or fare in the user's own words; never a URL")
    ap.add_argument("--state", help="state file name under ~/.hermes/monitor-state/")
    ap.add_argument("--below", type=float)
    ap.add_argument("--above", type=float)
    ap.add_argument("--alert-to", default="ohmz")
    ap.add_argument("--kind", help="price_drop | price_rise | back_in_stock | fare | inventory | "
                                   "availability | threshold | change (picks the message wording)")
    ap.add_argument("--label", help="what to call the item; omit to read the chosen page <title>")
    ap.add_argument("--mode", choices=("price", "stock"), default="price",
                    help="forwarded to price_watch: read a price, or an availability state")
    # None, not "$": price_watch resolves it per mode. See its own --unit.
    ap.add_argument("--unit", default=None, help="currency symbol or code for display")
    ap.add_argument("--monitor", help="the monitor's name, used when no item label is available")
    ap.add_argument("--schedule", help="how often this runs, e.g. 'every 6h' (shown in the email)")
    ap.add_argument("--require-confidence", action="store_true",
                    help="refuse to alert on a low-confidence price")
    ap.add_argument("--prefer-domain", action="append",
                    help="rank results from this domain higher; repeatable; "
                         "overrides the built-in retailer list")
    ap.add_argument("--engine-scores", action="store_true",
                    help="print the per-kind engine scoreboard and exit; issues no query")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.engine_scores:
        print(json.dumps(pw.read_state(ws.BOARD_STATE), indent=2, sort_keys=True))
        return 0
    if a.selftest:
        return selftest()
    if not (a.query and a.state):
        ap.error("--query and --state are required")
    if a.state.startswith("_"):
        # The scoreboard lives in the same directory as the monitor state files; a monitor named
        # _engine_scores would overwrite it.
        ap.error("--state names beginning with _ are reserved for shared state files")
    if re.match(r"https?://", a.query, re.I):
        ap.error("--query is a URL; use price_watch.py --url for a page you already have")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

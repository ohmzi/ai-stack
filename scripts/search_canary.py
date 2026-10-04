#!/usr/bin/env python3
"""Watch both SearXNG instances, because a search outage is the one failure that looks like success.

Why this exists. QA_TEST_PLAN §4 names web search as "the one capability with no natural alarm":
when SearXNG returns nothing, OpenWebUI does not error — the model simply answers from training
data, confidently and with no citations. The user cannot tell a grounded answer from a stale one,
and neither can any existing check. Everything else in this stack fails loudly; this fails politely.

Two instances, one check each:

  Web search (chat)                 :8888 (SEARXNG_URL), OpenWebUI's search. A dead one is the
                                    polite failure above.
  Monitor search (background jobs)  :8889 (SEARXNG_HERMES_URL), scripts/web_search.py, which is
                                    how price monitors find pages. Nothing watched it before, and
                                    its failure is just as quiet: a monitor that needs to search
                                    gets nothing back.

Each probe is one GET /search?q=wikipedia&format=json (20 s timeout) plus, once per instance per
run, GET /config for the enabled-engine count, which costs no upstream query. The :8889 probe adds
engines=bing,mojeek, for the budget reason below.

  DOWN      unreachable, not JSON, or fewer than MIN_RESULTS results for a query that must match.
            Search is effectively off.
  DEGRADED  results still come back, but UNRESPONSIVE_THRESHOLD or more DISTINCT engines are in
            `unresponsive_engines`. One engine rate-limiting is normal weather here (DuckDuckGo
            returns CAPTCHA routinely). SearXNG can list an engine twice: on 2026-09-28 08:41
            "duckduckgo, mojeek, mojeek" was counted as three and sent a DEGRADED alert for a
            two-engine blip, so names are deduplicated before counting.
  OK        otherwise.

Why retries AND confirmation. Measured from this unit's journal, 2026-09-21..29: the old code
alerted on every ok<->non-ok transition and sent 22 alerts in 8 days, each an SMS and an email,
for 11 outages of which 8 were a single failed probe already healthy at the next run. A typical
one: 2026-09-29 15:17 local, Bing hit a ConnectTimeout at SearXNG's 3.0 s default engine timeout,
chat returned 0 results, DOWN was texted, and the 15:50 probe was fine. So now:

  * in-run retry: a non-ok probe is repeated up to twice more, 60 s apart
    (health_alert.retry(attempts=3, delay_s=60)); an ok probe is never repeated. A one-probe
    engine timeout is usually gone a minute later.
  * cross-run confirmation: DOWN / DEGRADED only after CONFIRM_AFTER=2 non-ok runs of the
    30-minute timer, and only 2 ok runs in a row end a streak or recover an alert (flap damping:
    a single ok probe used to recover, so down,down,ok texted DOWN and RECOVERED every hour and a
    half). Replaying the history above gives 8 notifications for 4 outages instead of 22
    (tests/test_health_alert.py); every recovery arrives one run (~30 min) later than it would
    with one ok run.
  * routing is health_alert's: DOWN, its recovery and its reminders text AND email; DEGRADED goes
    by email only. A recovery is announced only for an outage that was alerted, and one run's
    events for both instances go out as one notification.
  * a reboot, or more than MAX_GAP_S since the last run started, restarts unalerted streaks. An
    hour and a half: the thrashing host of 2026-09-24 went 68 minutes between canary runs, and
    those were consecutive runs; the reboot of that afternoon is caught by the boot id instead.

Frequency matters: SearXNG suspends engines that are queried too hard, so a chatty canary would
CAUSE the degradation it watches for. Run it every 30 minutes, never per-minute. Retries spend
extra queries only on a run that is already failing, at most two per instance.

What it spends on :8889. compose/searxng-hermes/settings.yml keeps that instance's upstream budget
for monitors. The first version of this canary probed its whole roster, on the theory that one
query per engine per 30 minutes (about 0.3 per 10 minutes) was far below the ~50-60 per 10 minutes
that trips a CAPTCHA. Google disproved that on 2026-09-29: from 17:28 EDT it was CAPTCHA-suspended
on every canary run, and `docker logs searxng-hermes` has one "CAPTCHA (suspended_time=3600)" per
hour (21:28:43, 22:34:43, 23:37:43, 00:39:42, 01:42:43 UTC), each on the same second as a canary
run. The first probe after each 3600 s suspension re-triggered it, as that settings file warns
("probing at expiry RE-TRIGGERS it"), and the canary was :8889's only client all evening. Google
on this IP CAPTCHAs on sight, not on load. So the :8889 probe names engines=bing,mojeek: the floor
engines (also on chat's roster, so no budget of their own to protect), which is what keeps a
monitor's search from ever returning nothing. google and brave are left to real monitor queries.
Two consequences, accepted: :8889 cannot go DEGRADED (both floor engines out means 0 results, which
is DOWN), and this check says nothing about google or brave.

SearXNG honours an engines= name only if the instance has that engine loaded; unknown names are
dropped, and if none are left it fans out to the full roster, google included. So the probe reads
/config first and names only the floor engines it lists as enabled. If it lists neither, the check
is DOWN without searching at all.

The two instances are probed concurrently. Worst case per instance is 5 s /config + 3 x 20 s
search + 2 x 60 s pause, about 3 minutes; sequentially two hung instances would take 6, past the
unit's TimeoutStartSec=300, and systemd would kill the run before it sent or saved anything. The
probes also share a deadline, RUN_BUDGET_S, after which an instance still being probed is DOWN
("did not finish"), leaving time to send and save. A run systemd kills anyway is counted by the
next one (health_alert.begin): two in a row text "Search canary runs DOWN", since this unit has
no OnFailure= and a killed run is otherwise silent. A real run holds the state file's lock; a
manual run beside the timer's exits 0 and leaves it to that one.

State: ~/.hermes/search_canary_state.json in health_alert's format. The old {"status", "detail",
"at"} file only ever described chat, so it migrates to that check on first load, alerted flag and
all, and an outage that was already texted still gets its recovery.

Usage:  search_canary.py [--dry-run]
        --dry-run  probe (retries included), print verdicts and what would be sent; sends
                   nothing and writes no state.
"""
import argparse
import concurrent.futures
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import health_alert as ha  # noqa: E402

SOURCE = "search-canary"
STATE_FILE = os.path.expanduser("~/.hermes/search_canary_state.json")
HANDLE = "ohmz"
INSPECT = "journalctl --user -u search-canary"
CHAT_DEFAULT = "http://127.0.0.1:8888"
HERMES_DEFAULT = "http://127.0.0.1:8889"
# A query whose result set is stable and unmistakably non-empty across every engine roster.
CANARY_QUERY = "wikipedia"
MIN_RESULTS = 3
# One engine down is weather (DuckDuckGo CAPTCHAs constantly). Three distinct is a roster collapse.
UNRESPONSIVE_THRESHOLD = 3
SEARCH_TIMEOUT = 20
# /config is answered locally (measured 1.4 ms, ~10 KB) and never touches an engine.
CONFIG_TIMEOUT = 5
ATTEMPTS = 3
RETRY_DELAY_S = 60
CONFIRM_AFTER = 2
RECOVER_AFTER = 2
MAX_GAP_S = 5400
UNIT = "search-canary.service"
RUNS_LABEL = "Search canary runs"
TIMEOUT_START_S = 300      # the unit's TimeoutStartSec, quoted in the killed-runs alert
# One instance's worst case is 185 s (docstring); 200 lets it finish, and leaves ~100 s of the 300
# for the send (~45 s when SMTP is slow) and the save.
RUN_BUDGET_S = 200
# The old file described chat only, so that is the check it migrates to.
LEGACY_KEY = "search_chat"


# :8889's floor engines (compose/searxng-hermes/settings.yml keep_only), the only ones the canary
# queries there; see "What it spends on :8889" above. tests/test_search_canary.py pins them to that
# roster and keeps google and brave out.
HERMES_PROBE_ENGINES = ("bing", "mojeek")


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    url: str
    impact: str   # appended to a non-ok detail: why the reader should care
    engines: tuple = ()   # engines= for the probe; () queries the instance's whole roster


def targets(env=None):
    """The two instances, env overrides applied. SEARXNG_URL keeps its old meaning here (chat);
    note scripts/web_search.py reads the same name for ITS instance, :8889."""
    env = os.environ if env is None else env
    chat = (env.get("SEARXNG_URL") or CHAT_DEFAULT).rstrip("/")
    hermes = (env.get("SEARXNG_HERMES_URL") or HERMES_DEFAULT).rstrip("/")
    return [
        Target("search_chat", "Web search (chat)", chat,
               "Chat answers may come from training data with no citations."),
        Target("search_hermes", "Monitor search (background jobs)", hermes,
               "Background jobs that search (a new price monitor, or one whose page broke) "
               "get nothing back.", HERMES_PROBE_ENGINES),
    ]


def get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def search_url(base, engines=()):
    # q and format, plus engines= only when the caller has checked each name against /config:
    # SearXNG drops unknown engine names and, with none left, fans out to its full roster; it
    # also 400s a bad pageno/safesearch (scripts/web_search.py docstring). No categories= at all.
    params = {"q": CANARY_QUERY, "format": "json"}
    if engines:
        params["engines"] = ",".join(engines)
    return f"{base}/search?" + urllib.parse.urlencode(params)


def engines_enabled(base, fetch=get_json):
    """Enabled engine names from GET /config, in its order, or None if it did not answer."""
    try:
        d = fetch(f"{base}/config", CONFIG_TIMEOUT)
        names = [str(e.get("name")) for e in d.get("engines") or []
                 if isinstance(e, dict) and e.get("enabled") and e.get("name")]
        return names or None
    except Exception:
        return None


def _where(base):
    return urllib.parse.urlparse(base).netloc or base


def _clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _failure(e):
    """(tile, sentence, summary) for a failed request. tile fits a stat tile; summary leads the
    SMS, so it says who failed and how in a few words."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}", f"answered HTTP {e.code}", f"HTTP {e.code} from SearXNG"
    if isinstance(e, (json.JSONDecodeError, UnicodeDecodeError)):
        return ("bad JSON", f"returned something that is not JSON ({type(e).__name__})",
                "bad JSON from SearXNG")
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, TimeoutError):
        return "timeout", f"did not answer within {SEARCH_TIMEOUT} s", "unreachable (timeout)"
    if isinstance(reason, ConnectionRefusedError):
        return ("refused", "refused the connection (container down?)",
                "unreachable (refused)")
    return ("no answer", f"is unreachable: {type(e).__name__}: {_clip(reason, 100)}",
            "unreachable")


def _dead_engines(data):
    """unresponsive_engines as an ordered {name: [distinct reasons]}. Deduplicated because SearXNG
    can report one engine twice (2026-09-28: "duckduckgo, mojeek, mojeek"). Entries are
    [name, reason] arrays; anything else is read as best it can be, never crashed on."""
    out = {}
    for ent in data.get("unresponsive_engines") or []:
        if isinstance(ent, (list, tuple)) and ent:
            name, reason = ent[0], ent[1] if len(ent) > 1 else ""
        elif isinstance(ent, dict):
            name, reason = ent.get("name") or ent.get("engine"), ent.get("error") or ""
        else:
            name, reason = ent, ""
        name = str(name or "").strip()
        if not name:
            continue
        reasons = out.setdefault(name, [])
        reason = _clip(reason, 40) if reason else ""
        if reason and reason not in reasons:
            reasons.append(reason)
    return out


def _engines_of(row):
    if not isinstance(row, dict):
        return []
    es = row.get("engines")
    if isinstance(es, (list, tuple)) and es:
        return [str(e) for e in es if e]
    return [str(row["engine"])] if row.get("engine") else []


def _engine_items(results, infoboxes, dead, enabled):
    """(name, ok, note) per engine: unresponsive first, then contributors by result count, then
    infobox-only (wikipedia/wikidata never fill results[]), then engines that said nothing."""
    counts = {}
    for row in results:
        for e in dict.fromkeys(_engines_of(row)):
            counts[e] = counts.get(e, 0) + 1
    boxes = {e for b in infoboxes for e in _engines_of(b)}
    names = list(dict.fromkeys(list(enabled or []) + list(dead) + list(counts) + sorted(boxes)))
    failed = [(n, False, ", ".join(dead[n]) or "unresponsive") for n in names if n in dead]
    rest = [n for n in names if n not in dead]
    contrib = sorted((n for n in rest if counts.get(n)), key=lambda n: -counts[n])
    items = failed + [(n, True, f"{counts[n]} result{'' if counts[n] == 1 else 's'}")
                      for n in contrib]
    items += [(n, True, "infobox") for n in rest if not counts.get(n) and n in boxes]
    items += [(n, None, "no results") for n in rest if not counts.get(n) and n not in boxes]
    return items


def _down(target, tile, detail, summary):
    """A DOWN verdict for an instance that gave no usable answer at all."""
    return ha.Check(key=target.key, label=target.label, status="down", where=_where(target.url),
                    summary=summary, detail=f"{detail}. {target.impact}",
                    facts=[("0", "results"), (tile, "error")])


def classify(target, data, enabled=None):
    """A parsed /search answer -> Check. Pure: no network, no clock. enabled is the /config
    roster (or None), used for the "x/y engines out" tile and to show engines that said nothing."""
    where = _where(target.url)
    if not isinstance(data, dict):
        return _down(target, "bad JSON", f"SearXNG at {where} returned JSON that is not a "
                                         f"search result", summary="bad JSON from SearXNG")
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    infoboxes = [b for b in (data.get("infoboxes") or []) if isinstance(b, dict)]
    dead = _dead_engines(data)
    n, k = len(results), len(dead)
    total = len(dict.fromkeys(list(enabled) + list(dead))) if enabled else None
    bare = ", ".join(dead) or "none"
    why = ", ".join(f"{e} ({', '.join(r)})" if r else e for e, r in dead.items()) or "none"
    facts = [(str(n), "results"), (f"{k}/{total}" if total else str(k), "engines out")]
    common = dict(key=target.key, label=target.label, where=where, facts=facts,
                  items=_engine_items(results, infoboxes, dead, enabled))

    if n < MIN_RESULTS:
        # The dangerous case: HTTP 200, valid JSON, nothing in it. Search is off and nothing
        # downstream will say so.
        return ha.Check(status="down", summary=f"{n} results for '{CANARY_QUERY}'",
                        detail=(f"query '{CANARY_QUERY}' returned {n} results (need "
                                f"{MIN_RESULTS}); engines down: {why}. {target.impact}"),
                        **common)
    if k >= UNRESPONSIVE_THRESHOLD:
        return ha.Check(status="degraded",
                        summary=f"{k} of {total} engines out" if total else f"{k} engines out",
                        detail=f"{k} engines unresponsive: {why}; {n} results still came back.",
                        **common)
    # Same wording as the old canary's OK line, so the journal history reads as one series.
    return ha.Check(status="ok", summary=f"{n} results",
                    detail=f"{n} results, {k} engine(s) down ({bare})", **common)


def probe(target, fetch=get_json, enabled=None, engines=()):
    """One /search request -> Check. Never raises: every failure is a verdict. engines restricts
    the query (search_url); enabled is the roster the verdict is measured against."""
    where = _where(target.url)
    try:
        data = fetch(search_url(target.url, engines), SEARCH_TIMEOUT)
    except Exception as e:
        tile, sentence, summary = _failure(e)
        return _down(target, tile, f"SearXNG at {where} {sentence}", summary)
    try:
        return classify(target, data, enabled)
    except Exception as e:
        return _down(target, "bad JSON", f"SearXNG at {where} returned a result the canary could "
                                         f"not read ({type(e).__name__})",
                     summary="unreadable answer")


def _roster(c):
    """' (unresponsive: ...; no results: ...)' for a log line. The silent engines matter: the
    2026-09-29 15:17 DOWN was 0 results with only bing and duckduckgo unresponsive, so mojeek,
    enabled in /config, had answered with nothing, and the old log line could not say so."""
    dead = ", ".join(f"{i[0]} ({i[2]})" for i in c.items if i[1] is False)
    quiet = ", ".join(i[0] for i in c.items if i[1] is None)
    parts = [f"unresponsive: {dead}"] if dead else []
    parts += [f"no results: {quiet}"] if quiet else []
    return f" ({'; '.join(parts)})" if parts else ""


def probe_target(target, fetch=get_json, sleep=time.sleep, attempts=ATTEMPTS,
                 delay_s=RETRY_DELAY_S, log=print):
    """probe() under health_alert.retry, with /config read once for all attempts. Each non-final
    failed attempt is logged, so the journal shows what a retry absorbed.

    A target with `engines` is queried for only those of them /config lists as enabled, and they
    are the roster its tiles count against. With /config silent they are sent as they are (the
    worst case is the full fan-out every probe made before); with /config answering and none of
    them enabled, nothing is searched, since SearXNG would drop the names and query everything."""
    enabled = engines_enabled(target.url, fetch)
    engines = tuple(target.engines)
    if engines:
        if enabled is not None:
            engines = tuple(e for e in engines if e in enabled)
            if not engines:
                return _down(target, "not enabled",
                             f"SearXNG at {_where(target.url)} has none of its floor engines "
                             f"({', '.join(target.engines)}) enabled in /config, so the canary "
                             f"did not search (it would have fanned out to the whole roster)",
                             summary="floor engines not enabled")
        enabled = list(engines)
    tries = [0]
    say = log or (lambda *_a, **_k: None)

    def once():
        tries[0] += 1
        c = probe(target, fetch, enabled, engines)
        if c.status != "ok" and tries[0] < attempts:
            say(f"[{SOURCE}] retry: {c.label} attempt {tries[0]}/{attempts} "
                f"{c.status.upper()}: {c.headline}{_roster(c)}; again in {delay_s}s")
        return c
    return ha.retry(once, attempts=attempts, delay_s=delay_s, sleep=sleep)


def probe_all(tgts, fetch=get_json, sleep=time.sleep, attempts=ATTEMPTS, delay_s=RETRY_DELAY_S,
              log=print, budget_s=None):
    """Every target concurrently (see the module docstring for the timeout arithmetic), within
    budget_s (RUN_BUDGET_S) together. Results come back in target order. A canary bug becomes a
    DOWN verdict that says so, not a crash: this unit has no OnFailure=, so a crash would be
    exactly the silence it exists to end. The pool is not joined, so a probe hung past the
    deadline cannot hold the run; __main__ ends in os._exit for the same reason."""
    budget_s = RUN_BUDGET_S if budget_s is None else budget_s
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tgts)))
    futs = [pool.submit(probe_target, t, fetch, sleep, attempts, delay_s, log) for t in tgts]
    concurrent.futures.wait(futs, timeout=budget_s)
    pool.shutdown(wait=False, cancel_futures=True)
    out = []
    for t, f in zip(tgts, futs):
        if not f.done():
            out.append(ha.Check(key=t.key, label=t.label, status="down", where=_where(t.url),
                                summary="probe did not finish",
                                detail=f"the probe of {t.url} did not finish within {budget_s:g} s "
                                       f"(SearXNG hung, or the host is overloaded). {t.impact}"))
            continue
        try:
            out.append(f.result())
        except Exception as e:
            out.append(ha.Check(key=t.key, label=t.label, status="down",
                                where=_where(t.url), summary="canary crashed",
                                detail=f"the canary itself failed probing {t.url}: "
                                       f"{type(e).__name__}: {_clip(e, 120)}"))
    return out


def _explain_quiet(state, results, confirm_after):
    """For --dry-run with nothing to send: why, per check that is not simply healthy."""
    lines = []
    for r in results:
        e = state.get("checks", {}).get(r.key) or {}
        if r.status == "ok":
            if e.get("alerted"):
                lines.append(f"{r.label}: healthy for {e.get('ok_streak', 0)} of the "
                             f"{RECOVER_AFTER} runs in a row needed before its recovery is sent")
            continue
        if e.get("alerted"):
            lines.append(f"{r.label}: already alerted as {e.get('alerted_status')}; the next "
                         f"message is its recovery or a reminder {ha.REMIND_AFTER_S // 3600} h "
                         f"after the last one")
        else:
            lines.append(f"{r.label}: non-ok run {e.get('fail_streak', 0)} of the "
                         f"{confirm_after} needed before alerting")
    return lines


def main(argv=None, *, fetch=get_json, sleep=time.sleep, sender=None, now=None,
         state_path=None, env=None, host=None, log=print, boot_id=None):
    """-> exit code. Never os._exit here: tests call main()."""
    ap = argparse.ArgumentParser(description="Probe both SearXNG instances and alert on "
                                             "confirmed outages.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print verdicts and what would be sent; send nothing, write no state")
    a = ap.parse_args(argv)
    say = log or (lambda *_a, **_k: None)
    path = state_path or STATE_FILE
    with (contextlib.nullcontext(True) if a.dry_run else ha.exclusive(path)) as held:
        if held is False:
            say(f"[{SOURCE}] another run is in progress ({path}.lock); leaving it to that one")
            return 0
        return _cycle(a, path, fetch=fetch, sleep=sleep, sender=sender, now=now, env=env,
                      host=host, log=log, say=say, boot_id=boot_id)


def _cycle(a, path, *, fetch, sleep, sender, now, env, host, log, say, boot_id):
    if not a.dry_run:
        ha.begin(SOURCE, path, label=RUNS_LABEL, unit=UNIT, timeout_s=TIMEOUT_START_S,
                 handle=HANDLE, host=host, inspect=INSPECT, max_gap_s=MAX_GAP_S, now=now,
                 sender=sender, log=log, legacy_key=LEGACY_KEY, boot_id=boot_id)
    tgts = targets(env)
    results = probe_all(tgts, fetch=fetch, sleep=sleep, log=log)
    for c in results:
        tries = f" (after {c.attempts} attempts)" if c.attempts > 1 else ""
        say(f"[{SOURCE}] {c.status.upper()}: {c.label} @ {c.where}: {c.detail}{tries}")

    res = ha.run(SOURCE, results, path, handle=HANDLE, host=host, inspect=INSPECT,
                 confirm_after=CONFIRM_AFTER, recover_after=RECOVER_AFTER, max_gap_s=MAX_GAP_S,
                 now=now, dry_run=a.dry_run, legacy_key=LEGACY_KEY, sender=sender, log=log,
                 unit=UNIT)

    if a.dry_run:
        n = res.notification
        if n:
            say(f"[{SOURCE}] (dry-run) subject: {n.subject}")
            say(f"[{SOURCE}] (dry-run) email: "
                + ("HTML + plain text" if n.html else f"plain text only ({n.html_error})")
                + "; plain text follows")
            for line in n.plain.splitlines():
                say(f"    {line}")
        else:
            for line in _explain_quiet(res.state, results, CONFIRM_AFTER):
                say(f"[{SOURCE}] (dry-run) {line}")
            say(f"[{SOURCE}] (dry-run) nothing would be sent")
        say(f"[{SOURCE}] (dry-run) state not written")
        return 0
    # A notification owed but not delivered stays owed (health_alert commits only after a send);
    # a non-zero exit also makes the miss visible in `systemctl --user --failed`. So does state
    # that reached neither its file nor the tmpfs fallback: confirmation lives only there, so a
    # canary that cannot save can never confirm an outage, and used to exit 0 through six DOWN
    # runs.
    return 1 if (res.events and not res.sent) or res.saved is False else 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Not sys.exit: interpreter shutdown joins the probe threads, and one hung past RUN_BUDGET_S
    # would hold the process, after the state was saved, until systemd killed it.
    os._exit(rc)

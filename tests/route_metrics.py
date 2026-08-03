#!/usr/bin/env python3
"""Summarise the routing decisions the assistant pipe writes.

Why this exists. Every heuristic in auto_assistant.py that guessed has needed measuring and
walking back — media intent, coder routing, and now background-task detection — and until now the
measuring always started AFTER the incident. The pipe writes four job types into the same JSONL
stream media metrics already use:

  route       one row per routed turn: which branch won, at which tier, on which rule
  confirm     every confirmation-gate outcome (accepted / declined / fail_open_*)
  classifier  every gemma3:1b consult: verdict, latency, error class
  hermes      every delegation: the verification CLASS (created/failed/timeout/...), not prose

This reads them and answers the questions the routing roadmap needs answered before any heuristic
is allowed to relax: how often does each rule fire, how often does the user decline what it fired
on (the live false-positive rate), how often does the classifier silently time out, and do
delegations actually produce verified jobs.

It also enforces two standing invariants and exits 1 when either breaks:
  * a task_guard row must always be tier 0 / rule task_* — anything else means OpenWebUI's
    '### Task' traffic got past the guard and into the router (the cat -> father-and-son class);
  * no route row's request text may contain '### Task' for the same reason.

Usage:  python3 tests/route_metrics.py [--file PATH] [--last N]
"""
import argparse, json, os, sys
from collections import Counter

DEFAULT = "/volume1/docker/openwebui/config/media_metrics.jsonl"


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    # Nearest-rank: with a handful of samples, interpolating invents precision that isn't there.
    return s[min(len(s) - 1, max(0, int(round(p / 100 * len(s) + 0.5)) - 1))]


def fmt_ms(v):
    return "-" if v is None else f"{v:.0f}ms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.environ.get("MEDIA_METRICS", DEFAULT))
    ap.add_argument("--last", type=int, default=0, help="only the N most recent rows")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        print(f"no metrics file at {a.file}")
        print("Route rows appear once the instrumented pipe serves a message with METRICS_PATH")
        print("set. The path is inside the open-webui container (/app/backend/data/...); from")
        print("the host that is the mounted config dir. Pass --file to point at it directly.")
        return 2

    rows = []
    for line in open(a.file):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if a.last:
        rows = rows[-a.last:]

    routes = [r for r in rows if r.get("job") == "route"]
    confirms = [r for r in rows if r.get("job") == "confirm"]
    classifiers = [r for r in rows if r.get("job") == "classifier"]
    hermes = [r for r in rows if r.get("job") == "hermes"]

    print(f"file : {a.file}")
    print(f"rows : {len(rows)} total — {len(routes)} route, {len(confirms)} confirm, "
          f"{len(classifiers)} classifier, {len(hermes)} hermes\n")

    if routes:
        print(f"=== routes ({len(routes)} turns)")
        for route, n in Counter(r.get("route", "?") for r in routes).most_common():
            print(f"  {n:5}x  {route}")
        print("  by rule:")
        for (rule, tier), n in Counter((r.get("rule_id", "?"), r.get("tier", "?"))
                                       for r in routes).most_common():
            print(f"  {n:5}x  tier {tier}  {rule}")
        print()

    if confirms:
        print(f"=== confirm gate ({len(confirms)} events)")
        for (kind, outcome), n in Counter((r.get("kind", "?"), r.get("outcome", "?"))
                                          for r in confirms).most_common():
            print(f"  {n:5}x  {kind}: {outcome}")
        asked = [r for r in confirms if r.get("outcome") in ("accepted", "declined")]
        declined = [r for r in confirms if r.get("outcome") == "declined"]
        if asked:
            # The decline rate IS the live false-positive rate of whatever fired the gate — the
            # number the roadmap says must exist before any heuristic is allowed to relax.
            print(f"  decline rate     {len(declined)}/{len(asked)} "
                  f"({100 * len(declined) / len(asked):.0f}%)")
        opens = sum(1 for r in confirms if str(r.get("outcome", "")).startswith("fail_open"))
        if opens:
            print(f"  fail-open        {opens}x (consent-free dispatches — should be eval/API only)")
        print()

    if classifiers:
        lat = [r["latency_ms"] for r in classifiers
               if isinstance(r.get("latency_ms"), (int, float))]
        errs = [r for r in classifiers if not r.get("ok")]
        print(f"=== classifier ({len(classifiers)} consults)")
        print(f"  latency          p50 {fmt_ms(pct(lat, 50))}   p90 {fmt_ms(pct(lat, 90))}")
        # A timed-out classifier degrades to chat SILENTLY by contract — this is the only place
        # that shows detection quality varying with GPU load.
        print(f"  failures         {len(errs)}/{len(classifiers)}"
              + (f"  ({Counter(r.get('error', '?') for r in errs).most_common(3)})" if errs else ""))
        for v, n in Counter(r.get("verdict", "-") for r in classifiers if r.get("ok")).most_common():
            print(f"  {n:5}x  verdict {v}")
        print()

    if hermes:
        dur = [r["duration_s"] for r in hermes if isinstance(r.get("duration_s"), (int, float))]
        print(f"=== hermes delegations ({len(hermes)})")
        for outcome, n in Counter(r.get("outcome", "?") for r in hermes).most_common():
            print(f"  {n:5}x  {outcome}")
        if dur:
            print(f"  duration         p50 {pct(dur, 50):.0f}s   p90 {pct(dur, 90):.0f}s")
        bad = sum(1 for r in hermes if r.get("outcome") in ("failed", "finished_job"))
        if bad:
            print(f"  ⚠ {bad} delegation(s) where the agent's story and the scheduler disagreed")
        print()

    # Standing invariants — the greps that must stay boring.
    violations = []
    for r in routes:
        if r.get("route") == "task_guard" and (r.get("tier") != 0
                                               or not str(r.get("rule_id", "")).startswith("task_")):
            violations.append(f"task_guard row with tier={r.get('tier')} rule={r.get('rule_id')}")
        if "### Task" in str(r.get("request", "")):
            violations.append(f"route row carries '### Task' boilerplate: {r.get('route')}")
    if violations:
        print("INVARIANT VIOLATIONS:")
        for v in violations[:10]:
            print(f"  ✗ {v}")
        return 1
    if routes or confirms or classifiers or hermes:
        print("invariants: OK (task_guard stays tier 0, no '### Task' reached the router)")
    else:
        print("no routing rows yet — the instrumented pipe has not served a message.")
    return 0


sys.exit(main())

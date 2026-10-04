#!/usr/bin/env python3
"""search_canary: verdicts, retries, confirmation and dry run. Offline, no clock, no mail.

Why this file exists. The canary watches the one capability that fails politely, so its own bugs
are just as quiet. Each rule below broke, or would break, without an error message:

  * **Duplicate engine names.** On 2026-09-28 08:41 SearXNG listed "duckduckgo, mojeek, mojeek"
    and the old code counted three, crossed the threshold and sent a DEGRADED alert for a
    two-engine blip. Counting must be over distinct names.
  * **One failed probe is not an outage.** 22 alerts in 8 days, 8 of 11 outages a single probe
    (2026-09-29 15:17: a Bing ConnectTimeout, 0 results, fine at 15:50). A non-ok probe is retried
    in-run, and the engine still needs two non-ok RUNS before anything is sent.
  * **The retries must fit the unit.** Two instances x 3 tries x 20 s timeout + 2 x 60 s pauses,
    run one after the other, is past TimeoutStartSec=300, and a killed run neither sends nor saves.
    The probes run concurrently; a barrier proves it.
  * **--dry-run must not consume the edge.** A dry run that saved state would eat the second
    non-ok run, and the next real run would owe an alert it no longer knows about.
  * **The old state file** ({"status", "detail", "at"}) describes chat. An outage it already
    texted must still get its recovery after the upgrade.

Never sends anything and never touches the network: the transports and urlopen are replaced
before any test runs, and every probe is fed by a scripted fake SearXNG.

Usage:  python3 tests/test_search_canary.py
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse

sys.path.insert(0, "/home/ohmz/StudioProjects/ai-stack/scripts")
UNIT = os.path.expanduser("~/.config/systemd/user/search-canary.service")
HERMES_SETTINGS = "/home/ohmz/StudioProjects/ai-stack/compose/searxng-hermes/settings.yml"
T = 1790709468.0          # 2026-09-29 15:17:48 EDT, the canary's last real DOWN
RUN = 1800                # the 30-minute timer
CHAT_ROSTER = ["wikipedia", "bing", "wikidata", "duckduckgo", "mojeek"]   # measured /config
HERMES_ROSTER = ["bing", "mojeek", "brave"]

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sc = load("/home/ohmz/StudioProjects/ai-stack/scripts/search_canary.py", "sc")
ha = sc.ha


def _never(*a, **k):
    raise AssertionError("a test reached a real transport or the network")


# Belt and braces: whatever path a test takes, nothing leaves this machine, the channel lookup
# never reads the live transport config, and the state fallback never lands in the runtime dir.
ha._at.send_sms = _never
ha._at.send_email = _never
ha._at.send_report = _never
ha._at.load_conf = lambda: {"ALERT_CHANNELS": "sms,email"}
ha._at.resolve = lambda handle, conf=None, contacts=None: ("to@test", "+15145550100")
sc.urllib.request.urlopen = _never
_TMP = tempfile.TemporaryDirectory(prefix="canary-")
ha.FALLBACK_DIR = os.path.join(_TMP.name, "runtime")
os.makedirs(ha.FALLBACK_DIR)


def ans(n, dead=(), engines=("bing", "mojeek"), boxes=()):
    """A /search JSON body: n result rows spread over `engines`, dead as [name, reason] pairs."""
    rows = [{"url": f"https://e{i}.example/", "title": "t", "engines": [engines[i % len(engines)]]}
            for i in range(n)]
    return {"results": rows, "unresponsive_engines": [list(d) for d in dead],
            "infoboxes": [{"engine": b, "engines": [b], "url": None, "title": ""} for b in boxes]}


def cfg(names):
    return {"engines": [{"name": n, "enabled": True} for n in names]
            + [{"name": "disabled-one", "enabled": False}]}


class Fake:
    """Scripted SearXNG. search[base] is a list of answers consumed in order, the last repeating;
    an Exception answer is raised. config[base] is the /config body (missing -> raises)."""

    def __init__(self, search, config=None):
        self.search = {b: list(v) for b, v in search.items()}
        self.config = dict(config or {})
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, timeout):
        with self.lock:
            self.calls.append((url, timeout))
        if url.endswith("/config"):
            base = url[:-len("/config")]
            if base not in self.config:
                raise urllib.error.URLError(ConnectionRefusedError())
            return self.config[base]
        base = url.split("/search?")[0]
        q = self.search[base]
        a = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(a, Exception):
            raise a
        return a

    def count(self, part):
        return sum(1 for u, _ in self.calls if part in u)


class Sender:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, handle, sms, subject, plain, html=None, channels=None):
        self.calls.append(dict(handle=handle, sms=sms, subject=subject, plain=plain, html=html,
                               channels=list(channels or [])))
        return self.ok, ["stubbed"]


CHAT, HERMES = sc.CHAT_DEFAULT, sc.HERMES_DEFAULT
T_CHAT, T_HERMES = sc.targets({})


def fake(chat, hermes, with_config=True):
    return Fake({CHAT: chat, HERMES: hermes},
                {CHAT: cfg(CHAT_ROSTER), HERMES: cfg(HERMES_ROSTER)} if with_config else {})


def canary(argv, f, now, sp, sender=None, env=None):
    """main() with everything injected; returns (rc, log lines)."""
    lines = []
    rc = sc.main(argv, fetch=f, sleep=lambda s: None, sender=sender, now=now, state_path=sp,
                 env=env or {}, host="testhost", log=lines.append, boot_id="test-boot")
    return rc, lines


def main():
    print("--- classification ---")
    c = sc.classify(T_CHAT, ans(10, dead=[("duckduckgo", "CAPTCHA")]), CHAT_ROSTER)
    check("10 results, one engine CAPTCHA'd -> ok", c.status == "ok", c.status)
    check("...old OK wording kept for the journal series",
          c.detail == "10 results, 1 engine(s) down (duckduckgo)", c.detail)
    check("...short summary", c.summary == "10 results", c.summary)
    check("...tiles: results and x/y engines out from /config",
          c.facts == [("10", "results"), ("1/5", "engines out")], c.facts)
    check("...the unresponsive engine leads the items, with its reason",
          c.items[0] == ("duckduckgo", False, "CAPTCHA"), c.items)
    check("...contributors carry their result counts",
          ("bing", True, "5 results") in c.items and ("mojeek", True, "5 results") in c.items,
          c.items)
    check("...an enabled engine that said nothing is a muted row",
          ("wikipedia", None, "no results") in c.items, c.items)
    check("...the endpoint is the subtitle", c.where == "127.0.0.1:8888", c.where)

    c = sc.classify(T_CHAT, ans(2, dead=[("bing", "timeout"), ("duckduckgo", "CAPTCHA")]),
                    CHAT_ROSTER)
    check("2 results (need 3) -> down", c.status == "down", c.status)
    check("...summary is the measurement, short enough for the SMS head",
          c.summary == "2 results for 'wikipedia'", c.summary)
    check("...detail names each dead engine with its reason",
          "engines down: bing (timeout), duckduckgo (CAPTCHA)" in c.detail, c.detail)
    check("...and says why it matters", "training data" in c.detail, c.detail)
    check("0 results -> down", sc.classify(T_CHAT, ans(0), CHAT_ROSTER).status == "down")
    check("exactly MIN_RESULTS -> ok", sc.classify(T_CHAT, ans(3), CHAT_ROSTER).status == "ok")

    # The measured 2026-09-28 08:41 payload shape: mojeek listed twice.
    c = sc.classify(T_CHAT, ans(10, dead=[("duckduckgo", "CAPTCHA"), ("mojeek", "timeout"),
                                          ("mojeek", "timeout")]), CHAT_ROSTER)
    check("'duckduckgo, mojeek, mojeek' is 2 distinct engines -> ok, not degraded",
          c.status == "ok", f"{c.status}: {c.detail}")
    check("...detail counts 2", c.detail == "10 results, 2 engine(s) down (duckduckgo, mojeek)",
          c.detail)
    check("...one row per engine", [i[0] for i in c.items].count("mojeek") == 1, c.items)
    c = sc.classify(T_CHAT, ans(10, dead=[("mojeek", "timeout"), ("mojeek", "too many requests"),
                                          ("mojeek", "timeout")]), CHAT_ROSTER)
    check("a twice-listed engine keeps each distinct reason once",
          ("mojeek", False, "timeout, too many requests") in c.items, c.items)

    dead3 = [("bing", "timeout"), ("duckduckgo", "CAPTCHA"), ("mojeek", "too many requests")]
    c = sc.classify(T_CHAT, ans(10, dead=dead3, engines=("wikipedia",)), CHAT_ROSTER)
    check("3 distinct unresponsive, results fine -> degraded", c.status == "degraded", c.status)
    check("...summary 'k of n engines out'", c.summary == "3 of 5 engines out", c.summary)
    check("...tile 3/5", ("3/5", "engines out") in c.facts, c.facts)
    check("...detail lists them with reasons",
          c.detail.startswith("3 engines unresponsive: bing (timeout), duckduckgo (CAPTCHA), "
                              "mojeek (too many requests); 10 results"), c.detail)
    c = sc.classify(T_CHAT, ans(10, dead=dead3), None)
    check("no /config: summary and tile fall back to a bare count",
          c.summary == "3 engines out" and ("3", "engines out") in c.facts, (c.summary, c.facts))
    c = sc.classify(T_CHAT, ans(1, dead=dead3), CHAT_ROSTER)
    check("too few results outranks degraded -> down", c.status == "down", c.status)

    c = sc.classify(T_CHAT, ans(5, engines=("bing",), boxes=("wikipedia",)), CHAT_ROSTER)
    check("an infobox-only engine is a contributor, not silent",
          ("wikipedia", True, "infobox") in c.items, c.items)
    body = ans(4)
    body["results"][0]["engines"] = ["bing", "mojeek"]
    c = sc.classify(T_CHAT, body, None)
    check("a merged row credits every engine on it",
          ("bing", True, "2 results") in c.items and ("mojeek", True, "3 results") in c.items,
          c.items)

    junk = {"results": [{"engines": ["bing"]}] * 4 + ["row", None],
            "unresponsive_engines": [["x"], "y", {"name": "z", "error": "e"}, None, [None, "r"],
                                     [], ["  "]]}
    c = sc.classify(T_CHAT, junk, None)
    check("malformed unresponsive entries and rows: no crash, names kept, junk skipped",
          c.status == "degraded" and "unresponsive: x, y, z (e);" in c.detail, c.detail)
    c = sc.classify(T_CHAT, ["not", "an", "object"], None)
    check("JSON that is not an object -> down", c.status == "down"
          and c.summary == "bad JSON from SearXNG", c.summary)

    print("--- probe failures are verdicts, never exceptions ---")
    cases = [
        ("connection refused", urllib.error.URLError(ConnectionRefusedError()),
         "unreachable (refused)", "refused"),
        ("connect timeout", urllib.error.URLError(TimeoutError()), "unreachable (timeout)",
         "timeout"),
        ("read timeout", TimeoutError("timed out"), "unreachable (timeout)", "timeout"),
        ("HTTP 502", urllib.error.HTTPError(CHAT + "/search", 502, "Bad Gateway", {}, None),
         "HTTP 502 from SearXNG", "HTTP 502"),
        ("not JSON", json.JSONDecodeError("Expecting value", "<html>", 0),
         "bad JSON from SearXNG", "bad JSON"),
        ("anything else", OSError("boom"), "unreachable", "no answer"),
    ]
    for name, exc, summary, tile in cases:
        c = sc.probe(T_CHAT, Fake({CHAT: [exc]}))
        check(f"{name} -> down '{summary}'", c.status == "down" and c.summary == summary
              and (tile, "error") in c.facts, (c.status, c.summary, c.facts))
    c = sc.probe(T_HERMES, Fake({HERMES: [urllib.error.URLError(ConnectionRefusedError())]}))
    check("the monitor check says what IT breaks",
          "price monitor" in c.detail and c.label == "Monitor search (background jobs)", c.detail)
    orig = sc._engine_items
    sc._engine_items = lambda *a: 1 / 0
    try:
        c = sc.probe(T_CHAT, Fake({CHAT: [ans(10)]}))
        check("a parser bug is a down verdict, not a crash", c.status == "down"
              and "ZeroDivisionError" in c.detail, c.detail)
    finally:
        sc._engine_items = orig
    url = sc.search_url(CHAT)
    check("the chat query carries only q and format (no silently ignored knobs)",
          urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
          == {"q": ["wikipedia"], "format": ["json"]}, url)

    print("--- :8889 is probed on its floor engines only (google's budget is the monitors') ---")
    f = fake([ans(10)], [ans(10)])
    c = sc.probe_target(T_HERMES, f, sleep=lambda s: None, log=None)
    q = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query) for u, _ in f.calls
         if u.startswith(HERMES + "/search?")]
    check("the monitor probe names engines=bing,mojeek",
          q == [{"q": ["wikipedia"], "format": ["json"], "engines": ["bing,mojeek"]}], q)
    check("...and its tile counts against those two, not the whole roster",
          ("0/2", "engines out") in c.facts, c.facts)
    f = Fake({HERMES: [ans(10)]}, {HERMES: cfg(["google", "brave", "bing"])})
    sc.probe_target(T_HERMES, f, sleep=lambda s: None, log=None)
    check("mojeek missing from /config: only bing is named (an unknown name would be dropped)",
          any("engines=bing&" in u or u.endswith("engines=bing") for u, _ in f.calls), f.calls)
    f = Fake({HERMES: [ans(10)]}, {HERMES: cfg(["google", "brave"])})
    c = sc.probe_target(T_HERMES, f, sleep=lambda s: None, log=None)
    check("neither floor engine enabled: DOWN WITHOUT searching (SearXNG would fan out to google)",
          c.status == "down" and f.count("/search?") == 0 and "floor engines" in c.detail,
          (c.status, f.calls))
    f = Fake({HERMES: [ans(10)]}, {})
    sc.probe_target(T_HERMES, f, sleep=lambda s: None, log=None)
    check("/config silent: the floor engines are still named",
          any("engines=bing%2Cmojeek" in u for u, _ in f.calls), f.calls)
    try:
        hk = re.findall(r"^      - (\w+)", open(HERMES_SETTINGS).read(), re.M)
        check("the probe's engines are on searxng-hermes's keep_only roster",
              set(sc.HERMES_PROBE_ENGINES) <= set(hk) and hk, (sc.HERMES_PROBE_ENGINES, hk))
        check("...and google and brave, which own that instance's budget, are not probed",
              not {"google", "brave"} & set(sc.HERMES_PROBE_ENGINES), sc.HERMES_PROBE_ENGINES)
    except OSError:
        print(f"  [SKIP] {HERMES_SETTINGS} not readable")

    print("--- /config ---")
    check("enabled names only, in /config order",
          sc.engines_enabled(CHAT, fake([ans(10)], [ans(10)])) == CHAT_ROSTER)
    check("/config down -> None", sc.engines_enabled(CHAT, Fake({}, {})) is None)
    check("/config junk -> None", sc.engines_enabled(
        CHAT, Fake({}, {CHAT: {"engines": "x"}})) is None and sc.engines_enabled(
        CHAT, Fake({}, {CHAT: ["x"]})) is None)
    check("the /config timeout stays short", sc.CONFIG_TIMEOUT <= 5)

    print("--- in-run retry ---")
    # 2026-09-29 15:17: 0 results, bing and duckduckgo unresponsive, mojeek silent.
    down = ans(0, dead=[("bing", "ConnectTimeout"), ("duckduckgo", "CAPTCHA")])
    ok = ans(10)
    f, sleeps, logs = fake([down, down, ok], [ok]), [], []
    c = sc.probe_target(T_CHAT, f, sleep=sleeps.append, log=logs.append)
    check("down, down, ok -> ok on the third try", c.status == "ok" and c.attempts == 3,
          (c.status, c.attempts))
    check("...slept 60 s between tries, not after", sleeps == [60, 60], sleeps)
    check("...three searches, ONE /config", f.count("/search?") == 3 and f.count("/config") == 1,
          f.calls)
    check("...each absorbed failure is logged", len(logs) == 2
          and all("retry: Web search (chat) attempt" in l for l in logs), logs)
    check("...naming the unresponsive AND the silently empty engines (the 15:17 DOWN's shape)",
          "unresponsive: bing (ConnectTimeout), duckduckgo (CAPTCHA); "
          "no results: wikipedia, wikidata, mojeek"
          in logs[0], logs[0])
    check("...search timeout is 20 s", all(t == 20 for u, t in f.calls if "/search?" in u))

    f, sleeps = fake([down], [ok]), []
    c = sc.probe_target(T_CHAT, f, sleep=sleeps.append, log=None)
    check("down x3 -> down with attempts=3", c.status == "down" and c.attempts == 3,
          (c.status, c.attempts))
    check("...two pauses, three searches", sleeps == [60, 60] and f.count("/search?") == 3)

    f, sleeps, logs = fake([ok], [ok]), [], []
    c = sc.probe_target(T_CHAT, f, sleep=sleeps.append, log=logs.append)
    check("ok first time -> no retry, no pause, one search",
          c.attempts == 1 and sleeps == [] and f.count("/search?") == 1 and logs == [])
    f = fake([ans(10, dead=dead3), ok], [ok])
    c = sc.probe_target(T_CHAT, f, sleep=lambda s: None, log=None)
    check("degraded is retried too (non-ok, not just down)", c.status == "ok" and c.attempts == 2)
    f = fake([urllib.error.URLError(ConnectionRefusedError())], [ok])
    c = sc.probe_target(T_CHAT, f, sleep=lambda s: None, log=None)
    check("unreachable x3 -> down, no exception", c.status == "down" and c.attempts == 3)

    print("--- both instances, concurrently ---")
    barrier = threading.Barrier(2, timeout=5)
    base = fake([ok], [ok])

    def rendezvous(url, timeout):
        if "/search?" in url:
            barrier.wait()   # raises BrokenBarrierError unless both probes are in flight at once
        return base(url, timeout)
    rs = sc.probe_all(sc.targets({}), fetch=rendezvous, sleep=lambda s: None, log=None)
    check("both probes were in flight at the same time",
          [r.status for r in rs] == ["ok", "ok"], [(r.status, r.detail) for r in rs])
    check("...results come back in target order",
          [r.key for r in rs] == ["search_chat", "search_hermes"], [r.key for r in rs])

    def boom(s):
        raise RuntimeError("clock broke")
    rs = sc.probe_all(sc.targets({}), fetch=fake([down], [ok]), sleep=boom, log=None)
    check("a canary crash becomes a DOWN that says so, not a silent exit",
          rs[0].status == "down" and "canary itself failed" in rs[0].detail
          and rs[1].status == "ok", [(r.status, r.detail) for r in rs])

    gate = threading.Event()

    def hang(url, timeout):
        if "/search?" in url and url.startswith(HERMES):
            gate.wait(5)
        return base(url, timeout)
    import time as _time
    t0 = _time.monotonic()
    try:
        rs = sc.probe_all(sc.targets({}), fetch=hang, sleep=lambda s: None, log=None, budget_s=0.3)
    finally:
        gate.set()
    took = _time.monotonic() - t0
    check("a probe hung past the budget: DOWN 'did not finish', and the run moves on",
          rs[1].status == "down" and "did not finish within 0.3 s" in rs[1].detail
          and rs[0].status == "ok" and took < 2, (rs[1].detail, took))

    worst = sc.CONFIG_TIMEOUT + sc.ATTEMPTS * sc.SEARCH_TIMEOUT + \
        (sc.ATTEMPTS - 1) * sc.RETRY_DELAY_S
    check(f"RUN_BUDGET_S={sc.RUN_BUDGET_S} never cuts a failing run short (worst {worst} s)",
          sc.RUN_BUDGET_S >= worst)
    try:
        unit = open(UNIT).read()
        m = re.search(r"^TimeoutStartSec=(\d+)\s*$", unit, re.M)
        limit = int(m.group(1)) if m else 0
        check(f"unit TimeoutStartSec={limit} covers one instance's worst case ({worst} s) "
              f"with a minute to spare", limit >= worst + 60, limit)
        check("...but NOT two instances run back to back, which is why they run concurrently",
              2 * worst > limit, (2 * worst, limit))
        check("...and RUN_BUDGET_S leaves over a minute of it to send and save",
              limit - sc.RUN_BUDGET_S >= 60 and limit == sc.TIMEOUT_START_S,
              (limit, sc.RUN_BUDGET_S, sc.TIMEOUT_START_S))
    except OSError:
        print(f"  [SKIP] {UNIT} not readable")

    print("--- env overrides ---")
    check("defaults", [t.url for t in sc.targets({})] == [CHAT, HERMES])
    t = sc.targets({"SEARXNG_URL": "http://10.0.0.5:9999/",
                    "SEARXNG_HERMES_URL": "http://10.0.0.6:7777"})
    check("SEARXNG_URL moves chat, SEARXNG_HERMES_URL moves the monitor instance",
          [x.url for x in t] == ["http://10.0.0.5:9999", "http://10.0.0.6:7777"],
          [x.url for x in t])
    check("empty override -> default", [x.url for x in sc.targets(
        {"SEARXNG_URL": "", "SEARXNG_HERMES_URL": ""})] == [CHAT, HERMES])
    saved = {k: os.environ.get(k) for k in ("SEARXNG_URL", "SEARXNG_HERMES_URL")}
    try:
        os.environ["SEARXNG_URL"] = "http://chat.test:1"
        os.environ.pop("SEARXNG_HERMES_URL", None)
        check("targets() with no argument reads the process environment",
              [x.url for x in sc.targets()] == ["http://chat.test:1", HERMES])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    with tempfile.TemporaryDirectory() as td:
        env = {"SEARXNG_URL": "http://10.0.0.5:9999", "SEARXNG_HERMES_URL": "http://10.0.0.6:7777"}
        f = Fake({env["SEARXNG_URL"]: [ok], env["SEARXNG_HERMES_URL"]: [ok]})
        rc, lines = canary(["--dry-run"], f, T, os.path.join(td, "s.json"), env=env)
        hosts = {urllib.parse.urlparse(u).netloc for u, _ in f.calls}
        check("main() probes the overridden endpoints and nothing else",
              hosts == {"10.0.0.5:9999", "10.0.0.6:7777"}, hosts)

    print("--- cross-run confirmation (fake clock, temp state) ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "search_canary_state.json")
        snd = Sender()
        chat_down = fake([ans(0, dead=[("bing", "ConnectTimeout"), ("duckduckgo", "CAPTCHA")])],
                         [ok])
        rc, lines = canary([], chat_down, T, sp, snd)
        st = json.load(open(sp))
        check("run 1 down: nothing sent", snd.calls == [] and rc == 0, snd.calls)
        check("...state records both checks", set(st["checks"]) == {"search_chat",
                                                                    "search_hermes"}, st)
        check("...one non-ok run, three failed probes (the retries count)",
              st["checks"]["search_chat"]["fail_streak"] == 1
              and st["checks"]["search_chat"]["failed_probes"] == 3, st["checks"]["search_chat"])
        verdicts = [l for l in lines if re.search(r"\] (DOWN|DEGRADED|OK)", l)]
        check("...one journal verdict per check, in the grep the history fixture was cut with",
              len(verdicts) == 2 and "(after 3 attempts)" in verdicts[0], verdicts)

        rc, lines = canary([], chat_down, T + RUN, sp, snd)
        check("run 2 still down: ONE notification", len(snd.calls) == 1 and rc == 0,
              len(snd.calls))
        n = snd.calls[0]
        check("...texts and emails (DOWN)", n["channels"] == ["sms", "email"], n["channels"])
        check("...SMS leads with check, verdict and measurement",
              n["sms"].startswith("Web search (chat) DOWN: 0 results for 'wikipedia'"), n["sms"])
        check("...subject keeps the [stack] prefix mail filters match",
              n["subject"].startswith("[stack] Web search (chat) DOWN"), n["subject"])
        check("...HTML rendered, and it shows the passing monitor check too",
              n["html"] and "Monitor search (background jobs)" in n["html"])
        check("...footer counts what it took: 6 failed probes across 2 runs",
              "Sent after 6 failed probes across 2 runs over 30 minutes" in n["plain"], n["plain"])
        check("...engine rows with reasons in the email",
              "ConnectTimeout" in n["plain"] and "CAPTCHA" in n["plain"])
        check("...handle ohmz", n["handle"] == "ohmz")

        canary([], chat_down, T + 2 * RUN, sp, snd)
        check("run 3 still down: silent", len(snd.calls) == 1, len(snd.calls))
        rc, lines = canary(["--dry-run"], fake([ok], [ok]), T + 3 * RUN, sp)
        check("--dry-run on the first ok run explains the wait for a second",
              any("healthy for 1 of the 2 runs in a row needed" in l for l in lines), lines)
        canary([], fake([ok], [ok]), T + 3 * RUN, sp, snd)
        check("run 4 ok: not yet a recovery (one ok probe used to be enough)", len(snd.calls) == 1)
        canary([], fake([ok], [ok]), T + 4 * RUN, sp, snd)
        check("run 5 ok: one RECOVERED", len(snd.calls) == 2
              and "RECOVERED" in snd.calls[1]["sms"], snd.calls[-1]["sms"])
        check("...a recovery from DOWN texts too", snd.calls[1]["channels"] == ["sms", "email"])

        chat_degraded = fake([ans(10, dead=[("bing", "timeout"), ("duckduckgo", "CAPTCHA"),
                                            ("mojeek", "too many requests")],
                                  engines=("wikipedia",))], [ok])
        canary([], chat_degraded, T + 5 * RUN, sp, snd)
        check("chat degraded once: silent", len(snd.calls) == 2)
        canary([], chat_degraded, T + 6 * RUN, sp, snd)
        check("chat degraded twice: one notification", len(snd.calls) == 3)
        check("...DEGRADED goes by email only", snd.calls[2]["channels"] == ["email"],
              snd.calls[2]["channels"])
        check("...about the chat instance",
              snd.calls[2]["sms"].startswith("Web search (chat) DEGRADED"), snd.calls[2]["sms"])

        both = fake([urllib.error.URLError(ConnectionRefusedError())],
                    [urllib.error.URLError(ConnectionRefusedError())], with_config=False)
        canary([], fake([ok], [ok]), T + 7 * RUN, sp, snd)            # chat recovers
        canary([], fake([ok], [ok]), T + 8 * RUN, sp, snd)
        n_before = len(snd.calls)
        canary([], both, T + 9 * RUN, sp, snd)
        canary([], both, T + 10 * RUN, sp, snd)
        check("both instances down together: ONE notification for both",
              len(snd.calls) == n_before + 1, len(snd.calls) - n_before)
        check("...titled '2 checks'", "2 checks" in snd.calls[-1]["plain"], snd.calls[-1]["plain"])

    print("--- a failed send stays owed ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        bad = Sender(ok=False)
        chat_down = fake([ans(0)], [ok])
        canary([], chat_down, T, sp, bad)
        rc, _ = canary([], chat_down, T + RUN, sp, bad)
        check("send failed -> exit 1", rc == 1 and len(bad.calls) == 1, rc)
        check("...not recorded as alerted", not json.load(open(sp))["checks"]["search_chat"]
              ["alerted"])
        good = Sender()
        canary([], chat_down, T + 2 * RUN, sp, good)
        check("...so the next run sends it", len(good.calls) == 1
              and "DOWN" in good.calls[0]["sms"])

    print("--- the canary never goes silent over its own state or a second copy of itself ---")
    with tempfile.TemporaryDirectory() as td:
        ro = os.path.join(td, "ro")
        os.makedirs(ro)
        sp = os.path.join(ro, "s.json")
        snd = Sender()
        chat_down = fake([urllib.error.URLError(ConnectionRefusedError())], [ok])
        canary([], fake([ok], [ok]), T, sp, snd)
        os.chmod(ro, 0o500)                     # a stand-in for ENOSPC / a read-only remount
        try:
            rcs = [canary([], chat_down, T + i * RUN, sp, snd)[0] for i in (1, 2, 3)]
            check("state dir unwritable: the tmpfs fallback keeps the streak, DOWN on run 2",
                  len(snd.calls) == 1 and "DOWN" in snd.calls[0]["sms"] and rcs == [0, 0, 0],
                  (rcs, [c["sms"] for c in snd.calls]))
            real_fb, ha.FALLBACK_DIR = ha.FALLBACK_DIR, "/proc/no/such/dir"
            try:
                rcs = [canary([], chat_down, T + i * RUN, sp, snd)[0] for i in (4, 5)]
            finally:
                ha.FALLBACK_DIR = real_fb
            check("neither writable: exit 1 every run (it used to exit 0 through six DOWN runs)",
                  rcs == [1, 1], rcs)
        finally:
            os.chmod(ro, 0o755)
        sp = os.path.join(td, "locked.json")
        snd = Sender()
        with ha.exclusive(sp):
            f = fake([ans(0)], [ok])
            rc, lines = canary([], f, T, sp, snd)
        check("a run while another holds the lock: exit 0, no probe, no send, says why",
              rc == 0 and f.calls == [] and snd.calls == [] and any(
                  "another run is in progress" in l for l in lines), lines)
        sp = os.path.join(td, "killed.json")
        kw = dict(label=sc.RUNS_LABEL, unit=sc.UNIT, timeout_s=sc.TIMEOUT_START_S,
                  max_gap_s=sc.MAX_GAP_S, boot_id="test-boot", sender=snd, log=None)
        ha.begin(sc.SOURCE, sp, now=T, **kw)            # two runs systemd killed
        ha.begin(sc.SOURCE, sp, now=T + RUN, **kw)
        rc, lines = canary([], fake([ok], [ok]), T + 2 * RUN, sp, snd)
        check("two killed runs, then main(): 'Search canary runs DOWN' by text and email",
              [c["sms"] for c in snd.calls] == ["Search canary runs DOWN: 2 runs in a row killed "
                                                "by systemd"]
              and snd.calls[0]["channels"] == ["sms", "email"], [c["sms"] for c in snd.calls])
        check("...quoting TimeoutStartSec=300", "TimeoutStartSec=300" in snd.calls[0]["plain"])
        canary([], fake([ok], [ok]), T + 3 * RUN, sp, snd)
        check("...recovered by the second finished run",
              len(snd.calls) == 2 and snd.calls[1]["sms"].startswith("Search canary runs RECOVERED"),
              [c["sms"] for c in snd.calls])
        sp = os.path.join(td, "reboot.json")
        snd = Sender()
        down = fake([ans(0)], [ok])
        canary([], down, T, sp, snd)
        sc.main([], fetch=down, sleep=lambda s: None, sender=snd, now=T + 5040, state_path=sp,
                env={}, host="testhost", log=None, boot_id="after-reboot")
        check("a failure before a reboot plus one after it is not an outage (09-24 14:43/16:07)",
              snd.calls == [], [c["sms"] for c in snd.calls])

    print("--- the old state file migrates ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "search_canary_state.json")
        with open(sp, "w") as fh:
            json.dump({"status": "down", "detail": "query 'wikipedia' returned 0 results (need 3); "
                       "engines down: bing, duckduckgo", "at": int(T)}, fh)
        snd = Sender()
        canary([], fake([ok], [ok]), T + RUN, sp, snd)
        canary([], fake([ok], [ok]), T + 2 * RUN, sp, snd)
        st = json.load(open(sp))
        check("an old DOWN (already texted by the old code) gets its RECOVERED",
              len(snd.calls) == 1 and snd.calls[0]["sms"].startswith("Web search (chat) RECOVERED"),
              [c["sms"] for c in snd.calls])
        check("...filed under the chat check; state rewritten as v2 with both checks",
              st.get("version") == 2 and set(st["checks"]) == {"search_chat", "search_hermes"},
              st)
        with open(sp, "w") as fh:
            json.dump({"status": "ok", "detail": "10 results", "at": int(T)}, fh)
        snd = Sender()
        canary([], fake([ok], [ok]), T + RUN, sp, snd)
        check("an old OK migrates silently", snd.calls == []
              and json.load(open(sp)).get("version") == 2)
        with open(sp, "w") as fh:
            fh.write("{not json")
        canary([], fake([ok], [ok]), T + RUN, sp, snd)
        check("a corrupt file is fresh state, not a crash", json.load(open(sp)).get("version") == 2)

    print("--- --dry-run sends nothing and writes nothing ---")
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        chat_down = fake([ans(0, dead=[("bing", "timeout")])], [ok])
        rc, lines = canary(["--dry-run"], chat_down, T, sp)
        out = "\n".join(lines)
        check("no state file: dry run creates none", not os.path.exists(sp) and rc == 0)
        check("...prints the verdicts", "DOWN: Web search (chat)" in out
              and "OK: Monitor search (background jobs)" in out, out)
        check("...and why nothing would be sent", "non-ok run 1 of the 2 needed" in out
              and "nothing would be sent" in out, out)

        snd = Sender()
        canary([], chat_down, T, sp, snd)                   # a real run: streak 1
        before = open(sp).read()
        rc, lines = canary(["--dry-run"], chat_down, T + RUN, sp)   # sender=None: real transport
        out = "\n".join(lines)
        check("dry run at the confirming run: nothing sent (the default transport raises if hit)",
              rc == 0 and snd.calls == [])
        check("...state byte-for-byte unchanged, so the real run still owes the alert",
              open(sp).read() == before)
        check("...says what would go out: channels and SMS",
              "(dry-run, would send via sms+email)" in out
              and "Web search (chat) DOWN: 0 results for 'wikipedia'" in out, out)
        check("...the subject and the plain-text body",
              "(dry-run) subject: [stack] Web search (chat) DOWN" in out
              and "What's wrong" in out, out)
        check("...and that state was not written", "state not written" in out)
        canary([], chat_down, T + RUN, sp, snd)
        check("the real run after it still sends", len(snd.calls) == 1)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

#!/usr/bin/env python3
"""Every command the hermes brief prescribes must get past hermes's own cron guard.

Why this file exists. A scheduled hermes run has nobody present to approve a command, so its
guard (tools.approval.check_all_command_guards, with HERMES_CRON_SESSION=1 and approvals.cron_mode
at its default of deny) refuses anything it flags. On 2026-09-29 the brief told agent-authored jobs
to fetch with `python3 (urllib)` in a terminal command, which in practice means `python3 -c` or a
heredoc. Measured against the real guard on v0.21.4, both are refused ("script execution via -e/-c
flag", "script execution via heredoc"). So is curl to a plain-http remote host, `curl ... | python3`,
and a `>` redirect into ~/.hermes, which the brief's own state rule (rule 4) implied. Before that
rewrite the brief prescribed execute_code, a tool cron runs do not have. Each time the wording was
reasonable and the run it produced could not do what the wording asked.

So this runs the brief's actual command shapes through the actual guard. The shapes come from the
brief text itself (every 4-space-indented line starting with `python3 /` or `curl `), plus the
loopback SearXNG URL rule 3 names and the command _fc_watch_cmd generates for a fare watch. A shape
that stops passing, or a new placeholder the fill table does not know, fails here before a live job
finds out. Known-bad controls are asserted BLOCKED in the same run, so a guard that silently
approves everything cannot turn this file into a vacuous pass.

The guard lives in ~/.hermes/hermes-agent and needs that checkout's venv, so it runs in a
subprocess under ~/.hermes/hermes-agent/venv/bin/python. Without the checkout, or without the
tirith binary (the guard would try to download it), the guard half is skipped and says so. The
regex pins below it always run: they forbid the known-blocked shapes on any prescribed command line.

Also pinned: every vetted-command header (the three brief templates and _fc_watch_cmd) sets the
terminal call's timeout to 300. The job's model otherwise picks it, and it chose 60 s in about 77%
of measured calls, which killed a fare check whose own budget is 240 s.

Usage:  python3 tests/test_brief_guard.py [pipe_path]
"""
import importlib.util, json, os, re, shutil, subprocess, sys

PIPE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/StudioProjects/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_guard", PIPE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HERMES = os.path.expanduser("~/.hermes/hermes-agent")
HERMES_PY = os.path.join(HERMES, "venv", "bin", "python")

results, skipped = [], []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIP] {label}   ({why})")


# Placeholder -> a realistic value. An unknown placeholder fails the test on purpose: a new one
# means a new shape, and someone should decide what a real value for it looks like.
FILL = {
    "<URL>": "https://www.amazon.ca/dp/B0DEXAMPLE1",
    "<short_name>": "rtx5090",
    "<N>": "500",
    "<username>": "ohmz",
    "<kind>": "price_drop",
    "<job name>": "RTX 5090 price watch",
    "<schedule>": "every 6h",
    "<item words>": "rtx 5090",
}

# Shapes the guard refuses in cron context (measured, v0.21.4). None may appear on a command
# line the brief prescribes.
FORBIDDEN = [
    ("inline interpreter code (python -c / python3 -c)", re.compile(r"\bpython3?\s+-c\b")),
    ("shell -c (bash -c / sh -c)", re.compile(r"\b(?:ba)?sh\s+-l?c\b")),
    ("a heredoc (<<)", re.compile(r"<<")),
    ("a download piped into an interpreter", re.compile(r"\|\s*(?:python3?|bash|sh|perl|node)\b")),
    ("curl to a plain-http remote host",
     re.compile(r"\bcurl\b[^|]*\bhttp://(?!127\.0\.0\.1|localhost)")),
    ("a redirect into ~/.hermes", re.compile(r">>?\s*~?/?[^|]*\.hermes/")),
    ("execute_code", re.compile(r"execute_code")),
]


def brief_commands(brief):
    """(label, command) for every prescribed command line, placeholders filled."""
    out = []
    for n, ln in enumerate(brief.splitlines()):
        s = ln.strip()
        if ln.startswith("    ") and (s.startswith("python3 /") or s.startswith("curl ")):
            filled = s
            for k, v in FILL.items():
                filled = filled.replace(k, v)
            out.append((f"brief line {n}: {s[:60]}", filled))
    # Rule 3's search endpoint, as a job would curl it.
    for url in re.findall(r"http://127\.0\.0\.1:\d+/\S+?format=json", brief):
        out.append((f"rule 3 search: {url[:48]}",
                    f"curl -s '{url.replace('...', 'rtx+5090')}' | head -c 2000"))
    return out


def fc_command():
    p = mod.Pipe.__new__(mod.Pipe)
    prompt = p._fc_watch_cmd("YYZ-YVR-2026-10-02-RT-2026-11-02", "fc-yyz-yvr", "ohmz",
                             "Toronto → Vancouver fare watch under $1,000", "every 15m", 1000.0,
                             origin_name="Toronto", dest_name="Vancouver")
    return prompt, prompt.splitlines()[1]


# Runs inside hermes's venv. Reads {"commands": [...], "controls": [...]} on stdin.
GUARD_HELPER = r"""
import json, os, sys
os.environ["HERMES_CRON_SESSION"] = "1"
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
req = json.load(sys.stdin)
from tools import approval, approval_context as ac
out = {"cron_mode": ac._get_cron_approval_mode(), "results": {}}
for c in req["commands"] + req["controls"]:
    r = approval.check_all_command_guards(c, "local")
    out["results"][c] = [bool(r.get("approved")), (r.get("message") or "")[:200]]
try:
    import yaml
    from hermes_constants import get_hermes_home
    cfg = yaml.safe_load(open(os.path.join(str(get_hermes_home()), "config.yaml"))) or {}
    out["cron_toolsets"] = (cfg.get("platform_toolsets") or {}).get("cron")
except Exception as e:
    out["cron_toolsets"] = None
    out["cfg_error"] = str(e)[:120]
print(json.dumps(out))
"""

# Each of these is something the brief TELLS the agent is blocked. Asserting them blocked keeps
# the brief honest in the other direction too, and proves the guard is actually enforcing.
CONTROLS = {
    "python3 -c": "python3 -c 'print(1)'",
    "python -c": "python -c 'print(1)'",
    "heredoc": "python3 - <<'EOF'\nprint(1)\nEOF",
    "bash -c": "bash -c 'echo hi'",
    "curl | python3": "curl -sL https://example.com/ | python3 /tmp/extract.py",
    "curl http:// remote": "curl -s http://example.com/",
    "> into ~/.hermes": "echo 12.99 > ~/.hermes/monitor-state/rtx5090.txt",
    # A page address the guard cannot verify is refused even inside a VETTED command (measured
    # 2026-09-30); the brief says so in rules 3/5d, and _job_defects flags it after creation.
    "curl a bit.ly link": "curl -sL --max-time 60 'https://bit.ly/3XyZabc' | grep -oE "
                          "'[0-9][0-9,]*\\.[0-9]{2}' | head -n 5",
    "price_watch on a bit.ly link": "python3 /home/ohmz/StudioProjects/ai-stack/scripts/price_watch.py --url "
                                    "'https://bit.ly/3XyZabc' --state 'x' --below 50 --alert-to "
                                    "ohmz --kind price_drop --monitor 'x' --schedule 'every 6h'",
}


def url_cmd(url):
    return (f"python3 /home/ohmz/StudioProjects/ai-stack/scripts/price_watch.py --url '{url}' --state 'x' "
            f"--below 50 --alert-to ohmz --kind price_drop --monitor 'x' --schedule 'every 6h'")


# Each host class the pipe's _url_guard_block claims the guard refuses, one example apiece (every
# shortener it lists, individually), plus addresses it must NOT flag. Run through the real guard,
# so the pipe's list cannot claim more than the guard does, nor pass the brief's own example URL.
URL_BLOCKED = [f"https://{h}/3XyZabc" for h in sorted(mod.Pipe._URL_SHORTENERS)] + [
    "https://xn--80ak6aa92e.com/item", "https://93.184.216.34/item", "https://0x5db8d822/item",
    "https://[2606:2800:220:1::248]/item", "https://www.shop.zip/item", "https://www.shop.mov/item",
    "https://www.amazon.ca./dp/B0DEXAMPLE1"]
URL_ALLOWED = [FILL["<URL>"], "https://a.co/d/3xYzAbC", "https://amzn.to/3XyZabc",
               "https://www.bestbuy.ca/en-ca/product/nvidia-geforce-rtx-5090/18931348"]


def guard_available():
    if not os.path.exists(os.path.join(HERMES, "tools", "approval.py")):
        return f"no hermes checkout at {HERMES}"
    if not os.access(HERMES_PY, os.X_OK):
        return f"no hermes venv python at {HERMES_PY}"
    if not (shutil.which("tirith") or os.access(os.path.expanduser("~/.hermes/bin/tirith"), os.X_OK)):
        return "no tirith binary (the guard would try to download one)"
    return None


def main():
    brief = mod.Pipe._HERMES_BRIEF
    cmds = brief_commands(brief)
    fc_prompt, fc_cmd = fc_command()
    cmds.append(("_fc_watch_cmd (fare watch)", fc_cmd))

    print("--- the brief prescribes the shapes this file expects to find ---")
    kinds = [c for _, c in cmds]
    check("the three vetted-script templates are found",
          sum("scripts/price_" in c for c in kinds) == 3, kinds)
    check("rule 5a's curl example is found",
          any(c.startswith("curl -sL") and "https://" in c for c in kinds), kinds)
    check("rule 3's loopback SearXNG endpoint is found", any("127.0.0.1:8889" in c for c in kinds),
          kinds)
    for label, c in cmds:
        left = re.findall(r"<[^<>\s][^<>]*>", c)
        check(f"every placeholder is filled: {label}", not left, repr(left))

    print("--- regex pins: no prescribed command uses a shape the cron guard refuses ---")
    for label, c in cmds:
        bad = [name for name, rx in FORBIDDEN if rx.search(c)]
        check(f"clean: {label}", not bad, f"{bad} in {c!r}")
    check("the brief no longer prescribes fetching with python3/urllib",
          "python3 (urllib)" not in brief and "python3 with urllib" not in brief
          and "plain urllib" not in brief)
    check("...nor execute_code, which cron runs do not have", "execute_code" not in brief)
    check("rule 3 drops the plain-http fallback: a page no vetted script covers is refused",
          "cannot be watched: say so and create nothing" in brief)
    check("rule 4 writes state with the file tools, not a shell redirect",
          "write_file tool" in brief and "read_file tool" in brief)

    print("--- every vetted-command header sets timeout=300 ---")
    heads = [ln for ln in brief.splitlines() if ln.strip().startswith("Run this terminal command")]
    check("three headers in the brief (5d, 5d-ii, 5d-iii)", len(heads) == 3, heads)
    for h in heads:
        check(f"brief header names timeout=300: {h.strip()[:40]}...", "timeout=300" in h, h)
    check("_fc_watch_cmd's header names timeout=300", "timeout=300" in fc_prompt.splitlines()[0],
          fc_prompt.splitlines()[0])
    check("...and 300 exceeds flightclaw_watch's own budget (2 x 30 s RPC + CHECK_TIMEOUT_S)",
          300 > 2 * 30 + 180)
    check("...and the command is still line two, where _job_vetted_argv finds it",
          mod.Pipe._JOB_VETTED_RE.search(fc_prompt.splitlines()[1]) is not None
          and not mod.Pipe._JOB_VETTED_RE.search(fc_prompt.splitlines()[0]))

    print("--- the pipe's URL check agrees with itself before the guard is asked ---")
    for u in URL_BLOCKED:
        check(f"_url_guard_block flags {u}", mod.Pipe._url_guard_block(u) is not None)
    for u in URL_ALLOWED:
        check(f"_url_guard_block passes {u}", mod.Pipe._url_guard_block(u) is None,
              mod.Pipe._url_guard_block(u))
    check("the brief tells the agent a shortened link cannot be scheduled",
          "bit.ly" in brief and "ask the user for the full page address and create nothing" in brief)

    print("--- the real hermes cron guard approves every prescribed shape ---")
    why = guard_available()
    if why:
        skip("guard run", why)
    else:
        proc = subprocess.run([HERMES_PY, "-c", GUARD_HELPER],
                              input=json.dumps({"commands": [c for _, c in cmds]
                                                + [url_cmd(u) for u in URL_ALLOWED],
                                                "controls": list(CONTROLS.values())
                                                + [url_cmd(u) for u in URL_BLOCKED]}),
                              capture_output=True, text=True, timeout=120, cwd=HERMES)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            out = None
        check("the guard helper ran", out is not None,
              (proc.stderr or proc.stdout)[-400:])
        if out:
            check("cron approval mode is deny (else nothing below would mean anything)",
                  out["cron_mode"] == "deny", out["cron_mode"])
            for name, c in CONTROLS.items():
                ok, msg = out["results"][c]
                check(f"control BLOCKED, as the brief says: {name}", not ok, "approved")
            for label, c in cmds:
                ok, msg = out["results"][c]
                check(f"approved in cron: {label}", ok, msg)
            for u in URL_BLOCKED:
                ok, msg = out["results"][url_cmd(u)]
                check(f"the guard refuses what _url_guard_block flags: {u}", not ok, "approved")
            for u in URL_ALLOWED:
                ok, msg = out["results"][url_cmd(u)]
                check(f"...and approves what it passes: {u}", ok, msg)
            ts = out.get("cron_toolsets")
            if ts is None:
                skip("cron toolsets", out.get("cfg_error") or "platform_toolsets.cron not set")
            else:
                check("cron runs have the terminal and file toolsets the brief relies on",
                      "terminal" in ts and "file" in ts, ts)
                check("...and no code_execution, as rule 3 tells the agent",
                      "code_execution" not in ts, ts)

    fails = results.count(False)
    tail = f", {len(skipped)} skipped" if skipped else ""
    print(f"\n{len(results)} checks{tail} — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

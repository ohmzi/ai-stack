#!/usr/bin/env python3
"""A new tracker leaves a confirmation the host can send, on both creation paths.

WHY THIS FILE EXISTS. The container that creates a monitor holds no SMTP/Twilio credentials —
deliberately, per alert_transports.py's publish_profile — so the pipe cannot email or text
anyone itself. It can only append to a shared inbox file and trust scripts/hermes_delivery.py to
pick it up within a minute (pinned separately in tests/test_hermes_delivery.py). What THIS file
pins is the pipe's half: that both ways a monitor gets created — the deterministic flight watch
(_fc_make_watch, which already has structured slots in hand) and the agent-delegated general
watch (_enqueue_subscriptions_for, which only has a job's name/schedule/prompt to work with) —
actually leave a well-formed entry, and that a failure to enqueue never costs the user the
monitor itself, which by the time either path runs already exists in Hermes.

Usage:  python3 tests/test_subscribe_confirm.py [pipe_path]
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile

PIPE = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_subscribe", PIPE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = mod.Pipe

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def drain(agen):
    async def go():
        return "".join([c async for c in agen])
    return asyncio.run(go())


def new_pipe(inbox_path):
    p = P.__new__(P)
    mod.SUBSCRIBE_INBOX_FILE = inbox_path
    p._route_metric = lambda *a, **k: None
    p._metric = lambda **k: None
    return p


def inbox(path):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return []


def main():
    d = tempfile.mkdtemp()
    inbox_path = os.path.join(d, "pending_subscriptions.json")

    print("--- Path A (_job_flag_value): reading a job's own vetted command line ---")
    p = new_pipe(inbox_path)
    cmd = ("Run this terminal command and print its output verbatim as your entire response. "
          "Add nothing.\npython3 /home/ohmz/ai-stack/scripts/price_watch.py --url "
          "'https://amazon.ca/dp/B0DP6D3TRB' --state cat-board --alert-to ohmz --below 50 "
          "--unit '$' --monitor 'cat board watch' --schedule 'every 6h'")
    check("pulls --url", p._job_flag_value(cmd, "--url") == "https://amazon.ca/dp/B0DP6D3TRB")
    check("pulls --below", p._job_flag_value(cmd, "--below") == "50")
    check("pulls a quoted --unit", p._job_flag_value(cmd, "--unit") == "$")
    check("a flag that isn't there is None", p._job_flag_value(cmd, "--route-id") is None)
    check("a bare switch with nothing after it is None",
          p._job_flag_value(cmd + " --dry-run", "--dry-run") is None)
    check("empty prompt is None, not a crash", p._job_flag_value("", "--url") is None)
    check("no prompt at all is None, not a crash", p._job_flag_value(None, "--url") is None)
    check("unbalanced quotes degrade to None, not an exception",
          p._job_flag_value("python3 price_watch.py --url 'broken", "--url") is None)
    check("a line that isn't one of the vetted extractors is ignored",
          p._job_flag_value("echo --url http://evil.example", "--url") is None)
    check("the --flag=value form works too, not just --flag value",
          p._job_flag_value("python3 price_watch.py --url=https://x.example --below=-50 "
                            "--alert-to o", "--below") == "-50")

    print("--- Path A (_job_flag_value): a decoy flag before the real command is not read ---")
    # _job_flag_value shipped first without the same after-the-.py-filename slice
    # _job_stray_args already had, so a bare argv.index(flag) over the WHOLE shlex-split line
    # would return whichever came first — including one that isn't part of the vetted command
    # at all. A prompt containing extra text before the real invocation is not adversarial input
    # on a personal single-admin box, but it IS a plain correctness bug: the confirmation must
    # describe what the JOB watches, never something that merely appears earlier in the prompt.
    decoy = ("note --url http://evil.example/steal ; python3 "
            "/home/ohmz/ai-stack/scripts/price_watch.py --url https://real-site.example "
            "--below 50 --alert-to ohmz")
    check("the real command's --url wins, not the decoy ahead of it",
          p._job_flag_value(decoy, "--url") == "https://real-site.example",
          p._job_flag_value(decoy, "--url"))
    check("_job_stray_args and _job_flag_value now share the same line-scoping",
          p._job_stray_args(decoy) == ([], None))

    print("--- Path A (_enqueue_subscriptions_for): price-rise, stock counts, and target-free ---")
    p = new_pipe(inbox_path)
    rise_cmd = ("Run this terminal command...\npython3 "
               "/home/ohmz/ai-stack/scripts/price_watch.py --url 'https://x.example' "
               "--state x --alert-to ohmz --above 500 --kind price_rise "
               "--monitor 'resale watch' --schedule 'every 1h'")
    stock_cmd = ("Run this terminal command...\npython3 "
                "/home/ohmz/ai-stack/scripts/price_watch.py --url 'https://y.example' "
                "--state y --mode stock --kind inventory --below 3 --alert-to ohmz "
                "--monitor 'ticket count watch' --schedule 'every 15m'")
    p._enqueue_subscriptions_for(
        [{"id": "1111aaaabbbb", "name": "resale watch", "schedule_display": "every 1h",
          "prompt": rise_cmd},
         {"id": "2222ccccdddd", "name": "ticket count watch", "schedule_display": "every 15m",
          "prompt": stock_cmd}], "ohmz")
    got = inbox(inbox_path)
    rise, stock = got[0]["payload"], got[1]["payload"]
    check("a price-RISE watch carries its --above target with op='over', not 'under'",
          rise.get("target") == 500.0 and rise.get("op") == "over", rise)
    check("a --mode stock count is never dressed up as money",
          stock.get("target") == 3.0 and stock.get("unit") == "" and "op" in stock, stock)
    check("...and its op is still meaningful ('under N left')", stock.get("op") == "under", stock)

    print("--- Path A: a job name with embedded whitespace/newlines is collapsed ---")
    p = new_pipe(inbox_path)
    open(inbox_path, "w").write("[]")
    p._enqueue_subscriptions_for(
        [{"id": "3333eeee4444", "name": "weird   job\nname\there", "schedule_display": "daily"}],
        "ohmz")
    got = inbox(inbox_path)
    check("internal whitespace/newlines collapse to single spaces",
          got[0]["payload"]["item"] == "weird job name here", got[0]["payload"])

    print("--- Path A (_enqueue_subscriptions_for): one confirmation per created job ---")
    p = new_pipe(inbox_path)
    open(inbox_path, "w").write("[]")
    jobs = [
        {"id": "aaaa11112222", "name": "cat board watch", "schedule_display": "every 6h",
         "prompt": cmd},
        {"id": "bbbb33334444", "name": "no-details watch", "schedule_display": "every 1h",
         "prompt": "python3 /home/ohmz/ai-stack/scripts/stock_watch.py --url x --monitor y "
                  "--alert-to ohmz --schedule 'every 1h'"},
        {"id": "", "name": "an id-less job should never enqueue"},   # defensive: malformed record
    ]
    p._enqueue_subscriptions_for(jobs, "ohmz")
    got = inbox(inbox_path)
    check("exactly the two well-formed jobs enqueued", len(got) == 2, str(got))
    a, b = got[0]["payload"], got[1]["payload"]
    check("the first carries what its prompt named",
          a["item"] == "cat board watch" and a["schedule"] == "every 6h"
          and a["url"] == "https://amazon.ca/dp/B0DP6D3TRB" and a["target"] == 50.0
          and a["unit"] == "$", a)
    check("the second has no target/url to find, and doesn't fabricate them",
          "target" not in b and "url" not in b and b["item"] == "no-details watch", b)
    check("both are addressed to the handle that asked, on the entry AND inside the payload",
          got[0]["handle"] == "ohmz" and got[0]["payload"]["to"] == "ohmz")
    check("every entry names its job id", {e["job_id"] for e in got}
          == {"aaaa11112222", "bbbb33334444"}, str(got))
    check("kind is always 'subscribed'", all(e["payload"]["kind"] == "subscribed" for e in got))

    print("--- Path B (_fc_make_watch): the structured itinerary reaches the inbox inline ---")
    p = new_pipe(inbox_path)
    open(inbox_path, "w").write("[]")

    async def fc(tool, arguments, timeout=0):
        return "Tracking YYZ-YVR-2026-10-02-RT-2026-11-02: C$338 (F8)\nTarget price: C$1,000"

    def api(method, path, body=None, timeout=10):
        return 200, {"job": {"id": "cccc55556666", "next_run_at": None}}, None

    p._fc_call = fc
    p._hermes_api = api
    p._stamp_owner = lambda *a, **k: 1
    p._contact = lambda h: {}
    p._save_phone = lambda h, e: True
    p._alert_setup_block = lambda h: ""

    async def go():
        s = {"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"],
             "depart": {"kind": "exact", "date": "2026-10-02"},
             "ret": {"kind": "exact", "date": "2026-11-02"}, "target": 1000.0}
        return await p._fc_make_watch(s, None, handle="ohmz")

    reply = asyncio.run(go())
    got = inbox(inbox_path)
    check("the watch's own confirmation enqueued exactly once", len(got) == 1, str(got))
    fp = got[0]["payload"] if got else {}
    check("the route rides along as its DISPLAY name, no prompt-parsing needed",
          "Toronto" in (fp.get("item") or "") and "Vancouver" in (fp.get("item") or ""), fp)
    check("...not the airport code — s['origin']/s['dest'] are [code, name] pairs and the code "
          "half is never what a person reading an email wants to see",
          "YTO" not in fp.get("item", "") and "YVR" not in fp.get("item", ""), fp)
    check("...with the target already IN the name, not repeated as a separate field — a "
          "flight watch's own name always says 'under $N' when a target exists, and restating "
          "it cost the SMS the route itself to a 140-char truncation, live, during development",
          "1,000" in fp.get("item", "") and "target" not in fp, fp)
    check("the itinerary dates ride along too",
          fp.get("depart_found") == "2026-10-02" and fp.get("ret_found") == "2026-11-02", fp)
    check("no 'found on' claim — nothing was searched for, this is what was asked for",
          "source" not in fp, fp)
    check("the chat reply still confirms as before (this is additive, not a replacement)",
          "Watching it" in reply, reply[:200])

    print("--- a one-way flight enqueues without a return date it never had ---")
    p = new_pipe(inbox_path)
    open(inbox_path, "w").write("[]")
    p._fc_call = fc
    p._hermes_api = lambda *a, **k: (200, {"job": {"id": "dddd77778888"}}, None)
    p._stamp_owner = lambda *a, **k: 1
    p._contact = lambda h: {}
    p._save_phone = lambda h, e: True
    p._alert_setup_block = lambda h: ""

    async def go_oneway():
        s = {"origin": ["YTO", "Toronto"], "dest": ["YVR", "Vancouver"], "one_way": True,
             "depart": {"kind": "exact", "date": "2026-10-02"}}
        return await p._fc_make_watch(s, None, handle="ohmz")

    asyncio.run(go_oneway())
    got = inbox(inbox_path)
    fp = got[0]["payload"] if got else {}
    check("depart rides along, ret does not exist to fabricate",
          fp.get("depart_found") == "2026-10-02" and "ret_found" not in fp, fp)

    print("--- a failed enqueue never costs the user the monitor itself ---")
    p = new_pipe("/definitely/not/a/writable/path/pending_subscriptions.json")
    raised = False
    try:
        p._enqueue_subscription("ohmz", "eeee99990000", {"kind": "subscribed", "item": "x"})
    except Exception:
        raised = True
    check("an unwritable inbox path is swallowed, not raised", not raised)

    p2 = new_pipe(inbox_path)
    for bad in (None, ["not", "a", "dict"], "also not a dict", 42):
        raised = False
        try:
            p2._enqueue_subscription("ohmz", "ffff00001111", bad)
        except Exception:
            raised = True
        check(f"a non-dict payload ({bad!r}) is swallowed too, per the docstring's own promise",
              not raised)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

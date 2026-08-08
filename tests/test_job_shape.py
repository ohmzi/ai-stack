#!/usr/bin/env python3
"""Job-shape enforcement: hold an agent-authored cron job to the parts of the brief a machine can check.

WHY THIS FILE EXISTS. _HERMES_BRIEF is instructions to a 20B local model, and on 2026-08-07 it broke
three of them in one job. Two flight jobs reached the scheduler with deliver='origin' — whose only
origin on this host is the api_server, which has no push channel — so every run ended in "Adapter
send failed: API server uses HTTP request/response, not send()". Neither prompt asked for a LOG line,
so hermes_delivery.py correctly refused to read the prose as a measurement and posted
"⚠️ RUN DID NOT FOLLOW THE OUTPUT PROTOCOL" three times. And both prompts ended in the model's own
tool-call framing, stored verbatim:

    ...report the cheapest option regardless.</parameter>
    <parameter=deliver>
    origin

The delegation verifier already catches a job the agent CLAIMED but never created. It had nothing to
say about a job that exists and cannot work. These checks close that gap, and the boundary they hold
is the point: three defects decidable from the stored record with no opinion, and NOT one thing more.
Whether "every 6 hours, forever" is the duration the user asked for is a judgement, and a validator
that guessed would be the fabrication it exists to prevent.

The second half pins the repairs. A repair may only ever be mechanical — one allowed value, a cut at
a marker, a fixed block appended. Two cases must NOT be repaired and are pinned here for that
reason: a vetted-extractor job ("print this command's output verbatim, add nothing") must never have
a protocol block appended, because appending makes the run add something; and a prompt that is
nothing BUT markup is not truncated to a stub, because a scheduled job doing something arbitrary is
worse than one flagged for the user to cancel.

Usage:  python3 tests/test_job_shape.py [pipe_path]
"""
import importlib.util
import sys

PIPE = sys.argv[1] if len(sys.argv) > 1 else "/home/ohmz/ai-stack/pipes/live/auto_assistant.py"
spec = importlib.util.spec_from_file_location("aa_shape", PIPE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
P = mod.Pipe
p = P.__new__(P)

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def codes(job):
    return [d[0] for d in P._job_defects(job)]


# The job as hermes actually stored it on 2026-08-07. Kept verbatim, markup and all: a test written
# from a paraphrase of a live failure stops pinning the live failure.
REAL_PROMPT = (
    "Check flights from Toronto (YYZ) to Astana (NQZ/TSE). Look for departure in October 2026 and "
    "return in November 2026. Find if any round-trip fares are under $2000 CAD/USD/EUR — whatever "
    "the cheapest currency is. Search Google Flights, Skyscanner, and Expedia as well. Report: (1) "
    "the best prices found so far, (2) airlines, layovers, and dates, (3) links to each deal, and "
    "(4) note if anything new dropped under $2000 since your last check. If nothing under $2000 is "
    "available, report the cheapest option regardless.</parameter>\n<parameter=deliver>\norigin"
)
REAL = {"id": "4df0ab5bed14", "name": "Toronto-Astana Flight Tracker",
        "prompt": REAL_PROMPT, "deliver": "origin"}

# What rule 5d-ii is supposed to produce instead. Its whole contract is "add nothing".
VETTED = {"id": "52f821a8d3a2", "name": "YTO-YVR Flight Price Watch ($1000)", "deliver": "local",
          "prompt": ("Run this terminal command and print its output verbatim as your entire "
                     "response. Add nothing.\npython3 /home/ohmz/ai-stack/scripts/price_watch.py "
                     "--url 'https://example.com/x' --state 'yto_yvr' --below 1000 "
                     "--alert-to ohmz --kind fare --monitor 'YTO-YVR' --schedule 'every 15m'")}

# A hand-written job that obeys the brief: bounded prompt, protocol tail, local delivery.
GOOD = {"id": "aaaaaaaaaaaa", "name": "book price", "deliver": "local",
        "prompt": ("Fetch https://books.toscrape.com/ and extract the price of the first book "
                   "inside one execute_code call.\n\nFinish with:\nLOG: <summary>\n"
                   "ALERT(ohmz): <what happened>")}


# ---------------------------------------------------------------- the three defects

print("--- the measured job fails all three checks, in the order the repairs need ---")
check("all three are found", codes(REAL) == ["deliver", "markup", "protocol"], str(codes(REAL)))
check("markup is reported BEFORE protocol (the repair cuts, then appends)",
      codes(REAL).index("markup") < codes(REAL).index("protocol"))
check("the deliver reason names the value it found",
      "`origin`" in P._job_defects(REAL)[0][1])
check("the deliver cost says results reach nobody",
      "nobody" in P._job_defects(REAL)[0][2])

print("--- a well-formed job is silent ---")
check("the vetted-extractor job has no defects", codes(VETTED) == [], str(codes(VETTED)))
check("a hand-written job with a protocol tail has no defects", codes(GOOD) == [], str(codes(GOOD)))

print("--- delivery: exactly 'local', nothing else ---")
for bad in ("origin", "local,ohmz", "local ohmz", "LOCAL", "", None, "discord"):
    j = dict(GOOD, deliver=bad)
    check(f"deliver={bad!r} is a defect", "deliver" in codes(j))
check("an unset deliver says so rather than printing None",
      "(unset)" in P._job_defects(dict(GOOD, deliver=""))[0][1])
check("deliver='local' is the one accepted value", "deliver" not in codes(dict(GOOD, deliver="local")))
check("surrounding whitespace is not a different value",
      "deliver" not in codes(dict(GOOD, deliver="  local ")))

print("--- markup: the family, not one string (the two live jobs closed with different tags) ---")
for frag in ("</parameter>", "<parameter=deliver>", "</prompt>", "<function=run>", "</function>",
             "<invoke name=cronjob>", "<tool_call>", "</tool_call>", "<tool_use>",
             "<parameter=x>", "</invoke>"):
    j = dict(GOOD, prompt=GOOD["prompt"] + frag)
    check(f"{frag} is markup", "markup" in codes(j))

print("--- markup: no false positives on prose that merely mentions markup ---")
for frag in ("compare the <price> field to the saved one",
             "read the <name> tag from the page",
             "the function of this page is to list prices",
             "alert if x < parameter y",
             "use param=deliver in the query string",
             "the price is in a <span class='p'> element",
             "check whether 2 < 3 and report it"):
    j = dict(GOOD, prompt=GOOD["prompt"] + " " + frag)
    check(f"not markup: {frag[:38]!r}", "markup" not in codes(j), str(codes(j)))

print("--- protocol: a LOG instruction, or a vetted script that prints one itself ---")
check("no LOG anywhere is a defect", "protocol" in codes(dict(GOOD, prompt="Fetch the page.")))
check("a LOG: instruction satisfies it",
      "protocol" not in codes(dict(GOOD, prompt="Fetch it.\nLOG: <summary>")))
for script in ("price_watch.py", "price_search.py"):
    j = dict(GOOD, prompt=f"Run this and print its output verbatim.\npython3 /x/{script} --url 'u'")
    check(f"{script} is exempt — it prints the lines itself", "protocol" not in codes(j))
check("a lookalike script name is NOT exempt",
      "protocol" in codes(dict(GOOD, prompt="python3 /x/my_price_watcher.py --url 'u'")))


# ---------------------------------------------------------------- the repairs

print("--- repairing the measured job ---")
patch, fixed, stuck = P._job_patch(REAL, "ohmz")
check("nothing is unrepairable", stuck == [], str([d[0] for d in stuck]))
check("all three are repaired", [d[0] for d in fixed] == ["deliver", "markup", "protocol"],
      str([d[0] for d in fixed]))
check("delivery is set to local", patch.get("deliver") == "local", repr(patch.get("deliver")))
check("the markup is gone", not P._JOB_MARKUP_RE.search(patch["prompt"]))
check("the instruction itself survives the cut",
      "Check flights from Toronto (YYZ) to Astana" in patch["prompt"])
check("the cut lands at the marker, not mid-sentence",
      "report the cheapest option regardless." in patch["prompt"])
check("no stray 'origin' left behind", "\norigin" not in patch["prompt"])
check("a LOG instruction is now present", "LOG:" in patch["prompt"])
check("the ALERT line is addressed to the real handle", "ALERT(ohmz):" in patch["prompt"])
check("the repaired prompt would pass the checks it just failed",
      P._job_defects({"deliver": patch["deliver"], "prompt": patch["prompt"]}) == [])
check("the patch touches ONLY the two fields it fixed", set(patch) == {"deliver", "prompt"},
      str(sorted(patch)))

print("--- a repair is mechanical: it never rewrites what the job does ---")
check("the vetted job gets NO protocol tail appended",
      "prompt" not in P._job_patch(dict(VETTED, deliver="origin"), "ohmz")[0])
check("...and still has its delivery fixed",
      P._job_patch(dict(VETTED, deliver="origin"), "ohmz")[0] == {"deliver": "local"})
check("a clean job produces an empty patch", P._job_patch(GOOD, "ohmz") == ({}, [], []))

print("--- what must NOT be repaired ---")
only_markup = {"id": "bad1", "deliver": "origin", "prompt": "</parameter>\n<parameter=deliver>\norigin"}
patch2, fixed2, stuck2 = P._job_patch(only_markup, "ohmz")
check("a prompt that is only markup is not truncated to a stub", patch2 == {}, str(patch2))
check("...nothing is claimed as fixed", fixed2 == [], str([d[0] for d in fixed2]))
check("...and every defect is reported, delivery included",
      [d[0] for d in stuck2] == ["deliver", "markup", "protocol"], str([d[0] for d in stuck2]))

long_prompt = "Fetch https://example.com/x and extract the price. " * 100      # ~5000 chars
patch3, fixed3, stuck3 = P._job_patch({"id": "bad2", "deliver": "origin", "prompt": long_prompt},
                                      "ohmz")
check("a prompt with no room for the protocol block says so",
      [d[0] for d in stuck3] == ["protocol"], str([d[0] for d in stuck3]))
check("...and the delivery is still fixed", patch3 == {"deliver": "local"}, str(patch3))
check("a PATCH is never sent over hermes's 5000-char limit",
      len(P._job_patch(REAL, "ohmz")[0]["prompt"]) <= P._JOB_PROMPT_MAX)


# ---------------------------------------------------------------- talking to the scheduler

class Stub:
    """Records every _hermes_api call and answers with a canned result."""

    def __init__(self, result=(200, {}, None)):
        self.calls, self.result = [], result

    def __call__(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        return self.result


def enforce(jobs, result=(200, {}, None), uname="ohmz"):
    q = P.__new__(P)
    q._hermes_api = Stub(result)
    q._api_err_text = P._api_err_text
    return q._enforce_job_shape(jobs, uname), q._hermes_api


print("--- a clean creation is silent, and touches nothing ---")
text, api = enforce([GOOD, VETTED])
check("no text", text == "", repr(text[:80]))
check("no PATCH is sent", api.calls == [], str(api.calls))

print("--- a repaired creation says what it changed ---")
text, api = enforce([REAL])
check("exactly one PATCH", len(api.calls) == 1, str(len(api.calls)))
check("...to this job's id", api.calls[0][:2] == ("PATCH", "/api/jobs/4df0ab5bed14"),
      str(api.calls[0][:2]))
check("...carrying both fields", set(api.calls[0][2]) == {"deliver", "prompt"})
check("the reader is told it was repaired", "Repaired job" in text)
check("...which job", "4df0ab5bed14" in text)
check("...and both reasons that changed the job's text",
      all(s in text for s in ("markup", "`LOG:`")), text[:200])
check("nothing is called unfixable", "could not fix" not in text)
check("it appends to a stream rather than starting one", text.startswith("\n\n"))

print("--- delivery is repaired without narrating it (an omitted deliver defaults to origin) ---")
text, api = enforce([dict(GOOD, deliver="origin")])
check("the PATCH is still sent", api.calls and api.calls[0][2] == {"deliver": "local"}, str(api.calls))
check("...and nothing is said about it", text == "", repr(text[:120]))
text, api = enforce([dict(VETTED, deliver="origin")])
check("same for a vetted-extractor job", text == "" and len(api.calls) == 1, repr(text[:120]))
check("a silent repair still says nothing when several jobs need it",
      enforce([dict(GOOD, deliver="origin"), dict(VETTED, deliver="")])[0] == "")
text, _ = enforce([dict(GOOD, deliver="origin")], result=(0, None, "unreachable"))
check("but a delivery repair that FAILED always speaks", "could not fix" in text, text[:120])
check("...and says results reach nobody", "nobody" in text, text[:160])

print("--- a repair that did not land must never read as one that did ---")
for result in ((0, None, "unreachable"), (0, None, "timeout"),
               (404, {"error": "Job not found"}, "http_404"),
               (400, {"error": {"message": "Prompt must be ≤ 5000 characters"}}, "http_400")):
    text, api = enforce([REAL], result=result)
    check(f"{result[2]}: reported as unfixed", "could not fix" in text, text[:120])
    check(f"{result[2]}: NOT reported as repaired", "Repaired job" not in text, text[:120])
    check(f"{result[2]}: names the transport failure", "repair failed" in text, text[:120])
    check(f"{result[2]}: tells the user how to remove it", "cancel 4df0ab5bed14" in text)
text, _ = enforce([REAL], result=(404, {"error": "Job not found"}, "http_404"))
check("hermes's own reason is quoted, not swallowed", "Job not found" in text, text[:200])

print("--- a malformed job and a clean one in the same turn ---")
text, api = enforce([GOOD, REAL, only_markup])
check("one PATCH, for the one repairable job", len(api.calls) == 1, str(len(api.calls)))
check("the repairable one is reported repaired", "Repaired job `4df0ab5bed14`" in text)
check("the unrepairable one is reported unrepairable", "Job `bad1` was created malformed" in text)
check("the clean one is not mentioned at all", "aaaaaaaaaaaa" not in text)

print("--- a job with no id still produces readable text rather than raising ---")
text, _ = enforce([{"deliver": "origin", "prompt": "Fetch it."}])
check("it renders", "Repaired job" in text and "`?`" in text, text[:120])


# ---------------------------------------------------------------- summary
print()
n = len(results)
if all(results):
    print(f"{n} checks — ALL PASS")
    sys.exit(0)
print(f"{n} checks — {results.count(False)} FAILED")
sys.exit(1)

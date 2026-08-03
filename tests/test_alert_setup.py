#!/usr/bin/env python3
"""The alert SETUP path: ask for a number before scheduling, and say exactly what will happen.

Why this file exists. A task that should text you needs a number on file. Without that check the
job is created, runs, fires its condition, and the alert is skipped with "no phone for 'ohmz'" in a
log nobody reads — the user believes they are being watched and hears nothing. That is the same
silent-loss shape as the gateway eating links and the MX-less mail domain, arriving one layer
earlier.

Pinned here:

  * **The gate fires before creation, not after.** Scheduling first and asking later leaves a live
    monitor that texts nobody.
  * **It does not fire on management or follow-ups.** "cancel the price monitor" must never be
    interrupted by a form, and neither must "yes, reenable" mid-conversation.
  * **The parked request survives the turn.** The user asked for something; making them retype it
    after handing over a number is how a request gets lost.
  * **A bare phone number routes correctly.** "514-555-0123" matches no task predicate; without the
    marker check it reaches the chat model, which will happily claim to have saved it.
  * **Normalization agrees with the transports.** The pipe runs in a container and cannot import
    alert_transports, so the E.164 rule is duplicated. A number accepted by one and rejected by the
    other fails silently at 3am. Both implementations are run against the same table here.

Usage:  python3 tests/test_alert_setup.py
"""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name, **env):
    for k, v in env.items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def drain(agen):
    async def go():
        return "".join([c async for c in agen])
    return asyncio.run(go())


def main():
    tmp = tempfile.mkdtemp()
    contacts = os.path.join(tmp, "alert_contacts.json")
    profile = os.path.join(tmp, "alert_profile.json")
    json.dump({"ohmz": {"email": "o@gmail.com"}}, open(contacts, "w"))
    json.dump({"channels": ["sms", "email"], "sms_from": "relay@gmail.com",
               "sms_gateway": "msg.telus.com"}, open(profile, "w"))

    mod = load("/home/ohmz/ai-stack/pipes/live/auto_assistant.py", "aa",
               ALERT_CONTACTS=contacts, ALERT_PROFILE=profile)
    at = load("/home/ohmz/ai-stack/scripts/alert_transports.py", "at")
    p = mod.Pipe()

    print("--- alert intent is detected, ordinary tasks are not gated ---")
    for t in ["monitor the price and text me when it drops below 50",
              "watch this page and notify me if it changes",
              "check the price every 6h and alert me under 50",
              "let me know when it goes on sale",
              "ping me if the server goes down",
              "tell me when the price drops"]:
        check(f"alert intent: {t[:38]!r}", bool(p._WANTS_ALERT.search(t)))
    for t in ["monitor the price every 6 hours",
              "check this page daily and log the result",
              "list my background tasks",
              "write me a python script that texts people"]:
        check(f"NOT an alert request: {t[:38]!r}", not p._WANTS_ALERT.search(t))

    print("--- E.164 rule is identical on both sides of the container boundary ---")
    for raw in ["+15145579764", "5145579764", "(514) 557-9764", "514-557-9764", "15145579764",
                "+44 7700 900123", "123", "not a phone", "", None, "555 12", "1 514 557 9764"]:
        a, b = p._norm_phone(raw), at.normalize_phone(raw)
        check(f"{raw!r}: pipe={a!r} transports={b!r} agree", a == b, f"{a!r} vs {b!r}")

    print("--- the gate: asked for, and only for, a new alerting task with no number ---")
    CID = "chat-phone"
    prompt = p._phone_prompt("ohmz", "watch the price and text me under 50", CID)
    check("prompt names the handle", "`ohmz`" in prompt)
    check("prompt says why it matters", "text nobody" in prompt)
    check("prompt offers an email-only escape", "email only" in prompt)
    check("prompt mentions the fallback address", "o@gmail.com" in prompt)
    # A first-time user has no idea what shape to type, and no idea the text will arrive from an
    # email address — which reads as spam if it turns up unannounced.
    check("shows literal formats that are accepted", "514-555-0123" in prompt and "+1 514" in prompt)
    check("warns about the sender BEFORE the number is handed over",
          "relay@gmail.com" in prompt and prompt.index("relay@gmail.com") > prompt.index("Reply with"))
    check("tells them to save it as a contact", "read as spam" in prompt)
    # The request is parked on the PIPE, not in the message. It used to ride in an HTML comment,
    # which OpenWebUI escapes — users saw the base64 printed under the question.
    check("the prompt itself carries no hidden payload", "<!--" not in prompt, prompt[-80:])
    check("prompt parks the request out of band", bool(p._phone_ask.get(CID)))
    parked = p._pending_phone_request([], CID)
    check("...and it round-trips exactly", parked == "watch the price and text me under 50", parked)

    msgs = [{"role": "assistant", "content": prompt}]
    check("...and it round-trips through the store", parked == "watch the price and text me under 50")
    check("pipe recognises the parked state", p._pending_phone_request(msgs, CID) == parked)
    check("a chat with nothing parked returns None",
          p._pending_phone_request([{"role": "assistant", "content": "hi"}], "other") is None)
    # People answer questions out of order: phone prompt, "wait, how much does a text cost?",
    # answer, and only THEN the number. A single-turn scan had forgotten the parked request by
    # then, so the bare number fell through to the chat model as small talk.
    interposed = [{"role": "assistant", "content": prompt},
                  {"role": "user", "content": "wait — how much does a text cost?"},
                  {"role": "assistant", "content": "Nothing — the carrier gateway is free."}]
    check("parked state survives an interposed turn", p._pending_phone_request(interposed, CID) == parked)
    two_later = interposed + [{"role": "user", "content": "good to know"},
                              {"role": "assistant", "content": "Anything else?"}]
    check("...and legacy history (two turns back) is still read for old chats",
          p._pending_phone_request(two_later, "legacy-chat") is None)
    # Once the number arrives and the task is submitted, the prompt one turn back is SPENT.
    # Reading past the bg-task reply resurrected it: "no thanks" a turn after scheduling matched
    # the decline branch and re-submitted the job — a duplicate the user never asked for.
    consumed = [{"role": "assistant", "content": prompt},
                {"role": "user", "content": "514-555-0123"},
                {"role": "assistant",
                 "content": "✅ Saved. Verified scheduled." + mod.Pipe._BG_MARK}]
    check("a consumed prompt is not resurrected past the bg-task reply",
          p._pending_phone_request(consumed, "legacy-chat") is None)

    print("--- answering with a number saves it and runs the original request ---")
    sent = {}

    def fake_stream(text, uname="user", verify_creation=False, brief=None, scoped=False):
        sent.update(text=text, uname=uname, verify=verify_creation, scoped=scoped)
        async def go():
            yield "[scheduled]"
        return go()
    p._hermes_stream = fake_stream

    out = drain(p._phone_reply("514-555-0123", "ohmz", parked, CID))
    check("confirms the saved number", "✅ Saved" in out and "514-555-0123" in out, out)
    check("the parked request is what got delegated", sent.get("text") == parked, repr(sent))
    check("creation is still verified", sent.get("verify") is True, repr(sent))
    check("...and the ownership scope is carried across the phone turn",
          "scoped" in sent, repr(sent))
    saved = json.load(open(contacts))
    check("number persisted in E.164", saved["ohmz"]["phone"] == "+15145550123", repr(saved))
    check("existing email was not clobbered", saved["ohmz"]["email"] == "o@gmail.com", repr(saved))

    print("--- a junk number is rejected where it was typed, not at 3am ---")
    out = drain(p._say(""))  # warm-up, keeps the helper exercised
    r = p._phone_reply("12345", "ohmz", parked, CID)
    out = drain(r)
    check("explains what is wrong", "doesn't look like a mobile number" in out, out)
    check("the rejection carries no hidden payload either", "<!--" not in out, out[-80:])
    check("...and the request stays parked so the retry still has something to schedule",
          p._pending_phone_request([], CID) == parked)
    check("nothing was scheduled", sent.get("text") == parked, repr(sent))

    print("--- 'email only' proceeds without a number ---")
    sent.clear()
    p._phone_prompt("ohmz", parked, CID)   # re-park: the previous reply consumed it
    out = drain(p._phone_reply("email only", "ohmz", parked, CID))
    check("the task still gets created", sent.get("text") == parked, repr(sent))
    check("no prompt is repeated", "What number" not in out, out)

    print("--- an unrelated reply is NOT swallowed by the phone flow ---")
    check("free text falls through to normal routing",
          p._phone_reply("actually, what is the weather", "ohmz", parked, CID) is None)

    print("--- the confirmation block states the real delivery setup ---")
    block = p._alert_setup_block("ohmz")
    check("shows the number that will be texted", "+1 514-555-0123" in block, block)
    check("shows the address the text arrives from", "relay@gmail.com" in block, block)
    check("shows the email destination", "o@gmail.com" in block, block)
    check("mentions the run log channel", "background-tasks" in block, block)
    # These pin the FACTS the block has to convey, not the sentences it uses. The wording was
    # rewritten once already for being written from the system's side of the screen ("carriers
    # drop any message containing one"), and a test that pins prose makes plain-language edits
    # look like regressions.
    check("sets the no-links expectation", "won't have a link" in block, block)
    check("explains where the link is instead", "email" in block.split("link")[-1], block)
    # The #1 first-week misdiagnosis: a channel line arrives, no text does, and the user concludes
    # alerting is broken when the condition simply was not met.
    check("pre-empts 'I got a channel post but no text'",
          "only get a text when" in block, block)

    json.dump({"nobody": {}}, open(contacts, "w"))
    block = p._alert_setup_block("nobody")
    check("a user with no number is told so, not shown a blank",
          "no number on file" in block, block)

    print("--- email resolves the way delivery resolves it, not just from contacts ---")
    # Live failure: a user whose address is perfectly good was told "no address on file", because
    # this side read only the contacts file while the delivery side falls back to the OpenWebUI
    # user table. It reads as "your email alerts will not work" about a setup that works fine.
    import sqlite3
    db_path = os.path.join(tmp, "webui.db")
    db = sqlite3.connect(db_path)
    db.execute("create table user (id text, name text, email text)")
    db.execute("create table auth (id text, active int)")
    db.execute("insert into user values ('1','omariqbal97','omariqbal97@gmail.com')")
    db.execute("insert into auth values ('1',1)")
    db.commit(); db.close()
    mod.OWUI_DB = db_path
    json.dump({"ohmz": {"email": "override@x.com"}}, open(contacts, "w"))
    check("an account with no contacts entry still resolves",
          p._alert_email("omariqbal97") == "omariqbal97@gmail.com", p._alert_email("omariqbal97"))
    check("a contacts entry still overrides the account address",
          p._alert_email("ohmz") == "override@x.com", p._alert_email("ohmz"))
    check("an unknown handle resolves to nothing", p._alert_email("nobody") is None)
    block = p._alert_setup_block("omariqbal97")
    check("the confirmation shows the resolved address, not 'none'",
          "omariqbal97@gmail.com" in block and "no address on file" not in block, block)
    mod.OWUI_DB = "/nonexistent/webui.db"
    check("an unreadable database degrades to None, never raises",
          p._alert_email("omariqbal97") is None)

    print("--- an UPDATE is verified as success, not reported as a failed creation ---")
    # Live failure: "change my alert to every 5 minutes" rescheduled the job correctly AND said
    # nothing had been created — the loudest possible way to report success. Ground truth, not
    # keywords: a job the scheduler already had, now different.
    before = {"j1": {"schedule_display": "every 6h", "repeat": {"times": None}, "enabled": True,
                     "state": "scheduled"}}
    after = {"j1": {"schedule_display": "every 5m", "repeat": {"times": 4}, "enabled": True,
                    "state": "scheduled"}}
    ch = p._changed_jobs(before, after)
    check("a reschedule is detected", ch and ch[0][0] == "j1" and "rescheduled" in ch[0][1], ch)
    check("...and so is the new run budget", "run count changed" in ch[0][1], ch)
    off = {"j1": dict(after["j1"], enabled=False)}
    check("disabling is detected", "disabled" in p._changed_jobs(after, off)[0][1])
    check("re-enabling is detected", "enabled" in p._changed_jobs(off, after)[0][1])
    # These move on their own every minute; reporting them would claim a change on every turn.
    noise = {"j1": dict(after["j1"], next_run_at="later", last_status="ok",
                        last_run_at="now")}
    check("clock movement is NOT a change", p._changed_jobs(after, noise) == [],
          p._changed_jobs(after, noise))
    check("an identical snapshot is not a change", p._changed_jobs(after, after) == [])
    check("a brand-new job is not an update", p._changed_jobs({}, after) == [])
    check("a vanished job is not an update", p._changed_jobs(after, {}) == [])

    print("--- the brief forbids claiming confirmation it never made ---")
    brief = mod.Pipe._HERMES_BRIEF
    check("tells the agent it cannot open the page", "cannot open the page" in brief)
    check("names the live failure it came from", "AUD" in brief)
    check("stops it narrating skills and future intentions", "Do not mention" in brief)

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())

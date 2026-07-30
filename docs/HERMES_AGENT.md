# Hermes Agent — background tasks for the assistant

_Installed 2026-07-29. "Monitor this price for 2 weeks" typed into OpenWebUI now becomes a real,
bounded, GPU-safe scheduled job, executed by a local agent and reported back into the UI._

## What runs

| Piece | What / where |
|---|---|
| Runtime | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) **v0.19.0**, pinned at commit `b6729ba9`, installed at `~/.hermes` (uv venv, MIT). Do not run `hermes update` casually — upstream merges ~660 PRs between patch releases; re-verify with the tests below after any update. |
| Model | `hermes-genesis:agent` — a second Ollama tag of the SAME weights as the chat model (`ollama create` from `apex-compact` + `PARAMETER num_ctx 65536`; shares blobs, ~0 extra disk). Exists because hermes hard-requires a 64 K context window, and raising the global `OLLAMA_CONTEXT_LENGTH=32768` would tax every OpenWebUI chat turn instead. |
| Service | `hermes-gateway` systemd **user** service (linger enabled). Hosts the cron scheduler and the API server on `127.0.0.1:8642` (key in `~/.hermes/.env`, a copy staged at `/volume1/docker/openwebui/config/hermes_api_key` so the pipe can read it in-container). |
| GPU guard | `~/.hermes/plugins/gpuguard/` — a cron scheduler provider (config `cron.provider: gpuguard`) that defers ticks while ComfyUI's `/queue` shows anything running or pending. Due jobs are never lost, only deferred to the next 60 s tick. Covered by `tests/test_gpuguard.py` (9 checks). **Caveat:** a hand-run `hermes cron tick` bypasses the provider; the gateway path — the only unattended path — is guarded. |
| Delivery | **Deterministic since 2026-07-30**: jobs run NO delivery commands — they end their response with `LOG: <summary>` (always) and `ALERT(<user>): <msg>` (only when the user's condition holds). `scripts/hermes_delivery.py` (user timer, 1 min) parses each new output under `~/.hermes/cron/output/<job>/` and does the delivery itself: LOG → background-tasks channel webhook, ALERT → `send_alert()` → text (carrier email-to-SMS gateway) + email, both proven live 2026-07-30. Recipients validated against `^[a-z0-9_-]+$`, 3 alerts/run cap, per-leg retry (a failed push never re-posts the channel log). Born from two live failures: an agent-authored job that invented `send_webhook_post()` helpers and delivered nothing, then an agent that *claimed* deliveries which never happened. The LLM writes text; infrastructure delivers. Covered by `tests/test_hermes_delivery.py` (9 checks). |
| Entry point | The `auto_assistant` pipe routes background-task intent (`tests/test_bgtask_intent.py`, 30 checks, default-deny) to `POST 127.0.0.1:8642/v1/chat/completions` — an agent runtime, not an LLM proxy. The agent creates/manages its own cron jobs via its `cronjob` tool and streams confirmation back into the same chat. **No second model row in the picker; the single-pipe architecture holds.** |

## Config decisions that are deliberate

- `model.provider: ollama`, `base_url http://127.0.0.1:11434/v1`, `reasoning_effort: low` — on this
  endpoint `reasoning_effort` IS honoured (2 tokens vs 126 on a trivial call, measured); the pipe's
  native `/api/chat` uses `think:false` instead. The two endpoints behave oppositely — see MODELS.md.
- `platform_toolsets.api_server: [web, file, memory, session_search, todo, cronjob, skills]` —
  **no terminal, no browser, no code execution** on the chat-facing surface. Chat-reachable text
  must not be able to shell out; creating a cron job needs none of those.
- `platform_toolsets.cron: [web, terminal, file, memory, todo, cronjob, skills]` — jobs need
  `terminal` for `curl` (fetching pages, SearXNG at `:8888`, and the delivery POST). Hermes runs
  unattended jobs with dangerous-command approval in DENY mode by default; the hardline blocklist
  (fork bombs, filesystem wipes) applies regardless.
- No Nous Portal account, no cloud keys: search is local SearXNG via curl, fetching is curl. The
  hosted-tier tools (Firecrawl search, cloud browser, image gen) are simply absent.

## Using it

In OpenWebUI, just ask: *"monitor the price of X … check every 6 hours for 2 weeks"* /
*"list my background tasks"* / *"cancel the price monitor"* — or prefix with `/task` to force the
route. Results appear in the **background-tasks** channel. From a terminal: `hermes cron list`,
`hermes cron remove <id>`, `journalctl --user -u hermes-gateway -f`.

Proven end-to-end 2026-07-29 with a bounded demo (books.toscrape.com, every 2 m, repeat 2): run 1
posted "£51.77 (first run)", run 2 read the state file and posted "£51.77 (unchanged)", job then
retired itself.

## Personal alerts — Twilio SMS + SMTP email

A job that fires a condition emits `ALERT(<handle>): <message>`; the watcher routes it through
`scripts/alert_transports.py`, which sends **both** a text and an email by default. SMS is the buzz
(real text, real ringtone, no app, no OS notification settings involved); email is the record.
`ALERT_CHANNELS` narrows it to one.

**SMS has two methods** (`SMS_METHOD`): `gateway` (default when `SMS_GATEWAY` is set) emails the
carrier's free email-to-SMS bridge — e.g. Telus `<10-digit>@msg.telus.com` — so a text is just an
email and rides the same SMTP connection at zero cost; `twilio` uses the REST API (paid, and a
**trial** account is blocked from sending custom text — error 572006 — so it needs a real
upgrade). This box uses the gateway.

Addresses come from where they actually live: **email is derived automatically** from the
OpenWebUI user table by matching the handle against each account's email local part — no
configuration, new users work immediately. **Phone numbers are opt-in** per handle in
`~/.hermes/alert_contacts.json`, because OpenWebUI has no phone field and not everyone in a
household wants texts; a handle with no phone entry quietly gets email only.

Secrets live in `~/.hermes/alert_transports.env` (0600, never in git; a `.template` sits beside it).
Gmail needs an **app password**, not the account password. A Twilio **trial** account can only text
numbers verified in its console — the first thing to check if SMS 400s.

Design decisions worth keeping:

- **Partial success is success.** SMS delivered + email failed returns delivered, so the watcher
  never re-sends the text every minute to fix a mail problem. Failures are logged per leg.
- **E.164 or refuse.** Numbers are normalized locally and an unnormalizable one is skipped with a
  log line, rather than handed to Twilio to reject with a 400 nobody reads.
- **Unconfigured is not an error.** Missing config, missing phone, or every channel failing folds
  the alert text into the channel post flagged `⚠️ [alert]` — a fired condition is never lost.

Covered by `tests/test_alert_transports.py` (27 checks: normalization, resolution precedence,
fan-out semantics, channel selection, and the Twilio request shape against the documented API).
Everything verifiable offline is pinned there, so a live failure has exactly one unknown left.

### Why ntfy is gone (2026-07-30)

It was built, multi-user, and every server-side leg verified — per-user accounts provisioned by
bcrypt-hash mirroring from OpenWebUI, deny-all ACLs, the APNs wake relay, and debug logs showing the
phone authenticate and fetch each message **within one second of publish**. Alerts reached the app
every time; iOS refused to render a banner, through re-subscribes, base_url alignment, urgent
priority and a settings audit. The failure lived in a layer this stack cannot instrument. SMS has no
equivalent layer: the carrier either delivers a text or returns an error code.



### Alert delivery is verified, retried, and recorded

Every alert goes through a queue rather than a single fire-and-forget send:

- **Verified** — SMTP `send_message` returns the recipients the server *refused*; a non-empty
  result is treated as a failure, so a "sent" is only logged when the server actually accepted it.
  (Beyond that hop — carrier to handset — nothing is verifiable by anyone; that limit is real.)
- **Retried** — up to `MAX_ATTEMPTS` (4: the first try plus 3 retries) spaced `RETRY_AFTER_S`
  (5 minutes) apart, drained by the same 1-minute watcher tick. Backoff is honoured; a delivered
  alert stops immediately; a failed one is never retried forever.
- **Recorded** — every attempt appends to `~/.hermes/cron/output/alert_ledger.jsonl`
  (append-only, never rewritten) with timestamp, attempt number, recipient, per-channel result and
  the exact error. The live queue is `.alerts.json` beside it.

```bash
python3 scripts/hermes_delivery.py --ledger   # history + what is still pending
```

When an alert exhausts its attempts, a loud notice goes to the background-tasks channel with the
last error — an undeliverable alert is never silent.

### Price monitoring is deterministic code, not a per-run scraper

`scripts/price_watch.py` fetches, extracts, compares against saved state and prints the LOG/ALERT
lines itself. Jobs run it via `--script <name>.py --no-agent`, so **no model runs at all** — there is
no step at which a price can be invented.

It replaced the model-writes-a-scraper approach after that approach failed three separate ways in
production: a crash inside its own regex, an ALERT assigned to a variable it never printed, and
finally a run on a 1.5 MB Amazon page that abandoned the output protocol entirely and emitted
marketing prose containing an **invented promo code** beside a price it had never read. A price check
is arithmetic on a fetched string; giving it a 34B model added a hallucination surface and nothing
else.

Extraction is confidence-ranked and says which rank it used:

| Rank | Source |
|---|---|
| high | JSON-LD `price`, `og:price:amount`, `itemprop="price"`, known per-site containers |
| low | a bare currency match in visible text, or a marketplace-offer price |

A low-confidence reading is reported with its caveat inline; `--require-confidence` refuses to alert
on one at all. `--selector '<regex with one group>'` pins an exact element when a site needs it.

**Amazon serves rotating page variants, and honesty about which one you got is the whole game.**
On the common variant `corePrice_feature_div` ships **empty** — the buy box is written in by
JavaScript — leaving only the "New (N) from $X" marketplace offer, a real number but not the buy-box
price, reported as `amazon-offer-listing` / **low**. On other fetches of the *same URL* Amazon embeds
`"price"` and `"priceAmount"` as JSON, which reads **high**. So `best_candidates()` retries a few
times and takes a high-confidence variant when one appears, rather than reporting whichever variant
luck supplied. Both variants agreed on $46.99; the model that originally "read" this page had
reported $49.99, a number present in neither.

Counter-intuitive and verified twice on this host: amazon.ca serves the full page to a plain urllib
request and a **robot wall to a spoofed Chrome User-Agent**. Do not "fix" the fetch by adding one.

```bash
python3 scripts/price_watch.py --selftest     # runs both live reference pages
```

### The sending account, and the prefix you cannot remove

Every text arrives with the sending address written in front of it:

```
plexlaking@gmail.com Hi ohmz, Ohmz AI here! Zakkart 2-Pack Cat Scratching Board is $46.99 ...
```

That is the **gateway**, not this stack. An email-originated SMS has no sender field, so the carrier
writes the sender into the message body. Tested on Telus 2026-07-30 with four header variants: a
`From` display name is ignored entirely, and an explicit `Sender` header changes nothing. A subject
IS rendered, as `Subj: <subject>` ahead of the body — which is why gateway sends always use an empty
one. **No header removes the prefix.** Only two things do: a paid SMS API (Twilio et al), or not
using SMS.

So the sending address is worth choosing deliberately — it is the assistant's visible identity on
every text. Switching it changes BOTH legs, because the email leg *is* SMTP and a gateway text is an
email to the carrier:

```bash
python3 scripts/alert_transports.py --set-sender you@gmail.com 'your-app-password'
```

The credentials are proven against the live server **before** anything is written, and a failure
leaves the previous working config byte-for-byte intact. A wrong password here does not degrade one
channel — it silences every alert on the box, which is not a thing to discover three days later.

This also sets the SMS character budget. A single GSM-7 segment holds 160 ASCII characters and the
prefix eats ~21 of them, which is why the body is capped at 140 rather than 160. **A longer sending
address costs message length.**

### Texts must not contain links

Carrier email-to-SMS gateways **silently drop messages containing URLs**. There is no bounce, no
error code, and SMTP reports success — the text simply never arrives. Measured 2026-07-30: two price
alerts carrying an amazon.ca link were accepted by Gmail and never delivered, while a link-free test
sent minutes later arrived immediately.

Because the failure is invisible, it cannot be retried into working; it has to be avoided.
`sms_body()` reduces any URL to its bare host and caps the message at one segment. **Email always
carries the full text with the link intact** — which is the division of labour the two channels
already had: SMS is the buzz, email is the record.

### Setup happens before scheduling, not after the first miss

Asking for a task that should text you, with no number on file, used to produce a perfectly
scheduled job that ran, met its condition, and skipped the alert with `no phone for 'ohmz'` in a log
nobody reads. The user believes they are being watched and hears nothing — the same silent-loss
shape as everything else on this page, arriving one layer earlier.

The pipe now checks first. `_WANTS_ALERT` recognises alert intent ("text me", "notify me when",
"let me know if"), and if the handle has no number the request is **parked** rather than scheduled:
the original wording is base64'd into an invisible `<!--bg-need-phone:…-->` marker, so answering
with a number saves it and runs the original request in one turn — no retyping. A bare
`514-555-0123` matches no task predicate and would otherwise reach the chat model, which would
cheerfully claim to have saved it; the marker is what routes it correctly. A junk number is refused
where it was typed rather than silently at send time. `email only` proceeds without one. Managing or
following up on an existing task is never interrupted by the prompt.

On a verified creation the reply states the delivery setup outright — the number, the address the
text will arrive **from** (an email-to-SMS gateway shows as an address, not a number, which reads as
spam unannounced), the email destination, and that only a check meeting the condition texts you.
That last line pre-empts the most common first-week misdiagnosis: a channel post arrives, no text
does, and the user concludes alerting is broken when the condition simply was not met.

Contacts and the display profile live in `/volume1/docker/openwebui/config/alerts/`, the only path
both sides reach — the pipe runs inside the container, the transports on the host. The directory is
owned by the host user, not root: an atomic `tmp+rename` needs write permission on the DIRECTORY,
and publishing the profile from the unprivileged delivery timer silently failed until it had one.

### What an alert actually says

A job emits a **payload** describing what happened; `scripts/alert_templates.py` turns it into the
three surfaces. The job no longer builds sentences, and the transports no longer slice log lines.

```
ALERT(ohmz): Hi ohmz, the listing you're tracking - Zakkart 2-Pack Cat Scratching Board - is $46.99, under your $50.00 target. Link in email.
ALERT_DATA: {"kind":"price_drop","item":"Zakkart 2-Pack Cat Scratching Board","value":46.99,"prev":49.99,"target":50.0,...}
```

**Both lines, deliberately.** The structured one produces the good text and the laid-out email; the
plain one guarantees that a watcher which did not understand it still delivers something. The
watcher drops the plain line for any recipient whose payload it did understand, so nothing arrives
twice.

**The assistant introduces itself.** `Hi ohmz, Ohmz AI here! …` — because these texts arrive from
a mail-to-SMS gateway, so the handset shows an email address the user has no reason to recognise.
Naming the assistant answers the first question a text from an unknown sender raises, which is what
those characters buy. The name is configured (`ASSISTANT_NAME`, default "Ohmz AI") and injected at
delivery, so renaming it never means editing a job. It is deliberately absent from the email
subject: those first ~45 characters are the notification preview, and the sender is already shown
there.

When the 140 characters run out, the personal greeting is dropped **before** the identity — on a
text from an address you don't recognise, "who is this" beats "hello by name".

**The item names itself.** `item` comes from the page's own `<title>`, cut to its first clause and
stripped of the site name — "Zakkart 2-Pack Cat Scratching Board, 65cm Tall Cardboard L Shape
Vertical Cat Scratchers for Indoor Cats" becomes something a person recognises on a lock screen. It
is remembered in the monitor's state, so a later *failure* alert can still say what it was watching.
No model is involved: a generated product name is a fabrication with extra steps.

Kinds: `price_drop` `price_rise` `back_in_stock` `out_of_stock` `fare` `inventory` `availability`
`threshold` `change`, plus the problem kinds `unreachable` `blocked` `no_value` and the closing
`recovered`. An **unknown kind renders generically rather than raising** — a future job type reaches
the user before anyone updates the file.

The kind is chosen by keyword rules on the user's own words ("back in stock", "fare", "under 50"),
and only when the rules cannot tell does the agent pick one at job creation. That is the whole
extent of the model's involvement: it labels a category, once, and never supplies a number.

Decisions worth keeping:

- **The monitor name leads the email; the item name leads the text.** With several jobs running,
  *what* got cheap is the first question.
- **Confidence is printed on every run, including good ones** — if it only appeared on doubtful
  readings, its absence would need interpreting, and an omission would be indistinguishable from a
  bug. The text shows `(unconfirmed)`; the email explains *why* in full, because a caveat on a lock
  screen gets one glance and the measurement must not be pushed into truncation to make room.
- **Degradation has a fixed order.** Over 140 characters: shorten the item name, then drop the
  greeting, and never the pointer to the email — it is the only thing telling a first-time user
  where the link went. Rendering folds to ASCII *before* measuring, because the transport folds on
  the way out and measuring the prettier string let the transport's own truncation eat the pointer.
- **A page title is untrusted input.** It is HTML-escaped into the email; markup in a product name
  cannot inject.
- **The ledger records the body that was SENT**, not the one that was meant.

### When the monitor itself breaks

A monitor that silently stops working is worse than one that never existed, because it is trusted.
Three failure shapes each get their own message and their own advice:

| shape | detected as | told after |
|---|---|---|
| dead URL | 4xx, or a host that no longer resolves | 2 consecutive |
| site blocking checks | a robot wall instead of the page | 2 consecutive |
| transient | 5xx, timeout, reset | 3 consecutive |
| **loads, but no value readable** | page fetched, extractor finds nothing | 3 consecutive |

A single failure never alerts — blips happen, and a system that texts about them gets muted. After
the threshold it alerts **once per outage**, then stays quiet until it recovers; a broken URL texting
every 6 hours for a week trains the user to ignore the channel. Recovery closes the loop, unless the
condition also fired on that run — in which case one message covers both, since the target alert
already proves the monitor works.

Status alone cannot decide this: a dead amazon.ca product returns **HTTP 500**, not 404. So 4xx and
an unresolvable host are permanent; everything else gets more patience.

### What an alert actually says

| surface | shape |
|---|---|
| text | `amazon B0DP6D3TRB price: 46.99, under your 50.00 target (unconfirmed) Link in email.` |
| subject | `amazon B0DP6D3TRB price: 46.99, under your 50.00 target (unconfirmed - read from the offer listing, not the main price)` |
| body | the message with its link intact, monitor, job id, fire time, and where the text went |

Decisions worth keeping:

- **The monitor name leads.** With several jobs running, *which one fired* is the first question, and
  it has to be answered before the reader stops looking.
- **Confidence is printed on every run, including good ones.** If it only appeared on doubtful
  readings, its absence would need interpreting — and an omission would be indistinguishable from a
  bug that stopped emitting it.
- **The source ID is translated.** `amazon-offer-listing` is precise and means nothing to a person
  who will never open the source. The email says "read from the offer listing, not the main price";
  the text says `(unconfirmed)`, because a caveat on a lock screen gets one glance and the
  measurement must not be pushed into truncation to make room for it.
- **Degradation has a fixed order.** Over 140 characters, the monitor name is shortened before the
  measurement, and the pointer to the email is never dropped — it is the only thing telling a
  first-time user where the link went.
- **Everything is folded to ASCII.** A carrier gateway is a mail bridge with no promise of UTF-8; a
  mangled em dash undoes the link-stripping work by making the text look broken anyway.
- **The ledger records the body that was SENT.** It used to record the original alert text, so a
  text mangled in transit looked flawless in the one place an operator would check.

### SMTP acceptance is not deliverability

The relay accepting a message says nothing about whether a mailbox exists. Alerts were addressed to
`ohmz@ohmz.com` — the OpenWebUI login domain, which **has no MX record**. Gmail accepted every one,
discovered there was nowhere to deliver it, and bounced asynchronously to the sending account, where
nothing is watching. The ledger recorded "email sent" every time.

This is the same silent-loss shape as the SMS gateway eating links, and it became load-bearing the
moment texts started dropping URLs and saying "(link in email)": an undeliverable email leg leaves
the user holding a pointer to nothing.

`mail_domain_status()` classifies the domain before sending:

| | meaning | behaviour |
|---|---|---|
| `ok` | has MX records | send normally |
| `implicit` | no MX, but an A record | send, and mark the note **UNVERIFIABLE** — RFC 5321 sends mail to that host, which for a parked domain speaks no SMTP |
| `dead` | no MX, no A | refuse; record the reason instead of spending an "ok" on it |
| `unknown` | no resolver available | send — a missing `dig` must never stop an alert |

Addresses are per-handle in `~/.hermes/alert_contacts.json`, which overrides the OpenWebUI lookup:

```bash
python3 -c "import json,os;p=os.path.expanduser('~/.hermes/alert_contacts.json');print(open(p).read())"
```

### Un-substituted alert templates are never delivered

A model that echoes the protocol example instead of filling it in produces a syntactically perfect
ALERT line. One reached a real phone as
`<what happened, with the number>   (ONLY in a run where the user's alert condition holds)`.
Nothing downstream can distinguish that from a genuine alert, so the watcher drops any alert body
still carrying an angle-bracket placeholder — and says so in the channel log, because a silently
suppressed alert would make a broken job look like a calm one. Arithmetic (`price < 50`) is not
mistaken for a placeholder; `tests/test_hermes_delivery.py` pins both directions.

### Follow-ups in a task conversation

Every hermes reply ends with an invisible `<!--bg-task-->` marker. A short next message ("yes
reenable", "go ahead", "the first one") is routed back to hermes **only when the previous assistant
turn carried that marker** — otherwise the same words are ordinary chat. The prior exchange is sent
along so the answer has a referent, and the agent is told to call `cronjob(action='list')` and work
from real scheduler state.

This exists because of a live failure: the agent asked "re-enable this one, or create a new pair?",
the user answered "yes reenable", the message matched no task predicate, went to the **chat** model
— which read the job id out of the transcript and produced a confident confirmation of something it
had not done and could not do.

## Rollback

```bash
systemctl --user disable --now hermes-gateway     # stop the agent entirely
ollama rm hermes-genesis:agent                    # drop the 64K tag (weights stay via apex-compact)
```
Set `BG_TASKS = False` in the pipe to disconnect the route without touching hermes. Channels off:
`channels.enable=false` in the OWUI config table (webhook rows in `channel`/`channel_webhook` are
inert while disabled; DB backup at `webui.db.bak-channels`). Full uninstall: `hermes uninstall`.
The delivery watcher is its own timer: `systemctl --user disable --now hermes-delivery.timer`.

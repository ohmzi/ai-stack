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
| Delivery | **Deterministic since 2026-07-30**: jobs run NO delivery commands — they end their response with `LOG: <summary>` (always) and `ALERT(alerts-<user>): <msg>` (only when the user's condition holds). `scripts/hermes_delivery.py` (user timer, 1 min) parses each new output under `~/.hermes/cron/output/<job>/` and does the delivery itself: LOG → background-tasks channel webhook, ALERT → `send_alert()` (no transport configured since ntfy's removal — alerts ride the channel post flagged). Recipients validated against `^[a-z0-9_-]+$`, 3 alerts/run cap, per-leg retry (a failed push never re-posts the channel log). Born from two live failures: an agent-authored job that invented `send_webhook_post()` helpers and delivered nothing, then an agent that *claimed* deliveries which never happened. The LLM writes text; infrastructure delivers. Covered by `tests/test_hermes_delivery.py` (9 checks). |
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

## Rollback

```bash
systemctl --user disable --now hermes-gateway     # stop the agent entirely
ollama rm hermes-genesis:agent                    # drop the 64K tag (weights stay via apex-compact)
```
Set `BG_TASKS = False` in the pipe to disconnect the route without touching hermes. Channels off:
`channels.enable=false` in the OWUI config table (webhook rows in `channel`/`channel_webhook` are
inert while disabled; DB backup at `webui.db.bak-channels`). Full uninstall: `hermes uninstall`.
The delivery watcher is its own timer: `systemctl --user disable --now hermes-delivery.timer`.

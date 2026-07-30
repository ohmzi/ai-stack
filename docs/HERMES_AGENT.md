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

## Personal alerts — transport slot, currently empty

Jobs already emit `ALERT(<username>): <message>` when a user's condition fires; the watcher parses
and validates it. What is missing is a transport. **ntfy was built, verified, and removed on
2026-07-30**: every server-side leg was proven working — publish, per-user auth, the APNs wake
relay, and debug logs showing the phone fetching the message within one second of publish — yet iOS
never rendered a banner, only silent list entries. A last hop nobody can own is not a notification
system. SMS and email are being evaluated; whichever wins implements `send_alert()` in
`scripts/hermes_delivery.py` and nothing else changes.

Until then ALERT lines ride the channel post, flagged `⚠️ [alert]`, so a fired condition is visible
rather than lost.

Multi-user identity survives the removal transport-neutrally: `Pipe._alert_username()` derives a
stable handle from the OpenWebUI account (email local part, sanitized), the pipe passes it to
hermes, and job ALERT lines address it. Whatever transport arrives keys on that handle.

## Rollback

```bash
systemctl --user disable --now hermes-gateway     # stop the agent entirely
ollama rm hermes-genesis:agent                    # drop the 64K tag (weights stay via apex-compact)
```
Set `BG_TASKS = False` in the pipe to disconnect the route without touching hermes. Channels off:
`channels.enable=false` in the OWUI config table (webhook rows in `channel`/`channel_webhook` are
inert while disabled; DB backup at `webui.db.bak-channels`). Full uninstall: `hermes uninstall`.
The delivery watcher is its own timer: `systemctl --user disable --now hermes-delivery.timer`.

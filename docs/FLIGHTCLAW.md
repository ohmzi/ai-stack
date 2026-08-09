# FlightClaw: the fare engine

The measurement that unblocked this: `docs/FLIGHT_RECON.md` proved the HTML surface of all 19
flight sites unreadable (walls, or no deep link). FlightClaw reads a **different surface** —
Google Flights' own `GetShoppingResults` protobuf endpoint, via the `fli` library — and on
2026-08-09 it returned real YYZ→YVR fares from this host on the first try:

    C$348 round trip Oct 15 → Nov 12, F8 607 out / AC 112 back, currency auto-detected CAD,
    with a working google.com/travel/flights/booking?tfs= deep link per option.

No API key. The recon's conclusion stands for what it measured (HTML scraping); it simply no
longer decides the fare question.

## Install (as deployed)

    git clone https://github.com/ohmzi/flightclaw ~/flightclaw     # the user's own repo, master
    python3 -m venv ~/flightclaw/venv
    ~/flightclaw/venv/bin/pip install "flights==0.9.0" "mcp[cli]>=1.9,<2" fastmcp pydantic-settings

Two pins are load-bearing, both learned by the service failing to boot:
* `mcp<2` — mcp 2.0.0 renamed `McpError` → `MCPError` and fastmcp 2.14.1 imports the old name.
* `pydantic-settings` — fastmcp needs it and its dependency tree did not pull it in here.

## The always-on service

`~/.config/systemd/user/flightclaw.service` — same pattern as every other stack unit
(user unit + `Linger=yes`, so it starts at boot with no login):

    [Service]
    Environment=HOST=127.0.0.1
    Environment=PORT=8765          # 8000 and 8080 are taken on this host
    WorkingDirectory=/home/ohmz/flightclaw
    ExecStart=/home/ohmz/flightclaw/venv/bin/python /home/ohmz/flightclaw/server.py --http
    Restart=always
    [Install]
    WantedBy=default.target

Plus the standard self-alarm drop-in (`flightclaw.service.d/onfailure.conf` →
`OnFailure=stack-alert@%n.service`) and a `flightclaw` check in `scripts/stack_watchdog.py`
(is-active + HTTP probe of `/mcp`; a 406 to a bare GET is a live MCP server refusing politely).

## The protocol

MCP streamable HTTP at `http://127.0.0.1:8765/mcp`. POST JSON-RPC with
`Accept: application/json, text/event-stream`; `initialize` returns an `mcp-session-id` header
that every later call carries; responses arrive as SSE `data:` lines. 32 tools; the six this
stack uses: `search_flights`, `search_dates`, `track_flight`, `check_prices`, `list_tracked`,
`remove_tracked`. Tracking state lives in `~/flightclaw/data/tracked.json`, written ONLY by the
service — everything else reads it.

The OpenWebUI container runs `network_mode: host`, so the pipe reaches 127.0.0.1:8765 directly.

## Who calls it

* the `auto_assistant` pipe — live search + one-turn tracking in chat
* `scripts/flightclaw_watch.py` — the hermes-cron leg: calls `check_prices`, translates
  FlightClaw's alerts into the `LOG:` / `ALERT()` / `ALERT_DATA:` protocol that
  `hermes_delivery.py` → `alert_transports.py` already turn into SMS + email

## Optional later: Duffel

FlightClaw ships Duffel integration and auto-enables it when a key is configured. A free
test-mode key (instant, app.duffel.com) returns synthetic fares — enough to light the tools up;
real fares need Duffel live approval. Nothing in this stack requires it.

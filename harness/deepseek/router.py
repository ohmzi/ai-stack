#!/usr/bin/env python3
"""
deepseek-router -- one Anthropic-compatible endpoint in front of several backends.

Why this exists
---------------
Claude Code reads ANTHROPIC_BASE_URL once, at launch, so a single session can only
ever talk to one backend. Both upstreams wired up here -- the DeepSeek cloud API and
a local Ollama server -- already speak the Anthropic Messages API natively, so a
pass-through relay in front of them costs nothing but buys one important thing: the
*model name in the request body* decides the upstream. Claude Code's /model command
therefore switches backends mid-session, with no restart.

Routing
-------
Providers are matched in config order; the first whose `models` glob matches the
request's `model` field wins. Specific patterns must precede the "*" catch-all.

Auth
----
`auth: "passthrough"` forwards whatever credentials the client sent (so the API key
never has to be duplicated into this config). `auth: "dummy"` replaces them with a
placeholder, which is what a local Ollama server expects.
"""
from __future__ import annotations

import fnmatch
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get(
    "DEEPSEEK_ROUTER_CONFIG", os.path.expanduser("~/.config/deepseek/router.json")
)

# Describe *this* hop only -- never echoed upstream, never returned downstream.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

# Generous: a local 27B model on a 3090 can legitimately take minutes to first token.
UPSTREAM_TIMEOUT = float(os.environ.get("DEEPSEEK_ROUTER_TIMEOUT", "900"))
CHUNK = 65536


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not cfg.get("providers"):
        raise SystemExit(f"{CONFIG_PATH}: no providers configured")
    return cfg


def select_provider(cfg: dict, model: str | None) -> dict:
    """First provider whose glob list matches `model`; else the configured default."""
    if model:
        for prov in cfg["providers"]:
            for pattern in prov.get("models", []):
                if fnmatch.fnmatchcase(model, pattern):
                    return prov
    default = cfg.get("default_provider")
    for prov in cfg["providers"]:
        if prov["name"] == default:
            return prov
    return cfg["providers"][-1]


def text_of(content) -> str:
    """Flatten an Anthropic content field (string or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def normalize_messages(payload: dict) -> bool:
    """Fold mid-conversation system turns into the top-level system prompt.

    Claude Code injects `role: "system"` messages part-way through a conversation.
    Qwen's chat template -- like most llama.cpp templates -- refuses those:

        Jinja Exception: System message must be at the beginning.

    which Ollama surfaces as an opaque HTTP 500. The Anthropic API has a proper
    home for this text (the top-level `system` field), so hoist it there.
    """
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return False

    hoisted, kept = [], []
    for index, msg in enumerate(msgs):
        if index > 0 and isinstance(msg, dict) and msg.get("role") == "system":
            hoisted.append(text_of(msg.get("content")))
        else:
            kept.append(msg)

    if not hoisted:
        return False

    payload["messages"] = kept
    extra = "\n\n".join(h for h in hoisted if h)
    if extra:
        current = payload.get("system")
        if isinstance(current, str):
            payload["system"] = f"{current}\n\n{extra}"
        elif isinstance(current, list):
            payload["system"] = current + [{"type": "text", "text": extra}]
        else:
            payload["system"] = extra
    return True


def estimate_tokens(payload: dict) -> int:
    """Rough token count for backends with no count_tokens endpoint.

    One token per 4 characters, rounded up -- deliberately landing slightly high.
    Claude Code feeds this into its context accounting: an undercount lets the prompt
    grow past the model's real window, which Ollama answers with a hard 400
    (`exceeds the available context size`). Erring high just costs a slightly earlier
    auto-compact, which is the cheap direction to be wrong in.

    Prose on this model measures ~4.5 chars/token and JSON tool schemas are denser,
    so dividing by 4 is a mild overestimate in both directions' favour.
    """
    chars = 0

    def walk(node) -> None:
        nonlocal chars
        if isinstance(node, str):
            chars += len(node)
        elif isinstance(node, dict):
            for key, val in node.items():
                chars += len(key)
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("messages") or [])
    walk(payload.get("system") or "")
    walk(payload.get("tools") or [])
    # Ceiling division by 4. Prose measures ~4.5 chars/token on this model, and
    # JSON tool schemas denser, so this lands slightly high -- the safe side.
    return max(1, (chars + 3) // 4)


def usable_window(provider: dict) -> int | None:
    """Context window to advertise to the client, leaving room to generate.

    The backend's window covers prompt AND completion, so handing Claude Code the
    raw number means a full prompt leaves nothing for the reply and the request
    400s. Advertise `context_window - output_reserve` instead.
    """
    window = provider.get("context_window")
    if not window:
        return None
    return window - provider.get("output_reserve", 0)


class Router(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deepseek-router/1.0"
    cfg: dict = {}

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - base class API
        log(f"{self.address_string()} {fmt % args}")

    def handle(self) -> None:
        """Framework entry point (no arguments -- see the note on `dispatch`).

        A client closing a keep-alive socket is routine, not an error: Claude Code
        drops idle connections constantly. Without this, socketserver prints a full
        traceback per disconnect, which buries the failures actually worth reading.
        """
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self.dispatch("OPTIONS")

    def do_HEAD(self) -> None:
        # Claude Code warms the connection with a best-effort HEAD /api/hello.
        if urllib.parse.urlsplit(self.path).path.rstrip("/").endswith("/api/hello"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.dispatch("HEAD")

    def read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(blob)

    # -- routing ----------------------------------------------------------------

    # NOTE: deliberately not named `handle` -- BaseHTTPRequestHandler calls
    # self.handle() with no arguments to dispatch a request, and shadowing it
    # makes every single request blow up with a TypeError.
    def dispatch(self, method: str) -> None:
        raw = self.read_body()
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        payload: dict = {}
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}

        # GET /v1/models -- let the client see every routable model name.
        if method == "GET" and path.endswith("/models"):
            self.send_json(200, self.model_listing())
            return

        model = payload.get("model") if isinstance(payload, dict) else None
        provider = select_provider(self.cfg, model)

        # Backends without count_tokens would 404 in a shape Claude Code can't read.
        if path.endswith("/count_tokens") and not provider.get("supports_count_tokens"):
            self.send_json(200, {"input_tokens": estimate_tokens(payload)})
            return

        # Local templates choke on mid-conversation system turns; the cloud API
        # accepts them as-is, so this is opt-in per provider.
        if provider.get("normalize_system") and isinstance(payload, dict):
            if normalize_messages(payload):
                log(f"hoisted mid-conversation system message(s) for {provider['name']}")

        # `inject` FORCES these fields, deliberately overriding whatever the client
        # sent. Claude Code treats model names it doesn't recognize as current
        # models and sends them `thinking: {"type":"adaptive"}`, which a local
        # llama.cpp/Ollama backend answers with a 400. Forcing "disabled" sidesteps
        # that, and also stops reasoning tokens eating the caller's max_tokens.
        for key, val in (provider.get("inject") or {}).items():
            if isinstance(payload, dict):
                payload[key] = val

        body = json.dumps(payload).encode("utf-8") if payload else raw
        max_out = payload.get("max_tokens") if isinstance(payload, dict) else None
        log(f"{method} {path} model={model!r} max_tokens={max_out} -> {provider['name']}")
        self.forward(method, provider, body)

    def model_listing(self) -> dict:
        ids: list[str] = []
        for prov in self.cfg["providers"]:
            for pattern in prov.get("models", []):
                if pattern != "*" and "*" not in pattern and pattern not in ids:
                    ids.append(pattern)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "data": [
                {"id": mid, "type": "model", "display_name": mid, "created_at": now}
                for mid in ids
            ],
            "has_more": False,
            "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
        }

    # -- upstream relay ---------------------------------------------------------

    def forward(self, method: str, provider: dict, body: bytes) -> None:
        parts = urllib.parse.urlsplit(provider["base_url"])
        target = parts.path.rstrip("/") + self.path

        headers = {}
        for key, val in self.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low == "accept-encoding":
                continue  # keep bodies uncompressed so relay stays a byte copy
            headers[key] = val

        if provider.get("auth", "passthrough") == "dummy":
            headers["authorization"] = "Bearer ollama"
            headers.pop("x-api-key", None)
        headers["host"] = parts.netloc

        conn = (
            http.client.HTTPSConnection(parts.netloc, timeout=UPSTREAM_TIMEOUT,
                                        context=ssl.create_default_context())
            if parts.scheme == "https"
            else http.client.HTTPConnection(parts.netloc, timeout=UPSTREAM_TIMEOUT)
        )

        try:
            conn.request(method, target, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # upstream unreachable -- answer in Anthropic's shape
            log(f"upstream {provider['name']} failed: {exc!r}")
            self.send_json(
                502,
                {"type": "error",
                 "error": {"type": "api_error",
                           "message": f"deepseek-router: {provider['name']} unreachable: {exc}"}},
            )
            conn.close()
            return

        try:
            self.relay(resp)
        finally:
            conn.close()

    def relay(self, resp: http.client.HTTPResponse) -> None:
        length = resp.getheader("content-length")
        self.send_response(resp.status)
        for key, val in resp.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, val)
        if length is not None:
            self.send_header("Content-Length", length)
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        if self.command == "HEAD" or resp.status in (204, 304):
            return

        try:
            if length is not None:
                remaining = int(length)
                while remaining > 0:
                    chunk = resp.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            else:
                # read1() hands back whatever has arrived -- essential for SSE, where
                # read() would block until the whole (never-ending) stream completes.
                while True:
                    chunk = resp.read1(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client hung up mid-stream")


def main() -> int:
    if "--route" in sys.argv:
        cfg = load_config()
        for model in sys.argv[sys.argv.index("--route") + 1:]:
            prov = select_provider(cfg, model)
            window = usable_window(prov)
            suffix = ""
            if window:
                suffix = f"  (usable {window} of {prov['context_window']})"
            print(f"{model}\t-> {prov['name']}{suffix}")
        return 0

    if "--window" in sys.argv:
        cfg = load_config()
        for model in sys.argv[sys.argv.index("--window") + 1:]:
            print(usable_window(select_provider(cfg, model)) or "")
        return 0

    cfg = load_config()
    Router.cfg = cfg
    host = cfg["listen"]["host"]
    port = cfg["listen"]["port"]
    server = ThreadingHTTPServer((host, port), Router)
    server.daemon_threads = True
    log(f"listening on http://{host}:{port}  ({len(cfg['providers'])} providers)")
    for prov in cfg["providers"]:
        log(f"  {prov['name']:<12} {prov['base_url']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

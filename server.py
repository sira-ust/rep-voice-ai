#!/usr/bin/env python3
"""
Zero-dependency web UI for an ElevenLabs Agent.

Standard library only -- no pip install, no npm. Serves ./public and proxies the
handful of ElevenLabs REST calls that need your API key, so the key stays on the
server and never reaches the browser.

    python server.py            # http://127.0.0.1:8080
    python server.py --port 9000

Configuration lives in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
API_BASE = "https://api.elevenlabs.io/v1"
UPSTREAM_TIMEOUT = 20

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def call_elevenlabs(path: str, params: dict | None = None) -> dict:
    """GET the ElevenLabs API with the server-side key attached."""
    if not API_KEY:
        raise UpstreamError(
            500,
            "ELEVENLABS_API_KEY is not set. Put it in the .env file next to "
            "server.py, then restart the server.",
        )

    url = API_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    req = urllib.request.Request(
        url,
        headers={
            "xi-api-key": API_KEY,
            "Accept": "application/json",
            "User-Agent": "elevenlabs-webui/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail", parsed)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
        except (ValueError, AttributeError):
            pass
        if exc.code == 401:
            detail = "ElevenLabs rejected the API key (401). " + str(detail)
        raise UpstreamError(exc.code, str(detail)[:500]) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(502, "Could not reach the ElevenLabs API: %s" % (exc.reason,)) from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "ElevenLabsWebUI/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- plumbing ----------

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    # ---------- routing ----------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route.startswith("/api"):
            try:
                self._handle_api(route, query)
            except UpstreamError as exc:
                status = exc.status if 400 <= exc.status < 600 else 502
                self._json(status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001 - never kill the dev server
                self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return

        self._serve_static(parsed.path)

    do_HEAD = do_GET

    def _handle_api(self, route: str, query: dict) -> None:
        agent_id = (query.get("agent_id", [AGENT_ID])[0] or AGENT_ID).strip()

        if route == "/api/config":
            hint = ""
            if len(API_KEY) > 12:
                hint = API_KEY[:4] + "..." + API_KEY[-4:]
            self._json(200, {
                "agentId": AGENT_ID,
                "hasApiKey": bool(API_KEY),
                "apiKeyHint": hint,
            })
            return

        if route == "/api/agents":
            data = call_elevenlabs("/convai/agents", {"page_size": "100"})
            agents = [
                {"agentId": a.get("agent_id"), "name": a.get("name") or a.get("agent_id")}
                for a in data.get("agents", [])
                if a.get("agent_id")
            ]
            self._json(200, {"agents": agents})
            return

        if route == "/api/agent-info":
            if not agent_id:
                raise UpstreamError(400, "No agent selected.")
            data = call_elevenlabs("/convai/agents/" + urllib.parse.quote(agent_id))
            prompt = data.get("conversation_config", {}).get("agent", {}).get("prompt") or {}
            custom = prompt.get("custom_llm") or {}
            self._json(200, {
                "name": data.get("name"),
                "llm": prompt.get("llm"),
                "customLlmUrl": custom.get("url"),
                "customLlmModel": custom.get("model_id"),
            })
            return

        if route == "/api/conversation-token":
            if not agent_id:
                raise UpstreamError(400, "No agent selected. Set ELEVENLABS_AGENT_ID in .env or pick one in the UI.")
            data = call_elevenlabs("/convai/conversation/token", {"agent_id": agent_id})
            token = data.get("token")
            if not token:
                raise UpstreamError(502, "ElevenLabs did not return a conversation token.")
            self._json(200, {"token": token, "conversationId": data.get("conversation_id")})
            return

        if route == "/api/signed-url":
            if not agent_id:
                raise UpstreamError(400, "No agent selected. Set ELEVENLABS_AGENT_ID in .env or pick one in the UI.")
            data = call_elevenlabs("/convai/conversation/get-signed-url", {"agent_id": agent_id})
            signed_url = data.get("signed_url")
            if not signed_url:
                raise UpstreamError(502, "ElevenLabs did not return a signed URL.")
            self._json(200, {"signedUrl": signed_url})
            return

        self._json(404, {"error": "Unknown API route: " + route})

    def _serve_static(self, url_path: str) -> None:
        rel = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
        base = PUBLIC.resolve()
        target = (base / rel).resolve()

        # Refuse anything that escapes ./public
        if base != target and base not in target.parents:
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ElevenLabs agent web UI.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    args = parser.parse_args()

    url = "http://%s:%d" % (args.host, args.port)
    key_state = "loaded from .env" if API_KEY else "MISSING -- add ELEVENLABS_API_KEY to .env"
    print("")
    print("  ElevenLabs Agent Web UI")
    print("  " + url)
    print("  API key : " + key_state)
    print("  Agent   : " + (AGENT_ID or "not set -- pick one in the UI"))
    print("  Ctrl+C to stop")
    print("")

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("  Could not bind %s:%d -- %s" % (args.host, args.port, exc), file=sys.stderr)
        print("  Try: python server.py --port 8081", file=sys.stderr)
        return 1

    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Drive a real ElevenLabs conversation in text mode. Library, not a CLI.

Standard library only. Used by chat_test.py and test_phrasings.py, so the
WebSocket handling and the turn loop exist once rather than in each of them.

Text mode exercises the same agent, prompt, tools and LLM a voice call does --
everything except speech recognition and synthesis. That makes it the fastest
way to tell whether a change to the prompt or a tool actually worked.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request

import auth
import common  # loads .env on import

EL_API = "https://api.elevenlabs.io/v1"
KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def configured() -> bool:
    return bool(KEY and AGENT)


def el_get(path: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        EL_API + path, headers={"xi-api-key": KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class WebSocketClient:
    """The ~80 lines of RFC 6455 needed to hold one conversation."""

    def __init__(self, url: str, timeout: int = 30):
        parts = urllib.parse.urlparse(url)
        host = parts.hostname
        port = parts.port or 443
        path = parts.path + ("?" + parts.query if parts.query else "")

        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)

        nonce = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ) % (path, host, nonce)
        self.sock.sendall(handshake.encode())

        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("closed during handshake")
            self.buf += chunk
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("handshake failed: " + head.decode()[:300])
        self.buf = rest

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str | None:
        while True:
            b0, b1 = self._exact(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            data = self._exact(n)
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(b"\x88\x80" + os.urandom(4))
            self.sock.close()
        except OSError:
            pass


def converse(messages: list[str], quiet: float = 10.0, cap: float = 75.0,
             on_agent=None, on_user=None, rep: str = "") -> dict:
    """Say each message in turn, waiting for the agent to finish between them.

    Turn boundaries come from silence, not from counting replies. A tool-calling
    turn emits two replies several seconds apart -- the pre-tool speech and then
    the answer -- so anything that stops at the first one cuts off the tool call
    with it.

    Returns the conversation id, the agent's opener, and every reply that came
    after a message of ours. Only those later replies prove the LLM ran; the
    opener is static configuration.
    """
    signed = el_get("/convai/conversation/get-signed-url?agent_id=" + urllib.parse.quote(AGENT))
    ws = WebSocketClient(signed["signed_url"], timeout=20)
    ws.send(json.dumps({
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {"conversation": {"text_only": True}},
        # The prompt reads {{rep_name}}, and a referenced variable with nothing
        # behind it fails the session rather than resolving to blank. The web
        # page sends whoever is picked; here "All" is the honest default, and
        # callers who are testing a rep's own numbers say so in the message.
        "dynamic_variables": {"rep_name": rep or "All"},
        # And the signed version of the same thing, which is what the proxy
        # acts on. In direct mode nothing reads it; in proxy mode a request
        # without it is refused, so a text test would fail for a reason that
        # has nothing to do with what it was testing.
        "custom_llm_extra_body": {"scope_token": auth.issue_scope(rep)},
    }))

    result = {"conversation_id": None, "opener": None, "replies": [], "error": None}
    seen_opener = False

    def drain() -> None:
        last = time.time()
        started = time.time()
        while time.time() - last < quiet and time.time() - started < cap:
            try:
                raw = ws.recv()
            except (RuntimeError, socket.timeout) as exc:
                result["error"] = str(exc)
                return
            if raw is None:
                return
            try:
                evt = json.loads(raw)
            except ValueError:
                continue

            kind = evt.get("type")
            if kind == "audio":
                continue
            if kind == "ping":
                ws.send(json.dumps({"type": "pong",
                                    "event_id": evt.get("ping_event", {}).get("event_id")}))
                continue
            if kind == "conversation_initiation_metadata":
                result["conversation_id"] = \
                    evt["conversation_initiation_metadata_event"]["conversation_id"]
                continue
            if kind == "agent_response":
                text = evt.get("agent_response_event", {}).get("agent_response")
                nonlocal seen_opener
                if not seen_opener:
                    seen_opener = True
                    result["opener"] = text
                else:
                    result["replies"].append(text)
                if on_agent:
                    on_agent(text, seen_opener and len(result["replies"]) > 0)
                last = time.time()

    drain()                              # the agent's opener
    for message in messages:
        if on_user:
            on_user(message)
        ws.send(json.dumps({"type": "user_message", "text": message}))
        drain()

    ws.close()
    return result


def _values(parameters) -> list:
    """The argument values from a tool call, whatever shape they arrive in.

    ElevenLabs is not consistent here. A resolved call carries a list of
    {name, type, value} objects, but the model's raw request for the same tool
    can arrive as a bare {"value": ...} object instead, and system tools use
    their own shapes again. Iterating a dict yields its keys, so assuming the
    list form turned a str into the thing being asked for .get -- a crash in
    the test tooling rather than in the agent, but it took the run down with it.
    """
    if isinstance(parameters, dict):
        if "value" in parameters:
            return [parameters["value"]]
        return [v for v in parameters.values() if v is not None]
    out = []
    for item in (parameters or []):
        if isinstance(item, dict):
            if item.get("value") is not None:
                out.append(item["value"])
        elif item is not None:
            out.append(item)
    return out


def tool_calls(conversation_id: str, attempts: int = 8, pause: float = 3.0) -> list:
    """What the model sent to each tool, and how many rows came back.

    The conversation record lands a few seconds after the socket closes, so poll
    rather than read once and wrongly report that no tool was called.
    """
    for attempt in range(attempts):
        try:
            detail = el_get("/convai/conversations/" + conversation_id)
        except Exception:  # noqa: BLE001 - diagnostics only
            return []
        calls = []
        for turn in detail.get("transcript", []):
            for call in (turn.get("tool_calls") or []):
                try:
                    params = json.loads(call.get("params_as_json") or "{}")
                except ValueError:
                    params = {}
                calls.append({
                    "tool": call.get("tool_name"),
                    "values": _values(params.get("parameters")),
                    "rows": 0,
                })
        # Pair each result with its own call. This used to hand every result to
        # the first call still showing zero rows, which silently swapped the
        # numbers around whenever one lookup in a conversation came back empty
        # -- and then the test output blamed the wrong tool.
        pending = {}
        for turn in detail.get("transcript", []):
            for res in (turn.get("tool_results") or []):
                try:
                    value = json.loads(res.get("result_value") or "{}")
                except (ValueError, TypeError):
                    continue
                rows = len(((value.get("result") or {}).get("data_array")) or [])
                pending.setdefault(res.get("tool_name"), []).append(rows)
        for call in calls:
            queue = pending.get(call["tool"])
            if queue:
                call["rows"] = queue.pop(0)
        if calls:
            return calls
        if attempt < attempts - 1:
            time.sleep(pause)
    return []


def outcome(conversation_id: str) -> dict:
    """Status and termination reason, once the record has settled."""
    try:
        detail = el_get("/convai/conversations/" + conversation_id)
    except Exception:  # noqa: BLE001
        return {}
    meta = detail.get("metadata") or {}
    return {
        "status": detail.get("status"),
        "termination_reason": meta.get("termination_reason"),
        "error": meta.get("error"),
    }


if __name__ == "__main__":
    print(__doc__)

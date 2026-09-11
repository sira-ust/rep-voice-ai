#!/usr/bin/env python3
"""
Talk to the agent in text mode -- no microphone, no browser, no dependencies.

Drives the real ElevenLabs conversation WebSocket, so it exercises the same LLM
path a voice call does. The fastest way to tell whether a custom LLM works.

    python chat_test.py                          # send a default message
    python chat_test.py "What do you sell?"      # send your own

Exit code is 0 only if the agent generated a reply *after* your message. The
agent's opening line is static and does not prove the LLM ran, so it is not
counted.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import common  # loads .env on import

ROOT = Path(__file__).resolve().parent


KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def el_get(path: str) -> dict:
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1" + path,
        headers={"xi-api-key": KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


class WebSocketClient:
    """The ~80 lines of RFC 6455 needed to hold one conversation."""

    def __init__(self, url: str, timeout: int = 90):
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


def main() -> int:
    if not KEY or not AGENT:
        print("  ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set in .env")
        return 1

    message = sys.argv[1] if len(sys.argv) > 1 else "What do you help people with?"
    # With a tool-calling turn, wait for two replies: the pre-tool speech and
    # then the answer built from the tool result.
    max_replies = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    signed = el_get("/convai/conversation/get-signed-url?agent_id=" + urllib.parse.quote(AGENT))
    ws = WebSocketClient(signed["signed_url"])
    ws.send(json.dumps({
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {"conversation": {"text_only": True}},
    }))

    conversation_id = None
    replies: list[str] = []
    sent = False

    for _ in range(200):
        try:
            raw = ws.recv()
        except (RuntimeError, socket.timeout) as exc:
            print("  socket: %s" % exc)
            break
        if raw is None:
            break
        try:
            evt = json.loads(raw)
        except ValueError:
            continue

        kind = evt.get("type")
        if kind == "audio":
            continue
        if kind == "ping":
            ws.send(json.dumps({
                "type": "pong",
                "event_id": evt.get("ping_event", {}).get("event_id"),
            }))
            continue
        if kind == "conversation_initiation_metadata":
            conversation_id = evt["conversation_initiation_metadata_event"]["conversation_id"]
            print("  conversation: %s" % conversation_id)
            continue
        if kind == "agent_response":
            text = evt.get("agent_response_event", {}).get("agent_response")
            if not sent:
                print("  agent (opener) : %s" % text)
                print("  you            : %s" % message)
                ws.send(json.dumps({"type": "user_message", "text": message}))
                sent = True
                continue
            print("  agent (LLM)    : %s" % text)
            replies.append(text)
            # A tool call arrives as: pre-tool speech, then the real answer.
            if len(replies) >= max_replies:
                break
            continue

        if kind in ("agent_tool_request", "agent_tool_response",
                    "agent_tool_response_full_payload"):
            payload = json.dumps(evt)
            print("  tool           : %s" % payload[:400])
            continue

    ws.close()

    ok = bool(replies)
    print("\n  RESULT: %s" % ("the custom LLM generated a reply" if ok
                              else "no LLM reply -- generation failed"))

    if conversation_id:
        time.sleep(5)
        try:
            detail = el_get("/convai/conversations/" + conversation_id)
            meta = detail.get("metadata") or {}
            print("  status            : %s" % detail.get("status"))
            print("  termination_reason: %s" % meta.get("termination_reason"))
            if meta.get("error"):
                print("  error             : %s" % json.dumps(meta["error"]))
        except urllib.error.URLError as exc:
            print("  (could not fetch conversation record: %s)" % exc)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

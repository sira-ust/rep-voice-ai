#!/usr/bin/env python3
"""
Hold a multi-turn conversation with the agent and show what it did each turn.

Single-message tests cannot catch context failures. "What about the sauce?" only
misbehaves as the *third* turn of a conversation about pad thai noodles, so this
sends a scripted sequence down one connection.

    python test_conversation.py "Pad Thai noodles" "any other brands?" "what about the sauce?"
    python test_conversation.py --file script.txt      # one line per turn

After each of your turns it prints the agent's reply and, once the conversation
record lands, the search term the model actually sent.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_test import WebSocketClient, el_get, KEY, AGENT  # noqa: E402

DEFAULT_SCRIPT = [
    "I would like to order some stuff.",
    "Pad Thai noodles.",
    "What about other brands? Do we carry other brands?",
    "What about the sauce?",
]


def run(script: list[str], quiet: float = 10.0, cap: float = 75.0) -> str | None:
    """Send each line, then wait for the agent to go quiet before the next.

    Turn boundaries are decided by silence, not by guessing which replies are
    pre-tool speech. A tool-calling turn emits two replies several seconds
    apart, so anything cleverer than "wait until it stops talking" cuts the
    second one off -- and the tool call with it.
    """
    signed = el_get("/convai/conversation/get-signed-url?agent_id=" + urllib.parse.quote(AGENT))
    ws = WebSocketClient(signed["signed_url"], timeout=20)
    ws.send(json.dumps({
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {"conversation": {"text_only": True}},
    }))

    conversation_id = None
    pending = list(script)

    def drain(until_quiet: float) -> None:
        """Read events until the agent has said nothing for `until_quiet` secs."""
        nonlocal conversation_id
        last_event = time.time()
        started = time.time()
        while time.time() - last_event < until_quiet and time.time() - started < cap:
            try:
                raw = ws.recv()
            except (RuntimeError, socket.timeout):
                return
            if raw is None:
                return
            try:
                evt = json.loads(raw)
            except ValueError:
                continue
            kind = evt.get("type")
            if kind == "ping":
                ws.send(json.dumps({"type": "pong",
                                    "event_id": evt.get("ping_event", {}).get("event_id")}))
                continue
            if kind == "audio":
                continue
            if kind == "conversation_initiation_metadata":
                conversation_id = evt["conversation_initiation_metadata_event"]["conversation_id"]
                print("\n  conversation: %s" % conversation_id)
                continue
            if kind == "agent_response":
                print("  agent : %s" % evt.get("agent_response_event", {}).get("agent_response"))
                last_event = time.time()

    drain(quiet)                      # opener
    for line in pending:
        print("\n  you   : %s" % line)
        ws.send(json.dumps({"type": "user_message", "text": line}))
        drain(quiet)

    ws.close()
    return conversation_id


def show_tool_calls(conversation_id: str, attempts: int = 8, pause: float = 3.0) -> None:
    for attempt in range(attempts):
        detail = el_get("/convai/conversations/" + conversation_id)
        calls = []
        for t in detail.get("transcript", []):
            for c in (t.get("tool_calls") or []):
                try:
                    params = json.loads(c.get("params_as_json") or "{}")
                except ValueError:
                    params = {}
                values = [x.get("value") for x in (params.get("parameters") or [])
                          if x.get("value") is not None]
                calls.append((c.get("tool_name"), values))
        if calls:
            print("\n  === what the model searched for ===")
            for name, values in calls:
                print("      %-24s %s" % (name, ", ".join(repr(v) for v in values)))
            print("")
            return
        if attempt < attempts - 1:
            time.sleep(pause)
    print("\n  (no tool calls recorded)\n")


def main() -> int:
    if not KEY or not AGENT:
        print("  ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set in .env")
        return 1

    args = sys.argv[1:]
    if args and args[0] == "--file":
        script = [ln.strip() for ln in Path(args[1]).read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
    else:
        script = args or DEFAULT_SCRIPT

    cid = run(script)
    if cid:
        show_tool_calls(cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

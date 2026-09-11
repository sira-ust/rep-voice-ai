#!/usr/bin/env python3
"""
Run several spoken phrasings through the real agent and report what happened.

The interesting failure is not "the SQL is wrong" -- it is "the model sent the
wrong search term". This shows both, side by side, for a batch of phrasings.

    python test_phrasings.py                       # the built-in set
    python test_phrasings.py "do you have hoisin sauce?" "any fish sauce?"
    python test_phrasings.py --file phrasings.txt  # one phrasing per line

Each line of output shows the phrase you said, the value the model actually
passed to the tool, and how many rows came back.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse the WebSocket client and env loading from the single-shot tester.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_test import WebSocketClient, el_get, KEY, AGENT  # noqa: E402

DEFAULT_PHRASINGS = [
    "Do you have PadThai noodles?",
    "What pad thai noodles do we carry?",
    "Do we have any oyster sauces?",
    "Do you carry coconut milk?",
    "What kind of jasmine rice do we have?",
]


def one_call(message: str, max_replies: int = 3, quiet_after: float = 25.0) -> dict:
    """Hold one text conversation and return what the agent did."""
    signed = el_get("/convai/conversation/get-signed-url?agent_id=" + urllib.parse.quote(AGENT))
    ws = WebSocketClient(signed["signed_url"], timeout=60)
    ws.send(json.dumps({
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {"conversation": {"text_only": True}},
    }))

    out = {"message": message, "conversation_id": None, "replies": [], "error": None}
    sent = False
    started = time.time()

    while time.time() - started < quiet_after:
        try:
            raw = ws.recv()
        except (RuntimeError, socket.timeout) as exc:
            out["error"] = str(exc)
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
            ws.send(json.dumps({"type": "pong",
                                "event_id": evt.get("ping_event", {}).get("event_id")}))
            continue
        if kind == "conversation_initiation_metadata":
            out["conversation_id"] = evt["conversation_initiation_metadata_event"]["conversation_id"]
            continue
        if kind == "agent_response":
            text = evt.get("agent_response_event", {}).get("agent_response")
            if not sent:
                ws.send(json.dumps({"type": "user_message", "text": message}))
                sent = True
                continue
            out["replies"].append(text)
            if len(out["replies"]) >= max_replies:
                break

    ws.close()
    return out


def inspect(conversation_id: str, attempts: int = 8, pause: float = 3.0) -> tuple[list, int]:
    """What the model passed, and how many rows the tool returned.

    The conversation record lands a few seconds after the socket closes, so poll
    until the tool call shows up rather than reading once and reporting a false
    "no tool call".
    """
    sent, rows = [], 0
    for attempt in range(attempts):
        detail = el_get("/convai/conversations/" + conversation_id)
        sent, rows = [], 0
        for turn in detail.get("transcript", []):
            for call in (turn.get("tool_calls") or []):
                try:
                    params = json.loads(call.get("params_as_json") or "{}")
                except ValueError:
                    continue
                for item in (params.get("parameters") or []):
                    if item.get("value") is not None:
                        sent.append(item["value"])
            for result in (turn.get("tool_results") or []):
                try:
                    value = json.loads(result.get("result_value") or "{}")
                except (ValueError, TypeError):
                    continue
                rows += len(((value.get("result") or {}).get("data_array")) or [])
        if sent:
            break
        if attempt < attempts - 1:
            time.sleep(pause)
    return sent, rows


def main() -> int:
    if not KEY or not AGENT:
        print("  ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set in .env")
        return 1

    args = sys.argv[1:]
    if args and args[0] == "--file":
        phrasings = [ln.strip() for ln in Path(args[1]).read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.startswith("#")]
    else:
        phrasings = args or DEFAULT_PHRASINGS

    print("\n  Running %d phrasing(s). Each is a real conversation, so allow ~15s"
          " apiece.\n" % len(phrasings))

    failures = 0
    for phrase in phrasings:
        result = one_call(phrase)
        cid = result["conversation_id"]
        sent, rows = ([], 0)
        if cid:
            try:
                sent, rows = inspect(cid)
            except Exception as exc:  # noqa: BLE001 - diagnostics only
                result["error"] = str(exc)

        ok = rows > 0
        if not ok:
            failures += 1
        print("  %s  %s" % ("PASS" if ok else "MISS", phrase))
        print("        routed to  : %s" % (" -> ".join(sent) or "(no tool call)"))
        print("        rows       : %d" % rows)
        if result["replies"]:
            print("        said       : %s" % result["replies"][-1][:96])
        if result["error"]:
            print("        error      : %s" % result["error"])
        print("")

    print("  %d/%d found something.\n" % (len(phrasings) - failures, len(phrasings)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

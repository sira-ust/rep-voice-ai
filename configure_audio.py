#!/usr/bin/env python3
"""
Tune an ElevenLabs agent's noise handling and turn-taking.

Standard library only. Reads ELEVENLABS_* settings from .env next to this file.

    python configure_audio.py                  # show current settings
    python configure_audio.py --noisy          # preset for a noisy room
    python configure_audio.py --defaults       # back to ElevenLabs defaults
    python configure_audio.py --bvd on         # background voice filtering
    python configure_audio.py --eagerness eager
    python configure_audio.py --turn-timeout 5
    python configure_audio.py --keywords "gemma,vLLM,U.S. Trading"

Why this matters: with heavy background noise the turn detector can believe the
user never stopped speaking. The turn then never ends, so no transcript is
finalised, no LLM turn fires, and the agent stays silent past its turn_timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import common  # loads .env on import

ROOT = Path(__file__).resolve().parent
EL_API = "https://api.elevenlabs.io/v1"
TIMEOUT = 30



EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


class Fail(Exception):
    pass


def elevenlabs(path: str, method: str = "GET", body: dict | None = None) -> dict:
    if not EL_KEY:
        raise Fail("ELEVENLABS_API_KEY is missing from .env")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"xi-api-key": EL_KEY, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(EL_API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise Fail("HTTP %d on %s %s\n    %s" % (exc.code, method, path, detail)) from exc
    except urllib.error.URLError as exc:
        raise Fail("Could not reach the ElevenLabs API: %s" % (exc.reason,)) from exc


def get_agent() -> dict:
    if not AGENT_ID:
        raise Fail("ELEVENLABS_AGENT_ID is missing from .env")
    return elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))


def show(agent: dict | None = None) -> None:
    agent = agent or get_agent()
    cc = agent.get("conversation_config", {})
    vad = cc.get("vad") or {}
    turn = cc.get("turn") or {}
    asr = cc.get("asr") or {}

    print("  agent                     : %s" % agent.get("name"))
    print("  background_voice_detection: %s" % vad.get("background_voice_detection"))
    print("  turn_eagerness            : %s" % turn.get("turn_eagerness"))
    print("  turn_timeout              : %s s" % turn.get("turn_timeout"))
    print("  turn_model                : %s" % turn.get("turn_model"))
    print("  speculative_turn          : %s" % turn.get("speculative_turn"))
    print("  retranscribe_on_timeout   : %s" % turn.get("retranscribe_on_turn_timeout"))
    print("  asr quality / provider    : %s / %s" % (asr.get("quality"), asr.get("provider")))
    print("  asr keywords              : %s" % (asr.get("keywords") or []))


def patch(vad: dict | None = None, turn: dict | None = None, asr: dict | None = None) -> None:
    config: dict = {}
    if vad:
        config["vad"] = vad
    if turn:
        config["turn"] = turn
    if asr:
        config["asr"] = asr
    if not config:
        return
    elevenlabs(
        "/convai/agents/" + urllib.parse.quote(AGENT_ID),
        "PATCH",
        {"conversation_config": config},
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tune agent noise filtering and turn taking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--noisy", action="store_true",
                        help="preset: filter background voices, end turns decisively")
    parser.add_argument("--defaults", action="store_true",
                        help="restore ElevenLabs defaults")
    parser.add_argument("--bvd", choices=["on", "off"],
                        help="background voice filtering")
    parser.add_argument("--eagerness", choices=["patient", "normal", "eager"],
                        help="how readily the agent takes its turn")
    parser.add_argument("--turn-timeout", type=float, metavar="SECS",
                        help="seconds of quiet before the agent re-engages")
    parser.add_argument("--keywords", metavar="LIST",
                        help="comma-separated ASR keyword boosts")
    parser.add_argument("--force", action="store_true",
                        help="allow changing an agent not named *-test")
    args = parser.parse_args()

    # One agent serves every deployment, so a mutating run reaches
    # production unless pointed elsewhere. Reads are left alone.
    if args.noisy or args.defaults or args.bvd or args.eagerness or args.turn_timeout or args.keywords:
        try:
            common.require_test_agent("This", force=args.force)
        except common.Fail as exc:
            print("\n  ERROR: %s\n" % exc, file=sys.stderr)
            return 1

    try:
        print("\nBefore")
        show()

        vad: dict = {}
        turn: dict = {}
        asr: dict = {}

        if args.noisy:
            # Filter other voices, and keep a decisive turn end so constant
            # noise cannot hold the user's turn open indefinitely.
            vad["background_voice_detection"] = True
            turn["turn_eagerness"] = "eager"
            turn["turn_timeout"] = 5.0
            turn["retranscribe_on_turn_timeout"] = True

        if args.defaults:
            vad["background_voice_detection"] = False
            turn["turn_eagerness"] = "normal"
            turn["turn_timeout"] = 7.0
            turn["retranscribe_on_turn_timeout"] = False

        if args.bvd:
            vad["background_voice_detection"] = args.bvd == "on"
        if args.eagerness:
            turn["turn_eagerness"] = args.eagerness
        if args.turn_timeout is not None:
            turn["turn_timeout"] = args.turn_timeout
        if args.keywords is not None:
            asr["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]

        if not (vad or turn or asr):
            print("\n  Nothing to change. Try --noisy, or --help for individual flags.\n")
            return 0

        print("\nApplying")
        for label, block in (("vad", vad), ("turn", turn), ("asr", asr)):
            if block:
                print("  %-5s %s" % (label, json.dumps(block)))
        patch(vad or None, turn or None, asr or None)

        print("\nAfter")
        show()
        print("")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

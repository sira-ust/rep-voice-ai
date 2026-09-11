#!/usr/bin/env python3
"""
Push the agent's system prompt from agent_prompt.md.

Standard library only. Reads ELEVENLABS_* from .env next to this file.

    python configure_prompt.py            # show what the agent has now
    python configure_prompt.py --apply    # upload agent_prompt.md
    python configure_prompt.py --diff     # compare local file to the agent

The prompt is where conversational behaviour lives -- carrying the subject
across turns, how to search, what not to promise. A one-line prompt like
"You are a helpful assistant." leaves all of that to chance.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import common  # loads .env on import

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "agent_prompt.md"
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
        raise Fail("HTTP %d on %s %s\n    %s"
                   % (exc.code, method, path,
                      exc.read().decode("utf-8", "replace")[:500])) from exc
    except urllib.error.URLError as exc:
        raise Fail("Could not reach the ElevenLabs API: %s" % (exc.reason,)) from exc


def get_prompt() -> tuple[dict, str]:
    if not AGENT_ID:
        raise Fail("ELEVENLABS_AGENT_ID is missing from .env")
    agent = elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))
    prompt = (agent.get("conversation_config", {})
              .get("agent", {}).get("prompt", {})) or {}
    return agent, prompt.get("prompt") or ""


def local_prompt() -> str:
    if not PROMPT_FILE.is_file():
        raise Fail("%s not found" % PROMPT_FILE.name)
    return PROMPT_FILE.read_text(encoding="utf-8-sig").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the agent's system prompt.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="upload agent_prompt.md")
    group.add_argument("--diff", action="store_true", help="compare local file to the agent")
    args = parser.parse_args()

    try:
        agent, live = get_prompt()
        print("\n  agent  : %s" % agent.get("name"))
        print("  live   : %d chars" % len(live))
        print("  local  : %s (%d chars)" % (PROMPT_FILE.name, len(local_prompt())))

        if args.diff:
            diff = list(difflib.unified_diff(
                live.splitlines(), local_prompt().splitlines(),
                fromfile="agent (live)", tofile=PROMPT_FILE.name, lineterm=""))
            print("")
            if not diff:
                print("  Identical.")
            for line in diff[:200]:
                print("  " + line)
            print("")
            return 0

        if not args.apply:
            print("")
            if len(live) < 200:
                print("  The live prompt is very short. Conversational behaviour -- carrying")
                print("  the subject across turns, how to search, what not to promise -- is")
                print("  all decided here.")
            print("\n--- live prompt ---")
            print(live or "(empty)")
            print("\n  --diff to compare, --apply to upload %s\n" % PROMPT_FILE.name)
            return 0

        wanted = local_prompt()
        if wanted == live:
            print("\n  Already up to date.\n")
            return 0

        print("\n  Applying %s -> agent" % PROMPT_FILE.name)
        elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                   {"conversation_config": {"agent": {"prompt": {"prompt": wanted}}}})

        _, after = get_prompt()
        if after.strip() != wanted:
            raise Fail("The prompt did not stick (agent now has %d chars)" % len(after))
        print("  live prompt is now %d chars\n" % len(after))
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

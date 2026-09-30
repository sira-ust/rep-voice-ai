#!/usr/bin/env python3
"""
Push the agent's system prompt and greeting from agent_prompt.md and
agent_greeting.txt.

Standard library only. Reads ELEVENLABS_* from .env next to this file.

    python configure_prompt.py            # show what the agent has now
    python configure_prompt.py --apply    # upload both files
    python configure_prompt.py --diff     # compare local files to the agent

The greeting is the first thing every caller hears, so it is the cheapest
place to set expectations about what the agent can actually answer.

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
GREETING_FILE = ROOT / "agent_greeting.txt"
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


def get_prompt() -> tuple[dict, str, str]:
    if not AGENT_ID:
        raise Fail("ELEVENLABS_AGENT_ID is missing from .env")
    agent = elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))
    cfg = (agent.get("conversation_config", {}).get("agent", {})) or {}
    prompt = cfg.get("prompt") or {}
    return agent, prompt.get("prompt") or "", cfg.get("first_message") or ""


def local_prompt() -> str:
    if not PROMPT_FILE.is_file():
        raise Fail("%s not found" % PROMPT_FILE.name)
    return PROMPT_FILE.read_text(encoding="utf-8-sig").strip()


def local_greeting() -> str:
    if not GREETING_FILE.is_file():
        raise Fail("%s not found" % GREETING_FILE.name)
    return GREETING_FILE.read_text(encoding="utf-8-sig").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the agent's system prompt.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="upload agent_prompt.md")
    group.add_argument("--diff", action="store_true", help="compare local file to the agent")
    parser.add_argument("--force", action="store_true",
                        help="allow changing an agent not named *-test")
    args = parser.parse_args()

    # One agent serves every deployment, so a mutating run here reaches
    # production unless it is pointed somewhere else. Reads are left alone.
    if args.apply:
        try:
            common.require_test_agent("This", force=args.force)
        except common.Fail as exc:
            print("\n  ERROR: %s\n" % exc, file=sys.stderr)
            return 1


    try:
        agent, live, live_greeting = get_prompt()
        print("\n  agent    : %s" % agent.get("name"))
        print("  prompt   : live %d chars, local %d chars"
              % (len(live), len(local_prompt())))
        print("  greeting : %s"
              % ("in sync" if live_greeting == local_greeting()
                 else "DIFFERS from " + GREETING_FILE.name))
        print("             %r" % live_greeting)

        if args.diff:
            for live_text, local_text, name in (
                    (live, local_prompt(), PROMPT_FILE.name),
                    (live_greeting, local_greeting(), GREETING_FILE.name)):
                diff = list(difflib.unified_diff(
                    live_text.splitlines(), local_text.splitlines(),
                    fromfile="agent (live)", tofile=name, lineterm=""))
                print("\n  --- %s ---" % name)
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
            print("\n  --diff to compare, --apply to upload both files\n")
            return 0

        wanted, wanted_greeting = local_prompt(), local_greeting()
        if wanted == live and wanted_greeting == live_greeting:
            print("\n  Already up to date.\n")
            return 0

        changes = {}
        if wanted != live:
            changes["prompt"] = {"prompt": wanted}
        if wanted_greeting != live_greeting:
            changes["first_message"] = wanted_greeting

        print("\n  Applying: %s" % ", ".join(sorted(changes)))
        elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                   {"conversation_config": {"agent": changes}})

        # Read back rather than trust the write: a silently dropped field
        # looks exactly like success otherwise.
        _, after, after_greeting = get_prompt()
        if "prompt" in changes and after.strip() != wanted:
            raise Fail("The prompt did not stick (agent now has %d chars)" % len(after))
        if "first_message" in changes and after_greeting.strip() != wanted_greeting:
            raise Fail("The greeting did not stick (agent has %r)" % after_greeting)
        print("  prompt   : %d chars" % len(after))
        print("  greeting : %r\n" % after_greeting)
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

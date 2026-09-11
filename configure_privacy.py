#!/usr/bin/env python3
"""
Turn Zero Retention Mode on or off for the agent.

Standard library only. Reads ELEVENLABS_* from .env next to this file.

    python configure_privacy.py               # show current privacy settings
    python configure_privacy.py --zrm on      # no PII stored by ElevenLabs
    python configure_privacy.py --zrm off     # restore normal retention

Zero Retention Mode is a per-agent setting (platform_settings.privacy), not a
workspace one. Toggling it in the dashboard requires publishing the agent for it
to apply -- this script writes it through the API so there is no draft to forget.

Zero Retention Mode and voice recording are mutually exclusive -- the API rejects
the pair. Enabling ZRM here also sets record_voice to False.

What you lose while it is on: stored transcripts and the per-turn latency metrics
that analyze_call.py reads. Use post-call webhooks, or the browser event log in
the local web UI, to keep any record of a call.
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
        raise Fail("HTTP %d on %s %s\n    %s"
                   % (exc.code, method, path,
                      exc.read().decode("utf-8", "replace")[:500])) from exc
    except urllib.error.URLError as exc:
        raise Fail("Could not reach the ElevenLabs API: %s" % (exc.reason,)) from exc


def get_agent() -> dict:
    if not AGENT_ID:
        raise Fail("ELEVENLABS_AGENT_ID is missing from .env")
    return elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))


def show(agent: dict | None = None) -> dict:
    agent = agent or get_agent()
    privacy = (agent.get("platform_settings") or {}).get("privacy") or {}
    zrm = privacy.get("zero_retention_mode")

    llm = ((agent.get("conversation_config") or {}).get("agent") or {}).get("prompt", {}).get("llm")
    days = privacy.get("retention_days")
    days_note = {-1: "  (kept forever)", 0: "  (scheduled deletion)"}.get(days, "  days")
    redaction = privacy.get("conversation_history_redaction") or {}

    print("  agent               : %s" % agent.get("name"))
    print("  llm                 : %s%s" % (
        llm, "   <-- blocks zero retention mode" if llm == "custom-llm" else ""))
    print("  zero_retention_mode : %s%s" % (zrm, "   <-- no PII stored" if zrm else ""))
    print("  retention_days      : %s%s" % (days, days_note))
    print("  record_voice        : %s%s" % (
        privacy.get("record_voice"), "   <-- call audio is stored" if privacy.get("record_voice") else ""))
    print("  delete_audio        : %s" % privacy.get("delete_audio"))
    print("  delete_transcript   : %s" % privacy.get("delete_transcript_and_pii"))
    print("  pii_redaction       : %s %s" % (
        redaction.get("enabled"), redaction.get("entities") or ""))
    return privacy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent privacy / retention settings.",
        epilog="Zero Retention Mode is refused while the agent uses a custom LLM. "
               "--record-voice off and --retention-days work either way.")
    parser.add_argument("--zrm", choices=["on", "off"],
                        help="Zero Retention Mode (incompatible with custom LLM)")
    parser.add_argument("--record-voice", choices=["on", "off"],
                        help="whether ElevenLabs stores call audio")
    parser.add_argument("--retention-days", type=int, metavar="N",
                        help="delete conversations after N days (-1 keeps forever, 0 schedules deletion)")
    parser.add_argument("--apply-to-existing", action="store_true",
                        help="also schedule deletion for conversations already recorded "
                             "(IRREVERSIBLE)")
    args = parser.parse_args()

    try:
        print("\nBefore")
        privacy = show()

        # The settings that do not require Zero Retention Mode.
        simple = {}
        if args.record_voice:
            simple["record_voice"] = args.record_voice == "on"
        if args.retention_days is not None:
            simple["retention_days"] = args.retention_days
            # retention_days is only a deadline. Without these two flags nothing
            # is actually removed when it expires.
            if args.retention_days >= 0:
                simple["delete_transcript_and_pii"] = True
                simple["delete_audio"] = True
        if args.apply_to_existing:
            simple["apply_to_existing_conversations"] = True

        if simple:
            updated = dict(privacy)
            updated.update(simple)
            print("\nApplying")
            for k, v in simple.items():
                print("  %-19s -> %s" % (k, v))
            elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                       {"platform_settings": {"privacy": updated}})
            print("\nAfter")
            after = show()
            for k, v in simple.items():
                if after.get(k) != v:
                    raise Fail("%s did not stick -- still %s" % (k, after.get(k)))
            print("")
            if not args.zrm:
                return 0
            privacy = after

        if not args.zrm:
            print("\n  --record-voice off      stop storing call audio")
            print("  --retention-days 30     auto-delete conversations after 30 days")
            print("  --zrm on                full Zero Retention Mode (needs a built-in LLM)\n")
            return 0

        want = args.zrm == "on"
        if privacy.get("zero_retention_mode") == want:
            print("\n  Already %s. Nothing to do.\n" % args.zrm)
            return 0

        llm = ((get_agent().get("conversation_config") or {}).get("agent") or {}) \
            .get("prompt", {}).get("llm")
        if want and llm == "custom-llm":
            raise Fail(
                "ElevenLabs refuses Zero Retention Mode while this agent uses a custom LLM\n"
                "    (custom_llm_not_allowed_in_zrm). It cannot vouch for what your own\n"
                "    model server does with the prompts it receives.\n\n"
                "    Either keep the custom LLM and tighten retention instead:\n"
                "        python configure_privacy.py --record-voice off --retention-days 30\n"
                "    or move to a built-in model first:\n"
                "        python configure_llm.py --revert qwen35-397b-a17b")

        # Send the whole privacy block back with the one field changed, so no
        # sibling setting is dropped by a partial write.
        updated = dict(privacy)
        updated["zero_retention_mode"] = want

        print("\nApplying")
        print("  zero_retention_mode -> %s" % want)

        # The API rejects the pair outright: "Cannot enable both zero retention
        # mode and voice recording". Storing call audio is by definition storing
        # PII, so turning ZRM on means turning recording off.
        if want and updated.get("record_voice"):
            updated["record_voice"] = False
            print("  record_voice        -> False   (required: ZRM forbids stored audio)")
        elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                   {"platform_settings": {"privacy": updated}})

        print("\nAfter")
        after = show()
        if after.get("zero_retention_mode") != want:
            raise Fail("The change did not stick -- still %s"
                       % after.get("zero_retention_mode"))

        if want:
            print("\n  ElevenLabs will no longer store transcripts or per-turn metrics.")
            print("  analyze_call.py will report what is missing rather than show blanks.")
            print("  Use the browser Event log, or a post-call webhook, to keep records.")
        print("")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
